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
            "rotate_every_n": 5,
            "exclude_keywords": ["Traffic", "Expire", "DIRECT", "REJECT"]
        }
    }

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
        }
    except Exception as e:
        print(f"[Clash] 配置加载失败: {e}")
        return None


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
        self.secret = self.config["secret"]
        self.proxy_group = self.config["proxy_group"]
        self.rotate_every_n = self.config["rotate_every_n"]
        self.exclude_keywords = self.config["exclude_keywords"]

        # 状态
        self.available_nodes = []    # 可用节点列表
        self.current_node = None     # 当前选中节点
        self.hotel_count = 0         # 已处理酒店计数
        self.switch_count = 0        # 切换次数统计
        self.fail_count = 0          # 连续失败计数

        # 初始化: 获取可用节点列表
        self._refresh_nodes()

    def _headers(self):
        """构建请求头 (含 secret 认证)"""
        headers = {"Content-Type": "application/json"}
        if self.secret:
            headers["Authorization"] = f"Bearer {self.secret}"
        return headers

    def _refresh_nodes(self):
        """从 Clash API 获取代理组信息, 刷新可用节点列表"""
        try:
            url = f"{self.api_url}/proxies/{self.proxy_group}"
            resp = requests.get(url, headers=self._headers(), timeout=5)
            resp.raise_for_status()
            data = resp.json()

            all_nodes = data.get("all", [])
            self.current_node = data.get("now", "")

            # 过滤: 排除信息节点和特殊节点
            self.available_nodes = [
                node for node in all_nodes
                if not any(kw in node for kw in self.exclude_keywords)
                and node != self.current_node  # 排除当前节点 (切换时选非当前的)
            ]

            # 其实应该保留全部可用节点, 只在选择时排除当前
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
        """切换到指定节点"""
        try:
            url = f"{self.api_url}/proxies/{self.proxy_group}"
            resp = requests.put(
                url,
                headers=self._headers(),
                json={"name": node_name},
                timeout=5,
            )
            if resp.status_code == 204 or resp.status_code == 200:
                old = self.current_node
                self.current_node = node_name
                self.switch_count += 1
                self.fail_count = 0  # 重置连续失败计数
                print(f"[Clash] 切换节点: {old} → {node_name} (第{self.switch_count}次)")
                return True
            else:
                print(f"[Clash] 切换失败: HTTP {resp.status_code} - {resp.text}")
                return False
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
        定期轮换: 随机切换到另一个节点
        Returns: True 切换成功, False 切换失败或无可用节点
        """
        node = self._pick_random_node(exclude_current=True)
        if not node:
            print("[Clash] 无可用节点可切换")
            return False
        return self._switch_to(node)

    def on_hotel_done(self):
        """
        每完成一个酒店后调用, 达到 rotate_every_n 时自动切换
        Returns: True 如果触发了切换, False 如果未触发
        """
        self.hotel_count += 1
        if self.hotel_count >= self.rotate_every_n:
            self.hotel_count = 0
            return self.rotate()
        return False

    def on_failure(self):
        """
        请求失败时调用, 立即切换节点
        Returns: True 切换成功, False 切换失败
        """
        self.fail_count += 1
        print(f"[Clash] 检测到失败 (连续第{self.fail_count}次), 切换节点...")
        # 等待一小段时间让新节点生效
        success = self.rotate()
        if success:
            time.sleep(1)  # 给 Clash 切换时间
        return success

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

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
