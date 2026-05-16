# IHG 酒店价格监控项目 - 会话总结

## 项目目标
分析 IHG（洲际酒店集团）网站，获取旗下酒店的日历现金最低价和积分兑换价格，计算积分性价比(CPP)，监控价格变动，降价/房态变化时自动推送微信通知。

---

## 已完成的功能

### 1. 酒店列表抓取 (`ihg_hotel_list_fetcher.py`)
- 通过 `/explore` 页面展开区域 accordion，收集二级链接
- 支持 `--target` 指定国家/地区，`--region` 指定大区域
- 子区域递归：检测 "Hotels by State/Region" 区块深入收集
- 增量去重：已有 CSV 数据自动跳过
- 输出字段：mnemonic, name, brand_code, city, country, address, rating, review_count, url
- 结果按国家分组 + 评分排序
- **同时写入 SQLite 数据库**

### 2. 多酒店批量监控 (`ihg_batch_monitor.py`)
- 多 Tab 并发 (默认 2, 最大 3)
- 全量模式: 获取 365 天完整价格, 对比所有变化
- 增量模式: 只获取最远 62 天窗口, 快速检测新开放日期
- 房态检测: 售罄/重新开放/积分房售罄/价格涨降
- 失败自动重试 + 超时跳过
- 随机请求间隔 (200~500ms) 降低被检测风险
- **支持 Clash 代理** (`--proxy http://127.0.0.1:7890`)
- **SQLite 存储** (保留所有历史记录)
- **自动触发微信通知** (满足告警条件时)
- 支持从 `--codes` / `--from-csv` / `--from-json` / `--from-db` 读取酒店列表

### 3. SQLite 数据库 (`ihg_db.py`)
- `hotels` 表: 酒店基本信息 (从 hotel_list_fetcher 写入)
- `prices` 表: 每日价格记录 (每次采集新增记录, 保留历史)
- 支持: 基线加载 / 价格历史查询 / 统计信息
- 可独立运行: `python ihg_db.py --stats` / `--hotels` / `--history`

### 4. 价格告警通知 (`ihg_notify.py`)
- Server酱微信推送
- 4级告警规则:
  - 🔴 积分房重新开放 (中国节假日 + 价格合理)
  - 🟠 积分降价 ≥ 40%
  - 🟡 现金降价 ≥ 40%
  - 🟢 新日期低价 (≤ 历史均价×0.6)
- 中国节假日识别 2026~2027 (含前后2天缓冲)
- 平日均价计算 (排除节假日+周末)
- 酒店名从数据库自动读取
- 配置文件: `notify_config.json`

### 5. 新日期开放时间探测 (`ihg_detect_open_time.py`)
- 定时启动 (默认 00:59, 支持 `--start-time` 自定义)
- 每 10~15 分钟随机间隔检查最远可预订日期
- 检测到新日期开放后自动停止
- 日志保存到 `ihg_open_time_<CODE>.log`

### 6. 旧版工具 (保留)
- `ihg_price_monitor.py` - 单酒店价格监控 (被 batch_monitor 替代)
- `ihg_test_calendar_price.py` - 单酒店全年价格获取
- `ihg_debug_calendar.py` - Calendar API 调试

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
| 新日期开放 | 约 UTC 23:00~0:00（中国时间 07:00~08:00）每天新增一天 |
| 批量请求 | `hotelMnemonics` 传多个代码返回 400，**不可行** |
| 请求间隔 | 随机 200~500ms（模拟真实用户行为） |
| 含税价格 | `offers[refId].totalAmountAfterFeeTax` |
| 不含税价格 | `lowestRate.totalAmount` 或 `offers[refId].totalAmount` |
| 浏览器内并发 | 同一 page 的 6 个 fetch 并发会被 Akamai 限流，**不可行** |
| Clash 代理 | 通过 `--proxy http://127.0.0.1:7890` 走 Clash，已验证可用 |

---

## 遇到的问题及解决

| 问题 | 原因 | 解决 |
|------|------|------|
| 无头模式 accordion 找不到 | Akamai 拦截无头浏览器 | 默认有头模式 |
| 中文版 /zh-cn/explore 子区域少 | 中文页面结构不同 | 用英文版 /explore |
| 某些日期现金价 null | API 返回合并的日期区间 | `expand_date_range()` 展开 start~end |
| 积分请求 HTTP 500 | payload 参数和官网不同 | 抓包对比,分开现金/积分 payload |
| 3 Tab 同时建立 session 卡死 | Akamai 检测同时导航 | 逐个 Tab 建立, 错开 1~2 秒 |
| Windows 命令行输出卡住 | Python stdout 缓冲 | `sys.stdout.reconfigure(line_buffering=True)` |
| 浏览器内 6 请求并发卡死 | Akamai 限流同 session 并发 | 回退为串行请求 |
| `No module named 'ihg_db'` | 从其他目录运行时找不到模块 | `sys.path.insert(0, Path(__file__).parent)` |
| CSV 编码读取失败 | 旧文件用其他编码保存 | 删除旧文件重新生成 |

---

## 仓库文件结构

```
fffingermylife-prog/test (分支: feat/ihg-calendar-price)
├── ihg_batch_monitor.py         # 核心: 多酒店批量价格监控 + 通知
├── ihg_hotel_list_fetcher.py    # 正式版: 按国家抓取酒店列表
├── ihg_notify.py                # 通知模块: Server酱微信推送
├── ihg_db.py                    # 数据库封装: SQLite 读写
├── ihg_detect_open_time.py      # 工具: 新日期开放时间探测
├── ihg_test_calendar_price.py   # 测试: 单酒店全年价格获取
├── ihg_debug_calendar.py        # 调试: Calendar API 原始响应查看
├── ihg_price_monitor.py         # 旧版: 单酒店监控 (已被 batch 替代)
├── ihg_test_single_region.py    # 测试: 子区域递归酒店列表
├── ihg_playwright_fetcher.py    # 旧版: 价格抓取
├── ihg_calendar_price.py        # 旧版: 纯 Python + Cookie
├── ihg_calendar_browser.js      # 浏览器 Console 版
├── ihg_diagnostic.js            # API 诊断工具
├── ihg_extract_cookie.js        # Cookie 提取助手
├── notify_config.json           # 通知配置 (Server酱 key + 规则)
├── requirements.txt             # playwright, requests
├── README.md                    # 使用文档
└── PROJECT_CONTEXT.md           # 本文件
```

---

## 使用示例

```bash
# 1. 抓取越南酒店列表 (同时写入 SQLite)
python ihg_hotel_list_fetcher.py --target "Vietnam Hotels"

# 2. 批量监控 (直连)
python ihg_batch_monitor.py --codes DADHA,HKGKL,HKGKH,HKGIN --concurrency 3

# 3. 批量监控 (走 Clash 代理)
python ihg_batch_monitor.py --codes DADHA,HKGKL --proxy http://127.0.0.1:7890

# 4. 从数据库读取酒店, 增量模式
python ihg_batch_monitor.py --from-db --incremental

# 5. 按国家监控
python ihg_batch_monitor.py --from-db --country "Vietnam" --proxy http://127.0.0.1:7890

# 6. 查看数据库统计
python ihg_db.py --stats

# 7. 查看某酒店某天价格历史
python ihg_db.py --history HKGKL:2026-10-01

# 8. 测试微信通知
python ihg_notify.py --test

# 9. 探测新日期开放时间 (00:59 自动开始)
python ihg_detect_open_time.py --code HKGKL

# 10. 查看节假日表
python ihg_notify.py --holidays
```

---

## 每日监控计划 (建议)

```
07:35  增量模式 (检测新日期开放)
       python ihg_batch_monitor.py --from-db --incremental

08:30  全量模式 (重点酒店, 完整对比)
       python ihg_batch_monitor.py --codes 重点酒店 --days 365

14:00  全量模式 (其余酒店前半, 走代理)
       python ihg_batch_monitor.py --from-db --days 180 --proxy http://127.0.0.1:7890

20:00  全量模式 (其余酒店后半, 走代理)
       python ihg_batch_monitor.py --from-db --days 180 --proxy http://127.0.0.1:7890
```

---

## 下一步计划

### 优先级 1: Clash 进阶 (自动切换节点)
- 通过 Clash RESTful API 每批酒店自动切换代理节点
- 降低单 IP 被限流的风险
- 支持 200+ 酒店大规模监控

### 优先级 2: 数据分析输出
- CPP 排行榜：哪些酒店积分性价比最高
- 价格趋势图
- 最佳预订时机建议

### 优先级 3: 确认新日期开放精确时间
- 运行 `ihg_detect_open_time.py` 连续 2~3 天
- 确认后调整定时任务时间

---

## 给新会话的提示
- GitHub 仓库: `fffingermylife-prog/test`，分支 `feat/ihg-calendar-price`
- 用户环境: Windows + Python 3.12 + Playwright + Clash 代理 (端口 7890)
- 用户偏好: 中文沟通，代码注释用中文，喜欢简洁实用的方案
- 关键：不要猜测 IHG 的 URL/API 结构，所有参数都基于已验证的真实抓包
- Calendar API payload 现金和积分**不同**，不能共用同一个 payload
- API 返回的日期是合并区间（start~end），必须展开为逐天
- 浏览器内并发 fetch 会被 Akamai 限流，必须串行请求
- Server酱 SendKey 已配置在 notify_config.json 中
