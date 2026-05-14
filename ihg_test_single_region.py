"""
IHG 单区域测试脚本 v4 - 支持多层子区域递归
验证场景: Asia → Mainland China → 页面下方子区域链接 → 收集所有酒店

发现的问题:
    大区域 (如 Mainland China) 的 View More 可能不会显示全部酒店，
    页面下方还有子区域链接 (如北京、上海、广东等)，需要进一步点进去收集。

逻辑:
    1. 访问 /explore, 滚动到底部
    2. 展开 "Asia" 区域
    3. 访问 "Mainland China Hotels" 链接
    4. 在该页面:
       a) 先 View More 收集当前页酒店
       b) 检测页面下方的子区域链接 (排除已知的酒店详情链接)
       c) 逐个访问子区域 → View More → 收集酒店
    5. 输出结果, 对比有/无子区域递归的数量差异

用法:
    python ihg_test_single_region.py
"""

import asyncio
import json
import re
from pathlib import Path
from playwright.async_api import async_playwright


EXPLORE_URL = "https://www.ihg.com/zh-cn/explore"
USER_DATA_DIR = "./ihg_browser_profile"


def extract_mnemonic(url):
    """从酒店 URL 提取 mnemonic"""
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


def is_sub_region_link(href, text, parent_url):
    """
    判断一个链接是否是子区域链接 (而非酒店详情链接)
    子区域链接特征:
      - 在 ihg.com 域名下
      - 不包含 /hoteldetail, /hotels/, /reservation 等
      - 是类似 /beijing-china, /shanghai-china 的地理 slug
      - 文本通常含 "Hotels" 结尾
    """
    href_lower = href.lower()
    text_lower = text.lower()

    # 必须是 ihg.com 域名
    if "ihg.com" not in href_lower:
        return False

    # 排除酒店详情页
    if "/hoteldetail" in href_lower or "/hotels/" in href_lower:
        return False

    # 排除功能性页面
    excludes = [
        "/reservation", "/checkout", "/account", "/signin",
        "/legal", "/rewards", "/about", "/content", "/offers",
        "/customer-care", "/careers", "/development",
        ".pdf", ".jpg", ".png",
    ]
    if any(x in href_lower for x in excludes):
        return False

    # 排除当前页面自身
    if href.rstrip("/") == parent_url.rstrip("/"):
        return False

    # 文本太短或太长可能不是区域链接
    if len(text) < 3 or len(text) > 60:
        return False

    # 通常子区域链接文本含 "Hotels" (如 "Beijing Hotels", "Shanghai Hotels")
    # 或者是一个地理名称
    if "hotel" in text_lower:
        return True

    # 检查 URL slug 格式: ihg.com/<slug> 单段路径, 含连字符
    from urllib.parse import urlparse
    path = urlparse(href).path.strip("/")
    # 单段路径 (不含 /) 且含连字符 → 很可能是地理位置
    if "/" not in path and "-" in path and len(path) > 5:
        return True

    return False


async def collect_hotels_from_page(page, delay=3.0):
    """
    在当前页面执行: 滚动 + View More 循环 → 提取所有酒店链接
    返回: (hotels_dict, view_more_clicks)
    """
    # 初始滚动
    for _ in range(3):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(500)

    # 智能循环
    view_more_clicks = 0
    prev_count = 0
    no_change_rounds = 0

    while True:
        current_count = await page.evaluate("""
        () => document.querySelectorAll('a[href*="/hoteldetail"]').length
        """)

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

    # 提取酒店链接
    hotel_links = await page.evaluate("""
    () => {
        const arr = [];
        for (const a of document.querySelectorAll('a[href*="/hoteldetail"]')) {
            arr.push({href: a.href, text: (a.textContent || '').trim().slice(0, 100)});
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
                "name": hl["text"].split("\n")[0].strip()[:80],
                "url": hl["href"],
                "brand_code": extract_brand(hl["href"]),
                "city": extract_city(hl["href"]),
            }

    return hotels, view_more_clicks


async def collect_sub_region_links(page, current_url):
    """
    收集当前页面底部的子区域链接
    返回: [{href, text}, ...]
    """
    links = await page.evaluate("""
    (currentUrl) => {
        const arr = [];
        const seen = new Set();
        // 收集页面所有链接
        for (const a of document.querySelectorAll('a[href]')) {
            const href = a.href || '';
            const text = (a.textContent || '').trim();
            if (!href || !text) continue;
            if (seen.has(href)) continue;
            seen.add(href);
            arr.push({href, text});
        }
        return arr;
    }
    """, current_url)

    # 过滤出子区域链接
    sub_links = []
    seen_hrefs = set()
    for lk in links:
        if lk["href"] in seen_hrefs:
            continue
        if is_sub_region_link(lk["href"], lk["text"], current_url):
            seen_hrefs.add(lk["href"])
            sub_links.append(lk)

    return sub_links


async def main():
    Path(USER_DATA_DIR).mkdir(parents=True, exist_ok=True)

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
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        """)
        page = await context.new_page()

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

            # === Step 3: 展开 "Asia" ===
            region_name = "Asia"
            print(f"[Step 3] 展开 '{region_name}'...")

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

            # === Step 4: 收集 Asia 区域的二级链接 ===
            print(f"[Step 4] 收集 '{region_name}' 区域的二级链接...")
            region_links = await page.evaluate("""
            (regionName) => {
                const arr = [];
                const btns = [...document.querySelectorAll('button.cmp-accordion__button')];
                const btn = btns.find(b => b.textContent.trim() === regionName);
                if (!btn) return arr;

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

            print(f"    '{region_name}' 下有 {len(region_links)} 个二级链接:")
            for lk in region_links:
                print(f"      {lk['text']}: {lk['href']}")

            # === Step 5: 找到 "Mainland China" 并访问 ===
            china_link = None
            for lk in region_links:
                if "china" in lk["text"].lower() or "china" in lk["href"].lower():
                    china_link = lk
                    break
                # 中文版可能显示为 "中国大陆"
                if "中国" in lk["text"]:
                    china_link = lk
                    break

            if not china_link:
                print("[!] 没有找到 Mainland China 链接!")
                return

            china_url = china_link["href"]
            print(f"\n[Step 5] 访问: {china_link['text']} ({china_url})")

            await page.goto(china_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(2000)

            # === Step 6: 先在主页面收集酒店 (View More) ===
            print(f"\n[Step 6] 在主页面 (Mainland China) 收集酒店...")
            main_hotels, main_vm_clicks = await collect_hotels_from_page(page)
            print(f"    主页面酒店: {len(main_hotels)} 个 (View More: {main_vm_clicks} 次)")

            # === Step 7: 收集页面下方的子区域链接 ===
            print(f"\n[Step 7] 检测子区域链接...")
            sub_links = await collect_sub_region_links(page, china_url)
            print(f"    发现 {len(sub_links)} 个子区域链接:")
            for lk in sub_links[:20]:
                print(f"      {lk['text']:30s} → {lk['href']}")
            if len(sub_links) > 20:
                print(f"      ... 还有 {len(sub_links) - 20} 个")

            # === Step 8: 逐个访问所有子区域, 收集酒店 ===
            all_hotels = dict(main_hotels)  # 从主页面的酒店开始

            if sub_links:
                print(f"\n[Step 8] 访问所有 {len(sub_links)} 个子区域...")

                for idx, lk in enumerate(sub_links, 1):
                    print(f"\n  [8.{idx}/{len(sub_links)}] {lk['text']} → {lk['href']}")

                    # 加载子区域页面, 超时 60 秒, 失败重试 1 次
                    loaded = False
                    for attempt in range(2):
                        try:
                            await page.goto(lk["href"], wait_until="domcontentloaded", timeout=60000)
                            loaded = True
                            break
                        except Exception as e:
                            if attempt == 0:
                                print(f"    [!] 第1次超时, 重试...")
                                await page.wait_for_timeout(2000)
                            else:
                                print(f"    [!] 加载失败 (已重试): {e}")

                    if not loaded:
                        continue
                    await page.wait_for_timeout(2000)

                    sub_hotels, sub_vm = await collect_hotels_from_page(page)
                    new_count = 0
                    for mn, info in sub_hotels.items():
                        if mn not in all_hotels:
                            all_hotels[mn] = info
                            new_count += 1

                    print(f"    结果: {len(sub_hotels)} 个酒店, {new_count} 个新增 (View More: {sub_vm} 次)")
                    print(f"    累计: {len(all_hotels)} 个唯一酒店")

            # === 最终输出 ===
            print(f"\n{'='*60}")
            print(f"对比结果:")
            print(f"  只靠主页面 View More: {len(main_hotels)} 个酒店")
            print(f"  加上所有子区域后:     {len(all_hotels)} 个酒店")
            print(f"  增加了:              {len(all_hotels) - len(main_hotels)} 个")
            print(f"  子区域总数:          {len(sub_links)} 个")
            print(f"{'='*60}")

            print(f"\n前 20 个酒店:")
            for h in list(all_hotels.values())[:20]:
                print(f"  {h['mnemonic']:6s} | {h['brand_code']:3s} | {h['city'][:20]:20s} | {h['name'][:35]}")

            # 保存
            result = {
                "main_page_count": len(main_hotels),
                "total_with_sub_regions": len(all_hotels),
                "sub_region_links_found": len(sub_links),
                "sub_regions_visited": len(sub_links),
                "sub_region_links": sub_links,
                "hotels": list(all_hotels.values()),
            }
            with open("ihg_test_china_result.json", "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2, ensure_ascii=False)
            print(f"\n[+] 已保存: ihg_test_china_result.json")

        finally:
            await context.close()


if __name__ == "__main__":
    asyncio.run(main())
