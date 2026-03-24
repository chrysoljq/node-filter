# ⚠️ SONG BAO WAS HERE! Async Engine V3 is READY! 🐾
# mihomo-node-filter

自动筛选非机房代理节点的工具。从订阅链接或本地文件获取节点，通过 IP 数据库查询 + ASN 黑名单双重机制识别并过滤机房节点，输出干净的 mihomo/Clash 配置文件。

支持 GitHub Actions 每日自动更新。

## 特性

- **多来源支持**：订阅链接（Clash/mihomo YAML、Base64 编码）+ 本地文件
- **多协议解析**：SS、VMess、VLESS、Trojan、Hysteria、Hysteria2、TUIC
- **双模式检测**：
  - ⚡ **快速模式**（默认）：DNS 解析入口 IP → ip-api 检测，无需 mihomo
  - 🎯 **精确模式**（`--test`）：启动单个 mihomo 实例，通过 API 逐个切换节点获取**出口 IP** → ip-api 检测（推荐）
- **三重机房判定**：
  - ip-api.com `hosting` 标志
  - 已知机房 ASN 黑名单（AWS、GCP、Azure、Vultr、DigitalOcean 等 60+ 条）
  - ISP/Org 名称关键词匹配
- **单实例架构**：精确模式只启动一个 mihomo 进程，通过 RESTful API 切换节点，高效且稳定
- **AI 解锁检测**：联动精确模式并发检测节点对 ChatGPT, Claude, Gemini, Copilot, YouTube 的解锁情况
- **自动去重**：按 (type, server, port) 去重
- **名称过滤**：黑名单/白名单关键词过滤
- **GitHub Actions**：定时运行，自动提交更新

## 快速开始

### 本地运行

```bash
# 安装依赖
pip install -r requirements.txt

# ⚡ 快速模式（入口 IP 检测，无需 mihomo）
python main.py -s "https://your-subscription-url.com/sub"

# 🎯 精确模式（出口 IP 检测，需要 mihomo）
python main.py -s "https://..." --test
python main.py -s "https://..." --test --mihomo-bin /path/to/mihomo

# 🤖 AI 解锁检测（自动开启精确模式）
python main.py -s "https://..." --unlock
# 仅保留解锁任意 AI 服务的节点
python main.py -s "https://..." --unlock-only

# 本地文件
python main.py -f ./my_proxies.yaml

# 跳过机房检测（仅名称过滤+连通性/解锁测试）
python main.py -s "https://..." --test --no-detect

# 详细日志
python main.py -s "https://..." -v
```

### GitHub Actions + Worker

完整流程：

```
Worker /api/fetch ──→ Actions 筛选 ──→ Worker /api/config ──→ /sub?token=xxx 供客户端订阅
```

1. 部署 Worker（见 [`worker/README.md`](worker/README.md)），通过 API 添加订阅源
2. 在仓库 **Settings → Secrets** 中设置：

   | Secret | 说明 |
   |--------|------|
   | `WORKER_URL` | Worker 地址，如 `https://sub-worker.xxx.workers.dev` |
   | `WORKER_AUTH_TOKEN` | Worker 管理令牌 |

3. 启用 Actions，每天 UTC 02:00（北京时间 10:00）自动运行
4. 筛选后的配置会上传到 Worker，客户端订阅地址：
   ```
   https://your-worker.workers.dev/sub?token=your-sub-token
   ```

## 输出文件

| 文件 | 说明 |
|------|------|
| `output/filtered_config.yaml` | 完整的 mihomo 配置（可直接使用） |
| `output/filtered_proxies.yaml` | 仅节点列表（方便嵌入已有配置） |
| `output/filtered_gemini_config.yaml` | 仅解锁 Gemini 的配置 (需 `--unlock`) |
| `output/filtered_all_unlock_config.yaml`| 解锁全部配置中 AI 服务的配置 (需 `--unlock`) |
| `output/filter_report.md` | 筛选报告（附带 AI 解锁详细状态） |

## 配置文件

`config.yaml` 示例：

```yaml
sources:
  - type: subscription
    url: "https://your-subscription-url.com/sub"
  - type: file
    path: "./local_proxies.yaml"

filter:
  enable_datacenter_detection: true
  enable_connectivity_test: false
  name_blacklist:
    - "过期"
    - "剩余"
    - "官网"

output:
  dir: "./output"
  mixed_port: 7890
```

详细配置说明见 [config.yaml](config.yaml) 中的注释。

## 项目结构

```
├── main.py                  # 主入口
├── config.yaml              # 配置文件
├── filter/
│   ├── source.py            # 节点获取与解析
│   ├── detector.py          # 机房检测（入口IP/出口IP双模式）
│   ├── tester.py            # mihomo 单实例连通性测试
│   ├── unlock.py            # AI 服务解锁并发检测
│   └── output.py            # 输出生成
├── data/
│   └── datacenter_asn.yaml  # 机房 ASN 黑名单
├── worker/                  # CF Workers 订阅管理 API
│   ├── src/index.ts         # Worker 主体（路由/鉴权/CRUD/聚合）
│   ├── wrangler.toml
│   └── README.md
├── .github/workflows/
│   └── filter.yaml          # GitHub Actions
└── output/                  # 输出目录
```

## 检测原理

```
⚡ 快速模式（默认）               🎯 精确模式（--test，推荐）
─────────────────────           ──────────────────────────
节点 server 域名                 启动单个 mihomo 实例
    ↓ DNS 解析                       ↓ 加载所有节点
入口 IP                          API 切换节点 → 测延迟
    ↓ ip-api batch 查询               ↓ 通过代理请求
    ├── hosting 标志               出口 IP（真实落地 IP）
    ├── ASN 黑名单                    ↓ ip-api batch 查询
    └── 关键词匹配                    ├── hosting 标志
        ↓                            ├── ASN 黑名单
    机房 / 住宅                       └── 关键词匹配
                                         ↓
                                     机房 / 住宅
```

## License

MIT
