import { Env, Subscription, KV_SUBS_KEY, kvCacheKey } from "./types";

const KV_CONFIG_KEY = "output:config";

// ─── helpers ───

async function getSubs(kv: KVNamespace): Promise<Subscription[]> {
  const raw = await kv.get(KV_SUBS_KEY);
  return raw ? JSON.parse(raw) : [];
}

async function saveSubs(kv: KVNamespace, subs: Subscription[]): Promise<void> {
  await kv.put(KV_SUBS_KEY, JSON.stringify(subs));
}

function genId(): string {
  const c = "abcdefghijklmnopqrstuvwxyz0123456789";
  const a = crypto.getRandomValues(new Uint8Array(6));
  return Array.from(a, (b) => c[b % c.length]).join("");
}

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data, null, 2), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

// ─── 订阅源 CRUD ───

async function listSubs(env: Env): Promise<Response> {
  return json({ ok: true, data: await getSubs(env.KV) });
}

async function addSub(req: Request, env: Env): Promise<Response> {
  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return json({ ok: false, error: "无效 JSON" }, 400);
  }

  const items: { name?: string; url?: string }[] = Array.isArray(body)
    ? body
    : [body];
  const subs = await getSubs(env.KV);
  const added: Subscription[] = [];

  for (const item of items) {
    if (!item.name || !item.url) {
      return json({ ok: false, error: "缺少 name 或 url" }, 400);
    }
    if (subs.some((s) => s.name === item.name)) {
      return json({ ok: false, error: `名称已存在: ${item.name}` }, 409);
    }
    const sub: Subscription = {
      id: genId(),
      name: item.name,
      url: item.url,
      createdAt: new Date().toISOString(),
    };
    subs.push(sub);
    added.push(sub);
  }

  await saveSubs(env.KV, subs);
  return json({ ok: true, data: added }, 201);
}

async function deleteSub(id: string, env: Env): Promise<Response> {
  const subs = await getSubs(env.KV);
  const idx = subs.findIndex((s) => s.id === id || s.name === id);
  if (idx === -1) return json({ ok: false, error: "不存在" }, 404);

  const [removed] = subs.splice(idx, 1);
  await saveSubs(env.KV, subs);
  await env.KV.delete(kvCacheKey(removed.id));
  return json({ ok: true, data: removed });
}

async function refreshCache(env: Env): Promise<Response> {
  const subs = await getSubs(env.KV);
  for (const s of subs) await env.KV.delete(kvCacheKey(s.id));
  return json({ ok: true, data: { cleared: subs.length } });
}

// ─── 原始订阅内容拉取（供 Actions 使用）───

const CACHE_TTL = 300;

async function fetchOne(sub: Subscription, kv: KVNamespace): Promise<string> {
  const ck = kvCacheKey(sub.id);
  const cached = await kv.get(ck);
  if (cached) return cached;

  const resp = await fetch(sub.url, {
    headers: { "User-Agent": "clash.meta/mihomo" },
    signal: AbortSignal.timeout(15_000),
  });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  const text = await resp.text();
  await kv.put(ck, text, { expirationTtl: CACHE_TTL });
  return text;
}

async function handleFetchSubs(env: Env): Promise<Response> {
  const subs = await getSubs(env.KV);
  if (subs.length === 0) {
    return json({ ok: true, contents: [] });
  }

  const results = await Promise.allSettled(
    subs.map((s) => fetchOne(s, env.KV))
  );

  const contents: string[] = [];
  const errors: string[] = [];

  for (let i = 0; i < subs.length; i++) {
    const r = results[i];
    if (r.status === "fulfilled") {
      contents.push(r.value);
    } else {
      contents.push("");
      errors.push(`${subs[i].name}: ${r.reason}`);
    }
  }

  return json({ ok: true, contents, errors: errors.length ? errors : undefined });
}

// ─── 筛选结果上传/下载 ───

/** PUT /api/config — Actions 上传筛选后的 YAML */
async function uploadConfig(req: Request, env: Env): Promise<Response> {
  const yaml = await req.text();
  if (!yaml.trim()) {
    return json({ ok: false, error: "空内容" }, 400);
  }
  await env.KV.put(KV_CONFIG_KEY, yaml);
  const size = new Blob([yaml]).size;
  return json({ ok: true, data: { size, updatedAt: new Date().toISOString() } });
}

/** GET /sub?token=xxx — 客户端订阅，直接返回 YAML */
async function handleSub(env: Env): Promise<Response> {
  const yaml = await env.KV.get(KV_CONFIG_KEY);
  if (!yaml) {
    return new Response("# 暂无配置，请等待 Actions 筛选完成\nproxies: []\n", {
      status: 200,
      headers: yamlHeaders(),
    });
  }
  return new Response(yaml, { headers: yamlHeaders() });
}

function yamlHeaders(): HeadersInit {
  return {
    "content-type": "text/yaml; charset=utf-8",
    "content-disposition": 'attachment; filename="config.yaml"',
    "profile-update-interval": "6",
    "cache-control": "no-cache",
  };
}

// ─── 鉴权 ───

function checkToken(req: Request, expected: string): boolean {
  const url = new URL(req.url);
  const q = url.searchParams.get("token");
  const h = req.headers.get("Authorization")?.replace("Bearer ", "");
  return (h || q) === expected;
}

// ─── 路由 ───

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    const url = new URL(req.url);
    const path = url.pathname;
    const method = req.method;

    // CORS preflight
    if (method === "OPTIONS") {
      return new Response(null, {
        headers: {
          "access-control-allow-origin": "*",
          "access-control-allow-methods": "GET,POST,PUT,DELETE,OPTIONS",
          "access-control-allow-headers": "Authorization,Content-Type",
        },
      });
    }

    // /sub — SUB_TOKEN 鉴权，返回筛选后的 YAML
    if (path === "/sub") {
      if (!checkToken(req, env.SUB_TOKEN)) {
        return json({ ok: false, error: "无效 token" }, 403);
      }
      return handleSub(env);
    }

    // /api/* — AUTH_TOKEN 鉴权
    if (path.startsWith("/api/")) {
      if (!checkToken(req, env.AUTH_TOKEN)) {
        return json({ ok: false, error: "未授权" }, 401);
      }

      // 订阅源管理
      if (path === "/api/subs" && method === "GET") return listSubs(env);
      if (path === "/api/subs" && method === "POST") return addSub(req, env);
      if (path === "/api/subs/refresh" && method === "POST") return refreshCache(env);
      const m = path.match(/^\/api\/subs\/([^/]+)$/);
      if (m && method === "DELETE") return deleteSub(m[1], env);

      // 拉取原始订阅（供 Actions 获取节点内容）
      if (path === "/api/fetch" && method === "GET") return handleFetchSubs(env);

      // 上传筛选结果
      if (path === "/api/config" && method === "PUT") return uploadConfig(req, env);
      // 查看当前配置
      if (path === "/api/config" && method === "GET") {
        const yaml = await env.KV.get(KV_CONFIG_KEY);
        return json({ ok: true, data: { hasConfig: !!yaml, size: yaml?.length ?? 0 } });
      }

      return json({ ok: false, error: "Not Found" }, 404);
    }

    if (path === "/" || path === "") {
      return new Response("sub-worker is running\n", {
        headers: { "content-type": "text/plain" },
      });
    }

    return json({ ok: false, error: "Not Found" }, 404);
  },
};
