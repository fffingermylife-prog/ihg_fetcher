"""
IHG Hotel Calendar Price Fetcher
获取 IHG 旗下酒店日历所有开放日期的现金最低价和积分价格

API 端点: POST https://apis.ihg.com/availability/v1/calendar

现金价格响应结构:
    data.hotels[].calendar[].lowestRate.totalAmount

积分价格响应结构 (payload 加 rates.ratePlanCodes):
    data.hotels[].calendar[].offers[].totalPoints  (ratePlanCode 为 IVAN*)

功能:
- 自动滑动窗口: 获取未来 N 天的全部日历价格（单次请求上限 ~60 天）
- 智能合并请求: 先尝试一次请求同时拿现金+积分，失败则自动回退到两次请求

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
from datetime import date, datetime, timedelta
from typing import List, Dict, Optional, Tuple


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

# ====== 查询范围模式 ======
# 模式A: 固定日期范围 (SLIDING_WINDOW=False)
# 注意: startDate 必须 >= 今天, 否则会报 50027 Invalid system range
START_DATE = "2026-06-01"
END_DATE = "2026-07-31"

# 模式B: 滑动窗口获取 "日历开放的所有日期" (SLIDING_WINDOW=True)
SLIDING_WINDOW = True         # True = 自动滑动窗口获取未来 N 天
DAYS_AHEAD = 365              # 获取从今天起未来多少天 (IHG 一般开放 ~330-500 天)
WINDOW_SIZE_DAYS = 60         # 每次请求的日期窗口大小 (IHG 上限 ~60 天)

# 住宿天数
LENGTH_OF_STAY = 1

# 成人数
ADULTS = 1

# 请求间隔(秒)，避免被限流
REQUEST_DELAY = 2

# 积分房 Rate Plan Codes (IVAN* 系列)
POINTS_RATE_PLAN_CODES = ["IVAN1", "IVAN3", "IVAN5", "IVAN6", "IVAN7", "IVANI"]

# 是否先尝试合并请求 (一次请求同时获取现金+积分)
# True: 先试合并, 若失败自动回退双请求
# False: 直接用双请求 (现金/积分分开) ← 推荐, 诊断已确认 IHG 不支持合并
# 注意: 诊断测试确认 IHG API 的行为:
#   - 不带 rates → 只返回现金价, 不返回积分
#   - 带 rates.ratePlanCodes → 只返回积分价, 不返回现金
#   所以合并请求不可行, 默认关闭以节省一次浪费的验证请求
TRY_COMBINED_REQUEST = False

# ============ 配置结束 ============


class IHGCalendarFetcher:
    """IHG 日历价格获取器"""

    BASE_URL = "https://apis.ihg.com/availability/v1/calendar"

    def __init__(self, api_key: str, cookies: str = ""):
        self.api_key = api_key
        self.cookies = cookies
        self.session = requests.Session()
        self._setup_session()
        # 记录合并请求是否成功，避免后续无意义重试
        self._combined_mode_works: Optional[bool] = None

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
                {"otaCode": "AQC10", "count": adults}
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
            payload["rates"] = {"ratePlanCodes": POINTS_RATE_PLAN_CODES}
        return payload

    def _post(self, payload: dict, tag: str = "") -> Optional[dict]:
        """执行一次 POST 请求"""
        session_id, transaction_id = self._generate_ids()
        headers = {
            "ihg-sessionid": session_id,
            "ihg-transactionid": transaction_id,
        }

        try:
            response = self.session.post(
                self.BASE_URL, headers=headers, json=payload, timeout=30
            )
            if response.status_code == 200:
                print(f"[+] {tag} 请求成功")
                return response.json()
            else:
                print(f"[-] {tag} 请求失败: HTTP {response.status_code}")
                print(f"    响应: {response.text[:300]}")
                return None
        except requests.exceptions.RequestException as e:
            print(f"[-] {tag} 请求异常: {e}")
            return None

    def fetch_window(
        self,
        hotel_code: str,
        start_date: str,
        end_date: str,
        length_of_stay: int = 1,
        adults: int = 1,
        delay: float = 2.0,
    ) -> Tuple[Optional[dict], Optional[dict]]:
        """
        获取单个酒店在一个日期窗口内的价格数据

        策略:
            1. 如果 TRY_COMBINED_REQUEST=True 且合并模式未被证伪 -> 先试合并请求
            2. 合并请求验证: 响应中同时有 lowestRate 和 IVAN* 的 offer -> 成功
            3. 合并失败或被证伪 -> 拆成两个请求

        返回: (cash_response, points_response)
               合并模式成功时, 两个都指向同一个响应对象
        """
        # -------- 尝试合并请求 --------
        if TRY_COMBINED_REQUEST and self._combined_mode_works is not False:
            payload = self.build_payload(
                [hotel_code], start_date, end_date, length_of_stay, adults,
                points_mode=True  # 加上 rates.ratePlanCodes
            )
            tag = f"[{hotel_code} {start_date}~{end_date} 合并]"
            print(f"[*] {tag} 请求...")
            data = self._post(payload, tag)

            if data and self._has_both_cash_and_points(data):
                if self._combined_mode_works is None:
                    print(f"[✓] 合并模式验证成功，后续窗口将继续使用合并请求")
                self._combined_mode_works = True
                return data, data  # 同一份数据既包含现金也包含积分

            # 合并请求没拿到完整数据, 回退
            if self._combined_mode_works is None:
                print(f"[!] 合并请求未返回完整现金+积分数据，回退到双请求模式")
                self._combined_mode_works = False
            time.sleep(delay)

        # -------- 拆成两个请求 --------
        # 1) 现金请求
        cash_payload = self.build_payload(
            [hotel_code], start_date, end_date, length_of_stay, adults, points_mode=False
        )
        print(f"[*] [{hotel_code} {start_date}~{end_date} 现金] 请求...")
        cash_data = self._post(cash_payload, f"[{hotel_code} 现金]")

        time.sleep(delay)

        # 2) 积分请求
        pts_payload = self.build_payload(
            [hotel_code], start_date, end_date, length_of_stay, adults, points_mode=True
        )
        print(f"[*] [{hotel_code} {start_date}~{end_date} 积分] 请求...")
        pts_data = self._post(pts_payload, f"[{hotel_code} 积分]")

        return cash_data, pts_data

    @staticmethod
    def _has_both_cash_and_points(response: dict) -> bool:
        """判断一个响应中是否同时包含现金价格和积分价格"""
        if not response:
            return False
        hotels = response.get("data", {}).get("hotels", [])
        if not hotels:
            return False

        has_cash = False
        has_points = False

        for h in hotels:
            # 判断是否有现金价格
            for day in h.get("calendar", []):
                if day.get("lowestRate"):
                    has_cash = True
                # 判断是否有积分 offer
                for offer in day.get("offers", []):
                    rp_code = offer.get("ratePlanCode", "")
                    if rp_code.startswith("IVAN") and offer.get("totalPoints") is not None:
                        has_points = True
                if has_cash and has_points:
                    return True

        return has_cash and has_points


def iter_date_windows(
    start_date: date, total_days: int, window_size: int
) -> List[Tuple[str, str]]:
    """
    生成滑动窗口日期区间列表

    例如: start=2026-05-12, total=180, window=60
    → [(2026-05-12, 2026-07-11), (2026-07-12, 2026-09-10), (2026-09-11, 2026-11-08)]
    """
    windows = []
    current = start_date
    end_target = start_date + timedelta(days=total_days - 1)

    while current <= end_target:
        window_end = min(current + timedelta(days=window_size - 1), end_target)
        windows.append((current.isoformat(), window_end.isoformat()))
        current = window_end + timedelta(days=1)

    return windows


def parse_cash_response(response_data: dict) -> List[dict]:
    """
    解析现金价格

    路径: data.hotels[].calendar[].lowestRate.totalAmount
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

        for day_data in hotel_entry.get("calendar", []):
            date_str = day_data.get("start", "")
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
                "date": date_str,
                "cash_price": cash_price,
                "cash_currency": currency,
            })

    return results


def parse_points_response(response_data: dict) -> List[dict]:
    """
    解析积分价格

    路径: data.hotels[].calendar[].offers[].totalPoints
    只取 ratePlanCode 为 IVAN* 或 isRewardNight=true 的 offer
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

        # 识别积分 rate plan codes
        reward_plan_codes = set()
        for rp in hotel_entry.get("ratePlans", []):
            if rp.get("isRewardNight", False):
                reward_plan_codes.add(rp.get("code", ""))

        for day_data in hotel_entry.get("calendar", []):
            date_str = day_data.get("start", "")
            offers = day_data.get("offers", [])

            lowest_points = None
            for offer in offers:
                rp_code = offer.get("ratePlanCode", "")
                is_points_offer = (rp_code in reward_plan_codes) or rp_code.startswith("IVAN")

                if is_points_offer:
                    total_points = offer.get("totalPoints")
                    if total_points is not None:
                        try:
                            v = float(total_points)
                            if lowest_points is None or v < lowest_points:
                                lowest_points = v
                        except (ValueError, TypeError):
                            pass

            results.append({
                "hotel_code": hotel_code,
                "brand_code": brand_code,
                "date": date_str,
                "points_price": lowest_points,
            })

    return results


def merge_cash_and_points(
    cash_prices: List[dict], points_prices: List[dict]
) -> List[dict]:
    """合并现金和积分价格到同一行"""
    cash_map = {(r["hotel_code"], r["date"]): r for r in cash_prices}
    points_map = {(r["hotel_code"], r["date"]): r for r in points_prices}

    all_keys = set(list(cash_map.keys()) + list(points_map.keys()))
    merged = []

    for key in sorted(all_keys):
        hotel_code, date_str = key
        cash_row = cash_map.get(key, {})
        points_row = points_map.get(key, {})

        cash_price = cash_row.get("cash_price")
        points_price = points_row.get("points_price")

        # 计算每积分兑换价值 (CPP - cents per point)
        # 如果有现金价和积分价, 算一下性价比
        cpp = None
        if cash_price and points_price and points_price > 0:
            cpp = round(cash_price / points_price * 100, 4)  # 每积分值多少分(货币单位)

        merged.append({
            "hotel_code": hotel_code,
            "brand_code": cash_row.get("brand_code", points_row.get("brand_code", "")),
            "date": date_str,
            "cash_price": cash_price,
            "cash_currency": cash_row.get("cash_currency", ""),
            "points_price": points_price,
            "cents_per_point": cpp,  # 每积分价值(货币最小单位)
        })

    return merged


def export_to_csv(data: List[dict], filename: str = "ihg_prices.csv"):
    """导出价格数据到 CSV 文件"""
    if not data:
        print("[-] 没有数据可导出")
        return

    fieldnames = [
        "hotel_code", "brand_code", "date",
        "cash_price", "cash_currency", "points_price", "cents_per_point"
    ]

    with open(filename, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(data)

    print(f"[+] 数据已导出到: {filename} ({len(data)} 条)")


def export_to_json(data: List[dict], filename: str = "ihg_prices.json"):
    """导出价格数据到 JSON 文件"""
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"[+] 数据已导出到: {filename}")


def save_raw_response(response_data: dict, filename: str):
    """保存原始 API 响应"""
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(response_data, f, indent=2, ensure_ascii=False)


def main():
    print("=" * 60)
    print("IHG Calendar Price Fetcher - 全量版本")
    print("获取每天最低现金价 + 积分价 (滑动窗口 + 智能合并)")
    print("=" * 60)

    if not COOKIES:
        print("\n[!] 警告: 未设置 Cookie! 可能会被服务器拒绝")
        print("    请从浏览器 DevTools 复制 Cookie 并填入脚本顶部的 COOKIES 变量\n")

    # ---- 计算所有查询窗口 ----
    if SLIDING_WINDOW:
        # 从明天开始,避免 "Invalid system range" (50027) 错误
        # IHG API 不接受今天之前或今天的日期作为 startDate (部分时区)
        start = date.today() + timedelta(days=1)
        windows = iter_date_windows(start, DAYS_AHEAD, WINDOW_SIZE_DAYS)
        print(f"\n[*] 滑动窗口模式")
        print(f"    总跨度: {DAYS_AHEAD} 天 (从 {start.isoformat()})")
        print(f"    窗口数: {len(windows)} 个 (每窗口 ~{WINDOW_SIZE_DAYS} 天)")
    else:
        windows = [(START_DATE, END_DATE)]
        print(f"\n[*] 固定日期模式: {START_DATE} ~ {END_DATE}")

    print(f"    酒店数: {len(HOTEL_CODES)}")
    print(f"    合并请求优先: {'是 (失败自动回退)' if TRY_COMBINED_REQUEST else '否 (直接双请求)'}")
    print()

    fetcher = IHGCalendarFetcher(api_key=API_KEY, cookies=COOKIES)

    all_cash_prices = []
    all_points_prices = []
    raw_snapshots = []

    total_requests = len(HOTEL_CODES) * len(windows)
    request_idx = 0

    for hotel_idx, hotel_code in enumerate(HOTEL_CODES):
        print(f"\n{'='*50}")
        print(f"[酒店 {hotel_idx+1}/{len(HOTEL_CODES)}] {hotel_code}")
        print(f"{'='*50}")

        for win_idx, (win_start, win_end) in enumerate(windows):
            request_idx += 1
            print(f"\n--- 窗口 {win_idx+1}/{len(windows)} "
                  f"({win_start} ~ {win_end}) [总进度 {request_idx}/{total_requests}] ---")

            cash_data, pts_data = fetcher.fetch_window(
                hotel_code, win_start, win_end,
                LENGTH_OF_STAY, ADULTS, REQUEST_DELAY
            )

            if cash_data:
                all_cash_prices.extend(parse_cash_response(cash_data))
            if pts_data:
                all_points_prices.extend(parse_points_response(pts_data))

            # 存快照 (只存第一个窗口的, 避免文件太多)
            if win_idx == 0:
                if cash_data:
                    raw_snapshots.append({
                        "hotel": hotel_code,
                        "window": f"{win_start}~{win_end}",
                        "type": "cash",
                        "data": cash_data,
                    })
                if pts_data and pts_data is not cash_data:
                    raw_snapshots.append({
                        "hotel": hotel_code,
                        "window": f"{win_start}~{win_end}",
                        "type": "points",
                        "data": pts_data,
                    })

            # 窗口间间隔
            if not (hotel_idx == len(HOTEL_CODES) - 1 and win_idx == len(windows) - 1):
                time.sleep(REQUEST_DELAY)

    # ---- 合并 & 导出 ----
    merged_prices = merge_cash_and_points(all_cash_prices, all_points_prices)

    if merged_prices:
        print(f"\n[*] 价格预览 (前 10 条):")
        print(f"    {'酒店':<8} {'日期':<12} {'现金价':<10} {'货币':<6} {'积分价':<10} {'CPP'}")
        print(f"    {'-'*8} {'-'*12} {'-'*10} {'-'*6} {'-'*10} {'-'*6}")
        for row in merged_prices[:10]:
            cash_str = f"{row['cash_price']:.2f}" if row['cash_price'] else "N/A"
            pts_str = f"{int(row['points_price'])}" if row['points_price'] else "N/A"
            cpp_str = f"{row['cents_per_point']}" if row['cents_per_point'] is not None else "N/A"
            print(f"    {row['hotel_code']:<8} {row['date']:<12} {cash_str:<10} "
                  f"{row['cash_currency']:<6} {pts_str:<10} {cpp_str}")

        export_to_csv(merged_prices, "ihg_prices.csv")
        export_to_json(merged_prices, "ihg_prices.json")
    else:
        print("\n[!] 解析结果为空")

    # 保存一份原始响应样本, 便于调试
    if raw_snapshots:
        save_raw_response(raw_snapshots, "ihg_raw_snapshots.json")
        print(f"[+] 原始响应样本已保存到: ihg_raw_snapshots.json")

    print("\n" + "=" * 60)
    print(f"完成! 共 {len(merged_prices)} 条每日价格记录")
    print("=" * 60)


if __name__ == "__main__":
    main()
