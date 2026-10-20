import {
  IcBox,
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

/** 用户是否要求减少动效。这个界面里所有动画都要问过它一次。 */
function prefersReducedMotion(): boolean {
  try {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches
  } catch {
    return false
  }
}

/**
 * 上色。
 *
 * ※ 切主题走 View Transitions，而不是给颜色挂 transition。
 *
 * 理由不是好看，是**规格**：`界面规格.md` §六 要求「动效只动 transform/opacity」，
 * 因为动 `background-color` / `color` 会触发重绘，低端机掉帧。而直接给颜色挂
 * transition 正是这么干的 —— 六套主题切一次要重绘整个页面。
 *
 * `document.startViewTransition()` 内部做的是「截下旧帧 → 截下新帧 → 交叉溶解」，
 * 动的只有两张位图的 opacity，落在允许范围内。
 *
 * 不支持该 API 的浏览器、以及 `prefers-reduced-motion` 的用户，直接硬切 ——
 * 这正是改动之前的行为，不是降级到更差，是不做增强。
 *
 * @param animate 只有**用户主动切主题**才传 true。首帧上色（main.tsx）走默认值，
 *                否则会在页面还没渲染时做一次空白过渡。
 */
export function paint(id: ThemeId, animate = false): void {
  const root = document.documentElement
  // 值没变就别动 —— 省掉一次无意义的过渡（首帧会撞上这种情况）。
  if (root.getAttribute('data-theme') === id) return

  const apply = () => root.setAttribute('data-theme', id)

  const doc = document as unknown as {
    startViewTransition?: (cb: () => void) => unknown
  }
  if (!animate || typeof doc.startViewTransition !== 'function' || prefersReducedMotion()) {
    apply()
    return
  }
  doc.startViewTransition(apply)
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

// ※ 这两张表按**类目 slug** 映射，而 slug 由后端定
// （`yechao/domain/categories.py` 的六个动作类目）。加一个类目就要在这里
// 补两行；漏了不会报错，只会回落到 `KIND_FALLBACK_ICON` 与 `--fg-3` ——
// 而那个回落**看起来是正常的**，所以别指望它提醒你。
const KIND_COLOR: Record<string, string> = {
  // 「要办的事」是最需要一眼认出的那一类，用最亮的蓝。
  todo: 'var(--k-todo)',
  attend: 'var(--k-attend)',
  // 「只要知道」不需要动作，给最弱的那一档（粉）。
  know: 'var(--k-know)',
  claim: 'var(--k-claim)',
  trade: 'var(--k-trade)',
  recruit: 'var(--k-recruit)',
}

const KIND_ICON: Record<string, (p: { size?: number }) => React.ReactElement> = {
  // 文件 = 一份要填要交的东西 ⇒ 「要办的事」。
  todo: IcFileText,
  // 话筒 = 一个台子、一场活动 ⇒ 「要到场的事」。
  attend: IcMic,
  // 房子 = 日常安排（放假、维修、菜谱）⇒ 「只要知道」。
  know: IcHome,
  // 放大镜照着文件 = 在一堆东西里找一件 ⇒ 「失物招领」。
  claim: IcFileSearch,
  // 箱子 = 一件要出手的东西 ⇒ 「转让求购」。
  trade: IcBox,
  // 公文包 = 一个岗位 ⇒ 「招人」。
  recruit: IcBriefcase,
}

const KIND_FALLBACK_ICON = IcTag

export function kindColor(slug: string): string {
  return KIND_COLOR[slug] ?? 'var(--fg-3)'
}

export function kindIcon(slug: string) {
  return KIND_ICON[slug] ?? KIND_FALLBACK_ICON
}
