"""AI 解锁检测模块。

通过指定的 socks5 代理测试各个 AI 服务的可用性。
"""

import logging
import requests

logger = logging.getLogger(__name__)

def _check_chatgpt(r):
    text = r.text.lower()
    return "request is not allowed" in text and "disallowed isp" not in text

def _check_claude(r):
    blocked_codes = {"AF", "BY", "CN", "CU", "HK", "IR", "KP", "MO", "RU", "SY"}
    for line in r.text.splitlines():
        if line.startswith("loc="):
            code = line.split("=")[1].strip().upper()
            return code not in blocked_codes
    return False

# 定义支持检测的服务及判断逻辑
# check 函数返回 bool
SERVICES = {
    "ChatGPT": {
        "url": "https://ios.chat.openai.com/",
        "check": _check_chatgpt
    },
    "Claude": {
        "url": "https://claude.ai/cdn-cgi/trace",
        "check": _check_claude
    },
    "Gemini": {
        "url": "https://gemini.google.com/",
        "check": lambda r: "45631641,null,true" in r.text or "45631641,null,1" in r.text
    },
    "Copilot": {
        "url": "https://copilot.microsoft.com/",
        "check": lambda r: r.status_code == 200
    },
    "YouTube": {
        "url": "https://www.youtube.com/",
        "check": lambda r: r.status_code == 200
    },
}


def check_single_unlock(socks_port: int, service_name: str, timeout: int = 8) -> bool:
    """通过指定的 socks5 端口测试单个服务的解锁情况。"""
    svc = SERVICES.get(service_name)
    if not svc:
        logger.warning("未知的检测服务: %s", service_name)
        return False

    proxies = {
        "http": f"socks5h://127.0.0.1:{socks_port}",
        "https": f"socks5h://127.0.0.1:{socks_port}",
    }
    # 模拟常见浏览器 UA
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        resp = requests.get(
            svc["url"],
            proxies=proxies,
            headers=headers,
            timeout=timeout,
            allow_redirects=True
        )
        return svc["check"](resp)
    except requests.RequestException:
        # 网络异常，判定为未解锁或节点不通
        return False
    except Exception as e:
        logger.debug("检测 %s 异常: %s", service_name, e)
        return False


def check_unlock(
    socks_port: int,
    services: list[str] = None,
    timeout: int = 8
) -> dict[str, bool]:
    """检测指定服务列表的解锁情况。
    如果 services 未指定或为空，则检测所有配置了的服务。
    
    返回: {service_name: is_unlocked}
    """
    if not services:
        services = list(SERVICES.keys())

    results = {}
    for svc in services:
        if svc in SERVICES:
            results[svc] = check_single_unlock(socks_port, svc, timeout)
            
    return results
