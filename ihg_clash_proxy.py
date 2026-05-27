"""
IHG Clash 代理自动切换模块

功能:
    - 通过 Clash RESTful API 自动切换代理节点
    - 定期轮换: 每处理 N 个酒店后随机切换节点
    - 失败切换: 请求失败/超时时自动切换到下一个节点
    - 自动过滤非代理节点 (如 Traffic/Expire 信息节点)
    - 支持配置文件管理

配置 (notify_config.json 中 "clash" 字段):
    {
        "clash": {
            "api_url": "http://127.0.0.1:56285",
            "secret": "你的secret",
            "proxy_group": "Proxies",
            "rotate_every_n": 8,
            "exclude_keywords": ["Traffic", "Expire", "DIRECT", "REJECT"]
        }
    }

设计说明:
    本模块只负责"通知 Clash 切换节点"这一件事。
    切换后必须由调用方 (主流程) 关闭并重建整个 BrowserContext, 否则 Playwright
    到 Clash 代理的 HTTP/2 keep-alive 长连接会被复用 → 流量继续走旧节点。
    详见 ihg_batch_monitor.py 的批次循环实现。

用法:
    from ihg_clash_proxy import ClashProxyManager

    mgr = ClashProxyManager()  # 自动读取配置
    mgr.rotate()               # 随机切换节点
    mgr.on_failure()           # 失败时切换
    print(mgr.current_node)    # 当前节点名

    # 独立运行测试
    python ihg_clash_proxy.py --test
    python ihg_clash_proxy.py --list
    python ihg_clash_proxy.py --switch "🇭🇰 HKG 香港 01"
"""

import json
import random
import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests


# ============ 配置加载 ============

CONFIG_FILE = Path(__file__).parent / "notify_config.json"


def load_clash_config():
    """从 notify_config.json 加载 clash 配置"""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
        clash_cfg = config.get("clash", {})
        return {
            "api_url": clash_cfg.get("api_url", "http://127.0.0.1:56285"),
            "secret": clash_cfg.get("secret", ""),
            "proxy_group": clash_cfg.get("proxy_group", "Proxies"),
            "rotate_every_n": clash_cfg.get("rotate_every_n", 5),
            "exclude_keywords": clash_cfg.get("exclude_keywords", [
                "Traffic", "Expire", "DIRECT", "REJECT"
            ]),
            # 仅保留以国旗 emoji 开头的节点 (排除所有应用分组/大区分组)
            "only_flag_emoji": clash_cfg.get("only_flag_emoji", False),
        }
    except Exception as e:
        print(f"[Clash] 配置加载失败: {e}")
        return None


def _starts_with_flag_emoji(name):
    """判断名称是否以国旗 emoji 开头 (regional indicator symbols)
    国旗由两个 regional indicator 字符组成, Unicode 范围 U+1F1E6 ~ U+1F1FF
    """
    if not name or len(name) < 2:
        return False
    c0 = ord(name[0])
    c1 = ord(name[1])
    return 0x1F1E6 <= c0 <= 0x1F1FF and 0x1F1E6 <= c1 <= 0x1F1FF


# ============ 核心类 ============

class ClashProxyManager:
    """Clash 代理节点管理器"""

    def __init__(self, config=None):
        """
        初始化管理器
        Args:
            config: 可选, 手动传入配置字典; 为 None 时自动从配置文件加载
        """
        self.config = config or load_clash_config()
        if not self.config:
            raise RuntimeError("[Clash] 无法加载配置, 请检查 notify_config.json 中的 clash 字段")

        self.api_url = self.config["api_url"].rstrip("/")
        self.secret = self._sanitize_secret(self.config["secret"])
        self.proxy_group = self.config["proxy_group"]
        self.rotate_every_n = self.config["rotate_every_n"]
        self.exclude_keywords = self.config["exclude_keywords"]
        self.only_flag_emoji = self.config.get("only_flag_emoji", False)

        # 状态
        self.available_nodes = []    # 可用节点列表
        self.current_node = None     # 当前选中节点
        self.hotel_count = 0         # 已处理酒店计数 (供主流程参考, 不再触发自动切换)
        self.switch_count = 0        # 切换次数统计
        self.fail_count = 0          # 连续失败计数

        # 初始化: 获取可用节点列表
        self._refresh_nodes()

    @staticmethod
    def _sanitize_secret(raw):
        """
        清理 secret: 去除空白和中文引号, 校验是否为 ASCII
        HTTP 头不允许非 ASCII 字符, 否则会触发 latin-1 编码错误
        """
        if not raw:
            return ""
        # 去除常见的中文引号和空白
        cleaned = raw.strip().strip('"').strip("'")
        # 中文引号 \u201c \u201d \u2018 \u2019
        for ch in ('\u201c', '\u201d', '\u2018', '\u2019'):
            cleaned = cleaned.strip(ch)
        # 校验 ASCII
        try:
            cleaned.encode("ascii")
        except UnicodeEncodeError:
            print(f"[Clash] [警告] secret 包含非 ASCII 字符 (中文/emoji 等), 请检查 notify_config.json")
            print(f"        如果 Clash 没设置 secret, 请把 \"secret\" 字段留空字符串: \"secret\": \"\"")
            # 强行剔除非 ASCII 字符, 让程序不至于直接崩
            cleaned = cleaned.encode("ascii", errors="ignore").decode("ascii")
        return cleaned

    def _build_proxy_url(self, group_name=None):
        """构建 /proxies/<分组名> URL, 自动 URL 编码非 ASCII 字符"""
        name = group_name if group_name is not None else self.proxy_group
        # quote 默认会保留 / : 等, 用 safe="" 强制编码所有非 unreserved 字符
        encoded = quote(name, safe="")
        return f"{self.api_url}/proxies/{encoded}"

    def _headers(self):
        """构建请求头 (含 secret 认证)"""
        headers = {"Content-Type": "application/json"}
        if self.secret:
            headers["Authorization"] = f"Bearer {self.secret}"
        return headers

    def _refresh_nodes(self):
        """从 Clash API 获取代理组信息, 刷新可用节点列表"""
        try:
            url = self._build_proxy_url()
            resp = requests.get(url, headers=self._headers(), timeout=5)
            resp.raise_for_status()
            data = resp.json()

            all_nodes = data.get("all", [])
            self.current_node = data.get("now", "")

            # 过滤策略:
            # - only_flag_emoji=True: 只保留以国旗 emoji 开头的节点 (最严格, 推荐)
            # - 否则: 用关键词排除 (Traffic/Expire/分组节点等)
            if self.only_flag_emoji:
                self.available_nodes = [n for n in all_nodes if _starts_with_flag_emoji(n)]
            else:
                self.available_nodes = [
                    node for node in all_nodes
                    if not any(kw in node for kw in self.exclude_keywords)
                ]

        except requests.exceptions.ConnectionError:
            print(f"[Clash] 无法连接 Clash API ({self.api_url}), 请确认 Clash 已运行")
            self.available_nodes = []
        except Exception as e:
            print(f"[Clash] 获取节点列表失败: {e}")
            self.available_nodes = []

    def _switch_to(self, node_name):
        """切换到指定节点, 同时切换所有相关代理组确保生效

        重要: 仅切换 Clash 路由表是不够的。Playwright 浏览器到 Clash 本地代理的
        HTTP/2 keep-alive 长连接是 per-host (BrowserContext 级别) 的, 已建立的
        连接不会因 Clash 切换而断开 — 新流量仍会走旧节点。

        所以本模块只负责"通知 Clash 切换", 调用方 (主流程) 必须在切换前后关闭
        并重建整个 BrowserContext, 才能让流量真正走到新节点。
        参见 ihg_batch_monitor.py 的批次循环实现。
        """
        try:
            old = self.current_node

            # 1. 切换主代理组 (如 GLOBAL)
            url = self._build_proxy_url()
            resp = requests.put(
                url,
                headers=self._headers(),
                json={"name": node_name},
                timeout=5,
            )
            if resp.status_code not in (200, 204):
                print(f"[Clash] 切换 {self.proxy_group} 失败: HTTP {resp.status_code} - {resp.text}")
                return False

            # 2. 同时尝试切换 Proxies 组 (Global 模式下, 流量可能走 Proxies 子组)
            #    如果主组不是 Proxies, 则也切换 Proxies 确保生效
            if self.proxy_group.upper() != "PROXIES":
                try:
                    proxies_url = self._build_proxy_url("Proxies")
                    requests.put(
                        proxies_url,
                        headers=self._headers(),
                        json={"name": node_name},
                        timeout=5,
                    )
                except Exception:
                    pass  # Proxies 组可能不存在或节点不在该组, 忽略

            self.current_node = node_name
            self.switch_count += 1
            self.fail_count = 0  # 重置连续失败计数
            print(f"[Clash] 切换节点: {old} → {node_name} (第{self.switch_count}次)")

            # 3. 短暂等待 Clash 内部路由更新
            time.sleep(0.5)
            return True
        except Exception as e:
            print(f"[Clash] 切换异常: {e}")
            return False

    def _pick_random_node(self, exclude_current=True):
        """随机选择一个节点 (排除当前节点)"""
        candidates = self.available_nodes[:]
        if exclude_current and self.current_node in candidates:
            candidates.remove(self.current_node)
        if not candidates:
            # 所有节点都试过了, 刷新列表重新来
            self._refresh_nodes()
            candidates = self.available_nodes[:]
            if exclude_current and self.current_node in candidates:
                candidates.remove(self.current_node)
        if not candidates:
            return None
        return random.choice(candidates)

    # ============ 公开接口 ============

    def rotate(self):
        """
        随机切换到另一个节点
        Returns: True 切换成功, False 切换失败或无可用节点
        """
        node = self._pick_random_node(exclude_current=True)
        if not node:
            print("[Clash] 无可用节点可切换")
            return False
        return self._switch_to(node)

    def on_hotel_done(self):
        """
        每完成一个酒店后调用 (仅做计数, 不再触发自动切换)

        在批次重建模式下, 节点切换由主流程在批次边界统一控制
        (一个批次 = 一个 BrowserContext = 一个节点), 不再每 N 个酒店内部切换。
        本方法保留只是为了兼容性和可观测统计。
        """
        self.hotel_count += 1
        return False

    def on_failure(self):
        """
        请求失败时调用 (仅记录失败次数, 不再立即切换节点)

        Step 2 优化后的批次模式: 单个酒店失败不再中止整批 (失败延迟集中重试),
        失败酒店进入 requeue 后续批次重试; 主流程在每批结束后正常 rotate() 切节点,
        因此 on_failure 在此处只是计数, 不主动切换.
        """
        self.fail_count += 1
        return False

    def get_current(self):
        """获取当前节点名"""
        self._refresh_nodes()
        return self.current_node

    def list_nodes(self):
        """列出所有可用节点"""
        self._refresh_nodes()
        return self.available_nodes

    def switch(self, node_name):
        """手动切换到指定节点"""
        return self._switch_to(node_name)

    def get_stats(self):
        """获取统计信息"""
        return {
            "current_node": self.current_node,
            "available_count": len(self.available_nodes),
            "switch_count": self.switch_count,
            "hotel_count": self.hotel_count,
            "rotate_every_n": self.rotate_every_n,
        }

    def is_available(self):
        """检查 Clash API 是否可用"""
        try:
            resp = requests.get(
                f"{self.api_url}/proxies",
                headers=self._headers(),
                timeout=3,
            )
            return resp.status_code == 200
        except Exception:
            return False

    def warmup_nodes(self,
                     proxy_url="http://127.0.0.1:7890",
                     test_url=None,
                     timeout=5,
                     max_threshold_ms=2500,
                     top_n=None,
                     sample=None,
                     min_keep=3,
                     concurrency=20):
        """节点速度预热 (并发版): 通过 Clash 内置 /proxies/{name}/delay API 测每个节点延迟

        效果: 跑 batch_monitor 前先剔除慢节点, 节点池只保留快节点供后续轮换使用
              (实测同一池里节点速度差 3~10 倍, 选快节点能让单酒店耗时再砍 30~50%)

        实现: 用 Clash 标准 API `/proxies/{name}/delay`, 不需要切换节点直接探测.
              这是 yacd / Clash dashboard 等仪表盘用的同款接口, 可大并发调用.
              旧串行版每节点 ~3s (切换+ping); 并发版 30 节点 ~5s 出结果, 快 ~15 倍.

        典型调用 (用户场景: 保留延迟<400ms 的最快 15 个):
            mgr.warmup_nodes(max_threshold_ms=400, top_n=15)

        Args:
            proxy_url: [已废弃, 仅保留兼容] 旧版需要本地代理走流量, 新版直接调 Clash API
            test_url: Clash 内部探测目标 URL; None=Cloudflare 204 (稳定不被拦)
                     对 IHG 调优可设 'http://apis.ihg.com/...' (但 Akamai 可能影响结果)
            timeout: Clash 单节点测试超时秒数 (Clash 把这个值传给内部探测)
            max_threshold_ms: 延迟超此值的节点剔除 (用户场景 400, 默认 2500 宽松)
            top_n: 只保留前 N 个最快节点 (用户场景 15, None=全保留按速度排序)
            sample: 随机采样测试 N 个节点 (节点池>50 时省时间, None=全测)
            min_keep: 最少保留节点数 (即使全部超阈值, 也保留前 min_keep 个最快的)
            concurrency: 并发测试线程数 (默认 20, 节点池<20 时自动降到节点数)

        Returns:
            list of (node, delay_ms, status) 按速度升序; self.available_nodes 已被替换
        """
        if not self.available_nodes:
            print("[Clash] 没有可用节点, 跳过预热")
            return []

        # 默认测速目标: Cloudflare 204 (Clash 内部走节点探测, 稳定不被拦)
        if test_url is None:
            test_url = "http://cp.cloudflare.com/generate_204"

        candidates = list(self.available_nodes)
        if sample and len(candidates) > sample:
            candidates = random.sample(candidates, sample)

        # 并发数不超过候选节点数
        actual_concurrency = min(concurrency, len(candidates))

        print(f"[Clash] 节点速度预热 (并发 Clash API 探测)")
        print(f"        测试节点: {len(candidates)} 个 | 并发: {actual_concurrency} | 目标: {test_url}")
        print(f"        阈值: <={max_threshold_ms}ms | 超时: {timeout}s | top_n: {top_n or '全保留'}")

        original_node = self.current_node

        def _test_one_node(node):
            """通过 Clash /proxies/{name}/delay API 测单节点延迟 (无需切换)
            Returns: (node, delay_ms_or_None, status_str)
            """
            try:
                url = f"{self.api_url}/proxies/{quote(node, safe='')}/delay"
                params = {"timeout": int(timeout * 1000), "url": test_url}
                resp = requests.get(
                    url,
                    headers=self._headers(),
                    params=params,
                    timeout=timeout + 2,  # HTTP 调用比 Clash 内部超时多 2s 余量
                )
                if resp.status_code == 200:
                    data = resp.json()
                    delay = data.get("delay", 0)
                    if delay and delay > 0:
                        return (node, int(delay), "ok")
                    # delay=0 通常意味着 Clash 探测失败但没明确报错
                    return (node, None, "no_delay")
                elif resp.status_code == 408:
                    return (node, None, "timeout")
                else:
                    # 部分 Clash 版本对探测失败返回非 200 (如 500 + message)
                    try:
                        err = resp.json().get("message", "")[:30]
                    except Exception:
                        err = ""
                    return (node, None, f"http_{resp.status_code}({err})" if err else f"http_{resp.status_code}")
            except requests.exceptions.Timeout:
                return (node, None, "http_timeout")
            except Exception as e:
                return (node, None, f"err: {str(e)[:30]}")

        # 并发测试: 用 ThreadPoolExecutor (requests 是同步的, 用线程池更省事)
        import concurrent.futures
        t_start = time.time()
        all_results = []   # [(node, delay_ms_or_None, status), ...]

        with concurrent.futures.ThreadPoolExecutor(max_workers=actual_concurrency) as executor:
            futures = {executor.submit(_test_one_node, n): n for n in candidates}
            # as_completed: 返回顺序 = 完成顺序 → 快节点先打印
            for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
                node, delay, status = future.result()
                all_results.append((node, delay, status))
                if status == "ok":
                    marker = "✓" if delay <= max_threshold_ms else "⚠ 慢"
                    print(f"  [{i:>2}/{len(candidates)}] {node:<40} {delay:>5}ms {marker}")
                else:
                    print(f"  [{i:>2}/{len(candidates)}] {node:<40}    -   ✗ {status}")

        warmup_elapsed = time.time() - t_start

        # 排序: 仅成功的, 按延迟升序
        ok_nodes = [(n, d) for n, d, s in all_results if d is not None]
        ok_nodes.sort(key=lambda x: x[1])

        # 阈值筛选
        good = [(n, d) for n, d in ok_nodes if d <= max_threshold_ms]
        slow = [(n, d) for n, d in ok_nodes if d > max_threshold_ms]
        failed = [n for n, d, s in all_results if d is None]

        # 兜底: 至少保留 min_keep 个最快
        if len(good) < min_keep and len(ok_nodes) >= min_keep:
            good = ok_nodes[:min_keep]
            print(f"[Clash] [警告] 阈值 {max_threshold_ms}ms 太严, 放行前 {len(good)} 个最快 (min_keep={min_keep})")

        # 截 top_n
        if top_n is not None and len(good) > top_n:
            good = good[:top_n]

        fast_nodes = [n for n, d in good]

        print(f"[Clash] 预热完成 (耗时 {warmup_elapsed:.1f}s)")
        print(f"        测试: {len(candidates)} | 成功: {len(ok_nodes)} | 失败: {len(failed)}")
        print(f"        合格 (≤{max_threshold_ms}ms): {len([d for n, d in ok_nodes if d <= max_threshold_ms])} | "
              f"剔除: {len(slow)}慢 + {len(failed)}失败")
        print(f"        节点池保留: {len(fast_nodes)} 个 (后续批次仅在这些节点轮换)")
        if good:
            print(f"        最快: {good[0][0]} ({good[0][1]}ms)")
            print(f"        最慢保留: {good[-1][0]} ({good[-1][1]}ms)")

        # 替换 available_nodes 为快节点; 切到最快节点开跑
        if fast_nodes:
            self.available_nodes = fast_nodes
            self._switch_to(fast_nodes[0])
        else:
            print(f"[Clash] [警告] 预热后无可用节点, 恢复原节点 {original_node}")
            if original_node:
                self._switch_to(original_node)

        return all_results

    def test_proxy_connectivity(self, proxy_url="http://127.0.0.1:7890", test_url=None, timeout=10):
        """
        测试代理是否能正常连通外网
        注意: 默认用 Cloudflare 的连通性检测 URL (返回204), 不用 IHG (会被 Akamai 反爬拦截)
        Args:
            proxy_url: 代理地址 (如 http://127.0.0.1:7890)
            test_url: 测试连通性的目标 URL, None 时用默认连通性检测 URL
            timeout: 超时时间 (秒)
        Returns:
            (bool, str) - (是否可用, 描述信息)
        """
        # 默认测试 URL: Cloudflare 连通性检测端点 (返回 HTTP 204), 主备两个
        test_urls = [test_url] if test_url else [
            "http://cp.cloudflare.com/generate_204",
            "http://www.gstatic.com/generate_204",
        ]

        proxies = {"http": proxy_url, "https": proxy_url}
        last_err = ""
        for url in test_urls:
            try:
                resp = requests.get(
                    url,
                    proxies=proxies,
                    timeout=timeout,
                    allow_redirects=False,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/147.0.0.0"},
                )
                # 204 / 200 都算通
                if resp.status_code in (200, 204):
                    return True, f"HTTP {resp.status_code}, 响应时间 {resp.elapsed.total_seconds():.1f}s ({url})"
                last_err = f"HTTP {resp.status_code} ({url})"
            except requests.exceptions.ProxyError as e:
                last_err = f"代理连接失败: {str(e)[:80]}"
            except requests.exceptions.ConnectTimeout:
                last_err = f"代理连接超时 ({url})"
            except requests.exceptions.ReadTimeout:
                last_err = f"代理读取超时 ({url})"
            except requests.exceptions.ConnectionError as e:
                last_err = f"连接错误: {str(e)[:80]}"
            except Exception as e:
                last_err = f"未知错误: {str(e)[:80]}"
        return False, last_err


# ============ 命令行测试 ============

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Clash 代理节点管理工具")
    parser.add_argument("--test", action="store_true", help="测试连接 Clash API")
    parser.add_argument("--list", action="store_true", help="列出所有可用节点")
    parser.add_argument("--current", action="store_true", help="显示当前节点")
    parser.add_argument("--switch", type=str, default=None, help="切换到指定节点")
    parser.add_argument("--rotate", action="store_true", help="随机切换一次")
    parser.add_argument("--warmup", action="store_true",
                        help="节点速度预热: 并发测所有节点延迟, 剔除慢节点")
    parser.add_argument("--warmup-url", type=str, default=None,
                        help="预热测试 URL (默认 Cloudflare 204; 可设 IHG API 测实际路径)")
    parser.add_argument("--warmup-threshold", type=int, default=400,
                        help="预热: 超过此 ms 的节点剔除 (默认 400)")
    parser.add_argument("--warmup-top", type=int, default=15,
                        help="预热: 只保留前 N 个最快节点 (默认 15)")
    parser.add_argument("--warmup-sample", type=int, default=None,
                        help="预热: 随机采样 N 个节点测试 (节点池大时省时间)")
    parser.add_argument("--warmup-timeout", type=int, default=5,
                        help="预热: 单节点测试超时秒数 (默认 5)")
    parser.add_argument("--warmup-concurrency", type=int, default=20,
                        help="预热: 并发测试线程数 (默认 20)")
    parser.add_argument("--proxy", type=str, default="http://127.0.0.1:7890",
                        help="本地 Clash 代理地址 (默认 http://127.0.0.1:7890)")
    args = parser.parse_args()

    try:
        mgr = ClashProxyManager()
    except RuntimeError as e:
        print(e)
        sys.exit(1)

    if args.test:
        print(f"[Clash] API 地址: {mgr.api_url}")
        if mgr.is_available():
            print(f"[Clash] ✓ 连接成功")
            print(f"[Clash] 当前节点: {mgr.current_node}")
            print(f"[Clash] 可用节点: {len(mgr.available_nodes)} 个")
        else:
            print(f"[Clash] ✗ 连接失败")
            sys.exit(1)

    elif args.list:
        nodes = mgr.list_nodes()
        if not nodes:
            print("[Clash] 无可用节点")
            sys.exit(1)
        print(f"[Clash] 可用节点 ({len(nodes)} 个):")
        for i, node in enumerate(nodes, 1):
            marker = " ←" if node == mgr.current_node else ""
            print(f"  {i:2d}. {node}{marker}")

    elif args.current:
        current = mgr.get_current()
        print(f"[Clash] 当前节点: {current}")

    elif args.switch:
        success = mgr.switch(args.switch)
        if not success:
            sys.exit(1)

    elif args.rotate:
        success = mgr.rotate()
        if not success:
            sys.exit(1)

    elif args.warmup:
        results = mgr.warmup_nodes(
            proxy_url=args.proxy,
            test_url=args.warmup_url,
            timeout=args.warmup_timeout,
            max_threshold_ms=args.warmup_threshold,
            top_n=args.warmup_top,
            sample=args.warmup_sample,
            concurrency=args.warmup_concurrency,
        )
        if not results:
            print("[Clash] 预热未得到任何结果")
            sys.exit(1)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
