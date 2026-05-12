"""
IHG Hotel List Fetcher - 抓取 IHG 全球酒店目录

从 https://www.ihg.com/hotels/us/en/global/destinations/index 开始,
按 国家 → 城市 → 酒店 三层遍历, 提取每个酒店的:
- hotelMnemonic (5字母代码, 用于 calendar API)
- 酒店名称
- 品牌代码
- 国家
- 城市
- 地址

输出: ihg_hotels.csv

依赖:
    pip install playwright beautifulsoup4
    playwright install chromium

使用:
    python ihg_hotel_list_fetcher.py
    # 或带过滤
    python ihg_hotel_list_fetcher.py --countries cn,th,jp
    python ihg_hotel_list_fetcher.py --brands IC,HI,CP   # 仅指定品牌
"""

import argparse
import asyncio
import csv
import json
import re
import sys
from pathlib import Path
from typing import List, Dict, Optional, Set
from urllib.parse import urljoin, urlparse

from playwright.async_api import async_playwright, BrowserContext, Page


# ============ 配置 ============

ENTRY_URL = "https://www.ihg.com/hotels/us/en/global/destinations/index"

OUTPUT_CSV = "ihg_hotels.csv"

HEADLESS = True
USER_DATA_DIR = "./ihg_browser_profile"  # 和主脚本共享

# 并发控制
MAX_COUNTRIES_CONCURRENT = 3      # 同时抓几个国家
PAGE_LOAD_TIMEOUT_MS = 30000
DELAY_MS = 500                     # 页面间基础延迟

# ============ 配置结束 ============


# 品牌代码 → 品牌名 映射 (IHG 公开的品牌列表)
BRAND_MAP = {
    "SR": "Six Senses",
    "RC": "Regent",
    "IC": "InterContinental",
    "VX": "Vignette Collection",
    "UL": "Luxury & Lifestyle",
    "KI": "Kimpton",
    "HT": "Hotel Indigo",
    "VC": "voco",
    "CP": "Crowne Plaza",
    "EH": "EVEN Hotels",
    "HI": "Holiday Inn",
    "RS": "Holiday Inn Resort",
    "CV": "Holiday Inn Club Vacations",
    "EX": "Holiday Inn Express",
    "GE": "Garner",
    "AV": "avid hotels",
    "AT": "Atwell Suites",
    "SB": "Staybridge Suites",
    "HE": "Holiday Inn Express & Suites",
    "CW": "Candlewood Suites",
    "IS": "Iberostar",
    "MR": "Mr. & Mrs. Smith",
    "RU": "Ruby Hotels",
}


def extract_mnemonic_from_url(url: str) -> Optional[str]:
    """
    从酒店详情 URL 中提取 hotelMnemonic
    形如: /hotels/us/en/bangkok/bkkhb/hoteldetail → "BKKHB"
    """
    m = re.search(r'/hotels/[^/]+/[^/]+/[^/]+/([a-z0-9]{4,6})/(?:hoteldetail|index)', url.lower())
    if m:
        return m.group(1).upper()
    return None


def extract_brand_from_url(url: str) -> Optional[str]:
    """
    从 URL 路径中提取品牌名 (如 /intercontinental/, /holidayinn/)
    如果在详情页的 URL 里有品牌路径, 可以据此推断
    """
    url_lower = url.lower()
    # IHG 子品牌路径模式
    patterns = {
        "intercontinental": "IC",
        "regent": "RC",
        "sixsenses": "SR",
        "kimpton": "KI",
        "hotelindigo": "HT",
        "voco": "VC",
        "crowneplaza": "CP",
        "evenhotels": "EH",
        "holidayinnexpress": "EX",
        "holidayinnclubvacations": "CV",
        "holidayinnresort": "RS",
        "holidayinn": "HI",  # 注意: 这个要放在最后, 因为是 holiday inn express 的前缀
        "garner": "GE",
        "avidhotels": "AV",
        "atwellsuites": "AT",
        "staybridge": "SB",
        "candlewood": "CW",
        "iberostar": "IS",
        "mrandmrssmith": "MR",
        "vignettecollection": "VX",
        "ruby": "RU",
    }
    # 先匹配更具体的 (holidayinnexpress 在 holidayinn 前)
    for path_key, brand_code in sorted(patterns.items(), key=lambda x: -len(x[0])):
        if f"/{path_key}/" in url_lower:
            return brand_code
    return None


async def safe_goto(page: Page, url: str, retries: int = 2) -> bool:
    """带重试的页面跳转"""
    for i in range(retries + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
            await page.wait_for_timeout(DELAY_MS)
            return True
        except Exception as e:
            if i < retries:
                print(f"  [!] 加载失败 ({e}), 重试 {i+1}/{retries}")
                await page.wait_for_timeout(2000)
            else:
                print(f"  [X] 加载失败: {url}")
                return False
    return False


async def get_country_links(page: Page) -> List[Dict]:
    """
    从入口页面提取所有国家链接
    返回: [{country: "Thailand", country_code: "th", url: "..."}]
    """
    ok = await safe_goto(page, ENTRY_URL)
    if not ok:
        return []

    # 国家链接一般在 a 标签里, URL 形如 /hotels/us/en/destinations/<country>/...
    # 这里使用 JS 评估来抓取, 更灵活
    countries = await page.evaluate("""
    () => {
        const results = [];
        const seen = new Set();
        // 国家级别的链接: path 段数 ~ /hotels/us/en/destinations/<country>
        const links = document.querySelectorAll('a[href*="/destinations/"]');
        for (const a of links) {
            const href = a.getAttribute('href') || '';
            // 匹配国家级 URL
            const m = href.match(/\\/hotels\\/[^\\/]+\\/[^\\/]+\\/destinations\\/([a-z]{2})(\\/[a-z-]+)?(\\/|$)/i);
            if (m) {
                const countryCode = m[1].toLowerCase();
                const fullUrl = a.href;
                const text = (a.textContent || '').trim();
                if (!seen.has(countryCode) && text && text.length < 60) {
                    seen.add(countryCode);
                    results.push({
                        country: text,
                        country_code: countryCode,
                        url: fullUrl
                    });
                }
            }
        }
        return results;
    }
    """)

    print(f"[+] 发现 {len(countries)} 个国家")
    return countries


async def get_city_links_or_hotels(page: Page, country_url: str) -> Dict:
    """
    访问国家页面, 返回该页面上:
    - 城市链接 (如果是国家-城市列表)
    - 或直接是酒店链接 (小国家可能直接列出)
    """
    ok = await safe_goto(page, country_url)
    if not ok:
        return {"cities": [], "hotels": []}

    result = await page.evaluate("""
    () => {
        const cities = [];
        const hotels = [];
        const citySeen = new Set();
        const hotelSeen = new Set();

        const links = document.querySelectorAll('a[href]');
        for (const a of links) {
            const href = a.getAttribute('href') || '';
            const fullUrl = a.href;
            const text = (a.textContent || '').trim();

            // 酒店详情页: /hotels/<locale>/en/<city>/<mnemonic>/hoteldetail
            const hotelM = href.match(/\\/hotels\\/[^\\/]+\\/[^\\/]+\\/([^\\/]+)\\/([a-z0-9]{4,6})\\/(?:hoteldetail|index)/i);
            if (hotelM && !hotelSeen.has(hotelM[2])) {
                hotelSeen.add(hotelM[2]);
                hotels.push({
                    mnemonic: hotelM[2].toUpperCase(),
                    name: text,
                    city_slug: hotelM[1],
                    url: fullUrl,
                });
                continue;
            }

            // 城市链接: /hotels/<locale>/en/destinations/<country>/<city>
            const cityM = href.match(/\\/destinations\\/[a-z]{2}\\/([^\\/]+)\\/?$/i);
            if (cityM && !citySeen.has(cityM[1]) && text && text.length < 60) {
                citySeen.add(cityM[1]);
                cities.push({
                    city_slug: cityM[1],
                    city: text,
                    url: fullUrl,
                });
            }
        }
        return { cities, hotels };
    }
    """)

    return result


async def get_hotels_in_city(page: Page, city_url: str) -> List[Dict]:
    """访问城市页面, 获取所有酒店"""
    ok = await safe_goto(page, city_url)
    if not ok:
        return []

    hotels = await page.evaluate("""
    () => {
        const results = [];
        const seen = new Set();
        const links = document.querySelectorAll('a[href]');
        for (const a of links) {
            const href = a.getAttribute('href') || '';
            const fullUrl = a.href;
            const text = (a.textContent || '').trim();
            // 匹配酒店详情页
            const m = href.match(/\\/hotels\\/[^\\/]+\\/[^\\/]+\\/([^\\/]+)\\/([a-z0-9]{4,6})\\/(?:hoteldetail|index)/i);
            if (m && !seen.has(m[2])) {
                seen.add(m[2]);
                results.push({
                    mnemonic: m[2].toUpperCase(),
                    name: text,
                    city_slug: m[1],
                    url: fullUrl,
                });
            }
        }
        return results;
    }
    """)
    return hotels


async def enrich_hotel_details(page: Page, hotel: Dict) -> Dict:
    """
    访问酒店详情页, 补充: 品牌代码、完整地址
    这一步很慢, 可选执行
    """
    ok = await safe_goto(page, hotel["url"])
    if not ok:
        return hotel

    details = await page.evaluate("""
    () => {
        // 尝试从 window.digitalData 或 meta 标签获取结构化数据
        const d = window.digitalData || {};
        const hotelInfo = d.hotel || d.property || {};

        // 从 JSON-LD schema 提取地址
        let address = '';
        const ldScripts = document.querySelectorAll('script[type="application/ld+json"]');
        for (const s of ldScripts) {
            try {
                const obj = JSON.parse(s.textContent);
                const items = Array.isArray(obj) ? obj : [obj];
                for (const it of items) {
                    if (it.address) {
                        const a = it.address;
                        const parts = [a.streetAddress, a.addressLocality, a.addressRegion, a.postalCode, a.addressCountry].filter(Boolean);
                        address = parts.join(', ');
                        if (address) break;
                    }
                }
                if (address) break;
            } catch(e) {}
        }

        return {
            brand_from_data: hotelInfo.brandCode || hotelInfo.brand || '',
            address: address,
        };
    }
    """)

    hotel["address"] = details.get("address", "")
    brand_from_data = details.get("brand_from_data", "")
    if brand_from_data:
        hotel["brand_code"] = brand_from_data
    return hotel


async def crawl(
    country_filter: Optional[Set[str]] = None,
    brand_filter: Optional[Set[str]] = None,
    skip_hotel_details: bool = True,
    limit: Optional[int] = None,
) -> List[Dict]:
    """
    主爬虫流程
    - country_filter: 仅抓指定国家代码 (如 {"cn", "th", "jp"})
    - brand_filter: 仅保留指定品牌代码 (如 {"IC", "HI"})
    - skip_hotel_details: True=不访问每个酒店详情页 (快, 但没地址)
    - limit: 最多抓多少酒店 (测试用)
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
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )

        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
        """)

        page = await context.new_page()

        try:
            # Step 1: 国家列表
            print(f"\n[1/3] 获取国家列表...")
            countries = await get_country_links(page)
            if not countries:
                print(f"[!] 未抓到任何国家, 可能页面结构变化")
                return []

            if country_filter:
                countries = [c for c in countries if c["country_code"] in country_filter]
                print(f"[*] 过滤后: {len(countries)} 个国家")

            # Step 2: 每个国家 → 城市 → 酒店
            print(f"\n[2/3] 遍历每个国家...")
            for ci, country in enumerate(countries):
                if limit and len(all_hotels) >= limit:
                    print(f"[*] 已达到酒店数量上限 {limit}, 停止")
                    break

                print(f"\n--- 国家 {ci+1}/{len(countries)}: {country['country']} ({country['country_code']}) ---")

                data = await get_city_links_or_hotels(page, country["url"])
                cities = data["cities"]
                direct_hotels = data["hotels"]

                # 若国家页直接有酒店, 加进来
                for h in direct_hotels:
                    h["country"] = country["country"]
                    h["country_code"] = country["country_code"]
                    h["city"] = h.get("city_slug", "").replace("-", " ").title()
                    h["brand_code"] = extract_brand_from_url(h["url"]) or ""
                    all_hotels.append(h)

                # 遍历每个城市
                print(f"  城市: {len(cities)}, 国家页直接酒店: {len(direct_hotels)}")
                for cj, city in enumerate(cities):
                    if limit and len(all_hotels) >= limit:
                        break

                    print(f"  城市 {cj+1}/{len(cities)}: {city['city']}")
                    hotels_in_city = await get_hotels_in_city(page, city["url"])
                    for h in hotels_in_city:
                        h["country"] = country["country"]
                        h["country_code"] = country["country_code"]
                        h["city"] = city["city"]
                        h["brand_code"] = extract_brand_from_url(h["url"]) or ""
                        all_hotels.append(h)

                print(f"  累计酒店数: {len(all_hotels)}")

            # 去重 (按 mnemonic)
            seen_mn = set()
            unique_hotels = []
            for h in all_hotels:
                if h["mnemonic"] not in seen_mn:
                    seen_mn.add(h["mnemonic"])
                    unique_hotels.append(h)
            all_hotels = unique_hotels
            print(f"\n[*] 去重后共 {len(all_hotels)} 个酒店")

            # 品牌过滤
            if brand_filter:
                all_hotels = [h for h in all_hotels if h.get("brand_code") in brand_filter]
                print(f"[*] 品牌过滤后: {len(all_hotels)} 个")

            # Step 3: (可选) 访问每个酒店详情获取地址
            if not skip_hotel_details:
                print(f"\n[3/3] 补充酒店详情 (地址等)...")
                for hi, h in enumerate(all_hotels):
                    print(f"  [{hi+1}/{len(all_hotels)}] {h['mnemonic']} - {h.get('name','')[:40]}")
                    await enrich_hotel_details(page, h)

        finally:
            await context.close()

    return all_hotels


def export_csv(hotels: List[Dict], filename: str):
    """导出到 CSV"""
    if not hotels:
        print("[-] 没有数据可导出")
        return

    fieldnames = [
        "mnemonic", "name", "brand_code", "brand_name",
        "country", "country_code", "city", "address", "url"
    ]

    with open(filename, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for h in hotels:
            h["brand_name"] = BRAND_MAP.get(h.get("brand_code", ""), h.get("brand_code", ""))
            w.writerow(h)

    print(f"[+] 已导出 {len(hotels)} 个酒店到: {filename}")


def main():
    parser = argparse.ArgumentParser(description="IHG 酒店列表抓取器")
    parser.add_argument("--countries", help="仅抓指定国家代码(逗号分隔), 如 cn,th,jp")
    parser.add_argument("--brands", help="仅保留指定品牌代码(逗号分隔), 如 IC,HI,CP")
    parser.add_argument("--details", action="store_true", help="访问每个酒店详情页获取地址(慢)")
    parser.add_argument("--limit", type=int, help="最多抓多少酒店(测试用)")
    parser.add_argument("--output", default=OUTPUT_CSV, help=f"输出CSV路径 (默认: {OUTPUT_CSV})")
    args = parser.parse_args()

    country_filter = None
    if args.countries:
        country_filter = {c.strip().lower() for c in args.countries.split(",") if c.strip()}
        print(f"[*] 国家过滤: {country_filter}")

    brand_filter = None
    if args.brands:
        brand_filter = {b.strip().upper() for b in args.brands.split(",") if b.strip()}
        print(f"[*] 品牌过滤: {brand_filter}")

    print("=" * 60)
    print("IHG Hotel List Fetcher")
    print("=" * 60)

    try:
        hotels = asyncio.run(crawl(
            country_filter=country_filter,
            brand_filter=brand_filter,
            skip_hotel_details=not args.details,
            limit=args.limit,
        ))
    except KeyboardInterrupt:
        print("\n用户中断")
        sys.exit(130)

    export_csv(hotels, args.output)
    print("\n" + "=" * 60)
    print("完成!")
    print("=" * 60)


if __name__ == "__main__":
    main()
