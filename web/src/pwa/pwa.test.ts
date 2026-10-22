import { describe, expect, it } from 'vitest'
// Vite 的 ?raw：测试直接读源文件，不依赖 Node 类型（本仓库没装 @types/node）。
import manifestRaw from '../../public/manifest.webmanifest?raw'
import indexHtml from '../../index.html?raw'
import {
  clearCacheMarker,
  formatCacheTime,
  getSnapshot,
  noteCacheHit,
  noteOffline,
  noteOnline,
} from './offline'

describe('PWA manifest', () => {
  const manifest = JSON.parse(manifestRaw)

  it('has the required identity fields', () => {
    expect(manifest.name).toBe('夜抄')
    expect(manifest.short_name).toBe('夜抄')
    expect(manifest.lang).toBe('zh-CN')
    expect(manifest.start_url).toBe('./')
    expect(manifest.scope).toBe('./')
    expect(manifest.display).toBe('standalone')
  })

  it('has 192/512 any and maskable icons', () => {
    const icons = manifest.icons as Array<{ sizes: string; purpose?: string }>
    for (const [size, purpose] of [
      ['192x192', 'any'],
      ['512x512', 'any'],
      ['192x192', 'maskable'],
      ['512x512', 'maskable'],
    ]) {
      expect(
        icons.some((i) => i.sizes === size && String(i.purpose ?? 'any').includes(purpose)),
        `${size} ${purpose}`,
      ).toBe(true)
    }
  })
})

describe('index.html PWA invariants', () => {
  const html = indexHtml

  it('declares viewport-fit=cover', () => {
    expect(html).toContain('viewport-fit=cover')
  })

  it('links manifest, apple touch icon and theme-color id', () => {
    expect(html).toContain('rel="manifest"')
    expect(html).toContain('apple-touch-icon')
    expect(html).toContain('id="theme-color"')
  })
})

describe('offline store', () => {
  it('keeps the earliest cache hit time in the session', () => {
    clearCacheMarker()
    noteCacheHit(1_000)
    expect(getSnapshot().cachedAt).toBe(1_000_000)
    noteCacheHit(2_000)
    expect(getSnapshot().cachedAt).toBe(1_000_000)
  })

  it('toggles online/offline and clears the marker', () => {
    noteOffline()
    expect(getSnapshot().offline).toBe(true)
    noteOnline()
    expect(getSnapshot().offline).toBe(false)
    clearCacheMarker()
    expect(getSnapshot().cachedAt).toBe(null)
  })

  it('formats HH:MM in local time', () => {
    const d = new Date(2026, 0, 1, 7, 5, 0)
    expect(formatCacheTime(d.getTime())).toBe('07:05')
  })
})
