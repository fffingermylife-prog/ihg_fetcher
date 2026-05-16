"""
IHG 新日期开放时间探测脚本

功能:
    等待到指定时间后开始, 每隔随机 10~15 分钟检查一次指定酒店的最远可预订日期,
    记录"最远日期"何时从无价格变为有价格, 从而确定每日新日期开放的精确时间。
    检测到新日期开放后自动停止。

原理:
    1. 等待到指定启动时间 (默认 00:59)
    2. 获取当前最远可预订日期 (约 349 天后) 作为基准
    3. 每隔 10~15 分钟随机间隔请求, 检查最远日期是否变化
    4. 当最远日期往后推了 1 天 → 记录此刻时间 = 新日期开放时间, 自动停止

用法:
    # 立即启动, 等待到 00:59 开始探测 (检测到新日期后停止)
    python ihg_detect_open_time.py --code HKGKL

    # 自定义启动时间
    python ihg_detect_open_time.py --code HKGKL --start-time 06:30

    # 立即开始 (不等待)
    python ihg_detect_open_time.py --code HKGKL --now
"""

import argparse
import asyncio
import sys
import random
import time
from datetime import datetime, date, timedelta
from pathlib import Path
from playwright.async_api import async_playwright

# 强制实时输出
sys.stdout.reconfigure(line_buffering=True)

# ============ 配置 ============

USER_DATA_DIR = "./ihg_browser_profile"
API_KEY = "se9ym5iAzaW8pxfBjkmgbuGjJcr3Pj6Y"
POINTS_RATE_PLAN_CODES = ["IVAN1", "IVAN3", "IVAN5", "IVAN6", "IVAN7", "IVANI"]
SEED_URL = "https://www.ihg.com/hotels/us/en/find-hotels/hotel/rates"


# ============ API 调用 ============

async def fetch_farthest_date(page, hotel_code):
    """获取最远有价格的日期 (只请求最远的一个 62 天窗口)"""
    # 请求约 288~349 天后的窗口
    start = date.today() + timedelta(days=288)
    end = start + timedelta(days=61)

    payload = {
        "hotelMnemonics": [hotel_code],
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
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

    result = await page.evaluate("""
    async ({ apiKey, payload }) => {
        const uuid = () => 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
            const r = Math.random() * 16 | 0;
            const v = c === 'x' ? r : (r & 3 | 8);
            return v.toString(16);
        });
        try {
            const controller = new AbortController();
            const timeout = setTimeout(() => controller.abort(), 15000);
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
                signal: controller.signal,
            });
            clearTimeout(timeout);
            const text = await resp.text();
            let json = null;
            try { json = JSON.parse(text); } catch (e) {}
            return { status: resp.status, ok: resp.ok, data: json };
        } catch (err) {
            return { status: 0, ok: false, error: err.message };
        }
    }
    """, {"apiKey": API_KEY, "payload": payload})

    if not (result.get("ok") and result.get("data")):
        return None, None

    # 找出最远有价格的日期
    farthest = None
    farthest_points = None
    data = result["data"]
    for hotel in data.get("data", {}).get("hotels", []):
        for day in hotel.get("calendar", []):
            end_d = day.get("end") or day.get("start")
            if end_d:
                if farthest is None or end_d > farthest:
                    farthest = end_d
                    # 读取积分
                    for offer in day.get("offers", []):
                        tp = offer.get("totalPoints")
                        if tp:
                            farthest_points = int(tp)
                            break

    return farthest, farthest_points


# ============ 主逻辑 ============

async def main():
    parser = argparse.ArgumentParser(description="IHG 新日期开放时间探测")
    parser.add_argument("--code", type=str, default="HKGKL",
                        help="酒店代码 (默认 HKGKL)")
    parser.add_argument("--start-time", type=str, default="00:59",
                        help="开始探测的时间 (HH:MM, 默认 00:59)")
    parser.add_argument("--now", action="store_true",
                        help="立即开始, 不等待指定时间")
    args = parser.parse_args()

    hotel_code = args.code.upper()

    print("=" * 60)
    print(f"  IHG 新日期开放时间探测")
    print(f"  酒店: {hotel_code}")
    print(f"  间隔: 随机 10~15 分钟")
    print(f"  启动时间: {'立即' if args.now else args.start_time}")
    print(f"  停止条件: 检测到新日期开放后自动停止")
    print(f"  当前时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # 等待到指定时间
    if not args.now:
        target_hour, target_min = map(int, args.start_time.split(":"))
        now = datetime.now()
        target = now.replace(hour=target_hour, minute=target_min, second=0, microsecond=0)

        # 如果目标时间已过, 等到明天的这个时间
        if target <= now:
            target += timedelta(days=1)

        wait_seconds = (target - now).total_seconds()
        print(f"\n  ⏰ 等待到 {target.strftime('%Y-%m-%d %H:%M')} 开始探测")
        print(f"     还需等待 {wait_seconds/60:.0f} 分钟...")
        print(f"     (脚本保持运行, 请勿关闭窗口)\n")

        await asyncio.sleep(wait_seconds)
        print(f"  ✓ 到达指定时间, 开始探测!\n")

    # 日志文件
    log_file = Path(f"ihg_open_time_{hotel_code}.log")

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
            # 建立 session
            print(f"[初始化] 建立浏览器 session...")
            await page.goto(SEED_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)
            print(f"  ✓ Session 就绪")

            # 首次获取基准
            farthest, points = await fetch_farthest_date(page, hotel_code)
            if not farthest:
                print("[!] 首次请求失败, 请确认酒店代码和网络")
                return

            baseline_farthest = farthest
            print(f"\n[基准] 当前最远日期: {farthest} (积分: {points})")
            print(f"  等待新日期开放...\n")

            # 记录日志
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"\n--- 探测开始: {datetime.now().isoformat()} ---\n")
                f.write(f"酒店: {hotel_code}\n")
                f.write(f"基准最远日期: {farthest}\n")

            # 循环探测
            check_count = 0

            while True:
                # 随机等待 10~15 分钟
                wait_min = random.randint(10, 15)
                wait_ms = wait_min * 60 * 1000
                await page.wait_for_timeout(wait_ms)
                check_count += 1

                # 请求
                now_str = datetime.now().strftime("%H:%M:%S")
                new_farthest, new_points = await fetch_farthest_date(page, hotel_code)

                if not new_farthest:
                    print(f"  [{now_str}] #{check_count} 请求失败, 跳过")
                    continue

                if new_farthest > baseline_farthest:
                    # 🎉 检测到新日期开放! 停止探测
                    detect_time = datetime.now()
                    print(f"\n  {'='*50}")
                    print(f"  🎉 新日期开放!")
                    print(f"  检测时间: {detect_time.strftime('%Y-%m-%d %H:%M:%S')}")
                    print(f"  之前最远: {baseline_farthest}")
                    print(f"  现在最远: {new_farthest} (积分: {new_points})")
                    print(f"  检查次数: {check_count}")
                    print(f"  {'='*50}\n")

                    # 写日志
                    with open(log_file, "a", encoding="utf-8") as f:
                        f.write(f"\n🎉 新日期开放!\n")
                        f.write(f"  检测时间: {detect_time.isoformat()}\n")
                        f.write(f"  之前最远: {baseline_farthest}\n")
                        f.write(f"  现在最远: {new_farthest} (积分: {new_points})\n")
                        f.write(f"  检查次数: {check_count}\n")

                    # 自动停止
                    print(f"  ✓ 探测完成, 自动退出")
                    break

                else:
                    # 无变化
                    print(f"  [{now_str}] #{check_count} 最远: {new_farthest} (无变化, 下次约{wait_min}分钟后)")

        except KeyboardInterrupt:
            print(f"\n[中断] 用户手动停止")
        except Exception as e:
            print(f"\n[异常] {e}")
        finally:
            await context.close()

    print(f"\n[完成] 日志已保存: {log_file}")
    print(f"  总检查次数: {check_count}")


if __name__ == "__main__":
    asyncio.run(main())
