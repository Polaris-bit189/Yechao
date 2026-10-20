import {
  IcBox,
  IcCap,
  IcFileSearch,
  IcFileText,
  IcHome,
  IcMic,
  IcTag,
  IcBriefcase,
} from '../ui/icons'

/**
 * 主题。
 *
 * 五套在菜单里：宵（默认）、雾、月、樱、幽。
 * 古登堡是彩蛋 —— 页面空白处敲 `katsuji` 才切过去，不出现在菜单里。
 *
 * 彩蛋与另外五套受同样的约束（给全变量、过对比度），区别只是「不主动告诉
 * 你有它」。任何遍历主题做检查的代码要遍历 `ALL`，不是 `MENU`。
 */

export const IDS = ['yoi', 'kiri', 'tsuki', 'sakura', 'yuu', 'katsuji'] as const
export type ThemeId = (typeof IDS)[number]

export interface ThemeMeta {
  id: ThemeId
  name: string
  note: string
}

export const MENU: ThemeMeta[] = [
  { id: 'yoi', name: '宵', note: '深更' },
  { id: 'kiri', name: '雾', note: '赤夜' },
  { id: 'tsuki', name: '月', note: '满月' },
  { id: 'sakura', name: '樱', note: '落樱' },
  { id: 'yuu', name: '幽', note: '微光' },
]

export const EGG: ThemeMeta = { id: 'katsuji', name: '古登堡', note: '活字' }

export const ALL: ThemeMeta[] = [...MENU, EGG]

const KEY = 'yechao:theme'

export function stored(): ThemeId {
  try {
    const v = localStorage.getItem(KEY)
    if (v && (IDS as readonly string[]).includes(v)) return v as ThemeId
  } catch {
    // 隐私模式下访问 localStorage 会抛。主题存不住不该影响打开页面。
  }
  return 'yoi'
}

export function persist(id: ThemeId): void {
  try {
    localStorage.setItem(KEY, id)
  } catch {
    /* 同上 */
  }
}

export function paint(id: ThemeId): void {
  document.documentElement.setAttribute('data-theme', id)
}

/** 敲这个切彩蛋。 */
const CODE = 'katsuji'

/**
 * 监听「在空白处敲 katsuji」。
 *
 * 焦点在输入类元素里时不响应，否则在搜索框里搜这个词会把主题切掉。
 * 返回解绑函数，卸载时必须调。
 */
export function watchEgg(fn: () => void): () => void {
  let buf = ''

  function onKey(e: KeyboardEvent) {
    const el = e.target as HTMLElement | null
    if (el) {
      if (/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName) || el.isContentEditable) {
        buf = ''
        return
      }
    }
    // 只收可打印字符。Shift、方向键之类会污染缓冲。
    if (e.key.length !== 1) return
    buf = (buf + e.key.toLowerCase()).slice(-CODE.length)
    if (buf === CODE) {
      buf = ''
      fn()
    }
  }

  window.addEventListener('keydown', onKey)
  return () => window.removeEventListener('keydown', onKey)
}

// ── 类目外观 ────────────────────────────────────────────────
//
// 色与图标按 slug 映射，不读后端那个 `icon` 字段 —— 那是用户可编辑的
// 数据，把图标名写进用户数据会在换图标库那天让它报废。
// 未知 slug 回落到中性色 + Tag 图标，不回落 emoji：同一屏混两种图标
// 语言比一个陌生的图标更糟。

const KIND_COLOR: Record<string, string> = {
  notice: 'var(--k-notice)',
  academic: 'var(--k-academic)',
  activity: 'var(--k-activity)',
  life: 'var(--k-life)',
  secondhand: 'var(--k-secondhand)',
  lostfound: 'var(--k-lostfound)',
  job: 'var(--k-job)',
}

const KIND_ICON: Record<string, (p: { size?: number }) => React.ReactElement> = {
  notice: IcFileText,
  academic: IcCap,
  activity: IcMic,
  life: IcHome,
  secondhand: IcBox,
  lostfound: IcFileSearch,
  job: IcBriefcase,
}

const KIND_FALLBACK_ICON = IcTag

export function kindColor(slug: string): string {
  return KIND_COLOR[slug] ?? 'var(--fg-3)'
}

export function kindIcon(slug: string) {
  return KIND_ICON[slug] ?? KIND_FALLBACK_ICON
}
