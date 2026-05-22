"""
IHG 酒店列表抓取工具 - 按国家/地区收集酒店信息
基于 ihg_test_single_region.py 验证通过的逻辑

功能:
    - 通过 --target 指定要抓取的国家/地区 (支持多个, 逗号分隔)
    - 通过 --region 指定大区域 (默认 Asia)
    - 自动识别二级链接是 "国家级" 还是 "州/省级"
        * 国家级 (Asia/Europe/...): "Vietnam Hotels" → country="Vietnam"
        * 州/省级 (US & Canada):    "Alabama Hotels" → country="United States"
                                      "Ontario Hotels" → country="Canada"
    - 自动展开子区域递归收集 (Hotels by State/Region)
    - 增量去重: 如果输出文件已存在, 自动加载已有数据, 只追加新酒店
    - 结果按国家分组, 每个国家内按评分从高到低排序
    - 输出 CSV + JSON 双格式

用法:
    # 抓取越南和香港
    python ihg_hotel_list_fetcher.py --target "Vietnam Hotels,Hong Kong SAR Hotels"

    # 抓取中国大陆 (会递归子区域)
    python ihg_hotel_list_fetcher.py --target "Mainland China Hotels"

    # 抓取欧洲的法国
    python ihg_hotel_list_fetcher.py --region "Europe" --target "France Hotels"

    # 抓取整个美加 (会按州/省抓, 自动归并到 United States / Canada)
    python ihg_hotel_list_fetcher.py --region "US & Canada"

    # 指定输出文件名
    python ihg_hotel_list_fetcher.py --target "Japan Hotels" --output japan_hotels.csv

    # 仅修正数据库里已存在的 美国州/加拿大省 误记的 country 字段 (一次性迁移)
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


# ============ 国家归一化 ============
# IHG 的 "US & Canada" 大区域展开后, 二级链接直接是州/省级 (跳过了国家层),
# 这里把州/省名映射回所属国家, 避免 country 字段被误存为州/省名.

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
}

CA_PROVINCES = {
    "Alberta", "British Columbia", "Manitoba", "New Brunswick",
    "Newfoundland and Labrador", "Nova Scotia", "Ontario",
    "Prince Edward Island", "Quebec", "Saskatchewan",
    "Northwest Territories", "Nunavut", "Yukon",
}


def resolve_country(link_text):
    """从二级链接文字推断真实国家名.

    覆盖两种 IHG 区域展开模式:
      1) 国家级 (Asia/Europe/...):  "France Hotels"  → "France"
      2) 州/省级 (US & Canada):     "Alabama Hotels" → "United States"
                                     "Ontario Hotels" → "Canada"

    注: 美国领土 (Puerto Rico/Guam/U.S. Virgin Islands 等) 保持原文,
        不强行并入 "United States", 因 IHG 在前端把它们作为独立目的地展示.
    """
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
    """加载已有的 CSV 文件, 返回 {mnemonic: hotel_dict}"""
    hotels = {}
    if not Path(csv_path).exists():
        return hotels
    try:
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                mn = row.get("mnemonic", "").strip()
                if mn:
                    hotels[mn] = row
        print(f"    [增量] 已加载 {len(hotels)} 个已有酒店记录")
    except Exception as e:
        print(f"    [!] 加载已有文件失败: {e}")
    return hotels


def save_results(hotels_list, csv_path):
    """保存结果到 CSV, JSON 和 SQLite"""
    # 排序: 先按国家, 再按评分从高到低
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
        import sys
        from pathlib import Path as _P
        sys.path.insert(0, str(_P(__file__).parent))
        from ihg_db import IHGDatabase
        db = IHGDatabase()
        # 转换字段格式 (rating/review_count 转数值)
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


# ============ 数据迁移: 修复历史 US/Canada country 字段 ============

def run_fix_country():
    """一次性修复 hotels.country: 把误存的州/省名批量回写为所属国家.

    修复对象:
      - country IN US_STATES   → "United States"
      - country IN CA_PROVINCES → "Canada"
    其它国家不动. Puerto Rico / Guam / U.S. Virgin Islands 等领土保持原样.
    """
    try:
        from datetime import date as _date
        sys.path.insert(0, str(Path(__file__).parent))
        from ihg_db import IHGDatabase
    except Exception as e:
        print(f"[!] 无法导入 ihg_db: {e}")
        return

    db = IHGDatabase()
    print(f"[fix-country] 数据库: {db.db_path}")

    # 先 dry-run 统计将要影响的酒店
    placeholders_us = ",".join(["?"] * len(US_STATES))
    placeholders_ca = ",".join(["?"] * len(CA_PROVINCES))

    us_hotels = db.conn.execute(
        f"SELECT mnemonic, name, country FROM hotels WHERE country IN ({placeholders_us})",
        tuple(US_STATES)
    ).fetchall()
    ca_hotels = db.conn.execute(
        f"SELECT mnemonic, name, country FROM hotels WHERE country IN ({placeholders_ca})",
        tuple(CA_PROVINCES)
    ).fetchall()

    print(f"[fix-country] 待修复:")
    print(f"  美国 (按州存的): {len(us_hotels)} 个")
    print(f"  加拿大 (按省存的): {len(ca_hotels)} 个")

    if not us_hotels and not ca_hotels:
        print("[fix-country] 无需修复, 数据库已干净 ✓")
        db.close()
        return

    # 按州/省维度展示分布
    from collections import Counter
    if us_hotels:
        c = Counter(r["country"] for r in us_hotels)
        print(f"\n  美国分州分布 (top 10):")
        for st, n in c.most_common(10):
            print(f"    {st:30s} {n}")
    if ca_hotels:
        c = Counter(r["country"] for r in ca_hotels)
        print(f"\n  加拿大分省分布:")
        for pv, n in c.most_common():
            print(f"    {pv:30s} {n}")

    # 真改
    today = _date.today().isoformat()
    cur1 = db.conn.execute(
        f"UPDATE hotels SET country='United States', updated_at=? WHERE country IN ({placeholders_us})",
        (today, *US_STATES)
    )
    cur2 = db.conn.execute(
        f"UPDATE hotels SET country='Canada', updated_at=? WHERE country IN ({placeholders_ca})",
        (today, *CA_PROVINCES)
    )
    db.conn.commit()

    print(f"\n[fix-country] 已更新:")
    print(f"  → country='United States' : {cur1.rowcount} 行")
    print(f"  → country='Canada'        : {cur2.rowcount} 行")

    # 复核
    final_us = db.conn.execute(
        "SELECT COUNT(*) c FROM hotels WHERE country='United States'"
    ).fetchone()["c"]
    final_ca = db.conn.execute(
        "SELECT COUNT(*) c FROM hotels WHERE country='Canada'"
    ).fetchone()["c"]
    print(f"\n[fix-country] 修复后总数: 美国={final_us}, 加拿大={final_ca} ✓")

    db.close()


# ============ 页面操作 ============

async def collect_hotels_from_page(page, delay=3.0, country=""):
    """在当前页面: 滚动 + View More → 提取所有酒店卡片信息"""
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

    # 提取酒店卡片: 精确选择器 (已验证的真实 DOM)
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
            hotels[mn] = {
                "mnemonic": mn,
                "name": hl["name"].split("\n")[0].strip()[:80],
                "url": hl["href"],
                "brand_code": extract_brand(hl["href"]),
                "city": extract_city(hl["href"]),
                "country": country,
                "address": hl.get("address", ""),
                "rating": hl.get("rating", ""),
                "review_count": hl.get("reviewCount", ""),
            }
    return hotels, view_more_clicks


async def collect_sub_region_links(page, current_url):
    """收集 'Hotels by State/Region' 区块内的子区域链接"""
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


# ============ 主逻辑 ============

async def main():
    parser = argparse.ArgumentParser(description="IHG 酒店列表抓取 - 按国家/地区收集")
    parser.add_argument("--target", type=str, default=None,
                        help='目标国家/地区, 多个用逗号分隔 (如 "Vietnam Hotels,Hong Kong SAR Hotels"); '
                             '不指定时抓 --region 下所有二级链接 (整个大区域)')
    parser.add_argument("--region", type=str, default="Asia",
                        help='大区域关键词 (默认 Asia, 可选 Europe/Middle East/Africa/US & Canada 等)')
    parser.add_argument("--output", type=str, default=DEFAULT_OUTPUT,
                        help=f'输出 CSV 文件名 (默认 {DEFAULT_OUTPUT})')
    parser.add_argument("--delay", type=float, default=3.0,
                        help='View More 点击后等待秒数 (默认 3)')
    parser.add_argument("--fix-country", action="store_true",
                        help='仅运行数据库迁移: 把 hotels.country 中误存的 美国州/加拿大省 名'
                             '回写为 "United States" / "Canada", 然后退出 (不抓取)')
    args = parser.parse_args()

    # 数据迁移模式: 修复历史误记录的 country 字段, 不进入抓取流程
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

    # 增量: 加载已有数据
    all_hotels = load_existing_hotels(args.output)
    initial_count = len(all_hotels)

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
            # Step 1: 访问 /explore
            print(f"\n[1/4] 访问 {EXPLORE_URL}")
            await page.goto(EXPLORE_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)

            # 关 cookie
            try:
                btn = page.get_by_role("button", name="Accept All", exact=False)
                if await btn.count() > 0:
                    await btn.first.click(timeout=2000)
                    await page.wait_for_timeout(500)
            except Exception:
                pass

            # 滚动到底部
            for _ in range(8):
                await page.evaluate("window.scrollBy(0, window.innerHeight)")
                await page.wait_for_timeout(600)
            await page.wait_for_timeout(2000)

            # Step 2: 展开大区域
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

            # Step 3: 收集二级链接
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

            # Step 4: 决定要抓的目标列表
            if targets is None:
                # 未指定 --target → 抓整个大区域所有二级链接
                target_links_to_run = region_links
                print(f"[3/4] 未指定 --target, 抓整个 '{region_keyword}' 区域下全部 {len(target_links_to_run)} 个目标...")
            else:
                # 指定了 --target → 在 region_links 里查找匹配项
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
                        print(f"    可用选项 ({len(region_links)} 个), 请用 --target 选择其中一个文本:")
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
                target_country = resolve_country(target_link["text"])
                print(f"\n    === {target_link['text']}  → country='{target_country}' ===")

                if not await goto_with_retry(page, target_url):
                    continue
                await page.wait_for_timeout(2000)

                # 主页面收集
                main_hotels, vm = await collect_hotels_from_page(page, args.delay, target_country)
                new_count = 0
                for mn, info in main_hotels.items():
                    if mn not in all_hotels:
                        all_hotels[mn] = info
                        new_count += 1
                print(f"    主页面: {len(main_hotels)} 个酒店, {new_count} 个新增 (VM: {vm})")

                # 子区域递归
                sub_links = await collect_sub_region_links(page, target_url)
                if sub_links:
                    print(f"    子区域: {len(sub_links)} 个")
                    for idx, lk in enumerate(sub_links, 1):
                        print(f"      [{idx}/{len(sub_links)}] {lk['text']}")
                        if not await goto_with_retry(page, lk["href"]):
                            continue
                        await page.wait_for_timeout(2000)

                        sub_hotels, sub_vm = await collect_hotels_from_page(page, args.delay, target_country)
                        sub_new = 0
                        for mn, info in sub_hotels.items():
                            if mn not in all_hotels:
                                all_hotels[mn] = info
                                sub_new += 1
                        if sub_new > 0:
                            print(f"        +{sub_new} 新酒店 (累计: {len(all_hotels)})")

                print(f"    [{target_country}] 完成, 累计: {len(all_hotels)}")

        except KeyboardInterrupt:
            print("\n[!] 用户中断, 保存已收集数据...")
        except Exception as e:
            print(f"\n[!] 异常: {e}")
            import traceback
            traceback.print_exc()
        finally:
            await context.close()

    # Step 5: 保存结果
    hotels_list = list(all_hotels.values())
    new_total = len(hotels_list) - initial_count

    print(f"\n[4/4] 保存结果...")
    print(f"    本次新增: {new_total} 个酒店")
    print(f"    总计: {len(hotels_list)} 个唯一酒店")

    if hotels_list:
        save_results(hotels_list, args.output)

        # 按国家统计
        country_stats = {}
        for h in hotels_list:
            c = h.get("country", "未知")
            country_stats[c] = country_stats.get(c, 0) + 1
        print(f"\n    国家分布:")
        for c, n in sorted(country_stats.items(), key=lambda x: -x[1]):
            print(f"      {c}: {n}")
    else:
        print("    [!] 无数据")


if __name__ == "__main__":
    asyncio.run(main())
