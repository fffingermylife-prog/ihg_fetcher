"""一次性修复 DB: 重算被 IHG 汇率接口 source=K 污染的 cash_price_usd / cpp

原 bug:
    fetch_usd_rate 取 results[0].result, 而 IHG 对 CNY/EUR 等品牌定制汇率币种
    会同时返回 source=K (品牌专用, 可能 stale) 和 source=P (官方主源).
    结果 CNY 拿到 11.4745 (2022 年的 K 值), 所有大陆酒店 USD 虚高 78 倍.

本脚本:
    1. 通过 Playwright + IHG 汇率 API 拿到所有币种的最新正确汇率 (优先 source=P)
    2. 备份 DB
    3. 按币种批量 UPDATE cash_price_usd 和 cpp

用法:
    python fix_currency_rates.py            # 实际修复
    python fix_currency_rates.py --dry-run  # 只预览不改库
"""
import argparse
import asyncio
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

USER_DATA_DIR = "./ihg_browser_profile"
API_KEY = "se9ym5iAzaW8pxfBjkmgbuGjJcr3Pj6Y"
DB_PATH = "./ihg_data/ihg_prices.db"


async def fetch_rate(page, currency):
    """优先取 source=P, 带 sanity check (0, 5)"""
    if currency == "USD":
        return 1.0
    url = (
        f"https://apis.ihg.com/finance/conversions/v2/currencies"
        f"?qFcc={currency}&qTcc=USD&qV=1"
    )
    try:
        result = await page.evaluate("""
        async ({url, apiKey}) => {
            try {
                const resp = await fetch(url, {
                    method: "GET",
                    headers: {
                        "accept": "application/json, text/plain, */*",
                        "x-ihg-api-key": apiKey,
                    },
                    credentials: "include",
                });
                if (!resp.ok) return null;
                return await resp.json();
            } catch (e) { return null; }
        }
        """, {"url": url, "apiKey": API_KEY})
    except Exception as e:
        print(f"  {currency} fetch 异常: {e}")
        return None

    if not result:
        return None
    results = result.get("results") or result.get("data", {}).get("results", [])
    primary = next((r for r in results if r.get("source") == "P"), None)
    target = primary or (results[0] if results else None)
    if target:
        rate = target.get("result")
        if rate and 1e-7 < rate < 5:
            return rate, target.get("source", "?")
    return None


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只预览, 不改 DB")
    ap.add_argument("--db", default=DB_PATH)
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"DB 不存在: {db_path}")
        return

    # ====== 1. 收集 DB 里出现过的币种 ======
    conn = sqlite3.connect(db_path)
    currencies = sorted(
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT currency FROM prices WHERE currency IS NOT NULL AND currency != ''"
        ).fetchall()
    )
    print(f"DB 涉及币种 ({len(currencies)} 种): {currencies}")

    # ====== 2. 用 Playwright 拿正确汇率 ======
    print("\n=== 拉取最新汇率 (优先 source=P) ===")
    rates = {}
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            USER_DATA_DIR, headless=False, viewport={"width": 1280, "height": 720},
        )
        page = await ctx.new_page()
        await page.goto("https://www.ihg.com/", wait_until="domcontentloaded")
        await asyncio.sleep(2)

        for cur in currencies:
            r = await fetch_rate(page, cur)
            if r:
                rate, src = r
                rates[cur] = rate
                print(f"  {cur:4s} → USD = {rate:.8f} (source={src})")
            else:
                print(f"  {cur:4s} → 拿不到, 跳过")

        await ctx.close()

    if not rates:
        print("没拿到任何汇率, 退出")
        conn.close()
        return

    # ====== 3. 预览影响范围 ======
    print("\n=== 受影响记录预览 (按币种) ===")
    for cur, rate in rates.items():
        row = conn.execute(
            """
            SELECT COUNT(*) AS n,
                   SUM(CASE WHEN ABS(cash_price_usd
                       - ROUND(COALESCE(cash_price_after_tax, cash_price) * ?, 2)) > 0.01
                            THEN 1 ELSE 0 END) AS need_fix
            FROM prices
            WHERE currency = ?
              AND COALESCE(cash_price_after_tax, cash_price) IS NOT NULL
              AND cash_price_usd IS NOT NULL
            """,
            (rate, cur),
        ).fetchone()
        n, need_fix = row
        print(f"  {cur:4s}: 共 {n} 条, 其中需要修正 {need_fix} 条")

    if args.dry_run:
        print("\n--dry-run, 不修改 DB")
        conn.close()
        return

    # ====== 4. 备份 DB ======
    backup_path = db_path.with_suffix(
        f".bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    )
    shutil.copy2(db_path, backup_path)
    print(f"\n已备份 DB → {backup_path}")

    # ====== 5. 批量 UPDATE ======
    print("\n=== 开始修复 ===")
    total = 0
    for cur, rate in rates.items():
        # cash_price_usd = COALESCE(after_tax, cash_price) × rate
        # cpp            = cash_price_usd × 100 / points
        cur_count = conn.execute(
            """
            UPDATE prices
            SET cash_price_usd = ROUND(COALESCE(cash_price_after_tax, cash_price) * ?, 2),
                cpp = CASE
                    WHEN points IS NOT NULL AND points > 0
                    THEN ROUND(COALESCE(cash_price_after_tax, cash_price) * ? * 100.0 / points, 2)
                    ELSE NULL
                END
            WHERE currency = ?
              AND COALESCE(cash_price_after_tax, cash_price) IS NOT NULL
            """,
            (rate, rate, cur),
        ).rowcount
        print(f"  {cur:4s} (rate={rate:.6f}): UPDATE {cur_count} 条")
        total += cur_count

    conn.commit()
    conn.close()
    print(f"\n完成, 总计 UPDATE {total} 条记录")
    print(f"如果有问题, 可恢复备份: copy {backup_path} {db_path}")


if __name__ == "__main__":
    asyncio.run(main())
