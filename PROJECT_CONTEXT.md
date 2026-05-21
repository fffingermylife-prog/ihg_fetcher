# IHG 酒店价格监控项目 - 会话总结

## 项目目标
分析 IHG（洲际酒店集团）网站，获取旗下酒店的日历现金最低价和积分兑换价格，计算积分性价比(CPP)，监控价格变动，降价/房态变化时自动推送微信通知，通知里附带可点击的预订链接。

---

## 已完成的功能

### 1. 酒店列表抓取 (`ihg_hotel_list_fetcher.py`)
- 通过 `/explore` 页面展开区域 accordion，收集二级链接
- 支持 `--target` 指定国家/地区，`--region` 指定大区域
- 子区域递归：检测 "Hotels by State/Region" 区块深入收集
- 增量去重：已有 CSV 数据自动跳过
- 输出字段：mnemonic, name, brand_code, city, country, address, rating, review_count, url
- 结果按国家分组 + 评分排序
- 同时写入 SQLite 数据库

### 2. 多酒店批量监控 (`ihg_batch_monitor.py`)
- 多 Tab 并发 (默认 2, 最大 3)
- 全量模式: 获取 365 天完整价格, 对比所有变化
- 增量模式: 只获取最远 62 天窗口, 快速检测新开放日期
- 房态检测: 售罄/重新开放/积分房售罄/价格涨降
- 失败自动重试 + 超时跳过
- 随机请求间隔 (200~500ms) 降低被检测风险
- 支持从 `--codes` / `--from-csv` / `--from-json` / `--from-db` 读取酒店列表
- **支持 Clash 代理** (`--proxy http://127.0.0.1:7890`)
- **支持 Clash 自动切换节点** (`--auto-switch`)
- **启动前代理连通性检测** (用 Cloudflare 204 端点)
- **过期日期过滤** (compare_prices 自动跳过今天及之前的日期)
- **酒店中文名显示** (优先 notify_config.json 的 note，其次 db 的 name)
- **命令行精简显示**: 每酒店一行汇总（如 `5积售罄, 3积降, 2现降`）
- **详细日志写文件**: `./ihg_logs/monitor_YYYYMMDD_HHMMSS.log`，含完整变动 + 对比基准日期
- SQLite 存储 (保留所有历史记录)
- 自动触发微信通知 (满足告警条件时)

### 3. SQLite 数据库 (`ihg_db.py`)
- `hotels` 表: 酒店基本信息 (从 hotel_list_fetcher 写入)
- `prices` 表: 每日价格记录 (每次采集新增记录, 保留历史)
- 支持: 基线加载 / 价格历史查询 / 统计信息
- 可独立运行: `python ihg_db.py --stats` / `--hotels` / `--history`

### 4. 价格告警通知 (`ihg_notify.py`)
- Server酱微信推送, 唯一目的: 找到极具性价比的积分房和现金房
- **5条告警规则**（按优先级从高到低, 条件极端避免噪音）:
  - 💎 高 CPP 积分房: CPP ≥ 0.8 (`min_cpp_threshold`) — **CPP 单位为 USD 美分/积分**, 0.8 表示每万积分价值 $80 以上
  - 🔴 积分同日暴降: 同日积分降幅 ≥ 40% (`points_drop_pct`) — 需历史基准
  - 🟠 节假日积分低价: 节假日 + 积分 ≤ 平日均价×0.9 (`holiday_points_ratio`) — 无需历史
  - 🟣 现金深折扣: 现金 ≤ 平日均价×0.5 (`cash_deal_ratio`) — **均价比较用 USD, 跨币种可比**
  - 🟡 现金同日暴降: 同日现金降幅 ≥ 50% (`cash_drop_pct`) — 需历史基准
- **货币统一为 USD** (✨ 新增):
  - IHG Calendar API 返回的是酒店本地币 (MYR/JPY/HKD 等)
  - 通过 IHG 官方汇率 API `apis.ihg.com/finance/conversions/v2/currencies` 转 USD
  - 每个币种汇率仅查询一次 (run-level 缓存)
  - CPP 全球可比, 不再被本地币面值 (如 MYR/JPY) 误导
  - 通知中同时显示本地价 + USD 等价: 如 `394 MYR (≈$83)`
- **基准均价策略**: 统一用本次快照平日均价 (周一~周四 + 非节假日)
  - 一次全量 365 天有 ~150 个平日样本, 无需依赖历史
  - 首次采集的酒店也能立即识别高性价比日期
  - 历史数据仅用于"同日对比" (🔴/🟡 规则)
- 每家酒店最多推送 `top_n_per_hotel`(5) 条, 按性价比从高到低排序
- 告警去重: (hotel, date) 保留最高优先级
- 中国节假日识别 2026~2027 (含前后各2天缓冲)
- 每条告警自动附 IHG 官网预订链接 (`adjustMonth=true` + `monthIndex=01`)
- CLI: `python ihg_notify.py --url HKGKL:2026-10-01[:nights]`
- 所有参数可在 `notify_config.json` 的 `rules` 字段中调整 (含中文说明)

### 5. Clash 代理自动切换 (`ihg_clash_proxy.py`)
- 通过 Clash RESTful API 自动切换节点
- 定期轮换：每处理 N 个酒店随机切换节点（默认 5）
- 失败切换：请求失败/超时时立即切换
- **only_flag_emoji 模式**: 只保留以国旗 emoji 开头的节点（U+1F1E6 ~ U+1F1FF），自动排除应用分组 (YouTube/Disney/Final/HK/JP 等)
- **secret 自动清理**: 剔除非 ASCII 字符（避免 latin-1 编码错误），中文引号去除
- **URL 编码分组名**: 用 `urllib.parse.quote` 处理 emoji/中文分组名
- 启动前测试代理连通性（用 `cp.cloudflare.com/generate_204`，**不能用 IHG 测**因为会被 Akamai 反爬拦截返回 403）
- **✨ 代数计数器机制**: 解决 Playwright 持久连接导致切换节点后流量仍走旧节点的问题
  - 每次切换 `_switch_generation` +1，worker 检测代数变化后自动 `page.goto()` 刷新页面
  - 多 Worker 并发安全：每个 Worker 独立记录自己的代数
- 独立运行: `--test` / `--list` / `--current` / `--switch` / `--rotate`
- 配置在 `notify_config.json` 的 `clash` 字段

### 6. 新日期开放时间探测 (`ihg_detect_open_time.py`)
- 定时启动 (默认 00:59, 支持 `--start-time` 自定义)
- 每 10~15 分钟随机间隔检查最远可预订日期
- 检测到新日期开放后自动停止
- 日志保存到 `ihg_open_time_<CODE>.log`

### 7. 旧版工具 (已清理删除)
- 以下文件已在项目精简中删除，功能已被核心模块覆盖：
  - `ihg_price_monitor.py` → 被 `ihg_batch_monitor.py` 替代
  - `ihg_playwright_fetcher.py` → 被 batch_monitor 内置 fetch 替代
  - `ihg_calendar_price.py` → 旧版纯 Python + Cookie，不再使用
  - `ihg_calendar_browser.js` / `ihg_diagnostic.js` / `ihg_extract_cookie.js` → 一次性工具
  - `ihg_test_calendar_price.py` / `ihg_test_single_region.py` / `ihg_debug_calendar.py` → 测试/调试脚本

---

## 关键技术发现

| 项目 | 详情 |
|------|------|
| API Key (国际版) | `se9ym5iAzaW8pxfBjkmgbuGjJcr3Pj6Y` |
| API Key (中国小程序) | `HAX1XvhTaXfK1TwYp2LeMEwv8kJCgClV` (在 `apis.ihg.com.cn` 上) |
| Calendar API (用) | POST `https://apis.ihg.com/availability/v1/calendar` |
| 中国版 V3 接口 (未用) | POST `https://apis.ihg.com.cn/availability/v3/hotels/offers` 需 openId/unionCode/微信登录态 |
| IHG 微信小程序 AppID | `wx255b58f0992b3c53` |
| 反爬 | 必须用 Playwright 有头浏览器（Akamai 拦截无头模式） |
| 反爬 (extra) | requests 直接访问 `www.ihg.com` 会被反爬返回 403，用 Cloudflare 204 端点测代理 |
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
| Clash for Windows API | external-controller 端口在客户端 Settings 中查看（不是 config.yaml 里的） |
| Playwright 持久连接 | 浏览器到 Clash 本地代理的 TCP 连接是 Keep-Alive 的，切节点只对新连接生效，需 page.goto() 强制断开旧连接 |
| 货币转换 API | `apis.ihg.com/finance/conversions/v2/currencies?qFcc=XXX&qTcc=USD&qV=1` 返回 1单位本地币=N USD, 用此API统一货币 |
| Calendar API 货币 | API 永远返回酒店本地币 (`propertyCurrency`/`lowestRate.currency`), 即使 url 是 `/us/en/`, 需自行调用转换 API |

---

## 微信通知跳转方案探索（重要）

本次会话深入研究了「微信通知点击跳转到 IHG 预订页」的可行性，最终决策走 HTTP 链接路线：

### ❌ 微信小程序短链方案（已放弃）
- 用户分享 IHG 小程序得到的链接 `#小程序://IHG优悦会/8uGVbV2paEN91at` **能直接跳到指定酒店和日期**
- 但**短链是 IHG 在分享时一次性生成的**，第三方无法批量生成
- 每个"酒店+日期"组合都需要手动分享一次
- 节假日日期固定，可手动收集 20+ 个，但维护成本高且跨年要重新做

### ❌ 自建小程序跳 IHG 方案（已放弃）
- `wx.navigateToMiniProgram(appId='wx255b58f0992b3c53')` 技术上可行
- 但需要 IHG 在自己 app.json 里加白名单或微信开放平台互跳授权
- 个人小程序大概率没有授权，且微信审核会卡

### ✅ HTTP 链接方案（已验证可用）
- 通知里附 `https://www.ihg.com/redirect?path=rates&hotelCode=HKGKL&regionCode=1&localeCode=en&checkInDate=1&checkInMonthYear=102026&...&adjustMonth=true&monthIndex=01`
- 用户在微信里点击 → 内置浏览器打开 → IHG 预订页（预填酒店+日期）
- **已验证**: `adjustMonth=true` + `monthIndex=01` 可确保日期不偏移

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
| Clash secret 含中文导致 latin-1 错误 | HTTP 头不允许非 ASCII | `_sanitize_secret` 自动剔除非 ASCII 字符 |
| Clash 切换节点不生效 (API层面) | 用户在 Global 模式但代码切的是 Proxies 组 | 改成切 GLOBAL 组 + 加 only_flag_emoji 过滤 |
| Clash 切换后浏览器仍走旧节点 | Playwright 到代理的 TCP 持久连接 (Keep-Alive) 不受 Clash 路由更新影响 | 代数计数器 + 切换后 page.goto() 刷新页面断开旧连接池 |
| 代理连通性测试 IHG 返回 403 | Akamai 反爬拦截 requests | 改用 Cloudflare 204 端点 (cp.cloudflare.com/generate_204) |
| 报告含 2027-05-16 售罄(已过期) | compare_prices 没过滤过期日期 | 加 `today_str` 过滤 `[d for d in all_dates if d > today_str]` |
| 通知基准均价不准 | `get_hotel_avg_*` 仅取 `load_latest_prices()` 一次快照 | 改用 `get_all_cash_prices/get_all_points_prices` 取所有历史快照, 不足14样本时 fallback 本次快照 |
| 新开放日期同时积分+现金低价时漏报 | 原 `if not alert and ...` 互斥判定 | 拆为两个独立 if 分别检查积分和现金 |
| `new_date_below_avg_pct` 默认值不一致 | 配置 30, 代码硬编码 fallback 40 | 统一默认为 30 |
| 首次采集无历史基准, 找不到高性价比日期 | 旧逻辑只能比较历史 | 新增"首次采集 fallback 本次快照平日均价" |
| CPP 计算被本地币面值误导 (如 JHBCC 显示 CPP=3.03) | 直接用本地币×100/积分, MYR/JPY 等大面值币种 CPP 虚高 | 引入 IHG 官方汇率 API, 价格统一换算为 USD 后再算 CPP, 阈值改为 0.8 (USD美分/积分) |

---

## 仓库文件结构

```
fffingermylife-prog/ihg_fetcher (分支: feat/ihg-calendar-price)
├── ihg_batch_monitor.py         # 核心: 多酒店批量价格监控 + 通知 + 日志
├── ihg_hotel_list_fetcher.py    # 核心: 按国家抓取酒店列表
├── ihg_notify.py                # 核心: Server酱推送 + 预订链接生成
├── ihg_clash_proxy.py           # 核心: Clash 代理节点自动切换
├── ihg_db.py                    # 核心: SQLite 数据库封装
├── ihg_detect_open_time.py      # 工具: 新日期开放时间探测
├── notify_config.json           # 配置: Server酱 key + 告警规则 + Clash 配置
├── .gitignore                   # 忽略 ihg_data/ ihg_logs/ ihg_browser_profile/
├── requirements.txt             # 依赖: playwright, requests
├── README.md                    # 使用文档
└── PROJECT_CONTEXT.md           # 本文件
```

---

## notify_config.json 结构

```json
{
  "server_chan_key": "SCT...",
  "_rules_说明": {
    "min_cpp_threshold": "💎 高CPP积分房: CPP >= 此值触发 (0.7 = 每万分价值70元)",
    "points_drop_pct": "🔴 积分同日暴降: 降幅 >= 此百分比触发",
    "holiday_points_ratio": "🟠 节假日积分低价: 积分 <= 平日均价×此值触发",
    "cash_deal_ratio": "🟣 现金深折扣: 现金 <= 平日均价×此值触发 (0.5=半价)",
    "cash_drop_pct": "🟡 现金同日暴降: 降幅 >= 此百分比触发",
    "top_n_per_hotel": "每家酒店最多推送条数"
  },
  "rules": {
    "min_cpp_threshold": 0.8,
    "points_drop_pct": 40,
    "holiday_points_ratio": 0.9,
    "cash_deal_ratio": 0.5,
    "cash_drop_pct": 50,
    "top_n_per_hotel": 5
  },
  "hotels": {
    "DADHA": {"note": "岘港洲际"},
    "HKGKL": {"note": "香港金域假日"}
  },
  "clash": {
    "api_url": "http://127.0.0.1:64821",
    "secret": "secret",
    "proxy_group": "GLOBAL",
    "rotate_every_n": 5,
    "only_flag_emoji": true,
    "exclude_keywords": ["Traffic","Expire","DIRECT","REJECT","GLOBAL","Proxies","Final"]
  }
}
```

---

## 使用示例

```bash
# 1. 抓取越南酒店列表
python ihg_hotel_list_fetcher.py --target "Vietnam Hotels"

# 2. 批量监控 (Clash 自动切换节点) ← 推荐
python ihg_batch_monitor.py --codes DADHA,HKGKL --auto-switch

# 3. 批量监控 (直连)
python ihg_batch_monitor.py --codes DADHA,HKGKL --concurrency 3

# 4. 从数据库读取酒店, 增量模式
python ihg_batch_monitor.py --from-db --incremental

# 5. 按国家监控
python ihg_batch_monitor.py --from-db --country "Vietnam" --auto-switch

# 6. 查看数据库统计
python ihg_db.py --stats

# 7. 测试 Clash 连接 + 列出节点
python ihg_clash_proxy.py --test
python ihg_clash_proxy.py --list

# 8. 测试微信通知 (含示例预订链接)
python ihg_notify.py --test

# 9. 生成单个预订链接 (调试用)
python ihg_notify.py --url HKGKL:2026-10-01:2

# 10. 探测新日期开放时间
python ihg_detect_open_time.py --code HKGKL
```

---

## 每日监控计划 (建议)

```
07:35  增量模式 (检测新日期开放)
       python ihg_batch_monitor.py --from-db --incremental --auto-switch

08:30  全量模式 (重点酒店, 完整对比)
       python ihg_batch_monitor.py --codes 重点酒店 --days 365 --auto-switch

14:00  全量模式 (其余酒店前半)
       python ihg_batch_monitor.py --from-db --days 180 --auto-switch

20:00  全量模式 (其余酒店后半)
       python ihg_batch_monitor.py --from-db --days 180 --auto-switch
```

---

## 下一步计划

### ✅ 已完成: 预订链接 URL 修复
**问题**: 原来的 `/hotels/cn/zh/find-hotels/hotel/rooms?qSlH=...` 路径无法访问

**解决方案**: 改用 IHG 官方 redirect 服务 `https://www.ihg.com/redirect?...`

**关键参数发现** (经用户实测验证):
- `adjustMonth` 必须设为 `true` (设为 false 会导致月份偏移+1)
- `monthIndex` 必须设为 `01` (设为 00 同样会偏移)
- 需要 `regionCode=1` + `localeCode=en`
- 需要 `numberOfAdults=1` + `numberOfRooms=1`

**最终可用的 URL 格式**:
```
https://www.ihg.com/redirect?path=rates&hotelCode=HKGKL&regionCode=1&localeCode=en&checkInDate=1&checkInMonthYear=102026&checkOutDate=2&checkOutMonthYear=102026&numberOfAdults=1&numberOfRooms=1&adjustMonth=true&monthIndex=01
```

**同时修复**: 酒店名显示改为 `"酒店全名 [代码]"` 格式，确保推送内容显示酒店全名

### 优先级 2: 数据分析输出
- CPP 排行榜：哪些酒店积分性价比最高
- 价格趋势图（每日采集后基于历史 SQLite 数据）
- 最佳预订时机建议
- 节假日期间的"金价位"日期推荐

### 优先级 3: 确认新日期开放精确时间
- 运行 `ihg_detect_open_time.py` 连续 2~3 天
- 确认后调整定时任务时间

### 优先级 4: 监控规模化
- 200+ 酒店级别的稳定运行测试
- 失败重试和断点续跑机制
- 监控仪表板（可视化最近一次结果 + 历史趋势）

---

## 给新会话的提示
- GitHub 仓库: `fffingermylife-prog/ihg_fetcher`，分支 `feat/ihg-calendar-price`
- 用户环境: Windows + Python 3.12 + Playwright + Clash for Windows (Global 模式)
- 用户偏好: 中文沟通，代码注释用中文，喜欢简洁实用的方案
- 用户的 5 家关注酒店: DADHA(岘港洲际), HKGKL(香港金域假日), HKGKH(香港旺角皇冠假日), HKGIN(香港英迪格), PQCCP(富国岛皇冠假日)
- 关键：不要猜测 IHG 的 URL/API 结构，所有参数都基于已验证的真实抓包
- Calendar API payload 现金和积分**不同**，不能共用同一个 payload
- API 返回的日期是合并区间（start~end），必须展开为逐天
- 浏览器内并发 fetch 会被 Akamai 限流，必须串行请求
- requests 直接访问 `www.ihg.com` 会被反爬返回 403，所以代理连通性测试用 Cloudflare 204 端点
- Server酱 SendKey 已配置在 notify_config.json 中
- Clash for Windows API 端口在客户端 Settings 里查看实际值（每次启动可能变），**不是** config.yaml 里写的端口
- `notify_config.json` 的 `clash.proxy_group` 必须和用户实际使用的模式匹配（Global → "GLOBAL"，Rule → 实际生效的分组名）
- 微信小程序跳转方案已放弃（无法程序生成），现在走 IHG 官网 redirect 链接，已验证可用
- IHG redirect 链接关键参数: `adjustMonth=true` + `monthIndex=01`，否则月份会偏移+1
- Playwright 持久连接问题已解决：切换 Clash 节点后必须 page.goto() 刷新页面，否则旧 TCP 连接仍走原节点（代数计数器机制）
- **货币统一为 USD**：所有价格通过 IHG 官方汇率 API 换算为 USD 后再算 CPP，CPP 单位为 USD 美分/积分，阈值 0.8 = 每万积分换 $80 以上
- **CPP 字段语义已变更**: 旧版 = 本地币×100/积分（受币种面值影响）, 新版 = USD美分/积分（全球可比）, 旧 DB 数据的 CPP 不可直接用于新版阈值比较
