type CacheEntry = {
  value?: unknown;
  updatedAt: number;
  touched: number;
  promise?: Promise<unknown>;
};

const entries = new Map<string, CacheEntry>();
const epochs = new Map<string, number>();
const maxEntries = 64;
let touchSequence = 0;

function trimCache() {
  if (entries.size <= maxEntries) return;
  const removable = [...entries].filter(([, entry]) => !entry.promise).sort((left, right) => left[1].touched - right[1].touched);
  for (const [key] of removable.slice(0, entries.size - maxEntries)) entries.delete(key);
}

export function cacheKey(...parts: unknown[]) {
  return JSON.stringify(parts);
}

export function cacheKeyPrefix(...parts: unknown[]) {
  return JSON.stringify(parts).slice(0, -1);
}

export function peekCachedQuery<T>(key: string): T | undefined {
  const entry = entries.get(key);
  if (entry) entry.touched = ++touchSequence;
  return entry?.value as T | undefined;
}

export function setCachedQuery<T>(key: string, value: T) {
  entries.set(key, { value, updatedAt: Date.now(), touched: ++touchSequence });
  trimCache();
  return value;
}

export function loadCachedQuery<T>(
  key: string,
  loader: () => Promise<T>,
  options: { ttlMs: number; force?: boolean } = { ttlMs: 30_000 },
): Promise<T> {
  const current = entries.get(key);
  if (current?.promise) return current.promise as Promise<T>;
  if (!options.force && current?.value !== undefined && Date.now() - current.updatedAt < options.ttlMs) {
    return Promise.resolve(current.value as T);
  }
  if (current) current.touched = ++touchSequence;
  const entry = current || { updatedAt: 0, touched: ++touchSequence };
  const epoch = epochs.get(key) || 0;
  const promise = loader().then(value => {
    if ((epochs.get(key) || 0) === epoch) {
      entries.set(key, { value, updatedAt: Date.now(), touched: ++touchSequence });
      trimCache();
    }
    return value;
  }).catch(reason => {
    if ((epochs.get(key) || 0) === epoch) {
      entry.promise = undefined;
      if (entry.value === undefined) entries.delete(key); else entries.set(key, entry);
    }
    throw reason;
  });
  entry.promise = promise;
  entries.set(key, entry);
  return promise;
}

export function invalidateCachedQueries(prefix: string) {
  for (const key of entries.keys()) if (key.startsWith(prefix)) {
    entries.delete(key);
    epochs.set(key, (epochs.get(key) || 0) + 1);
  }
}

export function clearQueryCache() {
  entries.clear();
  epochs.clear();
  touchSequence = 0;
}
