"""
IHG Calendar Price Fetcher - Playwright 永久自动化版本

核心思路:
- 用 Playwright 启动真实 Chromium (绕过 Akamai 反爬)
- 在浏览器页面的 JS 上下文中执行 fetch 调用 API (自动带全部 cookie)
- 持久化 session (launch_persistent_context), 重复运行更快
- 完全无人值守, 适合 cron 定时任务

依赖安装:
    pip install playwright
    playwright install chromium

使用:
    python ihg_playwright_fetcher.py

定时任务 (crontab -e):
    0 3 * * *  cd /path/to/script && /usr/bin/python3 ihg_playwright_fetcher.py >> ihg.log 2>&1
"""

import asyncio
import csv
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from playwright.async_api import async_playwright, BrowserContext, Page


# ============ 配置区域 ============

# 要查询的酒店代码列表
HOTEL_CODES = [
    "BKKHB",   # InterContinental Bangkok
    # "SFOHA",
    # "NYCHA",
    # 添加更多...
]

# 滑动窗口配置
DAYS_AHEAD = 365            # 获取从明天起未来多少天
WINDOW_SIZE_DAYS = 60       # 每次请求的日期窗口大小 (IHG 上限 ~60 天)

# 住宿
LENGTH_OF_STAY = 1
ADULTS = 1

# API Key
API_KEY = "se9ym5iAzaW8pxfBjkmgbuGjJcr3Pj6Y"

# 积分 Rate Plan Codes
POINTS_RATE_PLAN_CODES = ["IVAN1", "IVAN3", "IVAN5", "IVAN6", "IVAN7", "IVANI"]

# 请求间隔 (毫秒), 避免被限流
REQUEST_DELAY_MS = 2000

# 浏览器设置
HEADLESS = True                     # True=后台静默, False=显示浏览器 (首次调试建议改 False)
USER_DATA_DIR = "./ihg_browser_profile"   # 持久化 session 目录 (cookie/localStorage 保存在这)

# 输出文件
OUTPUT_CSV = "ihg_prices.csv"
OUTPUT_JSON = "ihg_prices.json"
OUTPUT_RAW = "ihg_raw_snapshots.json"

# 种子 URL: 访问这个页面来建立 session
# 必须是 select-roomrate 格式的 URL, 否则 API 请求会返回 50027
SEED_URL_TEMPLATE = (
    "https://www.ihg.com/intercontinental/hotels/us/en/find-hotels/select-roomrate"
    "?fromRedirect=true&qSrt=sBR&qSlH={hotel_code}&qRms=1&qAdlt=1&qChld=0"
    "&qCiD={ci_day:02d}&qCiMy={ci_month:02d}{ci_year}"
    "&qCoD={co_day:02d}&qCoMy={co_month:02d}{co_year}"
    "&setPMCookies=true&qSHBrC=IC&qpMn=0&srb_u=1&qRmFltr="
)

# ============ 配置结束 ============


def build_seed_url(hotel_code: str) -> str:
    """为某个酒店构造 seed URL (用未来日期避免 50027 错误)"""
    ci = date.today() + timedelta(days=30)
    co = ci + timedelta(days=1)
    return SEED_URL_TEMPLATE.format(
        hotel_code=hotel_code,
        ci_day=ci.day, ci_month=ci.month, ci_year=ci.year,
        co_day=co.day, co_month=co.month, co_year=co.year,
    )


def iter_date_windows(
    start_date: date, total_days: int, window_size: int
) -> List[Tuple[str, str]]:
    """生成滑动窗口日期区间"""
    windows = []
    current = start_date
    end_target = start_date + timedelta(days=total_days - 1)

    while current <= end_target:
        window_end = min(current + timedelta(days=window_size - 1), end_target)
        windows.append((current.isoformat(), window_end.isoformat()))
        current = window_end + timedelta(days=1)
    return windows


async def fetch_via_browser(
    page: Page,
    hotel_code: str,
    start_date: str,
    end_date: str,
    points_mode: bool,
) -> Optional[dict]:
    """
    在浏览器页面上下文中执行 fetch API 调用
    请求会自动携带浏览器里所有的 cookie, 完美绕过 Akamai 反爬
    """
    payload: Dict = {
        "hotelMnemonics": [hotel_code],
        "startDate": start_date,
        "endDate": end_date,
        "lengthOfStay": LENGTH_OF_STAY,
        "guestCounts": [{"otaCode": "AQC10", "count": ADULTS}],
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

    mode_str = "积分" if points_mode else "现金"
    tag = f"[{hotel_code} {start_date}~{end_date} {mode_str}]"

    # 把 fetch 调用放在页面上下文里执行
    # 这样浏览器自动带上所有 cookie (包括 Akamai 的 _abck, bm_sz 等)
    js_code = """
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
    """

    try:
        result = await page.evaluate(js_code, {"apiKey": API_KEY, "payload": payload})
    except Exception as e:
        print(f"  [-] {tag} 页面执行失败: {e}")
        return None

    if result.get("ok") and result.get("data"):
        print(f"  [+] {tag} 成功")
        return result["data"]
    else:
        status = result.get("status", "?")
        err = result.get("error") or result.get("text") or result.get("data")
        print(f"  [-] {tag} 失败 HTTP {status}: {str(err)[:200]}")
        return None


def parse_cash_response(response_data: dict) -> List[dict]:
    """解析现金价格: data.hotels[].calendar[].lowestRate.totalAmount"""
    results = []
    if not response_data:
        return results

    for hotel_entry in response_data.get("data", {}).get("hotels", []):
        info = hotel_entry.get("hotel", {})
        hotel_code = info.get("hotelMnemonic", "UNKNOWN")
        brand_code = info.get("brandCode", "")
        currency_fallback = info.get("propertyCurrency", "")

        for day_data in hotel_entry.get("calendar", []):
            lr = day_data.get("lowestRate")
            price = None
            currency = currency_fallback
            if lr:
                try:
                    price = float(lr.get("totalAmount", 0)) or None
                except (ValueError, TypeError):
                    price = None
                currency = lr.get("currency", currency_fallback)

            results.append({
                "hotel_code": hotel_code,
                "brand_code": brand_code,
                "date": day_data.get("start", ""),
                "cash_price": price,
                "cash_currency": currency,
            })
    return results


def parse_points_response(response_data: dict) -> List[dict]:
    """解析积分价格: data.hotels[].calendar[].offers[].totalPoints (IVAN* / isRewardNight)"""
    results = []
    if not response_data:
        return results

    for hotel_entry in response_data.get("data", {}).get("hotels", []):
        info = hotel_entry.get("hotel", {})
        hotel_code = info.get("hotelMnemonic", "UNKNOWN")
        brand_code = info.get("brandCode", "")

        reward_codes = {
            rp.get("code", "")
            for rp in hotel_entry.get("ratePlans", [])
            if rp.get("isRewardNight")
        }

        for day_data in hotel_entry.get("calendar", []):
            lowest = None
            for offer in day_data.get("offers", []):
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
                "hotel_code": hotel_code,
                "brand_code": brand_code,
                "date": day_data.get("start", ""),
                "points_price": lowest,
            })
    return results


def merge_prices(cash_prices: List[dict], points_prices: List[dict]) -> List[dict]:
    """合并现金 + 积分到每日一行, 并计算 CPP (每积分价值)"""
    cash_map = {(r["hotel_code"], r["date"]): r for r in cash_prices}
    pts_map = {(r["hotel_code"], r["date"]): r for r in points_prices}

    all_keys = set(cash_map.keys()) | set(pts_map.keys())
    merged = []

    for key in sorted(all_keys):
        hotel_code, date_str = key
        c = cash_map.get(key, {})
        p = pts_map.get(key, {})
        cash_price = c.get("cash_price")
        points_price = p.get("points_price")

        cpp = None
        if cash_price and points_price and points_price > 0:
            cpp = round(cash_price / points_price * 100, 4)

        merged.append({
            "hotel_code": hotel_code,
            "brand_code": c.get("brand_code") or p.get("brand_code") or "",
            "date": date_str,
            "cash_price": cash_price,
            "cash_currency": c.get("cash_currency", ""),
            "points_price": points_price,
            "cents_per_point": cpp,
        })
    return merged


def export_csv(data: List[dict], filename: str):
    if not data:
        return
    fieldnames = [
        "hotel_code", "brand_code", "date",
        "cash_price", "cash_currency", "points_price", "cents_per_point"
    ]
    with open(filename, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(data)
    print(f"[+] 已导出 CSV: {filename} ({len(data)} 条)")


def export_json(data, filename: str):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"[+] 已导出 JSON: {filename}")


async def wait_for_valid_session(page: Page, hotel_code: str, max_attempts: int = 3) -> bool:
    """
    访问种子页面建立合法 session
    通过"预发一个 API 请求"检查 session 是否可用
    """
    for attempt in range(max_attempts):
        seed_url = build_seed_url(hotel_code)
        print(f"[*] 访问种子页面建立 session (尝试 {attempt + 1}/{max_attempts})...")
        print(f"    URL: {seed_url[:100]}...")

        try:
            await page.goto(seed_url, wait_until="domcontentloaded", timeout=60000)
            # 等待一下, 让前端 JS 加载完成 + Akamai cookie 就位
            await page.wait_for_timeout(5000)
        except Exception as e:
            print(f"    [!] 页面加载异常: {e}")

        # 预测试: 发一个小范围请求验证 session
        test_start = (date.today() + timedelta(days=1)).isoformat()
        test_end = (date.today() + timedelta(days=7)).isoformat()
        test_result = await fetch_via_browser(
            page, hotel_code, test_start, test_end, points_mode=False
        )

        if test_result and test_result.get("data", {}).get("hotels"):
            print(f"[✓] Session 有效")
            return True

        print(f"[!] Session 无效, 等待后重试...")
        await page.wait_for_timeout(3000)

    return False


async def run():
    print("=" * 60)
    print("IHG Playwright Fetcher - 永久自动化全量抓取")
    print("=" * 60)

    # 滑动窗口日期
    start = date.today() + timedelta(days=1)
    windows = iter_date_windows(start, DAYS_AHEAD, WINDOW_SIZE_DAYS)

    print(f"\n配置:")
    print(f"  酒店数: {len(HOTEL_CODES)}")
    print(f"  日期: {start.isoformat()} 起 {DAYS_AHEAD} 天 → {len(windows)} 个窗口")
    print(f"  预计请求数: {len(HOTEL_CODES) * len(windows) * 2} (现金+积分)")
    print(f"  Headless: {HEADLESS}")
    print(f"  Session 目录: {USER_DATA_DIR}")

    Path(USER_DATA_DIR).mkdir(parents=True, exist_ok=True)

    all_cash = []
    all_points = []
    raw_samples = []

    async with async_playwright() as p:
        # 持久化 context: cookie/storage 保存在 USER_DATA_DIR, 下次运行复用
        context: BrowserContext = await p.chromium.launch_persistent_context(
            USER_DATA_DIR,
            headless=HEADLESS,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )

        # 隐藏 webdriver 痕迹 (Akamai 会检测这个)
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        """)

        page = await context.new_page()

        try:
            # 为每个酒店抓数据
            for hi, hotel_code in enumerate(HOTEL_CODES):
                print(f"\n{'=' * 50}")
                print(f"[酒店 {hi + 1}/{len(HOTEL_CODES)}] {hotel_code}")
                print('=' * 50)

                # 为该酒店建立 session (必要时重新导航)
                ok = await wait_for_valid_session(page, hotel_code)
                if not ok:
                    print(f"[-] 酒店 {hotel_code} 无法建立有效 session, 跳过")
                    continue

                for wi, (ws, we) in enumerate(windows):
                    print(f"\n  --- 窗口 {wi + 1}/{len(windows)} ({ws} ~ {we}) ---")

                    # 现金请求
                    cash_data = await fetch_via_browser(page, hotel_code, ws, we, points_mode=False)
                    if cash_data:
                        all_cash.extend(parse_cash_response(cash_data))

                    await page.wait_for_timeout(REQUEST_DELAY_MS)

                    # 积分请求
                    pts_data = await fetch_via_browser(page, hotel_code, ws, we, points_mode=True)
                    if pts_data:
                        all_points.extend(parse_points_response(pts_data))

                    # 保存第一个窗口的原始响应, 用于调试
                    if wi == 0:
                        if cash_data:
                            raw_samples.append({
                                "hotel": hotel_code, "window": f"{ws}~{we}",
                                "type": "cash", "data": cash_data
                            })
                        if pts_data:
                            raw_samples.append({
                                "hotel": hotel_code, "window": f"{ws}~{we}",
                                "type": "points", "data": pts_data
                            })

                    # 窗口间间隔
                    is_last = (hi == len(HOTEL_CODES) - 1) and (wi == len(windows) - 1)
                    if not is_last:
                        await page.wait_for_timeout(REQUEST_DELAY_MS)

        finally:
            await context.close()

    # 合并 & 导出
    merged = merge_prices(all_cash, all_points)

    print("\n" + "=" * 60)
    print(f"完成! 共 {len(merged)} 条每日价格记录")
    print("=" * 60)

    if merged:
        print(f"\n价格预览 (前 10 条):")
        print(f"  {'酒店':<8} {'日期':<12} {'现金价':<10} {'货币':<6} {'积分价':<10} {'CPP'}")
        print(f"  {'-'*8} {'-'*12} {'-'*10} {'-'*6} {'-'*10} {'-'*6}")
        for row in merged[:10]:
            cash_str = f"{row['cash_price']:.2f}" if row['cash_price'] else "N/A"
            pts_str = f"{int(row['points_price'])}" if row['points_price'] else "N/A"
            cpp_str = f"{row['cents_per_point']}" if row['cents_per_point'] is not None else "N/A"
            print(f"  {row['hotel_code']:<8} {row['date']:<12} {cash_str:<10} "
                  f"{row['cash_currency']:<6} {pts_str:<10} {cpp_str}")

        export_csv(merged, OUTPUT_CSV)
        export_json(merged, OUTPUT_JSON)

    if raw_samples:
        export_json(raw_samples, OUTPUT_RAW)

    return len(merged)


if __name__ == "__main__":
    try:
        count = asyncio.run(run())
        sys.exit(0 if count > 0 else 1)
    except KeyboardInterrupt:
        print("\n用户中断")
        sys.exit(130)
    except Exception as e:
        print(f"\n[!] 运行失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
