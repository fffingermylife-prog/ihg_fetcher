"""
IHG 单区域测试脚本
- 只展开 "US & Canada"
- 收集二级链接
- 只访问第一个二级链接
- 反复点击 "View More Hotels" 收集所有酒店

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


def extract_mnemonic(url):
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

        try:
            # Step 1: 访问 /explore
            print(f"\n[Step 1] 访问 {EXPLORE_URL}")
            await page.goto(EXPLORE_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)

            # 关 cookie
            try:
                btn = page.get_by_role("button", name="Accept All", exact=False)
                if await btn.count() > 0:
                    await btn.first.click(timeout=2000)
                    print("    ✓ 关闭 cookie")
                    await page.wait_for_timeout(500)
            except Exception:
                pass

            # 滚动到底部
            print("[Step 2] 滚动到底部...")
            for _ in range(8):
                await page.evaluate("window.scrollBy(0, window.innerHeight)")
                await page.wait_for_timeout(600)
            await page.wait_for_timeout(2000)

            # Step 3: 只点击 "US & Canada"
            print("[Step 3] 点击展开 'US & Canada'...")
            clicked = await page.evaluate("""
            async () => {
                const wait = (ms) => new Promise(r => setTimeout(r, ms));
                const btns = [...document.querySelectorAll('button.cmp-accordion__button')];
                for (const b of btns) {
                    if (b.textContent.trim() === 'US & Canada') {
                        b.scrollIntoView({behavior: 'instant', block: 'center'});
                        await wait(300);
                        b.click();
                        await wait(2000);
                        return true;
                    }
                }
                return false;
            }
            """)
            print(f"    展开结果: {clicked}")
            await page.wait_for_timeout(2000)

            # Step 4: 收集展开后的二级链接
            print("[Step 4] 收集二级链接...")
            links = await page.evaluate("""
            () => {
                const arr = [];
                // cmp-list__item-link 是展开后目录链接的 class
                for (const a of document.querySelectorAll('a.cmp-list__item-link, a[href*="ihg.com/"]')) {
                    const href = a.href || '';
                    const text = (a.textContent || '').trim();
                    // 只要 ihg.com 根路径的单段 slug (如 /alabama-united-states)
                    try {
                        const path = new URL(href).pathname.replace(/\\/$/, '');
                        if (path && !path.includes('/hotels/') && path.split('/').length === 2 && path.includes('-')) {
                            arr.push({href, text});
                        }
                    } catch(e) {}
                }
                return arr;
            }
            """)
            print(f"    找到 {len(links)} 个二级链接")
            for lk in links[:5]:
                print(f"      {lk['text']}: {lk['href']}")
            if len(links) > 5:
                print(f"      ... 还有 {len(links) - 5} 个")

            if not links:
                print("\n[!] 没有找到二级链接! 输出页面上所有 a 标签供分析...")
                all_links = await page.evaluate("""
                () => [...document.querySelectorAll('a[href]')].slice(0, 50).map(a => ({
                    href: a.href, text: a.textContent.trim().slice(0, 60), class: a.className
                }))
                """)
                with open("debug_all_links.json", "w", encoding="utf-8") as f:
                    json.dump(all_links, f, indent=2, ensure_ascii=False)
                print(f"    → 已保存 debug_all_links.json")
                return

            # Step 5: 只访问第一个二级链接
            first_url = links[0]["href"]
            print(f"\n[Step 5] 访问第一个二级链接: {first_url}")
            await page.goto(first_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)

            # 滚动
            for _ in range(3):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(500)

            # Step 6: 反复点击 "View More Hotels"
            print("[Step 6] 反复点击 'View More Hotels'...")
            total_clicks = 0
            for round_num in range(50):
                clicked = await page.evaluate("""
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
                if not clicked:
                    print(f"    第 {round_num + 1} 轮: 没有 'View More' 按钮了, 停止")
                    break
                total_clicks += 1
                if total_clicks % 5 == 0:
                    print(f"    已点击 {total_clicks} 次...")
                await page.wait_for_timeout(1500)

            print(f"    共点击 {total_clicks} 次 'View More Hotels'")
            await page.wait_for_timeout(1000)

            # Step 7: 收集所有酒店链接
            print("[Step 7] 收集酒店链接...")
            all_hotel_links = await page.evaluate("""
            () => {
                const arr = [];
                for (const a of document.querySelectorAll('a[href]')) {
                    arr.push({href: a.href, text: (a.textContent || '').trim().slice(0, 100)});
                }
                return arr;
            }
            """)

            hotels = []
            seen = set()
            for lk in all_hotel_links:
                # 手动提取 mnemonic
                href = lk["href"]
                m = re.search(r'/hotels/[a-z]{2}/[a-z]{2}/[^/]+/([a-zA-Z0-9]{4,6})(?:/hoteldetail|/index|/?$)', href)
                if not m:
                    m = re.search(
                        r'/(?:intercontinental|regent|sixsenses|kimpton|hotelindigo|voco|crowneplaza|evenhotels|holidayinnexpress|holidayinnclubvacations|holidayinnresort|holidayinn|garner|garner-hotels|avidhotels|atwellsuites|staybridge|candlewood|iberostar|mrandmrssmith|vignettecollection|ruby|kimptonhotels)/hotels/[a-z]{2}/[a-z]{2}/[^/]+/([a-zA-Z0-9]{4,6})(?:/|$|\?)',
                        href, re.IGNORECASE
                    )
                if m:
                    mn = m.group(1).upper()
                    if mn not in seen:
                        seen.add(mn)
                        hotels.append({"mnemonic": mn, "name": lk["text"], "url": href})

            print(f"\n{'='*60}")
            print(f"结果: 共收集 {len(hotels)} 个酒店")
            print(f"{'='*60}")

            # 输出前 10 个
            for h in hotels[:10]:
                print(f"  {h['mnemonic']}: {h['name'][:50]}")
            if len(hotels) > 10:
                print(f"  ... 还有 {len(hotels) - 10} 个")

            # 保存
            with open("ihg_test_result.json", "w", encoding="utf-8") as f:
                json.dump(hotels, f, indent=2, ensure_ascii=False)
            print(f"\n[+] 完整结果已保存: ihg_test_result.json")

        finally:
            await context.close()


if __name__ == "__main__":
    asyncio.run(main())
