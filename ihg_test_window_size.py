"""
IHG Calendar API 窗口大小探测脚本

目的:
    测试 Calendar API 是否能接受 >62 天的查询窗口.
    官网默认 62 天, 如果能扩到 92/120 天, 总窗口数减少, batch_monitor 整体耗时可降:
        62 天 → 365/62 = 6 窗口
        92 天 → 365/92 = 4 窗口  (-33%)
        120 天 → 365/120 = 4 窗口  (-33%)
        180 天 → 365/180 = 3 窗口  (-50%)

测试维度:
    - 窗口跨度: 62 / 75 / 92 / 100 / 120 / 150 / 180 / 365 天
    - 模式: 现金 + 积分 (两种 payload, 历史 IHG 对积分模式更挑剔)
    - 记录: HTTP 状态, 响应时间, 实际返回的日期数, 错误信息

输出:
    每个组合一行结果, 最后给出推荐窗口大小.

用法:
    python ihg_test_window_size.py                          # 默认 HKGKL
    python ihg_test_window_size.py --code DADHA
    python ihg_test_window_size.py --code HKGKL --proxy http://127.0.0.1:7890
    python ihg_test_window_size.py --sizes 62,92,120        # 自定义测试窗口
    python ihg_test_window_size.py --start-offset 30        # 起始日期延后 N 天 (避免最近日期数据少)
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from playwright.async_api import async_playwright


USER_DATA_DIR = "./ihg_browser_profile"
API_KEY = "se9ym5iAzaW8pxfBjkmgbuGjJcr3Pj6Y"
POINTS_RATE_PLAN_CODES = ["IVAN1", "IVAN3", "IVAN5", "IVAN6", "IVAN7", "IVANI"]
SEED_URL = "https://www.ihg.com/hotels/us/en/find-hotels/hotel/rates"

# 默认测试窗口大小列表 (天)
DEFAULT_SIZES = [62, 75, 92, 100, 120, 150, 180, 365]

sys.stdout.reconfigure(line_buffering=True)


async def fetch_calendar(page, hotel_code, start_date, end_date, points_mode=False, timeout_ms=30000):
    """调用 Calendar API. 返回 (status_code, ok, data, error, elapsed_ms)"""
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

    t0 = time.time()
    result = await page.evaluate("""
    async ({ apiKey, payload, timeoutMs }) => {
        const uuid = () => 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
            const r = Math.random() * 16 | 0;
            const v = c === 'x' ? r : (r & 3 | 8);
            return v.toString(16);
        });
        try {
            const controller = new AbortController();
            const timeout = setTimeout(() => controller.abort(), timeoutMs);
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
            return { status: resp.status, ok: resp.ok, data: json, raw: text.slice(0, 500) };
        } catch (err) {
            return { status: 0, ok: false, error: err.message };
        }
    }
    """, {"apiKey": API_KEY, "payload": payload, "timeoutMs": timeout_ms})

    elapsed_ms = (time.time() - t0) * 1000
    return result, elapsed_ms


def count_returned_days(response_data):
    """统计 API 返回了多少天的数据 (展开合并区间)"""
    if not response_data:
        return 0
    total_days = 0
    for hotel in response_data.get("data", {}).get("hotels", []):
        for day in hotel.get("calendar", []):
            start_d = day.get("start")
            end_d = day.get("end", start_d)
            if start_d and end_d:
                d1 = date.fromisoformat(start_d)
                d2 = date.fromisoformat(end_d)
                total_days += (d2 - d1).days + 1
    return total_days


async def test_one_window(page, hotel_code, size_days, start_offset_days, points_mode):
    """测试一个组合: 跨度 size_days, 模式 (cash/points)
    Returns: dict with 详细结果
    """
    start = date.today() + timedelta(days=start_offset_days + 1)
    end = start + timedelta(days=size_days - 1)
    start_str = start.isoformat()
    end_str = end.isoformat()
    requested_days = size_days

    mode_label = "积分" if points_mode else "现金"

    try:
        result, elapsed_ms = await fetch_calendar(
            page, hotel_code, start_str, end_str, points_mode=points_mode, timeout_ms=30000
        )
    except Exception as e:
        return {
            "size": size_days, "mode": mode_label,
            "status": 0, "ok": False, "elapsed_ms": 0,
            "returned_days": 0, "requested_days": requested_days,
            "error": f"exception: {str(e)[:100]}",
            "date_range": f"{start_str}~{end_str}",
        }

    status = result.get("status", 0)
    ok = result.get("ok", False)
    data = result.get("data")
    err = result.get("error", "")
    raw = result.get("raw", "")
    returned_days = count_returned_days(data) if ok else 0

    # 取 errorCode 摘要
    error_summary = ""
    if not ok:
        if err:
            error_summary = err[:100]
        elif data and isinstance(data, dict):
            errors = data.get("errors") or []
            if errors and isinstance(errors[0], dict):
                error_summary = f"{errors[0].get('code', '')}: {errors[0].get('message', '')[:80]}"
            elif data.get("errorCode"):
                error_summary = f"{data.get('errorCode')}: {str(data.get('message', ''))[:80]}"
            else:
                error_summary = raw[:100] if raw else f"HTTP {status}"
        else:
            error_summary = raw[:100] if raw else f"HTTP {status}"

    return {
        "size": size_days, "mode": mode_label,
        "status": status, "ok": ok,
        "elapsed_ms": elapsed_ms,
        "returned_days": returned_days,
        "requested_days": requested_days,
        "error": error_summary,
        "date_range": f"{start_str}~{end_str}",
    }


def print_results_table(results):
    """打印结果表格"""
    print()
    print("=" * 100)
    print(f"  {'窗口':>6} | {'模式':<4} | {'状态':<10} | {'耗时':>8} | {'返回天数':>10} | 错误/备注")
    print("-" * 100)
    for r in results:
        size = r["size"]
        mode = r["mode"]
        if r["ok"]:
            status_str = f"✓ {r['status']}"
            err_str = ""
            if r["returned_days"] != r["requested_days"]:
                err_str = f"返回 {r['returned_days']}/{r['requested_days']} 天 (可能截断)"
        else:
            status_str = f"✗ {r['status']}"
            err_str = r["error"]
        elapsed_str = f"{r['elapsed_ms']:.0f}ms"
        print(f"  {size:>4}天 | {mode:<4} | {status_str:<10} | {elapsed_str:>8} | "
              f"{r['returned_days']:>4}/{r['requested_days']:<4} | {err_str}")
    print("=" * 100)


def print_recommendation(results):
    """根据结果给出推荐窗口大小"""
    print()
    print("─" * 100)
    print("  推荐分析:")
    print("─" * 100)

    # 按窗口大小聚合: 必须 cash+points 都 ok 且 returned == requested 才视作"合格"
    by_size = {}
    for r in results:
        size = r["size"]
        if size not in by_size:
            by_size[size] = {}
        by_size[size][r["mode"]] = r

    valid_sizes = []
    for size in sorted(by_size.keys()):
        cash = by_size[size].get("现金")
        points = by_size[size].get("积分")
        cash_ok = cash and cash["ok"] and cash["returned_days"] >= cash["requested_days"] - 1
        points_ok = points and points["ok"] and points["returned_days"] >= points["requested_days"] - 1
        if cash_ok and points_ok:
            valid_sizes.append(size)
            cash_t = cash["elapsed_ms"]
            points_t = points["elapsed_ms"]
            print(f"  ✓ {size:>3} 天: 双模式都OK | 现金 {cash_t:.0f}ms / 积分 {points_t:.0f}ms")
        else:
            issues = []
            if not cash:
                issues.append("现金未测")
            elif not cash["ok"]:
                issues.append(f"现金{cash['status']}")
            elif cash["returned_days"] < cash["requested_days"] - 1:
                issues.append(f"现金截断({cash['returned_days']}/{cash['requested_days']})")
            if not points:
                issues.append("积分未测")
            elif not points["ok"]:
                issues.append(f"积分{points['status']}")
            elif points["returned_days"] < points["requested_days"] - 1:
                issues.append(f"积分截断({points['returned_days']}/{points['requested_days']})")
            print(f"  ✗ {size:>3} 天: {', '.join(issues)}")

    print()
    if not valid_sizes:
        print("  [结论] 没有合格窗口, 维持当前 62 天")
    else:
        max_valid = max(valid_sizes)
        if max_valid > 62:
            saved_pct = (1 - 62 / max_valid) * 100 if max_valid <= 365 else 0
            new_window_count = (365 + max_valid - 1) // max_valid
            old_window_count = (365 + 62 - 1) // 62
            time_save_pct = (1 - new_window_count / old_window_count) * 100
            print(f"  [结论] 推荐扩大到 {max_valid} 天")
            print(f"         365天总窗口数: {old_window_count} → {new_window_count} ({time_save_pct:.0f}% 减少)")
            print(f"         单酒店耗时预计降低约 {time_save_pct:.0f}%")
            print(f"         修改方法: notify_config.json 的 runtime.window_size_days = {max_valid}")
        else:
            print(f"  [结论] 最大合格窗口仍是 62 天, 维持现状")


async def run_tests(args):
    """主测试流程"""
    sizes = args.sizes or DEFAULT_SIZES
    if isinstance(sizes, str):
        sizes = [int(s.strip()) for s in sizes.split(",") if s.strip()]

    print("=" * 100)
    print(f"  IHG Calendar API 窗口大小探测")
    print(f"  酒店: {args.code}")
    print(f"  起始日期偏移: 今天 + {args.start_offset + 1} 天")
    print(f"  测试窗口: {sizes}")
    print(f"  代理: {args.proxy or '直连'}")
    print("=" * 100)

    Path(USER_DATA_DIR).mkdir(parents=True, exist_ok=True)

    launch_opts = {
        "headless": False,
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        "viewport": {"width": 1280, "height": 900},
        "locale": "en-US",
        "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox"],
    }
    if args.proxy:
        launch_opts["proxy"] = {"server": args.proxy}

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(USER_DATA_DIR, **launch_opts)
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )

        try:
            page = await context.new_page()
            print("\n[1] 建立 session...")
            await page.goto(SEED_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(2000)
            print("    session 就绪 ✓")

            print(f"\n[2] 开始测试 {len(sizes)} 个窗口 × 2 模式 = {len(sizes) * 2} 次请求")
            results = []
            for i, size in enumerate(sizes, 1):
                # 每窗口测两次: 现金 + 积分
                for points_mode in (False, True):
                    mode_label = "积分" if points_mode else "现金"
                    print(f"  [{i}/{len(sizes)}] {size:>3}天 / {mode_label} ... ", end="", flush=True)
                    r = await test_one_window(page, args.code, size, args.start_offset, points_mode)
                    results.append(r)
                    if r["ok"]:
                        print(f"✓ {r['elapsed_ms']:.0f}ms ({r['returned_days']}/{r['requested_days']}天)")
                    else:
                        print(f"✗ HTTP {r['status']} - {r['error'][:60]}")
                    # 每次请求间随机延迟, 避免被限流
                    await page.wait_for_timeout(800)

            # 输出结果表格 + 推荐
            print_results_table(results)
            print_recommendation(results)

            # 保存 JSON 报告 (供后续分析)
            report_path = Path(__file__).parent / f"window_size_test_{args.code}.json"
            with open(report_path, "w", encoding="utf-8") as f:
                json.dump({
                    "hotel_code": args.code,
                    "tested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "results": results,
                }, f, ensure_ascii=False, indent=2)
            print(f"\n  [报告] 详细结果已保存到 {report_path}")

        finally:
            await context.close()


def main():
    parser = argparse.ArgumentParser(description="IHG Calendar API 窗口大小探测")
    parser.add_argument("--code", type=str, default="HKGKL",
                        help="测试用酒店代码 (默认 HKGKL 香港金域假日)")
    parser.add_argument("--sizes", type=str, default=None,
                        help=f"测试窗口大小列表, 逗号分隔 (默认 {','.join(map(str, DEFAULT_SIZES))})")
    parser.add_argument("--start-offset", type=int, default=0,
                        help="起始日期偏移 (今天 + offset + 1, 默认 0=明天起)")
    parser.add_argument("--proxy", type=str, default=None,
                        help="代理地址 (如 http://127.0.0.1:7890)")
    args = parser.parse_args()

    asyncio.run(run_tests(args))


if __name__ == "__main__":
    main()
