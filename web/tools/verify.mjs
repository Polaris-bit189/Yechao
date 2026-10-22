/**
 * 移动端 / PWA 验收。**失败退出码非零**，不是只打印一张表。
 *
 * 用法：
 *   node tools/verify.mjs
 *   node tools/verify.mjs --widths 320,375,390,414,767,768,1440
 *   node tools/verify.mjs --browsers chromium,webkit
 *   node tools/verify.mjs --offline --widths 375 --browsers chromium
 *   NEGATIVE_CONTROL=1 node tools/verify.mjs   # 自检：注入溢出，必须红
 *
 * 前置：`python -m yechao serve` 已启动，且 web/dist 是最新构建。
 * WebKit 未安装时打印 [skip]，不伪装成通过。
 */
import { chromium, webkit } from 'playwright'

const args = process.argv.slice(2)
const arg = (name, fallback) => {
  const i = args.indexOf(name)
  return i >= 0 ? args[i + 1] : fallback
}

const URL = arg('--url', process.env.YC_URL ?? 'http://127.0.0.1:9000/')
const widths = arg('--widths', '320,375,390,414,767,768,1440')
  .split(',')
  .map(Number)
  .filter((n) => Number.isFinite(n))
const browserNames = arg('--browsers', 'chromium').split(',')
const offlineMode = args.includes('--offline')
const negative = process.env.NEGATIVE_CONTROL === '1'

const PAGES = ['日报', '信息流', '类目', '日历', '隐私', '配置']
const EMOJI = /[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}\u{2B00}-\u{2BFF}\u{1F1E6}-\u{1F1FF}]/u

const violations = []
let skipped = 0
function fail(msg) {
  violations.push(msg)
  console.log('  ✗', msg)
}

const launchers = { chromium, webkit }

async function inspect(page, width, index) {
  await page.locator('.rail__link').nth(index).click()
  await page.waitForTimeout(650)
  const r = await page.evaluate(() => {
    const q = (s) => document.querySelector(s)
    const box = (el) => {
      const b = el.getBoundingClientRect()
      return { w: Math.round(b.width), h: Math.round(b.height), left: Math.round(b.left) }
    }
    const vis = (el) => !!el && getComputedStyle(el).display !== 'none' && el.getBoundingClientRect().width > 0
    const rail = q('.rail')
    const main = q('.main')
    const grid = q('.grid')
    const chips = q('.chips')
    const inputs = [...document.querySelectorAll('input, select, textarea')].filter(vis)
    const taps = [
      ...document.querySelectorAll(
        '.rail__link, .rail__small, .rail__help, .tabs__btn, .row__act, .monthbar__nav, .remind button, .offlinebar__go, .updatebar__go',
      ),
    ].filter(vis)
    return {
      docW: document.documentElement.scrollWidth,
      innerW: innerWidth,
      railPos: rail ? getComputedStyle(rail).position : null,
      railW: rail ? getComputedStyle(rail).width : null,
      railDir: rail ? getComputedStyle(rail).flexDirection : null,
      mainML: main ? getComputedStyle(main).marginLeft : null,
      gridCols: grid ? getComputedStyle(grid).gridTemplateColumns.trim().split(/\s+/).length : null,
      gridW: grid ? Math.ceil(grid.getBoundingClientRect().width) : null,
      gridParentW: grid?.parentElement ? Math.ceil(grid.parentElement.getBoundingClientRect().width) : null,
      inputFs: inputs.map((el) => getComputedStyle(el).fontSize),
      taps: taps.map((el) => ({ cls: String(el.className), ...box(el) })),
      chipsWrap: chips ? getComputedStyle(chips).flexWrap : null,
      rowAct: vis(q('.row__act')),
      rowTools: q('.row__tools') ? getComputedStyle(q('.row__tools')).display : null,
      dayDateFs: q('.day__date') ? getComputedStyle(q('.day__date')).fontSize : null,
      text: document.body.innerText,
    }
  })

  const where = `${width}px ${PAGES[index]}`
  if (r.docW > width + 1) fail(`${where}: 文档横向溢出 ${r.docW} > ${width}`)
  if (EMOJI.test(r.text)) fail(`${where}: 页面出现 emoji`)

  if (width < 768) {
    if (r.railPos !== 'sticky') fail(`${where}: rail ${r.railPos} ≠ sticky`)
    if (r.railDir !== 'row') fail(`${where}: rail 方向 ${r.railDir} ≠ row`)
    if (width === 320 && index === 5 && r.docW !== 320) fail(`320px 配置: scrollWidth ${r.docW} ≠ 320`)
    for (const fs of r.inputFs) {
      if (parseFloat(fs) < 16) fail(`${where}: 输入字号 ${fs} < 16px`)
    }
    for (const t of r.taps) {
      if (t.w < 44 || t.h < 44) fail(`${where}: ${t.cls} ${t.w}×${t.h} < 44`)
    }
    if (index === 0 && r.dayDateFs !== '26px') fail(`${where}: 日期字号 ${r.dayDateFs} ≠ 26px`)
    if (index === 1) {
      if (r.chipsWrap !== 'nowrap') fail(`${where}: chips flex-wrap=${r.chipsWrap} ≠ nowrap`)
      if (!r.rowAct) fail(`${where}: 移动「操作」按钮未显示`)
      if (r.rowTools !== 'none') fail(`${where}: 桌面行内工具仍在（${r.rowTools}）`)
    }
    if (index === 3) {
      if (r.gridCols !== 7) fail(`${where}: 日历列数 ${r.gridCols} ≠ 7`)
      if (r.gridW > r.gridParentW + 1) fail(`${where}: 日历网格 ${r.gridW} > 容器 ${r.gridParentW}`)
    }
  } else {
    if (r.railPos !== 'fixed' || r.railW !== '224px' || r.mainML !== '224px') {
      fail(`${where}: 桌面几何退化 rail=${r.railPos}/${r.railW} mainML=${r.mainML}`)
    }
    if (index === 1) {
      if (r.rowTools === 'none') fail(`${where}: 桌面行内工具被隐藏`)
      if (r.rowAct) fail(`${where}: 移动「操作」按钮出现在桌面`)
    }
  }
}

async function checkOffline(browser) {
  const context = await browser.newContext({ viewport: { width: 375, height: 812 } })
  const page = await context.newPage()
  const errs = []
  page.on('pageerror', (e) => errs.push(String(e)))
  page.on('console', (m) => m.type() === 'error' && errs.push(m.text()))
  await page.goto(URL, { waitUntil: 'networkidle' })
  await page.evaluate(() => navigator.serviceWorker.ready)
  await page.reload({ waitUntil: 'networkidle' })
  await page.waitForTimeout(900)
  await context.setOffline(true)
  await page.reload({ waitUntil: 'domcontentloaded' }).catch(() => {})
  await page.waitForTimeout(1400)
  const r = await page.evaluate(async () => {
    const bar = document.querySelector('.offlinebar')?.textContent ?? ''
    let fetchResult = null
    try {
      const res = await fetch('./api/digests')
      fetchResult = {
        status: res.status,
        hit: res.headers.get('X-Yechao-Cache'),
        at: res.headers.get('X-Yechao-Cached-At'),
      }
    } catch {
      fetchResult = { error: true }
    }
    return {
      rail: !!document.querySelector('.rail'),
      bar,
      fetch: fetchResult,
    }
  })
  await context.setOffline(false)
  if (!r.rail) fail('离线: 应用壳没有从缓存加载')
  if (!/缓存于 \d{2}:\d{2}/.test(r.bar)) fail(`离线: 横幅 "${r.bar}" 没有缓存时间`)
  if (r.fetch?.hit !== 'hit' || !r.fetch?.at) fail(`离线: API 未命中缓存 ${JSON.stringify(r.fetch)}`)
  const expected = errs.filter(
    (e) => !e.includes('ERR_INTERNET_DISCONNECTED') && !e.includes('status of 503'),
  )
  for (const e of expected) fail(`离线: 非预期控制台错误 ${e}`)
  await context.close()
}

for (const name of browserNames) {
  const launcher = launchers[name]
  if (!launcher) {
    console.log(`[skip] 未知浏览器 ${name}`)
    skipped += 1
    continue
  }
  let browser
  try {
    browser = await launcher.launch()
  } catch (e) {
    console.log(`[skip] ${name}: ${String(e.message).split('\n')[0]}`)
    skipped += 1
    continue
  }
  console.log(`\n== ${name} ==`)

  if (offlineMode) {
    await checkOffline(browser)
    await browser.close()
    continue
  }

  for (const width of widths) {
    const page = await browser.newPage({
      viewport: { width, height: width < 768 ? 812 : 900 },
    })
    const errs = []
    page.on('pageerror', (e) => errs.push(String(e)))
    page.on('console', (m) => m.type() === 'error' && errs.push(m.text()))
    await page.goto(URL, { waitUntil: 'networkidle' })
    await page.waitForTimeout(600)
    if (negative) {
      await page.addStyleTag({ content: 'body { min-width: 1200px !important; }' })
    }
    for (let i = 0; i < PAGES.length; i++) await inspect(page, width, i)
    for (const e of errs) fail(`${width}px: 控制台错误 ${e}`)
    await page.close()
  }
  await browser.close()
}

console.log(
  `\n检查完成：${browserNames.join(', ')} × ${offlineMode ? '离线' : widths.join('/') + 'px'}`,
)
if (violations.length) {
  console.log(`失败：${violations.length} 条违规`)
  process.exitCode = 1
} else if (skipped > 0) {
  console.log(`未完成：${skipped} 个浏览器被跳过（未验证），不能当作通过`)
  process.exitCode = 1
} else {
  console.log('通过：0 条违规')
}
