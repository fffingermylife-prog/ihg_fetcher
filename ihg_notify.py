"""
IHG 价格告警通知模块

功能:
    - 从 batch_monitor 的对比结果中筛选值得通知的变动
    - 支持 Server酱 微信推送
    - 支持邮件推送 (可选)
    - 中国节假日识别 (含前后各2天缓冲)

触发规则:
    🔴 积分房重新开放: 节假日日期 + 积分价 ≤ 平日历史均价×1.2
    🟠 积分降价: 降幅 ≥ 40%
    🟡 现金降价: 降幅 ≥ 40%
    🟢 新日期高性价比: 现金或积分 ≤ 历史均价×0.6

用法:
    # 作为模块被 ihg_batch_monitor.py 调用
    from ihg_notify import notify_changes

    # 独立测试
    python ihg_notify.py --test
"""

import json
import sys
from datetime import date, timedelta
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None


# ============ 配置 ============

# 配置文件路径 (和脚本同目录)
CONFIG_PATH = Path(__file__).parent / "notify_config.json"

# 默认规则
DEFAULT_RULES = {
    "points_drop_pct": 40,         # 积分降幅 ≥ 40% 通知
    "cash_drop_pct": 40,           # 现金降幅 ≥ 40% 通知
    "new_date_below_avg_pct": 30,  # 新日期 ≤ 历史均价×0.7 (即低于30%)
    "reopen_max_ratio": 0.9,       # 节假日重新开放积分 ≤ 平日均价×0.9 (即至少低 10%)
    # 旧字段兼容: 若未提供 reopen_max_ratio, 用 reopen_max_premium_pct (正数=允许溢价百分比)
    "reopen_max_premium_pct": -10,
}


# ============ 中国节假日 2026~2027 ============

def get_chinese_holidays():
    """获取 2026~2027 中国主要节假日日期 (含前后各2天缓冲)"""
    # 核心节假日日期
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

    # 扩展: 每个核心日期前后各加2天
    all_holiday_dates = set()
    for d_str in holidays_core:
        d = date.fromisoformat(d_str)
        for offset in range(-2, 3):  # -2, -1, 0, 1, 2
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
    """判断是否是工作日 (周一到周四)"""
    d = date.fromisoformat(date_str)
    return d.weekday() < 4  # 0=周一, 4=周五


# ============ 配置加载 ============

def load_config():
    """加载通知配置"""
    config = {
        "server_chan_key": "",
        "email": None,
        "rules": DEFAULT_RULES.copy(),
        "hotels": {},
    }

    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user_config = json.load(f)
            config.update(user_config)
            # 合并规则 (用户配置覆盖默认)
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
            "HKGKL": {"note": "香港假日酒店"},
            "DADHA": {"note": "岘港洲际"},
            "PQCCP": {"note": "富国岛皇冠假日"},
        }
    }
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(template, f, indent=2, ensure_ascii=False)
    print(f"[通知] 已生成配置模板: {CONFIG_PATH}")
    print(f"  请编辑填入你的 Server酱 SendKey")


# ============ 告警筛选逻辑 ============

def get_hotel_avg_points(db, hotel_code):
    """获取酒店平日积分均价 (排除节假日和周五六日)"""
    prices, _ = db.load_latest_prices(hotel_code)
    if not prices:
        return None

    weekday_points = []
    for p in prices:
        pts = p.get("points")
        if pts and is_weekday(p["date"]) and not is_holiday(p["date"]):
            weekday_points.append(pts)

    return sum(weekday_points) / len(weekday_points) if weekday_points else None


def get_hotel_avg_cash(db, hotel_code):
    """获取酒店平日现金均价 (排除节假日和周五六日)"""
    prices, _ = db.load_latest_prices(hotel_code)
    if not prices:
        return None

    weekday_cash = []
    for p in prices:
        cash = p.get("cash_price_after_tax") or p.get("cash_price")
        if cash and is_weekday(p["date"]) and not is_holiday(p["date"]):
            weekday_cash.append(cash)

    return sum(weekday_cash) / len(weekday_cash) if weekday_cash else None


def get_hotel_display_name(hotel_code, db, config):
    """获取酒店显示名称: 优先从数据库读取, 其次用配置备注, 最后用代码"""
    # 1. 从数据库读取 (最准确)
    hotel_info = db.get_hotel(hotel_code)
    if hotel_info and hotel_info.get("name"):
        name = hotel_info["name"]
        country = hotel_info.get("country", "")
        return f"{name} ({country})" if country else name

    # 2. 从配置文件读备注
    note = config.get("hotels", {}).get(hotel_code, {}).get("note", "")
    if note:
        return note

    # 3. 只返回代码
    return hotel_code


def filter_alerts(changes_by_hotel, db, config):
    """从所有变动中筛选出需要通知的告警"""
    rules = config.get("rules", DEFAULT_RULES)
    alerts = []

    for hotel_code, changes in changes_by_hotel.items():
        hotel_note = get_hotel_display_name(hotel_code, db, config)
        avg_points = get_hotel_avg_points(db, hotel_code)
        avg_cash = get_hotel_avg_cash(db, hotel_code)

        for c in changes:
            alert = None

            # 🔴 积分房重新开放 (节假日 + 价格合理)
            if c["type"] == "积分房重新开放":
                new_pts = c.get("new_value")
                if new_pts and is_holiday(c["date"]):
                    # 优先用 reopen_max_ratio (更直观), 否则换算 reopen_max_premium_pct
                    if "reopen_max_ratio" in rules:
                        max_ratio = rules["reopen_max_ratio"]
                    else:
                        max_premium = rules.get("reopen_max_premium_pct", -10)
                        max_ratio = 1 + max_premium / 100
                    if avg_points is None or new_pts <= avg_points * max_ratio:
                        ratio_hint = (
                            f"≤均价×{max_ratio:.2f}={avg_points*max_ratio:.0f}"
                            if avg_points else "无均价基准"
                        )
                        alert = {
                            "level": "🔴",
                            "type": "积分房重新开放(节假日)",
                            "hotel": hotel_code,
                            "note": hotel_note,
                            "date": c["date"],
                            "holiday": get_holiday_name(c["date"]),
                            "detail": f"积分{new_pts}" + (f" (平日均价{avg_points:.0f}, {ratio_hint})" if avg_points else ""),
                        }

            # 🟠 积分降价 ≥ 40%
            elif c["type"] == "积分降":
                pct = abs(c.get("pct", 0))
                if pct >= rules.get("points_drop_pct", 40):
                    alert = {
                        "level": "🟠",
                        "type": "积分大幅降价",
                        "hotel": hotel_code,
                        "note": hotel_note,
                        "date": c["date"],
                        "detail": f"{c['old_value']}→{c['new_value']} (-{pct:.0f}%)",
                    }

            # 🟡 现金降价 ≥ 40%
            elif c["type"] == "现金降":
                pct = abs(c.get("pct", 0))
                if pct >= rules.get("cash_drop_pct", 40):
                    alert = {
                        "level": "🟡",
                        "type": "现金大幅降价",
                        "hotel": hotel_code,
                        "note": hotel_note,
                        "date": c["date"],
                        "detail": f"{c['old_value']:.0f}→{c['new_value']:.0f} (-{pct:.0f}%) {c.get('currency','')}",
                    }

            # 🟢 新日期高性价比
            elif c["type"] == "新开放":
                below_pct = rules.get("new_date_below_avg_pct", 40)
                threshold = 1 - below_pct / 100  # 0.6

                # 检查积分
                new_pts = c.get("points")
                if new_pts and avg_points and new_pts <= avg_points * threshold:
                    pct_below = (1 - new_pts / avg_points) * 100
                    alert = {
                        "level": "🟢",
                        "type": "新日期积分低价",
                        "hotel": hotel_code,
                        "note": hotel_note,
                        "date": c["date"],
                        "detail": f"积分{new_pts} (低于均价{pct_below:.0f}%, 均价{avg_points:.0f})",
                    }

                # 检查现金
                new_cash = c.get("cash_price")
                if not alert and new_cash and avg_cash and new_cash <= avg_cash * threshold:
                    pct_below = (1 - new_cash / avg_cash) * 100
                    alert = {
                        "level": "🟢",
                        "type": "新日期现金低价",
                        "hotel": hotel_code,
                        "note": hotel_note,
                        "date": c["date"],
                        "detail": f"含税{new_cash:.0f} (低于均价{pct_below:.0f}%, 均价{avg_cash:.0f})",
                    }

            if alert:
                alerts.append(alert)

    # 按优先级排序
    priority_order = {"🔴": 0, "🟠": 1, "🟡": 2, "🟢": 3}
    alerts.sort(key=lambda a: (priority_order.get(a["level"], 9), a["date"]))

    return alerts


# ============ 消息格式化 ============

def format_message(alerts):
    """格式化告警消息"""
    if not alerts:
        return None, None

    title = f"IHG 价格告警 ({len(alerts)}条)"

    lines = [f"## IHG 价格告警\n", f"**{date.today().isoformat()}** 共 {len(alerts)} 条\n"]

    # 按级别分组
    by_level = {}
    for a in alerts:
        level = a["level"]
        if level not in by_level:
            by_level[level] = []
        by_level[level].append(a)

    for level in ["🔴", "🟠", "🟡", "🟢"]:
        items = by_level.get(level, [])
        if not items:
            continue

        level_name = items[0]["type"].split("(")[0]  # 取第一个的类型作为标题
        lines.append(f"\n### {level} {level_name} ({len(items)}条)\n")

        for a in items:
            holiday_tag = f" 🎉{a['holiday']}" if a.get("holiday") else ""
            lines.append(f"- **{a['note']}** `{a['date']}`{holiday_tag}")
            lines.append(f"  {a['detail']}")

    lines.append(f"\n---\n*监控时间: {date.today().isoformat()}*")

    body = "\n".join(lines)
    return title, body


# ============ 发送通知 ============

def send_server_chan(title, body, send_key):
    """通过 Server酱 发送微信通知"""
    if not send_key or send_key.startswith("你的"):
        print("[通知] Server酱 SendKey 未配置, 跳过")
        return False

    if requests is None:
        print("[通知] 缺少 requests 库, 请运行: pip install requests")
        return False

    url = f"https://sctapi.ftqq.com/{send_key}.send"
    data = {
        "title": title,
        "desp": body,
    }

    try:
        resp = requests.post(url, data=data, timeout=10)
        result = resp.json()
        if result.get("code") == 0:
            print(f"[通知] ✓ Server酱推送成功: {title}")
            return True
        else:
            print(f"[通知] ✗ Server酱推送失败: {result.get('message', '未知错误')}")
            return False
    except Exception as e:
        print(f"[通知] ✗ Server酱请求异常: {e}")
        return False


# ============ 主入口 (被 batch_monitor 调用) ============

def notify_changes(results, db):
    """
    从 batch_monitor 的结果中筛选告警并发送通知
    
    Args:
        results: batch_monitor 返回的结果列表
        db: IHGDatabase 实例
    """
    # 收集所有变动
    changes_by_hotel = {}
    for r in results:
        if r.get("success") and r.get("changes"):
            changes_by_hotel[r["hotel_code"]] = r["changes"]

    if not changes_by_hotel:
        return

    # 加载配置
    config = load_config()

    # 筛选告警
    alerts = filter_alerts(changes_by_hotel, db, config)

    if not alerts:
        print(f"[通知] 无需通知的告警")
        return

    # 格式化
    title, body = format_message(alerts)
    if not title:
        return

    # 打印到控制台
    print(f"\n{'='*60}")
    print(f"  📢 {title}")
    print(f"{'='*60}")
    for a in alerts:
        print(f"  {a['level']} [{a['note']}] {a['date']} {a['detail']}")
    print(f"{'='*60}")

    # 发送 Server酱
    send_key = config.get("server_chan_key", "")
    send_server_chan(title, body, send_key)


# ============ 命令行工具 ============

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="IHG 通知模块")
    parser.add_argument("--init", action="store_true", help="生成默认配置文件")
    parser.add_argument("--test", action="store_true", help="发送测试通知")
    parser.add_argument("--holidays", action="store_true", help="显示节假日列表")
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
            print(f"  如果配置文件不存在, 运行: python ihg_notify.py --init")
        else:
            title = "IHG 通知测试"
            body = "## 测试成功\n\n如果你看到这条消息, 说明 Server酱 配置正确。\n\n🔴 积分房重新开放\n🟠 积分大幅降价\n🟡 现金大幅降价\n🟢 新日期高性价比"
            send_server_chan(title, body, send_key)

    elif args.holidays:
        print("中国节假日 (含前后2天缓冲):")
        # 按月分组显示
        by_month = {}
        for d_str in sorted(HOLIDAY_DATES):
            month = d_str[:7]
            if month not in by_month:
                by_month[month] = []
            name = HOLIDAY_NAMES.get(d_str, "·")
            by_month[month].append(f"{d_str} {name}")

        for month, dates in sorted(by_month.items()):
            print(f"\n  {month}:")
            for d in dates:
                print(f"    {d}")

    else:
        parser.print_help()
