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
            return { status: resp.status, ok: resp.ok, data: json, rawText: text.slice(0, 50000) };
        } catch (err) {
            return { status: 0, ok: false, error: err.message };
        }
    }
    """, {"apiKey": API_KEY, "payload": payload})
    return result


async def main():
    parser = argparse.ArgumentParser(description="IHG Calendar API 调试")
    parser.add_argument("--code", type=str, default="HPHHL", help="酒店代码")
    parser.add_argument("--start", type=str, default=None, help="开始日期 (默认明天)")
    parser.add_argument("--end", type=str, default=None, help="结束日期 (默认 start+365天)")
    parser.add_argument("--days", type=int, default=365, help="查询天数 (当不指定 start/end 时)")
    args = parser.parse_args()

    # 计算日期范围
    from datetime import date, timedelta
    if args.start:
        start_str = args.start
    else:
        start_str = (date.today() + timedelta(days=1)).isoformat()
    if args.end:
        end_str = args.end
    else:
        start_d = date.fromisoformat(start_str)
        end_str = (start_d + timedelta(days=args.days - 1)).isoformat()

    Path(USER_DATA_DIR).mkdir(parents=True, exist_ok=True)

    print(f"调试: {args.code} | {start_str} ~ {end_str} (一次请求)")
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

            # 现金请求 - 一次请求全部天数
            print(f"\n[2] 现金请求: {start_str} ~ {end_str} (一次性 {args.days} 天)")
            cash_result = await fetch_raw(page, args.code, start_str, end_str, points_mode=False)
            print(f"    HTTP {cash_result.get('status')}, ok={cash_result.get('ok')}")

            # 积分请求
            print(f"\n[3] 积分请求: {start_str} ~ {end_str} (一次性 {args.days} 天)")
            points_result = await fetch_raw(page, args.code, start_str, end_str, points_mode=True)
            print(f"    HTTP {points_result.get('status')}, ok={points_result.get('ok')}")

        finally:
            await context.close()

    # 保存原始响应
    output = {
        "hotel_code": args.code,
        "date_range": f"{start_str} ~ {end_str}",
        "days_requested": args.days,
        "cash_response": cash_result.get("data") or cash_result.get("rawText", ""),
        "points_response": points_result.get("data") or points_result.get("rawText", ""),
    }

    out_file = f"ihg_debug_{args.code}_{start_str}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"\n[+] 原始响应已保存: {out_file}")

    # 分析现金响应
    print(f"\n{'='*60}")
    print("分析现金响应:")
    print(f"{'='*60}")
    cash_data = cash_result.get("data", {})
    if cash_data and "data" in cash_data:
        for hotel in cash_data["data"].get("hotels", []):
            calendar = hotel.get("calendar", [])
            print(f"\n  返回天数: {len(calendar)}")
            
            has_rate = 0
            no_rate = 0
            dates_with_rate = []
            dates_no_rate = []
            
            for day in calendar:
                d = day.get("start", "")
                lr = day.get("lowestRate")
                if lr and lr.get("totalAmount"):
                    has_rate += 1
                    dates_with_rate.append(d)
                else:
                    no_rate += 1
                    dates_no_rate.append(d)
            
            print(f"  有现金价: {has_rate} 天")
            print(f"  无现金价: {no_rate} 天")
            print(f"  日期范围: {calendar[0].get('start', '')} ~ {calendar[-1].get('start', '') if calendar else ''}")
            
            # 打印前几个无价格的日期的完整数据
            print(f"\n  前 5 个无现金价日期的原始数据:")
            shown = 0
            for day in calendar:
                d = day.get("start", "")
                lr = day.get("lowestRate")
                if not (lr and lr.get("totalAmount")):
                    if shown < 5:
                        print(f"\n    日期: {d}")
                        print(f"      lowestRate: {json.dumps(lr, ensure_ascii=False) if lr else 'null'}")
                        offers = day.get("offers", [])
                        print(f"      offers: {len(offers)} 个")
                        for o in offers[:2]:
                            print(f"        {json.dumps(o, ensure_ascii=False)[:300]}")
                        # 打印所有顶层字段
                        other_keys = [k for k in day.keys() if k not in ('start', 'lowestRate', 'offers')]
                        if other_keys:
                            print(f"      其他字段: {other_keys}")
                            for k in other_keys:
                                print(f"        {k}: {json.dumps(day[k], ensure_ascii=False)[:200]}")
                        shown += 1
            
            # 打印一个有价格日期做对比
            print(f"\n  对比: 一个有现金价的日期:")
            for day in calendar:
                lr = day.get("lowestRate")
                if lr and lr.get("totalAmount"):
                    print(f"    日期: {day.get('start', '')}")
                    print(f"      lowestRate: {json.dumps(lr, ensure_ascii=False)[:300]}")
                    offers = day.get("offers", [])
                    print(f"      offers: {len(offers)} 个")
                    for o in offers[:2]:
                        print(f"        {json.dumps(o, ensure_ascii=False)[:300]}")
                    break
    else:
        print("  [!] 无有效响应数据")
        if cash_result.get("rawText"):
            print(f"  原始文本: {cash_result['rawText'][:1000]}")
        if cash_result.get("error"):
            print(f"  错误: {cash_result['error']}")


if __name__ == "__main__":
    asyncio.run(main())
