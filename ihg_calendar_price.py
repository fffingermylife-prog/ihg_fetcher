"""
IHG Hotel Calendar Price Fetcher
获取 IHG 旗下酒店的日历房每天最低现金价格和积分价格

API 端点: POST https://apis.ihg.com/availability/v1/calendar

现金价格响应结构:
    data.hotels[].calendar[].lowestRate.totalAmount

积分价格响应结构 (payload 加 rates.ratePlanCodes):
    data.hotels[].calendar[].offers[].totalPoints
    (ratePlanCode 为 IVAN* 的 offer)

使用方法:
1. 在浏览器中打开 IHG 酒店页面，通过 DevTools 获取有效的 cookie
2. 将 cookie 填入 COOKIES 变量
3. 修改 HOTEL_CODES 列表添加你需要查询的酒店代码
4. 运行脚本: python ihg_calendar_price.py
"""

import requests
import json
import csv
import uuid
import time
from typing import List, Dict, Optional


# ============ 配置区域 ============

# API Key (从前端JS中提取)
API_KEY = "se9ym5iAzaW8pxfBjkmgbuGjJcr3Pj6Y"

# 需要从浏览器 DevTools 中获取的 Cookie
COOKIES = ""  # 粘贴你的完整 cookie 字符串

# 要查询的酒店代码列表
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

# 积分房 Rate Plan Codes
POINTS_RATE_PLAN_CODES = ["IVAN1", "IVAN3", "IVAN5", "IVAN6", "IVAN7", "IVANI"]

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
        return str(uuid.uuid4()), str(uuid.uuid4())

    def build_payload(
        self,
        hotel_codes: List[str],
        start_date: str,
        end_date: str,
        length_of_stay: int = 1,
        adults: int = 1,
        points_mode: bool = False,
    ) -> dict:
        """
        构建请求体

        参数:
            points_mode: True 则添加 rates.ratePlanCodes 获取积分价格
        """
        payload = {
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

        if points_mode:
            payload["rates"] = {
                "ratePlanCodes": POINTS_RATE_PLAN_CODES
            }

        return payload

    def fetch_calendar(
        self,
        hotel_codes: List[str],
        start_date: str,
        end_date: str,
        length_of_stay: int = 1,
        adults: int = 1,
        points_mode: bool = False,
    ) -> Optional[dict]:
        """获取日历价格数据"""
        session_id, transaction_id = self._generate_ids()

        headers = {
            "ihg-sessionid": session_id,
            "ihg-transactionid": transaction_id,
        }

        payload = self.build_payload(
            hotel_codes, start_date, end_date, length_of_stay, adults, points_mode
        )

        mode_str = "积分" if points_mode else "现金"
        print(f"[*] 请求酒店: {hotel_codes} ({mode_str})")
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

    def fetch_hotel_both_prices(
        self,
        hotel_code: str,
        start_date: str,
        end_date: str,
        length_of_stay: int = 1,
        adults: int = 1,
        delay: float = 2.0,
    ) -> Dict[str, Optional[dict]]:
        """获取单个酒店的现金和积分价格"""
        cash_data = self.fetch_calendar(
            [hotel_code], start_date, end_date, length_of_stay, adults, points_mode=False
        )

        time.sleep(delay)

        points_data = self.fetch_calendar(
            [hotel_code], start_date, end_date, length_of_stay, adults, points_mode=True
        )

        return {"cash": cash_data, "points": points_data}

    def fetch_multiple_hotels(
        self,
        hotel_codes: List[str],
        start_date: str,
        end_date: str,
        length_of_stay: int = 1,
        adults: int = 1,
        delay: float = 2.0,
    ) -> List[Dict[str, Optional[dict]]]:
        """批量获取多个酒店的现金+积分价格"""
        results = []

        for i, hotel_code in enumerate(hotel_codes):
            print(f"\n{'='*40}")
            print(f"[{i+1}/{len(hotel_codes)}] 酒店: {hotel_code}")
            print(f"{'='*40}")

            data = self.fetch_hotel_both_prices(
                hotel_code, start_date, end_date, length_of_stay, adults, delay
            )
            results.append(data)

            if i < len(hotel_codes) - 1:
                print(f"    等待 {delay} 秒...")
                time.sleep(delay)

        return results


def parse_cash_response(response_data: dict) -> List[dict]:
    """
    解析现金价格响应

    路径: data.hotels[].calendar[].lowestRate.totalAmount

    返回: [{"hotel_code", "brand_code", "date", "cash_price", "cash_currency"}, ...]
    """
    results = []
    if not response_data:
        return results

    data = response_data.get("data", {})
    hotels = data.get("hotels", [])

    for hotel_entry in hotels:
        hotel_info = hotel_entry.get("hotel", {})
        hotel_code = hotel_info.get("hotelMnemonic", "UNKNOWN")
        brand_code = hotel_info.get("brandCode", "")
        property_currency = hotel_info.get("propertyCurrency", "")

        calendar = hotel_entry.get("calendar", [])

        for day_data in calendar:
            date = day_data.get("start", "")
            lowest_rate = day_data.get("lowestRate")

            cash_price = None
            currency = property_currency

            if lowest_rate:
                try:
                    cash_price = float(lowest_rate.get("totalAmount", 0))
                except (ValueError, TypeError):
                    cash_price = None
                currency = lowest_rate.get("currency", property_currency)

            results.append({
                "hotel_code": hotel_code,
                "brand_code": brand_code,
                "date": date,
                "cash_price": cash_price,
                "cash_currency": currency,
            })

    return results


def parse_points_response(response_data: dict) -> List[dict]:
    """
    解析积分价格响应

    路径: data.hotels[].calendar[].offers[]
    积分offer的特征: ratePlanCode 为 IVAN* 开头, isRewardNight=true
    价格字段: offers[].totalPoints

    返回: [{"hotel_code", "brand_code", "date", "points_price"}, ...]
    """
    results = []
    if not response_data:
        return results

    data = response_data.get("data", {})
    hotels = data.get("hotels", [])

    # 构建 reward rate plan 集合 (从 ratePlans 中找 isRewardNight=true 的)
    for hotel_entry in hotels:
        hotel_info = hotel_entry.get("hotel", {})
        hotel_code = hotel_info.get("hotelMnemonic", "UNKNOWN")
        brand_code = hotel_info.get("brandCode", "")

        # 找出积分 rate plan codes
        rate_plans = hotel_entry.get("ratePlans", [])
        reward_plan_codes = set()
        for rp in rate_plans:
            if rp.get("isRewardNight", False):
                reward_plan_codes.add(rp["code"])

        # 如果没有从 ratePlans 识别出来，使用默认的 IVAN* 前缀判断
        calendar = hotel_entry.get("calendar", [])

        for day_data in calendar:
            date = day_data.get("start", "")
            offers = day_data.get("offers", [])

            # 找当天最低积分价格
            lowest_points = None

            for offer in offers:
                rate_plan_code = offer.get("ratePlanCode", "")

                # 判断是否是积分 offer
                is_points_offer = (
                    rate_plan_code in reward_plan_codes or
                    rate_plan_code.startswith("IVAN")
                )

                if is_points_offer:
                    total_points = offer.get("totalPoints")
                    if total_points is not None:
                        try:
                            points_val = float(total_points)
                            if lowest_points is None or points_val < lowest_points:
                                lowest_points = points_val
                        except (ValueError, TypeError):
                            pass

            results.append({
                "hotel_code": hotel_code,
                "brand_code": brand_code,
                "date": date,
                "points_price": lowest_points,
            })

    return results


def merge_cash_and_points(cash_prices: List[dict], points_prices: List[dict]) -> List[dict]:
    """
    合并现金价格和积分价格到同一行

    返回:
    [
        {
            "hotel_code": "BKKHB",
            "brand_code": "IC",
            "date": "2026-05-09",
            "cash_price": 6270.00,
            "cash_currency": "THB",
            "points_price": 44000.0,
        },
        ...
    ]
    """
    cash_map = {}
    for row in cash_prices:
        key = (row["hotel_code"], row["date"])
        cash_map[key] = row

    points_map = {}
    for row in points_prices:
        key = (row["hotel_code"], row["date"])
        points_map[key] = row

    all_keys = set(list(cash_map.keys()) + list(points_map.keys()))
    merged = []

    for key in sorted(all_keys):
        hotel_code, date = key
        cash_row = cash_map.get(key, {})
        points_row = points_map.get(key, {})

        merged.append({
            "hotel_code": hotel_code,
            "brand_code": cash_row.get("brand_code", points_row.get("brand_code", "")),
            "date": date,
            "cash_price": cash_row.get("cash_price"),
            "cash_currency": cash_row.get("cash_currency", ""),
            "points_price": points_row.get("points_price"),
        })

    return merged


def export_to_csv(data: List[dict], filename: str = "ihg_prices.csv"):
    """导出价格数据到 CSV 文件"""
    if not data:
        print("[-] 没有数据可导出")
        return

    fieldnames = ["hotel_code", "brand_code", "date", "cash_price", "cash_currency", "points_price"]

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


def save_raw_response(response_data: dict, filename: str):
    """保存原始 API 响应（用于调试）"""
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(response_data, f, indent=2, ensure_ascii=False)
    print(f"[+] 原始响应已保存到: {filename}")


def main():
    """主函数"""
    print("=" * 60)
    print("IHG Hotel Calendar Price Fetcher")
    print("获取每天最低现金价格 + 积分价格")
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

    fetcher = IHGCalendarFetcher(api_key=API_KEY, cookies=COOKIES)

    print(f"\n[*] 开始获取 {len(HOTEL_CODES)} 个酒店的日历价格...")
    print(f"    日期范围: {START_DATE} ~ {END_DATE}")
    print(f"    每个酒店获取: 现金价格 + 积分价格")
    print()

    results = fetcher.fetch_multiple_hotels(
        hotel_codes=HOTEL_CODES,
        start_date=START_DATE,
        end_date=END_DATE,
        length_of_stay=LENGTH_OF_STAY,
        adults=ADULTS,
        delay=REQUEST_DELAY,
    )

    if not results:
        print("\n[-] 未获取到任何数据")
        return

    # 保存原始响应
    for i, result in enumerate(results):
        if result["cash"]:
            save_raw_response(result["cash"], f"ihg_raw_cash_{i}.json")
        if result["points"]:
            save_raw_response(result["points"], f"ihg_raw_points_{i}.json")

    # 解析数据
    print("\n[*] 解析价格数据...")
    all_cash_prices = []
    all_points_prices = []

    for result in results:
        if result["cash"]:
            all_cash_prices.extend(parse_cash_response(result["cash"]))
        if result["points"]:
            all_points_prices.extend(parse_points_response(result["points"]))

    # 合并
    merged_prices = merge_cash_and_points(all_cash_prices, all_points_prices)

    # 打印预览
    if merged_prices:
        print(f"\n[*] 价格预览 (前10条):")
        print(f"    {'酒店':<8} {'日期':<12} {'现金价':<12} {'货币':<6} {'积分价'}")
        print(f"    {'-'*8} {'-'*12} {'-'*12} {'-'*6} {'-'*10}")
        for row in merged_prices[:10]:
            cash_str = f"{row['cash_price']:.2f}" if row['cash_price'] else "N/A"
            pts_str = f"{int(row['points_price'])}" if row['points_price'] else "N/A"
            print(f"    {row['hotel_code']:<8} {row['date']:<12} {cash_str:<12} "
                  f"{row['cash_currency']:<6} {pts_str}")

        export_to_csv(merged_prices)
        export_to_json(merged_prices)
    else:
        print("\n[!] 解析结果为空，请检查原始响应文件")

    print("\n" + "=" * 60)
    print("完成!")
    print("=" * 60)


if __name__ == "__main__":
    main()
