"""
IHG Calendar API 调试 - 查看指定日期的原始响应
用于诊断为什么某些日期现金价格为 null

用法:
    python ihg_debug_calendar.py --code HPHHL --start 2027-04-20 --end 2027-04-30
"""

import argparse
import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

USER_DATA_DIR = "./ihg_browser_profile"
API_KEY = "se9ym5iAzaW8pxfBjkmgbuGjJcr3Pj6Y"
POINTS_RATE_PLAN_CODES = ["IVAN1", "IVAN3", "IVAN5", "IVAN6", "IVAN7", "IVANI"]


async def fetch_raw(page, hotel_code, start_date, end_date, points_mode=False):
    """调用 Calendar API 并返回完整原始响应"""
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
            return { status: resp.status, ok: resp.ok, data: json, rawText: text.slice(0, 10000) };
        } catch (err) {
            return { status: 0, ok: false, error: err.message };
        }
    }
    """, {"apiKey": API_KEY, "payload": payload})
    return result


async def main():
    parser = argparse.ArgumentParser(description="IHG Calendar API 调试")
    parser.add_argument("--code", type=str, default="HPHHL", help="酒店代码")
    parser.add_argument("--start", type=str, default="2027-04-20", help="开始日期")
    parser.add_argument("--end", type=str, default="2027-04-30", help="结束日期")
    args = parser.parse_args()

    Path(USER_DATA_DIR).mkdir(parents=True, exist_ok=True)

    print(f"调试: {args.code} | {args.start} ~ {args.end}")
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
            # 建立 session
            print("[1] 建立 session...")
            await page.goto("https://www.ihg.com/hotels/us/en/find-hotels/hotel/rates",
                            wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)
            print("    ✓ OK")

            # 现金请求
            print(f"\n[2] 现金请求: {args.start} ~ {args.end}")
            cash_result = await fetch_raw(page, args.code, args.start, args.end, points_mode=False)
            print(f"    HTTP {cash_result.get('status')}, ok={cash_result.get('ok')}")

            # 积分请求
            print(f"\n[3] 积分请求: {args.start} ~ {args.end}")
            points_result = await fetch_raw(page, args.code, args.start, args.end, points_mode=True)
            print(f"    HTTP {points_result.get('status')}, ok={points_result.get('ok')}")

        finally:
            await context.close()

    # 保存原始响应
    output = {
        "hotel_code": args.code,
        "date_range": f"{args.start} ~ {args.end}",
        "cash_response": cash_result.get("data") or cash_result.get("rawText", ""),
        "points_response": points_result.get("data") or points_result.get("rawText", ""),
    }

    out_file = f"ihg_debug_{args.code}_{args.start}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"\n[+] 原始响应已保存: {out_file}")

    # 分析现金响应中 2027-04-25 的数据
    print(f"\n{'='*60}")
    print("分析现金响应中的日历数据:")
    print(f"{'='*60}")
    cash_data = cash_result.get("data", {})
    if cash_data and "data" in cash_data:
        for hotel in cash_data["data"].get("hotels", []):
            for day in hotel.get("calendar", []):
                d = day.get("start", "")
                if "2027-04-25" in d or True:  # 打印所有日期
                    lr = day.get("lowestRate")
                    offers = day.get("offers", [])
                    status = day.get("status", "")
                    sell = day.get("sellStrategy", "")
                    print(f"\n  日期: {d}")
                    print(f"    status: {status}")
                    print(f"    sellStrategy: {sell}")
                    print(f"    lowestRate: {json.dumps(lr, ensure_ascii=False) if lr else 'null'}")
                    if offers:
                        print(f"    offers ({len(offers)} 个):")
                        for o in offers[:3]:
                            print(f"      {json.dumps(o, ensure_ascii=False)[:200]}")
                    else:
                        print(f"    offers: []")
    else:
        print("  [!] 无有效响应数据")
        if cash_result.get("rawText"):
            print(f"  原始文本: {cash_result['rawText'][:500]}")


if __name__ == "__main__":
    asyncio.run(main())
