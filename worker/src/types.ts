export interface Env {
  KV: KVNamespace;
  AUTH_TOKEN: string;
  SUB_TOKEN: string;
  GLOBAL_UA?: string;
}

export interface Subscription {
  id: string;
  name: string;
  url: string;
  createdAt: string;
}

export const KV_SUBS_KEY = "subs:list";
export const kvCacheKey = (id: string) => `cache:${id}`;
