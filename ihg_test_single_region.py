"""
IHG 单区域测试脚本 v3
按层级抓取: /explore → 展开区域 → 二级国家/地区页 → 提取 /hoteldetail URL → hotelMnemonic

逻辑:
1. 访问 /explore, 滚动到底部
2. 逐个展开区域 (US & Canada, Europe, Asia 等)
3. 每展开一个区域, 收集该区域的二级链接 (州/国家)
4. 访问每个二级链接, 拦截 GraphQL + 点击 View More + 提取 DOM 中的酒店链接
5. 输出带层级信息的酒店列表

本次测试: 只展开 "US & Canada", 只访问前 2 个二级链接

用法:
    python ihg_test_single_region.py
"""

import asyncio
import json
import re
from pathlib import Path
from playwright.async_api import async_playwright


EXPLORE_URL = "https://www.ihg.com/explore"
USER_DATA_DIR = "./ihg_browser_profile"
GRAPHQL_URL = "apis.ihg.com/graphql"

# 拦截到的酒店代码
intercepted_mnemonics = set()


def on_request(request):
    """拦截 GraphQL 请求, 从 payload 中提取 hotelMnemonic 列表"""
    if GRAPHQL_URL in request.url:
        try:
            body = request.post_data
            if body and "hotelMnemonic" in body:
                data = json.loads(body)
                mnemonics = data.get("variables", {}).get("input", {}).get("hotelMnemonic", [])
                if mnemonics:
                    for mn in mnemonics:
                        intercepted_mnemonics.add(mn.upper())
                    print(f"    [拦截] GraphQL +{len(mnemonics)} 个代码 (累计: {len(intercepted_mnemonics)})")
        except Exception:
            pass


def parse_region_from_slug(slug):
    """从 URL slug 解析地理信息, 如 alabama-united-states → (Alabama, United States)"""
    # 常见模式: <state>-<country> 或 <country> 或 <city>-<country>
    parts = slug.replace("/", "").split("-")

    # 尝试识别国家 (最后一个或两个词)
    known_countries = {
        "united-states": "United States", "canada": "Canada",
        "united-kingdom": "United Kingdom", "france": "France",
        "germany": "Germany", "italy": "Italy", "spain": "Spain",
        "japan": "Japan", "china": "China", "thailand": "Thailand",
        "australia": "Australia", "brazil": "Brazil", "mexico": "Mexico",
        "india": "India", "singapore": "Singapore", "korea": "Korea",
    }

    slug_lower = slug.lower().strip("/")
    for country_slug, country_name in known_countries.items():
        if slug_lower.endswith(country_slug):
            state_part = slug_lower[:-(len(country_slug))].rstrip("-")
            state_name = state_part.replace("-", " ").title() if state_part else ""
            return state_name, country_name

    # 如果没匹配到已知国家, 把整个 slug 当作地区名
    return slug.replace("-", " ").title(), ""


def extract_mnemonic(url):
    """从酒店 URL 提取 mnemonic"""
    if not url:
        return None
    m = re.search(r'/hotels/[a-z]{2}/[a-z]{2}/[^/]+/([a-zA-Z0-9]{4,6})(?:/hoteldetail|/index|/?$|/\?|$)', url)
    if m:
        return m.group(1).upper()
    m = re.search(
        r'/(?:intercontinental|regent|sixsenses|kimpton|hotelindigo|voco|crowneplaza|evenhotels|holidayinnexpress|holidayinnclubvacations|holidayinnresort|holidayinn|garner|garner-hotels|avidhotels|atwellsuites|staybridge|candlewood|iberostar|mrandmrssmith|vignettecollection|ruby|kimptonhotels)/hotels/[a-z]{2}/[a-z]{2}/[^/]+/([a-zA-Z0-9]{4,6})(?:/|$|\?)',
        url, re.IGNORECASE
    )
    if m:
        return m.group(1).upper()
    return None


def extract_brand(url):
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
    }
    for key, code in sorted(patterns.items(), key=lambda x: -len(x[0])):
        if f"/{key}/" in url_lower:
            return code
    return ""


def extract_city(url):
    m = re.search(r'/hotels/[a-z]{2}/[a-z]{2}/([^/]+)/[a-zA-Z0-9]{4,6}', url)
    return m.group(1).replace("-", " ").title() if m else ""


async def main():
    Path(USER_DATA_DIR).mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            USER_DATA_DIR,
            headless=False,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        """)
        page = await context.new_page()

        # 只在进入二级页面后才开始拦截
        graphql_active = False

        def conditional_on_request(request):
            if graphql_active:
                on_request(request)

        page.on("request", conditional_on_request)

        try:
            # === Step 1: 访问 /explore ===
            print(f"\n[Step 1] 访问 {EXPLORE_URL}")
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
            print("[Step 2] 滚动到底部...")
            for _ in range(8):
                await page.evaluate("window.scrollBy(0, window.innerHeight)")
                await page.wait_for_timeout(600)
            await page.wait_for_timeout(2000)

            # === Step 3: 只展开 "US & Canada" ===
            region_name = "US & Canada"
            print(f"[Step 3] 只展开 '{region_name}'...")

            # 点击展开
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
            print(f"    展开结果: {clicked}")
            await page.wait_for_timeout(2000)

            # === Step 4: 从展开的面板中直接提取链接 ===
            print(f"[Step 4] 收集 '{region_name}' 区域的二级链接...")
            region_links = await page.evaluate("""
            (regionName) => {
                const arr = [];
                // 找到该区域的按钮
                const btns = [...document.querySelectorAll('button.cmp-accordion__button')];
                const btn = btns.find(b => b.textContent.trim() === regionName);
                if (!btn) return arr;
                
                // 按钮在 H3.cmp-accordion__header 里, 面板是 H3 的下一个兄弟 DIV
                const header = btn.closest('.cmp-accordion__header') || btn.parentElement;
                const panel = header?.nextElementSibling;
                if (!panel) return arr;
                
                // 从面板中提取所有链接
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

            print(f"    '{region_name}' 下有 {len(region_links)} 个二级链接")
            for lk in region_links[:5]:
                print(f"      {lk['text']}: {lk['href']}")
            if len(region_links) > 5:
                print(f"      ... 还有 {len(region_links) - 5} 个")

            if not region_links:
                print("[!] 没有找到二级链接, 退出")
                return

            # === Step 5: 只访问前 2 个二级链接 ===
            MAX_TEST = 2
            all_hotels = []

            for idx, lk in enumerate(region_links[:MAX_TEST]):
                url = lk["href"]
                link_text = lk["text"]
                slug = url.split("ihg.com/")[-1].strip("/")
                state, country = parse_region_from_slug(slug)

                print(f"\n[Step 5.{idx+1}] 访问: {link_text} ({url})")
                print(f"    解析: 区域={region_name}, 州/省={state}, 国家={country}")

                # 开启收集
                graphql_active = False  # 暂不拦截 GraphQL

                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(2000)

                # 初始滚动
                for _ in range(3):
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(500)

                # 循环: 提取酒店 → 滚动 → 点击 View More → 等待新卡片 → 重复直到数量不再增加
                view_more_clicks = 0
                prev_count = 0
                no_change_rounds = 0

                while True:
                    # 提取当前酒店数量
                    current_count = await page.evaluate("""
                    () => document.querySelectorAll('a[href*="/hoteldetail"]').length
                    """)

                    if current_count > prev_count:
                        no_change_rounds = 0
                        prev_count = current_count
                    else:
                        no_change_rounds += 1

                    # 连续 2 轮没有新增, 停止
                    if no_change_rounds >= 2:
                        break

                    # 滚动到底部
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(800)

                    # 尝试点击 View More Hotels
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
                        await page.wait_for_timeout(1500)  # 等新卡片加载
                    else:
                        # 没有按钮了, 再等一下确认
                        await page.wait_for_timeout(1000)
                        break

                if view_more_clicks > 0:
                    print(f"    点击了 {view_more_clicks} 次 View More, 最终 {prev_count} 个酒店卡片")

                # 关闭拦截
                graphql_active = False
                await page.wait_for_timeout(500)

                # 从 DOM 提取酒店链接
                hotel_links = await page.evaluate("""
                () => {
                    const arr = [];
                    for (const a of document.querySelectorAll('a[href*="/hoteldetail"]')) {
                        arr.push({href: a.href, text: (a.textContent || '').trim().slice(0, 100)});
                    }
                    return arr;
                }
                """)

                # 提取酒店信息
                page_hotels = {}
                for hl in hotel_links:
                    mn = extract_mnemonic(hl["href"])
                    if mn and mn not in page_hotels:
                        page_hotels[mn] = {
                            "mnemonic": mn,
                            "name": hl["text"].split("\n")[0].strip()[:80],
                            "url": hl["href"],
                            "brand_code": extract_brand(hl["href"]),
                            "city": extract_city(hl["href"]),
                            "region": region_name,
                            "state": state,
                            "country": country,
                        }

                print(f"    结果: 共 {len(page_hotels)} 个唯一酒店")
                all_hotels.extend(page_hotels.values())

            # === 最终输出 ===
            print(f"\n{'='*60}")
            print(f"最终结果: 共 {len(all_hotels)} 个酒店")
            print(f"{'='*60}")

            print(f"\n前 15 个酒店:")
            for h in all_hotels[:15]:
                loc = f"{h['city']}, {h['state']}, {h['country']}".strip(", ")
                print(f"  {h['mnemonic']:6s} | {h['brand_code']:3s} | {loc[:30]:30s} | {h['name'][:30]}")

            # 保存
            with open("ihg_test_result.json", "w", encoding="utf-8") as f:
                json.dump(all_hotels, f, indent=2, ensure_ascii=False)
            print(f"\n[+] 已保存: ihg_test_result.json")

        finally:
            await context.close()


if __name__ == "__main__":
    asyncio.run(main())
