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
- 多 Tab 并发 (默认 3, 最大 3)
- 全量模式: 获取 365 天完整价格, 对比所有变化
- 增量模式: 只获取最远 62 天窗口, 快速检测新开放日期
- 房态检测: 售罄/重新开放/积分房售罄/价格涨降
- 失败自动重试 + 超时跳过
- 随机请求间隔 (200~500ms) 降低被检测风险
- 支持从 `--codes` / `--from-csv` / `--from-json` / `--from-db` 读取酒店列表
- **支持 Clash 代理** (`--proxy http://127.0.0.1:7890`)
- **支持 Clash 自动切换节点** (`--auto-switch`) — 批次重建模式
- **启动前代理连通性检测** (用 Cloudflare 204 端点)
- **过期日期过滤** (compare_prices 自动跳过今天及之前的日期)
- **酒店中文名显示** (优先 notify_config.json 的 note，其次 db 的 name)
- **命令行精简显示**: 每酒店一行汇总（如 `5积售罄, 3积降, 2现降`）
- **详细日志写文件**: `./ihg_logs/monitor_YYYYMMDD_HHMMSS.log`
- SQLite 存储 (保留所有历史记录)
- 自动触发微信通知 (满足告警条件时)
- **Step 2 失败延迟集中重试** (✨ 新增): 单酒店失败不再中止整批, 失败酒店放入 requeue 进入下批次 (新节点) 重试; main 中 MAX_BATCH_ATTEMPTS=3 防无限循环
- **Step 3 同酒店现金/积分配对并发** (✨ 新增): `fetch_hotel_prices` 内 `asyncio.gather` 同窗口同时发现金+积分 2 个 fetch, 单酒店耗时从 ~8s 降到 ~4s (节省 ~50%)
- **请求间隔降低** (✨ Step 4): `REQUEST_DELAY_MS (200,500)→(150,350)ms` + session warmup `1-2s→0.8-1.5s`, 500 酒店总省 ~3 min
- **`--window-size` 参数** (✨ Step 5): 默认 62 (与官网一致), 可调 90 → 6 窗口变 4 窗口减 33% 请求数 (实验性, 用户需先小批量验证 IHG 是否接受 90 天 LOS)
- **主循环 3 道死循环防护** (✨ Step 6): processed_codes 集合 + MAX_TOTAL_ITERATIONS 上限 + 进度停滞检测, 防 actually_requeue 异常导致已完结酒店反复进入批次
- **CSV BOM 兼容**: `encoding='utf-8-sig'` 解决 Excel 导出 BOM 头导致 mnemonic 列读不到; 配合详细错误诊断, 不再静默返回空列表

### 3. SQLite 数据库 (`ihg_db.py`)
- `hotels` 表: 酒店基本信息 (从 hotel_list_fetcher 写入)
- `prices` 表: 每日价格记录 (每次采集新增记录, 保留历史)
- 支持: 基线加载 / 价格历史查询 / 统计信息
- 可独立运行: `python ihg_db.py --stats` / `--hotels` / `--history`

### 4. 价格告警通知 (`ihg_notify.py`)

#### 4.1 6 条告警规则 (rank 0~5, 越小越优先)

| Lvl | 规则 | 触发条件 (file 阈值) | 推送条件 (push 阈值) | 数据需求 |
|-----|------|---------------------|-------------------|---------|
| 💎 | 高 CPP 积分房 | CPP ≥ 0.8 美分/分 + 双重防 bug | CPP ≥ 1.0 美分/分 (每万分≥$100) | 无需历史 |
| 🟢 | **积分深折扣 (新增)** | 积分 ≤ 平日均价 × 0.5 (任意日期) | 积分 ≤ 平日均价 × 0.4 | 无需历史 |
| 🔴 | 积分同日暴降 | 同日降幅 ≥ 40% | 同日降幅 ≥ 50% | 需历史基准 |
| 🟠 | 节假日积分低价 | 节假日 + 积分 ≤ 平日均价 × 0.9 | 节假日 + 积分 ≤ 平日均价 × 0.7 | 无需历史 |
| 🟣 | 现金深折扣 | 现金 USD ≤ 平日均价 × 0.5 | 现金 USD ≤ 平日均价 × 0.4 | 无需历史 |
| 🟡 | 现金同日暴降 | 同日降幅 ≥ 50% | 同日降幅 ≥ 60% | 需历史基准 |

**🟢 积分深折扣的设计意图** (✨ 关键新增):
- 旧规则 🟠 节假日积分低价只在节假日触发, 平日 IHG 出现的积分 bug 价 (如 5000 分超低价)无法捕捉
- 🟢 不限节假日, 任意日期积分 ≤ 平日均价×ratio 即触发, 是平日积分 bug 价的核心捕捉规则
- 权重公式 `min(100, pct*1.5+40)` 基础分高, 优先级仅次于 💎

#### 4.2 三层阈值制 (✨ 减少推送噪音, 同时保留全量数据回查)

```
原始数据
   ↓ 应用 file 阈值 (xxx_threshold / xxx_ratio)
全量告警 → 按权重排序 → 写 ihg_logs/alerts_YYYYMMDD_HHMMSS.md (按酒店分组, 不限条数)
   ↓ 应用 push 阈值 (xxx_push, 更严)
推送候选 → 按权重排序 → 取 top_n_global (默认 30)
   ↓ Server酱推送
微信通知 (按酒店分组, 紧凑格式)
```

**权重评分公式** (统一 0~100, 越大越值得关注):
- 💎 高CPP:           `(cpp/threshold - 1)*50 + 70`  → 阈值时 70, 2× 时 100
- 🟢 积分深折扣:      `pct*1.5 + 40`                 → 50%折扣=100 (最稀缺, 基础分高)
- 🔴 积分暴降:        `pct*0.8 + 20`                 → 50%降幅=60
- 🟠 节假日积分低价:  `pct*1.5 + 30`                 → 30%低=75
- 🟣 现金深折扣:      `pct*1.2 + 25`                 → 50%折扣=85
- 🟡 现金暴降:        `pct*0.6 + 15`                 → 60%降幅=51

**file vs push 阈值的关系** (一图看懂):
- **包含关系**: `全量池 ⊇ 推送池` — 先过 file 阈值入文件, 再过 push 阈值才推送
- **角色定位**:
  - file 阈值 = "**记录的门槛**" (宽松, 给自己留底回查, 写入 `ihg_logs/alerts_*.md`)
  - push 阈值 = "**打扰你的门槛**" (严格, 只推真极端到 Server酱)
- **数值大小关系** (push 永远比 file 更严):
  - CPP / 暴降百分比类: push **数值更高** (如 CPP 0.8 → 1.0, 降幅 40% → 50%)
  - 折扣 ratio 类: push **数值更低** (如 0.5 → 0.4, 因为越小折扣越深)
- **三个调优场景**:
  - 🔇 推送过多 → 调严 push (如 `min_cpp_push: 1.0 → 1.2`)
  - 👀 怕漏好货 → 翻 `alerts_*.md`: 不在文件 → 降 file; 在文件没推 → 降 push
  - 🚫 完全屏蔽某规则 → file 和 push 都设极端值 (如 999)

#### 4.3 防 bug 价过滤 (💎 高 CPP 双重保护, ✨ 关键修复)

历史发现两类导致 💎 误推的 bug 价:
1. **积分异常高** → 现金/积分比虚高 → CPP 虚高
2. **现金异常高** → 现金/积分比虚高 → CPP 虚高

修复: 💎 触发时增加双重过滤:
```python
points_ok = not avg_points or points <= avg_points              # 排除积分虚高
cash_ok = not avg_cash_usd or cash_usd <= avg_cash_usd * 1.5    # 排除现金虚高
if cpp >= min_cpp and points and points_ok and cash_ok:
    # 触发 💎 告警
```

#### 4.4 货币统一为 USD
- IHG Calendar API 返回的是酒店本地币 (MYR/JPY/HKD 等)
- 通过 IHG 官方汇率 API `apis.ihg.com/finance/conversions/v2/currencies` 转 USD
- 每个币种汇率仅查询一次 (run-level 缓存)
- CPP 全球可比, 不再被本地币面值 (如 MYR/JPY) 误导
- 通知中只显示 USD 价 (精简格式)
- **优先取 source=P** (✨ 关键修复): IHG 接口对 CNY/EUR 等品牌定制汇率会同时返回:
  - `source=K` (品牌专用, **可能 stale** — 实测 CNY 停在 2022-11 的 11.4745)
  - `source=P` (官方主源, 当前实时 — CNY 实测 0.14649)
  - 旧代码取 `results[0]` 拿到 K 值导致大陆酒店 cash_price_usd 虚高 ~78 倍 → 💎 大量误推
  - 修复: 优先取 `source=P`, fallback 第 0 条; 加 sanity check `1e-7 < rate < 5` 挡住所有异常 stale
  - **历史污染数据**: 用 `python fix_currency_rates.py` 一次性修复 (拉新汇率 → 自动备份 → 按币种批量重算 cash_price_usd 和 cpp; 支持 `--dry-run` 预览)

#### 4.5 基准均价策略
- 统一用本次快照平日均价 (周一~周四 + 非节假日)
- 一次全量 365 天有 ~150 个平日样本, 无需依赖历史
- 至少 10 个平日样本才计算 (防止增量模式样本太少)

#### 4.6 节假日逻辑
- **长假** (春节/五一/国庆 ≥3天): 核心日期 + 前后各2天缓冲
- **单天假期** (元旦/清明/端午/中秋): 仅当天, **不加缓冲** (减少噪音)
- 中国节假日识别 2026~2027

#### 4.7 推送格式精简 (解决 Server酱 1406 超长报错)
- 按酒店分组, 酒店名只出现一次 (纯名字, 无代码)
- 每条告警一行: `💎 2026-10-01 15000分 ≈$82 CPP=0.55¢ [预订](url)`
- 现金价只显示 USD
- `send_server_chan` 加 30000 字符截断保护

#### 4.8 全量告警文件 (✨ 新增)
- 路径: `./ihg_logs/alerts_YYYYMMDD_HHMMSS.md`
- 内容: 所有满足 file 阈值的告警 (不限条数), 按权重降序, 按酒店分组
- 与推送的关系: 全量文件 ⊇ 推送 (推送是全量经 push 阈值过滤 + top_n_global)
- 用途: 回查历史触发情况, 调整阈值参考

#### 4.9 积分预算上限 `max_points_per_night` (✨ 新增)
- 默认值: **35000 分/晚** (后期可调)
- 作用范围: 仅 4 条积分规则 (💎🟢🟠🔴), 现金规则 (🟣🟡) 不受影响
- 过滤位置: **仅 push 阶段** (`passes_push_threshold`), file 阶段不受影响
  - 全量 `alerts_*.md` 仍记录所有 file 阈值触发的告警 (含 70000 分等高积分酒店)
  - 仅 Server酱 微信推送拦截高积分告警, 减少打扰
- 实现细节:
  - `filter_alerts` 中 4 条积分规则 alert dict 加 `"points": <值>` 字段
  - `passes_push_threshold` 开头检查 `alert["level"] in {💎🟢🟠🔴} and points > max_points → return False`
- 设计意图: 高积分酒店 (如 70000 分/晚 IC 顶级) 性价比再高也对小积分玩家不可达, 不必打扰推送; 但仍写文件保留, 积分储备充足时可参考

### 5. Clash 代理自动切换 (`ihg_clash_proxy.py`)
- 通过 Clash RESTful API 自动切换节点
- **批次重建模式** (✨ 唯一可靠方案):
  - 每批 N 个酒店 (默认 8) 共享一个 BrowserContext + 一个 Clash 节点
  - 一批结束后: 关 context (释放所有 socket) → 切节点 → 重建 context → 跑下一批
  - 同批内任一酒店失败 → 触发 abort_event → 整批中断 → 未完成酒店放回队首 → 下批新节点重试
  - 跨批次最多重试 3 次, 仍失败则放弃
- **only_flag_emoji 模式**: 只保留以国旗 emoji 开头的节点 (U+1F1E6 ~ U+1F1FF)
- **secret 自动清理**: 剔除非 ASCII 字符
- **URL 编码分组名**: `urllib.parse.quote` 处理 emoji/中文分组名
- 启动前测试代理连通性 (`cp.cloudflare.com/generate_204`)
- 配置在 `notify_config.json` 的 `clash` 字段, `rotate_every_n` 默认 8

### 6. 新日期开放时间探测 (`ihg_detect_open_time.py`)
- 定时启动 (默认 00:59, 支持 `--start-time` 自定义)
- 每 10~15 分钟随机间隔检查最远可预订日期
- 检测到新日期开放后自动停止
- 日志保存到 `ihg_open_time_<CODE>.log`

---

## 🎯 监控策略推荐 (基于实战经验)

### 极端性价比的本质拆解

| 类型 | 真信号 | 噪音 / 假信号 | 对应规则 |
|------|--------|--------------|---------|
| 积分 bug 价 | 积分 ≤ 平日均价 × 0.4 | 单点异常重复采集会消失 | 🟢 (file 0.5, push 0.4) |
| 现金 bug 价 | 现金 USD ≤ 平日均价 × 0.4 | 临时活动半价 | 🟣 (file 0.5, push 0.4) |
| 高 CPP 真值 | CPP ≥ 1.0 + 积分/现金都正常 | 现金 bug 高 → CPP 虚高 (已修) | 💎 (双重 bug 过滤) |
| 节假日机会 | 节假日积分 ≤ 平日均价 × 0.7 | ratio 0.9 噪音太多 | 🟠 (push 0.7) |
| 时效性机会 | 历史同日降 ≥ 50/60% | 短期波动 | 🔴 / 🟡 |

### 实战 SOP 建议

```
07:35  增量模式 (--from-db --incremental --auto-switch)
       ↓ 主要捕捉新开放日期 + 同日暴降 (🔴🟡)

08:30  全量重点酒店 (--codes 5家关注 --auto-switch)
       ↓ 完整对比, 推送 push 阈值的 top 30

14:00  全量批次 (--from-db, 部分酒店 --auto-switch)
20:00  全量批次 (--from-db, 剩余酒店 --auto-switch)
       ↓ 大池捞 bug 价 (🟢🟣)

每周日: 数据质量审计
       - 审查 ihg_logs/alerts_*.md 全量文件
       - 看哪些 "bug 价" 重复出现 (真 bug) vs 只出现一次 (噪音)
       - 调整阈值或加品牌/区域差异化配置
```

### 阈值调整指引

```
推送过多噪音 → 提高 push 阈值:
  - min_cpp_push 从 1.0 → 1.2
  - cash_deal_push_ratio 从 0.4 → 0.3
  - points_deep_discount_push_ratio 从 0.4 → 0.3

推送漏掉好货 → 降低 push 阈值:
  - 反向调整, 或者直接看全量文件 alerts_*.md

完全不想要某类规则 → 把 file 阈值设极端值:
  - 例: cash_deal_ratio 设为 0.01 (实际不可能触发)
```

### 进阶迭代方向 (待实施)

**Tier 1** (立刻收益, 已实施):
- ✅ 加 🟢 积分深折扣规则 (平日 bug 积分价利器)
- ✅ 三层阈值制 (file / push / top_n_global)
- ✅ 💎 CPP 双重 bug 过滤 (积分均价 + 现金均价)

**Tier 2** (需历史数据, 2~4 周后启用):
- ⏳ 历史同日均价对比 (利用 SQLite 7~14 天数据做基线)
- ⏳ 历史最低突破检测 (当前 < 历史均价×0.7 且 ≈ 历史最低 → 最确凿 bug 信号)
- ⏳ 趋势检测: 连续 3 次采集都偏低 → 提升权重 (确认稳定 vs 抓取异常)

**Tier 3** (长期优化):
- ⏳ 品牌差异化阈值: IC/RC 高端 CPP 阈值 0.7 即推; HX 中端 1.0 才推
- ⏳ 市场化阈值: 东南亚 cash_deal_ratio 用 0.5; 欧美用 0.3
- ⏳ 重复价格识别: 同日期连续 3 次都是同价 → 真定价 (非 bug)
- ⏳ 黑名单酒店: 中东某些 IC 现金常年虚高, 配置忽略其 💎 触发

---

## 关键技术发现

| 项目 | 详情 |
|------|------|
| API Key (国际版) | `se9ym5iAzaW8pxfBjkmgbuGjJcr3Pj6Y` |
| Calendar API | POST `https://apis.ihg.com/availability/v1/calendar` |
| IHG 微信小程序 AppID | `wx255b58f0992b3c53` |
| 反爬 | 必须用 Playwright 有头浏览器 (Akamai 拦截无头模式) |
| 反爬 (extra) | requests 直接访问 `www.ihg.com` 会被反爬返回 403, 用 Cloudflare 204 端点测代理 |
| 现金 payload | `guestCounts`: AQC10+AQC8, 无 `includeSellStrategy`, 无 `rates` |
| 积分 payload | `guestCounts`: 只 AQC10, `includeSellStrategy: "followChannel"`, `rates.ratePlanCodes` |
| 积分 codes | `["IVAN1","IVAN3","IVAN5","IVAN6","IVAN7","IVANI"]` |
| 日期区间 | API 返回合并区间 (start/end), 连续相同价格的天被压缩 |
| 窗口大小 | 62 天 (和官网一致) |
| 最远日期 | 约 349 天 (从今天算) |
| 新日期开放 | 约 UTC 23:00~0:00 (中国时间 07:00~08:00) 每天新增一天 |
| 批量请求 | `hotelMnemonics` 传多个代码返回 400, **不可行** |
| 浏览器内并发 | 同 page 6 个 fetch 并发会被 Akamai 限流, **必须串行** |
| Clash 代理 | 通过 `--proxy http://127.0.0.1:7890` 走 Clash |
| Clash for Windows API | external-controller 端口在客户端 Settings 中查看 (不是 config.yaml) |
| Playwright 持久连接 | 浏览器到 Clash 是 per-host HTTP/2 keep-alive 长连接, page.goto 会复用 idle socket。**唯一可靠切换: 关闭整个 BrowserContext 重建** |
| 货币转换 API | `apis.ihg.com/finance/conversions/v2/currencies?qFcc=XXX&qTcc=USD&qV=1` |
| Calendar API 货币 | API 永远返回酒店本地币 (`propertyCurrency`), 即使 url 是 `/us/en/`, 需自行调用转换 API |

---

## 微信通知跳转方案: HTTP 链接

### ❌ 微信小程序短链方案 (已放弃)
- IHG 小程序短链能跳到指定酒店和日期, 但**短链是分享时一次性生成**, 第三方无法批量

### ❌ 自建小程序跳 IHG 方案 (已放弃)
- 需要 IHG 在 app.json 加白名单或互跳授权, 个人小程序难审核

### ✅ HTTP 链接方案 (已验证)
- 通知附 `https://www.ihg.com/redirect?path=rates&hotelCode=...&adjustMonth=true&monthIndex=01`
- 微信内置浏览器打开 → IHG 预订页 (预填酒店+日期)
- **关键**: `adjustMonth=true` + `monthIndex=01` 可确保日期不偏移

---

## 遇到的问题及解决

| 问题 | 原因 | 解决 |
|------|------|------|
| 无头模式 accordion 找不到 | Akamai 拦截无头浏览器 | 默认有头模式 |
| 中文版 /zh-cn/explore 子区域少 | 中文页面结构不同 | 用英文版 /explore |
| 某些日期现金价 null | API 返回合并的日期区间 | `expand_date_range()` 展开 start~end |
| 积分请求 HTTP 500 | payload 参数和官网不同 | 抓包对比, 分开现金/积分 payload |
| 3 Tab 同时建立 session 卡死 | Akamai 检测同时导航 | 逐个 Tab 建立, 错开 1~2 秒 |
| Windows 命令行输出卡住 | Python stdout 缓冲 | `sys.stdout.reconfigure(line_buffering=True)` |
| 浏览器内 6 请求并发卡死 | Akamai 限流同 session 并发 | 回退为串行 |
| Clash secret 含中文导致 latin-1 错误 | HTTP 头不允许非 ASCII | `_sanitize_secret` 自动剔除 |
| Clash 切换节点不生效 (API 层) | Global 模式但代码切的是 Proxies 组 | 改成切 GLOBAL + only_flag_emoji 过滤 |
| ~~代数计数器+page.goto 切换~~ | ~~已废弃方案~~ | ~~不可靠, 仅触发 navigation 不重建 socket~~ |
| Clash 切换后 apis.ihg.com 仍走旧节点 | Chromium socket pool 是 per-host HTTP/2 keep-alive, page.goto 会复用 idle socket | **批次重建模式**: 每批关 context 释放所有 socket → 切节点 → 重建 |
| Server酱推送 1406 Data too long | 5酒店×365天告警太多, desp 超 MySQL 字段限制 | 精简格式 + 单天假期不缓冲 + top 30 + 30000 字符截断 |
| 代理连通性测试 IHG 返回 403 | Akamai 反爬拦截 requests | 改用 Cloudflare 204 端点 |
| 报告含 2027-05-16 售罄(已过期) | compare_prices 没过滤过期日期 | 加 `today_str > d` 过滤 |
| CPP 计算被本地币面值误导 (JHBCC CPP=3.03) | 直接用本地币×100/积分, MYR/JPY 大面值币种虚高 | 引入 IHG 官方汇率 API, 统一换算 USD 后算 CPP |
| 💎 高 CPP 推送了 bug 价日期 | 现金价异常高 (IHG 数据错误) → CPP 虚高 | **双重过滤**: 积分 ≤ 平日均价 + 现金 ≤ 平日均价×1.5 |
| 平日积分 bug 价无法捕捉 | 旧规则 🟠 只在节假日触发 | **新增 🟢 积分深折扣规则**, 任意日期触发 |
| 推送过多无差别噪音 | 单一阈值, 触发即推 | **三层阈值制**: file 入文件 + push 推送 + top_n_global |
| 高积分酒店 (70000 分/晚) 推送对小积分玩家不可达 | CPP 高但门槛过高, 全量过滤又会丢失数据 | `max_points_per_night=35000` **仅 push 阶段过滤**, file 全量保留供回查 |
| 单酒店失败立刻 abort 整批 → 浪费 7 个酒店进度 + 1 次 context 重建 | 旧设计: 任一失败立刻 set abort_event | **Step 2 失败延迟集中重试**: worker 仅 requeue, 失败酒店进入下批新节点重试 |
| 单酒店现金 6 + 积分 6 = 12 次串行, 单酒店 ~8s | 早期保守串行避开 Akamai 限流 | **Step 3 同酒店现金/积分配对并发**: `asyncio.gather` 同 page 2 路 fetch, 单酒店 ~4s |
| 大陆酒店 cash_price_usd 虚高 78 倍 → 💎 大量误推 (CNY=11.47) | IHG 汇率 API 对 CNY 同时返回 `source=K` (stale 2022-11 旧值 11.4745) 和 `source=P` (实时 0.14649); 旧代码 `results[0]` 拿到 K | **优先取 `source=P`** + sanity check `1e-7 < rate < 5`; 历史污染数据用 `fix_currency_rates.py` 重算 |
| 500+ 酒店跑完后偶尔重新循环 | `actually_requeue` 异常含已 success 酒店, 让其再进 remaining | **3 道死循环防护**: processed_codes 双重过滤 + MAX_TOTAL_ITERATIONS 上限 + 连续 5 批进度停滞强制结束 |
| Excel 导出 CSV 无法读取酒店 (mnemonic 列读到空) | utf-8 编码下 Excel 加 BOM 头, 第一列名变成 `\ufeffmnemonic` | encoding 改 `utf-8-sig` + fieldnames 检测 + FileNotFoundError 单独提示, 不再静默 |

---

## 仓库文件结构

```
fffingermylife-prog/ihg_fetcher (分支: feat/ihg-calendar-price)
├── ihg_batch_monitor.py         # 核心: 多酒店批量价格监控 + 批次重建模式
├── ihg_hotel_list_fetcher.py    # 核心: 按国家抓取酒店列表
├── ihg_notify.py                # 核心: 6规则 + 三层阈值 + 全量文件 + 推送
├── ihg_clash_proxy.py           # 核心: Clash 代理节点切换
├── ihg_db.py                    # 核心: SQLite 数据库封装
├── ihg_detect_open_time.py      # 工具: 新日期开放时间探测
├── fix_currency_rates.py        # 工具: 一次性修复 DB 被 source=K stale 汇率污染的历史数据
├── notify_config.json           # 配置: Server酱 key + 6规则三层阈值 + Clash
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
  "rules": {
    "min_cpp_threshold": 0.8,            "min_cpp_push": 1.0,
    "points_deep_discount_ratio": 0.5,   "points_deep_discount_push_ratio": 0.4,
    "points_drop_pct": 40,               "points_drop_push_pct": 50,
    "holiday_points_ratio": 0.9,         "holiday_points_push_ratio": 0.7,
    "cash_deal_ratio": 0.5,              "cash_deal_push_ratio": 0.4,
    "cash_drop_pct": 50,                 "cash_drop_push_pct": 60,
    "max_points_per_night": 35000,
    "top_n_global": 30
  },
  "hotels": {
    "DADHA": {"note": "岘港洲际"},
    "HKGKL": {"note": "香港金域假日"}
  },
  "clash": {
    "api_url": "http://127.0.0.1:64821",
    "secret": "secret",
    "proxy_group": "GLOBAL",
    "rotate_every_n": 8,
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

# 11. 一次性修复 DB 历史汇率污染数据 (CNY 等被 source=K stale 值污染)
python fix_currency_rates.py --dry-run    # 先预览影响范围
python fix_currency_rates.py              # 实际修复 (会自动备份 DB)

# 12. 实验性提速: 窗口大小调到 90, 减 33% 请求数
python ihg_batch_monitor.py --codes DADHA,HKGKL --window-size 90 --auto-switch
```

---

## 下一步计划

### 优先级 2: 数据分析输出
- CPP 排行榜: 哪些酒店积分性价比最高
- 价格趋势图 (基于历史 SQLite 数据)
- 最佳预订时机建议
- 节假日期间的"金价位"日期推荐

### 优先级 3: 历史数据驱动的进阶规则
- 历史同日均价对比 (替代/补充本次快照均价)
- 历史最低突破检测 (最确凿的 bug 价信号)
- 趋势检测 (连续偏低 vs 单次异常)

### 优先级 4: 监控规模化
- 200+ 酒店级别的稳定运行测试
- 失败重试和断点续跑机制
- 监控仪表板 (可视化最近一次结果 + 历史趋势)

---

## 给新会话的提示

- GitHub 仓库: `fffingermylife-prog/ihg_fetcher`, 分支 `feat/ihg-calendar-price`
- 用户环境: Windows + Python 3.12 + Playwright + Clash for Windows (Global 模式)
- 用户偏好: 中文沟通, 代码注释用中文, 简洁实用方案
- 用户的 5 家关注酒店: DADHA(岘港洲际), HKGKL(香港金域假日), HKGKH(香港旺角皇冠假日), HKGIN(香港英迪格), PQCCP(富国岛皇冠假日)
- 关键: 不要猜测 IHG 的 URL/API 结构, 所有参数基于已验证的真实抓包
- Calendar API payload 现金和积分**不同**, 不能共用
- API 返回日期是合并区间 (start~end), 必须展开为逐天
- 浏览器内并发 fetch 会被 Akamai 限流, 必须串行
- requests 直连 `www.ihg.com` 会被反爬返回 403, 代理连通性测试用 Cloudflare 204
- Clash for Windows API 端口在客户端 Settings 里看实际值 (每次启动可能变)
- `notify_config.json` 的 `clash.proxy_group` 必须和 Clash 实际模式匹配
- 微信小程序跳转放弃, 走 IHG 官网 redirect 链接 (`adjustMonth=true` + `monthIndex=01`)
- **Clash 节点切换 = 批次重建 BrowserContext** (✨ 关键): page.goto / CDP offline 等"轻量招"对 HTTP/2 keep-alive 长连接都无效, 唯一可靠方案是关闭整个 context 释放 socket 后再重建
- **货币统一为 USD**: 所有价格通过 IHG 官方汇率 API 换算为 USD, CPP 单位为 USD 美分/积分, 阈值 0.8 = 每万积分换 $80 以上
- **CPP 字段语义已变更**: 旧版 = 本地币×100/积分 (受币种面值影响); 新版 = USD美分/积分 (全球可比); 旧 DB 数据 CPP 不可直接用新版阈值比较
- **6 条告警规则** (rank 0~5): 💎🟢🔴🟠🟣🟡, 🟢 是平日积分 bug 价捕捉的核心规则
- **三层阈值制**: file 阈值入全量文件 (`ihg_logs/alerts_*.md`), push 阈值推 Server酱, top_n_global=30
- **💎 双重 bug 过滤**: 积分 ≤ 平日均价 + 现金 ≤ 平日均价×1.5 (排除现金/积分异常导致的 CPP 虚高)
- **节假日缓冲**: 单天假期 (元旦/清明/端午/中秋) 不加缓冲, 长假 (春节/五一/国庆 ≥3天) 加前后 2 天
- **阈值调优指引**: 推送多噪音 → 提高 push 阈值; 漏掉好货 → 看全量 `alerts_*.md` 文件回查
- **`max_points_per_night=35000`**: 4 条积分规则 (💎🟢🟠🔴) 推送阶段额外过滤; 现金规则 🟣🟡 不受影响; 全量 `alerts_*.md` 不过滤, 高积分酒店仍可回查
- **失败延迟集中重试 (Step 2)**: 单酒店失败仅 requeue, 不再 abort 整批; 失败酒店随主流程进入下批 (新节点) 重试; MAX_BATCH_ATTEMPTS=3 防无限循环
- **同酒店现金/积分配对并发 (Step 3)**: `fetch_hotel_prices` 内 `asyncio.gather` 同窗口 2 路 fetch (现金+积分); 单酒店耗时砍半 ~8s → ~4s; 仍受 Akamai 限制 (≤2 路同 page 并发, 项目历史已验证 6 路并发会被限流)
- **汇率 source=P 优先 (✨ 关键)**: IHG 接口对 CNY/EUR 等返回 K (stale) + P (实时) 两条; 必须取 P 否则大陆酒店 cash_usd 虚高 78 倍; sanity check `1e-7 < rate < 5` 双保险
- **历史 DB 修复脚本**: `fix_currency_rates.py` 一次性修正旧版 source=K 污染的 cash_price_usd / cpp; 自动备份 DB, 支持 `--dry-run`
- **可选提速 flag**: `--window-size 90` (6→4 窗口, 减 33% 请求, 需小批量验证 IHG 接受); `REQUEST_DELAY_MS=(150,350)` 已默认调低
- **死循环防护**: 主循环 3 道安全锁 (processed_codes / MAX_TOTAL_ITERATIONS / 连续 5 批停滞), 防 actually_requeue 异常导致 500+ 酒店跑完后重新循环
- **未实施的优化**: Tier 分层 / 缩天数 (维持全量 365 天 × 全部酒店); CPP 单位变更 (USD/万分, 当前用 USD美分/分 已稳定); ABORT_THRESHOLD=2 (Step 2 已超越)
