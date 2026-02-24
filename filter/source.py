"""节点源获取与解析模块。

支持以下来源：
- mihomo/Clash 订阅链接（返回 YAML 格式）
- 本地 YAML 配置文件
- Base64 编码的订阅链接（自动检测并解码）
"""

import base64
import logging
from pathlib import Path
from urllib.parse import urlparse, unquote

import requests
import yaml

logger = logging.getLogger(__name__)


def fetch_subscription(url: str, timeout: int = 30) -> str:
    """从订阅 URL 获取原始内容。"""
    headers = {
        "User-Agent": "clash.meta/mihomo",
        "Accept": "*/*",
    }
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def _try_base64_decode(text: str) -> str | None:
    """尝试将文本作为 Base64 解码，失败返回 None。"""
    text = text.strip()
    try:
        decoded = base64.b64decode(text + "==", validate=False).decode("utf-8")
        # 简单验证：解码结果应包含常见协议前缀
        protocols = ("ss://", "ssr://", "vmess://", "vless://", "trojan://",
                     "hysteria://", "hysteria2://", "hy2://", "tuic://")
        if any(proto in decoded for proto in protocols):
            return decoded
    except Exception:
        pass
    return None


def _parse_share_links(text: str) -> list[dict]:
    """解析分享链接格式的节点列表，转换为 mihomo proxy dict。"""
    proxies = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        proxy = _parse_single_link(line)
        if proxy:
            proxies.append(proxy)
    return proxies


def _parse_single_link(link: str) -> dict | None:
    """解析单条分享链接为 mihomo proxy dict。"""
    link = link.strip()
    if link.startswith("ss://"):
        return _parse_ss(link)
    if link.startswith("vmess://"):
        return _parse_vmess(link)
    if link.startswith("vless://"):
        return _parse_vless(link)
    if link.startswith("trojan://"):
        return _parse_trojan(link)
    if link.startswith(("hysteria2://", "hy2://")):
        return _parse_hysteria2(link)
    if link.startswith("hysteria://"):
        return _parse_hysteria(link)
    if link.startswith("tuic://"):
        return _parse_tuic(link)
    logger.debug("不支持的协议: %s", link[:30])
    return None


def _parse_ss(link: str) -> dict | None:
    """解析 ss:// 链接。"""
    try:
        link = link[5:]  # 去掉 ss://
        # 格式: method:password@host:port#name 或 base64#name
        name = ""
        if "#" in link:
            link, name = link.rsplit("#", 1)
            name = unquote(name)

        if "@" in link:
            userinfo, server_part = link.rsplit("@", 1)
            # userinfo 可能是 base64 编码的 method:password
            try:
                userinfo = base64.b64decode(userinfo + "==").decode("utf-8")
            except Exception:
                pass
            method, password = userinfo.split(":", 1)
        else:
            # 整体 base64 编码
            decoded = base64.b64decode(link + "==").decode("utf-8")
            userinfo, server_part = decoded.rsplit("@", 1)
            method, password = userinfo.split(":", 1)

        host, port = server_part.rsplit(":", 1)
        return {
            "name": name or f"ss-{host}:{port}",
            "type": "ss",
            "server": host,
            "port": int(port),
            "cipher": method,
            "password": password,
        }
    except Exception as e:
        logger.debug("解析 ss 链接失败: %s", e)
        return None


def _parse_vmess(link: str) -> dict | None:
    """解析 vmess:// 链接（V2rayN 格式 JSON base64）。"""
    import json
    try:
        raw = base64.b64decode(link[8:] + "==").decode("utf-8")
        conf = json.loads(raw)
        proxy = {
            "name": conf.get("ps", f"vmess-{conf['add']}:{conf['port']}"),
            "type": "vmess",
            "server": conf["add"],
            "port": int(conf["port"]),
            "uuid": conf["id"],
            "alterId": int(conf.get("aid", 0)),
            "cipher": conf.get("scy", "auto"),
        }
        net = conf.get("net", "tcp")
        if net == "ws":
            proxy["network"] = "ws"
            ws_opts = {}
            if conf.get("path"):
                ws_opts["path"] = conf["path"]
            if conf.get("host"):
                ws_opts["headers"] = {"Host": conf["host"]}
            if ws_opts:
                proxy["ws-opts"] = ws_opts
        elif net == "grpc":
            proxy["network"] = "grpc"
            if conf.get("path"):
                proxy["grpc-opts"] = {"grpc-service-name": conf["path"]}
        elif net == "h2":
            proxy["network"] = "h2"
            h2_opts = {}
            if conf.get("path"):
                h2_opts["path"] = conf["path"]
            if conf.get("host"):
                h2_opts["host"] = [conf["host"]]
            if h2_opts:
                proxy["h2-opts"] = h2_opts

        tls = conf.get("tls", "")
        if tls == "tls":
            proxy["tls"] = True
            if conf.get("sni"):
                proxy["servername"] = conf["sni"]
        return proxy
    except Exception as e:
        logger.debug("解析 vmess 链接失败: %s", e)
        return None


def _parse_vless(link: str) -> dict | None:
    """解析 vless:// 链接。"""
    try:
        parsed = urlparse(link)
        from urllib.parse import parse_qs
        params = parse_qs(parsed.query)

        def _get(key: str, default: str = "") -> str:
            return params.get(key, [default])[0]

        name = unquote(parsed.fragment) if parsed.fragment else ""
        proxy = {
            "name": name or f"vless-{parsed.hostname}:{parsed.port}",
            "type": "vless",
            "server": parsed.hostname,
            "port": parsed.port or 443,
            "uuid": parsed.username,
            "tls": _get("security") in ("tls", "reality"),
        }
        flow = _get("flow")
        if flow:
            proxy["flow"] = flow

        sni = _get("sni")
        if sni:
            proxy["servername"] = sni

        network = _get("type", "tcp")
        if network and network != "tcp":
            proxy["network"] = network

        if network == "ws":
            ws_opts = {}
            path = _get("path")
            if path:
                ws_opts["path"] = unquote(path)
            host = _get("host")
            if host:
                ws_opts["headers"] = {"Host": host}
            if ws_opts:
                proxy["ws-opts"] = ws_opts
        elif network == "grpc":
            sn = _get("serviceName")
            if sn:
                proxy["grpc-opts"] = {"grpc-service-name": sn}

        # Reality
        if _get("security") == "reality":
            proxy["reality-opts"] = {}
            pbk = _get("pbk")
            if pbk:
                proxy["reality-opts"]["public-key"] = pbk
            sid = _get("sid")
            if sid:
                proxy["reality-opts"]["short-id"] = sid
            fp = _get("fp")
            if fp:
                proxy["client-fingerprint"] = fp

        return proxy
    except Exception as e:
        logger.debug("解析 vless 链接失败: %s", e)
        return None


def _parse_trojan(link: str) -> dict | None:
    """解析 trojan:// 链接。"""
    try:
        parsed = urlparse(link)
        from urllib.parse import parse_qs
        params = parse_qs(parsed.query)

        def _get(key: str, default: str = "") -> str:
            return params.get(key, [default])[0]

        name = unquote(parsed.fragment) if parsed.fragment else ""
        proxy = {
            "name": name or f"trojan-{parsed.hostname}:{parsed.port}",
            "type": "trojan",
            "server": parsed.hostname,
            "port": parsed.port or 443,
            "password": unquote(parsed.username or ""),
        }
        sni = _get("sni")
        if sni:
            proxy["sni"] = sni

        network = _get("type", "tcp")
        if network == "ws":
            proxy["network"] = "ws"
            ws_opts = {}
            path = _get("path")
            if path:
                ws_opts["path"] = unquote(path)
            host = _get("host")
            if host:
                ws_opts["headers"] = {"Host": host}
            if ws_opts:
                proxy["ws-opts"] = ws_opts
        elif network == "grpc":
            proxy["network"] = "grpc"
            sn = _get("serviceName")
            if sn:
                proxy["grpc-opts"] = {"grpc-service-name": sn}

        return proxy
    except Exception as e:
        logger.debug("解析 trojan 链接失败: %s", e)
        return None


def _parse_hysteria2(link: str) -> dict | None:
    """解析 hysteria2:// / hy2:// 链接。"""
    try:
        parsed = urlparse(link)
        from urllib.parse import parse_qs
        params = parse_qs(parsed.query)

        def _get(key: str, default: str = "") -> str:
            return params.get(key, [default])[0]

        name = unquote(parsed.fragment) if parsed.fragment else ""
        proxy = {
            "name": name or f"hy2-{parsed.hostname}:{parsed.port}",
            "type": "hysteria2",
            "server": parsed.hostname,
            "port": parsed.port or 443,
            "password": unquote(parsed.username or ""),
        }
        sni = _get("sni")
        if sni:
            proxy["sni"] = sni
        obfs = _get("obfs")
        if obfs:
            proxy["obfs"] = obfs
            obfs_password = _get("obfs-password")
            if obfs_password:
                proxy["obfs-password"] = obfs_password
        insecure = _get("insecure")
        if insecure == "1":
            proxy["skip-cert-verify"] = True
        return proxy
    except Exception as e:
        logger.debug("解析 hysteria2 链接失败: %s", e)
        return None


def _parse_hysteria(link: str) -> dict | None:
    """解析 hysteria:// 链接。"""
    try:
        parsed = urlparse(link)
        from urllib.parse import parse_qs
        params = parse_qs(parsed.query)

        def _get(key: str, default: str = "") -> str:
            return params.get(key, [default])[0]

        name = unquote(parsed.fragment) if parsed.fragment else ""
        proxy = {
            "name": name or f"hysteria-{parsed.hostname}:{parsed.port}",
            "type": "hysteria",
            "server": parsed.hostname,
            "port": parsed.port or 443,
        }
        auth = _get("auth")
        if auth:
            proxy["auth-str"] = auth
        protocol = _get("protocol", "udp")
        proxy["protocol"] = protocol
        up = _get("upmbps") or _get("up")
        down = _get("downmbps") or _get("down")
        if up:
            proxy["up"] = up
        if down:
            proxy["down"] = down
        obfs = _get("obfs")
        if obfs:
            proxy["obfs"] = obfs
        sni = _get("peer") or _get("sni")
        if sni:
            proxy["sni"] = sni
        insecure = _get("insecure")
        if insecure == "1":
            proxy["skip-cert-verify"] = True
        alpn = _get("alpn")
        if alpn:
            proxy["alpn"] = alpn.split(",")
        return proxy
    except Exception as e:
        logger.debug("解析 hysteria 链接失败: %s", e)
        return None


def _parse_tuic(link: str) -> dict | None:
    """解析 tuic:// 链接。"""
    try:
        parsed = urlparse(link)
        from urllib.parse import parse_qs
        params = parse_qs(parsed.query)

        def _get(key: str, default: str = "") -> str:
            return params.get(key, [default])[0]

        name = unquote(parsed.fragment) if parsed.fragment else ""
        proxy = {
            "name": name or f"tuic-{parsed.hostname}:{parsed.port}",
            "type": "tuic",
            "server": parsed.hostname,
            "port": parsed.port or 443,
            "uuid": parsed.username or "",
            "password": parsed.password or "",
        }
        cc = _get("congestion_control", "bbr")
        proxy["congestion-controller"] = cc
        alpn = _get("alpn")
        if alpn:
            proxy["alpn"] = alpn.split(",")
        sni = _get("sni")
        if sni:
            proxy["sni"] = sni
        udp_relay_mode = _get("udp_relay_mode")
        if udp_relay_mode:
            proxy["udp-relay-mode"] = udp_relay_mode
        return proxy
    except Exception as e:
        logger.debug("解析 tuic 链接失败: %s", e)
        return None


def _parse_yaml_proxies(text: str) -> list[dict]:
    """从 YAML 文本中提取 proxies 列表，或者解析 JSON 数组形式的分享链接。"""
    try:
        import json
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = yaml.safe_load(text)

        if isinstance(data, dict):
            return data.get("proxies", [])
        if isinstance(data, list):
            # 如果是字符串列表，尝试作为分享链接解析
            if data and isinstance(data[0], str):
                proxies = []
                for item in data:
                    p = _parse_single_link(item)
                    if p:
                        proxies.append(p)
                return proxies
            # 如果本身就是 dict 列表（已经是解析好的节点）
            if data and isinstance(data[0], dict):
                return data
    except Exception as e:
        logger.warning("解析 YAML/JSON 失败: %s", e)
    return []


def parse_content(text: str) -> list[dict]:
    """自动检测内容格式并解析为 proxy dict 列表。"""
    text = text.strip()
    if not text:
        return []

    # 尝试作为 YAML 解析
    if text.startswith(("{", "proxies:", "mixed-port:", "port:")):
        proxies = _parse_yaml_proxies(text)
        if proxies:
            return proxies

    # 尝试作为 YAML 解析（可能是完整配置）
    try:
        data = yaml.safe_load(text)
        if isinstance(data, dict) and "proxies" in data:
            return data["proxies"]
    except Exception:
        pass

    # 尝试 base64 解码
    decoded = _try_base64_decode(text)
    if decoded:
        return _parse_share_links(decoded)

    # 尝试直接解析为分享链接
    protocols = ("ss://", "ssr://", "vmess://", "vless://", "trojan://",
                 "hysteria://", "hysteria2://", "hy2://", "tuic://")
    if any(text.startswith(p) for p in protocols):
        return _parse_share_links(text)

    logger.warning("无法识别的内容格式")
    return []


def load_sources(sources: list[dict]) -> list[dict]:
    """从多个来源加载并合并节点列表。

    sources 格式：
        [
            {"type": "subscription", "url": "https://..."},
            {"type": "file", "path": "/path/to/config.yaml"},
        ]
    """
    all_proxies = []
    seen_keys = set()

    for src in sources:
        src_type = src.get("type", "")
        try:
            if src_type == "subscription":
                url = src["url"]
                logger.info("正在获取订阅: %s", url[:60])
                content = fetch_subscription(url, timeout=src.get("timeout", 30))
                proxies = parse_content(content)
                logger.info("从订阅获取到 %d 个节点", len(proxies))
            elif src_type == "file":
                path = Path(src["path"]).expanduser()
                logger.info("正在读取文件: %s", path)
                content = path.read_text(encoding="utf-8")
                proxies = parse_content(content)
                logger.info("从文件获取到 %d 个节点", len(proxies))
            else:
                logger.warning("未知的源类型: %s", src_type)
                continue

            # 去重
            for p in proxies:
                key = (p.get("type"), p.get("server"), p.get("port"))
                if key not in seen_keys:
                    seen_keys.add(key)
                    all_proxies.append(p)

        except Exception as e:
            logger.error("处理源 %s 失败: %s", src, e)
            continue

    logger.info("共加载 %d 个唯一节点", len(all_proxies))
    return all_proxies
