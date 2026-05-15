"""
IHG Calendar API 批量测试 - 验证一次请求多个酒店是否可行

测试: hotelMnemonics 传入多个酒店代码, 看 API 是否正常返回所有酒店的数据

用法:
    python ihg_test_batch_calendar.py --codes HPHHL,SGNVC,HANHC
"""

import argparse
import asyncio
import json
from datetime import date, timedelta
from pathlib import Path
from playwright.async_api import async_playwright

USER_DATA_DIR = "./ihg_browser_profile"
API_KEY = "se9ym5iAzaW8pxfBjkmgbuGjJcr3Pj6Y"


async def fetch_batch(page, hotel_codes, start_date, end_date):
    """一次请求多个酒店的现金价格"""
    payload = {
        "hotelMnemonics": hotel_codes,
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


async def main():
    parser = argparse.ArgumentParser(description="IHG Calendar API 批量测试")
    parser.add_argument("--codes", type=str, default="HPHHL,SGNVC,HANHC",
                        help="酒店代码, 逗号分隔 (默认 HPHHL,SGNVC,HANHC)")
    parser.add_argument("--days", type=int, default=62, help="查询天数 (默认 62)")
    args = parser.parse_args()

    codes = [c.strip().upper() for c in args.codes.split(",") if c.strip()]
    start_str = (date.today() + timedelta(days=1)).isoformat()
    end_str = (date.today() + timedelta(days=args.days)).isoformat()

    Path(USER_DATA_DIR).mkdir(parents=True, exist_ok=True)

    print(f"批量测试: {codes}")
    print(f"日期: {start_str} ~ {end_str}")
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

            # 测试: 一次请求多个酒店
            print(f"\n[2] 批量请求 {len(codes)} 个酒店...")
            result = await fetch_batch(page, codes, start_str, end_str)
            print(f"    HTTP {result.get('status')}, ok={result.get('ok')}")

            if result.get("ok") and result.get("data"):
                data = result["data"]
                hotels = data.get("data", {}).get("hotels", [])
                print(f"\n    返回酒店数: {len(hotels)}")
                for hotel in hotels:
                    info = hotel.get("hotel", {})
                    code = info.get("hotelMnemonic", "?")
                    calendar = hotel.get("calendar", [])
                    print(f"      {code}: {len(calendar)} 条日历记录")
            else:
                print(f"    [!] 请求失败")
                if result.get("error"):
                    print(f"    错误: {result['error']}")

        finally:
            await context.close()

    print(f"\n{'='*60}")
    if result.get("ok"):
        print(f"✓ 批量请求成功! 一次可获取 {len(codes)} 个酒店的数据")
        print(f"  这意味着 100 个酒店只需 100÷{len(codes)} = {100//len(codes)} 批请求")
    else:
        print(f"✗ 批量请求失败, 可能需要逐个请求")


if __name__ == "__main__":
    asyncio.run(main())
