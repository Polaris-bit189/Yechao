// 注册 Service Worker。
//
// ※ 只在生产构建、且处于安全上下文时注册：
//   · dev server 下没有 dist/sw.js；
//   · 局域网 http://192.168.x.x 不是安全上下文，浏览器直接拒绝注册。
// 失败不 console.error —— 离线是增强，不是页面可用的前提；verify 脚本把
// console.error 当失败，而这里没有理由制造一条。

type UpdateEvent = CustomEvent<ServiceWorker>

export const PWA_UPDATE_EVENT = 'yechao:pwa-update'

export function registerPwa(): void {
  if (!import.meta.env.PROD) return
  if (typeof window === 'undefined' || !window.isSecureContext) return
  if (!('serviceWorker' in navigator)) return

  window.addEventListener('load', () => {
    navigator.serviceWorker
      .register('./sw.js', { scope: './', updateViaCache: 'none' })
      .then((reg) => {
        const announce = (worker: ServiceWorker | null) => {
          if (!worker) return
          window.dispatchEvent(
            new CustomEvent<ServiceWorker>(PWA_UPDATE_EVENT, { detail: worker }),
          )
        }

        // 已经有一个 waiting 的版本：直接提示。
        if (reg.waiting && navigator.serviceWorker.controller) announce(reg.waiting)

        reg.addEventListener('updatefound', () => {
          const worker = reg.installing
          if (!worker) return
          worker.addEventListener('statechange', () => {
            // 首次安装时 controller 为空：那是「刚接管」，不是「有更新」。
            if (worker.state === 'installed' && navigator.serviceWorker.controller) {
              announce(worker)
            }
          })
        })

        // 回到前台时问一次更新；不轮询。
        document.addEventListener('visibilitychange', () => {
          if (document.visibilityState === 'visible') {
            void reg.update().catch(() => {
              /* 离线或端点暂不可达：下一次再问 */
            })
          }
        })
      })
      .catch((e) => {
        console.info('PWA registration skipped:', e)
      })
  })
}

export function isPwaUpdateEvent(e: Event): e is UpdateEvent {
  return e.type === PWA_UPDATE_EVENT
}
