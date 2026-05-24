"""
make_tier.py - 把 hotels DB 切成监控 tier 子集 (按品牌 + 地区 + 评分)

设计思路:
    用户的核心需求 = 极低积分价 / 现金价兑换"高端酒店 + 度假区酒店"
    6000+ 全监控 = 算力浪费 + 推送爆炸 + 反爬压力, 用 tier 收敛到 ~5% (~300 家)

输出两个 CSV (格式跟 ihg_hotels.csv 一致, 直接喂给 batch_monitor --from-csv):
    tier_a_critical.csv  -- 关注核心 (从 notify_config.json hotels 字段读, 无则用默认 5 家)
                           可选, 用于测试或个别酒店高频监控
    tier_b_premium.csv   -- 主战场: 高端品牌全球 + 中高端品牌在度假区
                           频率建议: 每天 1 次全量 (晚上)
                           注: Tier B 不排除 Tier A 的酒店, 纯按规则筛.
                                两个 tier 互相独立 (可能有重叠).

用法:
    python make_tier.py
    然后只跑 Tier B (主用):
    python ihg_batch_monitor.py --from-csv tier_b_premium.csv --auto-switch

    Tier A 可选 (测试/个别酒店高频):
    python ihg_batch_monitor.py --from-csv tier_a_critical.csv --auto-switch

调整规则: 直接编辑下面的 PREMIUM_BRANDS / RESORT_BRANDS / RESORT_COUNTRIES.
"""

import csv
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from ihg_db import IHGDatabase  # noqa: E402


# ============ Tier 定义 (可调) ============

# Tier A 默认列表 (优先从 notify_config.json hotels 字段读)
DEFAULT_TIER_A = ["DADHA", "HKGKL", "HKGKH", "HKGIN", "PQCCP"]

# Tier B 品牌定义
PREMIUM_BRANDS = {
    # 顶级 (全球任何位置都纳入, 因为 bug 积分价高发)
    "IC",       # InterContinental
    "RC",       # Regent
    "华巴",     # HUALUXE
    "SR",       # Six Senses
    "KI",       # Kimpton (美国本土品牌, 全球纳入因为偶有积分 bug)
    "VX",       # Vignette Collection (精选系列)
}

RESORT_BRANDS = {
    # 中高端 / 精选品牌, 仅在度假区国家纳入 (城市的 Hotel Indigo / Crowne Plaza 多是商务酒店, 噪音多)
    "HT",       # Hotel Indigo (从 PREMIUM 移到这里 - 城市的 Indigo bug 少, 度假区的有价值)
    "CP",       # Crowne Plaza (用户关注的 5 家中 HKGKL/HKGKH/PQCCP 都是这个品牌)
    "VC",       # voco
    "RS",       # Holiday Inn Resort (用户已统一为 HI, 这里保留以防漏)
}

# 度假目的地 country 白名单 (RESORT_BRANDS 仅在这些国家纳入 Tier B)
RESORT_COUNTRIES = {
    # 东南亚海岛
    "Maldives", "Vietnam", "Indonesia", "Thailand", "Philippines", "Malaysia",
    # 加勒比 / 中美洲度假
    "Mexico", "Dominican Republic", "Aruba", "Curacao", "Bahamas",
    "Cayman Islands", "Jamaica", "Costa Rica", "Belize", "Panama",
    "Saint Lucia", "Saint Kitts and Nevis", "Antigua and Barbuda", "Barbados",
    "Sint Maarten", "Anguilla", "Trinidad and Tobago", "Bermuda",
    "Turks and Caicos Islands", "British Virgin Islands", "U.S. Virgin Islands",
    # 南太 / 印度洋
    "Fiji", "French Polynesia", "Vanuatu", "New Caledonia", "Cook Islands",
    "Mauritius", "Seychelles", "Sri Lanka",
    # 中东度假
    "United Arab Emirates", "Oman", "Egypt", "Qatar",
    # 欧洲度假岛 / 海岸
    "Spain", "Italy", "Greece", "Croatia", "Portugal", "Cyprus", "Malta",
    # 注意: Mainland China 不在此列. country 字段没有省/城市粒度,
    #       中国大陆中高端品牌大部分在城市 (北京/上海/广州/成都),
    #       不是度假区. 顶级品牌 (IC/华巴等) 已在 PREMIUM_BRANDS 全球纳入,
    #       三亚/海南的 IC/华巴会自动包含, 不会漏.
}

# 评分门槛 (低于此分一概忽略, 排除新店 / 边缘酒店)
MIN_RATING = 3.5

# 输出文件名
OUTPUT_TIER_A = "tier_a_critical.csv"
OUTPUT_TIER_B = "tier_b_premium.csv"

# CSV 字段 (跟 ihg_hotel_list_fetcher 输出一致)
FIELDS = ["mnemonic", "name", "brand_code", "city", "country",
          "address", "rating", "review_count", "url"]


# ============ 实现 ============

def load_tier_a_codes():
    """优先从 notify_config.json 的 hotels 字段读 (用户实际配置的关注酒店)."""
    cfg_path = Path("notify_config.json")
    if cfg_path.exists():
        try:
            with open(cfg_path, encoding="utf-8") as f:
                data = json.load(f)
            codes = list((data.get("hotels") or {}).keys())
            if codes:
                print(f"[+] Tier A 来源: notify_config.json ({len(codes)} 个)")
                return codes
        except Exception as e:
            print(f"[!] notify_config.json 读取失败 ({e}), 用默认列表")
    print(f"[+] Tier A 来源: 默认列表 ({len(DEFAULT_TIER_A)} 个)")
    return DEFAULT_TIER_A


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow(r)


def parse_rating(value):
    """rating 字段可能是 None / 数字 / 字符串"""
    if value in (None, "", "None"):
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def main():
    db = IHGDatabase()
    print(f"[+] DB: {Path(db.db_path).resolve()}")

    all_rows = [dict(r) for r in db.conn.execute("SELECT * FROM hotels").fetchall()]
    print(f"[+] 全库共 {len(all_rows)} 个酒店\n")
    by_mn = {r["mnemonic"]: r for r in all_rows}

    # ── Tier A ──
    print("=" * 70)
    tier_a_codes = load_tier_a_codes()
    tier_a_rows = []
    missing = []
    for c in tier_a_codes:
        if c in by_mn:
            tier_a_rows.append(by_mn[c])
        else:
            missing.append(c)
    write_csv(OUTPUT_TIER_A, tier_a_rows)
    print(f"\n[Tier A] {len(tier_a_rows)} 个酒店 → {OUTPUT_TIER_A}")
    for r in tier_a_rows:
        rating = r.get("rating") or "N/A"
        print(f"    {r['mnemonic']:8s} {r.get('brand_code') or '?':4s}  "
              f"{(r['name'] or '')[:38]:38s} {(r.get('country') or '')[:18]:18s}  rating={rating}")
    if missing:
        print(f"    ⚠ 这 {len(missing)} 个 mnemonic 在 DB 找不到: {missing}")

    # ── Tier B ──
    # 注: Tier B 不排除 Tier A 的酒店, 两个 tier 互相独立 (可能有重叠).
    # 纯按 [品牌 ∪ (品牌 ∩ 度假区)] + 评分门槛 筛选.
    print()
    print("=" * 70)
    tier_b_rows = []
    rejected_low_rating = 0
    rejected_brand_not_match = 0

    for r in all_rows:
        rating = parse_rating(r.get("rating"))
        if rating is not None and rating < MIN_RATING:
            rejected_low_rating += 1
            continue

        brand = r.get("brand_code") or ""
        country = r.get("country") or ""

        if brand in PREMIUM_BRANDS:
            tier_b_rows.append(r)         # 顶级品牌全球纳入
        elif brand in RESORT_BRANDS and country in RESORT_COUNTRIES:
            tier_b_rows.append(r)         # 中高端但在度假区
        else:
            rejected_brand_not_match += 1

    # 按 (评分降序, 国家, 品牌) 排序便于人工浏览
    tier_b_rows.sort(key=lambda r: (
        -(parse_rating(r.get("rating")) or 0),
        r.get("country") or "",
        r.get("brand_code") or ""
    ))
    write_csv(OUTPUT_TIER_B, tier_b_rows)
    print(f"\n[Tier B] {len(tier_b_rows)} 个酒店 → {OUTPUT_TIER_B}")
    print(f"    剔除原因:")
    print(f"      评分 < {MIN_RATING}:           {rejected_low_rating} 个")
    print(f"      品牌+地区不匹配:     {rejected_brand_not_match} 个")

    by_brand = Counter(r.get("brand_code") or "" for r in tier_b_rows)
    by_country = Counter(r.get("country") or "" for r in tier_b_rows)
    print(f"\n    按品牌分布:")
    for b, n in by_brand.most_common():
        print(f"      {b or '(空)':10s} {n:5d}")
    print(f"\n    按国家分布 (top 20):")
    for c, n in by_country.most_common(20):
        print(f"      {c or '(空)':25s} {n:5d}")

    db.close()

    # ── 使用说明 ──
    print()
    print("=" * 70)
    print("✓ 监控建议:")
    print(f"  python ihg_batch_monitor.py --from-csv {OUTPUT_TIER_B} --auto-switch")
    print(f"     → 主战场, 每天 1 次全量 (晚上)")
    print(f"     → 增量模式可加 --incremental, 速度快 6 倍, 适合每天跑")
    print(f"  python ihg_batch_monitor.py --from-csv {OUTPUT_TIER_A} --auto-switch")
    print(f"     → 可选, 测试或个别酒店高频监控")
    print()
    print("调整规则: 直接编辑 make_tier.py 顶部的")
    print("  PREMIUM_BRANDS / RESORT_BRANDS / RESORT_COUNTRIES / MIN_RATING")


if __name__ == "__main__":
    main()
