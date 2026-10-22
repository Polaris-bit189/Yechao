import { useSyncExternalStore } from 'react'
import {
  clearCacheMarker,
  formatCacheTime,
  getSnapshot,
  subscribe,
} from '../pwa/offline'
import './OfflineBanner.css'

/**
 * 离线/缓存横幅。
 *
 * 产品规矩：**旧数据必须可见地说自己是旧的**。所以这里不做「悄悄用缓存」，
 * 而是把缓存时间写出来；在线但本次会话用过缓存时，也给一个「重新加载」。
 * 正常在线且没有缓存命中时，这条不存在（不占首屏）。
 */
export function OfflineBanner() {
  const { cachedAt, offline } = useSyncExternalStore(subscribe, getSnapshot)
  if (!offline && cachedAt === null) return null

  const time = cachedAt === null ? null : formatCacheTime(cachedAt)
  const text =
    offline && time
      ? `离线 · 缓存于 ${time} · 只读`
      : offline
        ? '离线 · 没有可读缓存'
        : `当前显示缓存数据（缓存于 ${time}）`

  return (
    <div className="offlinebar" role="status">
      <span>{text}</span>
      {!offline && cachedAt !== null && (
        <button
          type="button"
          className="offlinebar__go"
          onClick={() => {
            clearCacheMarker()
            window.location.reload()
          }}
        >
          重新加载
        </button>
      )}
    </div>
  )
}
