/**
 * 截图：按主题 + 页面组合出图。
 *
 * 用法：node tools/shot.mjs <主题id> <前缀> [页序...]
 *   主题 id 见 src/lib/theme.ts（yoi / kiri / tsuki / sakura / yuu / katsuji）
 *   页序默认全部六页；页码 0..5 对应 日报/信息流/类目/日历/隐私/配置
 *
 * 为什么自己写一份而不复用 shoot.mjs：
 *   shoot.mjs 把输出目录硬编码成 D:/github/yechao/docs/shots（审计 S3-05 记过），
 *   而且只会用默认主题。换主题要改 localStorage 再刷新，它做不到。
 *
 * 目标地址默认 9000（后端 serve 出来的 dist），可用 YC_URL 覆盖。
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const theme = process.argv[2] ?? 'yoi'
const prefix = process.argv[3] ?? 'now'
const pages = process.argv.slice(4).map(Number)
const idxs = pages.length > 0 ? pages : [0, 1, 2, 3, 4, 5]

const URL = process.env.YC_URL ?? 'http://127.0.0.1:9000/'
const OUT = 'tools/shots'
mkdirSync(OUT, { recursive: true })

const NAMES = ['digests', 'feed', 'cats', 'calendar', 'privacy', 'settings']

// MOBILE=1 拍窄屏（375×812，规格里移动端截图用的尺寸）。
const MOBILE = process.env.MOBILE === '1'
const SIZE = MOBILE ? { width: 375, height: 812 } : { width: 1440, height: 1000 }

const b = await chromium.launch()
const p = await b.newPage({ viewport: SIZE })
const errs = []
p.on('pageerror', (e) => errs.push(String(e)))
p.on('console', (m) => m.type() === 'error' && errs.push(m.text()))

// 先落主题再进页面：主题从 localStorage 读，晚设会被初始渲染覆盖。
await p.goto(URL, { waitUntil: 'networkidle' })
await p.evaluate((t) => localStorage.setItem('yechao:theme', t), theme)
// 把提醒条的一次性标记也设上：它已经问过就不再出现，
// 截图要看的是「正常使用中」的界面，不是首次访问。
// KEEP_REMIND=1 反过来 —— 保留首次访问的样子，用来确认那条提示还在时
// 页面顶部有没有为它留出位置（它离开后那 36px 要收回去）。
if (process.env.KEEP_REMIND !== '1') {
  await p.evaluate(() => localStorage.setItem('yechao:remind-asked', '1'))
}
await p.reload({ waitUntil: 'networkidle' })
await p.waitForTimeout(1500)

for (const i of idxs) {
  await p.locator('.rail__link').nth(i).click()
  await p.waitForTimeout(900)
  const file = `${OUT}/${prefix}-${NAMES[i]}.png`
  await p.screenshot({ path: file })
  console.log('shot', file)
}

// 顺带量一下关键尺寸，方便「按尺寸核对」而不是只靠眼睛。
const box = await p.evaluate(() => {
  const r = (sel) => {
    const el = document.querySelector(sel)
    if (!el) return null
    const b = el.getBoundingClientRect()
    return { w: Math.round(b.width), h: Math.round(b.height), top: Math.round(b.top) }
  }
  return {
    grid: r('.grid'),
    cell: r('.grid__cell'),
    stat: r('.stat'),
    kindsRow: r('.kinds__row'),
    railNavLast: (() => {
      const els = document.querySelectorAll('.rail__link')
      const el = els[els.length - 1]
      return el ? { bottom: Math.round(el.getBoundingClientRect().bottom) } : null
    })(),
    railTailTop: r('.rail__tail')?.top ?? null,
  }
})
console.log('box', JSON.stringify(box))
if (errs.length) console.log('控制台错误:', errs.slice(0, 5))

await b.close()
