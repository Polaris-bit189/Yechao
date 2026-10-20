/**
 * 量关键间距。评审里的「约 40px」「约 60px」都是估的，改之前先量准。
 *
 * 用法：node tools/measure.mjs [url]
 */
import { chromium } from 'playwright'

const URL = process.argv[2] ?? process.env.YC_URL ?? 'http://localhost:5199/'

const b = await chromium.launch()
const p = await b.newPage({ viewport: { width: 1440, height: 1000 } })
await p.goto(URL, { waitUntil: 'networkidle' })
await p.evaluate(() => localStorage.setItem('yechao:remind-asked', '1'))
await p.reload({ waitUntil: 'networkidle' })
await p.waitForTimeout(1200)

const box = (el) => {
  const r = el.getBoundingClientRect()
  return { top: Math.round(r.top), bottom: Math.round(r.bottom), h: Math.round(r.height), w: Math.round(r.width) }
}

async function onPage(i) {
  await p.locator('.rail__link').nth(i).click()
  await p.waitForTimeout(700)
}

// 侧栏
await onPage(0)
console.log(
  '侧栏',
  JSON.stringify(
    await p.evaluate(() => {
      const q = (s) => document.querySelector(s)
      const r = (s) => {
        const el = q(s)
        if (!el) return null
        const b = el.getBoundingClientRect()
        return { top: Math.round(b.top), bottom: Math.round(b.bottom) }
      }
      const links = document.querySelectorAll('.rail__link')
      const last = links[links.length - 1].getBoundingClientRect()
      return { navEnd: Math.round(last.bottom), tailTop: r('.rail__tail')?.top }
    }),
  ),
)

// 页头到内容的间距（每页都量）
const HEAD_GAP = {
  0: ['.day__head', '.paper'],
  1: ['.phead', '.stream'],
  2: ['.phead', '.kinds'],
  3: ['.phead', '.monthbar'],
  4: ['.phead', '.verdict'],
  5: ['.phead', '.setup'],
}
for (const [idx, [headSel, nextSel]] of Object.entries(HEAD_GAP)) {
  await onPage(Number(idx))
  const r = await p.evaluate(
    ([a, c]) => {
      const q = (s) => document.querySelector(s)
      if (!q(a) || !q(c)) return null
      const A = q(a).getBoundingClientRect()
      const C = q(c).getBoundingClientRect()
      return { headBottom: Math.round(A.bottom), nextTop: Math.round(C.top), gap: Math.round(C.top - A.bottom) }
    },
    [headSel, nextSel],
  )
  console.log('页头间距', idx, headSel, JSON.stringify(r))
}

// 日历
await onPage(3)
console.log('日历', JSON.stringify(await p.evaluate(() => {
  const q = (s) => document.querySelector(s)
  const b = (s) => (q(s) ? { w: Math.round(q(s).getBoundingClientRect().width), h: Math.round(q(s).getBoundingClientRect().height) } : null)
  return { grid: b('.grid'), cell: b('.grid__cell'), page: b('.main__col') }
})))

// 隐私
await onPage(4)
console.log('隐私', JSON.stringify(await p.evaluate(() => {
  const s = document.querySelectorAll('.stat')
  return {
    stat: s.length ? { w: Math.round(s[0].getBoundingClientRect().width), h: Math.round(s[0].getBoundingClientRect().height) } : null,
    statsBottom: s.length ? Math.round(s[s.length - 1].getBoundingClientRect().bottom) : null,
    scopeBottom: Math.round(document.querySelector('.bound__scope').getBoundingClientRect().bottom),
  }
})))

// 类目
await onPage(2)
console.log('类目', JSON.stringify(await p.evaluate(() => {
  const rows = document.querySelectorAll('.kinds__row')
  const last = rows[rows.length - 1]
  return {
    n: rows.length,
    row: rows.length ? Math.round(rows[0].getBoundingClientRect().height) : null,
    listBottom: last ? Math.round(last.getBoundingClientRect().bottom) : null,
  }
})))

// 日报分节间距
await onPage(0)
console.log('日报', JSON.stringify(await p.evaluate(() => {
  const h = document.querySelectorAll('.paper__h2')
  const out = []
  for (let i = 1; i < h.length; i++) {
    const prev = h[i - 1].nextElementSibling
    out.push({
      节: h[i].textContent,
      上一块底: Math.round(prev.getBoundingClientRect().bottom),
      本节顶: Math.round(h[i].getBoundingClientRect().top),
      间距: Math.round(h[i].getBoundingClientRect().top - prev.getBoundingClientRect().bottom),
    })
  }
  return out
})))

await b.close()
