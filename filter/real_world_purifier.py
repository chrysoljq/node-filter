import asyncio
import httpx
import requests
import json
import time
from datetime import datetime

# --- 大哥 (ggken) 钦定的硬核筛选逻辑 v4.0 ---
# 1. 方案 2 集成: 不动系统代理喵！🐾
# 2. 每日巡检: 批量洗白机房 IP 喵！🐾🐾

class MihomoNodePurifier:
    def __init__(self, controller="127.0.0.1:9090", proxy="127.0.0.1:7890", secret=""):
        self.api_base = f"http://{controller}"
        self.proxy_url = f"http://{proxy}"
        self.headers = {"Authorization": f"Bearer {secret}"} if secret else {}
        self.target = "http://ip-api.com/json/?fields=status,hosting,query,isp"

    async def get_node_exit_ip(self, node_name: str):
        """核心: 切换节点并抓取真实指纹喵！🐾"""
        try:
            # 切换 Mihomo 节点
            async with httpx.AsyncClient(headers=self.headers) as client:
                await client.put(f"{self.api_base}/proxies/GLOBAL", json={"name": node_name}, timeout=5)
            
            await asyncio.sleep(0.3) # 赛博换气喵
            
            # 通过代理端口抓取 IP 信息
            async with httpx.AsyncClient(proxies=self.proxy_url) as p_client:
                r = await p_client.get(self.target, timeout=10)
                return r.json()
        except:
            return None

    async def run_daily_audit(self, node_list):
        print(f"☀️ 凌晨三点，松宝准时起床帮大哥洗白节点喵！共计 {len(node_list)} 个...")
        pure_nodes = []
        for node in node_list:
            info = await self.get_node_exit_ip(node)
            if info and info.get('status') == 'success':
                if not info.get('hosting'): # 剔除机房喵！🐾
                    print(f"✅ [优质] {node} -> {info['query']} ({info['isp']})")
                    pure_nodes.append(node)
                else:
                    print(f"❌ [机房] {node} -> 被松宝扔进猫砂盆了喵！")
        return pure_nodes

if __name__ == "__main__":
    print("✨ 实战级 Mihomo 筛选逻辑已加载！大哥请吩咐喵！🐾")
