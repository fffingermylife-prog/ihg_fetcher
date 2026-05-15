# IHG 酒店价格监控项目 - 会话总结

## 项目目标
分析 IHG（洲际酒店集团）网站，获取旗下酒店的日历现金最低价和积分兑换价格，计算积分性价比(CPP)，监控价格变动。

---

## 已完成的功能

### 1. 酒店列表抓取 (`ihg_hotel_list_fetcher.py`)
- 通过 `/explore` 页面展开区域 accordion，收集二级链接
- 支持 `--target` 指定国家/地区，`--region` 指定大区域
- 子区域递归：检测 "Hotels by State/Region" 区块深入收集
- 增量去重：已有 CSV 数据自动跳过
- 输出字段：mnemonic, name, brand_code, city, country, address, rating, review_count, url
- 结果按国家分组 + 评分排序

### 2. 日历价格获取 (`ihg_test_calendar_price.py`)
- Calendar API: POST `https://apis.ihg.com/availability/v1/calendar`
- 现金和积分**必须分两次请求**（payload 不同）
- 日期区间展开：API 返回合并的 start~end 区间，需逐天展开
- 含税价格：通过 `lowestRate.refIds` → `offers[id].totalAmountAfterFeeTax`
- CPP 计算：含税价 / 积分 × 100

### 3. 价格变动监控 (`ihg_price_monitor.py`)
- 读取基线 JSON → 全量获取 365 天 → 对比差异
- 检测：新开放 / 现金涨降 / 积分涨降 / 新增
- 首次运行无基线时只保存数据，不输出变化
- 支持 `--dry-run` 只看变化不保存

### 4. 调试工具 (`ihg_debug_calendar.py`)
- 简洁逐日表格输出（日期/现金/积分）
- 方便和官网日历对比验证

---

## 关键技术发现

| 项目 | 详情 |
|------|------|
| API Key | `se9ym5iAzaW8pxfBjkmgbuGjJcr3Pj6Y` |
| Calendar API | POST `https://apis.ihg.com/availability/v1/calendar` |
| 反爬 | 必须用 Playwright 有头浏览器（Akamai 拦截无头模式） |
| 现金 payload | `guestCounts`: AQC10+AQC8, 无 `includeSellStrategy`, 无 `rates` |
| 积分 payload | `guestCounts`: 只有 AQC10, 有 `includeSellStrategy: "followChannel"`, 有 `rates.ratePlanCodes` |
| 积分 codes | `["IVAN1","IVAN3","IVAN5","IVAN6","IVAN7","IVANI"]` |
| 日期区间 | API 返回合并区间（start/end），连续相同价格的天被压缩 |
| 窗口大小 | 62 天（和官网一致） |
| 最远日期 | 约 349 天（从今天算） |
| 新日期开放 | 大约 UTC 0:00（中国时间 08:00）每天新增一天 |
| 批量请求 | `hotelMnemonics` 传多个代码返回 400，**不可行** |
| 请求间隔 | 500ms（官网并行发请求，无需长停顿） |
| 含税价格 | `offers[refId].totalAmountAfterFeeTax` |
| 不含税价格 | `lowestRate.totalAmount` 或 `offers[refId].totalAmount` |

---

## 遇到的问题及解决

| 问题 | 原因 | 解决 |
|------|------|------|
| 无头模式 accordion 找不到 | Akamai 拦截无头浏览器 | 默认有头模式 |
| 中文版 /zh-cn/explore 子区域少 | 中文页面结构不同 | 用英文版 /explore |
| 某些日期现金价 null | API 返回合并的日期区间 | `expand_date_range()` 展开 start~end |
| 积分请求 HTTP 500 | payload 参数和官网不同 | 抓包对比,分开现金/积分 payload |
| 子区域收集超范围 | 整页扫描链接 | 限定 "Hotels by State/Region" 区块 |
| 页面加载超时 | 网络慢 | 90秒超时 + 重试3次 |
| 含税价格找不到 | 在 offers 数组里 | 通过 lowestRate.refIds 关联 offer |

---

## 仓库文件结构

```
fffingermylife-prog/test (分支: feat/ihg-calendar-price)
├── ihg_hotel_list_fetcher.py    # 正式版: 按国家抓取酒店列表
├── ihg_price_monitor.py         # 正式版: 价格变动监控 (每日运行)
├── ihg_test_calendar_price.py   # 测试: 单酒店全年价格获取
├── ihg_test_single_region.py    # 测试: 子区域递归酒店列表
├── ihg_debug_calendar.py        # 调试: Calendar API 原始响应查看
├── ihg_playwright_fetcher.py    # 旧版: 价格抓取 (已被新脚本替代)
├── ihg_calendar_price.py        # 旧版: 纯 Python + Cookie
├── ihg_calendar_browser.js      # 浏览器 Console 版
├── ihg_diagnostic.js            # API 诊断工具
├── ihg_extract_cookie.js        # Cookie 提取助手
├── requirements.txt             # playwright, requests
└── README.md                    # 使用文档
```

---

## 下一步计划

### 优先级 1: 多酒店监控
- `ihg_price_monitor.py` 支持 `--codes HPHHL,SGNVC,HANHC` 串行处理多个酒店
- 或从酒店列表 CSV/JSON 读取所有酒店代码
- 每酒店约 6 秒（500ms 间隔），100 酒店约 10 分钟

### 优先级 2: SQLite 存储
- 替代每酒店一个 JSON 文件
- 表结构: `hotel_code, date, cash_price, cash_after_tax, currency, points, cpp, fetch_date`
- 支持历史查询、趋势分析
- 一个 `ihg_prices.db` 文件存所有数据

### 优先级 3: 自动化定时任务
- 确定每日新日期开放的精确时间（需连续观察 2-3 天）
- 设置 cron/定时任务每日自动运行
- 可选：变价通知（邮件/微信）

### 优先级 4: 数据分析输出
- CPP 排行榜：哪些酒店积分性价比最高
- 价格趋势图
- 最佳预订时机建议

---

## 使用示例

```bash
# 1. 抓取越南酒店列表
python ihg_hotel_list_fetcher.py --target "Vietnam Hotels"

# 2. 获取单酒店全年价格
python ihg_test_calendar_price.py --code HPHHL --days 365

# 3. 监控价格变动
python ihg_price_monitor.py --code HPHHL

# 4. 调试某个日期范围
python ihg_debug_calendar.py --code HPHHL --start 2026-06-01 --end 2026-06-30
```

---

## 给新会话的提示
- GitHub 仓库: `fffingermylife-prog/test`，分支 `feat/ihg-calendar-price`
- 用户环境: Windows + Python 3.12 + Playwright
- 用户偏好: 中文沟通，代码注释用中文，喜欢简洁实用的方案
- 关键：不要猜测 IHG 的 URL/API 结构，所有参数都基于已验证的真实抓包
- Calendar API payload 现金和积分**不同**，不能共用同一个 payload
- API 返回的日期是合并区间（start~end），必须展开为逐天
