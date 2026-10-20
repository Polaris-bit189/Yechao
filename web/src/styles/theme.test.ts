/**
 * 主题系统的三个测试（规格 §七）。
 *
 * 为什么这三件事值得写成断言：主题是**数据**，不是代码。一套主题漏了一个
 * 圆角、对比度掉到 4:1、调用点里冒出一个写死的色值 —— 这三件事都不会让页面
 * 报错，只会让某一套主题「看起来不太对」。而没人会去逐套肉眼检查六套主题 ×
 * 六个页面，所以只能让测试去查。
 *
 *   ① token 齐不齐  —— 六块（**含彩蛋**）的 token 名集合两两完全相等；
 *                      且调用点引用到的每个变量都有定义。
 *   ② 对比度        —— 正文 ≥4.5:1、次要 ≥3:1；有壁纸的主题把最亮像素
 *                      一起算（算法与它的近似之处写在那个 describe 里）。
 *   ③ 颜色不外泄    —— 调用点里不许出现裸色值或裸调色板类名。
 *
 * ※ 遍历集合来自 `lib/theme.ts` 的 `ALL`（六套），不是 `MENU`（五套）。
 *    彩蛋 katsuji 不在菜单里，用 MENU 遍历会**整块漏测** —— 规格 §八.4 记的
 *    就是这个坑（从彩蛋块里删掉一个 token，测试照样全绿）。下面有一条断言
 *    专门盯着它，并且带负向对照（拿 MENU 跑一遍，集合必须不一样）。
 */
import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

import { ALL, MENU } from '../lib/theme'

// ── 读文件 ───────────────────────────────────────────────────
//
// 两套机制，各有各的理由：
//
//   · `.ts` / `.tsx` 走 Vite 的 `?raw` —— 干净，不碰文件系统。
//   · `.css` 只能落到 `node:fs`：Vitest 默认不处理 CSS，**连 `?raw` 都被
//     截成空串**（实测：src 下六个 `.css?raw` 全返回 ""）。所以先用 glob
//     拿到路径，再自己读。
//
// `node:fs` 的类型见 ./theme-test-types.d.ts（这个仓库没装 @types/node）。

const read = (rel: string): string => readFileSync(new URL(rel, import.meta.url), 'utf8')

const CSS_FILES: Record<string, string> = {}
for (const path of Object.keys(import.meta.glob('../**/*.css'))) CSS_FILES[path] = read(path)

const SOURCE_FILES = import.meta.glob('../**/*.{ts,tsx}', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>

// ※ Vite 的 `import.meta.glob` **不含导入它的那个模块自己**（实测：src 下
// 22 个 .ts/.tsx 里少了 theme.test.ts）。所以显式把本文件补进去 ——
// 规格 §七 提醒的正是这件事：扫描范围包括写测试的人自己，注释里写一个带
// 透明度的色值串也算违规。下面那几条正则就是照着这条约束拆开拼的。
SOURCE_FILES['./theme.test.ts'] = read('./theme.test.ts')

const themeCss = read('./theme.css')

// ── 一点点 CSS 解析 ──────────────────────────────────────────
//
// 只够解析「顶层规则 + 平铺的声明」。@media / @keyframes 里的嵌套会被整块
// 跳过去 —— 我们关心的 :root 与主题块都是平铺的。

interface Block {
  selector: string
  body: string
}

function parseBlocks(css: string): Block[] {
  const clean = css.replace(/\/\*[\s\S]*?\*\//g, '')
  const out: Block[] = []
  let i = 0
  while (i < clean.length) {
    const open = clean.indexOf('{', i)
    if (open === -1) break
    const selector = clean.slice(i, open).split('}').pop()!.trim()
    let depth = 1
    let j = open + 1
    while (j < clean.length && depth > 0) {
      if (clean[j] === '{') depth++
      else if (clean[j] === '}') depth--
      j++
    }
    out.push({ selector, body: clean.slice(open + 1, j - 1) })
    i = j
  }
  return out
}

const RE_DECL = new RegExp('--' + '([\\w-]+)\\s*:\\s*([^;]+);', 'g')

function declarations(body: string): Map<string, string> {
  const out = new Map<string, string>()
  for (const m of body.matchAll(RE_DECL)) out.set(m[1], m[2].trim())
  return out
}

function tokenNames(body: string): Set<string> {
  return new Set(declarations(body).keys())
}

const BLOCKS = parseBlocks(themeCss)

/**
 * 主题块的选择器**必须恰好**长这样（`:root` 别名可以有，别的不许有）。
 *
 * 严格匹配是刻意的：宽松一点，`[data-theme='yoi'] .sky` 这类规则也会被当成
 * 主题块，于是宵那块被一个不含任何 token 的规则覆盖 —— 症状是「六块都在」
 * 那条红，而不是静默漏测。宁可红在解析上，也不要绿在漏测上。
 */
const THEME_SELECTOR = new RegExp(
  '^(?::root[^,]*,\\s*)?' + "\\[data-theme='([a-z0-9]+)'\\]$",
)

/** 从 theme.css 里真的解析出来的六块。 */
const THEMES = new Map<string, Map<string, string>>()
for (const b of BLOCKS) {
  const m = b.selector.match(THEME_SELECTOR)
  if (m) THEMES.set(m[1], declarations(b.body))
}

/** 全局块：只放全站唯一的东西，不带任何 [data-theme]。 */
let GLOBAL: Map<string, string> | null = null
for (const b of BLOCKS) if (b.selector === ':root') GLOBAL = declarations(b.body)

/** parity 遍历的就是这个集合 —— 它必须来自 ALL，不是 MENU。 */
const TRAVERSED: string[] = ALL.map((t) => t.id)

/** 结构 token：这 13 个不是全局常量，是主题身份（规格 §四 铁律二）。 */
const STRUCTURAL = [
  'r1',
  'r2',
  'r3',
  'shadow',
  'shadow-lg',
  'blur',
  'sans',
  'mono',
  'track-tight',
  'track-wide',
  't-fast',
  't-mid',
  'ease',
]

/** 每块写满这么多。六块一起少一个，parity 是查不出来的，只能靠这个数。
 *  ※ 类目色从七降到六（类目体系按「读者要做什么」重推，见
 *  `yechao/domain/categories.py`），所以这个数从 42 变成 41。 */
const TOKENS_PER_THEME = 41

// ── 颜色 ─────────────────────────────────────────────────────
//
// ※ 下面这几条正则**故意拆成两段拼**。规格 §七 提醒：第三个测试扫的是
// .ts/.tsx 的**纯文本**，连注释里写一个带透明度的色值串都会被判违规 ——
// 而这个文件本身也在扫描范围里。拼开之后，文件里出现的是 `'#'` 与
// `'[0-9a-fA-F]{3,8}'` 两段，凑不出一个能被自己匹配到的色值。

const RE_RGBA_ONE = new RegExp(
  '^(?:rgb|hsl)a?' +
    '\\(\\s*([\\d.]+)[\\s,]+([\\d.]+)[\\s,]+([\\d.]+)(?:[\\s,/]+([\\d.]+))?\\s*\\)$',
)

const RE_RGBA_ALL = new RegExp(
  '(?:rgb|hsl)a?' +
    '\\(\\s*([\\d.]+)[\\s,]+([\\d.]+)[\\s,]+([\\d.]+)(?:[\\s,/]+([\\d.]+))?\\s*\\)',
  'g',
)

type Rgba = [number, number, number, number]

function parseColor(raw: string): Rgba | null {
  const s = raw.trim()
  if (s.startsWith('#')) {
    const h = s.slice(1)
    const byte = (i: number) => parseInt(h.slice(i, i + 2), 16)
    if (h.length === 3) {
      return [
        parseInt(h[0] + h[0], 16),
        parseInt(h[1] + h[1], 16),
        parseInt(h[2] + h[2], 16),
        1,
      ]
    }
    if (h.length === 6) return [byte(0), byte(2), byte(4), 1]
    return null
  }
  const m = s.match(RE_RGBA_ONE)
  if (!m) return null
  return [+m[1], +m[2], +m[3], m[4] === undefined ? 1 : +m[4]]
}

function mustColor(raw: string | undefined, where: string): Rgba {
  const c = raw === undefined ? null : parseColor(raw)
  if (!c) throw new Error(`${where}: 解析不出颜色（${String(raw)}）`)
  return c
}

function composite(fg: Rgba, bg: Rgba): Rgba {
  const a = fg[3]
  return [
    fg[0] * a + bg[0] * (1 - a),
    fg[1] * a + bg[1] * (1 - a),
    fg[2] * a + bg[2] * (1 - a),
    1,
  ]
}

function luminance(c: Rgba): number {
  const f = (v: number) => {
    const s = v / 255
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4
  }
  return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2])
}

function contrast(a: Rgba, b: Rgba): number {
  const la = luminance(a)
  const lb = luminance(b)
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05)
}

function themeColor(id: string, token: string): Rgba {
  return mustColor(THEMES.get(id)!.get(token), `${id} 的 --${token}`)
}

// ══ ① token 齐不齐 ══════════════════════════════════════════

describe('token 齐不齐', () => {
  it('六块都在，且没有多出来的块', () => {
    // 正反两边都断言：少了是漏写，多了是删主题时没删干净。
    expect([...THEMES.keys()].sort()).toEqual([...TRAVERSED].sort())
  })

  it('※ 遍历集合来自 ALL（含彩蛋 katsuji），不是 MENU', () => {
    // 规格 §八.4 栽过的坑：彩蛋不在主列表里，parity 遍历主列表 → 彩蛋整块
    // 没被测到。所以这里带一个**负向对照**：拿 MENU 跑一遍，集合必须不同。
    expect(TRAVERSED).toContain('katsuji')
    expect(MENU.map((t) => t.id)).not.toContain('katsuji')
    expect([...TRAVERSED].sort()).not.toEqual(MENU.map((t) => t.id).sort())

    // 光在集合里还不够 —— 那一块得真的被解析出来（不是空壳）。
    expect(THEMES.get('katsuji')!.size).toBeGreaterThan(0)
    expect([...THEMES.keys()]).toContain('katsuji')
  })

  it('六块的 token 名集合两两完全相等', () => {
    const [first, ...rest] = TRAVERSED
    const base = [...THEMES.get(first)!.keys()].sort()
    for (const id of rest) {
      const other = [...THEMES.get(id)!.keys()].sort()
      const missing = base.filter((n) => !other.includes(n))
      const extra = other.filter((n) => !base.includes(n))
      expect(
        { id, missing, extra },
        `${id} 与 ${first} 的 token 集合不一致（missing = 漏写，extra = 多写）`,
      ).toEqual({ id, missing: [], extra: [] })
    }
  })

  it('每块都写满，一个不落', () => {
    for (const id of TRAVERSED) {
      // 光靠 parity 查不出「六块一起少了一个」—— 数量是另一道闸。
      expect(THEMES.get(id)!.size, `${id} 的 token 数不对`).toBe(TOKENS_PER_THEME)
    }
  })

  it('结构 token 在每一块里都是自己写的（圆角/阴影/模糊/字体/字距/动效）', () => {
    for (const id of TRAVERSED) {
      const t = THEMES.get(id)!
      const absent = STRUCTURAL.filter((n) => !t.has(n))
      expect(absent, `${id} 缺结构 token：${absent.join(', ')}`).toEqual([])
    }
  })

  it('全局块与主题块的 token 名集合不相交 —— 堵死靠 :root 兜底的路', () => {
    expect(GLOBAL, '没找到 :root 全局块').not.toBeNull()
    const g = GLOBAL!
    // 空转自检：全局块必须真的被判到，扫一个空集合的断言恒为真。
    expect(g.size).toBeGreaterThan(0)

    for (const id of TRAVERSED) {
      const both = [...THEMES.get(id)!.keys()].filter((n) => g.has(n))
      expect(
        both,
        `这些 token 既在 :root 又在 ${id} 里 —— 主题块会静默盖住全局值，` +
          `「漏写」和「想继承」从此分不出来：${both.join(', ')}`,
      ).toEqual([])
    }
  })

  it('全局块只放全站唯一的东西（间距 / 骨架 / 字号阶）', () => {
    const g = [...GLOBAL!.keys()].sort()
    const allowed = new Set([
      's1',
      's2',
      's3',
      's4',
      's5',
      's6',
      's7',
      'rail',
      'page',
      'prose',
      'fs-date',
      'fs-title',
      'fs-card',
      'fs-body',
      'fs-sub',
      'fs-tag',
    ])
    const unexpected = g.filter((n) => !allowed.has(n))
    expect(
      unexpected,
      `这些不该是全局的 —— 判据是「它是否属于某套主题的气质」：${unexpected.join(', ')}`,
    ).toEqual([])
  })

  it('调用点引用到的每个变量都有定义', () => {
    // 「定义」= 任一 CSS 文件里任一规则块里的 --x 声明。
    const declared = new Set<string>()
    for (const css of Object.values(CSS_FILES)) {
      for (const b of parseBlocks(css)) for (const n of tokenNames(b.body)) declared.add(n)
    }
    expect(declared.size).toBeGreaterThan(0)

    // 由 JS 内联设置的变量：它们的「定义」在 tsx 的 style 里，不在 CSS 里。
    // Sky.tsx 给每个粒子槽发位置与周期，KindsPage.tsx 给每行发类目色。
    // 名字一律**不带前导 --**，跟 tokenNames() 保持一致。
    const INLINE_ONLY = new Set(['w', 'h', 'cycle', 'lag', 'sway', 'dur', 'kc'])

    const RE_VAR = new RegExp('var' + '\\(\\s*--([\\w-]+)', 'g')

    const missing: string[] = []
    let refs = 0
    for (const [file, text] of Object.entries({ ...CSS_FILES, ...SOURCE_FILES })) {
      for (const m of text.matchAll(RE_VAR)) {
        refs++
        if (!declared.has(m[1]) && !INLINE_ONLY.has(m[1])) missing.push(`${file}: var(--${m[1]})`)
      }
    }
    // 空转自检：一条 var() 都没扫到说明扫描本身坏了。
    expect(refs, '一条 var() 都没扫到').toBeGreaterThan(0)
    expect(missing, '这些变量没有定义').toEqual([])

    // 内联那几个也不能是凭空豁免：它们得真的被某个 tsx 设过。
    for (const v of INLINE_ONLY) {
      const used = Object.entries(SOURCE_FILES).some(([, text]) => text.includes(`'--${v}'`))
      expect(used, `--${v} 不在豁免名单里却也没人设它`).toBe(true)
    }
  })
})

// ══ ② 对比度 ════════════════════════════════════════════════

/**
 * 壁纸最亮像素。
 *
 * 有壁纸的主题（六套都有 `.sky`）不能只按 `--bg` 算对比度：文字底下实际是
 * 「底色 + 一层光」。算法是把该主题 `.sky` 里出现的光层（gradient 里的 rgba
 * 字面量与 `var(--fx-glow)`）逐层叠到 `--bg` 上，取**叠加后最亮的那一层**。
 *
 * 这是一次**解析式估算，不是采样**，三处近似写在这里免得被当成实测：
 *   - 没有真的渲染再读像素，所以「最亮」是算出来的；
 *   - 粒子（`.sky__dot`）不算：它们不铺满屏幕，不构成底色；
 *   - 取单层而不是把所有层叠满：真实像素不会同时吃满所有光层，全叠是过估，
 *     会让本来合格的配色判红（试过，雾和铅印两套会被全叠算法判死）。
 *
 * 所以这份表是**下界**（真实最亮点只会比它亮一点），不是实测值。
 * 每个主题算出来的值登记在 `WALLPAPER` 里，下面的对比度一律一起算。
 */
const WALLPAPER = new Map<string, Rgba>()

function skyLightLayers(id: string): Rgba[] {
  const t = THEMES.get(id)!
  const out: Rgba[] = []
  for (const b of BLOCKS) {
    if (!b.selector.startsWith(`[data-theme='${id}']`)) continue
    if (!b.selector.includes('.sky')) continue
    if (b.selector.includes('.sky__dot')) continue // 粒子不铺满
    for (const m of b.body.matchAll(RE_RGBA_ALL)) {
      out.push([+m[1], +m[2], +m[3], m[4] === undefined ? 1 : +m[4]])
    }
    for (const m of b.body.matchAll(new RegExp('var' + '\\((--fx-glow)\\)', 'g'))) {
      const c = parseColor(t.get(m[1]) ?? '')
      if (c) out.push(c)
    }
  }
  return out
}

for (const id of TRAVERSED) {
  const bg = themeColor(id, 'bg')
  let best = bg
  let bestL = luminance(bg)
  for (const layer of skyLightLayers(id)) {
    const c = composite(layer, bg)
    const L = luminance(c)
    if (L > bestL) {
      bestL = L
      best = c
    }
  }
  WALLPAPER.set(id, best)
}

/** 文字可能落在哪几种底上。`bg` 那一种按壁纸最亮像素算（更严）。 */
function grounds(id: string): { name: string; c: Rgba }[] {
  return [
    { name: '--bg', c: themeColor(id, 'bg') },
    { name: '--bg-2', c: themeColor(id, 'bg-2') },
    { name: '--surface', c: themeColor(id, 'surface') },
    { name: '--surface-2', c: themeColor(id, 'surface-2') },
    { name: '壁纸最亮像素', c: WALLPAPER.get(id)! },
  ]
}

const BODY_RATIO = 4.5
const SUB_RATIO = 3

/** 跑一组「前景色 × 所有底色」的对比度，返回没过的那些。 */
function check(ids: string[], tokens: string[], need: number): string[] {
  const fails: string[] = []
  for (const id of ids) {
    for (const token of tokens) {
      const fg = themeColor(id, token)
      for (const g of grounds(id)) {
        const r = contrast(composite(fg, g.c), g.c)
        if (r < need) {
          fails.push(`${id} --${token} on ${g.name} = ${r.toFixed(2)} < ${need}`)
        }
      }
    }
  }
  return fails
}

describe('对比度', () => {
  it('正文（--fg）在每种底色上 ≥ 4.5:1', () => {
    expect(check(TRAVERSED, ['fg'], BODY_RATIO), '正文对比度不足').toEqual([])
  })

  it('次要（--fg-2 / --fg-3）≥ 3:1', () => {
    expect(check(TRAVERSED, ['fg-2', 'fg-3'], SUB_RATIO), '次要对比度不足').toEqual([])
  })

  it('强调底上的文字（--on-accent on --accent）≥ 4.5:1', () => {
    const fails: string[] = []
    for (const id of TRAVERSED) {
      const bg = themeColor(id, 'accent')
      const fg = themeColor(id, 'on-accent')
      const r = contrast(composite(fg, bg), bg)
      if (r < BODY_RATIO) fails.push(`${id} = ${r.toFixed(2)}`)
    }
    expect(fails, '按钮/强调块上的字对比度不足').toEqual([])
  })

  it('链接色 --accent 当正文用 ≥ 4.5:1', () => {
    // base.css 里 `a { color: var(--accent) }` —— 它是正文的一部分，不是装饰。
    expect(check(TRAVERSED, ['accent'], BODY_RATIO), '链接色对比度不足').toEqual([])
  })

  it('语义色当文字用 ≥ 3:1（危险 / 警告 / 成功 / 六个类目）', () => {
    expect(
      check(
        TRAVERSED,
        [
          'danger',
          'warn',
          'ok',
          'k-todo',
          'k-attend',
          'k-know',
          'k-claim',
          'k-trade',
          'k-recruit',
        ],
        SUB_RATIO,
      ),
      '语义色对比度不足',
    ).toEqual([])
  })

  it('壁纸最亮像素登记表：六个主题每个都有一格', () => {
    // 空转自检：没登记就等于没算，不能假装算过。
    expect(WALLPAPER.size).toBe(TRAVERSED.length)
    for (const id of TRAVERSED) {
      const w = WALLPAPER.get(id)!
      // 壁纸只会把底色提亮，不会压暗 —— 比 --bg 还暗说明合成写反了。
      expect(luminance(w), `${id} 的壁纸最亮像素比 --bg 还暗`).toBeGreaterThanOrEqual(
        luminance(themeColor(id, 'bg')),
      )
    }
  })
})

// ══ ③ 颜色不外泄 ════════════════════════════════════════════

describe('颜色不外泄', () => {
  it('调用点（.ts / .tsx）里没有裸色值', () => {
    const RE_HEX = new RegExp('#' + '[0-9a-fA-F]{3,8}(?![0-9a-zA-Z_-])', 'g')
    const RE_FN = new RegExp('(?:rgb|hsl)a?' + '\\(', 'g')

    const hits: string[] = []
    let scanned = 0
    for (const [file, text] of Object.entries(SOURCE_FILES)) {
      scanned++
      text.split('\n').forEach((line, i) => {
        for (const m of line.matchAll(RE_HEX)) hits.push(`${file}:${i + 1}: ${m[0]}`)
        for (const m of line.matchAll(RE_FN)) hits.push(`${file}:${i + 1}: ${m[0]}`)
      })
    }
    // 空转自检：一个文件都没扫到说明 glob 坏了。
    expect(scanned, '一个源文件都没扫到').toBeGreaterThan(0)
    expect(
      hits,
      '颜色只能出现在 theme.css 里，调用点写的是语义变量（var(--accent) 之类）',
    ).toEqual([])
  })

  it('调用点里没有裸调色板类名（调色板名 + 色号那种写法）', () => {
    // 这一版是原生 CSS，没有 Tailwind；留着这条是防它哪天被引进来 ——
    // 裸调色板类名是最容易悄悄长出来的那一种硬编码颜色。
    const RE_PALETTE = new RegExp(
      '\\b(?:text|bg|border|ring|fill|stroke|from|via|to|divide|outline|shadow|caret|decoration|placeholder)' +
        '-(?:slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)' +
        '-\\d{2,3}\\b',
      'g',
    )
    const hits: string[] = []
    for (const [file, text] of Object.entries(SOURCE_FILES)) {
      for (const m of text.matchAll(RE_PALETTE)) hits.push(`${file}: ${m[0]}`)
    }
    expect(hits, '不许在调用点直接点名调色板').toEqual([])
  })

  it('--on-accent 与 --accent-weak 成对出现', () => {
    // 「坐在强调淡底上的文字」与那块淡底是一对：只有一半的时候，
    // 要么是淡底没有对应的文字色，要么反过来 —— 两种都是漏写。
    const bad = TRAVERSED.filter((id) => {
      const t = THEMES.get(id)!
      return t.has('on-accent') !== t.has('accent-weak')
    })
    expect(bad, '这两者必须成对').toEqual([])
    // 空转自检：这一条真的检查了六块。
    expect(TRAVERSED.length).toBe(6)
  })
})
