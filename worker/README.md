# Worker — 订阅管理 & 配置分发 API

部署在 Cloudflare Workers 上，负责：
1. 管理订阅源链接（CRUD）
2. 供 GitHub Actions 拉取原始订阅内容
3. 接收 Actions 上传的筛选后配置
4. 作为订阅链接供客户端（mihomo/Clash）直接使用

## 完整流程

```
┌─────────────┐     GET /api/fetch      ┌──────────────┐
│   Worker    │ ◄─────────────────────── │ GitHub Actions│
│  (KV 存储)  │                          │  (Python 筛选)  │
│             │ ───────────────────────► │              │
│             │   PUT /api/config        └──────────────┘
│             │     (上传 YAML)
│             │   POST /api/filter/config_gemini
│             │   POST /api/filter/config_all_unlock
│             │
│             │     GET /sub?token=xxx
│             │     GET /sub/gemini?token=xxx
│             │     GET /sub/all_unlock?token=xxx
│             │ ◄─────────────────────── mihomo 客户端
│             │       (返回 YAML)
└─────────────┘
```

## API

| 接口 | 方法 | 鉴权 | 说明 |
|------|------|------|------|
| `/api/subs` | GET | AUTH_TOKEN | 列出所有订阅源 |
| `/api/subs` | POST | AUTH_TOKEN | 添加订阅源（支持批量） |
| `/api/subs/:id` | DELETE | AUTH_TOKEN | 删除订阅源（id 或 name） |
| `/api/subs/refresh` | POST | AUTH_TOKEN | 清除订阅缓存 |
| `/api/fetch` | GET | AUTH_TOKEN | 拉取所有订阅原始内容（供 Actions） |
| `/api/config` | PUT | AUTH_TOKEN | 上传筛选后常规 YAML（Actions 调用） |
| `/api/filter/config_gemini` | POST | AUTH_TOKEN | 上传仅解锁 Gemini 的配置 |
| `/api/filter/config_all_unlock` | POST | AUTH_TOKEN | 上传全部解锁的配置 |
| `/api/config` | GET | AUTH_TOKEN | 查看当前配置状态 |
| `/sub?token=xxx` | GET | SUB_TOKEN | **常规节点订阅地址** |
| `/sub/gemini?token=xxx` | GET | SUB_TOKEN | **仅 Gemini 节点订阅地址** |
| `/sub/all_unlock?token=xxx` | GET | SUB_TOKEN | **全部解锁节点订阅地址** |

### 示例

```bash
TOKEN="your-auth-token"
W="https://sub-worker.xxx.workers.dev"

# 添加订阅
curl -X POST "$W/api/subs?token=$TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"jichang1","url":"https://xxx/api/v1/client/subscribe?token=abc&flag=meta"}'

# 批量添加
curl -X POST "$W/api/subs?token=$TOKEN" \
  -H 'Content-Type: application/json' \
  -d '[{"name":"s1","url":"https://..."},{"name":"s2","url":"https://..."}]'

# 查看
curl "$W/api/subs?token=$TOKEN"

# 删除
curl -X DELETE "$W/api/subs/jichang1?token=$TOKEN"

# 客户端订阅地址（填入 mihomo/Clash）
# 默认筛选: https://sub-worker.xxx.workers.dev/sub?token=your-sub-token
# 仅 Gemini: https://sub-worker.xxx.workers.dev/sub/gemini?token=your-sub-token
# 全解锁:   https://sub-worker.xxx.workers.dev/sub/all_unlock?token=your-sub-token
```

## 部署

```bash
cd worker
npm install

# 创建 KV namespace
npx wrangler kv namespace create KV
# 将输出的 id 填入 wrangler.toml

# 设置 secrets
npx wrangler secret put AUTH_TOKEN
npx wrangler secret put SUB_TOKEN

# 部署
npm run deploy
```

## GitHub Actions 对接

在仓库 Settings → Secrets 中设置：

| Secret | 说明 |
|--------|------|
| `WORKER_URL` | Worker 地址 |
| `WORKER_AUTH_TOKEN` | AUTH_TOKEN（管理接口） |
