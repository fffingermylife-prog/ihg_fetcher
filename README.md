# IHG Calendar Price Fetcher

获取 IHG 旗下酒店每天最低的现金价格和积分房价格。

## 📁 文件说明

| 文件 | 用途 | 适合场景 |
|------|------|---------|
| `ihg_playwright_fetcher.py` | **Playwright 自动化** ⭐推荐 | 长期自动监控, cron 定时任务 |
| `ihg_calendar_browser.js` | 浏览器 Console 脚本 | 临时一次性抓取 |
| `ihg_calendar_price.py` | 纯 Python + requests | 已有有效 cookie 时使用 |
| `ihg_diagnostic.js` | API 诊断工具 | 排查请求失败原因 |
| `ihg_extract_cookie.js` | 浏览器 Cookie 提取器 | 配合 Python 脚本用 |

## 🔑 API 关键事实

**端点:** `POST https://apis.ihg.com/availability/v1/calendar`

**请求行为:**
- 不带 `rates` 字段 → 只返回**现金价** (`calendar[].lowestRate`)
- 带 `rates.ratePlanCodes` → 只返回**积分价** (`calendar[].offers[].totalPoints`)
- **不支持合并请求**, 必须分 2 次

**限制:**
- `startDate` 必须 ≥ 今天, 否则返回 `50027 Invalid system range`
- 单次最多约 60 天的日期窗口
- 有 Akamai 反爬, **不能在普通服务器直接 curl 调用**

## 🚀 推荐方案: Playwright 自动化

### 1. 安装依赖

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. 配置

编辑 `ihg_playwright_fetcher.py` 顶部的配置区:

```python
HOTEL_CODES = ["BKKHB", "NYCHA", "SFOHA"]  # 要抓的酒店列表
DAYS_AHEAD = 365                            # 未来多少天
WINDOW_SIZE_DAYS = 60                       # 窗口大小
HEADLESS = True                             # 后台静默模式
```

### 3. 运行

```bash
python ihg_playwright_fetcher.py
```

首次运行会:
1. 启动 Chromium (headless)
2. 访问 IHG select-roomrate 页面建立合法 session
3. 在浏览器上下文里用 fetch 调用 API (自动携带所有 cookie)
4. 按滑动窗口抓取未来 365 天的所有价格
5. 输出:
   - `ihg_prices.csv` - 合并后的每日价格
   - `ihg_prices.json` - 同上 JSON 格式
   - `ihg_raw_snapshots.json` - 每酒店第一窗口的原始响应 (调试用)
   - `ihg_browser_profile/` - 浏览器 profile 目录, 下次复用

### 4. 定时任务

```bash
# crontab -e, 每天凌晨 3 点抓一次
0 3 * * *  cd /path/to/script && /usr/bin/python3 ihg_playwright_fetcher.py >> ihg.log 2>&1
```

## 📊 输出 CSV 格式

```csv
hotel_code,brand_code,date,cash_price,cash_currency,points_price,cents_per_point
BKKHB,IC,2026-05-13,5814.00,THB,40000,14.535
BKKHB,IC,2026-05-14,6270.00,THB,44000,14.25
```

- `cents_per_point` = 现金价 / 积分价 × 100, 越高越划算兑换

## 🛠️ 故障排查

| 现象 | 原因 | 解决 |
|------|------|------|
| `50027 Invalid system range` | startDate 是过去 | 脚本已自动用"明天"作为起点 |
| HTTP 403 | Akamai 拦截 | 用 Playwright 方案, 别用纯 Python |
| HTTP 400 | session 失效或 payload 错误 | 删掉 `ihg_browser_profile/` 重新运行 |
| 某些酒店 ratePlanCodes 不匹配 | IVAN* 对该酒店不全适用 | 脚本的 IVANI 是通用的, 一般能拿到 |

## 🎯 替代方案

### 浏览器 Console 一次性抓取

在 IHG 任意页面按 F12, 打开 Console, 粘贴 `ihg_calendar_browser.js` 内容:
- 自动用浏览器当前 cookie
- 自动滑动窗口
- 自动下载 CSV

## 📝 API 字段映射

### 现金响应解析路径
```
data.hotels[].hotel.hotelMnemonic        → hotel_code
data.hotels[].hotel.brandCode            → brand_code
data.hotels[].calendar[].start           → date
data.hotels[].calendar[].lowestRate.totalAmount  → cash_price
data.hotels[].calendar[].lowestRate.currency     → cash_currency
```

### 积分响应解析路径
```
data.hotels[].ratePlans[] (找 isRewardNight=true)  → 识别积分 plan codes
data.hotels[].calendar[].offers[] (ratePlanCode 匹配)
  → .totalPoints                         → points_price (取最低)
```
