"""
IHG 价格变动监控 - 全量对比, 发现新开放/涨价/降价

逻辑:
    1. 读取上次保存的价格文件 (ihg_prices_<code>.json)
    2. 重新获取全量 365 天现金+积分价格
    3. 逐日对比: 新开放 / 现金涨降 / 积分涨降
    4. 输出变化列表
    5. 保存最新数据覆盖旧文件

用法:
    # 监控单个酒店 (会自动读取 ihg_prices_HPHHL.json 作为基线)
    python ihg_price_monitor.py --code HPHHL

    # 指定基线文件
    python ihg_price_monitor.py --code HPHHL --baseline ihg_prices_HPHHL.json

    # 只显示变化, 不重新保存
    python ihg_price_monitor.py --code HPHHL --dry-run
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
WINDOW_SIZE_DAYS = 62
REQUEST_DELAY_MS = 2000


# ============ 工具函数 (复用 ihg_test_calendar_price.py) ============

def iter_date_windows(start_date, total_days, window_size):
    windows = []
    current = start_date
    end_target = start_date + timedelta(days=total_days - 1)
    while current <= end_target:
        window_end = min(current + timedelta(days=window_size - 1), end_target)
        windows.append((current.isoformat(), window_end.isoformat()))
        current = window_end + timedelta(days=1)
    return windows


def expand_date_range(start_str, end_str):
    start = date.fromisoformat(start_str)
    end = date.fromisoformat(end_str)
    dates = []
    current = start
    while current <= end:
        dates.append(current.isoformat())
        current += timedelta(days=1)
    return dates


async def fetch_calendar(page, hotel_code, start_date, end_date, points_mode=False):
    if points_mode:
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
            "rates": {"ratePlanCodes": POINTS_RATE_PLAN_CODES},
        }
    else:
        payload = {
            "hotelMnemonics": [hotel_code],
            "startDate": start_date,
            "endDate": end_date,
            "lengthOfStay": 1,
            "guestCounts": [
                {"otaCode": "AQC10", "count": 1},
                {"otaCode": "AQC8", "count": 0},
            ],
            "options": {
                "identifyLowestOfferPerRatePlan": True,
                "returnAmountsAfterTaxForLowestOffer": True,
                "lowestOfferPerRatePlan": True,
                "returnAverages": True,
            },
        }

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
            return { status: resp.status, ok: resp.ok, data: json };
        } catch (err) {
            return { status: 0, ok: false, error: err.message };
        }
    }
    """, {"apiKey": API_KEY, "payload": payload})

    if result.get("ok") and result.get("data"):
        return result["data"]
    return None


def parse_cash(response_data):
    results = []
    if not response_data:
        return results
    for hotel in response_data.get("data", {}).get("hotels", []):
        currency = hotel.get("hotel", {}).get("propertyCurrency", "")
        for day in hotel.get("calendar", []):
            lr = day.get("lowestRate")
            offers = day.get("offers", [])
            price = None
            price_after_tax = None
            cur = currency
            if lr:
                cur = lr.get("currency", currency)
                try:
                    price = float(lr.get("totalAmount", 0)) or None
                except (ValueError, TypeError):
                    price = None
                ref_ids = lr.get("refIds", [])
                if ref_ids and offers:
                    target_id = ref_ids[0]
                    for offer in offers:
                        if offer.get("id") == target_id:
                            try:
                                price_after_tax = float(offer.get("totalAmountAfterFeeTax", 0)) or None
                            except (ValueError, TypeError):
                                pass
                            break
            start_d = day.get("start", "")
            end_d = day.get("end", start_d)
            if start_d:
                for d in expand_date_range(start_d, end_d):
                    results.append({"date": d, "cash_price": price, "cash_price_after_tax": price_after_tax, "currency": cur})
    return results


def parse_points(response_data):
    results = []
    if not response_data:
        return results
    for hotel in response_data.get("data", {}).get("hotels", []):
        reward_codes = {rp.get("code", "") for rp in hotel.get("ratePlans", []) if rp.get("isRewardNight")}
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
            start_d = day.get("start", "")
            end_d = day.get("end", start_d)
            if start_d:
                for d in expand_date_range(start_d, end_d):
                    results.append({"date": d, "points": int(lowest) if lowest else None})
    return results


# ============ 对比逻辑 ============

def compare_prices(old_prices, new_prices):
    """对比新旧价格, 返回变化列表"""
    old_map = {p["date"]: p for p in old_prices}
    new_map = {p["date"]: p for p in new_prices}

    changes = []
    all_dates = sorted(set(old_map.keys()) | set(new_map.keys()))

    for d in all_dates:
        old = old_map.get(d, {})
        new = new_map.get(d, {})

        old_cash = old.get("cash_price")
        new_cash = new.get("cash_price")
        old_tax = old.get("cash_price_after_tax")
        new_tax = new.get("cash_price_after_tax")
        old_pts = old.get("points")
        new_pts = new.get("points")

        # 新开放的日期
        if not old and new:
            changes.append({
                "date": d, "type": "新开放",
                "cash_price": new_cash, "cash_after_tax": new_tax,
                "points": new_pts, "detail": "新日期"
            })
            continue

        # 现金价格变化
        if old_cash and new_cash and old_cash != new_cash:
            diff = new_cash - old_cash
            pct = (diff / old_cash) * 100
            changes.append({
                "date": d, "type": "现金↑" if diff > 0 else "现金↓",
                "old_value": old_cash, "new_value": new_cash,
                "diff": diff, "pct": pct,
                "currency": new.get("currency", "")
            })

        # 积分价格变化
        if old_pts and new_pts and old_pts != new_pts:
            diff = new_pts - old_pts
            pct = (diff / old_pts) * 100
            changes.append({
                "date": d, "type": "积分↑" if diff > 0 else "积分↓",
                "old_value": old_pts, "new_value": new_pts,
                "diff": diff, "pct": pct
            })

        # 原来没有现金, 现在有了
        if not old_cash and new_cash:
            changes.append({
                "date": d, "type": "现金新增",
                "cash_price": new_cash, "cash_after_tax": new_tax,
                "currency": new.get("currency", "")
            })

        # 原来没有积分, 现在有了
        if not old_pts and new_pts:
            changes.append({
                "date": d, "type": "积分新增",
                "points": new_pts
            })

    return changes


# ============ 主逻辑 ============

async def main():
    parser = argparse.ArgumentParser(description="IHG 价格变动监控")
    parser.add_argument("--code", type=str, required=True, help="酒店代码")
    parser.add_argument("--baseline", type=str, default=None,
                        help="基线文件 (默认 ihg_prices_<code>.json)")
    parser.add_argument("--days", type=int, default=365, help="查询天数 (默认 365)")
    parser.add_argument("--dry-run", action="store_true", help="只显示变化, 不保存")
    args = parser.parse_args()

    hotel_code = args.code.upper()
    baseline_file = args.baseline or f"ihg_prices_{hotel_code}.json"
    output_file = f"ihg_prices_{hotel_code}.json"

    print("=" * 75)
    print(f"IHG 价格变动监控")
    print(f"  酒店代码: {hotel_code}")
    print(f"  基线文件: {baseline_file}")
    print(f"  查询天数: {args.days}")
    print("=" * 75)

    # 读取基线数据
    old_prices = []
    if Path(baseline_file).exists():
        try:
            with open(baseline_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            old_prices = data.get("prices", [])
            old_date = data.get("fetch_date", "未知")
            print(f"\n  基线: {len(old_prices)} 天 (获取于 {old_date})")
        except Exception as e:
            print(f"\n  [!] 读取基线失败: {e}")
    else:
        print(f"\n  [!] 基线文件不存在, 将作为首次运行")

    # 获取最新价格
    start = date.today() + timedelta(days=1)
    windows = iter_date_windows(start, args.days, WINDOW_SIZE_DAYS)
    print(f"  请求: {start} ~ {start + timedelta(days=args.days - 1)} ({len(windows)}×2 次)")

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
            print(f"\n[1] 建立 session...")
            await page.goto("https://www.ihg.com/hotels/us/en/find-hotels/hotel/rates",
                            wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)
            print("    ✓ OK")

            print(f"[2] 获取现金价格...", end="", flush=True)
            all_cash = []
            for idx, (ws, we) in enumerate(windows, 1):
                resp = await fetch_calendar(page, hotel_code, ws, we, points_mode=False)
                all_cash.extend(parse_cash(resp))
                print(f" {idx}", end="", flush=True)
                await page.wait_for_timeout(REQUEST_DELAY_MS)
            print(f" ✓ ({len(all_cash)} 天)")

            print(f"[3] 获取积分价格...", end="", flush=True)
            all_points = []
            for idx, (ws, we) in enumerate(windows, 1):
                resp = await fetch_calendar(page, hotel_code, ws, we, points_mode=True)
                all_points.extend(parse_points(resp))
                print(f" {idx}", end="", flush=True)
                await page.wait_for_timeout(REQUEST_DELAY_MS)
            print(f" ✓ ({len(all_points)} 天)")

        finally:
            await context.close()

    # 合并最新结果
    cash_map = {c["date"]: c for c in all_cash}
    points_map = {p["date"]: p for p in all_points}
    all_dates = sorted(set(cash_map.keys()) | set(points_map.keys()))

    new_prices = []
    for d in all_dates:
        cash = cash_map.get(d, {})
        pts = points_map.get(d, {})
        cash_price = cash.get("cash_price")
        cash_price_after_tax = cash.get("cash_price_after_tax")
        points_price = pts.get("points")
        cpp = None
        price_for_cpp = cash_price_after_tax or cash_price
        if price_for_cpp and points_price and points_price > 0:
            cpp = round(price_for_cpp / points_price * 100, 2)
        new_prices.append({
            "date": d,
            "cash_price": cash_price,
            "cash_price_after_tax": cash_price_after_tax,
            "currency": cash.get("currency", ""),
            "points": points_price,
            "cpp": cpp,
        })

    # 对比差异
    print(f"\n[4] 对比变化...")
    changes = compare_prices(old_prices, new_prices)

    # 输出变化
    if changes:
        new_days = [c for c in changes if c["type"] == "新开放"]
        cash_up = [c for c in changes if c["type"] == "现金↑"]
        cash_down = [c for c in changes if c["type"] == "现金↓"]
        pts_up = [c for c in changes if c["type"] == "积分↑"]
        pts_down = [c for c in changes if c["type"] == "积分↓"]
        cash_new = [c for c in changes if c["type"] == "现金新增"]
        pts_new = [c for c in changes if c["type"] == "积分新增"]

        print(f"\n{'='*75}")
        print(f"价格变动 ({hotel_code}) | 基线 → 最新")
        print(f"{'='*75}")

        if new_days:
            print(f"\n  [新开放] {len(new_days)} 天:")
            for c in new_days:
                cash_str = f"现金 {c.get('cash_price', '-')}" if c.get('cash_price') else ""
                pts_str = f"积分 {c.get('points', '-')}" if c.get('points') else ""
                print(f"    {c['date']}  {cash_str}  {pts_str}")

        if cash_down:
            print(f"\n  [现金降价] {len(cash_down)} 天:")
            for c in cash_down:
                print(f"    {c['date']}  {c['old_value']:.2f} → {c['new_value']:.2f}  {c['diff']:+.2f} ({c['pct']:+.1f}%) {c.get('currency','')}")

        if cash_up:
            print(f"\n  [现金涨价] {len(cash_up)} 天:")
            for c in cash_up:
                print(f"    {c['date']}  {c['old_value']:.2f} → {c['new_value']:.2f}  {c['diff']:+.2f} ({c['pct']:+.1f}%) {c.get('currency','')}")

        if pts_down:
            print(f"\n  [积分降价] {len(pts_down)} 天:")
            for c in pts_down:
                print(f"    {c['date']}  {c['old_value']} → {c['new_value']}  {c['diff']:+.0f} ({c['pct']:+.1f}%)")

        if pts_up:
            print(f"\n  [积分涨价] {len(pts_up)} 天:")
            for c in pts_up:
                print(f"    {c['date']}  {c['old_value']} → {c['new_value']}  {c['diff']:+.0f} ({c['pct']:+.1f}%)")

        if cash_new:
            print(f"\n  [现金新增] {len(cash_new)} 天 (之前无现金价, 现在有了):")
            for c in cash_new[:10]:
                print(f"    {c['date']}  {c.get('cash_price', '')} {c.get('currency', '')}")

        if pts_new:
            print(f"\n  [积分新增] {len(pts_new)} 天 (之前无积分价, 现在有了):")
            for c in pts_new[:10]:
                print(f"    {c['date']}  {c.get('points', '')}")

        print(f"\n{'='*75}")
        print(f"汇总: 新开放 {len(new_days)} | 现金↓ {len(cash_down)} | 现金↑ {len(cash_up)} | 积分↓ {len(pts_down)} | 积分↑ {len(pts_up)}")
        print(f"{'='*75}")
    else:
        print(f"\n  ✓ 无价格变动")

    # 保存最新数据
    if not args.dry_run:
        result = {
            "hotel_code": hotel_code,
            "fetch_date": date.today().isoformat(),
            "days_queried": args.days,
            "summary": {
                "total_days": len(new_prices),
                "has_cash": sum(1 for p in new_prices if p["cash_price"]),
                "has_points": sum(1 for p in new_prices if p["points"]),
                "changes": len(changes),
            },
            "prices": new_prices,
        }
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print(f"\n[+] 最新数据已保存: {output_file}")
    else:
        print(f"\n[dry-run] 未保存")


if __name__ == "__main__":
    asyncio.run(main())
