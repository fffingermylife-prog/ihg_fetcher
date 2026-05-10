"""
IHG Hotel Calendar Price Fetcher
获取 IHG 旗下酒店的日历房现金价格和积分价格

使用方法:
1. 在浏览器中打开 IHG 酒店页面，通过 DevTools 获取有效的 cookie
2. 将 cookie 填入 COOKIES 变量
3. 修改 HOTEL_CODES 列表添加你需要查询的酒店代码
4. 运行脚本

API 端点: POST https://apis.ihg.com/availability/v1/calendar
"""

import requests
import json
import csv
import uuid
import time
from datetime import datetime, timedelta
from typing import List, Dict, Optional


# ============ 配置区域 ============

# API Key (从前端JS中提取，通常不会频繁变化)
API_KEY = "se9ym5iAzaW8pxfBjkmgbuGjJcr3Pj6Y"

# 需要从浏览器 DevTools 中获取的 Cookie
# 打开 IHG 网站 -> F12 -> Network -> 找到 calendar 请求 -> 复制 Cookie 头
COOKIES = ""  # 粘贴你的完整 cookie 字符串

# 要查询的酒店代码列表
# 可以从 IHG 网站 URL 中获取，如 qSlH=BKKHB 表示酒店代码为 BKKHB
HOTEL_CODES = [
    "BKKHB",   # InterContinental Bangkok
    "FAICW",   # 示例酒店
    # 添加更多酒店代码...
]

# 查询日期范围 (API 支持最多约2个月的范围)
START_DATE = "2026-05-10"
END_DATE = "2026-07-10"

# 住宿天数
LENGTH_OF_STAY = 1

# 成人数
ADULTS = 1

# 请求间隔(秒)，避免被限流
REQUEST_DELAY = 2

# ============ 配置结束 ============


class IHGCalendarFetcher:
    """IHG 日历价格获取器"""

    BASE_URL = "https://apis.ihg.com/availability/v1/calendar"

    def __init__(self, api_key: str, cookies: str = ""):
        self.api_key = api_key
        self.cookies = cookies
        self.session = requests.Session()
        self._setup_session()

    def _setup_session(self):
        """设置请求会话的默认头"""
        self.session.headers.update({
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json; charset=UTF-8",
            "ihg-language": "en-US",
            "referer": "https://www.ihg.com/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
            "x-ihg-api-key": self.api_key,
        })
        if self.cookies:
            self.session.headers["cookie"] = self.cookies

    def _generate_ids(self) -> tuple:
        """生成会话ID和事务ID"""
        session_id = str(uuid.uuid4())
        transaction_id = str(uuid.uuid4())
        return session_id, transaction_id

    def build_payload(
        self,
        hotel_codes: List[str],
        start_date: str,
        end_date: str,
        length_of_stay: int = 1,
        adults: int = 1,
    ) -> dict:
        """
        构建请求体

        参数:
            hotel_codes: 酒店代码列表，如 ["BKKHB", "FAICW"]
            start_date: 开始日期，格式 "YYYY-MM-DD"
            end_date: 结束日期，格式 "YYYY-MM-DD"
            length_of_stay: 住宿天数
            adults: 成人数
        """
        return {
            "hotelMnemonics": hotel_codes,
            "startDate": start_date,
            "endDate": end_date,
            "lengthOfStay": length_of_stay,
            "guestCounts": [
                {
                    "otaCode": "AQC10",  # 成人代码
                    "count": adults
                }
            ],
            "options": {
                "includeSellStrategy": "followChannel",
                "returnAmountsAfterTaxForLowestOffer": True,
                "returnAverages": True,
                "lowestOfferPerRatePlan": True,
                "identifyLowestOfferPerRatePlan": True,
            }
        }

    def fetch_calendar(
        self,
        hotel_codes: List[str],
        start_date: str,
        end_date: str,
        length_of_stay: int = 1,
        adults: int = 1,
    ) -> Optional[dict]:
        """
        获取日历价格数据

        返回: API 响应的 JSON 数据，失败返回 None
        """
        session_id, transaction_id = self._generate_ids()

        # 设置动态头
        headers = {
            "ihg-sessionid": session_id,
            "ihg-transactionid": transaction_id,
        }

        payload = self.build_payload(
            hotel_codes, start_date, end_date, length_of_stay, adults
        )

        print(f"[*] 请求酒店: {hotel_codes}")
        print(f"    日期范围: {start_date} ~ {end_date}")
        print(f"    住宿天数: {length_of_stay}, 成人: {adults}")

        try:
            response = self.session.post(
                self.BASE_URL,
                headers=headers,
                json=payload,
                timeout=30
            )

            if response.status_code == 200:
                data = response.json()
                print(f"[+] 请求成功！")
                return data
            else:
                print(f"[-] 请求失败: HTTP {response.status_code}")
                print(f"    响应: {response.text[:500]}")
                return None

        except requests.exceptions.RequestException as e:
            print(f"[-] 请求异常: {e}")
            return None

    def fetch_multiple_hotels(
        self,
        hotel_codes: List[str],
        start_date: str,
        end_date: str,
        length_of_stay: int = 1,
        adults: int = 1,
        batch_size: int = 1,
        delay: float = 2.0,
    ) -> List[dict]:
        """
        批量获取多个酒店的价格数据

        参数:
            batch_size: 每次请求包含的酒店数量 (API 可能支持多个)
            delay: 请求间隔秒数
        """
        results = []

        for i in range(0, len(hotel_codes), batch_size):
            batch = hotel_codes[i:i + batch_size]
            data = self.fetch_calendar(batch, start_date, end_date, length_of_stay, adults)
            if data:
                results.append(data)

            if i + batch_size < len(hotel_codes):
                print(f"    等待 {delay} 秒...")
                time.sleep(delay)

        return results


def parse_calendar_response(response_data: dict) -> List[dict]:
    """
    解析日历 API 的响应数据，提取每日价格信息

    返回格式:
    [
        {
            "hotel_code": "BKKHB",
            "date": "2026-05-10",
            "cash_price": 150.00,
            "cash_currency": "USD",
            "points_price": 40000,
            "available": True,
            "rate_plan": "...",
        },
        ...
    ]
    """
    parsed_results = []

    # IHG API 响应结构可能是:
    # { "hotelCalendars": [ { "hotelMnemonic": "XXX", "calendar": [...] } ] }
    # 或类似结构，需要根据实际响应调整

    if not response_data:
        return parsed_results

    # 尝试解析常见的响应结构
    hotel_calendars = response_data.get("hotelCalendars", [])

    if not hotel_calendars:
        # 尝试其他可能的键名
        for key in ["calendars", "data", "hotels", "results"]:
            if key in response_data:
                hotel_calendars = response_data[key]
                break

    if not hotel_calendars:
        print("[!] 无法识别响应结构，输出原始数据:")
        print(json.dumps(response_data, indent=2, ensure_ascii=False)[:2000])
        return parsed_results

    for hotel_cal in hotel_calendars:
        hotel_code = hotel_cal.get("hotelMnemonic", hotel_cal.get("hotelCode", "UNKNOWN"))
        calendar_days = hotel_cal.get("calendar", hotel_cal.get("dates", hotel_cal.get("days", [])))

        for day_data in calendar_days:
            date = day_data.get("date", day_data.get("startDate", ""))
            available = day_data.get("available", day_data.get("isAvailable", True))

            # 提取现金价格
            cash_price = None
            cash_currency = None
            points_price = None

            # 尝试多种可能的价格字段
            lowest_offer = day_data.get("lowestOffer", day_data.get("lowest", {}))
            if isinstance(lowest_offer, dict):
                cash_price = lowest_offer.get("amount", lowest_offer.get("price"))
                cash_currency = lowest_offer.get("currencyCode", lowest_offer.get("currency"))

            # 积分价格
            points_offer = day_data.get("pointsOffer", day_data.get("lowestPointsOffer", {}))
            if isinstance(points_offer, dict):
                points_price = points_offer.get("points", points_offer.get("amount"))

            # 备选: 直接从 amounts 字段获取
            amounts = day_data.get("amounts", {})
            if amounts:
                if not cash_price:
                    cash_price = amounts.get("afterTax", amounts.get("beforeTax"))
                    cash_currency = amounts.get("currencyCode")

            parsed_results.append({
                "hotel_code": hotel_code,
                "date": date,
                "cash_price": cash_price,
                "cash_currency": cash_currency,
                "points_price": points_price,
                "available": available,
            })

    return parsed_results


def export_to_csv(data: List[dict], filename: str = "ihg_prices.csv"):
    """导出价格数据到 CSV 文件"""
    if not data:
        print("[-] 没有数据可导出")
        return

    fieldnames = ["hotel_code", "date", "cash_price", "cash_currency", "points_price", "available"]

    with open(filename, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(data)

    print(f"[+] 数据已导出到: {filename}")
    print(f"    共 {len(data)} 条记录")


def export_to_json(data: List[dict], filename: str = "ihg_prices.json"):
    """导出价格数据到 JSON 文件"""
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"[+] 数据已导出到: {filename}")


def save_raw_response(response_data: dict, filename: str = "ihg_raw_response.json"):
    """保存原始 API 响应（用于调试和分析响应结构）"""
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(response_data, f, indent=2, ensure_ascii=False)

    print(f"[+] 原始响应已保存到: {filename}")


def main():
    """主函数"""
    print("=" * 60)
    print("IHG Hotel Calendar Price Fetcher")
    print("=" * 60)

    if not COOKIES:
        print("\n[!] 警告: 未设置 Cookie!")
        print("    请从浏览器 DevTools 中获取 Cookie 并填入脚本顶部的 COOKIES 变量")
        print("    步骤:")
        print("    1. 打开 https://www.ihg.com/ 并搜索酒店")
        print("    2. 打开 DevTools (F12) -> Network 面板")
        print("    3. 在页面上切换到 Points 视图并选择日历")
        print("    4. 找到 POST calendar 请求")
        print("    5. 复制请求头中的 Cookie 值")
        print("\n    如果不需要 Cookie 也能获取数据，程序将继续运行...")
        print()

    # 初始化获取器
    fetcher = IHGCalendarFetcher(api_key=API_KEY, cookies=COOKIES)

    # 获取数据
    print(f"\n[*] 开始获取 {len(HOTEL_CODES)} 个酒店的日历价格...")
    print(f"    日期范围: {START_DATE} ~ {END_DATE}")
    print()

    results = fetcher.fetch_multiple_hotels(
        hotel_codes=HOTEL_CODES,
        start_date=START_DATE,
        end_date=END_DATE,
        length_of_stay=LENGTH_OF_STAY,
        adults=ADULTS,
        batch_size=1,  # 每次请求一个酒店，更稳定
        delay=REQUEST_DELAY,
    )

    if not results:
        print("\n[-] 未获取到任何数据")
        return

    # 保存原始响应
    for i, result in enumerate(results):
        save_raw_response(result, f"ihg_raw_response_{i}.json")

    # 解析数据
    print("\n[*] 解析价格数据...")
    all_prices = []
    for result in results:
        prices = parse_calendar_response(result)
        all_prices.extend(prices)

    # 导出
    if all_prices:
        export_to_csv(all_prices)
        export_to_json(all_prices)
    else:
        print("\n[!] 解析结果为空，请检查 ihg_raw_response_*.json 文件")
        print("    确认实际的响应结构后，调整 parse_calendar_response() 函数")

    print("\n" + "=" * 60)
    print("完成!")
    print("=" * 60)


if __name__ == "__main__":
    main()
