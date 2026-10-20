/**
 * 在**渲染结果**上验证规格，而不是扫源码。
 *
 * 源码扫描会误报（注释里的符号、变量拼接的文本），而这些恰恰是
 * 「看起来违规、实际没问题」的地方。真正要问的是：**页面上有没有**。
 */
import { chromium } from 'playwright'

const b = await chromium.launch()
const p = await b.newPage({ viewport: { width: 1440, height: 1000 } })
const errs = []
p.on('pageerror', (e) => errs.push(String(e)))
p.on('console', (m) => m.type() === 'error' && errs.push(m.text()))

await p.goto('http://127.0.0.1:8787/', { waitUntil: 'networkidle' })
await p.waitForTimeout(1200)

const PAGES = [
  ['日报', 0],
  ['信息流', 1],
  ['类目', 2],
  ['日历', 3],
  ['隐私', 4],
  ['配置', 5],
]

const EMOJI =
  /[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}\u{2B00}-\u{2BFF}\u{1F1E6}-\u{1F1FF}]/gu

const rows = []
for (const [name, idx] of PAGES) {
  await p.locator('.rail__link').nth(idx).click()
  await p.waitForTimeout(900)

  const r = await p.evaluate(() => {
    const txt = document.body.innerText
    return {
      brand: document.querySelector('.rail__brand')?.innerText.replace(/\s+/g, ''),
      hasDot: !!document.querySelector('.rail__link.is-on'),
      // 选中项有没有圆点：::after 的宽度
      dotW: (() => {
        const el = document.querySelector('.rail__link.is-on')
        if (!el) return null
        return getComputedStyle(el, '::after').width
      })(),
      text: txt,
      suspicious: ['undefined', 'NaN', '读不到', '失败', '[object'].filter((w) => txt.includes(w)),
    }
  })

  const emoji = r.text.match(EMOJI) ?? []
  rows.push({
    页: name,
    徽标: r.brand,
    选中圆点: r.dotW,
    emoji: [...new Set(emoji)].join('') || '无',
    可疑字眼: r.suspicious.join(',') || '无',
  })
}

console.table(rows)
console.log('控制台错误:', errs.length === 0 ? '无' : errs.slice(0, 5))

// 移动端：导航是否变成顶部两行、有没有横向溢出
await p.setViewportSize({ width: 375, height: 812 })
await p.locator('.rail__link').nth(1).click()
await p.waitForTimeout(700)
const mob = await p.evaluate(() => ({
  overflow: document.documentElement.scrollWidth > window.innerWidth,
  navPos: getComputedStyle(document.querySelector('.rail')).position,
  navDir: getComputedStyle(document.querySelector('.rail')).flexDirection,
}))
console.log('移动端:', JSON.stringify(mob))

await b.close()
