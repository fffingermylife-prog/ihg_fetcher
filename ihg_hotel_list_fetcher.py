"""
IHG Hotel List Fetcher - 只从 /explore 页面抓取所有酒店

策略:
    1. 访问 https://www.ihg.com/explore
    2. 接受 cookie, 滚动触发懒加载
    3. 用 Playwright 的 get_by_role/get_by_text 按【文本】点击展开每个区域
    4. 展开后从 DOM 提取所有酒店链接 (含 MNEMONIC)
    5. 同时收集页面上的 /explore/xxx 子目录链接, 递归访问
    6. 输出 ihg_hotels.csv

关键设计:
- 不猜 CSS 选择器, 用 Playwright 原生 locator 按文本定位
- --debug 模式: 打印页面结构到 JSON, 帮助精准诊断
- 只爬 /explore 系列 URL, 不爬不存在的 hotel-directory

使用:
    python ihg_hotel_list_fetcher.py                             # 正常跑
    python ihg_hotel_list_fetcher.py --debug --headless false    # 调试
    python ihg_hotel_list_fetcher.py --no-bfs                    # 只抓主 /explore
    python ihg_hotel_list_fetcher.py --brands IC,HI              # 品牌过滤
"""

import argparse
import asyncio
import csv
import json
import re
import sys
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional, Set
from urllib.parse import urlparse, urlunparse

from playwright.async_api import async_playwright, BrowserContext, Page


# ============ 配置 ============

EXPLORE_URL = "https://www.ihg.com/explore"

OUTPUT_CSV = "ihg_hotels.csv"
OUTPUT_JSON = "ihg_hotels.json"
DEBUG_DUMP = "ihg_page_structure.json"

HEADLESS = True
USER_DATA_DIR = "./ihg_browser_profile"

MAX_PAGES = 200
MAX_DEPTH = 3
PAGE_LOAD_TIMEOUT_MS = 30000
DELAY_MS = 600
SCROLL_PASSES = 5

# IHG 区域名称 (Playwright 会尝试所有这些文本)
REGION_NAMES = [
    "US & Canada", "US and Canada", "United States & Canada", "North America",
    "Caribbean",
    "Mexico & Central America", "Mexico and Central America", "Mexico, Central America",
    "South America",
    "Europe",
    "Middle East",
    "Africa",
    "Asia", "Asia Pacific", "Asia-Pacific", "Southeast Asia",
    "Australia & Pacific", "Australia and Pacific", "Pacific", "Oceania",
    "Greater China", "China",
    "Japan", "Korea", "United Kingdom",
]

# ============ 配置结束 ============


BRAND_MAP = {
    "SR": "Six Senses", "RC": "Regent", "IC": "InterContinental",
    "VX": "Vignette Collection", "KI": "Kimpton", "HT": "Hotel Indigo",
    "VC": "voco", "CP": "Crowne Plaza", "EH": "EVEN Hotels",
    "HI": "Holiday Inn", "RS": "Holiday Inn Resort",
    "CV": "Holiday Inn Club Vacations", "EX": "Holiday Inn Express",
    "GE": "Garner", "AV": "avid hotels", "AT": "Atwell Suites",
    "SB": "Staybridge Suites", "CW": "Candlewood Suites",
    "IS": "Iberostar", "MR": "Mr. & Mrs. Smith", "RU": "Ruby Hotels",
}


# ============ URL 工具 ============

def normalize_url(url: str) -> str:
    try:
        p = urlparse(url)
        return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path.rstrip('/'), '', '', ''))
    except Exception:
        return url


def is_ihg_url(url: str) -> bool:
    try:
        return urlparse(url).netloc.lower().endswith("ihg.com")
    except Exception:
        return False


def extract_mnemonic_from_url(url: str) -> Optional[str]:
    """从 URL 提取酒店 mnemonic (4-6位字母数字)"""
    if not url:
        return None

    # qSlH 参数 (最可靠)
    m = re.search(r'[?&]qSlH=([a-zA-Z0-9]{4,6})(?:&|$)', url)
    if m:
        return m.group(1).upper()

    # /hotels/<country>/<lang>/<city>/<MNEMONIC>/hoteldetail 或 /index
    m = re.search(r'/hotels/[a-z]{2}/[a-z]{2}/[^/]+/([a-zA-Z0-9]{4,6})(?:/hoteldetail|/index|/?$|/\?|$)', url)
    if m:
        return m.group(1).upper()

    # 品牌子路径
    m = re.search(
        r'/(?:intercontinental|regent|sixsenses|kimpton|hotelindigo|voco|crowneplaza|evenhotels|holidayinnexpress|holidayinnclubvacations|holidayinnresort|holidayinn|garner|garner-hotels|avidhotels|atwellsuites|staybridge|candlewood|iberostar|mrandmrssmith|vignettecollection|ruby|kimptonhotels)/hotels/[a-z]{2}/[a-z]{2}/[^/]+/([a-zA-Z0-9]{4,6})(?:/|$|\?)',
        url, re.IGNORECASE
    )
    if m:
        return m.group(1).upper()

    return None


def is_explore_url(url: str) -> bool:
    """判断是否是 /explore 类目录 URL"""
    if not is_ihg_url(url):
        return False
    url_lower = url.lower()

    # 排除非英文本地化
    if re.match(r'^https?://[^/]+/[a-z]{2}/explore(/|$)', url_lower):
        if not re.match(r'^https?://[^/]+/en/explore', url_lower):
            return False

    if any(x in url_lower for x in [
        "/reservation", "/checkout", "/account", "/signin",
        "/legal", ".pdf", ".jpg", "/customer-care",
    ]):
        return False

    # 排除明显的营销文章
    if any(x in url_lower for x in [
        "/explore/new-hotels", "/explore/all-inclusive",
    ]):
        return False

    return "/explore" in url_lower or "/destinations" in url_lower


def extract_brand_from_url(url: str) -> str:
    url_lower = url.lower()
    patterns = {
        "intercontinental": "IC", "regent": "RC", "sixsenses": "SR",
        "kimptonhotels": "KI", "kimpton": "KI",
        "hotelindigo": "HT", "voco": "VC",
        "crowneplaza": "CP", "evenhotels": "EH",
        "holidayinnexpress": "EX", "holidayinnclubvacations": "CV",
        "holidayinnresort": "RS", "holidayinn": "HI",
        "garner-hotels": "GE", "garner": "GE",
        "avidhotels": "AV", "atwellsuites": "AT",
        "staybridge": "SB", "candlewood": "CW",
        "iberostar": "IS", "mrandmrssmith": "MR",
        "vignettecollection": "VX",
    }
    for path_key, brand_code in sorted(patterns.items(), key=lambda x: -len(x[0])):
        if f"/{path_key}/" in url_lower:
            return brand_code
    return ""


def extract_country_code_from_url(url: str) -> str:
    m = re.search(r'/hotels/([a-z]{2})/[a-z]{2}/', url)
    return m.group(1).upper() if m else ""


def extract_city_from_url(url: str) -> str:
    m = re.search(r'/hotels/[a-z]{2}/[a-z]{2}/([^/]+)/[a-zA-Z0-9]{4,6}(?:/|$|\?)', url)
    return m.group(1).replace("-", " ").title() if m else ""


# ============ Playwright 操作 ============

async def dismiss_cookies(page: Page):
    """关闭 cookie 横幅"""
    for txt in ["Accept All", "Accept", "I Accept", "Got it", "同意", "Allow All"]:
        try:
            btn = page.get_by_role("button", name=txt, exact=False)
            if await btn.count() > 0:
                await btn.first.click(timeout=2000)
                await page.wait_for_timeout(500)
                print(f"    ✓ 关闭 cookie ({txt})")
                return
        except Exception:
            continue


async def click_all_regions(page: Page) -> int:
    """按文本点击展开所有已知区域"""
    clicked = 0
    tried = set()

    for region in REGION_NAMES:
        key = region.lower()
        if key in tried:
            continue
        tried.add(key)

        # 按优先级尝试不同 locator
        strategies = [
            ("button-exact", page.get_by_role("button", name=region, exact=True)),
            ("button-contains", page.get_by_role("button", name=region, exact=False)),
            ("tab", page.get_by_role("tab", name=region, exact=False)),
            ("heading-click", page.get_by_role("heading", name=region, exact=True)),
            ("text-exact", page.get_by_text(region, exact=True)),
        ]

        for strategy_name, locator in strategies:
            try:
                if await locator.count() == 0:
                    continue
                try:
                    await locator.first.scroll_into_view_if_needed(timeout=3000)
                    await page.wait_for_timeout(300)
                    await locator.first.click(timeout=3000)
                    print(f"    ✓ 展开 [{region}] (via {strategy_name})")
                    clicked += 1
                    await page.wait_for_timeout(1200)
                    break
                except Exception:
                    try:
                        await locator.first.click(timeout=2000, force=True)
                        print(f"    ✓ 展开 [{region}] (forced)")
                        clicked += 1
                        await page.wait_for_timeout(1200)
                        break
                    except Exception:
                        continue
            except Exception:
                continue

    # 兜底: 点击所有 aria-expanded=false 的元素
    if clicked < 3:
        try:
            num = await page.evaluate("""
            async () => {
                const wait = (ms) => new Promise(r => setTimeout(r, ms));
                let count = 0;
                const btns = [...document.querySelectorAll('[aria-expanded="false"]')];
                for (const b of btns.slice(0, 40)) {
                    try {
                        b.scrollIntoView({behavior: 'instant', block: 'center'});
                        b.click();
                        count++;
                        await wait(300);
                    } catch(e) {}
                }
                await wait(1500);
                return count;
            }
            """)
            if num > 0:
                print(f"    ✓ 点击了 {num} 个 aria-expanded=false 元素")
                clicked += num
        except Exception:
            pass

    return clicked


async def collect_links(page: Page) -> List[Dict]:
    """收集当前 DOM 所有 a[href] 链接"""
    try:
        return await page.evaluate("""
        () => {
            const arr = [];
            for (const a of document.querySelectorAll('a[href]')) {
                const href = a.href;
                const text = (a.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 200);
                if (href && href.startsWith('http')) arr.push({href, text});
            }
            return arr;
        }
        """)
    except Exception:
        return []


async def dump_page_structure(page: Page) -> Dict:
    """调试: 导出页面结构, 帮助定位按钮"""
    return await page.evaluate("""
    () => {
        const keywords = ['US & Canada', 'Caribbean', 'Mexico', 'Europe',
            'Middle East', 'Africa', 'Asia', 'South America', 'Pacific',
            'China', 'Japan', 'Oceania', 'North America'];
        const out = {buttons: [], tabs: [], ariaExpanded: [], textMatches: [], headings: []};

        for (const el of document.querySelectorAll('button')) {
            const t = (el.textContent || '').trim().slice(0, 60);
            if (t && t.length < 80) {
                out.buttons.push({text: t, class: (el.className || '').slice(0, 80)});
            }
        }
        for (const el of document.querySelectorAll('[role="tab"]')) {
            out.tabs.push({text: (el.textContent || '').trim().slice(0, 60),
                           class: (el.className || '').slice(0, 80)});
        }
        for (const el of document.querySelectorAll('[aria-expanded]')) {
            out.ariaExpanded.push({
                tag: el.tagName, text: (el.textContent || '').trim().slice(0, 60),
                expanded: el.getAttribute('aria-expanded'),
                class: (el.className || '').slice(0, 80),
            });
        }
        for (const el of document.querySelectorAll('*')) {
            const t = (el.textContent || '').trim();
            if (!t || t.length > 60) continue;
            if (el.children && el.children.length > 2) continue;
            for (const kw of keywords) {
                if (t === kw || (t.startsWith(kw) && t.length < kw.length + 8)) {
                    out.textMatches.push({
                        tag: el.tagName, text: t.slice(0, 60),
                        class: (el.className || '').slice(0, 80),
                        parent: el.parentElement?.tagName,
                    });
                    break;
                }
            }
        }
        for (const el of document.querySelectorAll('h1,h2,h3,h4')) {
            out.headings.push({tag: el.tagName, text: (el.textContent || '').trim().slice(0, 80)});
        }
        out.buttons = out.buttons.slice(0, 40);
        out.tabs = out.tabs.slice(0, 30);
        out.ariaExpanded = out.ariaExpanded.slice(0, 40);
        out.textMatches = out.textMatches.slice(0, 30);
        out.headings = out.headings.slice(0, 30);
        return out;
    }
    """)


async def extract_hotels(page: Page) -> List[Dict]:
    """从页面提取酒店"""
    links = await collect_links(page)
    out = []
    seen = set()
    for lk in links:
        mn = extract_mnemonic_from_url(lk["href"])
        if mn and mn not in seen:
            seen.add(mn)
            out.append({
                "mnemonic": mn,
                "name": (lk["text"] or "").split("\n")[0].strip()[:100],
                "url": lk["href"],
                "brand_code": extract_brand_from_url(lk["href"]),
                "country_code": extract_country_code_from_url(lk["href"]),
                "city": extract_city_from_url(lk["href"]),
            })
    return out


# ============ 主流程 ============

async def crawl(debug=False, brand_filter=None, limit=None, do_bfs=True) -> List[Dict]:
    Path(USER_DATA_DIR).mkdir(parents=True, exist_ok=True)
    hotels: Dict[str, Dict] = {}

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            USER_DATA_DIR,
            headless=HEADLESS,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
        """)
        page = await context.new_page()

        try:
            # Stage 1: 主 /explore
            print(f"\n[Stage 1] 访问 {EXPLORE_URL}")
            await page.goto(EXPLORE_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)

            await dismiss_cookies(page)

            print(f"[Stage 1] 滚动触发懒加载...")
            for _ in range(SCROLL_PASSES):
                await page.evaluate("window.scrollBy(0, window.innerHeight)")
                await page.wait_for_timeout(700)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(2000)

            if debug:
                print(f"\n[DEBUG] 分析页面结构...")
                struct = await dump_page_structure(page)
                with open(DEBUG_DUMP, "w", encoding="utf-8") as f:
                    json.dump(struct, f, indent=2, ensure_ascii=False)
                print(f"    → 已保存: {DEBUG_DUMP}")
                print(f"    buttons: {len(struct['buttons'])}, tabs: {len(struct['tabs'])},")
                print(f"    aria-expanded: {len(struct['ariaExpanded'])}, text-matches: {len(struct['textMatches'])}")
                for m in struct["textMatches"][:10]:
                    print(f"      [{m['tag']}] {m['text']}  .{m.get('class','')[:40]}")

            print(f"\n[Stage 1] 点击展开所有区域...")
            clicked = await click_all_regions(page)
            print(f"    共展开 {clicked} 个元素")

            await page.wait_for_timeout(2500)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1500)

            stage1 = await extract_hotels(page)
            for h in stage1:
                hotels.setdefault(h["mnemonic"], h)
            print(f"[Stage 1 完成] +{len(stage1)} 酒店 | 累计 {len(hotels)}")

            # 收集 Stage 2 入队的 URL
            explore_urls = []
            seen_q = {normalize_url(EXPLORE_URL)}
            for lk in await collect_links(page):
                norm = normalize_url(lk["href"])
                if norm in seen_q:
                    continue
                if is_explore_url(lk["href"]):
                    seen_q.add(norm)
                    explore_urls.append(lk["href"])
            print(f"    → 发现 {len(explore_urls)} 个目录类 URL")

            # Stage 2: BFS
            if do_bfs and explore_urls and (not limit or len(hotels) < limit):
                print(f"\n[Stage 2] BFS 遍历...")
                queue = deque((u, 1) for u in explore_urls)
                done = 0
                while queue and done < MAX_PAGES:
                    url, depth = queue.popleft()
                    if depth > MAX_DEPTH:
                        continue
                    done += 1
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
                        await page.wait_for_timeout(DELAY_MS)
                    except Exception as e:
                        print(f"[{done}/{MAX_PAGES}] (d={depth}) {url[:80]}  [X] {e}")
                        continue

                    for _ in range(2):
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        await page.wait_for_timeout(500)

                    await click_all_regions(page)
                    await page.wait_for_timeout(800)

                    page_hotels = await extract_hotels(page)
                    new_h = 0
                    for h in page_hotels:
                        if h["mnemonic"] not in hotels:
                            hotels[h["mnemonic"]] = h
                            new_h += 1

                    new_d = 0
                    for lk in await collect_links(page):
                        norm = normalize_url(lk["href"])
                        if norm not in seen_q and is_explore_url(lk["href"]):
                            seen_q.add(norm)
                            queue.append((lk["href"], depth + 1))
                            new_d += 1

                    print(f"[{done}/{MAX_PAGES}] (d={depth}) {url[:70]} → +{new_h} 酒店, +{new_d} 目录 | 累计 {len(hotels)}")

                    if limit and len(hotels) >= limit:
                        print(f"[*] 达到上限 {limit}")
                        break
        finally:
            await context.close()

    all_hotels = list(hotels.values())
    if brand_filter:
        all_hotels = [h for h in all_hotels if h.get("brand_code") in brand_filter]
        print(f"[*] 品牌过滤后: {len(all_hotels)}")
    if limit:
        all_hotels = all_hotels[:limit]
    return all_hotels


# ============ 输出 ============

def export_csv(hotels: List[Dict], filename: str):
    if not hotels:
        print("[-] 无数据")
        return
    fields = ["mnemonic", "name", "brand_code", "brand_name", "country_code", "city", "url"]
    with open(filename, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for h in hotels:
            h["brand_name"] = BRAND_MAP.get(h.get("brand_code", ""), h.get("brand_code", ""))
            w.writerow(h)
    print(f"[+] CSV 已导出 {len(hotels)} 条: {filename}")


def export_json(hotels: List[Dict], filename: str):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(hotels, f, indent=2, ensure_ascii=False)
    print(f"[+] JSON 已导出: {filename}")


def main():
    parser = argparse.ArgumentParser(description="IHG 酒店列表抓取 (基于 /explore)")
    parser.add_argument("--brands", help="品牌过滤, 如 IC,HI")
    parser.add_argument("--limit", type=int, help="最多保留多少酒店")
    parser.add_argument("--no-bfs", action="store_true", help="只做 Stage 1")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES)
    parser.add_argument("--output", default=OUTPUT_CSV)
    parser.add_argument("--headless", default="true", choices=["true", "false"])
    parser.add_argument("--debug", action="store_true", help="调试: 导出页面结构")
    args = parser.parse_args()

    global HEADLESS, MAX_PAGES
    HEADLESS = (args.headless == "true")
    MAX_PAGES = args.max_pages

    brand_filter = None
    if args.brands:
        brand_filter = {b.strip().upper() for b in args.brands.split(",")}

    print("=" * 60)
    print(f"IHG Hotel List Fetcher | 入口: {EXPLORE_URL}")
    print(f"Headless: {HEADLESS} | Debug: {args.debug} | BFS: {not args.no_bfs}")
    print("=" * 60)

    try:
        hotels = asyncio.run(crawl(
            debug=args.debug,
            brand_filter=brand_filter,
            limit=args.limit,
            do_bfs=not args.no_bfs,
        ))
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        print(f"[!] 失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    print("\n" + "=" * 60)
    print(f"完成! 共 {len(hotels)} 个唯一酒店")
    print("=" * 60)

    if hotels:
        cc, bc = {}, {}
        for h in hotels:
            cc[h.get("country_code") or "?"] = cc.get(h.get("country_code") or "?", 0) + 1
            bc[h.get("brand_code") or "?"] = bc.get(h.get("brand_code") or "?", 0) + 1
        print("\n国家 Top 10:")
        for k, v in sorted(cc.items(), key=lambda x: -x[1])[:10]:
            print(f"  {k}: {v}")
        print("\n品牌分布:")
        for k, v in sorted(bc.items(), key=lambda x: -x[1]):
            print(f"  {k} ({BRAND_MAP.get(k, '未知')}): {v}")

    export_csv(hotels, args.output)
    export_json(hotels, args.output.replace(".csv", ".json"))

    if not hotels:
        print(f"\n[!] 未抓到数据, 请运行诊断:")
        print(f"    python ihg_hotel_list_fetcher.py --debug --headless false --no-bfs")
        print(f"    把 {DEBUG_DUMP} 的内容发给我")
    else:
        print(f"\n下一步: python ihg_playwright_fetcher.py --hotels-csv {args.output}")


if __name__ == "__main__":
    main()
