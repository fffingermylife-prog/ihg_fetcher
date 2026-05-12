"""
IHG Hotel List Fetcher - 从 ihg.com/explore 页面抓取全球酒店目录

策略:
- 访问 https://www.ihg.com/explore
- 页面底部有区域分类 (US & Canada, Europe, Asia, etc.)
- 逐个点击区域按钮展开, 然后从展开的 DOM 中提取所有酒店链接
- 提取: hotelMnemonic, 酒店名称, 品牌代码, 国家, 城市

输出: ihg_hotels.csv

依赖:
    pip install playwright
    playwright install chromium

使用:
    python ihg_hotel_list_fetcher.py
    python ihg_hotel_list_fetcher.py --brands IC,HI
    python ihg_hotel_list_fetcher.py --limit 100
"""

import argparse
import asyncio
import csv
import json
import re
import sys
from pathlib import Path
from typing import List, Dict, Optional, Set

from playwright.async_api import async_playwright, BrowserContext, Page


# ============ 配置 ============

EXPLORE_URL = "https://www.ihg.com/explore"

OUTPUT_CSV = "ihg_hotels.csv"

HEADLESS = True
USER_DATA_DIR = "./ihg_browser_profile"

# ============ 配置结束 ============

# 品牌代码 → 品牌名映射
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
        "iberostar": "IS", "mrandmrssmith": "MR", "vignettecollection": "VX",
    }
    for path_key, brand_code in sorted(patterns.items(), key=lambda x: -len(x[0])):
        if f"/{path_key}/" in url_lower or f".ihg.com/{path_key}" in url_lower:
            return brand_code
    return ""


def extract_mnemonic_from_url(url: str) -> Optional[str]:
    """从酒店 URL 中提取 hotelMnemonic (5字母代码)"""
    # 模式1: /hotels/us/en/city-name/XXXXX/hoteldetail
    m = re.search(r'/hotels/[^/]+/[^/]+/[^/]+/([a-zA-Z0-9]{4,6})(?:/|$|\?)', url)
    if m:
        return m.group(1).upper()
    # 模式2: URL 参数 qSlH=XXXXX
    m = re.search(r'qSlH=([a-zA-Z0-9]{4,6})', url)
    if m:
        return m.group(1).upper()
    return None


async def crawl_explore_page(
    brand_filter: Optional[Set[str]] = None,
    limit: Optional[int] = None,
) -> List[Dict]:
    """
    主爬虫:
    1. 打开 /explore
    2. 找到底部区域列表, 逐个点击展开
    3. 从展开的内容中提取酒店链接
    """
    all_hotels: List[Dict] = []
    Path(USER_DATA_DIR).mkdir(parents=True, exist_ok=True)

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
        """)

        page = await context.new_page()

        try:
            # Step 1: 访问 explore 页面
            print(f"[1/3] 访问 {EXPLORE_URL}...")
            await page.goto(EXPLORE_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(5000)

            # Step 2: 滚动到底部, 确保区域列表加载
            print(f"[2/3] 滚动到页面底部, 寻找区域列表...")
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(3000)

            # Step 3: 找到并点击所有区域按钮, 展开酒店列表
            print(f"[3/3] 展开所有区域并提取酒店...")

            hotels_data = await page.evaluate("""
            async () => {
                // 等待函数
                const wait = (ms) => new Promise(r => setTimeout(r, ms));

                // 策略1: 找到区域展开按钮并点击
                // 通常这些是 button, accordion, 或带 aria-expanded 的元素
                const expandButtons = [
                    ...document.querySelectorAll('button[aria-expanded="false"]'),
                    ...document.querySelectorAll('[role="tab"]'),
                    ...document.querySelectorAll('[data-toggle]'),
                    ...document.querySelectorAll('.accordion-trigger, .accordion-header, .region-toggle'),
                    ...document.querySelectorAll('h3 button, h4 button, h2 button'),
                ];

                // 也试试包含区域名称文本的可点击元素
                const regionNames = ['US & Canada', 'Caribbean', 'Mexico', 'Central America',
                    'South America', 'Europe', 'Middle East', 'Africa', 'Asia',
                    'Australia', 'Pacific', 'India', 'China', 'Japan', 'Korea'];

                const clickables = document.querySelectorAll('button, [role="button"], [role="tab"], summary, [aria-expanded]');
                for (const el of clickables) {
                    const text = (el.textContent || '').trim();
                    const isRegion = regionNames.some(r => text.includes(r));
                    if (isRegion && !expandButtons.includes(el)) {
                        expandButtons.push(el);
                    }
                }

                console.log('Found expand buttons:', expandButtons.length);

                // 逐个点击展开
                for (const btn of expandButtons) {
                    try {
                        btn.click();
                        await wait(500);
                    } catch(e) {}
                }

                // 额外等待内容渲染
                await wait(3000);

                // 提取所有酒店链接
                const hotels = [];
                const seen = new Set();
                const links = document.querySelectorAll('a[href]');

                for (const a of links) {
                    const href = a.href || '';
                    const text = (a.textContent || '').trim();

                    // 匹配酒店详情页 URL
                    // 模式: /hotels/<region>/<lang>/<city>/<mnemonic>/hoteldetail
                    const m1 = href.match(/\/hotels\/[^\/]+\/[^\/]+\/([^\/]+)\/([a-zA-Z0-9]{4,6})(?:\/hoteldetail|\/index|\?|$)/i);
                    if (m1) {
                        const mnemonic = m1[2].toUpperCase();
                        if (!seen.has(mnemonic) && text && text.length < 200 && text.length > 2) {
                            seen.add(mnemonic);
                            hotels.push({
                                mnemonic: mnemonic,
                                name: text.split('\\n')[0].trim().slice(0, 100),
                                city_slug: m1[1],
                                url: href,
                            });
                        }
                        continue;
                    }

                    // 模式2: 品牌子域名下的酒店链接
                    // /intercontinental/hotels/xx/en/city/MNEMONIC/hoteldetail
                    const m2 = href.match(/\/[^\/]+\/hotels\/[^\/]+\/[^\/]+\/([^\/]+)\/([a-zA-Z0-9]{4,6})(?:\/|$|\?)/i);
                    if (m2) {
                        const mnemonic = m2[2].toUpperCase();
                        if (!seen.has(mnemonic) && text && text.length < 200 && text.length > 2) {
                            seen.add(mnemonic);
                            hotels.push({
                                mnemonic: mnemonic,
                                name: text.split('\\n')[0].trim().slice(0, 100),
                                city_slug: m2[1],
                                url: href,
                            });
                        }
                    }
                }

                return hotels;
            }
            """)

            print(f"[+] 从 explore 页面提取到 {len(hotels_data)} 个酒店链接")

            # 如果 explore 页面抓不到足够数据, 尝试备用策略
            if len(hotels_data) < 10:
                print(f"[*] explore 页面数据较少, 尝试 hotel-directory 备用入口...")
                alt_hotels = await try_hotel_directory(page)
                hotels_data.extend(alt_hotels)
                print(f"[+] 备用入口补充 {len(alt_hotels)} 个, 总计 {len(hotels_data)}")

            # 处理提取结果
            for h in hotels_data:
                h["brand_code"] = extract_brand_from_url(h.get("url", ""))
                h["city"] = h.get("city_slug", "").replace("-", " ").title()
                # 从 URL 推断国家/区域 (简单处理)
                h["country"] = ""
                h["country_code"] = ""
                url = h.get("url", "")
                region_m = re.search(r'/hotels/([a-z]{2})/', url)
                if region_m:
                    h["country_code"] = region_m.group(1)

            all_hotels = hotels_data

        finally:
            await context.close()

    # 去重
    seen_mn = set()
    unique = []
    for h in all_hotels:
        mn = h.get("mnemonic", "")
        if mn and mn not in seen_mn:
            seen_mn.add(mn)
            unique.append(h)
    all_hotels = unique

    # 品牌过滤
    if brand_filter:
        all_hotels = [h for h in all_hotels if h.get("brand_code") in brand_filter]
        print(f"[*] 品牌过滤后: {len(all_hotels)} 个")

    # 数量限制
    if limit and len(all_hotels) > limit:
        all_hotels = all_hotels[:limit]
        print(f"[*] 截断到 {limit} 个")

    print(f"[+] 最终酒店数: {len(all_hotels)}")
    return all_hotels


async def try_hotel_directory(page: Page) -> List[Dict]:
    """
    备用策略: 访问 /hotels/us/en/hotel-directory 或类似页面
    IHG 有多个酒店目录入口, 这里尝试几个
    """
    backup_urls = [
        "https://www.ihg.com/hotels/us/en/hotel-directory",
        "https://www.ihg.com/hotels/gb/en/hotel-directory",
        "https://www.ihg.com/content/us/en/destinations",
    ]

    all_hotels = []

    for url in backup_urls:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)

            # 滚动加载
            for _ in range(3):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(1000)

            hotels = await page.evaluate("""
            () => {
                const hotels = [];
                const seen = new Set();
                const links = document.querySelectorAll('a[href]');
                for (const a of links) {
                    const href = a.href || '';
                    const text = (a.textContent || '').trim();
                    const m = href.match(/\/hotels\/[^\/]+\/[^\/]+\/([^\/]+)\/([a-zA-Z0-9]{4,6})(?:\/|$|\?)/i);
                    if (m) {
                        const mn = m[2].toUpperCase();
                        if (!seen.has(mn) && text && text.length > 2 && text.length < 200) {
                            seen.add(mn);
                            hotels.push({
                                mnemonic: mn,
                                name: text.split('\\n')[0].trim().slice(0, 100),
                                city_slug: m[1],
                                url: href,
                            });
                        }
                    }
                }
                return hotels;
            }
            """)

            if hotels:
                all_hotels.extend(hotels)
                print(f"    [+] {url} → {len(hotels)} 个酒店")

        except Exception as e:
            print(f"    [!] {url} 失败: {e}")

    return all_hotels


def export_csv(hotels: List[Dict], filename: str):
    """导出到 CSV"""
    if not hotels:
        print("[-] 没有数据可导出")
        return

    fieldnames = [
        "mnemonic", "name", "brand_code", "brand_name",
        "country", "country_code", "city", "url"
    ]

    with open(filename, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for h in hotels:
            h["brand_name"] = BRAND_MAP.get(h.get("brand_code", ""), h.get("brand_code", ""))
            w.writerow(h)

    print(f"[+] 已导出 {len(hotels)} 个酒店到: {filename}")


def main():
    parser = argparse.ArgumentParser(description="IHG 酒店列表抓取器 (从 explore 页面)")
    parser.add_argument("--brands", help="仅保留指定品牌代码(逗号分隔), 如 IC,HI,CP")
    parser.add_argument("--limit", type=int, help="最多保留多少酒店")
    parser.add_argument("--output", default=OUTPUT_CSV, help=f"输出CSV路径 (默认: {OUTPUT_CSV})")
    parser.add_argument("--headless", default="true", choices=["true", "false"],
                        help="是否无头模式 (默认 true, 调试时用 false)")
    args = parser.parse_args()

    global HEADLESS
    HEADLESS = args.headless == "true"

    brand_filter = None
    if args.brands:
        brand_filter = {b.strip().upper() for b in args.brands.split(",") if b.strip()}
        print(f"[*] 品牌过滤: {brand_filter}")

    print("=" * 60)
    print("IHG Hotel List Fetcher (from /explore page)")
    print("=" * 60)

    try:
        hotels = asyncio.run(crawl_explore_page(
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

    export_csv(hotels, args.output)

    print("\n" + "=" * 60)
    print("完成!")
    print(f"下一步: python ihg_playwright_fetcher.py --hotels-csv {args.output}")
    print("=" * 60)


if __name__ == "__main__":
    main()
