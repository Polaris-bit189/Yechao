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
const VERSION = '__VERSION__'
const PRECACHE = __PRECACHE__
const SHELL_CACHE = `yechao-shell-${VERSION}`

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
})

self.addEventListener('fetch', (event) => {
  const request = event.request
  // 写操作、跨域、API 一律不碰。API 的离线策略在 T6 单独加白名单。
  if (request.method !== 'GET') return
  const url = new URL(request.url)
  if (url.origin !== self.location.origin) return
  if (url.pathname.startsWith('/api/')) return

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
