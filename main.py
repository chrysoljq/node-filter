#!/usr/bin/env python3
"""mihomo-node-filter 主入口。

两种工作模式：
  快速模式（默认）：DNS 解析入口 IP → ip-api 检测 → 输出
  精确模式（--test）：mihomo 代理获取出口 IP → ip-api 检测 → 输出

精确模式流程：
  加载节点 → 名称过滤 → 启动 mihomo → 逐个切换测延迟+获取出口IP
  → 批量查询出口IP是否机房 → 输出
"""

import argparse
import logging
import sys
from pathlib import Path

import yaml

from filter.source import load_sources
from filter.detector import detect_by_entry_ip, detect_by_exit_ip
from filter.tester import test_proxies
from filter.output import (
    generate_mihomo_config,
    generate_proxy_list,
    generate_report,
)

logger = logging.getLogger("mihomo-node-filter")


def setup_logging(level: str = "INFO"):
    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def load_config(config_path: str) -> dict:
    path = Path(config_path)
    if not path.exists():
        logger.error("配置文件不存在: %s", path)
        sys.exit(1)
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def filter_by_name(
    proxies: list[dict],
    blacklist: list[str],
    whitelist: list[str],
) -> tuple[list[dict], list[dict]]:
    """按名称关键词过滤。白名单优先级高于黑名单。"""
    kept, removed = [], []
    for proxy in proxies:
        name = proxy.get("name", "").lower()
        if whitelist and any(kw.lower() in name for kw in whitelist):
            kept.append(proxy)
            continue
        if blacklist and any(kw.lower() in name for kw in blacklist):
            proxy["_filter_reason"] = "名称黑名单"
            removed.append(proxy)
            continue
        kept.append(proxy)
    if removed:
        logger.info("名称过滤: 移除 %d 个节点", len(removed))
    return kept, removed


def main():
    parser = argparse.ArgumentParser(
        description="mihomo-node-filter: 筛选非机房代理节点",
    )
    parser.add_argument("-c", "--config", default="config.yaml",
                        help="配置文件路径")
    parser.add_argument("-s", "--subscription", action="append", default=[],
                        help="订阅链接（可多次指定）")
    parser.add_argument("-f", "--file", action="append", default=[],
                        help="本地文件路径（可多次指定）")
    parser.add_argument("-o", "--output-dir", default=None,
                        help="输出目录")
    parser.add_argument("--test", action="store_true",
                        help="精确模式：启动 mihomo 获取出口 IP 检测")
    parser.add_argument("--no-detect", action="store_true",
                        help="跳过机房检测（仅名称过滤+连通性测试）")
    parser.add_argument("--mihomo-bin", default=None,
                        help="mihomo 二进制路径")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="详细日志")
    args = parser.parse_args()

    config = load_config(args.config)

    log_level = "DEBUG" if args.verbose else config.get("logging", {}).get("level", "INFO")
    setup_logging(log_level)
    logger.info("=== mihomo-node-filter ===")

    # ── 确定节点来源 ──
    sources = []
    if args.subscription:
        sources.extend({"type": "subscription", "url": u} for u in args.subscription)
    if args.file:
        sources.extend({"type": "file", "path": p} for p in args.file)
    if not sources:
        sources = config.get("sources", [])
    if not sources:
        logger.error("未指定任何节点来源")
        sys.exit(1)

    filter_config = config.get("filter", {})
    conn_config = filter_config.get("connectivity", {})

    # ── 步骤 1: 加载节点 ──
    logger.info("[1] 加载节点...")
    proxies = load_sources(sources)
    if not proxies:
        logger.error("未获取到任何节点")
        sys.exit(1)

    # ── 步骤 2: 名称过滤 ──
    logger.info("[2] 名称过滤...")
    proxies, name_removed = filter_by_name(
        proxies,
        filter_config.get("name_blacklist", []),
        filter_config.get("name_whitelist", []),
    )

    # ── 确定工作模式 ──
    use_mihomo = args.test or filter_config.get("enable_connectivity_test", False)

    residential = proxies
    datacenter = list(name_removed)
    unknown = []
    test_results = None

    if use_mihomo:
        # ═══ 精确模式：mihomo 出口 IP 检测 ═══
        mihomo_bin = args.mihomo_bin or conn_config.get("mihomo_bin", "mihomo")
        test_url = conn_config.get("test_url", "https://www.gstatic.com/generate_204")
        timeout = conn_config.get("timeout", 10)

        # 步骤 3: 连通性测试 + 获取出口 IP
        logger.info("[3] 启动 mihomo 测试连通性 + 获取出口 IP...")
        test_results = test_proxies(
            proxies,
            mihomo_bin=mihomo_bin,
            test_url=test_url,
            timeout=timeout,
        )

        # 过滤不可用节点
        alive_names = {r["name"] for r in test_results if r["alive"]}
        dead = [p for p in proxies if p.get("name") not in alive_names]
        for p in dead:
            p["_filter_reason"] = "连接失败"
        proxies = [p for p in proxies if p.get("name") in alive_names]
        datacenter.extend(dead)

        # 步骤 4: 出口 IP 机房检测
        if not args.no_detect:
            logger.info("[4] 出口 IP 机房检测...")
            residential, dc, unknown = detect_by_exit_ip(proxies, test_results)
            datacenter.extend(dc)
        else:
            logger.info("[4] 跳过机房检测")
            residential = proxies
    else:
        # ═══ 快速模式：入口 IP 检测 ═══
        if not args.no_detect:
            logger.info("[3] 入口 IP 快速检测...")
            residential, dc, unknown = detect_by_entry_ip(proxies)
            datacenter.extend(dc)
        else:
            logger.info("[3] 跳过机房检测")

        logger.info("[4] 跳过（未启用 mihomo 测试，使用 --test 启用）")

    # ── 输出 ──
    final_proxies = residential + unknown
    output_config = config.get("output", {})
    output_dir = Path(args.output_dir or output_config.get("dir", "./output"))

    logger.info("=== 结果 ===")
    logger.info("保留: %d | 过滤: %d | 未知: %d",
                len(residential), len(datacenter), len(unknown))

    generate_mihomo_config(
        final_proxies,
        output_dir / output_config.get("config_file", "filtered_config.yaml"),
        mixed_port=output_config.get("mixed_port", 7890),
        api_port=output_config.get("api_port", 9090),
    )
    generate_proxy_list(
        final_proxies,
        output_dir / output_config.get("proxies_file", "filtered_proxies.yaml"),
    )
    generate_report(
        residential, datacenter, unknown, test_results,
        output_dir / output_config.get("report_file", "filter_report.md"),
    )

    # ── 远程推送 ──
    push_config = config.get("remote_push", {})
    if push_config.get("enable", False):
        base_url = push_config.get("url", "").rstrip("/")
        token = push_config.get("token")
        if base_url:
            from filter.output import push_to_worker
            # 推送到专门的 filter 接口，避免覆盖 custom_yaml
            yaml_url = f"{base_url}/api/filter/config"
            report_url = f"{base_url}/api/filter/report"

            # 1. 推送配置文件
            full_config_path = output_dir / output_config.get("config_file", "filtered_config.yaml")
            if full_config_path.exists():
                content = full_config_path.read_text(encoding="utf-8")
                push_to_worker(content, yaml_url, token, data_type="yaml")

            # 2. 推送报告
            report_path = output_dir / output_config.get("report_file", "filter_report.md")
            if report_path.exists():
                report_content = report_path.read_text(encoding="utf-8")
                push_to_worker(report_content, report_url, token, data_type="report")
        else:
            logger.warning("启用了远程推送但未指定 url")

    logger.info("=== 完成 ===")


if __name__ == "__main__":
    main()
