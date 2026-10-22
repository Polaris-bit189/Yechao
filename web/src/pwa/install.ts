// 安装入口。只在 Help 里露出，不占首屏。
//
// Android/桌面 Chrome 走 `beforeinstallprompt`；iOS 与鸿蒙没有这个事件，
// 给的是「浏览器菜单 → 添加到主屏幕」的文字指引。鸿蒙不承诺 standalone。

type InstallPromptEvent = Event & {
  prompt: () => Promise<void>
  userChoice?: Promise<{ outcome: 'accepted' | 'dismissed' }>
}

let deferred: InstallPromptEvent | null = null
const listeners = new Set<() => void>()

function emit(): void {
  for (const fn of listeners) fn()
}

export function initInstall(): void {
  if (typeof window === 'undefined') return
  window.addEventListener('beforeinstallprompt', (event) => {
    // 不阻止默认的话，Chrome 会自己弹一个我们控制不了时机的横幅。
    event.preventDefault()
    deferred = event as InstallPromptEvent
    emit()
  })
  window.addEventListener('appinstalled', () => {
    deferred = null
    emit()
  })
}

export function subscribeInstall(fn: () => void): () => void {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

export function getInstallPrompt(): InstallPromptEvent | null {
  return deferred
}

export function isStandalone(): boolean {
  if (typeof window === 'undefined') return false
  const nav = navigator as Navigator & { standalone?: boolean }
  return window.matchMedia('(display-mode: standalone)').matches || nav.standalone === true
}

export type InstallHint = 'installed' | 'prompt' | 'ios' | 'other'

export function installHint(): InstallHint {
  if (isStandalone()) return 'installed'
  if (deferred) return 'prompt'
  if (/iphone|ipad|ipod/i.test(navigator.userAgent)) return 'ios'
  return 'other'
}

export async function promptInstall(): Promise<boolean> {
  const event = deferred
  if (!event) return false
  deferred = null
  emit()
  await event.prompt()
  await event.userChoice?.catch(() => undefined)
  return true
}

/** 清掉 API 离线缓存；壳缓存保留（删了应用就起不来）。 */
export async function clearOfflineCaches(): Promise<void> {
  if (!('caches' in window)) return
  const keys = await caches.keys()
  await Promise.all(keys.filter((k) => k.startsWith('yechao-api-')).map((k) => caches.delete(k)))
  navigator.serviceWorker?.controller?.postMessage({ type: 'CLEAR_OFFLINE_CACHE' })
}
