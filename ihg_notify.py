"""
IHG 价格告警通知模块

唯一目的: 找到极具性价比的积分房和现金房, 推送微信通知

5条告警规则 (按优先级从高到低):
    💎 高 CPP 积分房    — CPP ≥ 阈值 (无需历史, 首次采集就能触发)
    🔴 积分同日暴降    — 同日积分降幅 ≥ 阈值 (需要历史基准)
    🟠 节假日积分低价  — 节假日 + 积分 ≤ 本次平日均价×ratio (无需历史)
    🟣 现金深折扣      — 现金 ≤ 本次平日均价×ratio (无需历史)
    🟡 现金同日暴降    — 同日现金降幅 ≥ 阈值 (需要历史基准)

权重评分 & 全局 Top N:
    每条告警根据规则类型 + 偏离程度计算统一权重 (0~100),
    所有酒店的告警混在一起按权重降序排, 取 top_n_global(默认10) 推送。
    推送格式按酒店分组, 酒店名只出现一次 (无代码), 每条极简显示。

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
    "min_cpp_threshold": 0.8,        # 💎 CPP ≥ 0.8 视为高性价比积分房 (USD 美分/积分)
    "points_drop_pct": 40,           # 🔴 同日积分降幅 ≥ 40% 推送
    "holiday_points_ratio": 0.9,     # 🟠 节假日积分 ≤ 平日均价×0.9
    "cash_deal_ratio": 0.5,          # 🟣 现金 ≤ 平日均价×50%
    "cash_drop_pct": 50,             # 🟡 同日现金降幅 ≥ 50% 推送
    "top_n_per_hotel": 5,            # 每酒店最多入围 N 条 (预筛)
    "top_n_global": 30,              # 全局最终推送条数
}


# ============ 中国节假日 2026~2027 ============

def _build_holidays():
    """构建 2026~2027 中国节假日集合
    规则:
      - 连续假期 ≥ 3 天 (春节/五一/国庆): 核心日期 + 前后各2天缓冲
      - 单天假期 (元旦/清明/端午/中秋): 仅当天, 不加缓冲
    """
    # 长假 (≥3天): 加前后2天缓冲
    long_holidays = {
        # 2026 春节 (7天)
        "2026-02-17": "春节", "2026-02-18": "春节", "2026-02-19": "春节",
        "2026-02-20": "春节", "2026-02-21": "春节", "2026-02-22": "春节",
        "2026-02-23": "春节",
        # 2026 五一 (5天)
        "2026-05-01": "五一", "2026-05-02": "五一", "2026-05-03": "五一",
        "2026-05-04": "五一", "2026-05-05": "五一",
        # 2026 国庆 (7天)
        "2026-10-01": "国庆", "2026-10-02": "国庆", "2026-10-03": "国庆",
        "2026-10-04": "国庆", "2026-10-05": "国庆", "2026-10-06": "国庆",
        "2026-10-07": "国庆",
        # 2027 春节 (7天)
        "2027-02-06": "春节", "2027-02-07": "春节", "2027-02-08": "春节",
        "2027-02-09": "春节", "2027-02-10": "春节", "2027-02-11": "春节",
        "2027-02-12": "春节",
        # 2027 五一 (5天)
        "2027-05-01": "五一", "2027-05-02": "五一", "2027-05-03": "五一",
        "2027-05-04": "五一", "2027-05-05": "五一",
        # 2027 国庆 (7天)
        "2027-10-01": "国庆", "2027-10-02": "国庆", "2027-10-03": "国庆",
        "2027-10-04": "国庆", "2027-10-05": "国庆", "2027-10-06": "国庆",
        "2027-10-07": "国庆",
    }

    # 单天假期: 不加缓冲
    short_holidays = {
        "2026-01-01": "元旦",
        "2026-04-05": "清明",
        "2026-05-31": "端午",
        "2026-09-25": "中秋",
        "2027-01-01": "元旦",
        "2027-04-05": "清明",
        "2027-06-19": "端午",
        "2027-09-15": "中秋",
    }

    # 构建最终集合
    expanded = set()
    names = {}  # date_str -> holiday_name

    # 长假: 核心日期 + 前后2天缓冲
    for d_str, name in long_holidays.items():
        d = date.fromisoformat(d_str)
        for offset in range(-2, 3):
            dd = (d + timedelta(days=offset)).isoformat()
            expanded.add(dd)
            if dd not in names:
                names[dd] = name

    # 单天假期: 仅当天
    for d_str, name in short_holidays.items():
        expanded.add(d_str)
        names[d_str] = name

    return expanded, names


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
        kind: "cash_usd" (现金 USD 均价) 或 "points"
    Returns:
        均价 float 或 None (样本不足时)
    """
    values = []
    for p in prices:
        if not is_weekday_non_holiday(p.get("date", "")):
            continue
        if kind == "cash_usd":
            v = p.get("cash_price_usd")
        else:
            v = p.get("points")
        if v:
            values.append(v)
    # 至少 10 个平日样本才计算
    return sum(values) / len(values) if len(values) >= 10 else None


# ============ 权重评分系统 ============

def compute_weight(alert):
    """统一权重评分 (0~100), 值越大性价比越高 / 越值得关注

    公式设计:
      💎 高CPP: weight = min(100, (cpp / threshold - 1) * 50 + 60)
         → CPP=阈值时 60 分, CPP=2×阈值时 100 分
      🔴 积分暴降: weight = min(100, pct * 0.8 + 20)
         → 40%降幅=52分, 80%降幅=84分
      🟠 节假日积分低价: weight = min(100, below_avg_pct * 1.5 + 30)
         → 低于均价10%=45分, 30%=75分
      🟣 现金深折扣: weight = min(100, below_avg_pct * 1.2 + 25)
         → 50%折扣=85分, 40%折扣=73分
      🟡 现金暴降: weight = min(100, pct * 0.6 + 15)
         → 50%降幅=45分, 80%降幅=63分

    核心思路: 💎高CPP 和 🟣现金深折扣 基础分高 (真金白银的性价比),
              🟠节假日 有旅行场景加成, 🔴🟡暴降类属于时效性机会分稍低。
    """
    level = alert["level"]
    score = alert.get("score", 0)

    if level == "💎":
        # score = cpp 值 (如 0.95)
        threshold = alert.get("threshold", 0.8)
        if threshold > 0:
            weight = min(100, (score / threshold - 1) * 50 + 60)
        else:
            weight = 60
    elif level == "🔴":
        # score = 降幅百分比 (如 45)
        weight = min(100, score * 0.8 + 20)
    elif level == "🟠":
        # score = 低于均价百分比 (如 15)
        weight = min(100, score * 1.5 + 30)
    elif level == "🟣":
        # score = 低于均价百分比 (如 55)
        weight = min(100, score * 1.2 + 25)
    elif level == "🟡":
        # score = 降幅百分比 (如 55)
        weight = min(100, score * 0.6 + 15)
    else:
        weight = 0

    return round(weight, 1)


# ============ 告警筛选 ============

def filter_alerts(results, db, config):
    """从 batch_monitor 结果中筛选 5 类告警, 计算权重, 全局 top N"""
    rules = config.get("rules", DEFAULT_RULES)
    today_str = date.today().isoformat()
    all_alerts = []

    for r in results:
        if not r.get("success"):
            continue

        hotel_code = r["hotel_code"]
        changes = r.get("changes", [])
        prices = r.get("prices", [])

        # 获取酒店显示名 (纯名字, 无代码)
        label = _get_hotel_label(hotel_code, db, config)

        # 计算本次快照平日均价
        avg_cash_usd = compute_weekday_avg(prices, "cash_usd")
        avg_points = compute_weekday_avg(prices, "points")

        # === 基于本次快照的扫描 ===
        hotel_snapshot_alerts = []
        min_cpp = rules.get("min_cpp_threshold", 0.8)

        for p in prices:
            d = p.get("date", "")
            if not d or d <= today_str:
                continue

            cash_usd = p.get("cash_price_usd")
            points = p.get("points")
            cpp = p.get("cpp")  # USD 美分/积分

            # 💎 高 CPP 积分房
            # 额外条件: 积分不能高于平日积分均价 (排除现金 bug 价导致 CPP 虚高)
            if cpp and cpp >= min_cpp and points:
                if avg_points and points > avg_points:
                    pass  # 积分高于均价 → 大概率是现金 bug 价, 跳过
                else:
                    hotel_snapshot_alerts.append({
                        "level": "💎", "rank": 0,
                        "type": "高CPP积分房",
                        "hotel": hotel_code, "label": label, "date": d,
                        "detail": f"{points}分 ≈${cash_usd:.0f} CPP={cpp:.2f}¢",
                        "score": cpp,
                        "threshold": min_cpp,
                    })

            # 🟠 节假日积分低价
            ratio = rules.get("holiday_points_ratio", 0.9)
            if points and avg_points and is_holiday(d) and points <= avg_points * ratio:
                pct = (1 - points / avg_points) * 100
                hotel_snapshot_alerts.append({
                    "level": "🟠", "rank": 2,
                    "type": "节假日积分低价",
                    "hotel": hotel_code, "label": label, "date": d,
                    "detail": f"{points}分 (-{pct:.0f}%均价) {get_holiday_name(d)}",
                    "score": pct,
                })

            # 🟣 现金深折扣
            deal_ratio = rules.get("cash_deal_ratio", 0.5)
            if cash_usd and avg_cash_usd and cash_usd <= avg_cash_usd * deal_ratio:
                pct = (1 - cash_usd / avg_cash_usd) * 100
                hotel_snapshot_alerts.append({
                    "level": "🟣", "rank": 3,
                    "type": "现金深折扣",
                    "hotel": hotel_code, "label": label, "date": d,
                    "detail": f"${cash_usd:.0f} (-{pct:.0f}%均价${avg_cash_usd:.0f})",
                    "score": pct,
                })

        # 每酒店按 score 排序后全部加入 (全量文件需要所有, 推送最后只取 top_n_global)
        hotel_snapshot_alerts.sort(key=lambda x: -x["score"])
        all_alerts.extend(hotel_snapshot_alerts)

        # === 基于 changes 的历史对比 ===
        for c in changes:
            d = c.get("date", "")
            if not d or d <= today_str:
                continue

            # 🔴 积分同日暴降
            if c["type"] == "积分降":
                pct = abs(c.get("pct", 0))
                threshold = rules.get("points_drop_pct", 40)
                if pct >= threshold:
                    all_alerts.append({
                        "level": "🔴", "rank": 1,
                        "type": "积分同日暴降",
                        "hotel": hotel_code, "label": label, "date": d,
                        "detail": f"{c['old_value']}→{c['new_value']}分 (-{pct:.0f}%)",
                        "score": pct,
                    })

            # 🟡 现金同日暴降
            elif c["type"] == "现金降":
                pct = abs(c.get("pct", 0))
                threshold = rules.get("cash_drop_pct", 50)
                if pct >= threshold:
                    all_alerts.append({
                        "level": "🟡", "rank": 4,
                        "type": "现金同日暴降",
                        "hotel": hotel_code, "label": label, "date": d,
                        "detail": f"${c.get('new_value',0):.0f} (原${c.get('old_value',0):.0f}, -{pct:.0f}%)",
                        "score": pct,
                    })

    # 去重: (hotel, date) 保留最高优先级 (rank 最小)
    dedup = {}
    for a in all_alerts:
        key = (a["hotel"], a["date"])
        if key not in dedup or a["rank"] < dedup[key]["rank"]:
            dedup[key] = a
    all_alerts = list(dedup.values())

    # 计算统一权重
    for a in all_alerts:
        a["weight"] = compute_weight(a)

    # 全局按权重降序排
    all_alerts.sort(key=lambda a: -a["weight"])

    # 写入全量告警文件 (所有触发条件的, 不限条数, 供回查)
    _write_all_alerts_file(all_alerts)

    # 推送只取 top_n_global
    top_n_global = rules.get("top_n_global", 10)
    return all_alerts[:top_n_global]


def _write_all_alerts_file(all_alerts):
    """将全量告警写入文件 (按权重排序, 不限条数)
    文件路径: ./ihg_logs/alerts_YYYYMMDD_HHMMSS.md
    """
    if not all_alerts:
        return None
    try:
        from datetime import datetime
        log_dir = Path(__file__).parent / "ihg_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        file_path = log_dir / f"alerts_{ts}.md"

        lines = []
        lines.append(f"# IHG 全量告警 ({len(all_alerts)}条)")
        lines.append(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"按权重降序排列\n")

        # 按酒店分组
        from collections import OrderedDict
        hotel_groups = OrderedDict()
        for a in all_alerts:
            code = a["hotel"]
            if code not in hotel_groups:
                hotel_groups[code] = {"label": a["label"], "items": []}
            hotel_groups[code]["items"].append(a)

        for code, group in hotel_groups.items():
            lines.append(f"## {group['label']} ({len(group['items'])}条)\n")
            for a in group["items"]:
                url = build_booking_url(code, a["date"])
                lines.append(f"- {a['level']} `{a['date']}` W={a['weight']:.0f} | {a['detail']} [预订]({url})")
            lines.append("")

        lines.append(f"\n---\n共 {len(all_alerts)} 条告警, {len(hotel_groups)} 家酒店")

        with open(file_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(f"[通知] 全量告警已写入: {file_path} ({len(all_alerts)}条)")
        return file_path
    except Exception as e:
        print(f"[通知] 写全量告警文件失败: {e}")
        return None


def _get_hotel_label(hotel_code, db, config):
    """获取酒店显示名 (纯名字, 无代码)"""
    # 优先 notify_config.json 中的 note
    note = config.get("hotels", {}).get(hotel_code, {}).get("note", "")
    if note:
        return note
    # 其次 db 的 name
    if db:
        info = db.get_hotel(hotel_code)
        if info and info.get("name"):
            return info["name"]
    return hotel_code


# ============ 消息格式化 (精简版) ============

def format_message(alerts):
    """格式化精简 Markdown 消息
    结构: 按酒店分组, 酒店名只出现一次, 每条一行 (日期 + 简要信息 + 预订链接)
    """
    if not alerts:
        return None, None

    title = f"IHG 高性价比 ({len(alerts)}条)"
    lines = [f"## {title}\n"]

    # 按酒店分组 (保持权重顺序, 但同酒店合并)
    from collections import OrderedDict
    hotel_groups = OrderedDict()
    for a in alerts:
        code = a["hotel"]
        if code not in hotel_groups:
            hotel_groups[code] = {"label": a["label"], "items": []}
        hotel_groups[code]["items"].append(a)

    for code, group in hotel_groups.items():
        lines.append(f"### {group['label']}\n")
        for a in group["items"]:
            url = build_booking_url(code, a["date"])
            # 紧凑格式: emoji 日期 详情 [预订]
            lines.append(f"- {a['level']} `{a['date']}` {a['detail']} [预订]({url})")
        lines.append("")  # 空行分隔酒店

    lines.append(f"---\n*权重排序, 共{len(alerts)}条*")
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

    # Server酱 desp 限制约 32KB, 超长时截断
    MAX_DESP_LEN = 30000
    if len(body) > MAX_DESP_LEN:
        body = body[:MAX_DESP_LEN] + "\n\n...(已截断)"
        print(f"[通知] 消息体超长, 已截断至 {MAX_DESP_LEN} 字符")

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
        print(f"  {a['level']} [{a['label']}] {a['date']} {a['detail']} (W={a['weight']:.0f})")
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
                f"5条告警规则 + 权重评分:\n"
                f"💎 高CPP积分房 | 🔴 积分暴降 | 🟠 节假日积分低价\n"
                f"🟣 现金深折扣 | 🟡 现金暴降\n\n"
                f"全局 top 10 按权重推送\n\n"
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
