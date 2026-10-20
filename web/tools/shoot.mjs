import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

mkdirSync('D:/github/yechao/docs/shots', { recursive: true })

const b = await chromium.launch()
const p = await b.newPage({ viewport: { width: 1440, height: 1000 } })
await p.goto('http://127.0.0.1:8787/', { waitUntil: 'networkidle' })
await p.waitForTimeout(1600)

const shots = [
  ['digests', 0],
  ['feed', 1],
  ['cats', 2],
  ['calendar', 3],
  ['privacy', 4],
  ['settings', 5],
]

for (const [name, idx] of shots) {
  await p.locator('.rail__link').nth(idx).click()
  await p.waitForTimeout(1000)
  await p.screenshot({ path: `D:/github/yechao/docs/shots/${name}.png` })
  console.log('shot', name)
}

// 窄屏
await p.setViewportSize({ width: 375, height: 812 })
await p.locator('.rail__link').nth(0).click()
await p.waitForTimeout(700)
await p.screenshot({ path: 'D:/github/yechao/docs/shots/digests-mobile.png' })
await p.locator('.rail__link').nth(1).click()
await p.waitForTimeout(700)
await p.screenshot({ path: 'D:/github/yechao/docs/shots/feed-mobile.png' })

await b.close()
console.log('done')
