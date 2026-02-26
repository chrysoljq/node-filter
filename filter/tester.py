"""节点测试模块（单实例 + 多 listener 并发架构）。

启动一个 mihomo 实例，通过 listeners 为每个节点分配独立的入站端口，
然后并发测试连通性并获取出口 IP。

流程：
  1. 将节点分批（每批 = concurrency 个）
  2. 每批：生成 mihomo 配置（listeners 绑定端口→节点）→ 启动 → 并发测试 → 停止
  3. 汇总结果
"""

import logging
import os
import signal
import shutil
import socket
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
import yaml

logger = logging.getLogger(__name__)


def _find_free_ports(count: int, start: int = 20000) -> list[int]:
    """找到 count 个连续可用端口。"""
    ports = []
    port = start
    while len(ports) < count and port < 65535:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                ports.append(port)
        except OSError:
            pass
        port += 1
    if len(ports) < count:
        raise RuntimeError(f"只找到 {len(ports)}/{count} 个可用端口")
    return ports


def _clean_proxy(proxy: dict) -> dict:
    """移除内部标记字段。"""
    return {k: v for k, v in proxy.items() if not k.startswith("_")}


def _generate_config(
    proxies: list[dict],
    port_map: dict[str, int],
    api_port: int,
) -> dict:
    """生成 mihomo 配置：每个节点通过 listener 绑定到独立端口。

    Args:
        proxies: 本批节点列表
        port_map: {节点名: socks5 端口} 映射
        api_port: RESTful API 端口
    """
    clean = []
    seen_names = {}

    for p in proxies:
        cp = _clean_proxy(p)
        name = cp.get("name", "unknown")

        if name in seen_names:
            seen_names[name] += 1
            name = f"{name}_{seen_names[name]}"
            cp["name"] = name
        else:
            seen_names[name] = 0

        clean.append(cp)

    names = [p["name"] for p in clean]

    # 构建 listeners：每个节点一个 socks5 入站
    listeners = []
    for cp in clean:
        name = cp["name"]
        port = port_map.get(name)
        if port:
            listeners.append({
                "name": f"socks-{name}",
                "type": "socks",
                "port": port,
                "proxy": name,
            })

    return {
        "mixed-port": 0,
        "external-controller": f"127.0.0.1:{api_port}",
        "mode": "rule",
        "log-level": "warning",
        "ipv6": False,
        "proxies": clean,
        "listeners": listeners,
        "proxy-groups": [
            {
                "name": "GLOBAL",
                "type": "select",
                "proxies": names,
            },
        ],
        "rules": ["MATCH,GLOBAL"],
    }


def _wait_for_api(proc, api_port: int, timeout: float = 60.0) -> bool:
    """等待 mihomo API 就绪。"""
    url = f"http://127.0.0.1:{api_port}/version"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            logger.error("mihomo 进程已意外退出")
            return False
        try:
            resp = requests.get(url, timeout=1)
            if resp.status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(0.5)
    return False


def _test_delay(socks_port: int, test_url: str, timeout: int) -> tuple[bool, int]:
    """通过指定 socks5 端口测试连通性。"""
    proxies = {
        "http": f"socks5h://127.0.0.1:{socks_port}",
        "https": f"socks5h://127.0.0.1:{socks_port}",
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


def _get_exit_ip(socks_port: int, timeout: int = 8) -> str | None:
    """通过指定 socks5 端口获取出口 IP。"""
    proxies = {
        "http": f"socks5h://127.0.0.1:{socks_port}",
        "https": f"socks5h://127.0.0.1:{socks_port}",
    }
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


def _test_single_proxy(
    name: str,
    socks_port: int,
    test_url: str,
    timeout: int,
    index: int,
    total: int,
) -> dict:
    """测试单个节点（在线程池中并发执行）。"""
    result = {"name": name, "alive": False, "delay": 0, "exit_ip": None}

    alive, delay = _test_delay(socks_port, test_url, timeout)
    result["alive"] = alive
    result["delay"] = delay

    if not alive:
        result["error"] = "连接超时或失败"
        logger.info("  [%d/%d] ✗ %s - 不可用", index, total, name)
        return result

    exit_ip = _get_exit_ip(socks_port, timeout=timeout)
    result["exit_ip"] = exit_ip

    logger.info(
        "  [%d/%d] ✓ %s - %dms - 出口IP: %s",
        index, total, name, delay, exit_ip or "未知",
    )
    return result


class MihomoInstance:
    """管理一个 mihomo 进程的生命周期。"""

    def __init__(self, mihomo_bin: str = "mihomo"):
        self.mihomo_bin = mihomo_bin
        self.proc = None
        self.tmpdir = None
        self.api_port = 0

    def start(self, proxies: list[dict], port_map: dict[str, int]) -> bool:
        """启动 mihomo，使用 listeners 为每个节点分配独立端口。"""
        self.api_port = _find_free_ports(1, start=19000)[0]

        config = _generate_config(proxies, port_map, self.api_port)

        self.tmpdir = tempfile.mkdtemp(prefix="mihomo_filter_")
        config_path = Path(self.tmpdir) / "config.yaml"
        config_path.write_text(
            yaml.dump(config, allow_unicode=True, default_flow_style=False),
            encoding="utf-8",
        )

        logger.info(
            "启动 mihomo (api=%d, %d 个节点, %d 个 listener)...",
            self.api_port, len(proxies), len(config.get("listeners", [])),
        )

        self.stderr_log = Path(self.tmpdir) / "mihomo_stderr.log"
        self.stderr_file = open(self.stderr_log, "w")

        try:
            self.proc = subprocess.Popen(
                [self.mihomo_bin, "-d", self.tmpdir],
                stdout=self.stderr_file,
                stderr=self.stderr_file,
                preexec_fn=os.setsid,
            )
        except FileNotFoundError:
            logger.error("找不到 mihomo 二进制: %s", self.mihomo_bin)
            self.cleanup()
            return False

        if not _wait_for_api(self.proc, self.api_port):
            exit_code = self.proc.poll()
            self.stderr_file.close()
            err_msg = self.stderr_log.read_text(encoding="utf-8")
            logger.error(
                "mihomo 启动失败或超时 (ExitCode: %s)。日志内容:\n%s",
                exit_code, err_msg,
            )
            self.stop()
            return False

        logger.info("mihomo 启动成功")
        return True

    def stop(self):
        """停止 mihomo 进程。"""
        if hasattr(self, "stderr_file") and self.stderr_file and not self.stderr_file.closed:
            self.stderr_file.close()
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
    concurrency: int = 20,
) -> list[dict]:
    """批量并发测试节点：连通性 + 获取出口 IP。

    单个 mihomo 实例 + 多 listeners 端口，通过线程池并发测试。
    节点按 concurrency 分批，每批启动一次 mihomo。

    Args:
        proxies: 节点列表
        mihomo_bin: mihomo 二进制路径
        test_url: 测试 URL
        timeout: 超时秒数
        concurrency: 并发数（每批同时测试的节点数）

    Returns:
        [{"name", "alive", "delay", "exit_ip", "error"}, ...]
    """
    if not proxies:
        return []

    results = []
    total = len(proxies)
    batch_size = concurrency

    logger.info("并发测试: %d 个节点, 每批 %d 个并发", total, batch_size)

    # 分批处理
    for batch_start in range(0, total, batch_size):
        batch = proxies[batch_start:batch_start + batch_size]
        batch_num = batch_start // batch_size + 1
        total_batches = (total + batch_size - 1) // batch_size
        logger.info("── 批次 %d/%d (%d 个节点) ──", batch_num, total_batches, len(batch))

        # 为本批节点分配端口
        try:
            ports = _find_free_ports(len(batch), start=20000)
        except RuntimeError as e:
            logger.error("端口分配失败: %s", e)
            results.extend(
                {"name": p.get("name", "?"), "alive": False,
                 "delay": 0, "exit_ip": None, "error": "端口分配失败"}
                for p in batch
            )
            continue

        # 构建 name → port 映射（需要处理重名，与 _generate_config 一致）
        seen_names = {}
        resolved_names = []
        for p in batch:
            name = p.get("name", "unknown")
            if name in seen_names:
                seen_names[name] += 1
                name = f"{name}_{seen_names[name]}"
            else:
                seen_names[name] = 0
            resolved_names.append(name)

        port_map = dict(zip(resolved_names, ports))

        with MihomoInstance(mihomo_bin) as mi:
            if not mi.start(batch, port_map):
                results.extend(
                    {"name": p.get("name", "?"), "alive": False,
                     "delay": 0, "exit_ip": None, "error": "mihomo 启动失败"}
                    for p in batch
                )
                continue

            # 等待 listeners 就绪
            time.sleep(1)

            # 并发测试本批所有节点
            with ThreadPoolExecutor(max_workers=len(batch)) as executor:
                futures = {}
                for i, (rname, port) in enumerate(port_map.items()):
                    global_idx = batch_start + i + 1
                    future = executor.submit(
                        _test_single_proxy,
                        rname, port, test_url, timeout,
                        global_idx, total,
                    )
                    futures[future] = rname

                for future in as_completed(futures):
                    try:
                        result = future.result()
                        results.append(result)
                    except Exception as e:
                        name = futures[future]
                        logger.error("测试节点异常 %s: %s", name, e)
                        results.append({
                            "name": name, "alive": False,
                            "delay": 0, "exit_ip": None, "error": str(e),
                        })

    alive_count = sum(1 for r in results if r["alive"])
    logger.info("测试完成: %d/%d 存活", alive_count, total)
    return results
