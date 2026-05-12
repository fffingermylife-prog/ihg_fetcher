"""
IHG Hotel List Fetcher - BFS 策略抓取全球酒店目录

工作流:
    https://www.ihg.com/explore
    ↓ 提取所有 "目录" 类链接 (explore/xxx, destinations/xx, hotels/xx/en/...)
    ↓ BFS 广度遍历每个目录页面
    ↓ 在每个页面中同时:
        - 提取酒店卡片链接 (含 MNEMONIC) → 存入结果
        - 提取新的目录链接 → 加入队列
    ↓ 输出 ihg_hotels.csv

关键设计:
- 用 BFS 替代"点击展开", 更稳定
- 所有发现的 URL 都过一遍分类器 (目录 vs 酒店卡片)
- 同 mnemonic 自动去重
- 深度限制 + 页面数限制, 避免无限爬取
- 持久化 profile, 反爬

依赖:
    pip install playwright
    playwright install chromium

使用:
    python ihg_hotel_list_fetcher.py
    python ihg_hotel_list_fetcher.py --max-pages 200
    python ihg_hotel_list_fetcher.py --brands IC,HI
    python ihg_hotel_list_fetcher.py --limit 100 --headless false  # 调试
"""

import argparse
import asyncio
import csv
import json
import re
import sys
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse, urlunparse

from playwright.async_api import async_playwright, BrowserContext, Page


# ============ 配置 ============

# 入口 URL 列表 - 包含 /explore, 每个国家的 hotel-directory, 各品牌的 destinations
START_URLS = [
    "https://www.ihg.com/explore",
    # 各国 hotel-directory (IHG 各主要市场)
    "https://www.ihg.com/hotels/us/en/hotel-directory",
    "https://www.ihg.com/hotels/gb/en/hotel-directory",
    "https://www.ihg.com/hotels/cn/en/hotel-directory",
    "https://www.ihg.com/hotels/jp/en/hotel-directory",
    "https://www.ihg.com/hotels/th/en/hotel-directory",
    "https://www.ihg.com/hotels/au/en/hotel-directory",
    "https://www.ihg.com/hotels/in/en/hotel-directory",
    "https://www.ihg.com/hotels/de/en/hotel-directory",
    "https://www.ihg.com/hotels/fr/en/hotel-directory",
    "https://www.ihg.com/hotels/kr/en/hotel-directory",
    "https://www.ihg.com/hotels/mx/en/hotel-directory",
    "https://www.ihg.com/hotels/br/en/hotel-directory",
    "https://www.ihg.com/hotels/ca/en/hotel-directory",
    "https://www.ihg.com/hotels/ae/en/hotel-directory",
    "https://www.ihg.com/hotels/eg/en/hotel-directory",
    "https://www.ihg.com/hotels/za/en/hotel-directory",
    "https://www.ihg.com/hotels/sg/en/hotel-directory",
    "https://www.ihg.com/hotels/my/en/hotel-directory",
    "https://www.ihg.com/hotels/id/en/hotel-directory",
    "https://www.ihg.com/hotels/ph/en/hotel-directory",
    "https://www.ihg.com/hotels/vn/en/hotel-directory",
    "https://www.ihg.com/hotels/nz/en/hotel-directory",
    "https://www.ihg.com/hotels/hk/en/hotel-directory",
    "https://www.ihg.com/hotels/tw/en/hotel-directory",
    "https://www.ihg.com/hotels/es/en/hotel-directory",
    "https://www.ihg.com/hotels/it/en/hotel-directory",
    "https://www.ihg.com/hotels/nl/en/hotel-directory",
    "https://www.ihg.com/hotels/tr/en/hotel-directory",
    "https://www.ihg.com/hotels/sa/en/hotel-directory",
    "https://www.ihg.com/hotels/qa/en/hotel-directory",
    # 各品牌的 destinations 入口
    "https://www.ihg.com/intercontinental/destinations/us/en/explore",
    "https://www.ihg.com/intercontinental/destinations/gb/en/explore",
    "https://www.ihg.com/holidayinn/destinations/us/en/explore",
    "https://www.ihg.com/holidayinnexpress/destinations/us/en/explore",
    "https://www.ihg.com/crowneplaza/destinations/us/en/explore",
    "https://www.ihg.com/hotelindigo/destinations/us/en/explore",
    "https://www.ihg.com/kimptonhotels/destinations/us/en/explore",
    "https://www.ihg.com/staybridge/destinations/us/en/explore",
    "https://www.ihg.com/candlewood/destinations/us/en/explore",
    "https://www.ihg.com/voco/destinations/us/en/explore",
    "https://www.ihg.com/garner-hotels/destinations/us/en/explore",
    "https://www.ihg.com/regent/destinations/us/en/explore",
]

OUTPUT_CSV = "ihg_hotels.csv"
OUTPUT_JSON = "ihg_hotels.json"

HEADLESS = True
USER_DATA_DIR = "./ihg_browser_profile"

# BFS 控制
MAX_PAGES = 500                  # 最多访问多少页
MAX_DEPTH = 5                    # BFS 最大深度
PAGE_LOAD_TIMEOUT_MS = 30000
DELAY_MS = 400                   # 每页间隔
SCROLL_PASSES = 3                # 每页滚动次数 (触发懒加载)

# 只要英文页面, 避免本地化重复 (ar, de, fr, es 等的主入口等同于 en)
ENGLISH_ONLY = True

# 连续 N 页没有新酒店时, 停止访问同一"分支"的类似页面
DEAD_END_THRESHOLD = 10

# ============ 配置结束 ============


# 品牌映射
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


# ============ URL 分类器 ============

def normalize_url(url: str) -> str:
    """URL 规范化 (去 fragment + 查询, 统一小写 host)"""
    try:
        p = urlparse(url)
        return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path.rstrip('/'), '', '', ''))
    except Exception:
        return url


def is_ihg_url(url: str) -> bool:
    """是否是 ihg.com 主域名的 URL"""
    try:
        host = urlparse(url).netloc.lower()
        return host.endswith("ihg.com") or host.endswith(".ihg.com") or host == "ihg.com"
    except Exception:
        return False


def is_directory_url(url: str) -> bool:
    """
    判断是否是 "目录页" URL (可能包含更多酒店链接)
    匹配:
        /explore
        /explore/<region>            e.g. /explore/europe
        /hotels/<region>/en/destinations/...
        /<brand>/destinations/<region>/en/explore
        /content/<region>/en/destinations
    """
    url_lower = url.lower()
    # 必须是 ihg.com
    if not is_ihg_url(url):
        return False
    # 排除酒店详情页 (避免误判)
    if "/hoteldetail" in url_lower or "/hotel-detail" in url_lower:
        return False
    # 排除明显的非目录路径
    if any(x in url_lower for x in [
        "/reservation", "/checkout", "/account", "/signin", "/login",
        "/customercare", "/legal", "/privacy", "/rewards-club/",
        "/content/us/en/about/", ".pdf", ".jpg", ".png",
    ]):
        return False

    # ENGLISH_ONLY: 过滤非英文本地化 URL
    # IHG URL 模式: /<lang>/explore 或 /hotels/<country>/<lang>/...
    if ENGLISH_ONLY:
        # /<2字母>/explore 形式 (如 /ar/explore, /de/explore, /fr/explore)
        if re.match(r'^https?://[^/]+/[a-z]{2}/explore(/|$)', url_lower):
            # 只允许 /en/explore 或没有语言前缀的 /explore
            if not re.match(r'^https?://[^/]+/en/explore', url_lower):
                return False
        # /hotels/<country>/<lang>/... 形式, 只允许 lang=en
        m = re.match(r'^https?://[^/]+/(?:[a-z-]+/)?hotels/[a-z]{2}/([a-z]{2})/', url_lower)
        if m and m.group(1) != "en":
            return False
        # /<brand>/destinations/<country>/<lang>/... 形式, 只允许 lang=en
        m = re.match(r'^https?://[^/]+/[a-z-]+/destinations/[a-z]{2}/([a-z]{2})/', url_lower)
        if m and m.group(1) != "en":
            return False
        # /<brand>/content/<country>/<lang>/... 形式, 只允许 lang=en
        m = re.match(r'^https?://[^/]+/[a-z-]+/content/[a-z]{2}/([a-z]{2})/', url_lower)
        if m and m.group(1) != "en":
            return False

    # 排除营销页, 但允许各品牌的 /explore-hotels 目录
    if "/explore/" in url_lower and "/explore-hotels" not in url_lower:
        # /explore/<region> 是营销页, 内容重复不含酒店链接
        # 只保留 /explore 本身作为入口
        marketing_patterns = [
            "/explore/europe", "/explore/asia", "/explore/americas",
            "/explore/middle-east", "/explore/africa", "/explore/oceania",
            "/explore/caribbean", "/explore/new-hotels", "/explore/us",
            "/explore/uk", "/explore/all-inclusive",
        ]
        for mp in marketing_patterns:
            if mp in url_lower:
                return False

    patterns = [
        r"/explore(/|$|\?)",
        r"/destinations(/|$)",
        r"/destination(/|$)",
        r"/hotel-directory",
        r"/explore-hotels",
        r"/find-hotels",
        r"/locations(/|$)",
    ]
    for pat in patterns:
        if re.search(pat, url_lower):
            return True
    return False


def is_hotel_detail_url(url: str) -> bool:
    """是否是酒店详情页 URL (可提取 mnemonic)"""
    return extract_mnemonic_from_url(url) is not None


def extract_mnemonic_from_url(url: str) -> Optional[str]:
    """
    从 URL 中提取 hotelMnemonic (通常 5 字母代码)
    常见模式:
        /hotels/<region>/<lang>/<city>/<MNEMONIC>/hoteldetail
        /hotels/<region>/<lang>/<city>/<MNEMONIC>/index
        /<brand>/hotels/<region>/<lang>/<city>/<MNEMONIC>/...
        ?qSlH=<MNEMONIC>
    """
    if not url:
        return None

    # 模式1: /hoteldetail 结尾
    m = re.search(r'/hotels/[a-z]{2}/[a-z]{2}/[^/]+/([a-z0-9]{4,6})(?:/hoteldetail|/index|/?$|/\?|$)', url, re.IGNORECASE)
    if m:
        return m.group(1).upper()

    # 模式2: 品牌子路径
    m = re.search(r'/(?:intercontinental|regent|sixsenses|kimpton|hotelindigo|voco|crowneplaza|evenhotels|holidayinnexpress|holidayinnclubvacations|holidayinnresort|holidayinn|garner|avidhotels|atwellsuites|staybridge|candlewood|iberostar|mrandmrssmith|vignettecollection|ruby)/hotels/[a-z]{2}/[a-z]{2}/[^/]+/([a-z0-9]{4,6})(?:/|$|\?)', url, re.IGNORECASE)
    if m:
        return m.group(1).upper()

    # 模式3: qSlH 参数
    m = re.search(r'[?&]qSlH=([a-zA-Z0-9]{4,6})(?:&|$)', url)
    if m:
        return m.group(1).upper()

    return None


def extract_brand_from_url(url: str) -> str:
    """从 URL 路径中推断品牌代码"""
    url_lower = url.lower()
    patterns = {
        "intercontinental": "IC", "regent": "RC", "sixsenses": "SR",
        "kimpton": "KI", "hotelindigo": "HT", "voco": "VC",
        "crowneplaza": "CP", "evenhotels": "EH",
        "holidayinnexpress": "EX", "holidayinnclubvacations": "CV",
        "holidayinnresort": "RS", "holidayinn": "HI",
        "garner": "GE", "avidhotels": "AV", "atwellsuites": "AT",
        "staybridge": "SB", "candlewood": "CW",
        "iberostar": "IS", "mrandmrssmith": "MR",
        "vignettecollection": "VX",
    }
    # 先匹配更长的路径名 (holidayinnexpress 在 holidayinn 前)
    for path_key, brand_code in sorted(patterns.items(), key=lambda x: -len(x[0])):
        if f"/{path_key}/" in url_lower or f".com/{path_key}/" in url_lower:
            return brand_code
    return ""


def extract_country_code_from_url(url: str) -> str:
    """从 URL 提取国家代码"""
    # /hotels/<region>/en/... 的 region 就是国家代码 (us, gb, cn, th...)
    m = re.search(r'/hotels/([a-z]{2})/[a-z]{2}/', url)
    if m:
        return m.group(1).upper()
    # /destinations/<country>/en/...
    m = re.search(r'/destinations/([a-z]{2})/', url)
    if m:
        return m.group(1).upper()
    return ""


def extract_city_from_url(url: str) -> str:
    """从 URL 提取城市 slug"""
    m = re.search(r'/hotels/[a-z]{2}/[a-z]{2}/([^/]+)/[a-z0-9]{4,6}(?:/|$|\?)', url, re.IGNORECASE)
    if m:
        return m.group(1).replace("-", " ").title()
    return ""


# ============ 页面抓取 ============

async def load_page_and_collect_links(page: Page, url: str) -> Tuple[List[Dict], bool]:
    """
    加载页面 + 滚动 + 提取所有链接
    返回: (链接列表 [{href, text}], 是否成功)
    """
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
        await page.wait_for_timeout(DELAY_MS)
    except Exception as e:
        print(f"    [X] 加载失败: {e}")
        return [], False

    # 滚动到底部, 触发懒加载
    try:
        for _ in range(SCROLL_PASSES):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(600)
    except Exception:
        pass

    # 尝试点击可能的 "展开" 按钮 (区域 accordion)
    try:
        await page.evaluate("""
        async () => {
            const wait = (ms) => new Promise(r => setTimeout(r, ms));
            // 点击所有 aria-expanded=false 的元素
            const btns = [...document.querySelectorAll('[aria-expanded="false"]')];
            for (const b of btns.slice(0, 30)) {
                try { b.click(); await wait(100); } catch(e) {}
            }
            await wait(1000);
        }
        """)
    except Exception:
        pass

    # 收集所有链接
    try:
        links = await page.evaluate("""
        () => {
            const arr = [];
            for (const a of document.querySelectorAll('a[href]')) {
                const href = a.href;
                const text = (a.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 200);
                if (href && href.startsWith('http')) {
                    arr.push({ href, text });
                }
            }
            return arr;
        }
        """)
    except Exception:
        links = []

    return links, True


# ============ 主爬虫 (BFS) ============

async def crawl_bfs(
    max_pages: int = MAX_PAGES,
    max_depth: int = MAX_DEPTH,
    brand_filter: Optional[Set[str]] = None,
    limit: Optional[int] = None,
) -> List[Dict]:
    """
    BFS 爬虫主流程:
    - 从 START_URLS 开始
    - 每访问一个目录页, 提取:
        * 酒店卡片 → 加入结果
        * 新的目录链接 → 加入队列
    """
    Path(USER_DATA_DIR).mkdir(parents=True, exist_ok=True)

    hotels: Dict[str, Dict] = {}      # mnemonic → {name, url, brand, country, city}
    visited: Set[str] = set()
    queue: deque = deque()            # (url, depth)

    # 初始化队列
    for u in START_URLS:
        queue.append((u, 0))

    async with async_playwright() as p:
        context: BrowserContext = await p.chromium.launch_persistent_context(
            USER_DATA_DIR,
            headless=HEADLESS,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )

        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        """)

        page = await context.new_page()

        try:
            pages_visited = 0
            while queue and pages_visited < max_pages:
                url, depth = queue.popleft()
                norm = normalize_url(url)

                if norm in visited:
                    continue
                visited.add(norm)
                pages_visited += 1

                print(f"[{pages_visited}/{max_pages}] (d={depth}) {url[:100]}")
                links, ok = await load_page_and_collect_links(page, url)
                if not ok:
                    continue

                new_dir_count = 0
                new_hotel_count = 0

                for link in links:
                    href = link["href"]
                    text = link["text"]
                    href_norm = normalize_url(href)

                    # 是酒店卡片?
                    mnemonic = extract_mnemonic_from_url(href)
                    if mnemonic:
                        if mnemonic not in hotels:
                            hotels[mnemonic] = {
                                "mnemonic": mnemonic,
                                "name": text.split("\n")[0].strip()[:100] if text else "",
                                "url": href,
                                "brand_code": extract_brand_from_url(href),
                                "country_code": extract_country_code_from_url(href),
                                "city": extract_city_from_url(href),
                            }
                            new_hotel_count += 1
                        continue

                    # 是目录页? 加入队列
                    if depth < max_depth and is_directory_url(href) and href_norm not in visited:
                        # 限制队列大小, 避免爆炸
                        if len(queue) < max_pages * 3:
                            queue.append((href, depth + 1))
                            new_dir_count += 1

                print(f"    → +{new_hotel_count} 个酒店, +{new_dir_count} 个目录链接 | 累计酒店: {len(hotels)}")

                # 达到酒店数量上限, 提前结束
                if limit and len(hotels) >= limit:
                    print(f"[*] 已达到酒店上限 {limit}, 提前结束")
                    break

        finally:
            await context.close()

    # 转为列表
    all_hotels = list(hotels.values())

    # 品牌过滤
    if brand_filter:
        all_hotels = [h for h in all_hotels if h.get("brand_code") in brand_filter]
        print(f"[*] 品牌过滤后: {len(all_hotels)} 个")

    # 截断
    if limit and len(all_hotels) > limit:
        all_hotels = all_hotels[:limit]

    return all_hotels


# ============ 输出 ============

def export_csv(hotels: List[Dict], filename: str):
    if not hotels:
        print("[-] 没有数据可导出")
        return

    fieldnames = [
        "mnemonic", "name", "brand_code", "brand_name",
        "country_code", "city", "url"
    ]

    with open(filename, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for h in hotels:
            h["brand_name"] = BRAND_MAP.get(h.get("brand_code", ""), h.get("brand_code", ""))
            w.writerow(h)

    print(f"[+] 已导出 {len(hotels)} 个酒店到: {filename}")


def export_json(hotels: List[Dict], filename: str):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(hotels, f, indent=2, ensure_ascii=False)
    print(f"[+] 已导出到: {filename}")


def main():
    parser = argparse.ArgumentParser(description="IHG 酒店列表 BFS 爬虫")
    parser.add_argument("--brands", help="品牌过滤 (逗号分隔) 如 IC,HI,CP")
    parser.add_argument("--limit", type=int, help="最多保留多少酒店")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES,
                        help=f"最多访问多少页面 (默认 {MAX_PAGES})")
    parser.add_argument("--max-depth", type=int, default=MAX_DEPTH,
                        help=f"BFS 最大深度 (默认 {MAX_DEPTH})")
    parser.add_argument("--output", default=OUTPUT_CSV, help=f"输出 CSV (默认 {OUTPUT_CSV})")
    parser.add_argument("--headless", default="true", choices=["true", "false"],
                        help="是否无头模式 (调试时用 false)")
    args = parser.parse_args()

    global HEADLESS
    HEADLESS = (args.headless == "true")

    brand_filter = None
    if args.brands:
        brand_filter = {b.strip().upper() for b in args.brands.split(",") if b.strip()}
        print(f"[*] 品牌过滤: {brand_filter}")

    print("=" * 60)
    print("IHG Hotel List Fetcher (BFS)")
    print(f"入口: {', '.join(START_URLS)}")
    print(f"最大页数: {args.max_pages} | 最大深度: {args.max_depth}")
    print(f"Headless: {HEADLESS}")
    print("=" * 60)

    try:
        hotels = asyncio.run(crawl_bfs(
            max_pages=args.max_pages,
            max_depth=args.max_depth,
            brand_filter=brand_filter,
            limit=args.limit,
        ))
    except KeyboardInterrupt:
        print("\n用户中断")
        sys.exit(130)
    except Exception as e:
        print(f"\n[!] 运行失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    # 输出
    print("\n" + "=" * 60)
    print(f"抓取完成! 共 {len(hotels)} 个唯一酒店")
    print("=" * 60)

    if hotels:
        # 按国家统计
        country_counts = {}
        brand_counts = {}
        for h in hotels:
            cc = h.get("country_code", "") or "N/A"
            bc = h.get("brand_code", "") or "N/A"
            country_counts[cc] = country_counts.get(cc, 0) + 1
            brand_counts[bc] = brand_counts.get(bc, 0) + 1

        print(f"\n国家分布 (Top 10):")
        for cc, cnt in sorted(country_counts.items(), key=lambda x: -x[1])[:10]:
            print(f"  {cc}: {cnt}")

        print(f"\n品牌分布:")
        for bc, cnt in sorted(brand_counts.items(), key=lambda x: -x[1]):
            bn = BRAND_MAP.get(bc, bc)
            print(f"  {bc} ({bn}): {cnt}")

    export_csv(hotels, args.output)
    export_json(hotels, args.output.replace(".csv", ".json"))

    print(f"\n下一步: python ihg_playwright_fetcher.py --hotels-csv {args.output}")


if __name__ == "__main__":
    main()
