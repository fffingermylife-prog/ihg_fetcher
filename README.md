# IHG 酒店价格 & 积分性价比监控

抓取 IHG（洲际酒店集团）旗下酒店的**日历现金最低价**和**积分兑换价格**，统一换算为 **USD** 计算积分性价比 **CPP**（USD 美分/积分），通过 **Server酱**推送微信通知，附带可点击的 IHG 官网预订链接。

> **目标场景**: 监控自己关注的酒店，第一时间发现「真正值得抢」的积分房和现金房。

---

## ✨ 核心特性

- 🔁 **多酒店并发抓取**（Playwright 多 Tab，默认 2 路并发）
- 💰 **货币统一为 USD**（通过 IHG 官方汇率 API 实时换算，CPP 全球可比）
- 🚨 **5 条告警规则**（CPP / 积分暴降 / 节假日积分 / 现金深折扣 / 现金暴降）
- 📲 **Server酱微信推送** + IHG 官网 redirect 预订链接（已验证日期不偏移）
- 🔄 **Clash 自动切换节点**（含代数计数器机制，解决 Playwright 持久连接问题）
- 🗂 **SQLite 全历史保留**（每次采集新增一条记录，可做趋势分析）
- 🌏 **酒店目录爬虫**（按国家/区域抓取，自动递归子区域，支持增量去重）
- ⏰ **新日期开放探测**（约每天 UTC 23:00 新增一天，可定时探测）

---

## 📦 模块结构

```
ihg_fetcher/
├── ihg_hotel_list_fetcher.py   # 抓酒店目录 → SQLite + CSV
├── ihg_batch_monitor.py        # 多酒店批量价格监控（核心入口）
├── ihg_notify.py               # Server酱推送 + 5 条告警规则 + 预订链接
├── ihg_clash_proxy.py          # Clash 节点自动切换
├── ihg_db.py                   # SQLite 数据库封装
├── ihg_detect_open_time.py     # 探测新日期开放时间
├── notify_config.json          # 配置（Server酱 key / 告警阈值 / Clash / 关注酒店）
├── requirements.txt            # 依赖（playwright + requests）
├── PROJECT_CONTEXT.md          # 项目完整上下文 & 决策记录
└── README.md
```

| 模块 | 职责 |
|------|------|
| `ihg_hotel_list_fetcher.py` | 通过 `/explore` 页面收集酒店目录，支持 `--target` / `--region` 过滤，自动递归子区域，写入 SQLite + CSV |
| `ihg_batch_monitor.py` | 主入口：多 Tab 并发抓 365 天日历价（现金 + 积分），写库 + 触发通知 |
| `ihg_notify.py` | 5 条告警规则 + Server酱推送，自动生成可点击 IHG redirect 链接 |
| `ihg_clash_proxy.py` | 通过 Clash RESTful API 切换节点，含 emoji 国旗过滤 + 代数计数器机制 |
| `ihg_db.py` | hotels（基本信息）+ prices（每日价格历史）两张表，可独立 CLI 查询 |
| `ihg_detect_open_time.py` | 定时探测每天新开放的最远日期，确认开放时间窗口 |

---

## 🚀 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. 配置 `notify_config.json`

```json
{
  "server_chan_key": "SCT...",
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
    "secret": "your_secret",
    "proxy_group": "GLOBAL",
    "rotate_every_n": 5,
    "only_flag_emoji": true,
    "exclude_keywords": ["Traffic","Expire","DIRECT","REJECT","GLOBAL","Final"]
  }
}
```

> 💡 `clash.api_url` 端口请在 Clash for Windows 客户端 `Settings → External Controller` 中查看（**不是** config.yaml 里的端口，每次重启可能会变）。
>
> 💡 `clash.proxy_group` 必须和 Clash 当前模式匹配：Global 模式 → `"GLOBAL"`；Rule 模式 → 实际生效的代理分组名。

### 3. 抓取酒店目录（首次使用）

```bash
# 按国家抓取（推荐：从国家入口页开始，自动收录所有酒店）
python ihg_hotel_list_fetcher.py --target "Vietnam Hotels"

# 也可按大区域抓取
python ihg_hotel_list_fetcher.py --region "Asia, Middle East and Africa"
```

输出会写入 SQLite (`ihg_data/ihg.db`) 和 `ihg_data/ihg_hotels.csv`。

### 4. 批量监控价格

```bash
# 推荐: Clash 自动切换节点
python ihg_batch_monitor.py --codes DADHA,HKGKL --auto-switch

# 直连 (适合非大陆环境)
python ihg_batch_monitor.py --codes DADHA,HKGKL --concurrency 3

# 从数据库读取所有酒店, 增量模式 (只看最远 62 天窗口, 快速检测新开放日期)
python ihg_batch_monitor.py --from-db --incremental --auto-switch

# 按国家筛选
python ihg_batch_monitor.py --from-db --country "Vietnam" --auto-switch
```

满足任一告警规则的日期会自动通过 Server酱推送到微信，附 IHG 预订链接。

---

## 🚨 5 条告警规则

| 规则 | 触发条件 | 默认阈值 | 优先级 |
|------|---------|---------|--------|
| 💎 高 CPP 积分房 | CPP ≥ 阈值 | `0.8` USD美分/积分（≈每万积分 $80）| 1 |
| 🔴 积分同日暴降 | 同日积分降幅 ≥ 阈值 | `40%` | 2 |
| 🟠 节假日积分低价 | 节假日 + 积分 ≤ 平日均价×系数 | `0.9` | 3 |
| 🟣 现金深折扣 | 现金 ≤ 平日均价×系数（USD 比较）| `0.5`（半价）| 4 |
| 🟡 现金同日暴降 | 同日现金降幅 ≥ 阈值 | `50%` | 5 |

**基准均价策略**: 使用本次快照的平日均价（周一~周四 + 非节假日），单次全量 365 天约有 150 个平日样本，**首次采集**的酒店也能立即识别高性价比日期。同日对比规则（🔴/🟡）才需要历史基准。

**去重**: 同一 (酒店, 日期) 只保留最高优先级的规则；每家酒店最多推 `top_n_per_hotel` 条。

---

## 💰 关于 USD 货币统一（重要）

> ⚠️ **CPP 字段语义已变更**：旧版 = 本地币×100/积分（受 MYR/JPY 等大面值币种误导，CPP 虚高）；**新版 = USD 美分/积分**（全球可比）。**旧 DB 数据的 CPP 不可直接用新版阈值比较**。

- IHG Calendar API 永远返回酒店本地币（即使 URL 是 `/us/en/`）
- 通过 IHG 官方汇率 API `apis.ihg.com/finance/conversions/v2/currencies` 转 USD
- 每个币种汇率仅查询一次（run-level 缓存）
- 通知中同时显示本地价 + USD 等价：`394 MYR (≈$83)`

---

## 🔌 Clash 代理使用说明

支持通过 Clash 自动轮换节点，规避反爬限流：

```bash
# 启动前测试连通性 + 列出节点
python ihg_clash_proxy.py --test
python ihg_clash_proxy.py --list

# 手动切换 / 查看当前节点
python ihg_clash_proxy.py --switch
python ihg_clash_proxy.py --current
```

**关键机制**:
- ✅ 启动前用 **Cloudflare 204** 端点测代理（不能用 IHG 测，会被 Akamai 反爬返回 403）
- ✅ `only_flag_emoji=true` 只保留以国旗 emoji 开头的节点，自动排除 YouTube/Disney/HK/JP 等应用分组
- ✅ **代数计数器机制**: 切换节点后通过 `page.goto()` 强制断开 Playwright 持久 TCP 连接，确保新流量走新节点

---

## 🔗 预订链接说明

通知中的链接基于 IHG 官方 redirect 服务，**已实测**点击后日期不偏移：

```
https://www.ihg.com/redirect?path=rates&hotelCode=HKGKL&regionCode=1&localeCode=en
  &checkInDate=1&checkInMonthYear=102026&checkOutDate=2&checkOutMonthYear=102026
  &numberOfAdults=1&numberOfRooms=1
  &adjustMonth=true&monthIndex=01
```

> ⚠️ `adjustMonth=true` + `monthIndex=01` 必须同时设置，否则月份会偏移 +1。

CLI 调试单条链接：
```bash
python ihg_notify.py --url HKGKL:2026-10-01:2   # 香港金域假日, 2026-10-01 起住 2 晚
python ihg_notify.py --test                      # 发送一条测试通知
```

---

## 🗄 SQLite 数据库

```bash
python ihg_db.py --stats       # 统计信息
python ihg_db.py --hotels      # 列出所有酒店
python ihg_db.py --history     # 查询历史价格
```

数据库结构:
- `hotels`: 酒店基本信息（mnemonic, name, brand, city, country, rating, ...）
- `prices`: 每日价格记录（每次采集**新增**记录，保留所有历史）

---

## ⏰ 推荐每日监控计划

```text
07:35  增量模式（检测新日期开放）
       python ihg_batch_monitor.py --from-db --incremental --auto-switch

08:30  全量模式（重点酒店，完整对比）
       python ihg_batch_monitor.py --codes 重点酒店列表 --days 365 --auto-switch

14:00  全量模式（其余酒店前半 180 天）
       python ihg_batch_monitor.py --from-db --days 180 --auto-switch

20:00  全量模式（其余酒店后半 180 天）
       python ihg_batch_monitor.py --from-db --days 180 --auto-switch
```

新日期开放约在 **UTC 23:00~0:00**（中国时间 07:00~08:00）每天新增一天。可通过 `ihg_detect_open_time.py` 连续 2~3 天精确探测：

```bash
python ihg_detect_open_time.py --code HKGKL
```

---

## 🛠 常见问题

| 现象 | 原因 | 解决 |
|------|------|------|
| 抓酒店目录页 accordion 找不到 | Akamai 拦截无头浏览器 | 默认有头模式（不要改 headless） |
| 某些日期价格缺失 | API 返回的是合并的日期区间 | 代码已 `expand_date_range()` 展开 start~end |
| 积分请求 HTTP 500 | 现金/积分 payload 不同 | 已分开请求，**不可合并** |
| 浏览器内并发 6 个 fetch 卡死 | Akamai 限流同 page 并发 | 已改回串行（多 Tab 间并发 OK） |
| 代理连通性测试 IHG 返回 403 | Akamai 反爬拦截 requests | 用 Cloudflare 204 端点测 |
| Clash secret 含中文导致 latin-1 错误 | HTTP 头不允许非 ASCII | 已 `_sanitize_secret` 自动剔除 |
| Clash 切换节点后浏览器仍走旧节点 | Playwright TCP 持久连接 Keep-Alive | 切换后必须 `page.goto()` 刷新（已通过代数计数器实现） |
| CPP 异常虚高（如显示 3.0+） | 旧版用本地币×100/积分，MYR/JPY 面值大 | 已改 USD 美分/积分，阈值 0.8 |
| 通知里酒店名只显示代码 | `notify_config.json` 没配 `note` | 在 `hotels.<CODE>.note` 加中文名 |

---

## 🔬 已知技术细节

- **API 端点**: `POST https://apis.ihg.com/availability/v1/calendar`
- **API Key**: 内置在代码中（IHG 公开 SDK Key）
- **现金 payload**: `guestCounts: AQC10 + AQC8`，无 `rates`
- **积分 payload**: `guestCounts: 只 AQC10`，必须带 `rates.ratePlanCodes = ["IVAN1","IVAN3","IVAN5","IVAN6","IVAN7","IVANI"]` 和 `includeSellStrategy: "followChannel"`
- **窗口大小**: 单次最多 62 天（与官网一致），最远可订日期约 349 天
- **请求间隔**: 随机 200~500ms（模拟真实用户）
- **批量 hotelMnemonics 不支持**: 传多个代码会返回 HTTP 400
- **价格字段**: 含税 `offers[refId].totalAmountAfterFeeTax`；不含税 `lowestRate.totalAmount`

更多决策记录、踩坑历史、未来计划见 [`PROJECT_CONTEXT.md`](./PROJECT_CONTEXT.md)。

---

## 📜 License

仅个人学习使用，请遵守 IHG 服务条款，勿用于商业爬虫。
