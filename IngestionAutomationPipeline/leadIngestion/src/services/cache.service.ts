import NodeCache from "node-cache";
import { env } from "../config/env";

/**
 * Thin wrapper around node-cache so the rest of the app depends on an
 * interface, not a specific cache implementation. Swap this out for
 * Redis by re-implementing the same three methods.
 */
class CacheService {
  private store: NodeCache;

  constructor(ttlSeconds: number) {
    this.store = new NodeCache({ stdTTL: ttlSeconds, checkperiod: Math.max(60, ttlSeconds / 10) });
  }

  has(key: string): boolean {
    return this.store.has(key);
  }

  get<T>(key: string): T | undefined {
    return this.store.get<T>(key);
  }

  set<T>(key: string, value: T): void {
    this.store.set(key, value);
  }

  del(key: string): void {
    this.store.del(key);
  }
}

export const cacheService = new CacheService(env.cacheTtlSeconds);
