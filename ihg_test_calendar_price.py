"""
IHG 日历价格测试 - 单酒店全年现金+积分价格获取

基于 ihg_playwright_fetcher.py 已验证的 API 逻辑:
- Calendar API: POST https://apis.ihg.com/availability/v1/calendar
- 现金价格: 不带 rates 字段 → data.hotels[].calendar[].lowestRate.totalAmount
- 积分价格: 带 rates.ratePlanCodes → data.hotels[].calendar[].offers[].totalPoints
- 必须分两次请求 (合并不返回现金价)
- startDate ≥ 明天
- 单次最多 ~60 天, 用滑动窗口覆盖 365 天
- 用 Playwright 真实浏览器绕过 Akamai

用法:
    # 测试单个酒店 (默认 BKKHB)
    python ihg_test_calendar_price.py --code BKKHB

    # 从酒店列表文件中取第一个
    python ihg_test_calendar_price.py --from-json ihg_test_result.json

    # 指定天数
    python ihg_test_calendar_price.py --code SGNVC --days 90
"""

import argparse
import asyncio
import json
from datetime import date, timedelta
from pathlib import Path
from playwright.async_api import async_playwright


# ============ 配置 ============

USER_DATA_DIR = "./ihg_browser_profile"
API_KEY = "se9ym5iAzaW8pxfBjkmgbuGjJcr3Pj6Y"
POINTS_RATE_PLAN_CODES = ["IVAN1", "IVAN3", "IVAN5", "IVAN6", "IVAN7", "IVANI"]

# 滑动窗口
WINDOW_SIZE_DAYS = 60
REQUEST_DELAY_MS = 2000

# Seed URL (用于建立浏览器 session)
SEED_URL = "https://www.ihg.com/hotels/us/en/find-hotels/hotel/rates"


# ============ 工具函数 ============

def iter_date_windows(start_date, total_days, window_size):
    """生成滑动窗口日期区间"""
    windows = []
    current = start_date
    end_target = start_date + timedelta(days=total_days - 1)
    while current <= end_target:
        window_end = min(current + timedelta(days=window_size - 1), end_target)
        windows.append((current.isoformat(), window_end.isoformat()))
        current = window_end + timedelta(days=1)
    return windows


async def fetch_calendar(page, hotel_code, start_date, end_date, points_mode=False):
    """在浏览器上下文中调用 Calendar API"""
    payload = {
        "hotelMnemonics": [hotel_code],
        "startDate": start_date,
        "endDate": end_date,
        "lengthOfStay": 1,
        "guestCounts": [{"otaCode": "AQC10", "count": 1}],
        "options": {
            "includeSellStrategy": "followChannel",
            "returnAmountsAfterTaxForLowestOffer": True,
            "returnAverages": True,
            "lowestOfferPerRatePlan": True,
            "identifyLowestOfferPerRatePlan": True,
        },
    }
    if points_mode:
        payload["rates"] = {"ratePlanCodes": POINTS_RATE_PLAN_CODES}

    result = await page.evaluate("""
    async ({ apiKey, payload }) => {
        const uuid = () => 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
            const r = Math.random() * 16 | 0;
            const v = c === 'x' ? r : (r & 3 | 8);
            return v.toString(16);
        });
        try {
            const resp = await fetch("https://apis.ihg.com/availability/v1/calendar", {
                method: "POST",
                headers: {
                    "accept": "application/json, text/plain, */*",
                    "content-type": "application/json; charset=UTF-8",
                    "ihg-language": "en-US",
                    "ihg-sessionid": uuid(),
                    "ihg-transactionid": uuid(),
                    "x-ihg-api-key": apiKey,
                },
                body: JSON.stringify(payload),
                credentials: "include",
            });
            const text = await resp.text();
            let json = null;
            try { json = JSON.parse(text); } catch (e) {}
            return { status: resp.status, ok: resp.ok, data: json, text: json ? null : text.slice(0, 500) };
        } catch (err) {
            return { status: 0, ok: false, error: err.message };
        }
    }
    """, {"apiKey": API_KEY, "payload": payload})

    if result.get("ok") and result.get("data"):
        return result["data"]
    else:
        status = result.get("status", "?")
        err = result.get("error") or result.get("text", "")
        print(f"    [!] API 失败 HTTP {status}: {str(err)[:200]}")
        return None


def parse_cash(response_data):
    """解析现金价格响应"""
    results = []
    if not response_data:
        return results
    for hotel in response_data.get("data", {}).get("hotels", []):
        currency = hotel.get("hotel", {}).get("propertyCurrency", "")
        for day in hotel.get("calendar", []):
            lr = day.get("lowestRate")
            price = None
            cur = currency
            if lr:
                try:
                    price = float(lr.get("totalAmount", 0)) or None
                except (ValueError, TypeError):
                    price = None
                cur = lr.get("currency", currency)
            results.append({
                "date": day.get("start", ""),
                "cash_price": price,
                "currency": cur,
            })
    return results


def parse_points(response_data):
    """解析积分价格响应"""
    results = []
    if not response_data:
        return results
    for hotel in response_data.get("data", {}).get("hotels", []):
        # 找出哪些 ratePlan 是积分兑换
        reward_codes = {
            rp.get("code", "")
            for rp in hotel.get("ratePlans", [])
            if rp.get("isRewardNight")
        }
        for day in hotel.get("calendar", []):
            lowest = None
            for offer in day.get("offers", []):
                rp = offer.get("ratePlanCode", "")
                if rp in reward_codes or rp.startswith("IVAN"):
                    tp = offer.get("totalPoints")
                    if tp is not None:
                        try:
                            v = float(tp)
                            if lowest is None or v < lowest:
                                lowest = v
                        except (ValueError, TypeError):
                            pass
            results.append({
                "date": day.get("start", ""),
                "points": int(lowest) if lowest else None,
            })
    return results


# ============ 主逻辑 ============

async def main():
    parser = argparse.ArgumentParser(description="IHG 日历价格测试 - 单酒店")
    parser.add_argument("--code", type=str, default="BKKHB",
                        help="酒店代码 (默认 BKKHB)")
    parser.add_argument("--from-json", type=str, default=None,
                        help="从 JSON 文件中读取第一个酒店代码")
    parser.add_argument("--days", type=int, default=365,
                        help="查询天数 (默认 365)")
    parser.add_argument("--output", type=str, default=None,
                        help="输出 JSON 文件名 (默认 ihg_prices_<code>.json)")
    args = parser.parse_args()

    # 确定酒店代码
    hotel_code = args.code.upper()
    if args.from_json:
        try:
            with open(args.from_json, "r", encoding="utf-8") as f:
                data = json.load(f)
            hotels = data.get("hotels", data) if isinstance(data, dict) else data
            if hotels and isinstance(hotels, list):
                hotel_code = hotels[0].get("mnemonic", hotel_code)
                print(f"从 {args.from_json} 读取第一个酒店: {hotel_code}")
        except Exception as e:
            print(f"[!] 读取 JSON 失败: {e}, 使用默认代码 {hotel_code}")

    output_file = args.output or f"ihg_prices_{hotel_code}.json"

    print("=" * 60)
    print(f"IHG 日历价格测试")
    print(f"  酒店代码: {hotel_code}")
    print(f"  查询天数: {args.days}")
    print(f"  输出文件: {output_file}")
    print("=" * 60)

    # 生成日期窗口
    start = date.today() + timedelta(days=1)
    windows = iter_date_windows(start, args.days, WINDOW_SIZE_DAYS)
    print(f"\n  日期范围: {start} ~ {start + timedelta(days=args.days - 1)}")
    print(f"  滑动窗口: {len(windows)} 个 (每个 {WINDOW_SIZE_DAYS} 天)")

    Path(USER_DATA_DIR).mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            USER_DATA_DIR,
            headless=False,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )
        page = await context.new_page()

        try:
            # 建立 session: 先访问 IHG 网站
            print(f"\n[1] 建立浏览器 session...")
            await page.goto("https://www.ihg.com/hotels/us/en/find-hotels/hotel/rates",
                            wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)
            print("    ✓ Session 已建立")

            # 获取现金价格
            print(f"\n[2] 获取现金价格 ({len(windows)} 个窗口)...")
            all_cash = []
            for idx, (ws, we) in enumerate(windows, 1):
                print(f"    [{idx}/{len(windows)}] {ws} ~ {we} (现金)")
                resp = await fetch_calendar(page, hotel_code, ws, we, points_mode=False)
                cash = parse_cash(resp)
                all_cash.extend(cash)
                await page.wait_for_timeout(REQUEST_DELAY_MS)

            print(f"    ✓ 现金: {len(all_cash)} 天, 有价格: {sum(1 for c in all_cash if c['cash_price'])} 天")

            # 获取积分价格
            print(f"\n[3] 获取积分价格 ({len(windows)} 个窗口)...")
            all_points = []
            for idx, (ws, we) in enumerate(windows, 1):
                print(f"    [{idx}/{len(windows)}] {ws} ~ {we} (积分)")
                resp = await fetch_calendar(page, hotel_code, ws, we, points_mode=True)
                pts = parse_points(resp)
                all_points.extend(pts)
                await page.wait_for_timeout(REQUEST_DELAY_MS)

            print(f"    ✓ 积分: {len(all_points)} 天, 有积分: {sum(1 for p in all_points if p['points'])} 天")

        finally:
            await context.close()

    # 合并结果
    print(f"\n[4] 合并结果...")
    cash_map = {c["date"]: c for c in all_cash}
    points_map = {p["date"]: p for p in all_points}
    all_dates = sorted(set(cash_map.keys()) | set(points_map.keys()))

    merged = []
    for d in all_dates:
        cash = cash_map.get(d, {})
        pts = points_map.get(d, {})
        cash_price = cash.get("cash_price")
        points_price = pts.get("points")

        # 计算 CPP (每点价值, 单位: 分)
        cpp = None
        if cash_price and points_price and points_price > 0:
            cpp = round(cash_price / points_price * 100, 2)

        merged.append({
            "date": d,
            "cash_price": cash_price,
            "currency": cash.get("currency", ""),
            "points": points_price,
            "cpp": cpp,
        })

    # 输出统计
    has_cash = sum(1 for m in merged if m["cash_price"])
    has_points = sum(1 for m in merged if m["points"])
    has_both = sum(1 for m in merged if m["cash_price"] and m["points"])

    print(f"\n{'='*60}")
    print(f"结果统计 ({hotel_code}):")
    print(f"  总天数: {len(merged)}")
    print(f"  有现金价: {has_cash} 天")
    print(f"  有积分价: {has_points} 天")
    print(f"  有两者 (可算CPP): {has_both} 天")

    if has_both > 0:
        cpps = [m["cpp"] for m in merged if m["cpp"]]
        avg_cpp = sum(cpps) / len(cpps) if cpps else 0
        min_cpp = min(cpps) if cpps else 0
        max_cpp = max(cpps) if cpps else 0
        print(f"\n  CPP 统计 (每万积分价值):")
        print(f"    平均: {avg_cpp:.2f} 分/点")
        print(f"    最低: {min_cpp:.2f} 分/点")
        print(f"    最高: {max_cpp:.2f} 分/点")

    # 显示前 10 天
    print(f"\n  前 10 天:")
    print(f"  {'日期':12s} | {'现金':>8s} | {'货币':4s} | {'积分':>8s} | {'CPP':>6s}")
    print(f"  {'-'*50}")
    for m in merged[:10]:
        cash_str = f"{m['cash_price']:.0f}" if m["cash_price"] else "-"
        pts_str = f"{m['points']}" if m["points"] else "-"
        cpp_str = f"{m['cpp']:.2f}" if m["cpp"] else "-"
        print(f"  {m['date']:12s} | {cash_str:>8s} | {m['currency']:4s} | {pts_str:>8s} | {cpp_str:>6s}")

    print(f"{'='*60}")

    # 保存
    result = {
        "hotel_code": hotel_code,
        "fetch_date": date.today().isoformat(),
        "days_queried": args.days,
        "summary": {
            "total_days": len(merged),
            "has_cash": has_cash,
            "has_points": has_points,
            "has_both": has_both,
        },
        "prices": merged,
    }
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"\n[+] 已保存: {output_file}")


if __name__ == "__main__":
    asyncio.run(main())
