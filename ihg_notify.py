"""
IHG 价格告警通知模块

功能:
    - 从 batch_monitor 的对比结果中筛选值得通知的变动
    - 支持 Server酱 微信推送
    - 中国节假日识别 (含前后各2天缓冲)

告警等级 (优先级从高到低):
    🔴 积分房重新开放 — 节假日 + 积分价 ≤ 平日均价×reopen_max_ratio
    💎 高 CPP 积分房 — 任意未来日期 + CPP ≥ min_cpp_threshold
    🟣 现金深折扣  — 任意未来日期 + 现金价 ≤ 平日均价×cash_deal_ratio
    🟠 积分大降价  — 降幅 ≥ points_drop_pct
    🟡 现金大降价  — 降幅 ≥ cash_drop_pct
    🟢 新开放低价  — 新出现的日期 + 价格 ≤ 均价×(1 - new_date_below_avg_pct/100)

基准均价策略 (现金/积分):
    1. 优先用历史所有快照的平日(周一~周四+非节假日)均价
    2. 历史样本不足 (< min_baseline_samples) 时, 用本次快照的平日均价 (首次采集场景)
    3. 本次快照样本也不足 → 跳过该酒店的高性价比检测 (避免误报)

用法:
    # 作为模块被 ihg_batch_monitor.py 调用
    from ihg_notify import notify_changes

    # 独立测试
    python ihg_notify.py --test
    python ihg_notify.py --url HKGKL:2026-10-01:2
"""

import json
import sys
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode

try:
    import requests
except ImportError:
    requests = None


# ============ 配置 ============

CONFIG_PATH = Path(__file__).parent / "notify_config.json"

DEFAULT_RULES = {
    # 基于 changes 的传统告警
    "points_drop_pct": 40,           # 🟠 积分降幅 ≥ 40% 通知
    "cash_drop_pct": 40,             # 🟡 现金降幅 ≥ 40% 通知
    "new_date_below_avg_pct": 30,    # 🟢 新开放日期 ≤ 均价×0.7 (低于30%)
    "reopen_max_ratio": 0.9,         # 🔴 节假日重开积分 ≤ 平日均价×0.9

    # 基于本次快照的高性价比扫描 (新增)
    "cash_deal_ratio": 0.6,          # 🟣 现金 ≤ 平日均价×0.6 视为深折扣
    "min_cpp_threshold": 0.6,        # 💎 CPP ≥ 0.6 视为高性价比积分房
    "top_n_per_hotel": 5,            # 每家酒店最多推送 N 条高性价比
    "min_baseline_samples": 14,      # 平日均价至少需要 N 个有效样本
}

BOOKING_DEFAULT_NIGHTS = 1


# ============ 中国节假日 2026~2027 ============

def get_chinese_holidays():
    """获取 2026~2027 中国主要节假日 (含前后各2天缓冲)"""
    holidays_core = {
        # 2026
        "2026-01-01": "元旦",
        "2026-02-16": "除夕", "2026-02-17": "春节", "2026-02-18": "春节",
        "2026-02-19": "春节", "2026-02-20": "春节", "2026-02-21": "春节",
        "2026-02-22": "春节", "2026-02-23": "春节", "2026-02-24": "春节初八",
        "2026-04-05": "清明",
        "2026-05-01": "五一", "2026-05-02": "五一", "2026-05-03": "五一",
        "2026-05-04": "五一", "2026-05-05": "五一",
        "2026-05-31": "端午",
        "2026-09-25": "中秋",
        "2026-10-01": "国庆", "2026-10-02": "国庆", "2026-10-03": "国庆",
        "2026-10-04": "国庆", "2026-10-05": "国庆", "2026-10-06": "国庆",
        "2026-10-07": "国庆",
        # 2027
        "2027-01-01": "元旦",
        "2027-02-05": "除夕", "2027-02-06": "春节", "2027-02-07": "春节",
        "2027-02-08": "春节", "2027-02-09": "春节", "2027-02-10": "春节",
        "2027-02-11": "春节", "2027-02-12": "春节", "2027-02-13": "春节初八",
        "2027-04-05": "清明",
        "2027-05-01": "五一", "2027-05-02": "五一", "2027-05-03": "五一",
        "2027-05-04": "五一", "2027-05-05": "五一",
        "2027-06-19": "端午",
        "2027-09-15": "中秋",
        "2027-10-01": "国庆", "2027-10-02": "国庆", "2027-10-03": "国庆",
        "2027-10-04": "国庆", "2027-10-05": "国庆", "2027-10-06": "国庆",
        "2027-10-07": "国庆",
    }

    # 每个核心日期前后各加 2 天作为缓冲
    all_holiday_dates = set()
    for d_str in holidays_core:
        d = date.fromisoformat(d_str)
        for offset in range(-2, 3):
            all_holiday_dates.add((d + timedelta(days=offset)).isoformat())

    return all_holiday_dates, holidays_core


HOLIDAY_DATES, HOLIDAY_NAMES = get_chinese_holidays()


def is_holiday(date_str):
    """判断日期是否是节假日 (含前后2天缓冲)"""
    return date_str in HOLIDAY_DATES


def get_holiday_name(date_str):
    """获取节假日名称 (如果是核心日期)"""
    return HOLIDAY_NAMES.get(date_str, "假期")


def is_weekday(date_str):
    """判断是否是平日 (周一到周四)"""
    d = date.fromisoformat(date_str)
    return d.weekday() < 4  # 0=周一, 4=周五


def is_weekday_non_holiday(date_str):
    """判断是否是平日且非节假日 (用于均价基准)"""
    return is_weekday(date_str) and not is_holiday(date_str)


# ============ 预订链接 ============

def build_booking_url(hotel_code, check_in_date, nights=None):
    """生成 IHG 官网预订链接 (微信内置浏览器可直接打开)

    使用 IHG 官方 redirect 服务, 自动跳转到正确品牌页面。
    关键参数 adjustMonth=true + monthIndex=01 防止月份偏移。

    Args:
        hotel_code: 酒店代码 (如 HKGKL)
        check_in_date: 入住日期, 字符串 (YYYY-MM-DD) 或 date 对象
        nights: 入住天数, 默认 1
    """
    if isinstance(check_in_date, str):
        check_in = date.fromisoformat(check_in_date)
    else:
        check_in = check_in_date

    if nights is None:
        nights = BOOKING_DEFAULT_NIGHTS
    check_out = check_in + timedelta(days=nights)

    params = {
        "path": "rates",
        "hotelCode": hotel_code,
        "regionCode": "1",
        "localeCode": "en",
        "checkInDate": check_in.day,
        "checkInMonthYear": f"{check_in.month:02d}{check_in.year}",
        "checkOutDate": check_out.day,
        "checkOutMonthYear": f"{check_out.month:02d}{check_out.year}",
        "numberOfAdults": "1",
        "numberOfRooms": "1",
        "adjustMonth": "true",
        "monthIndex": "01",
    }
    return f"https://www.ihg.com/redirect?{urlencode(params)}"


# ============ 配置加载 ============

def load_config():
    """加载通知配置, 与默认值合并"""
    config = {
        "server_chan_key": "",
        "rules": DEFAULT_RULES.copy(),
        "hotels": {},
    }

    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user_config = json.load(f)
            config.update(user_config)
            # 用户配置覆盖默认规则 (浅合并保证缺失字段有缺省值)
            if "rules" in user_config:
                config["rules"] = {**DEFAULT_RULES, **user_config["rules"]}
        except Exception as e:
            print(f"[通知] 配置加载失败: {e}, 使用默认配置")

    return config


def save_default_config():
    """生成默认配置文件模板"""
    template = {
        "server_chan_key": "你的SendKey (从 sct.ftqq.com 获取)",
        "rules": DEFAULT_RULES,
        "hotels": {
            "HKGKL": {"note": "香港金域假日"},
            "DADHA": {"note": "岘港洲际"},
        }
    }
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(template, f, indent=2, ensure_ascii=False)
    print(f"[通知] 已生成配置模板: {CONFIG_PATH}")


# ============ 基准均价计算 ============

def _avg_or_none(values):
    """求均值, 空列表返回 None"""
    return sum(values) / len(values) if values else None


def compute_baseline(hotel_code, current_prices, db, kind, min_samples):
    """智能计算基准均价

    策略:
        1. 优先用历史所有快照的平日(非节假日)均价
        2. 历史样本不足时, 退回用本次快照的平日均价 (首次采集)
        3. 本次快照也不足 → 返回 None (跳过该酒店性价比检测)

    Args:
        hotel_code: 酒店代码
        current_prices: 本次快照的价格列表 [{"date": ..., "cash_price_after_tax": ..., "points": ...}, ...]
        db: IHGDatabase 实例
        kind: "cash" 或 "points"
        min_samples: 最小样本数门槛

    Returns:
        (avg, sample_count, source) — source 为 "历史" / "本次" / ""
    """
    # 1. 历史所有快照的平日值
    if kind == "cash":
        history = db.get_all_cash_prices(hotel_code)
    else:
        history = db.get_all_points_prices(hotel_code)

    history_weekday = [v for d, v in history if v and is_weekday_non_holiday(d)]
    if len(history_weekday) >= min_samples:
        return _avg_or_none(history_weekday), len(history_weekday), "历史"

    # 2. 本次快照的平日值 (首次采集 fallback)
    if kind == "cash":
        current_values = [
            (p.get("cash_price_after_tax") or p.get("cash_price"))
            for p in current_prices
            if (p.get("cash_price_after_tax") or p.get("cash_price"))
            and is_weekday_non_holiday(p["date"])
        ]
    else:
        current_values = [
            p["points"] for p in current_prices
            if p.get("points") and is_weekday_non_holiday(p["date"])
        ]

    if len(current_values) >= min_samples:
        return _avg_or_none(current_values), len(current_values), "本次"

    # 3. 样本不足
    return None, 0, ""


# ============ 高性价比日期扫描 (新增) ============

def find_high_value_dates(current_prices, baseline_cash, baseline_points, rules):
    """从本次快照中找出高性价比日期 (现金深折扣 + 高 CPP 积分房)

    Args:
        current_prices: 本次快照
        baseline_cash: 现金基准均价 (None 则跳过现金检测)
        baseline_points: 积分基准均价 (此处暂未使用, 保留扩展性)
        rules: 配置规则

    Returns:
        list of dict, 每条 {kind, date, score, ...}, 已按 score 降序
    """
    cash_ratio = rules.get("cash_deal_ratio", 0.6)
    min_cpp = rules.get("min_cpp_threshold", 0.6)
    today_str = date.today().isoformat()

    candidates = []

    for p in current_prices:
        d = p.get("date", "")
        if not d or d <= today_str:
            continue  # 跳过过期日期

        cash = p.get("cash_price_after_tax") or p.get("cash_price")
        points = p.get("points")
        cpp = p.get("cpp")

        # 🟣 现金深折扣
        if cash and baseline_cash:
            ratio = cash / baseline_cash
            if ratio <= cash_ratio:
                candidates.append({
                    "kind": "cash",
                    "date": d,
                    "value": cash,
                    "currency": p.get("currency", ""),
                    "baseline": baseline_cash,
                    "discount_pct": (1 - ratio) * 100,
                    "score": 1 - ratio,  # 0.4 = 便宜 40%
                    "points": points,
                    "cpp": cpp,
                })

        # 💎 高 CPP 积分房
        if cpp and points and cpp >= min_cpp:
            candidates.append({
                "kind": "points",
                "date": d,
                "value": points,
                "cash": cash,
                "currency": p.get("currency", ""),
                "cpp": cpp,
                "min_cpp": min_cpp,
                "score": (cpp - min_cpp) / min_cpp,  # 比阈值高多少倍
            })

    # 按 score 降序 (性价比高的排前)
    candidates.sort(key=lambda x: -x["score"])
    return candidates


# ============ 酒店显示名 ============

def get_hotel_display_name(hotel_code, db, config):
    """获取酒店显示名: 数据库 name → 配置 note → 仅代码"""
    hotel_info = db.get_hotel(hotel_code)
    if hotel_info and hotel_info.get("name"):
        return f"{hotel_info['name']} [{hotel_code}]"

    note = config.get("hotels", {}).get(hotel_code, {}).get("note", "")
    if note:
        return f"{note} [{hotel_code}]"

    return hotel_code


# ============ 告警筛选 ============

def filter_alerts(results, db, config):
    """从 batch_monitor 的 results 中筛选告警

    整合两条数据源:
        1. 基于 changes (变化对比) 的 4 类传统告警
        2. 基于 current_prices (本次快照) 的 2 类高性价比扫描 (新增)
    """
    rules = {**DEFAULT_RULES, **config.get("rules", {})}
    today_str = date.today().isoformat()
    alerts = []

    for r in results:
        if not r.get("success"):
            continue

        hotel_code = r["hotel_code"]
        hotel_label = get_hotel_display_name(hotel_code, db, config)
        changes = r.get("changes", [])
        current_prices = r.get("prices", [])

        # 计算基准均价 (历史优先, 本次 fallback)
        min_samples = rules.get("min_baseline_samples", 14)
        avg_cash, n_cash, src_cash = compute_baseline(
            hotel_code, current_prices, db, "cash", min_samples)
        avg_pts, n_pts, src_pts = compute_baseline(
            hotel_code, current_prices, db, "points", min_samples)

        # === 1. 基于 changes 的传统告警 ===
        for c in changes:
            d = c.get("date", "")
            if not d or d <= today_str:
                continue  # 双重保险: 过滤过期日期

            ctype = c["type"]

            # 🔴 积分房重新开放 (节假日 + 价格合理)
            if ctype == "积分房重新开放":
                new_pts = c.get("new_value")
                if new_pts and is_holiday(d):
                    max_ratio = rules.get("reopen_max_ratio", 0.9)
                    threshold = avg_pts * max_ratio if avg_pts else None
                    if threshold is None or new_pts <= threshold:
                        ratio_hint = (f"≤均价×{max_ratio:.2f}={threshold:.0f}"
                                      if threshold else "无均价基准")
                        avg_hint = (f", 平日均价{avg_pts:.0f}({src_pts}{n_pts}样本)"
                                    if avg_pts else "")
                        alerts.append({
                            "level": "🔴",
                            "rank": 0,
                            "type": "积分房重新开放(节假日)",
                            "hotel": hotel_code,
                            "label": hotel_label,
                            "date": d,
                            "dim": "points",
                            "holiday": get_holiday_name(d),
                            "detail": f"积分{new_pts}{avg_hint} {ratio_hint}",
                            "sort_key": (0, 0),
                        })

            # 🟠 积分降价 ≥ points_drop_pct
            elif ctype == "积分降":
                pct = abs(c.get("pct", 0))
                if pct >= rules.get("points_drop_pct", 40):
                    alerts.append({
                        "level": "🟠",
                        "rank": 3,
                        "type": "积分大幅降价",
                        "hotel": hotel_code,
                        "label": hotel_label,
                        "date": d,
                        "dim": "points",
                        "detail": f"{c['old_value']}→{c['new_value']} (-{pct:.0f}%)",
                        "sort_key": (3, -pct),
                    })

            # 🟡 现金降价 ≥ cash_drop_pct
            elif ctype == "现金降":
                pct = abs(c.get("pct", 0))
                if pct >= rules.get("cash_drop_pct", 40):
                    alerts.append({
                        "level": "🟡",
                        "rank": 4,
                        "type": "现金大幅降价",
                        "hotel": hotel_code,
                        "label": hotel_label,
                        "date": d,
                        "dim": "cash",
                        "detail": (f"{c['old_value']:.0f}→{c['new_value']:.0f} "
                                   f"(-{pct:.0f}%) {c.get('currency','')}"),
                        "sort_key": (4, -pct),
                    })

            # 🟢 新开放日期低价 (积分 OR 现金, 双独立判定 — 修复原 bug)
            elif ctype == "新开放":
                below_pct = rules.get("new_date_below_avg_pct", 30)
                threshold_ratio = 1 - below_pct / 100  # 0.7

                new_pts = c.get("points")
                if new_pts and avg_pts and new_pts <= avg_pts * threshold_ratio:
                    pct_below = (1 - new_pts / avg_pts) * 100
                    alerts.append({
                        "level": "🟢",
                        "rank": 5,
                        "type": "新日期积分低价",
                        "hotel": hotel_code,
                        "label": hotel_label,
                        "date": d,
                        "dim": "points",
                        "detail": (f"积分{new_pts} (低于均价{pct_below:.0f}%, "
                                   f"均价{avg_pts:.0f})"),
                        "sort_key": (5, -pct_below),
                    })

                new_cash = c.get("cash_price")
                if new_cash and avg_cash and new_cash <= avg_cash * threshold_ratio:
                    pct_below = (1 - new_cash / avg_cash) * 100
                    alerts.append({
                        "level": "🟢",
                        "rank": 5,
                        "type": "新日期现金低价",
                        "hotel": hotel_code,
                        "label": hotel_label,
                        "date": d,
                        "dim": "cash",
                        "detail": (f"含税{new_cash:.0f} (低于均价{pct_below:.0f}%, "
                                   f"均价{avg_cash:.0f})"),
                        "sort_key": (5, -pct_below),
                    })

        # === 2. 基于本次快照的高性价比扫描 (新增 💎 / 🟣) ===
        if current_prices and (avg_cash or avg_pts):
            value_dates = find_high_value_dates(
                current_prices, avg_cash, avg_pts, rules)
            top_n = rules.get("top_n_per_hotel", 5)

            for v in value_dates[:top_n]:
                if v["kind"] == "points":
                    cash_hint = (f", 现金{v['cash']:.0f}{v.get('currency','')}"
                                 if v.get("cash") else "")
                    alerts.append({
                        "level": "💎",
                        "rank": 1,
                        "type": "高 CPP 积分房",
                        "hotel": hotel_code,
                        "label": hotel_label,
                        "date": v["date"],
                        "dim": "points",
                        "detail": (f"积分{v['value']}{cash_hint}, "
                                   f"CPP={v['cpp']:.2f} (≥{v['min_cpp']:.2f})"),
                        "sort_key": (1, -v["score"]),
                    })
                else:  # kind == "cash"
                    pts_hint = (f", 积分{v['points']}, CPP={v['cpp']:.2f}"
                                if v.get("points") and v.get("cpp") else "")
                    alerts.append({
                        "level": "🟣",
                        "rank": 2,
                        "type": "现金深折扣",
                        "hotel": hotel_code,
                        "label": hotel_label,
                        "date": v["date"],
                        "dim": "cash",
                        "detail": (f"含税{v['value']:.0f}{v.get('currency','')} "
                                   f"(基准{v['baseline']:.0f}, "
                                   f"-{v['discount_pct']:.0f}%){pts_hint}"),
                        "sort_key": (2, -v["score"]),
                    })

    # === 3. 去重: 同一 (hotel, date, dim) 保留最高级别 ===
    dedup = {}
    for a in alerts:
        key = (a["hotel"], a["date"], a["dim"])
        if key not in dedup or a["rank"] < dedup[key]["rank"]:
            dedup[key] = a
    alerts = list(dedup.values())

    # === 4. 排序: 先按级别(rank), 再按级别内 sort_key (score 高的优先) ===
    alerts.sort(key=lambda a: a["sort_key"])

    return alerts


# ============ 消息格式化 ============

# 级别展示顺序与标题
LEVEL_ORDER = ["🔴", "💎", "🟣", "🟠", "🟡", "🟢"]
LEVEL_TITLES = {
    "🔴": "积分房重新开放(节假日)",
    "💎": "高 CPP 积分房",
    "🟣": "现金深折扣",
    "🟠": "积分大幅降价",
    "🟡": "现金大幅降价",
    "🟢": "新日期低价",
}


def format_message(alerts):
    """格式化告警消息为 Markdown"""
    if not alerts:
        return None, None

    title = f"IHG 价格告警 ({len(alerts)}条)"
    today = date.today().isoformat()

    lines = [f"## IHG 价格告警", f"**{today}** · 共 {len(alerts)} 条\n"]

    # 按级别分组
    by_level = {}
    for a in alerts:
        by_level.setdefault(a["level"], []).append(a)

    for level in LEVEL_ORDER:
        items = by_level.get(level, [])
        if not items:
            continue

        title_str = LEVEL_TITLES.get(level, "")
        # 高性价比类标注"性价比从高到低"
        suffix = " · 性价比从高到低" if level in ("💎", "🟣") else ""
        lines.append(f"\n### {level} {title_str} ({len(items)}条){suffix}\n")

        for a in items:
            holiday_tag = f" 🎉{a['holiday']}" if a.get("holiday") else ""
            url = build_booking_url(a["hotel"], a["date"])
            lines.append(f"- **[{a['label']}]({url})** `{a['date']}`{holiday_tag}")
            lines.append(f"  {a['detail']}")
            lines.append(f"  [👉 立即预订]({url})")

    lines.append(f"\n---\n*监控时间: {today}*")
    return title, "\n".join(lines)


# ============ 发送 ============

def send_server_chan(title, body, send_key):
    """通过 Server酱 发送微信通知"""
    if not send_key or send_key.startswith("你的"):
        print("[通知] Server酱 SendKey 未配置, 跳过")
        return False
    if requests is None:
        print("[通知] 缺少 requests 库, 请运行: pip install requests")
        return False

    url = f"https://sctapi.ftqq.com/{send_key}.send"
    try:
        resp = requests.post(url, data={"title": title, "desp": body}, timeout=10)
        result = resp.json()
        if result.get("code") == 0:
            print(f"[通知] ✓ Server酱推送成功: {title}")
            return True
        print(f"[通知] ✗ Server酱推送失败: {result.get('message', '未知错误')}")
        return False
    except Exception as e:
        print(f"[通知] ✗ Server酱请求异常: {e}")
        return False


# ============ 主入口 (被 batch_monitor 调用) ============

def notify_changes(results, db):
    """从 batch_monitor 的 results 中筛选告警并发送通知

    Args:
        results: batch_monitor 返回的结果列表, 每项含 hotel_code/success/changes/prices
        db: IHGDatabase 实例
    """
    if not results:
        return

    config = load_config()
    alerts = filter_alerts(results, db, config)

    if not alerts:
        print(f"[通知] 无需通知的告警")
        return

    title, body = format_message(alerts)
    if not title:
        return

    # 控制台预览
    print(f"\n{'='*60}")
    print(f"  📢 {title}")
    print(f"{'='*60}")
    for a in alerts:
        print(f"  {a['level']} [{a['label']}] {a['date']} {a['detail']}")
    print(f"{'='*60}")

    # 发送 Server酱
    send_server_chan(title, body, config.get("server_chan_key", ""))


# ============ 命令行工具 ============

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="IHG 通知模块")
    parser.add_argument("--init", action="store_true", help="生成默认配置文件")
    parser.add_argument("--test", action="store_true", help="发送测试通知")
    parser.add_argument("--holidays", action="store_true", help="显示节假日列表")
    parser.add_argument("--url", type=str, default=None,
                        help="生成 IHG 预订链接, 格式: HKGKL:2026-10-01[:2]")
    args = parser.parse_args()

    if args.init:
        save_default_config()

    elif args.test:
        config = load_config()
        send_key = config.get("server_chan_key", "")
        if not send_key or send_key.startswith("你的"):
            print("请先配置 Server酱 SendKey:")
            print(f"  1. 编辑 {CONFIG_PATH}")
            print(f"  2. 填入 server_chan_key")
            print(f"  如配置文件不存在, 运行: python ihg_notify.py --init")
        else:
            sample_url = build_booking_url("HKGKL", "2026-10-01")
            title = "IHG 通知测试"
            body = (
                "## 测试成功\n\n"
                "如果你看到这条消息, 说明 Server酱 配置正确。\n\n"
                "### 6 类告警\n\n"
                "🔴 积分房重新开放(节假日)\n"
                "💎 高 CPP 积分房\n"
                "🟣 现金深折扣\n"
                "🟠 积分大幅降价\n"
                "🟡 现金大幅降价\n"
                "🟢 新日期低价\n\n"
                "---\n"
                "**预订链接示例**:\n\n"
                f"- [HKGKL 2026-10-01]({sample_url})\n"
            )
            send_server_chan(title, body, send_key)

    elif args.holidays:
        print("中国节假日 (含前后2天缓冲):")
        by_month = {}
        for d_str in sorted(HOLIDAY_DATES):
            month = d_str[:7]
            by_month.setdefault(month, []).append(
                f"{d_str} {HOLIDAY_NAMES.get(d_str, '·')}")
        for month, dates in sorted(by_month.items()):
            print(f"\n  {month}:")
            for d in dates:
                print(f"    {d}")

    elif args.url:
        parts = args.url.split(":")
        if len(parts) < 2:
            print("[!] 格式错误, 应为: HKGKL:2026-10-01 或 HKGKL:2026-10-01:2")
            sys.exit(1)
        code = parts[0].strip().upper()
        check_in = parts[1].strip()
        nights = int(parts[2]) if len(parts) >= 3 else None
        print(build_booking_url(code, check_in, nights=nights))

    else:
        parser.print_help()
