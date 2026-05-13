"""
IHG 酒店列表完整抓取脚本
基于 ihg_test_single_region.py 验证通过的逻辑扩展

工作流程:
    1. 访问 /explore，滚动到底部
    2. 逐个展开所有区域 accordion (9 个大区)
    3. 从每个区域面板中收集二级链接 (州/国家/地区页)
    4. 访问每个二级链接，智能循环: 提取酒店 → 滚动 → View More → 等待 → 重复直到数量不增加
    5. 输出 CSV: mnemonic, name, brand_code, city, region, state, country, url

命令行参数:
    python ihg_hotel_list_fetcher.py                                    # 抓取所有区域
    python ihg_hotel_list_fetcher.py --region "Alabama Hotels"          # 只抓指定地区 (按链接文本匹配)
    python ihg_hotel_list_fetcher.py --region "alabama-united-states"   # 按 URL slug 匹配
    python ihg_hotel_list_fetcher.py --headless false                   # 有头模式调试
    python ihg_hotel_list_fetcher.py --output my_hotels.csv             # 指定输出文件
    python ihg_hotel_list_fetcher.py --regions-only                     # 只列出所有二级链接，不抓酒店
"""

import argparse
import asyncio
import csv
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from playwright.async_api import async_playwright


# ============ 配置 ============

EXPLORE_URL = "https://www.ihg.com/explore"
USER_DATA_DIR = "./ihg_browser_profile"

OUTPUT_CSV = "ihg_hotels.csv"
OUTPUT_JSON = "ihg_hotels.json"

# 9 个大区域 (IHG /explore 页面的 accordion 标题)
REGION_NAMES = [
    "US & Canada",
    "Caribbean",
    "Mexico & Central America",
    "South America",
    "Europe",
    "Middle East",
    "Africa",
    "Asia",
    "Australia & Pacific Islands",
]

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


# ============ 工具函数 ============

def extract_mnemonic(url: str) -> Optional[str]:
    """从酒店 URL 提取 mnemonic (4-6 位字母数字代码)"""
    if not url:
        return None
    # 标准路径: /hotels/<country>/<lang>/<city>/<MNEMONIC>/hoteldetail
    m = re.search(r'/hotels/[a-z]{2}/[a-z]{2}/[^/]+/([a-zA-Z0-9]{4,6})(?:/hoteldetail|/index|/?$|/\?|$)', url)
    if m:
        return m.group(1).upper()
    # 品牌子路径
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


def extract_brand(url: str) -> str:
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


def extract_city(url: str) -> str:
    """从 URL 提取城市名"""
    m = re.search(r'/hotels/[a-z]{2}/[a-z]{2}/([^/]+)/[a-zA-Z0-9]{4,6}', url)
    return m.group(1).replace("-", " ").title() if m else ""


def parse_region_from_slug(slug: str) -> Tuple[str, str]:
    """从 URL slug 解析州/省和国家, 如 alabama-united-states → (Alabama, United States)"""
    known_countries = {
        "united-states": "United States", "canada": "Canada",
        "united-kingdom": "United Kingdom", "france": "France",
        "germany": "Germany", "italy": "Italy", "spain": "Spain",
        "japan": "Japan", "china": "China", "thailand": "Thailand",
        "australia": "Australia", "brazil": "Brazil", "mexico": "Mexico",
        "india": "India", "singapore": "Singapore", "korea": "Korea",
        "indonesia": "Indonesia", "malaysia": "Malaysia", "philippines": "Philippines",
        "vietnam": "Vietnam", "turkey": "Turkey", "saudi-arabia": "Saudi Arabia",
        "united-arab-emirates": "United Arab Emirates", "qatar": "Qatar",
        "egypt": "Egypt", "south-africa": "South Africa", "nigeria": "Nigeria",
        "kenya": "Kenya", "morocco": "Morocco", "new-zealand": "New Zealand",
        "portugal": "Portugal", "netherlands": "Netherlands", "belgium": "Belgium",
        "switzerland": "Switzerland", "austria": "Austria", "poland": "Poland",
        "czech-republic": "Czech Republic", "greece": "Greece", "ireland": "Ireland",
        "sweden": "Sweden", "norway": "Norway", "denmark": "Denmark",
        "finland": "Finland", "russia": "Russia", "argentina": "Argentina",
        "chile": "Chile", "colombia": "Colombia", "peru": "Peru",
        "costa-rica": "Costa Rica", "panama": "Panama", "jamaica": "Jamaica",
        "bahamas": "Bahamas", "dominican-republic": "Dominican Republic",
        "puerto-rico": "Puerto Rico", "bermuda": "Bermuda",
    }

    slug_lower = slug.lower().strip("/")
    for country_slug, country_name in sorted(known_countries.items(), key=lambda x: -len(x[0])):
        if slug_lower.endswith(country_slug):
            state_part = slug_lower[:-(len(country_slug))].rstrip("-")
            state_name = state_part.replace("-", " ").title() if state_part else ""
            return state_name, country_name

    # 没有匹配到已知国家，把整个 slug 当作地区名
    return slug.replace("-", " ").title(), ""


def match_region_filter(link_text: str, link_href: str, filter_value: str) -> bool:
    """判断二级链接是否匹配 --region 参数"""
    filter_lower = filter_value.lower().strip()

    # 匹配链接文本 (如 "Alabama Hotels")
    if filter_lower in link_text.lower():
        return True

    # 匹配 URL slug (如 "alabama-united-states")
    slug = link_href.split("ihg.com/")[-1].strip("/").lower()
    if filter_lower in slug:
        return True

    # 精确匹配 slug
    if slug == filter_lower:
        return True

    return False


# ============ 主逻辑 ============

async def main():
    parser = argparse.ArgumentParser(description="IHG 酒店列表完整抓取")
    parser.add_argument("--region", type=str, default=None,
                        help='只抓指定地区, 支持链接文本 (如 "Alabama Hotels") 或 URL slug (如 "alabama-united-states")')
    parser.add_argument("--headless", default="true", choices=["true", "false"],
                        help="是否无头模式 (默认 true)")
    parser.add_argument("--output", default=OUTPUT_CSV, help="输出 CSV 文件名")
    parser.add_argument("--regions-only", action="store_true",
                        help="只列出所有二级链接，不实际抓取酒店")
    parser.add_argument("--delay", type=float, default=3.0,
                        help="View More 点击后等待秒数 (默认 3)")
    args = parser.parse_args()

    headless = (args.headless == "true")
    Path(USER_DATA_DIR).mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print(f"IHG 酒店列表抓取 | 入口: {EXPLORE_URL}")
    print(f"模式: {'无头' if headless else '有头'} | 区域过滤: {args.region or '全部'}")
    print("=" * 70)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            USER_DATA_DIR,
            headless=headless,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        """)
        page = await context.new_page()

        try:
            # === Step 1: 访问 /explore ===
            print(f"\n[Step 1] 访问 {EXPLORE_URL}")
            await page.goto(EXPLORE_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)

            # 关闭 cookie 横幅
            try:
                btn = page.get_by_role("button", name="Accept All", exact=False)
                if await btn.count() > 0:
                    await btn.first.click(timeout=2000)
                    await page.wait_for_timeout(500)
                    print("    ✓ 关闭 cookie 横幅")
            except Exception:
                pass

            # === Step 2: 滚动到底部 (和 ihg_test_single_region.py 完全一致) ===
            print("[Step 2] 滚动到底部...")
            for _ in range(8):
                await page.evaluate("window.scrollBy(0, window.innerHeight)")
                await page.wait_for_timeout(600)
            await page.wait_for_timeout(2000)

            # 诊断: 检查 accordion 按钮是否已经存在
            btn_count = await page.evaluate("""
            () => {
                const btns = [...document.querySelectorAll('button.cmp-accordion__button')];
                return {
                    count: btns.length,
                    texts: btns.map(b => b.textContent.trim()).slice(0, 20)
                };
            }
            """)
            print(f"    检测到 {btn_count['count']} 个 accordion 按钮")
            if btn_count['texts']:
                print(f"    按钮文本: {btn_count['texts']}")

            # 如果没找到 accordion 按钮, 可能需要更多滚动/等待
            if btn_count['count'] == 0:
                print("    [!] 未检测到 accordion 按钮, 尝试额外滚动...")
                # 再次滚动到最底部并等待
                for _ in range(5):
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(1000)
                await page.wait_for_timeout(3000)
                # 再检查一次
                btn_count = await page.evaluate("""
                () => {
                    const btns = [...document.querySelectorAll('button.cmp-accordion__button')];
                    return {
                        count: btns.length,
                        texts: btns.map(b => b.textContent.trim()).slice(0, 20)
                    };
                }
                """)
                print(f"    重试后检测到 {btn_count['count']} 个 accordion 按钮")
                if btn_count['texts']:
                    print(f"    按钮文本: {btn_count['texts']}")
                if btn_count['count'] == 0:
                    print("    [!] 仍然找不到 accordion 按钮!")
                    print("    可能原因: 1) 页面被 Akamai 拦截 2) 页面结构变化 3) 需要有头模式")
                    print("    建议: python ihg_hotel_list_fetcher.py --headless false")
                    # 导出当前页面 HTML 片段帮助诊断
                    html_snippet = await page.evaluate("""
                    () => document.body.innerHTML.slice(0, 3000)
                    """)
                    print(f"    页面片段 (前3000字符): {html_snippet[:500]}...")

            # === Step 3: 逐个展开所有区域 (严格复制测试脚本逻辑) ===
            print("[Step 3] 展开所有区域 accordion...")
            all_region_links = {}  # {region_name: [{href, text}, ...]}

            for region_name in REGION_NAMES:
                # 点击展开该区域 (和 ihg_test_single_region.py 完全一致的 JS)
                clicked = await page.evaluate("""
                async (regionName) => {
                    const wait = (ms) => new Promise(r => setTimeout(r, ms));
                    const btns = [...document.querySelectorAll('button.cmp-accordion__button')];
                    for (const b of btns) {
                        if (b.textContent.trim() === regionName) {
                            b.scrollIntoView({behavior: 'instant', block: 'center'});
                            await wait(300);
                            b.click();
                            await wait(2000);
                            return true;
                        }
                    }
                    return false;
                }
                """, region_name)

                if not clicked:
                    print(f"    [!] 未找到区域: {region_name}")
                    continue

                await page.wait_for_timeout(2000)

                # 从展开的面板中提取二级链接
                region_links = await page.evaluate("""
                (regionName) => {
                    const arr = [];
                    const btns = [...document.querySelectorAll('button.cmp-accordion__button')];
                    const btn = btns.find(b => b.textContent.trim() === regionName);
                    if (!btn) return arr;

                    // 按钮在 H3.cmp-accordion__header 里, 面板是 H3 的下一个兄弟 DIV
                    const header = btn.closest('.cmp-accordion__header') || btn.parentElement;
                    const panel = header?.nextElementSibling;
                    if (!panel) return arr;

                    const links = panel.querySelectorAll('a');
                    for (const a of links) {
                        const href = a.href || '';
                        const text = (a.textContent || '').trim();
                        if (href && text) {
                            arr.push({href, text});
                        }
                    }
                    return arr;
                }
                """, region_name)

                all_region_links[region_name] = region_links
                print(f"    ✓ {region_name}: {len(region_links)} 个二级链接")

            # 汇总
            total_links = sum(len(v) for v in all_region_links.values())
            print(f"\n    共收集 {total_links} 个二级链接 (跨 {len(all_region_links)} 个区域)")

            # === 如果只列出链接 ===
            if args.regions_only:
                print(f"\n{'='*70}")
                print("所有二级链接列表:")
                print(f"{'='*70}")
                for region_name, links in all_region_links.items():
                    print(f"\n  [{region_name}] ({len(links)} 个)")
                    for lk in links:
                        slug = lk['href'].split('ihg.com/')[-1].strip('/')
                        print(f"    {lk['text']:40s} → {slug}")
                await context.close()
                return

            # === Step 4: 应用 --region 过滤 ===
            target_links = []  # [(region_name, href, link_text, slug)]
            for region_name, links in all_region_links.items():
                for lk in links:
                    if args.region:
                        if not match_region_filter(lk["text"], lk["href"], args.region):
                            continue
                    slug = lk["href"].split("ihg.com/")[-1].strip("/")
                    target_links.append((region_name, lk["href"], lk["text"], slug))

            if args.region and not target_links:
                print(f"\n[!] 没有找到匹配 '--region {args.region}' 的二级链接")
                print("    提示: 用 --regions-only 列出所有可用的二级链接")
                await context.close()
                return

            print(f"\n[Step 4] 待抓取: {len(target_links)} 个二级页面")
            if args.region:
                print(f"    (已过滤: --region '{args.region}')")

            # === Step 5: 逐个访问二级链接，抓取酒店 ===
            all_hotels = {}  # mnemonic → hotel_info (全局去重)

            for idx, (region_name, url, link_text, slug) in enumerate(target_links, 1):
                state, country = parse_region_from_slug(slug)
                print(f"\n[{idx}/{len(target_links)}] {link_text}")
                print(f"    URL: {url}")
                print(f"    解析: 区域={region_name}, 州/省={state}, 国家={country}")

                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                except Exception as e:
                    print(f"    [!] 页面加载失败: {e}")
                    continue
                await page.wait_for_timeout(2000)

                # 初始滚动
                for _ in range(3):
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(500)

                # 智能循环: 提取 → 滚动 → View More → 等待 → 重复直到数量不增加
                prev_count = 0
                no_change_rounds = 0
                view_more_clicks = 0

                while True:
                    # 获取当前酒店链接数量
                    current_count = await page.evaluate("""
                    () => document.querySelectorAll('a[href*="/hoteldetail"]').length
                    """)

                    if current_count > prev_count:
                        no_change_rounds = 0
                        prev_count = current_count
                    else:
                        no_change_rounds += 1

                    # 连续 2 轮数量不增加，停止
                    if no_change_rounds >= 2:
                        break

                    # 滚动到底部
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(800)

                    # 尝试点击 View More
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
                        await page.wait_for_timeout(int(args.delay * 1000))
                    else:
                        # 没有 View More 按钮了，再等一下确认
                        await page.wait_for_timeout(1000)
                        break

                # 从 DOM 提取所有酒店链接
                hotel_links = await page.evaluate("""
                () => {
                    const arr = [];
                    for (const a of document.querySelectorAll('a[href*="/hoteldetail"]')) {
                        arr.push({href: a.href, text: (a.textContent || '').trim().slice(0, 100)});
                    }
                    return arr;
                }
                """)

                # 解析酒店信息
                page_new = 0
                for hl in hotel_links:
                    mn = extract_mnemonic(hl["href"])
                    if mn and mn not in all_hotels:
                        all_hotels[mn] = {
                            "mnemonic": mn,
                            "name": hl["text"].split("\n")[0].strip()[:80],
                            "brand_code": extract_brand(hl["href"]),
                            "city": extract_city(hl["href"]),
                            "region": region_name,
                            "state": state,
                            "country": country,
                            "url": hl["href"],
                        }
                        page_new += 1

                if view_more_clicks > 0:
                    print(f"    View More: {view_more_clicks} 次点击")
                print(f"    结果: {len(hotel_links)} 个链接, {page_new} 个新酒店 (累计: {len(all_hotels)})")

        except KeyboardInterrupt:
            print("\n[!] 用户中断")
        except Exception as e:
            print(f"\n[!] 异常: {e}")
            import traceback
            traceback.print_exc()
        finally:
            await context.close()

    # === 输出结果 ===
    hotels_list = list(all_hotels.values())

    print(f"\n{'='*70}")
    print(f"完成! 共 {len(hotels_list)} 个唯一酒店")
    print(f"{'='*70}")

    if not hotels_list:
        print("[!] 未抓到数据，可能原因:")
        print("    1. 网络问题或 Akamai 拦截")
        print("    2. 页面结构变化")
        print("    建议: python ihg_hotel_list_fetcher.py --headless false 观察浏览器行为")
        return

    # 统计
    region_stats = {}
    brand_stats = {}
    for h in hotels_list:
        region_stats[h["region"]] = region_stats.get(h["region"], 0) + 1
        bc = h["brand_code"] or "?"
        brand_stats[bc] = brand_stats.get(bc, 0) + 1

    print("\n区域分布:")
    for k, v in sorted(region_stats.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v}")

    print("\n品牌分布:")
    for k, v in sorted(brand_stats.items(), key=lambda x: -x[1]):
        brand_name = BRAND_MAP.get(k, "未知")
        print(f"  {k} ({brand_name}): {v}")

    # 导出 CSV
    csv_path = args.output
    fields = ["mnemonic", "name", "brand_code", "city", "region", "state", "country", "url"]
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for h in hotels_list:
            writer.writerow(h)
    print(f"\n[+] CSV 已导出: {csv_path} ({len(hotels_list)} 条)")

    # 导出 JSON
    json_path = csv_path.replace(".csv", ".json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(hotels_list, f, indent=2, ensure_ascii=False)
    print(f"[+] JSON 已导出: {json_path}")

    # 显示前几条
    print(f"\n前 10 个酒店:")
    for h in hotels_list[:10]:
        loc = ", ".join(filter(None, [h["city"], h["state"], h["country"]]))
        print(f"  {h['mnemonic']:6s} | {h['brand_code']:3s} | {loc[:35]:35s} | {h['name'][:30]}")

    print(f"\n下一步: python ihg_playwright_fetcher.py --hotels-csv {csv_path}")


if __name__ == "__main__":
    asyncio.run(main())
