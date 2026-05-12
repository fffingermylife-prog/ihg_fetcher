# IHG Calendar Price Fetcher

获取 IHG 旗下酒店每天最低的现金价格和积分房价格，支持**全量抓取**和**人民币价格换算**。

## 📁 文件说明

| 文件 | 用途 | 适合场景 |
|------|------|---------|
| `ihg_hotel_list_fetcher.py` | **抓取 IHG 全球酒店目录** | 第一步, 生成 `ihg_hotels.csv` |
| `ihg_playwright_fetcher.py` | **Playwright 价格抓取** (主脚本) | 长期自动监控, cron 定时任务 |
| `ihg_calendar_browser.js` | 浏览器 Console 脚本 | 临时一次性抓取少数酒店 |
| `ihg_calendar_price.py` | 纯 Python + requests | 已有有效 cookie 时使用 |
| `ihg_diagnostic.js` | API 诊断工具 | 排查请求失败原因 |
| `ihg_extract_cookie.js` | 浏览器 Cookie 提取器 | 配合 Python 脚本用 |

## 🚀 完整工作流 (推荐)

### 第一步: 抓取酒店目录

```bash
# 安装依赖
pip install -r requirements.txt
playwright install chromium

# 全量抓取 (所有国家, 所有酒店)
python ihg_hotel_list_fetcher.py

# 或只抓特定国家
python ihg_hotel_list_fetcher.py --countries cn,th,jp

# 或只抓特定品牌 (IC=InterContinental)
python ihg_hotel_list_fetcher.py --brands IC --countries cn
```

生成 `ihg_hotels.csv`, 字段:
```csv
mnemonic,name,brand_code,brand_name,country,country_code,city,address,url
BKKHB,"InterContinental Bangkok",IC,"InterContinental",Thailand,th,Bangkok,"973 Phloen Chit Road...",https://...
```

### 第二步: 读取 CSV 抓价格

```bash
# 抓所有酒店的价格
python ihg_playwright_fetcher.py --hotels-csv ihg_hotels.csv

# 或限制: 只抓中国的 IC 品牌酒店
python ihg_playwright_fetcher.py --hotels-csv ihg_hotels.csv --country cn --brand IC

# 或限制数量 (测试用)
python ihg_playwright_fetcher.py --hotels-csv ihg_hotels.csv --limit 5
```

输出 `ihg_prices.csv`, 包含:
```csv
hotel_code,hotel_name,brand_code,brand_name,country,city,date,cash_price,cash_currency,cash_price_cny,points_price,cpp_cny_per_1k_points
BKKHB,InterContinental Bangkok,IC,InterContinental,Thailand,Bangkok,2026-05-13,5814.00,THB,1224.35,40000,30.61
```

## 💰 关于 CNY 汇率

- 汇率来源: [Frankfurter API](https://frankfurter.dev) (免费, 无需 key, 基于欧洲央行数据)
- 启动时一次性拉取当天汇率缓存到内存
- `cash_price_cny` = 本地价 × 当日汇率
- `cpp_cny_per_1k_points` = 每 1000 积分兑换的 CNY 价值 = `cash_price_cny / points_price × 1000`
  - 越高越划算; 一般 IC/高端品牌日期灵活时能到 30+ CNY/1k, 低端品牌或周末可能只有 10-20

## 🔑 API 关键事实

**端点:** `POST https://apis.ihg.com/availability/v1/calendar`

**请求行为:**
- 不带 `rates` 字段 → 只返回**现金价** (`calendar[].lowestRate`)
- 带 `rates.ratePlanCodes` → 只返回**积分价** (`calendar[].offers[].totalPoints`)
- **不支持合并请求**, 必须分 2 次 (已通过诊断脚本确认)

**限制:**
- `startDate` 必须 ≥ 今天, 否则返回 `50027 Invalid system range`
- 单次最多约 60 天的日期窗口
- Akamai 反爬, 必须通过浏览器上下文调用

## ⏰ 定时任务 (每天自动抓一次)

```bash
# crontab -e
# 每天凌晨 3 点抓最新价格
0 3 * * * cd /home/you/ihg && /usr/bin/python3 ihg_playwright_fetcher.py --hotels-csv ihg_hotels.csv --output ihg_prices_$(date +\%Y\%m\%d).csv >> ihg.log 2>&1

# 每周一刷新一次酒店列表 (新开业酒店偶尔会加入)
0 2 * * 1 cd /home/you/ihg && /usr/bin/python3 ihg_hotel_list_fetcher.py >> ihg_list.log 2>&1
```

## 🛠️ 命令行参数

### `ihg_hotel_list_fetcher.py`

```
--countries cn,th,jp    只抓指定国家 (国家代码)
--brands IC,HI          只保留指定品牌
--details               额外访问每个酒店详情页抓地址 (很慢)
--limit 100             最多抓多少个酒店
--output custom.csv     自定义输出路径
```

### `ihg_playwright_fetcher.py`

```
--hotels-csv FILE       从 CSV 读取酒店列表
--codes BKKHB,NYCHA     直接指定代码(逗号分隔)
--country cn            从 CSV 过滤国家
--brand IC              从 CSV 过滤品牌
--limit 10              最多抓多少酒店
--output PATH           自定义输出 CSV
```

## 🛠️ 故障排查

| 现象 | 原因 | 解决 |
|------|------|------|
| `50027 Invalid system range` | startDate 是过去 | 脚本已用"明天"作起点 |
| HTTP 403 | Akamai 拦截 | 用 Playwright 方案 |
| HTTP 400 | session 失效 | 删掉 `ihg_browser_profile/` 重跑 |
| 酒店列表为空 | IHG 页面结构变化 | 开 `HEADLESS=False` 观察 |
| 汇率加载失败 | 网络问题 | 会降级为不输出 CNY, 只保留本地价 |
