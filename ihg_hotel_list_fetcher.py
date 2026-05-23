"""
IHG 酒店列表抓取工具 - 按国家/地区收集酒店信息

核心特性:
    - country 字段以酒店 address 末尾的国家名为准 (准确度 99%+)
        * 解决 IHG region accordion 结构不规则的问题:
          - "US & Canada"             → 二级链接是州/省级 (Alabama Hotels / Ontario Hotels)
          - "Mexico & Central America" → Mexico 段是城市级 (Acapulco Hotels), 中美洲段是国家级
          - "Asia / Europe / Middle East / Africa" → 国家级
          每种结构混着用 link 文字推国家都会出错, 改从酒店自己的 address 提
    - 跨次抓取自动修正历史错误 (与 SQLite upsert 行为对齐, CSV 也走 upsert 而非 skip)
    - --fix-country: 一次性根据 address 重算全库 country, 修复历史误记录

用法:
    # 抓取整个区域
    python ihg_hotel_list_fetcher.py --region "Asia"
    python ihg_hotel_list_fetcher.py --region "US & Canada"
    python ihg_hotel_list_fetcher.py --region "Mexico & Central America"

    # 指定单个/多个目标
    python ihg_hotel_list_fetcher.py --target "Vietnam Hotels"
    python ihg_hotel_list_fetcher.py --region "Europe" --target "France Hotels,Italy Hotels"

    # 一次性修复历史 country 误记录 (基于 address 重算, 不抓取)
    python ihg_hotel_list_fetcher.py --fix-country
"""

import argparse
import asyncio
import csv
import json
import re
import sys
from pathlib import Path
from playwright.async_api import async_playwright


# ============ 配置 ============

EXPLORE_URL = "https://www.ihg.com/explore"
USER_DATA_DIR = "./ihg_browser_profile"
DEFAULT_OUTPUT = "ihg_hotels.csv"


# ============ 国家名规范集合 ============
# 这是 country 字段的"规范值"全集. address 解析出来的国家名必须落在这里 (或别名表),
# 否则视为解析失败, 回退到上层 link 文字推断.
# 命名风格采用 IHG 前端展示 (如 "Mainland China" / "Hong Kong SAR" 而非中立 ISO 全名),
# 跟 explore 页面 / hotels 表 country 字段保持一致.

KNOWN_COUNTRIES = {
    # 北美
    "United States", "Canada",
    # 中美洲 / 加勒比
    "Mexico", "Costa Rica", "Guatemala", "El Salvador", "Honduras",
    "Nicaragua", "Panama", "Belize",
    "Dominican Republic", "Haiti", "Cuba", "Jamaica", "Trinidad and Tobago",
    "Barbados", "Bahamas", "Puerto Rico", "Cayman Islands", "Aruba", "Curacao",
    "British Virgin Islands", "U.S. Virgin Islands", "Turks and Caicos Islands",
    "Antigua and Barbuda", "Saint Lucia", "Saint Kitts and Nevis",
    "Saint Vincent and the Grenadines", "Grenada", "Dominica", "Bermuda",
    "Sint Maarten", "Anguilla",
    # 南美
    "Brazil", "Argentina", "Chile", "Peru", "Colombia", "Ecuador", "Venezuela",
    "Bolivia", "Paraguay", "Uruguay", "Guyana", "Suriname",
    # 东亚 / 东南亚
    "Mainland China", "Hong Kong SAR", "Macau SAR", "Taiwan",
    "Japan", "South Korea", "North Korea",
    "Vietnam", "Thailand", "Indonesia", "Philippines", "Malaysia", "Singapore",
    "Cambodia", "Laos", "Myanmar", "Brunei", "Timor-Leste",
    # 南亚
    "India", "Sri Lanka", "Bangladesh", "Nepal", "Pakistan", "Maldives", "Bhutan",
    "Afghanistan",
    # 大洋洲
    "Australia", "New Zealand", "Fiji", "Papua New Guinea", "New Caledonia",
    "French Polynesia", "Samoa", "Tonga", "Vanuatu", "Cook Islands", "Solomon Islands",
    # 中东
    "United Arab Emirates", "Saudi Arabia", "Qatar", "Kuwait", "Bahrain", "Oman",
    "Jordan", "Lebanon", "Israel", "Palestine", "Turkey", "Yemen", "Iraq", "Iran", "Syria",
    # 非洲
    "Egypt", "Morocco", "Tunisia", "Algeria", "Libya", "Sudan",
    "South Africa", "Nigeria", "Kenya", "Ethiopia", "Tanzania", "Uganda",
    "Ghana", "Senegal", "Cote d'Ivoire", "Cameroon", "Mozambique",
    "Mauritius", "Madagascar", "Seychelles", "Namibia", "Botswana",
    "Zambia", "Zimbabwe", "Rwanda", "Angola", "Gabon", "Djibouti",
    # 欧洲
    "United Kingdom", "Ireland", "France", "Germany", "Italy", "Spain", "Portugal",
    "Netherlands", "Belgium", "Luxembourg", "Switzerland", "Austria",
    "Sweden", "Norway", "Denmark", "Finland", "Iceland",
    "Poland", "Czech Republic", "Slovakia", "Hungary", "Romania", "Bulgaria",
    "Greece", "Russia", "Ukraine", "Belarus", "Lithuania", "Latvia", "Estonia",
    "Serbia", "Croatia", "Bosnia and Herzegovina", "Slovenia", "Albania",
    "North Macedonia", "Montenegro", "Malta", "Cyprus", "Moldova",
    "Armenia", "Georgia", "Azerbaijan",
    "Kazakhstan", "Uzbekistan", "Tajikistan", "Kyrgyzstan", "Turkmenistan",
    "Liechtenstein", "Monaco", "Andorra", "San Marino", "Vatican City",
}

# 别名表: address 末尾常见的简写/不规范写法 → 上面 KNOWN_COUNTRIES 里的标准名.
# 通过用户实测样本驱动迭代 (如 IHG 写 "Trinidad & Tobago" 而非 "Trinidad and Tobago").

COUNTRY_ALIASES = {
    # 美国
    "USA": "United States", "U.S.A.": "United States", "U.S.": "United States",
    "United States of America": "United States",
    # 英国
    "UK": "United Kingdom", "U.K.": "United Kingdom",
    "Britain": "United Kingdom", "Great Britain": "United Kingdom",
    "England": "United Kingdom", "Scotland": "United Kingdom",
    "Wales": "United Kingdom", "Northern Ireland": "United Kingdom",
    # 中国大陆
    "China": "Mainland China", "P.R. China": "Mainland China",
    "P.R.China": "Mainland China", "PRC": "Mainland China",
    "People's Republic of China": "Mainland China",
    # 港澳台
    "Hong Kong": "Hong Kong SAR", "Hong Kong, China": "Hong Kong SAR",
    "Macau": "Macau SAR", "Macao": "Macau SAR", "Macao, China": "Macau SAR",
    "Taiwan, Province of China": "Taiwan",
    # 韩国
    "Republic of Korea": "South Korea", "Korea": "South Korea",
    # 越南 / 缅甸
    "Viet Nam": "Vietnam",
    "Burma": "Myanmar",
    # 中东
    "UAE": "United Arab Emirates", "U.A.E.": "United Arab Emirates",
    # 别名
    "Czechia": "Czech Republic",
    # IHG 实测样本里发现的写法差异
    "Trinidad & Tobago": "Trinidad and Tobago",
    "Antigua & Barbuda": "Antigua and Barbuda",
    "Saint Vincent & the Grenadines": "Saint Vincent and the Grenadines",
    # 加勒比 St./Saint
    "St. Lucia": "Saint Lucia", "St Lucia": "Saint Lucia",
    "St. Kitts and Nevis": "Saint Kitts and Nevis",
    "St Kitts and Nevis": "Saint Kitts and Nevis",
    "St. Vincent and the Grenadines": "Saint Vincent and the Grenadines",
    "St. Maarten": "Sint Maarten",
    # 非洲
    "Côte d'Ivoire": "Cote d'Ivoire",
    "Ivory Coast": "Cote d'Ivoire",
}

# 大小写不敏感快查表 (运行期不再变更)
_KNOWN_LOWER = {c.lower(): c for c in KNOWN_COUNTRIES}
_ALIAS_LOWER = {a.lower(): v for a, v in COUNTRY_ALIASES.items()}


def extract_country_from_address(address):
    """从 IHG 酒店 address 字段提取所在国家名 (返回 KNOWN_COUNTRIES 里的规范值).

    解析策略:
        1. 用 [,;] 拆段, 只看最后 3 段 (避免地址中间偶然出现的国家名干扰)
        2. 从右往左, 每段先剥离尾随邮编:
           - 字母数字混合: "T9M 0K9" / "KY1-1303" / "SW1A 1AA"
           - 纯数字 + 可选连字符: "00680-6328" / "550002" / "850-0931"
        3. 剩下文字做别名匹配 → 精确匹配 KNOWN_COUNTRIES (大小写不敏感)
        4. 任一段命中则返回, 未命中返回 None (调用方应回退到其它来源)
    """
    if not address:
        return None
    parts = [p.strip() for p in re.split(r'[,;\n]', address) if p.strip()]
    for part in reversed(parts[-3:]):
        cleaned = part
        # 剥离尾随字母数字邮编 (如 T9M 0K9, KY1-1303, A1A 0R5, SW1A 1AA)
        cleaned = re.sub(
            r'\b[A-Z]\d[A-Z]?[\s\-]?\d?[A-Z]?\d?[A-Z]?\b\s*$',
            '', cleaned, flags=re.IGNORECASE
        ).strip()
        # 剥离尾随纯数字邮编 (如 00680-6328, 550002, 850-0931)
        cleaned = re.sub(r'\b\d{2,}[-\s]?\d*\b\s*$', '', cleaned).strip()
        # 移除任何剩余的数字/连字符
        cleaned = re.sub(r'[\d\-]+', '', cleaned).strip()
        cleaned = ' '.join(cleaned.split())
        if not cleaned:
            continue
        low = cleaned.lower()
        if low in _ALIAS_LOWER:
            return _ALIAS_LOWER[low]
        if low in _KNOWN_LOWER:
            return _KNOWN_LOWER[low]
    return None


# ============ 兜底: 从 region accordion 的 link 文字推断 ============
# 极端情况 (address 字段缺失或 IHG 写了字典里没有的国家名) 才用到.
# 对 US/Canada 区域有特殊处理, 因为它的二级链接是州/省级.

US_STATES = {
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
    "Connecticut", "Delaware", "District of Columbia", "Florida", "Georgia",
    "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky",
    "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota",
    "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire",
    "New Jersey", "New Mexico", "New York", "North Carolina", "North Dakota",
    "Ohio", "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island",
    "South Carolina", "South Dakota", "Tennessee", "Texas", "Utah", "Vermont",
    "Virginia", "Washington", "West Virginia", "Wisconsin", "Wyoming",
    # 用户实际数据里发现的旧错误形态
    "Washington DC",
}
CA_PROVINCES = {
    "Alberta", "British Columbia", "Manitoba", "New Brunswick",
    "Newfoundland and Labrador", "Newfoundland",  # 单写也算
    "Nova Scotia", "Ontario",
    "Prince Edward Island", "Quebec", "Saskatchewan",
    "Northwest Territories", "Nunavut", "Yukon",
}


def resolve_country_from_link(link_text):
    """[兜底] 从 region accordion link 文字推国家. 仅在 address 解析失败时使用."""
    name = (link_text or "").replace(" Hotels", "").strip()
    if name in US_STATES:
        return "United States"
    if name in CA_PROVINCES:
        return "Canada"
    return name


# ============ 工具函数 ============

def extract_mnemonic(url):
    """从酒店 URL 提取 mnemonic (4-6 位字母数字代码)"""
    if not url:
        return None
    m = re.search(r'/hotels/[a-z]{2}/[a-z]{2}/[^/]+/([a-zA-Z0-9]{4,6})(?:/hoteldetail|/index|/?$|/\?|$)', url)
    if m:
        return m.group(1).upper()
    m = re.search(
        r'/(?:intercontinental|regent|sixsenses|kimpton|hotelindigo|voco|crowneplaza|evenhotels|'
        r'holidayinnexpress|holidayinnclubvacations|holidayinnresort|holidayinn|garner|garner-hotels|'
        r'avidhotels|atwellsuites|staybridge|candlewood|iberostar|mrandmrssmith|vignettecollection|'
        r'ruby|kimptonhotels)/hotels/[a-z]{2}/[a-z]{2}/[^/]+/([a-zA-Z0-9]{4,6})(?:/|$|\?)',
        url, re.IGNORECASE
    )
    if m:
        return m.group(1).upper()
    return None


def extract_brand(url):
    """从 URL 路径提取品牌代码"""
    url_lower = url.lower()
    patterns = {
        "intercontinental": "IC", "regent": "RC", "sixsenses": "SR",
        "kimptonhotels": "KI", "kimpton": "KI", "hotelindigo": "HT",
        "voco": "VC", "crowneplaza": "CP", "evenhotels": "EH",
        "holidayinnexpress": "EX", "holidayinnclubvacations": "CV",
        "holidayinnresort": "RS", "holidayinn": "HI",
        "garner-hotels": "GE", "garner": "GE",
        "avidhotels": "AV", "atwellsuites": "AT",
        "staybridge": "SB", "candlewood": "CW",
        "iberostar": "IS", "mrandmrssmith": "MR",
        "vignettecollection": "VX", "ruby": "RU",
    }
    for key, code in sorted(patterns.items(), key=lambda x: -len(x[0])):
        if f"/{key}/" in url_lower:
            return code
    return ""


def extract_city(url):
    """从 URL 提取城市名"""
    m = re.search(r'/hotels/[a-z]{2}/[a-z]{2}/([^/]+)/[a-zA-Z0-9]{4,6}', url)
    return m.group(1).replace("-", " ").title() if m else ""


def load_existing_hotels(csv_path):
    """加载已有的 CSV 文件, 返回 {mnemonic: hotel_dict}.

    重要: 加载时会用 address 重新校准每条记录的 country 字段.
    这是为了让 country 永远是 "address 派生字段", 防止以下污染场景:
      - 老 CSV 里 country 是历史脏数据 (如 'Florida' 州名), 直接 load 会反向写回
        SQLite 把 fix-country 的修复成果覆盖掉
      - 用户手动编辑了 CSV 的 country 字段 (改错了)
    校准成本极低 (纯字符串处理), 一次性无脏数据后所有调用都是 noop.
    """
    hotels = {}
    if not Path(csv_path).exists():
        return hotels
    try:
        recalibrated = 0
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                mn = row.get("mnemonic", "").strip()
                if not mn:
                    continue
                # 自动校准 country (address → country)
                addr = row.get("address", "") or ""
                if addr.strip():
                    new_country = extract_country_from_address(addr)
                    if new_country and new_country != (row.get("country") or "").strip():
                        row["country"] = new_country
                        recalibrated += 1
                hotels[mn] = row
        msg = f"    [增量] 已加载 {len(hotels)} 个已有酒店记录"
        if recalibrated:
            msg += f"  (其中 {recalibrated} 条 country 字段已根据 address 自动校准)"
        print(msg)
    except Exception as e:
        print(f"    [!] 加载已有文件失败: {e}")
    return hotels


def save_results(hotels_list, csv_path):
    """保存结果到 CSV, JSON 和 SQLite"""
    def sort_key(h):
        country = h.get("country", "")
        rating_str = h.get("rating", "")
        try:
            rating = float(rating_str)
        except (ValueError, TypeError):
            rating = 0.0
        return (country, -rating)

    hotels_list.sort(key=sort_key)

    # CSV
    fields = ["mnemonic", "name", "brand_code", "city", "country", "address", "rating", "review_count", "url"]
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for h in hotels_list:
            writer.writerow(h)
    print(f"\n[+] CSV 已导出: {csv_path} ({len(hotels_list)} 条)")

    # JSON
    json_path = csv_path.replace(".csv", ".json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(hotels_list, f, indent=2, ensure_ascii=False)
    print(f"[+] JSON 已导出: {json_path}")

    # SQLite
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from ihg_db import IHGDatabase
        db = IHGDatabase()
        db_hotels = []
        for h in hotels_list:
            rating = None
            try:
                rating = float(h.get("rating", ""))
            except (ValueError, TypeError):
                pass
            review_count = None
            try:
                review_count = int(h.get("review_count", ""))
            except (ValueError, TypeError):
                pass
            db_hotels.append({
                "mnemonic": h.get("mnemonic", ""),
                "name": h.get("name", ""),
                "brand_code": h.get("brand_code", ""),
                "city": h.get("city", ""),
                "country": h.get("country", ""),
                "address": h.get("address", ""),
                "rating": rating,
                "review_count": review_count,
                "url": h.get("url", ""),
            })
        db.upsert_hotels(db_hotels)
        db.close()
        print(f"[+] SQLite 已更新: {db.db_path} ({len(db_hotels)} 个酒店)")
    except Exception as e:
        print(f"[!] SQLite 写入失败 (不影响 CSV/JSON): {e}")


# ============ --fix-country: 基于 address 全库重算 country ============

def run_fix_country():
    """根据每行的 address 重算 country, 全库一刀切修复历史误记录.

    覆盖所有历史错误形态:
      - 美国州名     ("Alabama" / "California" / ...)         → "United States"
      - 加拿大省名   ("Ontario" / "British Columbia" / ...)   → "Canada"
      - 墨西哥城市   ("Acapulco" / "Cancun" / ...)            → "Mexico"
      - 区域误归类   (link 文字推, 如香港 region 抓到深圳)   → 真实国家
      - 任何老错误                                              → address 解析正确国家
    address 缺失/解析失败的酒店, 保持原值不动 (打印 SKIPPED).
    """
    try:
        from datetime import date as _date
        from collections import Counter
        sys.path.insert(0, str(Path(__file__).parent))
        from ihg_db import IHGDatabase
    except Exception as e:
        print(f"[!] 无法导入 ihg_db: {e}")
        return

    db = IHGDatabase()
    print(f"[fix-country] 数据库绝对路径: {Path(db.db_path).resolve()}")

    rows = db.conn.execute(
        "SELECT mnemonic, name, country, address FROM hotels"
    ).fetchall()
    print(f"[fix-country] 共 {len(rows)} 个酒店, 开始基于 address 重算 country...")

    to_update = []          # [(mnemonic, old_country, new_country)]
    no_change = 0
    no_address = 0
    unparsable = []         # [(mnemonic, country, address)]: address 有但解析不出国家

    for r in rows:
        addr = r["address"] or ""
        if not addr.strip():
            no_address += 1
            continue
        new_country = extract_country_from_address(addr)
        if new_country is None:
            unparsable.append((r["mnemonic"], r["country"], addr))
            continue
        if new_country == r["country"]:
            no_change += 1
            continue
        to_update.append((r["mnemonic"], r["country"], new_country))

    # 概览
    print(f"\n[fix-country] 分析结果:")
    print(f"  → 需修复:      {len(to_update)} 个")
    print(f"  → 已正确:      {no_change} 个")
    print(f"  → address 缺失: {no_address} 个 (保持原值)")
    print(f"  → address 解析失败: {len(unparsable)} 个 (保持原值, 详见下文)")

    # 把变更按 (旧 → 新) 聚合, top 30
    if to_update:
        change_counter = Counter((old, new) for _, old, new in to_update)
        print(f"\n[fix-country] 变更分布 (按 旧 → 新, top 30):")
        for (old, new), n in change_counter.most_common(30):
            old_disp = old or "(空)"
            print(f"    {old_disp:30s} → {new:25s}  {n:5d} 行")

    # address 解析失败的样本: 提示用户反馈以补充 KNOWN_COUNTRIES / COUNTRY_ALIASES
    if unparsable:
        print(f"\n[fix-country] ⚠ 解析失败样本 (最多 10 个, 反馈给 Kiro 以扩充国家别名表):")
        for mn, c, addr in unparsable[:10]:
            print(f"    {mn:8s} country={c!r:20s} address={addr[:80]}")

    if not to_update:
        print("\n[fix-country] SQLite 数据库已干净 ✓")
        db.close()
        # 即便 SQLite 干净, CSV/JSON 也可能脏 (用户独立编辑过), 一起校准
        _sync_fix_csv_json(DEFAULT_OUTPUT)
        return

    # 执行修复
    today = _date.today().isoformat()
    for mn, _old, new in to_update:
        db.conn.execute(
            "UPDATE hotels SET country=?, updated_at=? WHERE mnemonic=?",
            (new, today, mn)
        )
    db.conn.commit()

    # 复核
    final = db.conn.execute(
        "SELECT country, COUNT(*) c FROM hotels GROUP BY country ORDER BY c DESC LIMIT 30"
    ).fetchall()
    print(f"\n[fix-country] ✓ SQLite 已更新 {len(to_update)} 行")
    print(f"[fix-country] 修复后 country 分布 (top 30):")
    for r in final:
        print(f"    {(r['country'] or '(空)'):30s} {r['c']:5d}")

    db.close()

    # ─── 同步修复 CSV / JSON (如果存在) ───
    # 没这步会被下一次抓取的 load_existing_hotels() 反向污染回 SQLite
    _sync_fix_csv_json(DEFAULT_OUTPUT)


def _sync_fix_csv_json(csv_path):
    """把 CSV / JSON 里 country 字段也基于 address 重算一遍, 保持三处一致."""
    csv_p = Path(csv_path)
    json_p = Path(csv_path.replace(".csv", ".json"))
    if not csv_p.exists() and not json_p.exists():
        print(f"\n[fix-country] CSV/JSON 不存在 ({csv_p}), 跳过")
        return

    if csv_p.exists():
        print(f"\n[fix-country] 同步修复 CSV: {csv_p.resolve()}")
        rows_in = []
        with open(csv_p, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames or []
            for row in reader:
                rows_in.append(row)
        changed = 0
        for row in rows_in:
            addr = row.get("address", "") or ""
            if not addr.strip():
                continue
            new_c = extract_country_from_address(addr)
            if new_c and new_c != (row.get("country") or "").strip():
                row["country"] = new_c
                changed += 1
        with open(csv_p, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in rows_in:
                writer.writerow(row)
        print(f"[fix-country] ✓ CSV 已更新 {changed} 行 (共 {len(rows_in)} 条)")

    if json_p.exists():
        print(f"[fix-country] 同步修复 JSON: {json_p.resolve()}")
        with open(json_p, "r", encoding="utf-8") as f:
            data = json.load(f)
        changed = 0
        for h in data:
            addr = h.get("address", "") or ""
            if not addr.strip():
                continue
            new_c = extract_country_from_address(addr)
            if new_c and new_c != (h.get("country") or "").strip():
                h["country"] = new_c
                changed += 1
        with open(json_p, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"[fix-country] ✓ JSON 已更新 {changed} 行 (共 {len(data)} 条)")


# ============ 页面操作 ============

async def collect_hotels_from_page(page, delay=3.0, country_fallback=""):
    """在当前页面: 滚动 + View More → 提取所有酒店卡片.

    每个酒店 country 字段确定顺序:
      1) extract_country_from_address(address) 解析地址末尾   ← 主路径, 99% 命中
      2) country_fallback (来自上层 region link 文字推)        ← 兜底
    """
    # 初始滚动
    for _ in range(3):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(500)

    # 智能循环: View More 直到数量不增加
    view_more_clicks = 0
    prev_count = 0
    no_change_rounds = 0

    while True:
        current_count = await page.evaluate(
            "() => document.querySelectorAll('a[href*=\"/hoteldetail\"]').length"
        )
        if current_count > prev_count:
            no_change_rounds = 0
            prev_count = current_count
        else:
            no_change_rounds += 1
        if no_change_rounds >= 2:
            break

        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(800)

        clicked_vm = await page.evaluate("""
        () => {
            const els = [...document.querySelectorAll('button, a, [role="button"]')];
            for (const el of els) {
                const t = (el.textContent || '').trim().toLowerCase();
                if (t.includes('view more') || t.includes('load more') || t.includes('show more')) {
                    el.scrollIntoView({behavior: 'instant', block: 'center'});
                    el.click();
                    return true;
                }
            }
            return false;
        }
        """)
        if clicked_vm:
            view_more_clicks += 1
            await page.wait_for_timeout(int(delay * 1000))
        else:
            await page.wait_for_timeout(1000)
            break

    # 提取酒店卡片
    hotel_links = await page.evaluate("""
    () => {
        const arr = [];
        for (const li of document.querySelectorAll('li.cmp-list__item')) {
            const linkEl = li.querySelector('a.cmp-card__title-link[href*="/hoteldetail"]');
            if (!linkEl) continue;
            const href = linkEl.href;
            const name = linkEl.textContent.trim().slice(0, 100);
            const addrEl = li.querySelector('address.cmp-card__address');
            const address = addrEl ? addrEl.textContent.trim().replace(/\\s+/g, ' ') : '';
            let rating = '';
            let reviewCount = '';
            const ratingEl = li.querySelector('span.cmp-card__rating-count');
            if (ratingEl) rating = ratingEl.textContent.trim();
            const reviewEl = li.querySelector('a.cmp-card__rating-count');
            if (reviewEl) {
                const rt = reviewEl.textContent.trim();
                const m = rt.match(/([\\d,]+)/);
                if (m) reviewCount = m[1].replace(/,/g, '');
            }
            arr.push({href, name, address, rating, reviewCount});
        }
        return arr;
    }
    """)

    hotels = {}
    for hl in hotel_links:
        mn = extract_mnemonic(hl["href"])
        if mn and mn not in hotels:
            address = hl.get("address", "")
            country = extract_country_from_address(address) or country_fallback
            hotels[mn] = {
                "mnemonic": mn,
                "name": hl["name"].split("\n")[0].strip()[:80],
                "url": hl["href"],
                "brand_code": extract_brand(hl["href"]),
                "city": extract_city(hl["href"]),
                "country": country,
                "address": address,
                "rating": hl.get("rating", ""),
                "review_count": hl.get("reviewCount", ""),
            }
    return hotels, view_more_clicks


async def collect_sub_region_links(page, current_url):
    """收集 'Hotels by State/Region/City' 区块内的子区域链接"""
    return await page.evaluate("""
    (currentUrl) => {
        const arr = [];
        const seen = new Set();
        const headings = [...document.querySelectorAll('h2, h3, h4, [class*="heading"], [class*="title"]')];
        let container = null;
        for (const h of headings) {
            const t = (h.textContent || '').trim().toLowerCase();
            if (t.includes('hotels by state') || t.includes('hotels by region') ||
                t.includes('hotels by city') || t.includes('hotels by area')) {
                container = h.parentElement;
                if (container && container.querySelectorAll('a').length < 3) {
                    container = container.parentElement;
                }
                break;
            }
        }
        if (!container) {
            const sections = document.querySelectorAll('[class*="region"], [class*="state"], [class*="destination-links"]');
            for (const s of sections) {
                if (s.querySelectorAll('a').length >= 3) { container = s; break; }
            }
        }
        if (!container) return arr;
        for (const a of container.querySelectorAll('a[href]')) {
            const href = a.href || '';
            const text = (a.textContent || '').trim();
            if (!href || !text) continue;
            if (seen.has(href)) continue;
            if (href.includes('/hoteldetail') || href.includes('/hotels/')) continue;
            if (href.replace(/\\/$/, '') === currentUrl.replace(/\\/$/, '')) continue;
            seen.add(href);
            arr.push({href, text});
        }
        return arr;
    }
    """, current_url)


async def goto_with_retry(page, url, max_retries=3, timeout=90000):
    """访问页面, 支持重试"""
    for attempt in range(max_retries):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            return True
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"    [!] 第{attempt+1}次超时, 重试...")
                await page.wait_for_timeout(3000)
            else:
                print(f"    [!] 加载失败 (已重试{max_retries}次): {e}")
    return False


# ============ 增量合并 (跨次抓取 upsert, 自动修正历史错误) ============

def merge_hotel(all_hotels, mn, info, counters):
    """把一条新抓到的酒店并入 all_hotels.

    - 新 mnemonic    → 加入, counters["new"] += 1
    - 已存在 mnemonic → 用新数据覆盖关键字段, 关键字段变更打印日志, counters["updated"] += 1
      (与 SQLite upsert 行为对齐, CSV 不再 skip 重复; 跨区域抓取自动修正旧错误的 country)
    """
    if mn not in all_hotels:
        all_hotels[mn] = info
        counters["new"] += 1
        return

    old = all_hotels[mn]
    changes = []
    for key in ("country", "city"):
        old_val = (old.get(key) or "").strip()
        new_val = (info.get(key) or "").strip()
        if new_val and old_val != new_val:
            changes.append(f"{key}: {old_val!r} → {new_val!r}")
    if changes:
        print(f"        [更新] {mn}  " + "  |  ".join(changes))
    all_hotels[mn] = info
    counters["updated"] += 1


# ============ 主逻辑 ============

async def main():
    parser = argparse.ArgumentParser(description="IHG 酒店列表抓取 - 按国家/地区收集")
    parser.add_argument("--target", type=str, default=None,
                        help='目标国家/地区, 多个用逗号分隔 (如 "Vietnam Hotels,Hong Kong SAR Hotels"); '
                             '不指定时抓 --region 下所有二级链接')
    parser.add_argument("--region", type=str, default="Asia",
                        help='大区域关键词 (默认 Asia, 可选 Europe / Middle East / Africa / '
                             'US & Canada / Mexico & Central America 等)')
    parser.add_argument("--output", type=str, default=DEFAULT_OUTPUT,
                        help=f'输出 CSV 文件名 (默认 {DEFAULT_OUTPUT})')
    parser.add_argument("--delay", type=float, default=3.0,
                        help='View More 点击后等待秒数 (默认 3)')
    parser.add_argument("--fix-country", action="store_true",
                        help='不抓取, 仅根据 hotels.address 字段重算所有酒店的 country (一次性迁移). '
                             '修复所有历史误记录 (州/省名/城市名/跨区域错归类), 一刀切.')
    args = parser.parse_args()

    # 数据迁移模式: 修复历史 country 字段, 不进入抓取流程
    if args.fix_country:
        run_fix_country()
        return

    targets = [t.strip() for t in args.target.split(",") if t.strip()] if args.target else None
    region_keyword = args.region

    Path(USER_DATA_DIR).mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print(f"IHG 酒店列表抓取")
    print(f"  目标: {', '.join(targets) if targets else '<整个大区域>'}")
    print(f"  区域: {region_keyword}")
    print(f"  输出: {args.output}")
    print("=" * 70)

    all_hotels = load_existing_hotels(args.output)
    initial_count = len(all_hotels)
    counters = {"new": 0, "updated": 0}

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
            print(f"\n[1/4] 访问 {EXPLORE_URL}")
            await page.goto(EXPLORE_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)

            try:
                btn = page.get_by_role("button", name="Accept All", exact=False)
                if await btn.count() > 0:
                    await btn.first.click(timeout=2000)
                    await page.wait_for_timeout(500)
            except Exception:
                pass

            for _ in range(8):
                await page.evaluate("window.scrollBy(0, window.innerHeight)")
                await page.wait_for_timeout(600)
            await page.wait_for_timeout(2000)

            print(f"[2/4] 展开 '{region_keyword}' 区域...")
            clicked = await page.evaluate("""
            async (keyword) => {
                const wait = (ms) => new Promise(r => setTimeout(r, ms));
                const btns = [...document.querySelectorAll('button.cmp-accordion__button')];
                for (const b of btns) {
                    if (b.textContent.trim().includes(keyword)) {
                        b.scrollIntoView({behavior: 'instant', block: 'center'});
                        await wait(300);
                        b.click();
                        await wait(2000);
                        return b.textContent.trim();
                    }
                }
                return false;
            }
            """, region_keyword)

            if not clicked:
                print(f"    [!] 未找到含 '{region_keyword}' 的区域!")
                all_btns = await page.evaluate(
                    "() => [...document.querySelectorAll('button.cmp-accordion__button')].map(b => b.textContent.trim())"
                )
                print(f"    可用区域: {all_btns}")
                return
            print(f"    ✓ 展开: {clicked}")
            await page.wait_for_timeout(2000)

            region_links = await page.evaluate("""
            (keyword) => {
                const arr = [];
                const btns = [...document.querySelectorAll('button.cmp-accordion__button')];
                const btn = btns.find(b => b.textContent.trim().includes(keyword));
                if (!btn) return arr;
                const header = btn.closest('.cmp-accordion__header') || btn.parentElement;
                const panel = header?.nextElementSibling;
                if (!panel) return arr;
                for (const a of panel.querySelectorAll('a')) {
                    const href = a.href || '';
                    const text = (a.textContent || '').trim();
                    if (href && text) arr.push({href, text});
                }
                return arr;
            }
            """, region_keyword)

            if targets is None:
                target_links_to_run = region_links
                print(f"[3/4] 未指定 --target, 抓整个 '{region_keyword}' 下全部 {len(target_links_to_run)} 个目标...")
            else:
                target_links_to_run = []
                for target_name in targets:
                    target_lower = target_name.lower()
                    matched = None
                    for lk in region_links:
                        if target_lower in lk["text"].lower() or target_lower in lk["href"].lower():
                            matched = lk
                            break
                    if matched:
                        target_links_to_run.append(matched)
                    else:
                        print(f"\n    [!] 未匹配 '{target_name}', 跳过")
                        print(f"    可用选项 ({len(region_links)} 个):")
                        for lk in region_links[:50]:
                            print(f"      - {lk['text']}")
                        if len(region_links) > 50:
                            print(f"      ... (还有 {len(region_links) - 50} 个未显示)")
                if not target_links_to_run:
                    print(f"    [!] 无任何匹配目标, 退出")
                    return
                print(f"[3/4] 开始抓取 {len(target_links_to_run)} 个目标...")

            for target_link in target_links_to_run:
                target_url = target_link["href"]
                country_fallback = resolve_country_from_link(target_link["text"])
                print(f"\n    === {target_link['text']}  (link 推断 country={country_fallback!r}, 实际以 address 为准) ===")

                if not await goto_with_retry(page, target_url):
                    continue
                await page.wait_for_timeout(2000)

                main_hotels, vm = await collect_hotels_from_page(page, args.delay, country_fallback)
                pre = counters["new"] + counters["updated"]
                for mn, info in main_hotels.items():
                    merge_hotel(all_hotels, mn, info, counters)
                added = counters["new"] + counters["updated"] - pre
                print(f"    主页面: {len(main_hotels)} 个酒店, {added} 个被合并 "
                      f"(累计 new={counters['new']} updated={counters['updated']}, VM: {vm})")

                sub_links = await collect_sub_region_links(page, target_url)
                if sub_links:
                    print(f"    子区域: {len(sub_links)} 个")
                    for idx, lk in enumerate(sub_links, 1):
                        print(f"      [{idx}/{len(sub_links)}] {lk['text']}")
                        if not await goto_with_retry(page, lk["href"]):
                            continue
                        await page.wait_for_timeout(2000)
                        sub_hotels, sub_vm = await collect_hotels_from_page(page, args.delay, country_fallback)
                        for mn, info in sub_hotels.items():
                            merge_hotel(all_hotels, mn, info, counters)

                print(f"    [{target_link['text']}] 完成, 累计 {len(all_hotels)} (new={counters['new']} updated={counters['updated']})")

        except KeyboardInterrupt:
            print("\n[!] 用户中断, 保存已收集数据...")
        except Exception as e:
            print(f"\n[!] 异常: {e}")
            import traceback
            traceback.print_exc()
        finally:
            await context.close()

    hotels_list = list(all_hotels.values())

    print(f"\n[4/4] 保存结果...")
    print(f"    本次新增: {counters['new']} 个酒店")
    print(f"    本次更新: {counters['updated']} 个酒店 (已有记录的 country/city 等字段被新数据覆盖)")
    print(f"    总计: {len(hotels_list)} 个唯一酒店 (起始 {initial_count})")

    if hotels_list:
        save_results(hotels_list, args.output)
        country_stats = {}
        for h in hotels_list:
            c = h.get("country", "未知")
            country_stats[c] = country_stats.get(c, 0) + 1
        print(f"\n    国家分布 (top 30):")
        for c, n in sorted(country_stats.items(), key=lambda x: -x[1])[:30]:
            print(f"      {c}: {n}")
    else:
        print("    [!] 无数据")


if __name__ == "__main__":
    asyncio.run(main())
