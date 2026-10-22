// 离线/缓存状态。
//
// ※ 只记「这次会话里 API 真的从缓存返回过」，不靠一个全局布尔猜。
//   页面在线时若某个请求命中了 SW 的 API 缓存（例如服务端慢、回落到缓存），
//   横幅会写「当前显示缓存数据（缓存于 HH:MM）」，而不是假装是最新的。
//
// 时间取**本次会话最早**的一次缓存命中：一页里几个请求可能来自不同时刻，
// 报最早的那个才不会把旧数据说新。

export type OfflineSnapshot = {
  cachedAt: number | null // 毫秒时间戳
  offline: boolean
}

let cachedAt: number | null = null
let offline = typeof navigator !== 'undefined' ? !navigator.onLine : false
let snapshot: OfflineSnapshot = { cachedAt, offline }

const listeners = new Set<() => void>()

function publish(): void {
  snapshot = { cachedAt, offline }
  for (const fn of listeners) fn()
}

export function subscribe(fn: () => void): () => void {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

export function getSnapshot(): OfflineSnapshot {
  return snapshot
}

/** SW 响应头里的 X-Yechao-Cached-At 是**秒**。 */
export function noteCacheHit(epochSeconds: number): void {
  if (!Number.isFinite(epochSeconds) || epochSeconds <= 0) return
  const ms = epochSeconds < 1e12 ? epochSeconds * 1000 : epochSeconds
  const next = cachedAt === null ? ms : Math.min(cachedAt, ms)
  if (next === cachedAt) return
  cachedAt = next
  publish()
}

export function noteOffline(): void {
  if (offline) return
  offline = true
  publish()
}

export function noteOnline(): void {
  if (!offline) return
  offline = false
  publish()
}

export function clearCacheMarker(): void {
  if (cachedAt === null) return
  cachedAt = null
  publish()
}

export function formatCacheTime(ms: number): string {
  const d = new Date(ms)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

if (typeof window !== 'undefined') {
  window.addEventListener('online', noteOnline)
  window.addEventListener('offline', noteOffline)
}
