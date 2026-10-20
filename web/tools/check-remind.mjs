/**
 * 走一遍「首次访问 → 点掉提醒条 → 刷新」这条路，确认：
 *   1. 点过之后它不再出现（评审 §三：它是常驻横幅）；
 *   2. 它消失后，顶部为它留的 36px 也收回去（否则每页顶上永远压一条空带）。
 */
import { chromium } from 'playwright'

const TARGET = process.argv[2] ?? process.env.YC_URL ?? 'http://localhost:5199/'

const b = await chromium.launch()
const p = await b.newPage({ viewport: { width: 1440, height: 1000 } })

const state = () =>
  p.evaluate(() => {
    const shell = document.querySelector('.shell')
    const rail = document.querySelector('.rail')
    const main = document.querySelector('.main')
    return {
      有提醒条: !!document.querySelector('.remind'),
      is_bare: shell?.classList.contains('is-bare') ?? null,
      侧栏顶: rail ? Math.round(rail.getBoundingClientRect().top) : null,
      内容顶: main ? Math.round(main.getBoundingClientRect().top) : null,
      内容内边距上: main ? getComputedStyle(main).paddingTop : null,
    }
  })

await p.goto(TARGET, { waitUntil: 'networkidle' })
await p.evaluate(() => localStorage.removeItem('yechao:remind-asked'))
await p.reload({ waitUntil: 'networkidle' })
await p.waitForTimeout(900)
console.log('首次访问 ', JSON.stringify(await state()))

await p.locator('.remind__no').click()
await p.waitForTimeout(2400)
console.log('点过「不用」', JSON.stringify(await state()))

await p.reload({ waitUntil: 'networkidle' })
await p.waitForTimeout(900)
console.log('再刷新  ', JSON.stringify(await state()))

await p.screenshot({ path: 'tools/shots/remind-gone.png' })
await b.close()
