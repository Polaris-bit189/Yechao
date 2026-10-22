/* 夜抄 Service Worker。
 *
 * 由 web/pwa/vite-plugin.mjs 在 `vite build` 末尾写入 dist/sw.js：
 *   构建版本与预缓存清单由插件注入（见下面的 VERSION / PRECACHE）。
 *
 * 策略（T5）：
 *   · 应用壳：安装时预缓存；静态资源 cache-first（带哈希，内容不可变）。
 *   · 导航请求：network-first（3s 超时），失败回落缓存的 index.html；
 *     两者都没有时返回**可见的** 503 页，不给白屏。
 *   · 非 GET 一律不拦截（T6 只加 /api 的 GET 白名单）。
 *   · 新版本安装后**不自动 skipWaiting**：页面提示「新版本可用」，
 *     用户点了才接管并刷新一次。
 */
const VERSION = '9545655eaac7'
const PRECACHE = [
  "./",
  "./assets/index-0HTeSarj.js",
  "./assets/index-LKYVkFon.css",
  "./favicon.svg",
  "./icon-maskable.svg",
  "./icon-source.svg",
  "./icons/apple-touch-icon.png",
  "./icons/icon-192.png",
  "./icons/icon-512.png",
  "./icons/icon-maskable-192.png",
  "./icons/icon-maskable-512.png",
  "./index.html",
  "./manifest.webmanifest"
]
const SHELL_CACHE = `yechao-shell-${VERSION}`
// API 缓存与壳缓存**分开**：壳随构建版本换，API 数据跨版本保留
// （否则每次发版都把用户离线能读的日报清掉）。
const API_CACHE = 'yechao-api-v1'
const API_LIMIT = 40

const OFFLINE_HTML = `<!doctype html>
<html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>夜抄 · 离线</title>
<body style="margin:0;min-height:100svh;display:grid;place-items:center;background:#0a0c12;color:#e7ecf6;font:16px/1.7 system-ui,sans-serif">
  <main style="max-width:28rem;padding:2rem;text-align:center">
    <h1 style="font-size:1.25rem;margin:0 0 .75rem">离线，且没有可读缓存</h1>
    <p style="margin:0;color:#aab4c8">联网后刷新一次；已经读过的日报会保存在本机，下次离线可以直接打开。</p>
  </main>
</body></html>`

function offlinePage() {
  return new Response(OFFLINE_HTML, {
    status: 503,
    headers: { 'Content-Type': 'text/html; charset=utf-8' },
  })
}

async function networkFirst(request, timeoutMs = 3000, fallbackUrl = null) {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  try {
    const response = await fetch(request, { signal: controller.signal })
    return response
  } catch {
    if (fallbackUrl) {
      const cache = await caches.open(SHELL_CACHE)
      const hit = await cache.match(fallbackUrl)
      if (hit) return hit
    }
    return offlinePage()
  } finally {
    clearTimeout(timer)
  }
}

// 只有**只读、可重复、且不含私人凭据**的 GET 才缓存。
// `/api/status`、`/api/consent`、`/api/privacy`、`/api/llm`、`/api/config/*`、
// `/api/jobs*` 一律不缓存。
function isCacheableApi(url) {
  const path = url.pathname
  if (path === '/api/digests' || path.startsWith('/api/digests/')) return true
  if (path === '/api/items') {
    const q = url.searchParams
    const offset = q.get('offset')
    const limit = Number(q.get('limit') ?? '50')
    // 只缓存第一页：后续页是「翻更多」，离线读不到不算缺陷。
    return (
      (offset === null || offset === '' || offset === '0') &&
      Number.isFinite(limit) &&
      limit <= 50 &&
      !q.get('q')
    )
  }
  if (path.startsWith('/api/items/')) return true
  return ['/api/calendar', '/api/categories', '/api/persons', '/api/groups', '/api/urgent'].includes(path)
}

async function rememberApi(request, response) {
  // 只缓存 200；4xx/5xx 原样返回，不拿旧缓存顶替错误。
  if (!response || response.status !== 200) return response
  const cache = await caches.open(API_CACHE)
  const headers = new Headers(response.headers)
  headers.set('X-Yechao-Cached-At', String(Math.floor(Date.now() / 1000)))
  const copy = new Response(await response.clone().blob(), {
    status: response.status,
    statusText: response.statusText,
    headers,
  })
  await cache.put(request, copy)
  const keys = await cache.keys()
  for (const key of keys.slice(0, Math.max(0, keys.length - API_LIMIT))) {
    await cache.delete(key)
  }
  return response
}

async function apiNetworkFirst(request) {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), 3000)
  try {
    const response = await fetch(request, { signal: controller.signal })
    return await rememberApi(request, response)
  } catch {
    const cache = await caches.open(API_CACHE)
    const hit = await cache.match(request)
    if (hit) {
      const headers = new Headers(hit.headers)
      headers.set('X-Yechao-Cache', 'hit')
      return new Response(await hit.blob(), { status: 200, headers })
    }
    return new Response(JSON.stringify({ detail: '离线，且没有可读缓存' }), {
      status: 503,
      headers: { 'Content-Type': 'application/json', 'X-Yechao-Offline': '1' },
    })
  } finally {
    clearTimeout(timer)
  }
}

self.addEventListener('install', (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(SHELL_CACHE)
      await cache.addAll(PRECACHE)
      // 只有首次安装才立刻接管；更新要等页面提示用户。
      if (!self.registration.active) await self.skipWaiting()
    })(),
  )
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys()
      await Promise.all(
        keys
          .filter((key) => key.startsWith('yechao-shell-') && key !== SHELL_CACHE)
          .map((key) => caches.delete(key)),
      )
      await self.clients.claim()
    })(),
  )
})

self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'SKIP_WAITING') {
    void self.skipWaiting()
  }
  if (event.data && event.data.type === 'CLEAR_OFFLINE_CACHE') {
    event.waitUntil(caches.delete(API_CACHE))
  }
})

self.addEventListener('fetch', (event) => {
  const request = event.request
  // 写操作、跨域、API 一律不碰。API 的离线策略在 T6 单独加白名单。
  if (request.method !== 'GET') return
  const url = new URL(request.url)
  if (url.origin !== self.location.origin) return
  if (url.pathname.startsWith('/api/')) {
    if (isCacheableApi(url)) event.respondWith(apiNetworkFirst(request))
    return
  }

  // 导航：网络优先，失败回落缓存的应用壳。
  if (request.mode === 'navigate') {
    const shell = new URL('./index.html', self.registration.scope).href
    event.respondWith(networkFirst(request, 3000, shell))
    return
  }

  // 静态资源：带哈希的构建产物不可变，cache-first。
  event.respondWith(
    (async () => {
      const hit = await caches.match(request)
      if (hit) return hit
      try {
        return await fetch(request)
      } catch {
        return offlinePage()
      }
    })(),
  )
})
