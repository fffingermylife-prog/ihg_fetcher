"""
IHG 价格告警通知模块

唯一目的: 找到极具性价比的积分房和现金房, 推送微信通知

5条告警规则 (按优先级从高到低):
    💎 高 CPP 积分房    — CPP ≥ 阈值 (无需历史, 首次采集就能触发)
    🔴 积分同日暴降    — 同日积分降幅 ≥ 阈值 (需要历史基准)
    🟠 节假日积分低价  — 节假日 + 积分 ≤ 本次平日均价×ratio (无需历史)
    🟣 现金深折扣      — 现金 ≤ 本次平日均价×ratio (无需历史)
    🟡 现金同日暴降    — 同日现金降幅 ≥ 阈值 (需要历史基准)

基准均价策略:
    统一使用"本次快照平日均价" (周一~周四 + 非节假日)
    理由: 一次全量采集 365 天有 ~150 个平日样本, 足够稳健, 不依赖历史

用法:
    from ihg_notify import notify_changes
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
    "min_cpp_threshold": 0.7,        # 💎 CPP ≥ 0.7 视为高性价比积分房
    "points_drop_pct": 40,           # 🔴 同日积分降幅 ≥ 40% 推送
    "holiday_points_ratio": 0.9,     # 🟠 节假日积分 ≤ 平日均价×0.9
    "cash_deal_ratio": 0.5,          # 🟣 现金 ≤ 平日均价×50%
    "cash_drop_pct": 50,             # 🟡 同日现金降幅 ≥ 50% 推送
    "top_n_per_hotel": 5,            # 每酒店最多推送 N 条
}


# ============ 中国节假日 2026~2027 ============

def _build_holidays():
    """构建 2026~2027 中国节假日集合 (核心日期 + 前后各2天缓冲)"""
    core = {
        "2026-01-01": "元旦",
        "2026-02-17": "春节", "2026-02-18": "春节", "2026-02-19": "春节",
        "2026-02-20": "春节", "2026-02-21": "春节", "2026-02-22": "春节",
        "2026-04-05": "清明",
        "2026-05-01": "五一", "2026-05-02": "五一", "2026-05-03": "五一",
        "2026-05-04": "五一", "2026-05-05": "五一",
        "2026-05-31": "端午", "2026-09-25": "中秋",
        "2026-10-01": "国庆", "2026-10-02": "国庆", "2026-10-03": "国庆",
        "2026-10-04": "国庆", "2026-10-05": "国庆", "2026-10-06": "国庆",
        "2026-10-07": "国庆",
        "2027-01-01": "元旦",
        "2027-02-06": "春节", "2027-02-07": "春节", "2027-02-08": "春节",
        "2027-02-09": "春节", "2027-02-10": "春节", "2027-02-11": "春节",
        "2027-04-05": "清明",
        "2027-05-01": "五一", "2027-05-02": "五一", "2027-05-03": "五一",
        "2027-05-04": "五一", "2027-05-05": "五一",
        "2027-06-19": "端午", "2027-09-15": "中秋",
        "2027-10-01": "国庆", "2027-10-02": "国庆", "2027-10-03": "国庆",
        "2027-10-04": "国庆", "2027-10-05": "国庆", "2027-10-06": "国庆",
        "2027-10-07": "国庆",
    }
    expanded = set()
    for d_str in core:
        d = date.fromisoformat(d_str)
        for offset in range(-2, 3):
            expanded.add((d + timedelta(days=offset)).isoformat())
    return expanded, core


HOLIDAY_DATES, HOLIDAY_NAMES = _build_holidays()


def is_holiday(date_str):
    return date_str in HOLIDAY_DATES


def get_holiday_name(date_str):
    return HOLIDAY_NAMES.get(date_str, "假期")


def is_weekday_non_holiday(date_str):
    """平日 = 周一~周四 + 非节假日"""
    d = date.fromisoformat(date_str)
    return d.weekday() < 4 and date_str not in HOLIDAY_DATES


# ============ 预订链接 ============

def build_booking_url(hotel_code, check_in_date, nights=1):
    """生成 IHG 官网预订链接 (adjustMonth=true + monthIndex=01 防偏移)"""
    if isinstance(check_in_date, str):
        check_in = date.fromisoformat(check_in_date)
    else:
        check_in = check_in_date
    check_out = check_in + timedelta(days=nights)
    params = {
        "path": "rates", "hotelCode": hotel_code,
        "regionCode": "1", "localeCode": "en",
        "checkInDate": check_in.day,
        "checkInMonthYear": f"{check_in.month:02d}{check_in.year}",
        "checkOutDate": check_out.day,
        "checkOutMonthYear": f"{check_out.month:02d}{check_out.year}",
        "numberOfAdults": "1", "numberOfRooms": "1",
        "adjustMonth": "true", "monthIndex": "01",
    }
    return f"https://www.ihg.com/redirect?{urlencode(params)}"


# ============ 配置加载 ============

def load_config():
    """加载配置, 与默认值合并"""
    config = {"server_chan_key": "", "rules": DEFAULT_RULES.copy(), "hotels": {}}
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user = json.load(f)
            config.update(user)
            if "rules" in user:
                config["rules"] = {**DEFAULT_RULES, **user["rules"]}
        except Exception as e:
            print(f"[通知] 配置加载失败: {e}")
    return config


# ============ 本次快照平日均价 ============

def compute_weekday_avg(prices, kind):
    """从本次快照计算平日均价 (周一~周四, 非节假日)
    Args:
        prices: 本次快照列表
        kind: "cash" 或 "points"
    Returns:
        均价 float 或 None (样本不足时)
    """
    values = []
    for p in prices:
        if not is_weekday_non_holiday(p.get("date", "")):
            continue
        if kind == "cash":
            v = p.get("cash_price_after_tax") or p.get("cash_price")
        else:
            v = p.get("points")
        if v:
            values.append(v)
    # 至少 10 个平日样本才计算 (防止增量模式样本太少)
    return sum(values) / len(values) if len(values) >= 10 else None


# ============ 告警筛选 ============

def filter_alerts(results, db, config):
    """从 batch_monitor 结果中筛选 5 类告警"""
    rules = config.get("rules", DEFAULT_RULES)
    today_str = date.today().isoformat()
    alerts = []

    for r in results:
        if not r.get("success"):
            continue

        hotel_code = r["hotel_code"]
        changes = r.get("changes", [])
        prices = r.get("prices", [])

        # 获取酒店显示名
        label = _get_hotel_label(hotel_code, db, config)

        # 计算本次快照平日均价
        avg_cash = compute_weekday_avg(prices, "cash")
        avg_points = compute_weekday_avg(prices, "points")

        # === 基于本次快照的扫描 (无需历史, 首次采集就能触发) ===

        hotel_snapshot_alerts = []

        for p in prices:
            d = p.get("date", "")
            if not d or d <= today_str:
                continue

            cash = p.get("cash_price_after_tax") or p.get("cash_price")
            points = p.get("points")
            cpp = p.get("cpp")

            # 💎 高 CPP 积分房
            min_cpp = rules.get("min_cpp_threshold", 0.7)
            if cpp and cpp >= min_cpp and points:
                cash_hint = f", 现金{cash:.0f}{p.get('currency','')}" if cash else ""
                hotel_snapshot_alerts.append({
                    "level": "💎", "rank": 0,
                    "type": "高CPP积分房",
                    "hotel": hotel_code, "label": label, "date": d,
                    "detail": f"积分{points}{cash_hint}, CPP={cpp:.2f}",
                    "score": cpp,  # CPP 越高越好
                })

            # 🟠 节假日积分低价
            ratio = rules.get("holiday_points_ratio", 0.9)
            if points and avg_points and is_holiday(d) and points <= avg_points * ratio:
                pct = (1 - points / avg_points) * 100
                hotel_snapshot_alerts.append({
                    "level": "🟠", "rank": 2,
                    "type": "节假日积分低价",
                    "hotel": hotel_code, "label": label, "date": d,
                    "holiday": get_holiday_name(d),
                    "detail": f"积分{points} (均价{avg_points:.0f}, -{pct:.0f}%) 🎉{get_holiday_name(d)}",
                    "score": pct,  # 低于均价百分比越大越好
                })

            # 🟣 现金深折扣
            deal_ratio = rules.get("cash_deal_ratio", 0.5)
            if cash and avg_cash and cash <= avg_cash * deal_ratio:
                pct = (1 - cash / avg_cash) * 100
                hotel_snapshot_alerts.append({
                    "level": "🟣", "rank": 3,
                    "type": "现金深折扣",
                    "hotel": hotel_code, "label": label, "date": d,
                    "detail": f"含税{cash:.0f}{p.get('currency','')} (均价{avg_cash:.0f}, -{pct:.0f}%)",
                    "score": pct,
                })

        # 按 score 降序, 取 top_n
        top_n = rules.get("top_n_per_hotel", 5)
        hotel_snapshot_alerts.sort(key=lambda x: -x["score"])
        alerts.extend(hotel_snapshot_alerts[:top_n])

        # === 基于 changes 的历史对比 (需要有历史基准) ===

        for c in changes:
            d = c.get("date", "")
            if not d or d <= today_str:
                continue

            # 🔴 积分同日暴降
            if c["type"] == "积分降":
                pct = abs(c.get("pct", 0))
                threshold = rules.get("points_drop_pct", 40)
                if pct >= threshold:
                    alerts.append({
                        "level": "🔴", "rank": 1,
                        "type": "积分同日暴降",
                        "hotel": hotel_code, "label": label, "date": d,
                        "detail": f"{c['old_value']}→{c['new_value']} (-{pct:.0f}%)",
                        "score": pct,
                    })

            # 🟡 现金同日暴降
            elif c["type"] == "现金降":
                pct = abs(c.get("pct", 0))
                threshold = rules.get("cash_drop_pct", 50)
                if pct >= threshold:
                    alerts.append({
                        "level": "🟡", "rank": 4,
                        "type": "现金同日暴降",
                        "hotel": hotel_code, "label": label, "date": d,
                        "detail": f"{c['old_value']:.0f}→{c['new_value']:.0f} (-{pct:.0f}%) {c.get('currency','')}",
                        "score": pct,
                    })

    # 去重: (hotel, date) 保留最高优先级 (rank 最小)
    dedup = {}
    for a in alerts:
        key = (a["hotel"], a["date"])
        if key not in dedup or a["rank"] < dedup[key]["rank"]:
            dedup[key] = a
    alerts = list(dedup.values())

    # 排序: 先 rank (优先级), 再 score 降序 (性价比)
    alerts.sort(key=lambda a: (a["rank"], -a["score"]))
    return alerts


def _get_hotel_label(hotel_code, db, config):
    """获取酒店显示名"""
    if db:
        info = db.get_hotel(hotel_code)
        if info and info.get("name"):
            return f"{info['name']} [{hotel_code}]"
    note = config.get("hotels", {}).get(hotel_code, {}).get("note", "")
    return f"{note} [{hotel_code}]" if note else hotel_code


# ============ 消息格式化 ============

LEVEL_TITLES = {
    "💎": "高 CPP 积分房",
    "🔴": "积分同日暴降",
    "🟠": "节假日积分低价",
    "🟣": "现金深折扣",
    "🟡": "现金同日暴降",
}


def format_message(alerts):
    """格式化 Markdown 消息"""
    if not alerts:
        return None, None

    title = f"IHG 高性价比告警 ({len(alerts)}条)"
    lines = [f"## {title}", f"**{date.today().isoformat()}**\n"]

    # 按级别分组
    by_level = {}
    for a in alerts:
        by_level.setdefault(a["level"], []).append(a)

    for level in ["💎", "🔴", "🟠", "🟣", "🟡"]:
        items = by_level.get(level, [])
        if not items:
            continue
        lines.append(f"\n### {level} {LEVEL_TITLES[level]} ({len(items)}条)\n")
        for a in items:
            url = build_booking_url(a["hotel"], a["date"])
            lines.append(f"- **[{a['label']}]({url})** `{a['date']}`")
            lines.append(f"  {a['detail']}")
            lines.append(f"  [👉预订]({url})")

    lines.append(f"\n---\n*{date.today().isoformat()}*")
    return title, "\n".join(lines)


# ============ 发送 ============

def send_server_chan(title, body, send_key):
    """Server酱微信推送"""
    if not send_key or send_key.startswith("你的"):
        print("[通知] Server酱 SendKey 未配置, 跳过")
        return False
    if not requests:
        print("[通知] 缺少 requests 库")
        return False
    try:
        resp = requests.post(
            f"https://sctapi.ftqq.com/{send_key}.send",
            data={"title": title, "desp": body}, timeout=10)
        result = resp.json()
        if result.get("code") == 0:
            print(f"[通知] ✓ 推送成功: {title}")
            return True
        print(f"[通知] ✗ 推送失败: {result.get('message', '')}")
    except Exception as e:
        print(f"[通知] ✗ 异常: {e}")
    return False


# ============ 主入口 ============

def notify_changes(results, db):
    """从 batch_monitor 结果筛选告警并推送"""
    if not results:
        return

    config = load_config()
    alerts = filter_alerts(results, db, config)

    if not alerts:
        print("[通知] 无高性价比告警")
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

    send_server_chan(title, body, config.get("server_chan_key", ""))


# ============ CLI ============

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="IHG 通知模块")
    parser.add_argument("--test", action="store_true", help="发送测试通知")
    parser.add_argument("--url", type=str, help="生成预订链接 HKGKL:2026-10-01[:nights]")
    args = parser.parse_args()

    if args.test:
        config = load_config()
        key = config.get("server_chan_key", "")
        if not key or key.startswith("你的"):
            print(f"请先在 {CONFIG_PATH} 配置 server_chan_key")
        else:
            url = build_booking_url("HKGKL", "2026-10-01")
            send_server_chan("IHG 通知测试",
                f"## 测试成功\n\n"
                f"5条告警规则:\n"
                f"💎 高CPP积分房 | 🔴 积分暴降 | 🟠 节假日积分低价\n"
                f"🟣 现金深折扣 | 🟡 现金暴降\n\n"
                f"[预订链接示例]({url})", key)

    elif args.url:
        parts = args.url.split(":")
        if len(parts) < 2:
            print("格式: HKGKL:2026-10-01 或 HKGKL:2026-10-01:2")
            sys.exit(1)
        code = parts[0].upper()
        nights = int(parts[2]) if len(parts) >= 3 else 1
        print(build_booking_url(code, parts[1], nights))

    else:
        parser.print_help()
