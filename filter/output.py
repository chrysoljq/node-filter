"""输出模块。

生成筛选后的 mihomo/Clash YAML 配置文件。
"""

import logging
import requests
from datetime import datetime, timezone
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


def push_to_worker(content: str, url: str, token: str = None, data_type: str = "yaml") -> bool:
    """将配置内容推送到远程 Worker。

    Args:
        content: 要推送的内容
        url: Worker 的 API 接口地址（如 .../api/yaml 或 .../api/report）
        token: 鉴权 Token
        data_type: 数据类型，'yaml' 或 'report'
    """
    try:
        headers = {"Content-Type": "application/json"}
        params = {}
        if token:
            params["token"] = token

        payload = {data_type: content}
        resp = requests.post(url, json=payload, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        logger.info("内容已成功推送到远程 Worker: %s (%s)", url, data_type)
        return True
    except Exception as e:
        logger.error("推送到远程 Worker 失败 (%s): %s", data_type, e)
        return False


# 默认的代理组模板
_DEFAULT_PROXY_GROUPS = [
    {
        "name": "🚀 节点选择",
        "type": "select",
        "proxies": ["♻️ 自动选择", "DIRECT"],
    },
    {
        "name": "♻️ 自动选择",
        "type": "url-test",
        "url": "https://www.gstatic.com/generate_204",
        "interval": 300,
        "tolerance": 50,
        "proxies": [],
    },
]

# 默认规则
_DEFAULT_RULES = [
    "GEOIP,LAN,DIRECT",
    "GEOIP,CN,DIRECT",
    "MATCH,🚀 节点选择",
]


def _clean_proxy(proxy: dict) -> dict:
    """移除内部标记字段。"""
    return {k: v for k, v in proxy.items() if not k.startswith("_")}


def generate_mihomo_config(
    proxies: list[dict],
    output_path: str | Path,
    mixed_port: int = 7890,
    api_port: int = 9090,
    extra_proxy_groups: list[dict] | None = None,
    extra_rules: list[str] | None = None,
    test_results: list[dict] | None = None,
) -> Path:
    """生成完整的 mihomo 配置文件。

    Args:
        proxies: 筛选后的节点列表
        output_path: 输出文件路径
        mixed_port: 混合代理端口
        api_port: API 端口
        extra_proxy_groups: 额外的代理组
        extra_rules: 额外的规则
        test_results: 连通性测试结果（用于添加延迟信息到节点名）
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # 清理节点
    clean_proxies = [_clean_proxy(p) for p in proxies]

    if not clean_proxies:
        logger.warning("没有可用节点，生成空配置")

    # 节点名列表
    proxy_names = [p["name"] for p in clean_proxies]

    # 构建代理组
    proxy_groups = []
    for group in _DEFAULT_PROXY_GROUPS:
        g = dict(group)
        if g["name"] == "🚀 节点选择":
            g["proxies"] = ["♻️ 自动选择", "DIRECT"] + proxy_names
        elif g["name"] == "♻️ 自动选择":
            g["proxies"] = proxy_names.copy()
        proxy_groups.append(g)

    if extra_proxy_groups:
        proxy_groups.extend(extra_proxy_groups)

    # 构建规则
    rules = list(extra_rules) if extra_rules else list(_DEFAULT_RULES)

    # 构建完整配置
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    config = {
        "# 由 mihomo-node-filter 自动生成": None,
        "# 更新时间": now,
        "mixed-port": mixed_port,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "info",
        "ipv6": False,
        "external-controller": f"127.0.0.1:{api_port}",
        "dns": {
            "enable": True,
            "enhanced-mode": "fake-ip",
            "fake-ip-range": "198.18.0.1/16",
            "nameserver": [
                "https://doh.pub/dns-query",
                "https://dns.alidns.com/dns-query",
            ],
        },
        "proxies": clean_proxies,
        "proxy-groups": proxy_groups,
        "rules": rules,
    }

    # 自定义 YAML 输出，去掉 None 值的注释行
    lines = []
    lines.append(f"# 由 mihomo-node-filter 自动生成")
    lines.append(f"# 更新时间: {now}")
    lines.append(f"# 节点数量: {len(clean_proxies)}")
    lines.append("")

    # 移除注释键
    config.pop("# 由 mihomo-node-filter 自动生成", None)
    config.pop("# 更新时间", None)

    yaml_content = yaml.dump(
        config,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )

    full_content = "\n".join(lines) + yaml_content

    output_path.write_text(full_content, encoding="utf-8")
    logger.info("配置文件已写入: %s (%d 个节点)", output_path, len(clean_proxies))
    return output_path


def generate_proxy_list(
    proxies: list[dict],
    output_path: str | Path,
) -> Path:
    """生成仅包含 proxies 列表的 YAML（方便嵌入其他配置）。"""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    clean_proxies = [_clean_proxy(p) for p in proxies]
    content = yaml.dump(
        {"proxies": clean_proxies},
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    header = (
        f"# 由 mihomo-node-filter 自动生成\n"
        f"# 更新时间: {now}\n"
        f"# 节点数量: {len(clean_proxies)}\n"
        f"\n"
    )

    output_path.write_text(header + content, encoding="utf-8")
    logger.info("节点列表已写入: %s (%d 个节点)", output_path, len(clean_proxies))
    return output_path


def generate_report(
    residential: list[dict],
    datacenter: list[dict],
    unknown: list[dict],
    test_results: list[dict] | None,
    output_path: str | Path,
) -> Path:
    """生成筛选报告。"""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    lines = [
        f"# 节点筛选报告",
        f"# 生成时间: {now}",
        "",
        f"## 总计",
        f"- 住宅节点: {len(residential)}",
        f"- 机房节点: {len(datacenter)}",
        f"- 未知节点: {len(unknown)}",
        "",
    ]

    if residential:
        lines.append("## 住宅节点（保留）")
        for p in residential:
            name = p.get("name", "unknown")
            ip = p.get("_exit_ip", p.get("_entry_ip", ""))
            org = p.get("_exit_org", p.get("_entry_org", ""))
            cc = p.get("_exit_country", p.get("_entry_country", ""))
            delay = p.get("_delay", "")
            delay_str = f" | {delay}ms" if delay else ""
            
            unlock_str = ""
            if "_unlock" in p:
                unlocked_svcs = [k for k, v in p["_unlock"].items() if v]
                unlock_str = f" | 解锁: {', '.join(unlocked_svcs) if unlocked_svcs else '无'}"
                
            lines.append(f"- {name} | {ip} | {org} | {cc}{delay_str}{unlock_str}")
        lines.append("")

    if datacenter:
        lines.append("## 机房节点（过滤）")
        for p in datacenter:
            name = p.get("name", "unknown")
            ip = p.get("_exit_ip", p.get("_entry_ip", ""))
            org = p.get("_exit_org", p.get("_entry_org", ""))
            reason = p.get("_filter_reason", "")
            lines.append(f"- {name} | {ip} | {org} | 原因: {reason}")
        lines.append("")

    if test_results:
        alive = [r for r in test_results if r["alive"]]
        dead = [r for r in test_results if not r["alive"]]
        lines.append("## 连通性测试")
        lines.append(f"- 存活: {len(alive)}")
        lines.append(f"- 失败: {len(dead)}")
        if alive:
            lines.append("")
            lines.append("### 存活节点")
            for r in sorted(alive, key=lambda x: x["delay"]):
                lines.append(f"- {r['name']} | {r['delay']}ms")
        if dead:
            lines.append("")
            lines.append("### 失败节点")
            for r in dead:
                lines.append(f"- {r['name']} | {r.get('error', 'unknown')}")
                
    if test_results and any("unlock" in r for r in test_results):
        lines.append("")
        lines.append("## AI 解锁检测")
        for r in [r for r in test_results if r["alive"] and "unlock" in r]:
            unlock_dict = r["unlock"]
            if not unlock_dict:
                continue
            status_list = [f"{k}: {'✓' if v else '✗'}" for k, v in unlock_dict.items()]
            lines.append(f"- {r['name']} | " + " | ".join(status_list))

    output_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("报告已写入: %s", output_path)
    return output_path
