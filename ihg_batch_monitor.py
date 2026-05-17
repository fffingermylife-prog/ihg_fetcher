"""
IHG 多酒店批量价格监控 - 并发高效版

特性:
    - 多 Tab 并发 (默认 2, 最大 3), 大幅提升效率
    - 全量模式: 获取 365 天完整价格, 对比所有变化
    - 增量模式: 只获取最远 62 天窗口, 快速检测新开放日期
    - 房态检测: 售罄/重新开放/积分房售罄
    - 失败自动重试 1 次, 仍失败跳过并记录
    - 进度显示 + 变价汇总报告
    - JSON 文件存储 (后续可替换为 SQLite)

用法:
    # 指定酒店代码
    python ihg_batch_monitor.py --codes HPHHL,SGNVC,HANHC

    # 从 CSV 文件读取 (需有 mnemonic 列)
    python ihg_batch_monitor.py --from-csv hotels_vietnam.csv

    # 从 JSON 文件读取
    python ihg_batch_monitor.py --from-json ihg_test_result.json

    # 增量模式 (只检测新开放)
    python ihg_batch_monitor.py --codes HPHHL,SGNVC --incremental

    # 调整并发数
    python ihg_batch_monitor.py --codes HPHHL,SGNVC,HANHC --concurrency 3

    # 只看变化不保存
    python ihg_batch_monitor.py --codes HPHHL --dry-run
"""

import argparse
import asyncio
import csv
import json
import random
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from playwright.async_api import async_playwright


# ============ 配置 ============

USER_DATA_DIR = "./ihg_browser_profile"
API_KEY = "se9ym5iAzaW8pxfBjkmgbuGjJcr3Pj6Y"
POINTS_RATE_PLAN_CODES = ["IVAN1", "IVAN3", "IVAN5", "IVAN6", "IVAN7", "IVANI"]

WINDOW_SIZE_DAYS = 62
REQUEST_DELAY_MS = (200, 500)  # 随机延迟区间 (毫秒)
MAX_RETRIES = 1

# 数据目录
DATA_DIR = "./ihg_data"

# Seed URL
SEED_URL = "https://www.ihg.com/hotels/us/en/find-hotels/hotel/rates"

# 强制 stdout 实时输出 (解决 Windows 命令行缓冲问题)
sys.stdout.reconfigure(line_buffering=True)


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


def load_hotel_codes(args):
    """从各种来源加载酒店代码列表"""
    codes = []

    if args.codes:
        codes = [c.strip().upper() for c in args.codes.split(",") if c.strip()]

    elif args.from_csv:
        try:
            with open(args.from_csv, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    code = row.get("mnemonic", "").strip().upper()
                    if code:
                        codes.append(code)
        except Exception as e:
            print(f"[!] 读取 CSV 失败: {e}")

    elif args.from_json:
        try:
            with open(args.from_json, "r", encoding="utf-8") as f:
                data = json.load(f)
            hotels = data.get("hotels", data) if isinstance(data, dict) else data
            if isinstance(hotels, list):
                for h in hotels:
                    code = h.get("mnemonic", "").strip().upper()
                    if code:
                        codes.append(code)
        except Exception as e:
            print(f"[!] 读取 JSON 失败: {e}")

    elif args.from_db:
        try:
            db = get_db()
            codes = db.get_hotel_codes(args.country)
            if not codes:
                print(f"[!] 数据库中无酒店记录" + (f" (国家: {args.country})" if args.country else ""))
        except Exception as e:
            print(f"[!] 读取数据库失败: {e}")

    # 去重保序
    seen = set()
    unique = []
    for c in codes:
        if c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


# ============ API 调用 ============

async def fetch_calendar(page, hotel_code, start_date, end_date, points_mode=False):
    """在浏览器上下文中调用 Calendar API"""
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

    if result.get("ok") and result.get("data"):
        return result["data"]
    return None


# ============ 解析函数 ============

def parse_cash(response_data):
    """解析现金价格响应"""
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
                    results.append({
                        "date": d,
                        "cash_price": price,
                        "cash_price_after_tax": price_after_tax,
                        "currency": cur,
                    })
    return results


def parse_points(response_data):
    """解析积分价格响应"""
    results = []
    if not response_data:
        return results
    for hotel in response_data.get("data", {}).get("hotels", []):
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
                    results.append({
                        "date": d,
                        "points": int(lowest) if lowest else None,
                    })
    return results


# ============ 单酒店获取逻辑 ============

async def fetch_hotel_prices(page, hotel_code, windows):
    """获取单个酒店的全部价格 (现金+积分), 返回合并后的列表"""
    all_cash = []
    all_points = []

    # 获取现金价格
    for ws, we in windows:
        resp = await fetch_calendar(page, hotel_code, ws, we, points_mode=False)
        all_cash.extend(parse_cash(resp))
        await page.wait_for_timeout(random.randint(*REQUEST_DELAY_MS))

    # 获取积分价格
    for ws, we in windows:
        resp = await fetch_calendar(page, hotel_code, ws, we, points_mode=True)
        all_points.extend(parse_points(resp))
        await page.wait_for_timeout(random.randint(*REQUEST_DELAY_MS))

    # 合并
    cash_map = {c["date"]: c for c in all_cash}
    points_map = {p["date"]: p for p in all_points}
    all_dates = sorted(set(cash_map.keys()) | set(points_map.keys()))

    merged = []
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
        merged.append({
            "date": d,
            "cash_price": cash_price,
            "cash_price_after_tax": cash_price_after_tax,
            "currency": cash.get("currency", ""),
            "points": points_price,
            "cpp": cpp,
        })

    return merged


# ============ 对比逻辑 (含房态检测) ============

def compare_prices(old_prices, new_prices):
    """对比新旧价格, 返回变化列表 (含房态检测, 使用含税价)"""
    old_map = {p["date"]: p for p in old_prices}
    new_map = {p["date"]: p for p in new_prices}
    changes = []
    all_dates = sorted(set(old_map.keys()) | set(new_map.keys()))

    for d in all_dates:
        old = old_map.get(d, {})
        new = new_map.get(d, {})

        # 优先用含税价, 没有则用不含税价
        old_cash = old.get("cash_price_after_tax") or old.get("cash_price")
        new_cash = new.get("cash_price_after_tax") or new.get("cash_price")
        old_pts = old.get("points")
        new_pts = new.get("points")

        # 新开放的日期 (之前完全没有记录)
        if not old and new:
            changes.append({
                "date": d, "type": "新开放",
                "cash_price": new.get("cash_price_after_tax") or new.get("cash_price"),
                "cash_after_tax": new.get("cash_price_after_tax"),
                "points": new_pts,
            })
            continue

        # === 房态检测 ===

        # 现金售罄 (之前有现金价, 现在没了)
        if old_cash and not new_cash:
            changes.append({"date": d, "type": "现金售罄", "old_value": old_cash})

        # 现金重新开放 (之前没现金价, 现在有了)
        elif not old_cash and new_cash:
            changes.append({
                "date": d, "type": "现金重新开放",
                "new_value": new_cash,
                "currency": new.get("currency", ""),
            })

        # 现金价格变化
        elif old_cash and new_cash and old_cash != new_cash:
            diff = new_cash - old_cash
            pct = (diff / old_cash) * 100
            changes.append({
                "date": d,
                "type": "现金涨" if diff > 0 else "现金降",
                "old_value": old_cash, "new_value": new_cash,
                "diff": diff, "pct": pct,
                "currency": new.get("currency", ""),
            })

        # 积分房售罄 (之前有积分价, 现在没了)
        if old_pts and not new_pts:
            changes.append({"date": d, "type": "积分房售罄", "old_value": old_pts})

        # 积分房重新开放 (之前没积分价, 现在有了)
        elif not old_pts and new_pts:
            changes.append({"date": d, "type": "积分房重新开放", "new_value": new_pts})

        # 积分价格变化
        elif old_pts and new_pts and old_pts != new_pts:
            diff = new_pts - old_pts
            pct = (diff / old_pts) * 100
            changes.append({
                "date": d,
                "type": "积分涨" if diff > 0 else "积分降",
                "old_value": old_pts, "new_value": new_pts,
                "diff": diff, "pct": pct,
            })

    return changes


# ============ 存储函数 (SQLite) ============

# 全局数据库实例 (在 main 中初始化)
_db = None


def get_db():
    """获取数据库实例"""
    global _db
    if _db is None:
        import sys
        from pathlib import Path as _P
        sys.path.insert(0, str(_P(__file__).parent))
        from ihg_db import IHGDatabase
        _db = IHGDatabase()
    return _db


def load_baseline(hotel_code):
    """从 SQLite 加载最近一次采集的价格作为基线"""
    db = get_db()
    return db.load_latest_prices(hotel_code)


def save_prices(hotel_code, prices, days_queried):
    """保存价格数据到 SQLite"""
    db = get_db()
    db.save_prices(hotel_code, prices)


# ============ 并发 Worker ============

async def worker(worker_id, page, task_queue, results, windows, dry_run, clash_mgr=None):
    """并发 worker: 从队列取酒店代码, 获取价格, 对比变化"""
    while True:
        try:
            idx, hotel_code, total_count = task_queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        t0 = time.time()
        success = False
        retries = 0

        while retries <= MAX_RETRIES:
            try:
                # 单酒店整体超时 120 秒 (正常约 6~8 秒)
                prices = await asyncio.wait_for(
                    fetch_hotel_prices(page, hotel_code, windows),
                    timeout=120
                )
                success = True
                break
            except asyncio.TimeoutError:
                retries += 1
                if retries <= MAX_RETRIES:
                    print(f"  [W{worker_id}] {hotel_code} 超时, 重试 ({retries}/{MAX_RETRIES})...")
                    await page.wait_for_timeout(random.randint(3000, 5000))
                else:
                    print(f"  [W{worker_id}] {hotel_code} 超时, 跳过")
            except Exception as e:
                retries += 1
                if retries <= MAX_RETRIES:
                    print(f"  [W{worker_id}] {hotel_code} 失败, 重试 ({retries}/{MAX_RETRIES})...")
                    await page.wait_for_timeout(random.randint(2000, 4000))
                else:
                    print(f"  [W{worker_id}] {hotel_code} 失败, 跳过: {str(e)[:100]}")

        elapsed = time.time() - t0

        if success and prices:
            # 加载基线对比
            old_prices, old_date = load_baseline(hotel_code)
            changes = []

            if old_prices:
                changes = compare_prices(old_prices, prices)

            # 保存最新数据
            if not dry_run:
                save_prices(hotel_code, prices, len(windows) * WINDOW_SIZE_DAYS)

            results.append({
                "hotel_code": hotel_code,
                "success": True,
                "days": len(prices),
                "changes": changes,
                "elapsed": elapsed,
                "is_first_run": not bool(old_prices),
            })
            status = "首次" if not old_prices else f"{len(changes)}变化"
            print(f"  [{idx}/{total_count}] {hotel_code} ✓ {elapsed:.1f}s ({len(prices)}天, {status})")

            # Clash: 每完成一个酒店, 检查是否需要轮换节点
            if clash_mgr:
                clash_mgr.on_hotel_done()
        else:
            results.append({
                "hotel_code": hotel_code,
                "success": False,
                "elapsed": elapsed,
                "changes": [],
            })
            print(f"  [{idx}/{total_count}] {hotel_code} ✗ {elapsed:.1f}s (失败)")

            # Clash: 失败时立即切换节点
            if clash_mgr:
                clash_mgr.on_failure()


# ============ 报告输出 ============

def print_report(results):
    """输出汇总变价报告"""
    all_changes = []
    for r in results:
        if r["success"] and r["changes"]:
            for c in r["changes"]:
                c["hotel_code"] = r["hotel_code"]
            all_changes.extend(r["changes"])

    if not all_changes:
        print(f"\n  ✓ 所有酒店无价格/房态变动")
        return

    # 按类型分组
    type_groups = {}
    for c in all_changes:
        t = c["type"]
        if t not in type_groups:
            type_groups[t] = []
        type_groups[t].append(c)

    print(f"\n{'='*80}")
    print(f"  变动汇总报告")
    print(f"{'='*80}")

    # 优先显示重要变动
    priority_order = [
        "积分房售罄", "积分房重新开放", "新开放",
        "现金售罄", "现金重新开放",
        "积分降", "积分涨", "现金降", "现金涨",
    ]

    for change_type in priority_order:
        items = type_groups.pop(change_type, [])
        if not items:
            continue

        print(f"\n  [{change_type}] {len(items)} 条:")
        # 按酒店分组显示
        by_hotel = {}
        for item in items:
            code = item["hotel_code"]
            if code not in by_hotel:
                by_hotel[code] = []
            by_hotel[code].append(item)

        for code, hotel_items in by_hotel.items():
            if len(hotel_items) <= 3:
                for item in hotel_items:
                    detail = format_change_detail(item)
                    print(f"    {code} {item['date']} {detail}")
            else:
                # 多天折叠显示
                dates = [item["date"] for item in hotel_items]
                print(f"    {code} {dates[0]}~{dates[-1]} ({len(hotel_items)}天)")
                # 显示前 2 条
                for item in hotel_items[:2]:
                    detail = format_change_detail(item)
                    print(f"      {item['date']} {detail}")
                print(f"      ... 等 {len(hotel_items)-2} 条")

    # 剩余类型
    for change_type, items in type_groups.items():
        if items:
            print(f"\n  [{change_type}] {len(items)} 条")

    # 汇总统计
    print(f"\n{'='*80}")
    success_count = sum(1 for r in results if r["success"])
    fail_count = sum(1 for r in results if not r["success"])
    first_run = sum(1 for r in results if r.get("is_first_run"))
    total_changes = len(all_changes)
    print(f"  统计: 成功 {success_count} | 失败 {fail_count} | 首次运行 {first_run} | 总变动 {total_changes}")
    print(f"{'='*80}")


def format_change_detail(item):
    """格式化单条变动详情"""
    t = item["type"]
    if t in ("现金涨", "现金降"):
        return f"{item['old_value']:.0f}→{item['new_value']:.0f} ({item['pct']:+.1f}%) {item.get('currency','')}"
    elif t in ("积分涨", "积分降"):
        return f"{item['old_value']}→{item['new_value']} ({item['pct']:+.1f}%)"
    elif t == "新开放":
        parts = []
        if item.get("cash_price"):
            parts.append(f"现金{item['cash_price']:.0f}")
        if item.get("points"):
            parts.append(f"积分{item['points']}")
        return " ".join(parts) if parts else ""
    elif t in ("现金售罄", "积分房售罄"):
        return f"(原价 {item.get('old_value', '?')})"
    elif t in ("现金重新开放", "积分房重新开放"):
        return f"→ {item.get('new_value', '?')}"
    return ""


# ============ 主逻辑 ============

async def main():
    parser = argparse.ArgumentParser(description="IHG 多酒店批量价格监控")
    parser.add_argument("--codes", type=str, default=None,
                        help="酒店代码, 逗号分隔 (如 HPHHL,SGNVC,HANHC)")
    parser.add_argument("--from-csv", type=str, default=None,
                        help="从 CSV 文件读取酒店代码 (需有 mnemonic 列)")
    parser.add_argument("--from-json", type=str, default=None,
                        help="从 JSON 文件读取酒店代码")
    parser.add_argument("--from-db", action="store_true",
                        help="从 SQLite 数据库读取酒店代码")
    parser.add_argument("--country", type=str, default=None,
                        help="配合 --from-db 按国家筛选酒店")
    parser.add_argument("--concurrency", type=int, default=2,
                        help="并发 Tab 数 (默认 2, 最大 3)")
    parser.add_argument("--incremental", action="store_true",
                        help="增量模式: 只获取最远 62 天窗口")
    parser.add_argument("--days", type=int, default=365,
                        help="全量模式查询天数 (默认 365)")
    parser.add_argument("--dry-run", action="store_true",
                        help="只显示变化, 不保存数据")
    parser.add_argument("--proxy", type=str, default=None,
                        help="代理地址 (如 http://127.0.0.1:7890)")
    parser.add_argument("--auto-switch", action="store_true",
                        help="启用 Clash 自动切换节点 (需配置 notify_config.json 中 clash 字段)")
    args = parser.parse_args()

    # 限制并发数
    concurrency = min(max(args.concurrency, 1), 3)

    # 加载酒店列表
    hotel_codes = load_hotel_codes(args)
    if not hotel_codes:
        print("[!] 未指定酒店代码, 请使用 --codes, --from-csv 或 --from-json")
        return

    # 初始化 Clash 自动切换 (如果启用)
    clash_mgr = None
    if args.auto_switch:
        try:
            from ihg_clash_proxy import ClashProxyManager
            clash_mgr = ClashProxyManager()
            if clash_mgr.is_available():
                print(f"[Clash] ✓ 已连接, 当前节点: {clash_mgr.current_node}")
                print(f"[Clash]   可用节点: {len(clash_mgr.available_nodes)} 个, 每 {clash_mgr.rotate_every_n} 个酒店轮换")
                # 启动时先随机切换一次
                clash_mgr.rotate()
            else:
                print("[Clash] ✗ 无法连接 Clash API, 将不使用自动切换")
                clash_mgr = None
        except Exception as e:
            print(f"[Clash] 初始化失败: {e}, 将不使用自动切换")
            clash_mgr = None

        # 如果启用了 auto-switch 但没指定 --proxy, 自动设为 Clash 本地代理
        if clash_mgr and not args.proxy:
            args.proxy = "http://127.0.0.1:7890"
            print(f"[Clash] 自动设置代理: {args.proxy}")

    # 确定日期窗口
    start = date.today() + timedelta(days=1)
    if args.incremental:
        # 增量模式: 只取最远的一个窗口 (检测新开放)
        far_start = start + timedelta(days=args.days - WINDOW_SIZE_DAYS)
        windows = [(far_start.isoformat(), (far_start + timedelta(days=WINDOW_SIZE_DAYS - 1)).isoformat())]
        mode_str = "增量"
    else:
        # 全量模式
        windows = iter_date_windows(start, args.days, WINDOW_SIZE_DAYS)
        mode_str = "全量"

    print("=" * 80)
    print(f"  IHG 批量价格监控")
    print(f"  模式: {mode_str} | 并发: {concurrency} Tab | 酒店: {len(hotel_codes)} 个")
    print(f"  日期: {windows[0][0]} ~ {windows[-1][1]} ({len(windows)} 个窗口)")
    print(f"  每酒店请求: 现金 {len(windows)} 次 + 积分 {len(windows)} 次")
    est_time = len(hotel_codes) * len(windows) * 2 * (sum(REQUEST_DELAY_MS) / 2 / 1000) / concurrency
    print(f"  预估耗时: ~{est_time:.0f}s ({est_time/60:.1f}min)")
    if args.dry_run:
        print(f"  [dry-run 模式, 不保存数据]")
    if args.proxy:
        print(f"  代理: {args.proxy}")
    if clash_mgr:
        print(f"  Clash自动切换: ✓ (每{clash_mgr.rotate_every_n}个酒店轮换)")
    print("=" * 80)

    # 启动前检测: 代理连通性验证
    if args.proxy:
        print(f"\n[0] 检测代理连通性...")
        try:
            if clash_mgr:
                # 有 Clash 管理器, 用它的方法
                ok, msg = clash_mgr.test_proxy_connectivity(args.proxy)
            else:
                # 没有 Clash 管理器, 手动测试 (用通用连通性检测URL, 避免被反爬拦截)
                import requests as _req
                proxies = {"http": args.proxy, "https": args.proxy}
                ok, msg = False, ""
                for _url in ["http://cp.cloudflare.com/generate_204",
                             "http://www.gstatic.com/generate_204"]:
                    try:
                        _resp = _req.get(
                            _url, proxies=proxies, timeout=10, allow_redirects=False,
                            headers={"User-Agent": "Mozilla/5.0"},
                        )
                        if _resp.status_code in (200, 204):
                            ok = True
                            msg = f"HTTP {_resp.status_code}, 响应时间 {_resp.elapsed.total_seconds():.1f}s"
                            break
                        msg = f"HTTP {_resp.status_code}"
                    except Exception as _e:
                        msg = str(_e)[:100]
        except Exception as e:
            ok = False
            msg = str(e)[:100]

        if ok:
            print(f"    代理可用 ✓ ({msg})")
        else:
            print(f"    代理不可用 ✗ ({msg})")
            print(f"[!] 代理 {args.proxy} 无法连通 IHG, 请检查:")
            print(f"    1. Clash 是否正在运行")
            print(f"    2. 代理端口是否正确")
            print(f"    3. 当前节点是否可用")
            if clash_mgr:
                # 尝试切换节点再试一次
                print(f"[Clash] 尝试切换节点后重试...")
                clash_mgr.rotate()
                import time as _t
                _t.sleep(2)
                ok2, msg2 = clash_mgr.test_proxy_connectivity(args.proxy)
                if ok2:
                    print(f"    切换后可用 ✓ ({msg2})")
                else:
                    print(f"    切换后仍不可用 ✗ ({msg2})")
                    print(f"[!] 退出. 请确认 Clash 代理正常后重新运行.")
                    return
            else:
                print(f"[!] 退出. 请确认代理正常后重新运行.")
                return

    # 创建任务队列
    task_queue = asyncio.Queue()
    for idx, code in enumerate(hotel_codes, 1):
        task_queue.put_nowait((idx, code, len(hotel_codes)))

    results = []

    Path(USER_DATA_DIR).mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        # 构建启动参数
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

        context = await p.chromium.launch_persistent_context(
            USER_DATA_DIR,
            **launch_opts,
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )

        # 创建多个 page (Tab)
        pages = []
        for i in range(concurrency):
            page = await context.new_page()
            pages.append(page)

        try:
            # 逐个 Tab 建立 session (避免同时 goto 触发 Akamai)
            print(f"\n[1] 建立浏览器 session ({concurrency} 个 Tab)...")
            for i, page in enumerate(pages):
                await page.goto(SEED_URL, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(random.randint(1000, 2000))
                print(f"    Tab {i+1} ✓")
            await pages[0].wait_for_timeout(random.randint(500, 1000))
            print("    全部就绪")

            # 启动并发 worker
            print(f"\n[2] 开始获取价格...")
            t_start = time.time()

            worker_tasks = []
            for i, page in enumerate(pages):
                worker_tasks.append(
                    worker(i + 1, page, task_queue, results, windows, args.dry_run, clash_mgr)
                )
            await asyncio.gather(*worker_tasks)

            total_time = time.time() - t_start
            print(f"\n[3] 全部完成, 总耗时: {total_time:.1f}s")

        finally:
            await context.close()

    # 输出报告
    print_report(results)

    # 发送通知 (如果有变动)
    try:
        from ihg_notify import notify_changes
        notify_changes(results, get_db())
    except Exception as e:
        print(f"[通知] 发送失败: {e}")

    # 关闭数据库
    if _db:
        _db.close()


if __name__ == "__main__":
    asyncio.run(main())
