"""
IHG 价格告警通知模块

唯一目的: 找到极具性价比的积分房和现金房, 推送微信通知

6条告警规则 (按优先级 rank 从高到低):
    💎 高 CPP 积分房     (rank 0) — CPP ≥ 阈值 + 防 bug 双重过滤
    🟢 积分深折扣        (rank 1) — 任意日期积分 ≤ 平日均价×ratio (捕捉平日 bug 价)
    🔴 积分同日暴降      (rank 2) — 同日积分降幅 ≥ 阈值 (需历史)
    🟠 节假日积分低价    (rank 3) — 节假日 + 积分 ≤ 平日均价×ratio
    🟣 现金深折扣        (rank 4) — 现金 USD ≤ 平日均价×ratio
    🟡 现金同日暴降      (rank 5) — 同日现金降幅 ≥ 阈值 (需历史)

三层阈值制 (减少推送噪音, 同时保留全量数据用于回查):
    1. file  阈值 (xxx_threshold / xxx_ratio):  入全量文件 ihg_logs/alerts_*.md
    2. push  阈值 (xxx_push):                   推送 Server酱 微信
    3. 全局排序后取 top_n_global (默认 30) 推送

防 bug 价过滤 (💎 高 CPP):
    - 积分 > 平日积分均价 → 跳过 (积分异常高 = 可能 IHG 数据异常)
    - 现金 > 平日现金均价×1.5 → 跳过 (现金虚高 = 假性高 CPP)

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

# ============ 6 条告警规则元数据表 (整合档 B2: 表驱动) ============
# level: 推送/文件中的 emoji 标记 (作为唯一键)
# rank: 排序优先级 (越小越优先, 用于 (hotel,date) 去重时保留高优先级)
# type: 告警类型显示名
# kind: "points" 积分类 (受 max_points_per_night 推送过滤) / "cash" 现金类 (不受限)
# file_key: file 阈值在 rules dict 中的 key
# file_default: file 阈值默认值 (rules 缺省时回退使用)
# push_key: push 阈值在 rules dict 中的 key
# push_default: push 阈值默认值
# score_kind:
#   "absolute"      - score 直接和阈值比 (score >= threshold). 用于 CPP / 暴降百分比类
#   "below_avg_pct" - score 是 (1-ratio)*100, push 比较 (1-push_ratio)*100. 用于深折扣类
ALERT_RULES = {
    "💎": {
        "rank": 0, "type": "高CPP积分房", "kind": "points",
        "file_key": "min_cpp_threshold", "file_default": 0.8,
        "push_key": "min_cpp_push", "push_default": 1.0,
        "score_kind": "absolute",
    },
    "🟢": {
        "rank": 1, "type": "积分深折扣", "kind": "points",
        "file_key": "points_deep_discount_ratio", "file_default": 0.5,
        "push_key": "points_deep_discount_push_ratio", "push_default": 0.4,
        "score_kind": "below_avg_pct",
    },
    "🔴": {
        "rank": 2, "type": "积分同日暴降", "kind": "points",
        "file_key": "points_drop_pct", "file_default": 40,
        "push_key": "points_drop_push_pct", "push_default": 50,
        "score_kind": "absolute",
    },
    "🟠": {
        "rank": 3, "type": "节假日积分低价", "kind": "points",
        "file_key": "holiday_points_ratio", "file_default": 0.9,
        "push_key": "holiday_points_push_ratio", "push_default": 0.7,
        "score_kind": "below_avg_pct",
    },
    "🟣": {
        "rank": 4, "type": "现金深折扣", "kind": "cash",
        "file_key": "cash_deal_ratio", "file_default": 0.5,
        "push_key": "cash_deal_push_ratio", "push_default": 0.4,
        "score_kind": "below_avg_pct",
    },
    "🟡": {
        "rank": 5, "type": "现金同日暴降", "kind": "cash",
        "file_key": "cash_drop_pct", "file_default": 50,
        "push_key": "cash_drop_push_pct", "push_default": 60,
        "score_kind": "absolute",
    },
}

# 积分类规则集合 (受 max_points_per_night 推送过滤影响)
POINTS_LEVELS = {lvl for lvl, meta in ALERT_RULES.items() if meta["kind"] == "points"}


def _make_alert(level, hotel_code, label, d, detail, score, **extra):
    """从 ALERT_RULES 表构建 alert dict, 自动填入 rank/type
    extra 可传 threshold (💎 用于 weight 计算) / points (积分类用于 push max_points 过滤) 等额外字段
    """
    meta = ALERT_RULES[level]
    alert = {
        "level": level,
        "rank": meta["rank"],
        "type": meta["type"],
        "hotel": hotel_code,
        "label": label,
        "date": d,
        "detail": detail,
        "score": score,
    }
    alert.update(extra)
    return alert


# DEFAULT_RULES 自动从 ALERT_RULES 派生 file/push 默认值, 加 max_points/top_n_global
def _build_default_rules():
    d = {}
    for meta in ALERT_RULES.values():
        d[meta["file_key"]] = meta["file_default"]
        d[meta["push_key"]] = meta["push_default"]
    d["max_points_per_night"] = 35000
    d["top_n_per_hotel"] = 5
    d["top_n_global"] = 30
    return d


DEFAULT_RULES = _build_default_rules()


# ============ 中国节假日 2026~2027 ============

def _build_holidays():
    """构建 2026~2027 中国节假日集合
    规则:
      - 连续假期 ≥ 3 天 (春节/五一/国庆): 核心日期 + 前后各2天缓冲
      - 单天假期 (元旦/清明/端午/中秋): 仅当天, 不加缓冲
    """
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

    expanded = set()
    names = {}
    for d_str, name in long_holidays.items():
        d = date.fromisoformat(d_str)
        for offset in range(-2, 3):
            dd = (d + timedelta(days=offset)).isoformat()
            expanded.add(dd)
            if dd not in names:
                names[dd] = name
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
    return sum(values) / len(values) if len(values) >= 10 else None


# ============ 权重评分系统 ============

def compute_weight(alert):
    """统一权重评分 (0~100), 值越大越值得关注

    公式设计 (按重要性给予基础分):
      💎 高CPP:           min(100, (cpp/threshold - 1)*50 + 70)  → 阈值时 70, 2×阈值时 100
      🟢 积分深折扣:      min(100, pct*1.5 + 40)                 → 50%折扣=100, 30%折扣=85 (最稀缺, 基础分高)
      🔴 积分暴降:        min(100, pct*0.8 + 20)                 → 50%降幅=60, 80%降幅=84
      🟠 节假日积分低价:  min(100, pct*1.5 + 30)                 → 30%低=75, 10%低=45
      🟣 现金深折扣:      min(100, pct*1.2 + 25)                 → 50%折扣=85, 60%折扣=97
      🟡 现金暴降:        min(100, pct*0.6 + 15)                 → 60%降幅=51, 80%降幅=63
    """
    level = alert["level"]
    score = alert.get("score", 0)

    if level == "💎":
        threshold = alert.get("threshold", 0.8)
        if threshold > 0:
            weight = min(100, (score / threshold - 1) * 50 + 70)
        else:
            weight = 70
    elif level == "🟢":
        # score = below_avg_pct (积分低于均价的百分比)
        weight = min(100, score * 1.5 + 40)
    elif level == "🔴":
        weight = min(100, score * 0.8 + 20)
    elif level == "🟠":
        weight = min(100, score * 1.5 + 30)
    elif level == "🟣":
        weight = min(100, score * 1.2 + 25)
    elif level == "🟡":
        weight = min(100, score * 0.6 + 15)
    else:
        weight = 0

    return round(weight, 1)


# ============ Push 阈值过滤 ============

def passes_push_threshold(alert, rules):
    """根据 alert 类型 + push 阈值判断是否进入推送 (整合档 B2: 改为查 ALERT_RULES 表)

    设计: 全量文件保留所有 file 阈值触发的告警 (供回查),
          推送只发更严的 push 阈值, 减少噪音聚焦真极端。

    积分上限过滤: 4 条积分规则 (💎🟢🟠🔴, kind=points) 推送前额外检查 max_points_per_night,
                  超过预算的高积分酒店即使 CPP 再高也不推送 (但全量文件已记录).
    """
    level = alert["level"]
    meta = ALERT_RULES.get(level)
    if not meta:
        return False

    # 积分类告警: 推送阶段额外过滤 max_points_per_night
    if meta["kind"] == "points":
        max_points = rules.get("max_points_per_night", 35000)
        alert_points = alert.get("points")
        if alert_points and alert_points > max_points:
            return False  # 高积分酒店不推送 (但仍在 alerts_*.md 中可查)

    score = alert.get("score", 0)
    push_value = rules.get(meta["push_key"], meta["push_default"])

    if meta["score_kind"] == "absolute":
        # CPP / 暴降百分比类: score 直接和 push 阈值比较
        return score >= push_value
    else:  # below_avg_pct
        # 深折扣类: score 是 (1-ratio)*100, push 阈值需转换为 (1-push_ratio)*100 才能比
        return score >= (1 - push_value) * 100


# ============ 告警筛选 ============

def filter_alerts(results, db, config):
    """从 batch_monitor 结果中筛选 6 类告警:
       1. 用 file 阈值收集所有触发条件 → all_alerts (全量写文件)
       2. 用 push 阈值二次过滤 → push_alerts (推送)
       3. 全局按权重排序, 取 top_n_global

    Returns: 推送用 push_alerts (top_n_global 限制后)
    """
    rules = config.get("rules", DEFAULT_RULES)
    today_str = date.today().isoformat()
    all_alerts = []

    for r in results:
        if not r.get("success"):
            continue

        hotel_code = r["hotel_code"]
        changes = r.get("changes", [])
        prices = r.get("prices", [])
        label = _get_hotel_label(hotel_code, db, config)

        # 平日均价
        avg_cash_usd = compute_weekday_avg(prices, "cash_usd")
        avg_points = compute_weekday_avg(prices, "points")

        # ===== 基于本次快照扫描 =====
        hotel_snapshot_alerts = []
        # 从 ALERT_RULES 表 + 用户 rules 取阈值, 缺省时回退到 file_default
        min_cpp = rules.get(ALERT_RULES["💎"]["file_key"], ALERT_RULES["💎"]["file_default"])
        deep_ratio = rules.get(ALERT_RULES["🟢"]["file_key"], ALERT_RULES["🟢"]["file_default"])
        holiday_ratio = rules.get(ALERT_RULES["🟠"]["file_key"], ALERT_RULES["🟠"]["file_default"])
        deal_ratio = rules.get(ALERT_RULES["🟣"]["file_key"], ALERT_RULES["🟣"]["file_default"])
        # 注: max_points_per_night 不在此处过滤, 移到 passes_push_threshold 推送阶段
        # 这样所有 file 阈值触发的告警 (含高积分酒店) 都会写入 alerts_*.md 供回查

        for p in prices:
            d = p.get("date", "")
            if not d or d <= today_str:
                continue

            cash_usd = p.get("cash_price_usd")
            points = p.get("points")
            cpp = p.get("cpp")  # USD 美分/积分

            # 💎 高 CPP 积分房 (双重 bug 价过滤)
            if cpp and cpp >= min_cpp and points:
                points_ok = not avg_points or points <= avg_points        # 排除积分虚高
                cash_ok = not avg_cash_usd or cash_usd <= avg_cash_usd * 1.5  # 排除现金虚高
                if points_ok and cash_ok:
                    hotel_snapshot_alerts.append(_make_alert(
                        "💎", hotel_code, label, d,
                        f"{points}分 ≈${cash_usd:.0f} CPP={cpp:.2f}¢",
                        cpp,
                        threshold=min_cpp,  # weight 公式 💎 用
                        points=points,      # push 阶段 max_points 过滤用
                    ))

            # 🟢 积分深折扣 (任意日期, 平日 bug 积分价利器)
            if points and avg_points and points <= avg_points * deep_ratio:
                pct = (1 - points / avg_points) * 100
                hotel_snapshot_alerts.append(_make_alert(
                    "🟢", hotel_code, label, d,
                    f"{points}分 (-{pct:.0f}%均价{avg_points:.0f}分)",
                    pct,
                    points=points,
                ))

            # 🟠 节假日积分低价
            if points and avg_points and is_holiday(d) and points <= avg_points * holiday_ratio:
                pct = (1 - points / avg_points) * 100
                hotel_snapshot_alerts.append(_make_alert(
                    "🟠", hotel_code, label, d,
                    f"{points}分 (-{pct:.0f}%均价) {get_holiday_name(d)}",
                    pct,
                    points=points,
                ))

            # 🟣 现金深折扣 (现金规则, 不受 max_points 约束)
            if cash_usd and avg_cash_usd and cash_usd <= avg_cash_usd * deal_ratio:
                pct = (1 - cash_usd / avg_cash_usd) * 100
                hotel_snapshot_alerts.append(_make_alert(
                    "🟣", hotel_code, label, d,
                    f"${cash_usd:.0f} (-{pct:.0f}%均价${avg_cash_usd:.0f})",
                    pct,
                ))

        hotel_snapshot_alerts.sort(key=lambda x: -x["score"])
        all_alerts.extend(hotel_snapshot_alerts)

        # ===== 基于 changes 历史对比 =====
        for c in changes:
            d = c.get("date", "")
            if not d or d <= today_str:
                continue

            # 🔴 积分同日暴降 (file 阶段不卡 max_points, 推送阶段再卡)
            if c["type"] == "积分降":
                pct = abs(c.get("pct", 0))
                drop_threshold = rules.get(ALERT_RULES["🔴"]["file_key"], ALERT_RULES["🔴"]["file_default"])
                if pct >= drop_threshold:
                    all_alerts.append(_make_alert(
                        "🔴", hotel_code, label, d,
                        f"{c['old_value']}→{c['new_value']}分 (-{pct:.0f}%)",
                        pct,
                        points=c.get("new_value"),  # push 阶段 max_points 过滤用
                    ))
            # 🟡 现金同日暴降
            elif c["type"] == "现金降":
                pct = abs(c.get("pct", 0))
                drop_threshold = rules.get(ALERT_RULES["🟡"]["file_key"], ALERT_RULES["🟡"]["file_default"])
                if pct >= drop_threshold:
                    all_alerts.append(_make_alert(
                        "🟡", hotel_code, label, d,
                        f"${c.get('new_value',0):.0f} (原${c.get('old_value',0):.0f}, -{pct:.0f}%)",
                        pct,
                    ))

    # 去重: (hotel, date) 保留最高优先级 (rank 最小)
    dedup = {}
    for a in all_alerts:
        key = (a["hotel"], a["date"])
        if key not in dedup or a["rank"] < dedup[key]["rank"]:
            dedup[key] = a
    all_alerts = list(dedup.values())

    # 计算权重 + 全局降序
    for a in all_alerts:
        a["weight"] = compute_weight(a)
    all_alerts.sort(key=lambda a: -a["weight"])

    # 写全量告警文件 (file 阈值的所有触发, 不限条数)
    _write_all_alerts_file(all_alerts)

    # push 阈值二次过滤 + top_n_global
    push_alerts = [a for a in all_alerts if passes_push_threshold(a, rules)]
    top_n_global = rules.get("top_n_global", 30)
    return push_alerts[:top_n_global]


def _write_all_alerts_file(all_alerts):
    """全量告警写入文件 (file 阈值触发的所有, 按权重排序)
    路径: ./ihg_logs/alerts_YYYYMMDD_HHMMSS.md
    """
    if not all_alerts:
        return None
    try:
        from datetime import datetime
        log_dir = Path(__file__).parent / "ihg_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        file_path = log_dir / f"alerts_{ts}.md"

        lines = [
            f"# IHG 全量告警 ({len(all_alerts)}条)",
            f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"按权重降序排列, 仅满足 file 阈值即入库 (推送层会用更严的 push 阈值过滤)\n",
        ]

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
                lines.append(f"- {a['level']} **{a['type']}** `{a['date']}` W={a['weight']:.0f} | {a['detail']} [预订]({url})")
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
    note = config.get("hotels", {}).get(hotel_code, {}).get("note", "")
    if note:
        return note
    if db:
        info = db.get_hotel(hotel_code)
        if info and info.get("name"):
            return info["name"]
    return hotel_code


# ============ 消息格式化 ============

def format_message(alerts):
    """格式化精简 Markdown 消息: 按酒店分组, 每条一行"""
    if not alerts:
        return None, None

    title = f"IHG 高性价比 ({len(alerts)}条)"
    lines = [f"## {title}\n"]

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
            lines.append(f"- {a['level']} **{a['type']}** `{a['date']}` {a['detail']} [预订]({url})")
        lines.append("")

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
        print("[通知] 无满足 push 阈值的告警 (file 阈值告警已写入文件)")
        return

    title, body = format_message(alerts)
    if not title:
        return

    print(f"\n{'='*60}")
    print(f"  📢 {title}")
    print(f"{'='*60}")
    for a in alerts:
        print(f"  {a['level']} [{a['type']}] [{a['label']}] {a['date']} {a['detail']} (W={a['weight']:.0f})")
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
                f"6条告警规则 + 三层阈值:\n"
                f"💎 高CPP | 🟢 积分深折扣 | 🔴 积分暴降\n"
                f"🟠 节假日积分 | 🟣 现金深折扣 | 🟡 现金暴降\n\n"
                f"file 阈值入库, push 阈值推送, top {DEFAULT_RULES['top_n_global']} 条\n\n"
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
