"""
IHG Calendar Price Fetcher - Playwright 永久自动化版本

核心思路:
- 用 Playwright 启动真实 Chromium (绕过 Akamai 反爬)
- 在浏览器页面的 JS 上下文中执行 fetch 调用 API (自动带全部 cookie)
- 持久化 session (launch_persistent_context), 重复运行更快
- 完全无人值守, 适合 cron 定时任务

新增功能:
- 支持从 ihg_hotels.csv 读取酒店列表 (由 ihg_hotel_list_fetcher.py 生成)
- 汇率换算: 把各种本地货币 (THB/USD/...) 换算成人民币 (CNY)
- 输出包含本地价 + CNY 价 + CPP(CNY 口径)

依赖安装:
    pip install playwright requests
    playwright install chromium

使用:
    # 方式 A: 用脚本内配置的 HOTEL_CODES
    python ihg_playwright_fetcher.py

    # 方式 B: 从 CSV 读取酒店列表
    python ihg_playwright_fetcher.py --hotels-csv ihg_hotels.csv
    python ihg_playwright_fetcher.py --hotels-csv ihg_hotels.csv --country cn
    python ihg_playwright_fetcher.py --hotels-csv ihg_hotels.csv --brand IC --limit 5

    # 方式 C: 只看指定的几个酒店
    python ihg_playwright_fetcher.py --codes BKKHB,NYCHA

定时任务 (crontab -e):
    0 3 * * *  cd /path/to/script && /usr/bin/python3 ihg_playwright_fetcher.py --hotels-csv ihg_hotels.csv >> ihg.log 2>&1
"""

import argparse
import asyncio
import csv
import json
import os
import sys
from datetime import date, timedelta, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from playwright.async_api import async_playwright, BrowserContext, Page


# ============ 配置区域 ============

# 默认酒店代码 (当没指定 --hotels-csv 或 --codes 时使用)
HOTEL_CODES = [
    "BKKHB",   # InterContinental Bangkok
]

# 滑动窗口配置
DAYS_AHEAD = 365
WINDOW_SIZE_DAYS = 60

# 住宿
LENGTH_OF_STAY = 1
ADULTS = 1

# API Key
API_KEY = "se9ym5iAzaW8pxfBjkmgbuGjJcr3Pj6Y"

# 积分 Rate Plan Codes
POINTS_RATE_PLAN_CODES = ["IVAN1", "IVAN3", "IVAN5", "IVAN6", "IVAN7", "IVANI"]

# 请求间隔 (毫秒)
REQUEST_DELAY_MS = 2000

# 浏览器设置
HEADLESS = True
USER_DATA_DIR = "./ihg_browser_profile"

# 输出文件
OUTPUT_CSV = "ihg_prices.csv"
OUTPUT_JSON = "ihg_prices.json"
OUTPUT_RAW = "ihg_raw_snapshots.json"

# 汇率: 目标货币
TARGET_CURRENCY = "CNY"

# 积分价值估算 (CNY): 用于在没有同日现金价时做 CPP 估算
# 按 IHG Points 业内公认价值 ~0.5 美分/点 = ~0.036 CNY/点, 这里不使用, 而是严格用实际现金价

# 种子 URL 模板
SEED_URL_TEMPLATE = (
    "https://www.ihg.com/intercontinental/hotels/us/en/find-hotels/select-roomrate"
    "?fromRedirect=true&qSrt=sBR&qSlH={hotel_code}&qRms=1&qAdlt=1&qChld=0"
    "&qCiD={ci_day:02d}&qCiMy={ci_month:02d}{ci_year}"
    "&qCoD={co_day:02d}&qCoMy={co_month:02d}{co_year}"
    "&setPMCookies=true&qSHBrC=IC&qpMn=0&srb_u=1&qRmFltr="
)

# ============ 配置结束 ============


# ============ 汇率模块 ============

# IHG 内部汇率 API (与网站显示的价格完全一致)
IHG_FX_API = "https://apis.ihg.com/finance/conversions/v2/currencies"

# 常见的 IHG 酒店使用的货币列表 (用于批量预取汇率)
COMMON_CURRENCIES = [
    "USD", "EUR", "GBP", "JPY", "THB", "CNY", "AUD", "CAD", "SGD", "HKD",
    "KRW", "INR", "AED", "SAR", "MYR", "IDR", "PHP", "TWD", "VND", "NZD",
    "CHF", "SEK", "NOK", "DKK", "MXN", "BRL", "ZAR", "EGP", "QAR", "BHD",
    "OMR", "KWD", "JOD", "TRY", "RUB", "PLN", "CZK", "HUF", "RON", "BGN",
]


class FxRates:
    """
    汇率缓存器
    - 优先使用 IHG 自己的汇率 API (与网站一致)
    - 回退到 Frankfurter API (免费)
    """

    def __init__(self, target: str = "CNY"):
        self.target = target.upper()
        self.rates: Dict[str, float] = {self.target: 1.0}
        self.source: str = ""

    def load(self) -> bool:
        """加载汇率, 优先 IHG API, 回退 Frankfurter"""
        if self._load_ihg():
            return True
        print("[*] IHG 汇率 API 不可用, 回退到 Frankfurter...")
        return self._load_frankfurter()

    def _load_ihg(self) -> bool:
        """
        使用 IHG 内部汇率 API:
        GET https://apis.ihg.com/finance/conversions/v2/currencies?qFcc=USD&qTcc=CNY&qV=1
        返回 1 USD = ? CNY
        """
        loaded = 0
        for cur in COMMON_CURRENCIES:
            if cur.upper() == self.target:
                continue
            try:
                resp = requests.get(
                    IHG_FX_API,
                    params={"qFcc": cur, "qTcc": self.target, "qV": "1"},
                    headers={
                        "x-ihg-api-key": API_KEY,
                        "accept": "application/json",
                    },
                    timeout=10,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    # 响应通常包含换算后的金额
                    # 可能结构: {"convertedAmount": 7.28, ...} 或 {"data": {"convertedValue": 7.28}}
                    rate = self._extract_rate_from_ihg_response(data)
                    if rate and rate > 0:
                        self.rates[cur.upper()] = rate
                        loaded += 1
            except Exception:
                pass

        if loaded > 0:
            self.source = "IHG Finance API"
            print(f"[+] IHG 汇率加载成功, 共 {loaded} 个币种 → {self.target}")
            return True
        return False

    @staticmethod
    def _extract_rate_from_ihg_response(data: dict) -> Optional[float]:
        """
        从 IHG 汇率 API 响应中提取换算率
        可能的结构:
        - {"convertedAmount": 7.28}
        - {"data": {"convertedValue": 7.28}}
        - {"result": 7.28}
        - 直接数字
        """
        if not data:
            return None

        # 尝试多种可能的字段
        for key in ["convertedAmount", "convertedValue", "result", "value", "amount"]:
            if key in data:
                try:
                    return float(data[key])
                except (ValueError, TypeError):
                    pass

        # 嵌套在 data 里
        inner = data.get("data", {})
        if isinstance(inner, dict):
            for key in ["convertedAmount", "convertedValue", "result", "value", "amount"]:
                if key in inner:
                    try:
                        return float(inner[key])
                    except (ValueError, TypeError):
                        pass

        # 如果整个响应只有一个数字字段
        for v in data.values():
            if isinstance(v, (int, float)) and v > 0:
                return float(v)

        return None

    def _load_frankfurter(self) -> bool:
        """回退: 从 Frankfurter 拉取汇率"""
        try:
            resp = requests.get(
                "https://api.frankfurter.dev/v1/latest",
                params={"base": self.target},
                timeout=10,
            )
            if resp.status_code != 200:
                print(f"[!] Frankfurter API 返回 {resp.status_code}")
                return False

            data = resp.json()
            src_rates = data.get("rates", {})

            for currency, rate_to_cny_base in src_rates.items():
                if rate_to_cny_base and rate_to_cny_base > 0:
                    self.rates[currency.upper()] = 1.0 / rate_to_cny_base

            self.rates[self.target] = 1.0
            self.source = f"Frankfurter ({data.get('date', '')})"
            print(f"[+] Frankfurter 汇率加载成功, 共 {len(self.rates)} 个币种")
            return True
        except Exception as e:
            print(f"[!] Frankfurter 汇率也加载失败: {e}")
            return False

    def load_single_via_ihg_browser(self, page, from_currency: str) -> Optional[float]:
        """
        在浏览器上下文里调 IHG 汇率 API (自动带 cookie, 绕过 Akamai)
        用于运行时碰到新币种时动态补充
        """
        # 这个方法由 async 主循环调用, 这里只是占位
        # 实际调用在 fetch_fx_via_browser
        pass

    def to_target(self, amount: Optional[float], currency: str) -> Optional[float]:
        """把 amount (以 currency 计价) 换算为目标货币"""
        if amount is None or not currency:
            return None
        currency = currency.upper()
        if currency == self.target:
            return round(amount, 2)
        rate = self.rates.get(currency)
        if rate is None:
            fallback_map = {"RMB": "CNY", "YUAN": "CNY"}
            rate = self.rates.get(fallback_map.get(currency, ""))
        if rate is None:
            return None
        return round(amount * rate, 2)


# ============ Playwright 抓取 ============


async def fetch_fx_via_browser(page: Page, from_currency: str, to_currency: str = "CNY") -> Optional[float]:
    """
    在浏览器上下文中调用 IHG 汇率 API
    GET https://apis.ihg.com/finance/conversions/v2/currencies?qFcc=USD&qTcc=CNY&qV=1
    这样可以绕过 Akamai, 同时使用 IHG 官方汇率
    """
    js_code = """
    async ({ fromCur, toCur, apiKey }) => {
        try {
            const url = `https://apis.ihg.com/finance/conversions/v2/currencies?qFcc=${fromCur}&qTcc=${toCur}&qV=1`;
            const resp = await fetch(url, {
                method: "GET",
                headers: {
                    "accept": "application/json",
                    "x-ihg-api-key": apiKey,
                },
                credentials: "include",
            });
            if (!resp.ok) return { ok: false, status: resp.status };
            const data = await resp.json();
            return { ok: true, data: data };
        } catch (err) {
            return { ok: false, error: err.message };
        }
    }
    """
    try:
        result = await page.evaluate(js_code, {
            "fromCur": from_currency.upper(),
            "toCur": to_currency.upper(),
            "apiKey": API_KEY,
        })
        if result.get("ok") and result.get("data"):
            data = result["data"]
            rate = FxRates._extract_rate_from_ihg_response(data)
            if rate and rate > 0:
                return rate
    except Exception as e:
        print(f"  [!] 浏览器汇率查询失败 ({from_currency}→{to_currency}): {e}")
    return None


async def load_fx_via_browser(page: Page, fx: FxRates) -> None:
    """
    在浏览器已建立 session 后, 通过 IHG 汇率 API 加载所有常用币种的汇率
    这是最可靠的方式 (绕过 Akamai + 使用 IHG 官方汇率)
    """
    print(f"[*] 通过浏览器加载 IHG 汇率 ({fx.target})...")
    loaded = 0
    for cur in COMMON_CURRENCIES:
        if cur.upper() == fx.target:
            continue
        if cur.upper() in fx.rates:
            loaded += 1
            continue  # 已有
        rate = await fetch_fx_via_browser(page, cur, fx.target)
        if rate:
            fx.rates[cur.upper()] = rate
            loaded += 1
        await asyncio.sleep(0.3)  # 别太快

    if loaded > 0:
        fx.source = "IHG Finance API (via browser)"
        print(f"[+] 浏览器汇率加载完成, 共 {loaded} 个币种 → {fx.target}")


def build_seed_url(hotel_code: str) -> str:
    """为某个酒店构造 seed URL"""
    ci = date.today() + timedelta(days=30)
    co = ci + timedelta(days=1)
    return SEED_URL_TEMPLATE.format(
        hotel_code=hotel_code,
        ci_day=ci.day, ci_month=ci.month, ci_year=ci.year,
        co_day=co.day, co_month=co.month, co_year=co.year,
    )


def iter_date_windows(
    start_date: date, total_days: int, window_size: int
) -> List[Tuple[str, str]]:
    """生成滑动窗口日期区间"""
    windows = []
    current = start_date
    end_target = start_date + timedelta(days=total_days - 1)
    while current <= end_target:
        window_end = min(current + timedelta(days=window_size - 1), end_target)
        windows.append((current.isoformat(), window_end.isoformat()))
        current = window_end + timedelta(days=1)
    return windows


async def fetch_via_browser(
    page: Page,
    hotel_code: str,
    start_date: str,
    end_date: str,
    points_mode: bool,
) -> Optional[dict]:
    """在浏览器页面上下文中执行 fetch API 调用"""
    payload: Dict = {
        "hotelMnemonics": [hotel_code],
        "startDate": start_date,
        "endDate": end_date,
        "lengthOfStay": LENGTH_OF_STAY,
        "guestCounts": [{"otaCode": "AQC10", "count": ADULTS}],
        "options": {
            "includeSellStrategy": "followChannel",
            "returnAmountsAfterTaxForLowestOffer": True,
            "returnAverages": True,
            "lowestOfferPerRatePlan": True,
            "identifyLowestOfferPerRatePlan": True,
        },
    }
    if points_mode:
        payload["rates"] = {"ratePlanCodes": POINTS_RATE_PLAN_CODES}

    mode_str = "积分" if points_mode else "现金"
    tag = f"[{hotel_code} {start_date}~{end_date} {mode_str}]"

    js_code = """
    async ({ apiKey, payload }) => {
        const uuid = () => 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
            const r = Math.random() * 16 | 0;
            const v = c === 'x' ? r : (r & 3 | 8);
            return v.toString(16);
        });

        try {
            const resp = await fetch("https://apis.ihg.com/availability/v1/calendar", {
                method: "POST",
                headers: {
                    "accept": "application/json, text/plain, */*",
                    "content-type": "application/json; charset=UTF-8",
                    "ihg-language": "en-US",
                    "ihg-sessionid": uuid(),
                    "ihg-transactionid": uuid(),
                    "x-ihg-api-key": apiKey,
                },
                body: JSON.stringify(payload),
                credentials: "include",
            });
            const text = await resp.text();
            let json = null;
            try { json = JSON.parse(text); } catch (e) {}
            return { status: resp.status, ok: resp.ok, data: json, text: json ? null : text.slice(0, 500) };
        } catch (err) {
            return { status: 0, ok: false, error: err.message };
        }
    }
    """

    try:
        result = await page.evaluate(js_code, {"apiKey": API_KEY, "payload": payload})
    except Exception as e:
        print(f"  [-] {tag} 页面执行失败: {e}")
        return None

    if result.get("ok") and result.get("data"):
        print(f"  [+] {tag} 成功")
        return result["data"]
    else:
        status = result.get("status", "?")
        err = result.get("error") or result.get("text") or result.get("data")
        print(f"  [-] {tag} 失败 HTTP {status}: {str(err)[:200]}")
        return None


def parse_cash_response(response_data: dict) -> List[dict]:
    """解析现金价格"""
    results = []
    if not response_data:
        return results

    for hotel_entry in response_data.get("data", {}).get("hotels", []):
        info = hotel_entry.get("hotel", {})
        hotel_code = info.get("hotelMnemonic", "UNKNOWN")
        brand_code = info.get("brandCode", "")
        currency_fallback = info.get("propertyCurrency", "")

        for day_data in hotel_entry.get("calendar", []):
            lr = day_data.get("lowestRate")
            price = None
            currency = currency_fallback
            if lr:
                try:
                    price = float(lr.get("totalAmount", 0)) or None
                except (ValueError, TypeError):
                    price = None
                currency = lr.get("currency", currency_fallback)

            results.append({
                "hotel_code": hotel_code,
                "brand_code": brand_code,
                "date": day_data.get("start", ""),
                "cash_price": price,
                "cash_currency": currency,
            })
    return results


def parse_points_response(response_data: dict) -> List[dict]:
    """解析积分价格"""
    results = []
    if not response_data:
        return results

    for hotel_entry in response_data.get("data", {}).get("hotels", []):
        info = hotel_entry.get("hotel", {})
        hotel_code = info.get("hotelMnemonic", "UNKNOWN")
        brand_code = info.get("brandCode", "")

        reward_codes = {
            rp.get("code", "")
            for rp in hotel_entry.get("ratePlans", [])
            if rp.get("isRewardNight")
        }

        for day_data in hotel_entry.get("calendar", []):
            lowest = None
            for offer in day_data.get("offers", []):
                rp = offer.get("ratePlanCode", "")
                if rp in reward_codes or rp.startswith("IVAN"):
                    tp = offer.get("totalPoints")
                    if tp is not None:
                        try:
                            v = float(tp)
                            if lowest is None or v < lowest:
                                lowest = v
                        except (ValueError, TypeError):
                            pass

            results.append({
                "hotel_code": hotel_code,
                "brand_code": brand_code,
                "date": day_data.get("start", ""),
                "points_price": lowest,
            })
    return results


def merge_prices(
    cash_prices: List[dict],
    points_prices: List[dict],
    fx: FxRates,
    hotel_meta: Dict[str, dict],
) -> List[dict]:
    """
    合并现金 + 积分到每日一行
    - 用汇率换算成 CNY
    - CPP 使用 CNY 口径 (每积分兑换价值 CNY × 100)
    - 加入酒店元数据 (名称、国家、城市)
    """
    cash_map = {(r["hotel_code"], r["date"]): r for r in cash_prices}
    pts_map = {(r["hotel_code"], r["date"]): r for r in points_prices}

    all_keys = set(cash_map.keys()) | set(pts_map.keys())
    merged = []

    for key in sorted(all_keys):
        hotel_code, date_str = key
        c = cash_map.get(key, {})
        p = pts_map.get(key, {})
        cash_price = c.get("cash_price")
        cash_currency = c.get("cash_currency", "")
        points_price = p.get("points_price")

        # 汇率换算
        cash_price_cny = fx.to_target(cash_price, cash_currency) if cash_price else None

        # CPP: 每 1000 点能换多少人民币 (越高越划算)
        cpp_cny_per_1k = None
        if cash_price_cny and points_price and points_price > 0:
            cpp_cny_per_1k = round(cash_price_cny / points_price * 1000, 2)

        # 酒店元数据
        meta = hotel_meta.get(hotel_code, {})

        merged.append({
            "hotel_code": hotel_code,
            "hotel_name": meta.get("name", ""),
            "brand_code": c.get("brand_code") or p.get("brand_code") or meta.get("brand_code", ""),
            "brand_name": meta.get("brand_name", ""),
            "country": meta.get("country", ""),
            "city": meta.get("city", ""),
            "date": date_str,
            "cash_price": cash_price,
            "cash_currency": cash_currency,
            "cash_price_cny": cash_price_cny,
            "points_price": points_price,
            "cpp_cny_per_1k_points": cpp_cny_per_1k,
        })
    return merged


def export_csv(data: List[dict], filename: str):
    if not data:
        return
    fieldnames = [
        "hotel_code", "hotel_name", "brand_code", "brand_name",
        "country", "city", "date",
        "cash_price", "cash_currency", "cash_price_cny",
        "points_price", "cpp_cny_per_1k_points"
    ]
    with open(filename, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(data)
    print(f"[+] 已导出 CSV: {filename} ({len(data)} 条)")


def export_json(data, filename: str):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"[+] 已导出 JSON: {filename}")


def load_hotels_from_csv(
    csv_path: str,
    country_filter: Optional[str] = None,
    brand_filter: Optional[str] = None,
    limit: Optional[int] = None,
) -> Tuple[List[str], Dict[str, dict]]:
    """
    从 CSV 读取酒店列表
    返回: (酒店代码列表, 代码→元数据的映射)
    """
    if not Path(csv_path).exists():
        print(f"[!] CSV 文件不存在: {csv_path}")
        return [], {}

    codes = []
    meta = {}
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            mn = (row.get("mnemonic") or "").strip().upper()
            if not mn:
                continue

            if country_filter and (row.get("country_code") or "").lower() != country_filter.lower():
                continue
            if brand_filter and (row.get("brand_code") or "").upper() != brand_filter.upper():
                continue

            codes.append(mn)
            meta[mn] = {
                "name": row.get("name", ""),
                "brand_code": row.get("brand_code", ""),
                "brand_name": row.get("brand_name", ""),
                "country": row.get("country", ""),
                "country_code": row.get("country_code", ""),
                "city": row.get("city", ""),
                "address": row.get("address", ""),
            }

            if limit and len(codes) >= limit:
                break

    print(f"[+] 从 {csv_path} 读取 {len(codes)} 个酒店")
    return codes, meta


async def wait_for_valid_session(page: Page, hotel_code: str, max_attempts: int = 3) -> bool:
    """访问种子页面建立合法 session"""
    for attempt in range(max_attempts):
        seed_url = build_seed_url(hotel_code)
        print(f"[*] 访问种子页面建立 session (尝试 {attempt + 1}/{max_attempts})...")

        try:
            await page.goto(seed_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(5000)
        except Exception as e:
            print(f"    [!] 页面加载异常: {e}")

        test_start = (date.today() + timedelta(days=1)).isoformat()
        test_end = (date.today() + timedelta(days=7)).isoformat()
        test_result = await fetch_via_browser(
            page, hotel_code, test_start, test_end, points_mode=False
        )

        if test_result and test_result.get("data", {}).get("hotels"):
            print(f"[✓] Session 有效")
            return True

        print(f"[!] Session 无效, 等待后重试...")
        await page.wait_for_timeout(3000)

    return False


async def run(
    hotel_codes: List[str],
    hotel_meta: Dict[str, dict],
    fx: FxRates,
    output_csv: str = OUTPUT_CSV,
    output_json: str = OUTPUT_JSON,
    output_raw: str = OUTPUT_RAW,
) -> int:
    print("=" * 60)
    print("IHG Playwright Fetcher - 永久自动化全量抓取")
    print("=" * 60)

    start = date.today() + timedelta(days=1)
    windows = iter_date_windows(start, DAYS_AHEAD, WINDOW_SIZE_DAYS)

    print(f"\n配置:")
    print(f"  酒店数: {len(hotel_codes)}")
    print(f"  日期: {start.isoformat()} 起 {DAYS_AHEAD} 天 → {len(windows)} 个窗口")
    print(f"  预计请求数: {len(hotel_codes) * len(windows) * 2} (现金+积分)")
    print(f"  目标货币: {TARGET_CURRENCY}")
    print(f"  Headless: {HEADLESS}")

    Path(USER_DATA_DIR).mkdir(parents=True, exist_ok=True)

    all_cash = []
    all_points = []
    raw_samples = []
    valid_sessions = 0

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
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        """)

        page = await context.new_page()

        try:
            fx_loaded_via_browser = False

            for hi, hotel_code in enumerate(hotel_codes):
                hname = hotel_meta.get(hotel_code, {}).get("name", "")
                print(f"\n{'=' * 50}")
                print(f"[酒店 {hi + 1}/{len(hotel_codes)}] {hotel_code} {hname[:40]}")
                print('=' * 50)

                ok = await wait_for_valid_session(page, hotel_code)
                if not ok:
                    print(f"[-] 酒店 {hotel_code} 无法建立有效 session, 跳过")
                    continue
                valid_sessions += 1

                # 第一个成功建立 session 后, 通过浏览器加载 IHG 汇率
                if not fx_loaded_via_browser:
                    await load_fx_via_browser(page, fx)
                    fx_loaded_via_browser = True

                for wi, (ws, we) in enumerate(windows):
                    print(f"\n  --- 窗口 {wi + 1}/{len(windows)} ({ws} ~ {we}) ---")

                    cash_data = await fetch_via_browser(page, hotel_code, ws, we, points_mode=False)
                    if cash_data:
                        all_cash.extend(parse_cash_response(cash_data))

                    await page.wait_for_timeout(REQUEST_DELAY_MS)

                    pts_data = await fetch_via_browser(page, hotel_code, ws, we, points_mode=True)
                    if pts_data:
                        all_points.extend(parse_points_response(pts_data))

                    if wi == 0:
                        if cash_data:
                            raw_samples.append({
                                "hotel": hotel_code, "window": f"{ws}~{we}",
                                "type": "cash", "data": cash_data
                            })
                        if pts_data:
                            raw_samples.append({
                                "hotel": hotel_code, "window": f"{ws}~{we}",
                                "type": "points", "data": pts_data
                            })

                    is_last = (hi == len(hotel_codes) - 1) and (wi == len(windows) - 1)
                    if not is_last:
                        await page.wait_for_timeout(REQUEST_DELAY_MS)

        finally:
            await context.close()

    # 合并
    merged = merge_prices(all_cash, all_points, fx, hotel_meta)

    print("\n" + "=" * 60)
    print(f"完成! 成功酒店: {valid_sessions}/{len(hotel_codes)}, 共 {len(merged)} 条每日价格")
    print("=" * 60)

    if merged:
        print(f"\n价格预览 (前 10 条):")
        header = ["酒店", "日期", "现金(本地)", "币种", "现金(CNY)", "积分", "CPP(CNY/1k)"]
        print("  " + " | ".join(f"{h:<10}" for h in header))
        print("  " + "-" * 90)
        for row in merged[:10]:
            cash_str = f"{row['cash_price']:.2f}" if row['cash_price'] else "N/A"
            cny_str = f"{row['cash_price_cny']:.2f}" if row['cash_price_cny'] else "N/A"
            pts_str = f"{int(row['points_price'])}" if row['points_price'] else "N/A"
            cpp_str = f"{row['cpp_cny_per_1k_points']}" if row['cpp_cny_per_1k_points'] else "N/A"
            cells = [
                row['hotel_code'][:10], row['date'][:10],
                cash_str[:10], (row['cash_currency'] or '')[:10],
                cny_str[:10], pts_str[:10], cpp_str[:10]
            ]
            print("  " + " | ".join(f"{c:<10}" for c in cells))

        export_csv(merged, output_csv)
        export_json(merged, output_json)

    if raw_samples:
        export_json(raw_samples, output_raw)

    return len(merged)


def main():
    parser = argparse.ArgumentParser(description="IHG 价格全量抓取")
    parser.add_argument("--hotels-csv", help="从 CSV 文件读取酒店列表 (由 ihg_hotel_list_fetcher.py 生成)")
    parser.add_argument("--codes", help="直接指定酒店代码(逗号分隔), 如 BKKHB,NYCHA")
    parser.add_argument("--country", help="过滤: 只抓指定国家代码 (如 cn, th)")
    parser.add_argument("--brand", help="过滤: 只抓指定品牌 (如 IC)")
    parser.add_argument("--limit", type=int, help="最多抓多少酒店")
    parser.add_argument("--output", default=OUTPUT_CSV, help=f"输出CSV (默认 {OUTPUT_CSV})")
    args = parser.parse_args()

    # 1) 解析酒店列表
    hotel_codes: List[str] = []
    hotel_meta: Dict[str, dict] = {}

    if args.codes:
        hotel_codes = [c.strip().upper() for c in args.codes.split(",") if c.strip()]
    elif args.hotels_csv:
        hotel_codes, hotel_meta = load_hotels_from_csv(
            args.hotels_csv,
            country_filter=args.country,
            brand_filter=args.brand,
            limit=args.limit,
        )
    else:
        hotel_codes = HOTEL_CODES[:]

    if not hotel_codes:
        print("[!] 没有酒店可抓, 请用 --hotels-csv 或 --codes 指定")
        sys.exit(1)

    # 2) 加载汇率
    fx = FxRates(TARGET_CURRENCY)
    if not fx.load():
        print("[!] 汇率加载失败, 将只输出本地货币价格")

    # 3) 运行
    try:
        count = asyncio.run(run(hotel_codes, hotel_meta, fx, output_csv=args.output))
        sys.exit(0 if count > 0 else 1)
    except KeyboardInterrupt:
        print("\n用户中断")
        sys.exit(130)
    except Exception as e:
        print(f"\n[!] 运行失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
