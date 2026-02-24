"""节点测试模块（单实例架构）。

启动一个 mihomo 实例加载所有节点，通过 RESTful API 切换节点，
逐个测试连通性并获取出口 IP。

流程：
  1. 生成包含所有节点的 mihomo 配置
  2. 启动 mihomo 进程
  3. 遍历节点：通过 API 切换 → 测延迟 → 获取出口 IP
  4. 关闭 mihomo
"""

import logging
import os
import signal
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import requests
import yaml

logger = logging.getLogger(__name__)


def _find_free_port(start: int = 10000) -> int:
    """找一个可用端口。"""
    for port in range(start, start + 1000):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    raise RuntimeError("找不到可用端口")


def _clean_proxy(proxy: dict) -> dict:
    """移除内部标记字段。"""
    return {k: v for k, v in proxy.items() if not k.startswith("_")}


def _generate_config(proxies: list[dict], mixed_port: int, api_port: int) -> dict:
    """生成包含所有节点的 mihomo 配置，每个节点一个 select 组便于切换。"""
    clean = [_clean_proxy(p) for p in proxies]
    names = [p["name"] for p in clean]

    return {
        "mixed-port": mixed_port,
        "external-controller": f"127.0.0.1:{api_port}",
        "mode": "rule",
        "log-level": "silent",
        "ipv6": False,
        "proxies": clean,
        "proxy-groups": [
            {
                "name": "GLOBAL",
                "type": "select",
                "proxies": names,
            },
        ],
        "rules": ["MATCH,GLOBAL"],
    }


def _wait_for_api(api_port: int, timeout: float = 8.0) -> bool:
    """等待 mihomo API 就绪。"""
    url = f"http://127.0.0.1:{api_port}/version"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = requests.get(url, timeout=1)
            if resp.status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(0.3)
    return False


def _switch_proxy(api_port: int, proxy_name: str) -> bool:
    """通过 API 切换 GLOBAL 组的活跃节点。"""
    url = f"http://127.0.0.1:{api_port}/proxies/GLOBAL"
    try:
        resp = requests.put(url, json={"name": proxy_name}, timeout=3)
        return resp.status_code == 204
    except requests.RequestException as e:
        logger.debug("切换节点失败 %s: %s", proxy_name, e)
        return False


def _test_delay(mixed_port: int, test_url: str, timeout: int) -> tuple[bool, int]:
    """通过代理测试连通性，返回 (alive, delay_ms)。"""
    proxies = {
        "http": f"http://127.0.0.1:{mixed_port}",
        "https": f"http://127.0.0.1:{mixed_port}",
    }
    start = time.time()
    try:
        resp = requests.get(test_url, proxies=proxies, timeout=timeout)
        delay = int((time.time() - start) * 1000)
        if resp.status_code in (200, 204):
            return True, delay
        return False, 0
    except requests.RequestException:
        return False, 0


def _get_exit_ip(mixed_port: int, timeout: int = 8) -> str | None:
    """通过代理获取出口 IP。"""
    proxies = {
        "http": f"http://127.0.0.1:{mixed_port}",
        "https": f"http://127.0.0.1:{mixed_port}",
    }
    # 多个 IP 查询源做 fallback
    ip_apis = [
        ("http://ip-api.com/json/?fields=query", lambda r: r.json().get("query")),
        ("https://api.ipify.org?format=json", lambda r: r.json().get("ip")),
        ("https://ifconfig.me/ip", lambda r: r.text.strip()),
    ]
    for url, extractor in ip_apis:
        try:
            resp = requests.get(url, proxies=proxies, timeout=timeout)
            if resp.status_code == 200:
                ip = extractor(resp)
                if ip:
                    return ip
        except Exception:
            continue
    return None


class MihomoInstance:
    """管理一个 mihomo 进程的生命周期。"""

    def __init__(self, mihomo_bin: str = "mihomo"):
        self.mihomo_bin = mihomo_bin
        self.proc = None
        self.tmpdir = None
        self.mixed_port = 0
        self.api_port = 0

    def start(self, proxies: list[dict]) -> bool:
        """启动 mihomo，加载所有节点。"""
        self.mixed_port = _find_free_port(10000)
        self.api_port = _find_free_port(self.mixed_port + 1)

        config = _generate_config(proxies, self.mixed_port, self.api_port)

        self.tmpdir = tempfile.mkdtemp(prefix="mihomo_filter_")
        config_path = Path(self.tmpdir) / "config.yaml"
        config_path.write_text(
            yaml.dump(config, allow_unicode=True, default_flow_style=False),
            encoding="utf-8",
        )

        logger.info(
            "启动 mihomo (port=%d, api=%d, %d 个节点)...",
            self.mixed_port, self.api_port, len(proxies),
        )

        try:
            self.proc = subprocess.Popen(
                [self.mihomo_bin, "-d", self.tmpdir],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=os.setsid,
            )
        except FileNotFoundError:
            logger.error("找不到 mihomo 二进制: %s", self.mihomo_bin)
            self.cleanup()
            return False

        if not _wait_for_api(self.api_port):
            logger.error("mihomo API 启动超时")
            self.stop()
            return False

        logger.info("mihomo 启动成功")
        return True

    def stop(self):
        """停止 mihomo 进程。"""
        if self.proc:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                except Exception:
                    pass
            self.proc = None
            logger.info("mihomo 已停止")
        self.cleanup()

    def cleanup(self):
        """清理临时目录。"""
        if self.tmpdir:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
            self.tmpdir = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stop()


def test_proxies(
    proxies: list[dict],
    mihomo_bin: str = "mihomo",
    test_url: str = "https://www.gstatic.com/generate_204",
    timeout: int = 10,
) -> list[dict]:
    """批量测试节点：连通性 + 获取出口 IP。

    启动单个 mihomo 实例，通过 API 逐个切换节点测试。

    Returns:
        [
            {
                "name": "节点名",
                "alive": True/False,
                "delay": 123,
                "exit_ip": "1.2.3.4" | None,
                "error": "..." | None,
            },
            ...
        ]
    """
    if not proxies:
        return []

    results = []

    with MihomoInstance(mihomo_bin) as mi:
        if not mi.start(proxies):
            # mihomo 启动失败，所有节点标记为失败
            return [
                {"name": p.get("name", "?"), "alive": False,
                 "delay": 0, "exit_ip": None, "error": "mihomo 启动失败"}
                for p in proxies
            ]

        total = len(proxies)
        for i, proxy in enumerate(proxies):
            name = proxy.get("name", "unknown")
            result = {"name": name, "alive": False, "delay": 0, "exit_ip": None}

            # 切换节点
            if not _switch_proxy(mi.api_port, name):
                result["error"] = "切换节点失败"
                logger.info("  [%d/%d] ✗ %s - 切换失败", i + 1, total, name)
                results.append(result)
                continue

            # 短暂等待连接建立
            time.sleep(0.3)

            # 测延迟
            alive, delay = _test_delay(mi.mixed_port, test_url, timeout)
            result["alive"] = alive
            result["delay"] = delay

            if not alive:
                result["error"] = "连接超时或失败"
                logger.info("  [%d/%d] ✗ %s - 不可用", i + 1, total, name)
                results.append(result)
                continue

            # 获取出口 IP
            exit_ip = _get_exit_ip(mi.mixed_port, timeout=timeout)
            result["exit_ip"] = exit_ip

            logger.info(
                "  [%d/%d] ✓ %s - %dms - 出口IP: %s",
                i + 1, total, name, delay, exit_ip or "未知",
            )
            results.append(result)

    alive_count = sum(1 for r in results if r["alive"])
    logger.info("测试完成: %d/%d 存活", alive_count, total)
    return results
