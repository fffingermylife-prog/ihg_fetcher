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

# 全局汇率缓存 (run-level, 不持久化, 每次运行重新查询)
# 格式: {"MYR": 0.213, "JPY": 0.00636, "USD": 1.0, ...}
_usd_rate_cache = {"USD": 1.0}

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


# ============ 汇率转换 (本地币 → USD) ============

async def fetch_usd_rate(page, currency):
    """通过 IHG 官方汇率 API 获取 currency → USD 汇率, 结果缓存到 _usd_rate_cache

    使用 IHG 网站自己的汇率接口 (前端显示美元价时调用的同一个):
        https://apis.ihg.com/finance/conversions/v2/currencies?qFcc=<from>&qTcc=USD&qV=1

    Args:
        page: Playwright page (在浏览器内 fetch, 自动带 cookie)
        currency: 源币种代码 (MYR/JPY/HKD 等)

    Returns:
        float 汇率 (1 单位本地币 = N USD), 或 None 失败时
    """
    if not currency:
        return None
    if currency in _usd_rate_cache:
        return _usd_rate_cache[currency]

    api_url = (
        f"https://apis.ihg.com/finance/conversions/v2/currencies"
        f"?qFcc={currency}&qTcc=USD&qV=1"
    )
    try:
        result = await page.evaluate("""
        async ({ apiUrl, apiKey }) => {
            try {
                const controller = new AbortController();
                const timeout = setTimeout(() => controller.abort(), 10000);
                const resp = await fetch(apiUrl, {
                    method: "GET",
                    headers: {
                        "accept": "application/json, text/plain, */*",
                        "x-ihg-api-key": apiKey,
                    },
                    credentials: "include",
                    signal: controller.signal,
                });
                clearTimeout(timeout);
                if (!resp.ok) return { ok: false, status: resp.status };
                const data = await resp.json();
                return { ok: true, data: data };
            } catch (e) {
                return { ok: false, error: e.message };
            }
        }
        """, {"apiUrl": api_url, "apiKey": API_KEY})

        if result and result.get("ok") and result.get("data"):
            results = result["data"].get("results", [])
            # IHG 接口对 CNY/EUR 等"品牌定制汇率"币种会返回两条:
            #   - source=K: 品牌专用, 实测 CNY 的 K 源停留在 2022-11 的过期值 11.47, 会让本地币 → USD 虚高 78 倍
            #   - source=P: 官方主源, 才是当前实时汇率
            # 必须优先取 P, 取不到再 fallback 到第 0 条
            primary = next((r for r in results if r.get("source") == "P"), None)
            target = primary or (results[0] if results else None)
            if target:
                rate = target.get("result")
                # sanity check: 1 单位本地币 → USD 的合理区间 (1e-7, 5)
                # 上限 5 已覆盖 KWD (~3.27)、BHD (~2.65)、OMR (~2.60) 等最高价值货币
                # 同时挡住 CNY=11.47 这种异常 stale 值
                if rate and 1e-7 < rate < 5:
                    _usd_rate_cache[currency] = rate
                    src = target.get("source", "?")
                    print(f"  [汇率] {currency} → USD: {rate:.6f} (source={src})")
                    return rate
                else:
                    print(f"  [汇率] {currency} 异常值 rate={rate} source={target.get('source')}, 已丢弃")
        print(f"  [汇率] {currency} → USD 获取失败, 跳过 USD 转换")
    except Exception as e:
        print(f"  [汇率] {currency} 异常: {str(e)[:60]}")

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
        merged.append({
            "date": d,
            "cash_price": cash.get("cash_price"),
            "cash_price_after_tax": cash.get("cash_price_after_tax"),
            "currency": cash.get("currency", ""),
            "points": pts.get("points"),
        })

    # 获取该酒店所有币种 → USD 汇率 (通常只有一种, 单次查询)
    currencies = {p["currency"] for p in merged if p.get("currency")}
    for cur in currencies:
        await fetch_usd_rate(page, cur)

    # 计算 USD 价格 + CPP (单位: USD/万分, 即每 10000 积分对应多少 USD)
    # CPP = USD 价格 × 10000 / 积分
    # 例: USD 80, 10000 积分 → CPP = 80 USD/万分 (即每万积分价值 $80)
    for p in merged:
        cur = p.get("currency")
        rate = _usd_rate_cache.get(cur) if cur else None
        cash_local = p.get("cash_price_after_tax") or p.get("cash_price")

        # USD 价格
        if rate and cash_local:
            p["cash_price_usd"] = round(cash_local * rate, 2)
        else:
            p["cash_price_usd"] = None

        # CPP (USD/万分)
        if p["cash_price_usd"] and p.get("points") and p["points"] > 0:
            p["cpp"] = round(p["cash_price_usd"] * 10000 / p["points"], 2)
        else:
            p["cpp"] = None

    return merged


# ============ 对比逻辑 (含房态检测) ============

def compare_prices(old_prices, new_prices):
    """对比新旧价格, 返回变化列表 (含房态检测, 使用含税价)
    注: 自动过滤过期日期 (今天及之前), 只关心未来的价格变动
    """
    old_map = {p["date"]: p for p in old_prices}
    new_map = {p["date"]: p for p in new_prices}
    changes = []
    all_dates = sorted(set(old_map.keys()) | set(new_map.keys()))

    # 过滤过期日期: 只保留 > 今天的日期 (今天和过去都没意义)
    today_str = date.today().isoformat()
    all_dates = [d for d in all_dates if d > today_str]

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


# 酒店名缓存 (避免每次都查 db)
_hotel_name_cache = {}


def get_hotel_name(hotel_code):
    """获取酒店名称 (优先 notify_config.json 的 note 备注, 然后是 db 里的 name)
    返回值: 酒店名称, 找不到则返回空字符串
    """
    if hotel_code in _hotel_name_cache:
        return _hotel_name_cache[hotel_code]

    name = ""
    # 1) 优先用 notify_config.json 里的 note (用户自己起的中文别名)
    try:
        import json as _json
        from pathlib import Path as _P
        cfg_path = _P(__file__).parent / "notify_config.json"
        if cfg_path.exists():
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = _json.load(f)
            note = cfg.get("hotels", {}).get(hotel_code, {}).get("note", "")
            if note:
                name = note
    except Exception:
        pass

    # 2) 再用数据库里的 name
    if not name:
        try:
            db = get_db()
            hotel = db.get_hotel(hotel_code)
            if hotel and hotel.get("name"):
                name = hotel["name"]
        except Exception:
            pass

    _hotel_name_cache[hotel_code] = name
    return name


def format_hotel_label(hotel_code, max_name_len=None):
    """格式化酒店标签: "CODE (名称)" 或仅 "CODE"
    Args:
        hotel_code: 酒店代码
        max_name_len: 名称最大显示长度 (默认 None 不截断, 传整数则截断超出部分)
    """
    name = get_hotel_name(hotel_code)
    if not name:
        return hotel_code
    if max_name_len is not None and len(name) > max_name_len:
        name = name[:max_name_len] + "..."
    return f"{hotel_code} ({name})"


def load_baseline(hotel_code):
    """从 SQLite 加载最近一次采集的价格作为基线"""
    db = get_db()
    return db.load_latest_prices(hotel_code)


def save_prices(hotel_code, prices, days_queried):
    """保存价格数据到 SQLite"""
    db = get_db()
    db.save_prices(hotel_code, prices)


# ============ 并发 Worker ============

async def worker(worker_id, page, batch_queue, results, requeue, windows, dry_run, abort_event):
    """并发 worker (批次模式): 从批次队列取酒店, 跑完即写入 results;
    任何失败立即触发 abort_event 通知主流程关 context 重建。

    Args:
        batch_queue: 仅含本批次酒店的 asyncio.Queue, 元素 (idx, code, total)
        results: 成功结果会 append 到这个 list (主流程共享)
        requeue: 失败/未完成的酒店 code 会 append 到这个 list, 主流程下批重试
        abort_event: 任一 worker 检测到失败 → set; 其他 worker 跑完手头任务即退出
    """
    while True:
        # 1. 检查批次是否已被通知中止
        if abort_event.is_set():
            return

        # 2. 从队列取一个酒店任务
        try:
            idx, hotel_code, total_count = batch_queue.get_nowait()
        except asyncio.QueueEmpty:
            return

        label = format_hotel_label(hotel_code)
        t0 = time.time()
        success = False
        prices = None
        retries = 0

        # 3. 单酒店整体超时 120 秒 (正常 6~8 秒), 失败重试 MAX_RETRIES 次
        #    用 try-finally 保证: 即使 worker 中途被外部 cancel, 也能把任务放回 requeue
        completed = False
        try:
            while retries <= MAX_RETRIES:
                try:
                    prices = await asyncio.wait_for(
                        fetch_hotel_prices(page, hotel_code, windows),
                        timeout=120
                    )
                    success = True
                    break
                except asyncio.TimeoutError:
                    retries += 1
                    if retries <= MAX_RETRIES:
                        print(f"  [W{worker_id}] {label} 超时, 重试 ({retries}/{MAX_RETRIES})...")
                        await page.wait_for_timeout(random.randint(3000, 5000))
                    else:
                        print(f"  [W{worker_id}] {label} 超时, 触发批次重建")
                except Exception as e:
                    retries += 1
                    if retries <= MAX_RETRIES:
                        print(f"  [W{worker_id}] {label} 失败, 重试 ({retries}/{MAX_RETRIES})...")
                        await page.wait_for_timeout(random.randint(2000, 4000))
                    else:
                        print(f"  [W{worker_id}] {label} 失败, 触发批次重建: {str(e)[:100]}")

            elapsed = time.time() - t0

            if success and prices:
                # 加载基线对比 (old_date 用于报告中说明对比基准)
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
                    "baseline_date": old_date,
                    "prices": prices,
                })
                status = "首次" if not old_prices else f"{len(changes)}变化"
                print(f"  [{idx}/{total_count}] {label} ✓ {elapsed:.1f}s ({len(prices)}天, {status})")
                completed = True
            else:
                # 失败: 放回 requeue 让下个批次 (新节点) 重试, 并触发整批中止
                requeue.append(hotel_code)
                abort_event.set()
                print(f"  [{idx}/{total_count}] {label} ✗ {elapsed:.1f}s (失败, 已触发批次重建)")
                completed = True
        finally:
            # 兜底: 如果 worker 被外部 cancel 或抛出未捕获异常,
            # 当前任务也要放回 requeue, 避免酒店被吞掉
            if not completed:
                requeue.append(hotel_code)
                abort_event.set()


async def run_batch(playwright, batch_codes, total_index_map, concurrency, windows,
                    dry_run, launch_opts):
    """跑一个批次: 启动新 BrowserContext → 多 worker 并发 → 关 context

    重要: 每个批次绑定一个 Clash 节点。本函数生命周期内 context 全程使用同一个
    出口节点; 关闭 context 后所有 socket 释放, 主流程切换节点再重建 context,
    才能保证下一批流量真正走到新节点 (解决 Playwright 持久连接复用旧节点问题)。

    Args:
        playwright: 当前 async_playwright 实例
        batch_codes: 这一批要跑的酒店代码列表
        total_index_map: dict[code -> (idx, total)] 用于显示全局进度 "X/Y"
        concurrency: 本批并发 Tab 数
        windows: 日期窗口列表
        dry_run: 是否跳过持久化
        launch_opts: launch_persistent_context 参数 (含 proxy)

    Returns:
        (results, requeue):
            results - 本批跑成功的酒店结果 list
            requeue - 因失败/abort 未完成的酒店 code list (含失败那个 + 队列剩余)
    """
    Path(USER_DATA_DIR).mkdir(parents=True, exist_ok=True)

    results = []
    requeue = []
    abort_event = asyncio.Event()

    # 1. 启动 context (新节点的连接池从这里开始)
    context = await playwright.chromium.launch_persistent_context(
        USER_DATA_DIR,
        **launch_opts,
    )
    await context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
    )

    try:
        # 2. 创建 page (Tab) 并逐个建立 session (避免同时 goto 触发 Akamai)
        pages = []
        session_ok = True
        for i in range(concurrency):
            page = await context.new_page()
            pages.append(page)

        for i, page in enumerate(pages):
            try:
                await page.goto(SEED_URL, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(random.randint(1000, 2000))
                print(f"    Tab {i+1} session 就绪 ✓")
            except Exception as e:
                print(f"    Tab {i+1} session 建立失败: {str(e)[:80]}")
                session_ok = False
                break

        if not session_ok:
            # session 阶段就失败 → 整批 requeue, 主流程切节点重试
            requeue.extend(batch_codes)
            return results, requeue

        # 3. 准备批次队列
        batch_queue = asyncio.Queue()
        for code in batch_codes:
            idx, total = total_index_map.get(code, (0, len(batch_codes)))
            batch_queue.put_nowait((idx, code, total))

        # 4. 启动并发 worker
        worker_tasks = [
            worker(i + 1, page, batch_queue, results, requeue, windows, dry_run, abort_event)
            for i, page in enumerate(pages)
        ]
        await asyncio.gather(*worker_tasks)

        # 5. abort 时队列里可能还有未取走的酒店, 也放回 requeue
        while not batch_queue.empty():
            try:
                _, code, _ = batch_queue.get_nowait()
                if code not in requeue:  # 防御性去重
                    requeue.append(code)
            except asyncio.QueueEmpty:
                break
    finally:
        # 关闭 context: 释放所有 socket, 下批重建后才会建立新连接到新节点
        try:
            await context.close()
        except Exception as e:
            print(f"    [!] 关闭 context 异常 (忽略): {str(e)[:80]}")

    return results, requeue


# ============ 报告输出 ============

# 日志目录 (每次运行一个详情文件)
LOG_DIR = Path(__file__).parent / "ihg_logs"

# 优先级顺序 (重要变动放前面)
PRIORITY_ORDER = [
    "积分房售罄", "积分房重新开放", "新开放",
    "现金售罄", "现金重新开放",
    "积分降", "积分涨", "现金降", "现金涨",
]

# 类型简写 (用于命令行精简显示)
TYPE_SHORT = {
    "积分房售罄": "积售罄",
    "积分房重新开放": "积开放",
    "现金售罄": "现售罄",
    "现金重新开放": "现开放",
    "新开放": "新开放",
    "积分降": "积降",
    "积分涨": "积涨",
    "现金降": "现降",
    "现金涨": "现涨",
}


def print_report(results):
    """输出汇总变价报告
    - 命令行: 精简显示 (每酒店一行汇总, 不展开日期)
    - 日志文件: 完整详情 (按类型分组 + 每个日期变动 + 对比基准时间)
    """
    all_changes = []
    # 收集每个酒店的对比基准时间, 用于在报告中标注 "vs 上次 YYYY-MM-DD HH:MM"
    baseline_by_hotel = {}
    for r in results:
        if r["success"]:
            if r.get("baseline_date"):
                baseline_by_hotel[r["hotel_code"]] = r["baseline_date"]
            if r["changes"]:
                for c in r["changes"]:
                    c["hotel_code"] = r["hotel_code"]
                all_changes.extend(r["changes"])

    # 写日志文件 (无论是否有变动都写, 方便回溯)
    log_path = _write_detail_log(results, all_changes, baseline_by_hotel)

    if not all_changes:
        print(f"\n  ✓ 所有酒店无价格/房态变动")
        if log_path:
            print(f"  详细日志: {log_path}")
        return

    # ===== 命令行: 精简显示 =====
    print(f"\n{'='*80}")
    print(f"  变动汇总 (对比基准: 各酒店上一次采集快照)")
    print(f"{'='*80}")

    # 按酒店聚合: hotel_code -> {type: count}
    hotel_summary = {}
    for c in all_changes:
        code = c["hotel_code"]
        t = c["type"]
        if code not in hotel_summary:
            hotel_summary[code] = {}
        hotel_summary[code][t] = hotel_summary[code].get(t, 0) + 1

    # 排序: 有更重要变动 (积分房售罄/重新开放/新开放) 的酒店排前
    def hotel_priority(code):
        types = hotel_summary[code]
        # 用最高优先级类型作为主排序键
        for i, t in enumerate(PRIORITY_ORDER):
            if t in types:
                return (i, -sum(types.values()))  # 同优先级, 变动多的排前
        return (99, 0)

    sorted_codes = sorted(hotel_summary.keys(), key=hotel_priority)

    for code in sorted_codes:
        label = format_hotel_label(code)
        types = hotel_summary[code]
        # 按 PRIORITY_ORDER 顺序拼出 "5积降, 3积售罄" 这样的简短统计
        parts = []
        for t in PRIORITY_ORDER:
            if t in types:
                parts.append(f"{types[t]}{TYPE_SHORT.get(t, t)}")
        # 剩余类型
        for t, n in types.items():
            if t not in PRIORITY_ORDER:
                parts.append(f"{n}{t}")
        summary_str = ", ".join(parts)
        print(f"  {label}: {summary_str}")

    # 命令行底部统计 + 日志路径
    print(f"\n{'='*80}")
    success_count = sum(1 for r in results if r["success"])
    fail_count = sum(1 for r in results if not r["success"])
    first_run = sum(1 for r in results if r.get("is_first_run"))
    total_changes = len(all_changes)
    print(f"  统计: 成功 {success_count} | 失败 {fail_count} | 首次运行 {first_run} | 总变动 {total_changes}")
    if log_path:
        print(f"  详细日志: {log_path}")
    print(f"{'='*80}")


def _write_detail_log(results, all_changes, baseline_by_hotel):
    """写完整详情到日志文件, 返回文件路径; 失败返回 None"""
    try:
        from datetime import datetime
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = LOG_DIR / f"monitor_{ts}.log"

        lines = []
        lines.append("=" * 80)
        lines.append(f"  IHG 价格监控详细日志")
        lines.append(f"  生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"  对比基准: 各酒店上一次采集快照")
        lines.append("=" * 80)

        # 顶部统计
        success_count = sum(1 for r in results if r["success"])
        fail_count = sum(1 for r in results if not r["success"])
        first_run = sum(1 for r in results if r.get("is_first_run"))
        lines.append(f"\n  统计: 成功 {success_count} | 失败 {fail_count} | 首次运行 {first_run} | 总变动 {len(all_changes)}\n")

        # 失败列表
        failed = [r for r in results if not r["success"]]
        if failed:
            lines.append("─" * 80)
            lines.append(f"  失败酒店 ({len(failed)} 个):")
            lines.append("─" * 80)
            for r in failed:
                lines.append(f"  {format_hotel_label(r['hotel_code'])} (耗时 {r['elapsed']:.1f}s)")
            lines.append("")

        if not all_changes:
            lines.append("\n  ✓ 所有酒店无价格/房态变动\n")
        else:
            # 按类型分组展示完整详情
            type_groups = {}
            for c in all_changes:
                type_groups.setdefault(c["type"], []).append(c)

            lines.append("─" * 80)
            lines.append(f"  变动详情 (按类型分组)")
            lines.append("─" * 80)

            seen_types = set()
            for change_type in PRIORITY_ORDER:
                items = type_groups.get(change_type, [])
                if not items:
                    continue
                seen_types.add(change_type)
                lines.append(f"\n[{change_type}] {len(items)} 条:")

                # 按酒店再分组
                by_hotel = {}
                for item in items:
                    by_hotel.setdefault(item["hotel_code"], []).append(item)

                for code, hotel_items in by_hotel.items():
                    label = format_hotel_label(code)
                    baseline = baseline_by_hotel.get(code, "")
                    baseline_hint = f"  [基准: {baseline}]" if baseline else ""
                    lines.append(f"  {label}{baseline_hint}")
                    for item in hotel_items:
                        detail = format_change_detail(item)
                        lines.append(f"    {item['date']} {detail}")

            # 剩余类型
            for change_type, items in type_groups.items():
                if change_type in seen_types:
                    continue
                lines.append(f"\n[{change_type}] {len(items)} 条:")
                by_hotel = {}
                for item in items:
                    by_hotel.setdefault(item["hotel_code"], []).append(item)
                for code, hotel_items in by_hotel.items():
                    label = format_hotel_label(code)
                    baseline = baseline_by_hotel.get(code, "")
                    baseline_hint = f"  [基准: {baseline}]" if baseline else ""
                    lines.append(f"  {label}{baseline_hint}")
                    for item in hotel_items:
                        detail = format_change_detail(item)
                        lines.append(f"    {item['date']} {detail}")

        lines.append("\n" + "=" * 80)
        lines.append(f"  日志结束")
        lines.append("=" * 80)

        with open(log_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return log_path
    except Exception as e:
        print(f"[!] 写日志失败: {e}")
        return None


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
    parser.add_argument("--concurrency", type=int, default=3,
                        help="并发 Tab 数 (默认 3, 最大 3)")
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

    # 批次大小: 启用 Clash 时按 rotate_every_n 切批, 否则一批跑完
    if clash_mgr:
        batch_size = max(1, clash_mgr.rotate_every_n)
    else:
        batch_size = max(1, len(hotel_codes))

    est_batches = (len(hotel_codes) + batch_size - 1) // batch_size

    print("=" * 80)
    print(f"  IHG 批量价格监控")
    print(f"  模式: {mode_str} | 并发: {concurrency} Tab | 酒店: {len(hotel_codes)} 个")
    print(f"  日期: {windows[0][0]} ~ {windows[-1][1]} ({len(windows)} 个窗口)")
    print(f"  每酒店请求: 现金 {len(windows)} 次 + 积分 {len(windows)} 次")
    if clash_mgr:
        print(f"  批次模式: 每批 {batch_size} 个酒店 → 关 context + 切节点 + 重建 (估 {est_batches} 批)")
    else:
        print(f"  单批模式: 直连无切换")
    # 预估耗时: 抓取耗时 + 每批重建 context 约 8 秒开销
    est_fetch = len(hotel_codes) * len(windows) * 2 * (sum(REQUEST_DELAY_MS) / 2 / 1000) / concurrency
    est_overhead = est_batches * 8 if clash_mgr else 0
    est_time = est_fetch + est_overhead
    print(f"  预估耗时: ~{est_time:.0f}s ({est_time/60:.1f}min)")
    if args.dry_run:
        print(f"  [dry-run 模式, 不保存数据]")
    if args.proxy:
        print(f"  代理: {args.proxy}")
    if clash_mgr:
        print(f"  Clash当前节点: {clash_mgr.current_node}")
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

    # ============ 批次循环 ============
    # 每批: 启动新 context (绑定当前节点) → 跑 N 个酒店 → 关 context → 切节点 → 下一批
    # 同一批内任一失败 → abort 整批 → 未完成酒店放回队首, 下批 (新节点) 重试
    #
    # 三道安全锁 (避免主循环死循环):
    # 1) processed_codes: 已成功或已放弃的酒店, 任何情况下都不会再进 batch
    # 2) MAX_TOTAL_ITERATIONS: 总迭代上限, 超过强制退出
    # 3) 进度停滞检测: 连续 5 批没新增成功 + remaining 没缩小, 强制放弃剩余

    # 全局索引映射 (按初始顺序固定, 重试时显示同一个 idx)
    total_index_map = {code: (idx, len(hotel_codes)) for idx, code in enumerate(hotel_codes, 1)}

    # 跨批次尝试计数 (避免一个酒店被反复重试无限循环)
    MAX_BATCH_ATTEMPTS = 3
    attempt_count = {code: 0 for code in hotel_codes}
    abandoned = []   # 超过 MAX_BATCH_ATTEMPTS 后放弃的酒店

    # 已完结集合 (成功 or 放弃), 双保险防止酒店在 batch / remaining 间循环
    processed_codes = set()
    # 卡死保护
    MAX_TOTAL_ITERATIONS = max(50, 10 * len(hotel_codes))  # 远大于正常情况的上限
    STAGNATION_LIMIT = 5  # 连续多少批无进展则强制结束

    # 构建 launch 参数 (每批用同一份, 但 context 每批新建)
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

    remaining = list(hotel_codes)
    results = []
    batch_num = 0
    stagnation_count = 0
    last_progress_metric = (0, len(hotel_codes))  # (results_len, remaining_len)

    async with async_playwright() as p:
        t_start = time.time()

        while remaining:
            batch_num += 1

            # ===== 安全锁 #2: 总迭代上限 =====
            if batch_num > MAX_TOTAL_ITERATIONS:
                print(f"\n[!] 已超过 MAX_TOTAL_ITERATIONS={MAX_TOTAL_ITERATIONS} 批, 强制结束")
                print(f"    剩余未跑: {len(remaining)} 个 → 全部移入 abandoned")
                for code in remaining:
                    if code not in abandoned and code not in processed_codes:
                        abandoned.append(code)
                        processed_codes.add(code)
                remaining = []
                break

            # ===== 安全锁 #1: 过滤掉已完结的酒店 =====
            # 任何理由 (workers 重复 requeue / actually_requeue 含意外重复 / etc) 都不会让
            # 已经 success 或 abandon 的酒店再进入批次
            before_filter = len(remaining)
            remaining = [c for c in remaining if c not in processed_codes]
            filtered_n = before_filter - len(remaining)
            if filtered_n > 0:
                print(f"[批次 {batch_num}] 过滤掉 {filtered_n} 个已完结酒店")

            if not remaining:
                break

            # ===== 取一批 =====
            batch = []
            new_remaining = []
            for code in remaining:
                if len(batch) < batch_size:
                    if attempt_count[code] >= MAX_BATCH_ATTEMPTS:
                        if code not in abandoned:
                            abandoned.append(code)
                        processed_codes.add(code)
                        continue
                    batch.append(code)
                    attempt_count[code] += 1
                else:
                    # 即使 batch 满了, 这里也要检查是否已超尝试次数, 避免 abandoned 无限往后挤
                    if attempt_count[code] >= MAX_BATCH_ATTEMPTS:
                        if code not in abandoned:
                            abandoned.append(code)
                        processed_codes.add(code)
                    else:
                        new_remaining.append(code)
            remaining = new_remaining

            if not batch:
                continue

            node_label = clash_mgr.current_node if clash_mgr else "直连"
            print(f"\n{'─'*80}")
            print(f"[批次 {batch_num}] 节点: {node_label} | 本批 {len(batch)} 个 | 待跑剩余 {len(remaining)} 个 "
                  f"| 已成功 {len(results)} | 已放弃 {len(abandoned)}")
            print(f"  酒店: {', '.join(batch)}")
            print(f"{'─'*80}")

            t_batch = time.time()
            try:
                batch_results, batch_requeue = await run_batch(
                    p, batch, total_index_map, concurrency, windows,
                    args.dry_run, launch_opts,
                )
            except Exception as e:
                # run_batch 自身崩溃 (极少): 整批 requeue, 切节点重试
                print(f"[批次 {batch_num}] run_batch 异常: {str(e)[:120]}")
                batch_results = []
                batch_requeue = list(batch)

            results.extend(batch_results)
            # 标记本批成功的酒店为已完结
            for r in batch_results:
                processed_codes.add(r["hotel_code"])

            batch_elapsed = time.time() - t_batch

            # 失败/未完成的放回队首 (优先在下个批次/新节点重试)
            if batch_requeue:
                seen_in_results = {r["hotel_code"] for r in batch_results}
                # 双重过滤: 不在本批结果里 + 不在已完结集合里
                actually_requeue = [
                    c for c in batch_requeue
                    if c not in seen_in_results and c not in processed_codes
                ]
                remaining = actually_requeue + remaining
                print(f"[批次 {batch_num}] 完成 {len(batch_results)}/{len(batch)} | "
                      f"放回 {len(actually_requeue)} 个待重试 | 耗时 {batch_elapsed:.1f}s")
            else:
                print(f"[批次 {batch_num}] 完成 ✓ {len(batch_results)}/{len(batch)} | 耗时 {batch_elapsed:.1f}s")

            # ===== 安全锁 #3: 进度停滞检测 =====
            cur_metric = (len(results), len(remaining))
            if cur_metric == last_progress_metric:
                stagnation_count += 1
                if stagnation_count >= STAGNATION_LIMIT:
                    print(f"\n[!] 连续 {STAGNATION_LIMIT} 批无进展 (results={cur_metric[0]} remaining={cur_metric[1]}), 强制结束")
                    for code in remaining:
                        if code not in processed_codes:
                            abandoned.append(code)
                            processed_codes.add(code)
                    remaining = []
                    break
            else:
                stagnation_count = 0
                last_progress_metric = cur_metric

            # 如果还有酒店要跑, 切换节点准备下一批
            if remaining and clash_mgr:
                print(f"[批次 {batch_num}] 切换 Clash 节点准备下一批...")
                clash_mgr.rotate()
                await asyncio.sleep(2)  # 给 Clash 路由表更新时间

        total_time = time.time() - t_start
        print(f"\n[完成] 共 {batch_num} 批, 总耗时: {total_time:.1f}s "
              f"(成功 {len(results)} | 放弃 {len(abandoned)})")

    # 标记被放弃的酒店 (放进 results 让报告里看到)
    for code in abandoned:
        results.append({
            "hotel_code": code,
            "success": False,
            "elapsed": 0,
            "changes": [],
            "abandoned": True,
        })
    if abandoned:
        print(f"[!] {len(abandoned)} 个酒店超过 {MAX_BATCH_ATTEMPTS} 次重试仍失败, 已放弃: {', '.join(abandoned)}")

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
