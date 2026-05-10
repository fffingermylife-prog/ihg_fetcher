"""
IHG Hotel Calendar Price Fetcher
获取 IHG 旗下酒店的日历房每天最低现金价格

实际 API 响应结构:
{
    "data": {
        "hotels": [{
            "hotel": { "brandCode": "IC", "hotelMnemonic": "BKKHB", "propertyCurrency": "THB" },
            "calendar": [{
                "start": "2026-05-09",
                "end": "2026-05-09",
                "lowestRate": {
                    "totalAmount": "6270.00",
                    "averageDailyAmount": "6270.00",
                    "currency": "THB",
                    "refIds": [30, 34]
                }
            }, ...]
        }]
    }
}

使用方法:
1. 在浏览器中打开 IHG 酒店页面，通过 DevTools 获取有效的 cookie
2. 将 cookie 填入 COOKIES 变量
3. 修改 HOTEL_CODES 列表添加你需要查询的酒店代码
4. 运行脚本: python ihg_calendar_price.py

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

# API Key (从前端JS中提取)
API_KEY = "se9ym5iAzaW8pxfBjkmgbuGjJcr3Pj6Y"

# 需要从浏览器 DevTools 中获取的 Cookie
# 打开 IHG 网站 -> F12 -> Network -> 找到 calendar 请求 -> 复制 Cookie 头
COOKIES = ""  # 粘贴你的完整 cookie 字符串

# 要查询的酒店代码列表
# 可以从 IHG 网站 URL 中获取，如 qSlH=BKKHB 表示酒店代码为 BKKHB
HOTEL_CODES = [
    "BKKHB",   # InterContinental Bangkok
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
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
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
        """构建请求体"""
        return {
            "hotelMnemonics": hotel_codes,
            "startDate": start_date,
            "endDate": end_date,
            "lengthOfStay": length_of_stay,
            "guestCounts": [
                {
                    "otaCode": "AQC10",
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
        """获取日历价格数据"""
        session_id, transaction_id = self._generate_ids()

        headers = {
            "ihg-sessionid": session_id,
            "ihg-transactionid": transaction_id,
        }

        payload = self.build_payload(
            hotel_codes, start_date, end_date, length_of_stay, adults
        )

        print(f"[*] 请求酒店: {hotel_codes}")
        print(f"    日期范围: {start_date} ~ {end_date}")

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
        """批量获取多个酒店的价格数据"""
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
    解析日历 API 响应，提取每天最低价格

    实际响应路径: data -> hotels[] -> hotel + calendar[]
    每天价格路径: calendar[].lowestRate.totalAmount

    返回:
    [
        {
            "hotel_code": "BKKHB",
            "brand_code": "IC",
            "currency": "THB",
            "date": "2026-05-09",
            "lowest_price": 6270.00,
            "average_daily_price": 6270.00,
        },
        ...
    ]
    """
    parsed_results = []

    if not response_data:
        return parsed_results

    # 实际结构: { "data": { "hotels": [...] } }
    data = response_data.get("data", {})
    hotels = data.get("hotels", [])

    if not hotels:
        print("[!] 响应中未找到 data.hotels")
        print(f"    顶层键: {list(response_data.keys())}")
        if "data" in response_data:
            print(f"    data 层键: {list(response_data['data'].keys())}")
        return parsed_results

    for hotel_entry in hotels:
        # 酒店基本信息
        hotel_info = hotel_entry.get("hotel", {})
        hotel_code = hotel_info.get("hotelMnemonic", "UNKNOWN")
        brand_code = hotel_info.get("brandCode", "")
        property_currency = hotel_info.get("propertyCurrency", "")

        # 日历数据
        calendar = hotel_entry.get("calendar", [])

        for day_data in calendar:
            date = day_data.get("start", "")
            lowest_rate = day_data.get("lowestRate", {})

            if lowest_rate:
                total_amount = lowest_rate.get("totalAmount")
                avg_daily_amount = lowest_rate.get("averageDailyAmount")
                currency = lowest_rate.get("currency", property_currency)

                # 转为浮点数
                try:
                    total_amount = float(total_amount) if total_amount else None
                except (ValueError, TypeError):
                    total_amount = None

                try:
                    avg_daily_amount = float(avg_daily_amount) if avg_daily_amount else None
                except (ValueError, TypeError):
                    avg_daily_amount = None

                parsed_results.append({
                    "hotel_code": hotel_code,
                    "brand_code": brand_code,
                    "currency": currency,
                    "date": date,
                    "lowest_price": total_amount,
                    "average_daily_price": avg_daily_amount,
                })
            else:
                # 当天无可用房间
                parsed_results.append({
                    "hotel_code": hotel_code,
                    "brand_code": brand_code,
                    "currency": property_currency,
                    "date": date,
                    "lowest_price": None,
                    "average_daily_price": None,
                })

    return parsed_results


def export_to_csv(data: List[dict], filename: str = "ihg_prices.csv"):
    """导出价格数据到 CSV 文件"""
    if not data:
        print("[-] 没有数据可导出")
        return

    fieldnames = ["hotel_code", "brand_code", "currency", "date", "lowest_price", "average_daily_price"]

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
    """保存原始 API 响应（用于调试）"""
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(response_data, f, indent=2, ensure_ascii=False)
    print(f"[+] 原始响应已保存到: {filename}")


def main():
    """主函数"""
    print("=" * 60)
    print("IHG Hotel Calendar Price Fetcher")
    print("获取每天最低现金价格")
    print("=" * 60)

    if not COOKIES:
        print("\n[!] 警告: 未设置 Cookie!")
        print("    请从浏览器 DevTools 中获取 Cookie 并填入脚本顶部的 COOKIES 变量")
        print("    步骤:")
        print("    1. 打开 https://www.ihg.com/ 并搜索酒店")
        print("    2. 打开 DevTools (F12) -> Network 面板")
        print("    3. 找到 POST apis.ihg.com/availability/v1/calendar 请求")
        print("    4. 复制请求头中的 Cookie 值")
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
        batch_size=1,
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

    # 打印预览
    if all_prices:
        print(f"\n[*] 价格预览 (前10条):")
        print(f"    {'酒店':<8} {'日期':<12} {'最低价':<12} {'货币'}")
        print(f"    {'-'*8} {'-'*12} {'-'*12} {'-'*4}")
        for row in all_prices[:10]:
            price_str = f"{row['lowest_price']:.2f}" if row['lowest_price'] else "N/A"
            print(f"    {row['hotel_code']:<8} {row['date']:<12} {price_str:<12} {row['currency']}")

        # 导出
        export_to_csv(all_prices)
        export_to_json(all_prices)
    else:
        print("\n[!] 解析结果为空，请检查 ihg_raw_response_*.json 文件")

    print("\n" + "=" * 60)
    print("完成!")
    print("=" * 60)


if __name__ == "__main__":
    main()
