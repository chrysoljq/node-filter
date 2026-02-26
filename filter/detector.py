"""机房节点检测模块。

支持两种检测模式：
1. 入口 IP 快速检测（无需 mihomo）：DNS 解析节点域名 → 查询入口 IP
2. 出口 IP 精确检测（需要 mihomo）：通过代理获取出口 IP → 查询出口 IP

三重判定机制：
- ASN 黑名单：匹配已知机房 ASN
- IP-API 查询：hosting 标志 + org/isp 关键词匹配
- AbuseIPDB 查询：usageType 机房标记 + abuseConfidenceScore 风险评分
"""

import ipaddress
import logging
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
import yaml

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent.parent / "data"
_ASN_FILE = _DATA_DIR / "datacenter_asn.yaml"


def _load_datacenter_data() -> tuple[set[int], list[str]]:
    """加载机房 ASN 列表和关键词列表。"""
    try:
        data = yaml.safe_load(_ASN_FILE.read_text(encoding="utf-8"))
        asns = set(data.get("datacenter_asns", {}).keys())
        keywords = [kw.lower() for kw in data.get("datacenter_keywords", [])]
        logger.info("加载机房数据: %d 个 ASN, %d 个关键词", len(asns), len(keywords))
        return asns, keywords
    except Exception as e:
        logger.error("加载机房数据失败: %s", e)
        return set(), []


_dc_asns, _dc_keywords = _load_datacenter_data()

# AbuseIPDB 中表示机房/托管的 usageType 关键词
_ABUSE_DC_USAGE_TYPES = {
    "data center/web hosting/transit",
    "hosting",
    "content delivery network",
}


class IPInfo:
    """单个 IP 的查询结果（ip-api.com）。"""

    def __init__(self, ip: str, data: dict):
        self.ip = ip
        self.raw = data
        self.success = data.get("status") == "success"
        self.country = data.get("country", "")
        self.country_code = data.get("countryCode", "")
        self.region = data.get("regionName", "")
        self.city = data.get("city", "")
        self.isp = data.get("isp", "")
        self.org = data.get("org", "")
        self.as_number = self._extract_asn(data.get("as", ""))
        self.as_name = data.get("as", "")
        self.hosting = data.get("hosting", False)

    @staticmethod
    def _extract_asn(as_str: str) -> int | None:
        if not as_str:
            return None
        parts = as_str.split()
        if parts and parts[0].upper().startswith("AS"):
            try:
                return int(parts[0][2:])
            except ValueError:
                pass
        return None


class AbuseIPInfo:
    """单个 IP 的 AbuseIPDB 查询结果。"""

    def __init__(self, ip: str, data: dict):
        self.ip = ip
        self.raw = data
        self.success = bool(data)
        self.abuse_score = data.get("abuseConfidenceScore", 0)
        self.usage_type = data.get("usageType", "")
        self.isp = data.get("isp", "")
        self.domain = data.get("domain", "")
        self.is_tor = data.get("isTor", False)
        self.is_whitelisted = data.get("isWhitelisted", False)
        self.total_reports = data.get("totalReports", 0)
        self.country_code = data.get("countryCode", "")

    @property
    def is_datacenter(self) -> bool:
        """根据 usageType 判断是否机房。"""
        if not self.usage_type:
            return False
        return self.usage_type.lower() in _ABUSE_DC_USAGE_TYPES


def query_ip_batch(ips: list[str], timeout: int = 10) -> dict[str, IPInfo]:
    """批量查询 IP 信息（ip-api.com）。

    返回 {ip: IPInfo} 映射。
    ip-api.com 限制：batch 最多 100 个，每分钟 15 次。
    """
    result = {}
    batch_size = 100

    for i in range(0, len(ips), batch_size):
        batch = ips[i:i + batch_size]
        payload = [
            {"query": ip, "fields": "status,country,countryCode,regionName,"
                                     "city,isp,org,as,hosting"}
            for ip in batch
        ]
        try:
            resp = requests.post(
                "http://ip-api.com/batch",
                json=payload,
                timeout=timeout,
            )
            resp.raise_for_status()
            for ip, data in zip(batch, resp.json()):
                result[ip] = IPInfo(ip, data)
        except Exception as e:
            logger.error("IP-API batch 查询失败: %s", e)
            for ip in batch:
                result[ip] = IPInfo(ip, {"status": "fail"})

        if i + batch_size < len(ips):
            logger.debug("IP-API 速率限制等待...")
            time.sleep(4)

    return result


# ─── AbuseIPDB 查询 ───


def _query_abuseipdb_single(
    ip: str,
    api_key: str,
    timeout: int = 10,
) -> AbuseIPInfo:
    """查询单个 IP 的 AbuseIPDB 信息。"""
    try:
        resp = requests.get(
            "https://api.abuseipdb.com/api/v2/check",
            params={"ipAddress": ip},
            headers={
                "Key": api_key,
                "Accept": "application/json",
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        return AbuseIPInfo(ip, data)
    except Exception as e:
        logger.debug("AbuseIPDB 查询失败 %s: %s", ip, e)
        return AbuseIPInfo(ip, {})


def query_abuseipdb_batch(
    ips: list[str],
    api_key: str,
    concurrency: int = 5,
    timeout: int = 10,
) -> dict[str, AbuseIPInfo]:
    """并发查询多个 IP 的 AbuseIPDB 信息。

    Args:
        ips: IP 列表
        api_key: AbuseIPDB API Key
        concurrency: 并发线程数（建议不超过 5，避免触发速率限制）
        timeout: 单次查询超时

    Returns: {ip: AbuseIPInfo} 映射
    """
    result = {}
    if not ips or not api_key:
        return result

    workers = min(concurrency, len(ips))
    logger.info("AbuseIPDB 查询 %d 个 IP (并发 %d)...", len(ips), workers)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(_query_abuseipdb_single, ip, api_key, timeout): ip
            for ip in ips
        }
        for future in as_completed(future_map):
            ip = future_map[future]
            try:
                result[ip] = future.result()
            except Exception as e:
                logger.debug("AbuseIPDB 异常 %s: %s", ip, e)
                result[ip] = AbuseIPInfo(ip, {})

    success_count = sum(1 for v in result.values() if v.success)
    logger.info("AbuseIPDB 查询完成: %d/%d 成功", success_count, len(ips))
    return result


# ─── 综合判定 ───


def is_datacenter(
    info: IPInfo,
    abuse_info: AbuseIPInfo | None = None,
) -> tuple[bool, str]:
    """综合判断 IP 是否为机房。返回 (是否机房, 原因)。

    综合 ip-api + AbuseIPDB + ASN 黑名单三重判定。
    """
    if not info.success:
        return False, "查询失败，默认保留"

    reasons = []

    # 1. ip-api hosting 标记
    if info.hosting:
        reasons.append("ip-api hosting 标记")

    # 2. ASN 黑名单
    if info.as_number and info.as_number in _dc_asns:
        reasons.append(f"ASN {info.as_number} 在黑名单中")

    # 3. 关键词匹配
    check_text = f"{info.org} {info.isp} {info.as_name}".lower()
    matched = [kw for kw in _dc_keywords if kw in check_text]
    if matched:
        reasons.append(f"关键词: {', '.join(matched[:3])}")

    # 4. AbuseIPDB 判定
    if abuse_info and abuse_info.success:
        if abuse_info.is_datacenter:
            reasons.append(f"AbuseIPDB: {abuse_info.usage_type}")
        if abuse_info.is_tor:
            reasons.append("AbuseIPDB: Tor 节点")
        if abuse_info.abuse_score >= 25:
            reasons.append(f"AbuseIPDB: 风险评分 {abuse_info.abuse_score}")

    if reasons:
        return True, "; ".join(reasons)
    return False, "非机房"


# ─── 入口 IP 快速检测（无需 mihomo）───


def _resolve_server(server: str) -> str | None:
    """解析域名为 IP。已是 IP 则直接返回。"""
    try:
        ipaddress.ip_address(server)
        return server
    except ValueError:
        pass
    try:
        results = socket.getaddrinfo(server, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        if results:
            return results[0][4][0]
    except socket.gaierror:
        pass
    return None


def detect_by_entry_ip(
    proxies: list[dict],
    abuseipdb_key: str = "",
) -> tuple[list[dict], list[dict], list[dict]]:
    """通过入口 IP 检测机房节点（快速模式，无需 mihomo）。

    Returns: (residential, datacenter, unknown)
    """
    # 并发解析所有 server → IP
    unique_servers = list({p.get("server", "") for p in proxies} - {""})
    server_ips = {}

    with ThreadPoolExecutor(max_workers=min(20, len(unique_servers) or 1)) as executor:
        future_map = {
            executor.submit(_resolve_server, server): server
            for server in unique_servers
        }
        for future in as_completed(future_map):
            server = future_map[future]
            try:
                server_ips[server] = future.result()
            except Exception:
                server_ips[server] = None

    resolved = sum(1 for v in server_ips.values() if v)
    logger.info("DNS 解析: %d/%d 成功", resolved, len(server_ips))

    # 收集需要查询的 IP
    proxy_ip_pairs = []
    no_ip = []
    for p in proxies:
        ip = server_ips.get(p.get("server", ""))
        if ip:
            proxy_ip_pairs.append((p, ip))
        else:
            no_ip.append(p)

    if not proxy_ip_pairs:
        return [], [], no_ip

    # 批量查询
    unique_ips = list({ip for _, ip in proxy_ip_pairs})
    logger.info("查询 %d 个入口 IP...", len(unique_ips))
    ip_infos = query_ip_batch(unique_ips)

    # AbuseIPDB 查询
    abuse_infos = {}
    if abuseipdb_key:
        abuse_infos = query_abuseipdb_batch(unique_ips, abuseipdb_key)

    # 分类
    residential, datacenter = [], []
    for p, ip in proxy_ip_pairs:
        info = ip_infos.get(ip)
        if not info:
            no_ip.append(p)
            continue

        abuse_info = abuse_infos.get(ip)
        is_dc, reason = is_datacenter(info, abuse_info)
        name = p.get("name", "?")
        p["_entry_ip"] = ip
        p["_entry_org"] = info.org
        p["_entry_country"] = info.country_code

        if is_dc:
            p["_filter_reason"] = f"入口IP({ip}): {reason}"
            logger.info("  [机房] %s | %s | %s", name, ip, reason)
            datacenter.append(p)
        else:
            logger.info("  [保留] %s | %s | %s", name, ip, info.org)
            residential.append(p)

    logger.info("入口IP检测: 保留 %d, 机房 %d, 未知 %d",
                len(residential), len(datacenter), len(no_ip))
    return residential, datacenter, no_ip


# ─── 出口 IP 精确检测（需要 mihomo + tester 的结果）───


def detect_by_exit_ip(
    proxies: list[dict],
    test_results: list[dict],
    abuseipdb_key: str = "",
) -> tuple[list[dict], list[dict], list[dict]]:
    """通过出口 IP 检测机房节点（精确模式）。

    Args:
        proxies: 节点列表
        test_results: tester.test_proxies() 的返回结果（含 exit_ip）
        abuseipdb_key: AbuseIPDB API Key（可选）

    Returns: (residential, datacenter, unknown)
    """
    # 建立 name → test_result 映射
    result_map = {r["name"]: r for r in test_results}

    # 收集存活且有出口 IP 的节点
    has_exit_ip = []
    no_exit_ip = []

    for p in proxies:
        name = p.get("name", "")
        tr = result_map.get(name)
        if not tr or not tr.get("alive"):
            no_exit_ip.append(p)
            continue
        exit_ip = tr.get("exit_ip")
        if not exit_ip:
            no_exit_ip.append(p)
            continue
        has_exit_ip.append((p, exit_ip, tr))

    if not has_exit_ip:
        return [], [], no_exit_ip

    # 批量查询出口 IP
    unique_ips = list({ip for _, ip, _ in has_exit_ip})
    logger.info("查询 %d 个出口 IP...", len(unique_ips))
    ip_infos = query_ip_batch(unique_ips)

    # AbuseIPDB 查询
    abuse_infos = {}
    if abuseipdb_key:
        abuse_infos = query_abuseipdb_batch(unique_ips, abuseipdb_key)

    residential, datacenter = [], []
    for p, exit_ip, tr in has_exit_ip:
        info = ip_infos.get(exit_ip)
        if not info:
            no_exit_ip.append(p)
            continue

        abuse_info = abuse_infos.get(exit_ip)
        is_dc, reason = is_datacenter(info, abuse_info)
        name = p.get("name", "?")
        p["_exit_ip"] = exit_ip
        p["_exit_org"] = info.org
        p["_exit_country"] = info.country_code
        p["_delay"] = tr.get("delay", 0)

        if is_dc:
            p["_filter_reason"] = f"出口IP({exit_ip}): {reason}"
            logger.info("  [机房] %s | 出口 %s | %s", name, exit_ip, reason)
            datacenter.append(p)
        else:
            logger.info("  [保留] %s | 出口 %s | %s | %dms",
                        name, exit_ip, info.org, tr.get("delay", 0))
            residential.append(p)

    logger.info("出口IP检测: 保留 %d, 机房 %d, 未知 %d",
                len(residential), len(datacenter), len(no_exit_ip))
    return residential, datacenter, no_exit_ip
