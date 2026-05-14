"""
IHG Calendar API 调试 - 查看指定日期范围的现金+积分价格
输出简洁的逐日表格, 方便和官网日历对比

用法:
    python ihg_debug_calendar.py --code HPHHL --start 2026-06-01 --end 2026-06-30
    python ihg_debug_calendar.py --code HPHHL --days 60
"""

import argparse
import asyncio
import json
from datetime import date, timedelta
from pathlib import Path
from playwright.async_api import async_playwright

USER_DATA_DIR = "./ihg_browser_profile"
API_KEY = "se9ym5iAzaW8pxfBjkmgbuGjJcr3Pj6Y"
POINTS_RATE_PLAN_CODES = ["IVAN1", "IVAN3", "IVAN5", "IVAN6", "IVAN7", "IVANI"]


def expand_date_range(start_str, end_str):
    """展开日期区间为逐天列表"""
    start = date.fromisoformat(start_str)
    end = date.fromisoformat(end_str)
    dates = []
    current = start
    while current <= end:
        dates.append(current.isoformat())
        current += timedelta(days=1)
    return dates


async def fetch_raw(page, hotel_code, start_date, end_date, points_mode=False):
    """调用 Calendar API"""
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
    return result


def parse_cash_to_map(response_data):
    """解析现金响应, 返回 {date: {price, currency}}"""
    result = {}
    if not response_data or "data" not in response_data:
        return result
    for hotel in response_data["data"].get("hotels", []):
        currency = hotel.get("hotel", {}).get("propertyCurrency", "")
        for day in hotel.get("calendar", []):
            lr = day.get("lowestRate")
            price = None
            cur = currency
            if lr:
                try:
                    price = float(lr.get("totalAmount", 0)) or None
                except (ValueError, TypeError):
                    pass
                cur = lr.get("currency", currency)
            start_d = day.get("start", "")
            end_d = day.get("end", start_d)
            if start_d:
                for d in expand_date_range(start_d, end_d):
                    result[d] = {"price": price, "currency": cur}
    return result


def parse_points_to_map(response_data):
    """解析积分响应, 返回 {date: points}"""
    result = {}
    if not response_data or "data" not in response_data:
        return result
    for hotel in response_data["data"].get("hotels", []):
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
            start_d = day.get("start", "")
            end_d = day.get("end", start_d)
            if start_d:
                for d in expand_date_range(start_d, end_d):
                    result[d] = int(lowest) if lowest else None
    return result


async def main():
    parser = argparse.ArgumentParser(description="IHG Calendar API 调试")
    parser.add_argument("--code", type=str, default="HPHHL", help="酒店代码")
    parser.add_argument("--start", type=str, default=None, help="开始日期 (默认明天)")
    parser.add_argument("--end", type=str, default=None, help="结束日期")
    parser.add_argument("--days", type=int, default=62, help="查询天数 (默认 62)")
    args = parser.parse_args()

    if args.start:
        start_str = args.start
    else:
        start_str = (date.today() + timedelta(days=1)).isoformat()
    if args.end:
        end_str = args.end
    else:
        end_str = (date.fromisoformat(start_str) + timedelta(days=args.days - 1)).isoformat()

    Path(USER_DATA_DIR).mkdir(parents=True, exist_ok=True)

    print(f"酒店: {args.code} | 日期: {start_str} ~ {end_str}")
    print("=" * 60)

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
            print("[1] 建立 session...")
            await page.goto("https://www.ihg.com/hotels/us/en/find-hotels/hotel/rates",
                            wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)
            print("    ✓ OK")

            print(f"[2] 请求现金价格...")
            cash_result = await fetch_raw(page, args.code, start_str, end_str, points_mode=False)
            cash_ok = cash_result.get("ok")
            print(f"    HTTP {cash_result.get('status')}")

            print(f"[3] 请求积分价格...")
            points_result = await fetch_raw(page, args.code, start_str, end_str, points_mode=True)
            pts_ok = points_result.get("ok")
            print(f"    HTTP {points_result.get('status')}")

        finally:
            await context.close()

    # 解析
    cash_map = parse_cash_to_map(cash_result.get("data")) if cash_ok else {}
    points_map = parse_points_to_map(points_result.get("data")) if pts_ok else {}

    # 生成完整日期列表
    all_dates = expand_date_range(start_str, end_str)

    # 输出表格
    print(f"\n{'='*60}")
    print(f"{'日期':12s} | {'现金':>10s} | {'货币':4s} | {'积分':>8s}")
    print(f"{'-'*60}")

    has_cash = 0
    has_points = 0
    for d in all_dates:
        cash_info = cash_map.get(d, {})
        cash_price = cash_info.get("price") if cash_info else None
        currency = cash_info.get("currency", "") if cash_info else ""
        points = points_map.get(d)

        cash_str = f"{cash_price:.2f}" if cash_price else ""
        pts_str = f"{points}" if points else ""

        if cash_price:
            has_cash += 1
        if points:
            has_points += 1

        print(f"{d:12s} | {cash_str:>10s} | {currency:4s} | {pts_str:>8s}")

    print(f"{'-'*60}")
    print(f"总计: {len(all_dates)} 天, 有现金价 {has_cash} 天, 有积分价 {has_points} 天")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
