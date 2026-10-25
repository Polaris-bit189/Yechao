/**
 * 移动端 / PWA 验收。**失败退出码非零**，不是只打印一张表。
 *
 * 用法：
 *   node tools/verify.mjs
 *   node tools/verify.mjs --widths 320,375,390,414,767,768,1440
 *   node tools/verify.mjs --browsers chromium,webkit
 *   node tools/verify.mjs --offline --widths 375 --browsers chromium
 *   NEGATIVE_CONTROL=1 node tools/verify.mjs   # 自检：注入溢出，必须红
 *   NEGATIVE_TAP=1 node tools/verify.mjs --widths 375   # 自检：主题菜单行注入 34px，必须红
 *   NEGATIVE_MENU=1 node tools/verify.mjs --widths 375  # 自检：菜单压到 44px，必须红
 *   NEGATIVE_LABEL=1 node tools/verify.mjs --widths 375 # 自检：说明文字强制折行，必须红
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
const negativeTap = process.env.NEGATIVE_TAP === '1'
const negativeMenu = process.env.NEGATIVE_MENU === '1'
const negativeLabel = process.env.NEGATIVE_LABEL === '1'

const PAGES = ['日报', '信息流', '类目', '日历', '隐私', '配置']
const EMOJI = /[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}\u{2B00}-\u{2BFF}\u{1F1E6}-\u{1F1FF}]/u

const violations = []
let skipped = 0
function fail(msg) {
  violations.push(msg)
  console.log('  ✗', msg)
}

const launchers = { chromium, webkit }

/** 打开主题菜单并测量每一行。
 *
 *  ※ 这是**假绿**的补丁：主题菜单默认关闭，`.themebox__row` 不在 DOM 里可见，
 *  即便把它放进 `taps`，`vis()` 也会因 `width === 0` 把它过滤掉 —— 断言恒绿。
 *  所以这里**先点开**，并且**必须**至少有一行可见，否则 `fail`，绝不静默跳过。
 *  触发按钮取自源码 `App.tsx`：`.rail__theme` 里那个 `button.rail__small`
 *  （`aria-expanded` 控制 `.themebox`）。
 */
async function openThemeMenu(page) {
  const trigger = page.locator('.rail__theme .rail__small')
  await trigger.click()
  await page.waitForTimeout(250)
  const info = await page.evaluate(() => {
    const vis = (el) =>
      getComputedStyle(el).display !== 'none' && el.getBoundingClientRect().width > 0
    const menu = document.querySelector('.themebox')
    const triggerEl = document.querySelector('.rail__theme .rail__small')
    const menuW = menu ? Math.round(menu.getBoundingClientRect().width) : 0
    const triggerW = triggerEl ? Math.round(triggerEl.getBoundingClientRect().width) : 0
    const rows = [...document.querySelectorAll('.themebox__row')].filter(vis).map((el) => {
      const b = el.getBoundingClientRect()
      // ※ 不能拿「标签宽度 < 2×字号」当竖排判据：主题名是单字（宵/雾/月/樱/幽），
      //   12px 宽本来就正常，这条断言会把正常布局判成 144 条假红。
      //   竖排/折行的真实几何是**多行或被旋转**：行盒数 > 1，或元素高超过行高。
      const parts = [...el.querySelectorAll('span, em')].map((node) => {
        const nb = node.getBoundingClientRect()
        const cs = getComputedStyle(node)
        return {
          tag: node.tagName.toLowerCase(),
          text: node.textContent,
          w: Math.round(nb.width),
          h: Math.round(nb.height),
          fs: parseFloat(cs.fontSize),
          lh: parseFloat(cs.lineHeight),
          whiteSpace: cs.whiteSpace,
          writingMode: cs.writingMode,
          lines: node.getClientRects().length,
        }
      })
      return {
        w: Math.round(b.width),
        h: Math.round(b.height),
        left: Math.round(b.left),
        right: Math.round(b.right),
        parts,
      }
    })
    return { menuW, triggerW, rows }
  })
  if (info.rows.length === 0) fail('主题菜单没打开')
  return { trigger, ...info }
}

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
        '.rail__link, .rail__small, .rail__help, .tabs__btn, .row__act, .row__title button, .row__map, .mside__row, .themebox__row, .monthbar__nav, .remind button, .offlinebar__go, .updatebar__go',
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

    // ※ 主题菜单：**打开后**才测（关着时不可见，不算已检查）。除了 44px 触控，
    // 还要查**布局本身** —— 只量 44 会漏掉「菜单被压成 44px 宽、字竖排」这种坏掉。
    const { trigger, menuW, triggerW, rows } = await openThemeMenu(page)
    // ※ 菜单要按内容撑开，不能缩成触发按钮的宽度。实测可用 60px，负向对照 44px；
    //   阈值取「触发按钮宽 + 12」（菜单左右内边距各 4，再留 4px 余量），
    //   不写 80 这种与内容无关的数字 —— 单字主题名下 60px 是正常可读的。
    if (menuW < triggerW + 12) {
      fail(`${where}: .themebox 宽 ${menuW} < 触发按钮 ${triggerW} + 12（菜单没按内容撑开）`)
    }
    for (const row of rows) {
      if (row.w < 44 || row.h < 44) fail(`${where}: .themebox__row ${row.w}×${row.h} < 44`)
      if (row.left < 0 || row.right > width + 1) {
        fail(`${where}: .themebox__row 超出视口 left=${row.left} right=${row.right} > ${width}`)
      }
      for (const part of row.parts) {
        const label = `${where}: 主题 ${part.tag}「${part.text}」`
        if (part.w <= 0 || part.h <= 0) fail(`${label} 不可见 ${part.w}×${part.h}`)
        if (part.whiteSpace !== 'nowrap') {
          fail(`${label} white-space=${part.whiteSpace} ≠ nowrap（长主题名会被折行）`)
        }
        if (part.writingMode !== 'horizontal-tb') {
          fail(`${label} writing-mode=${part.writingMode} ≠ horizontal-tb（竖排）`)
        }
        // 行盒数 > 1 就是折行；高度再兜一层，防止别的布局把文字拉成两行。
        const lh = Number.isFinite(part.lh) && part.lh > 0 ? part.lh : part.fs * 1.5
        if (part.lines > 1 || part.h > lh * 1.5) {
          fail(`${label} 高 ${part.h} > 1.5×行高 ${lh}（被折行/竖排）`)
        }
      }
    }
    // 测完关掉，避免浮层遮挡造成后面的误报。再点一次触发按钮即关（源码是 toggle）。
    await trigger.click()
    await page.waitForTimeout(200)
    const stillOpen = await page.evaluate(() =>
      [...document.querySelectorAll('.themebox__row')].some(
        (el) => getComputedStyle(el).display !== 'none' && el.getBoundingClientRect().width > 0,
      ),
    )
    if (stillOpen) fail(`${where}: 主题菜单未关闭（会遮挡后续检查）`)
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
    if (negativeTap) {
      await page.addStyleTag({
        content:
          '.themebox__row { width:34px !important; min-width:34px !important; max-width:34px !important; }',
      })
    }
    if (negativeMenu) {
      await page.addStyleTag({
        content:
          '.themebox { width:44px !important; min-width:44px !important; max-width:44px !important; }',
      })
    }
    if (negativeLabel) {
      // 反向注入：说明文字允许换行且只剩一个字宽，必须触发「行盒数 > 1」。
      // 若这条注入仍然全绿，说明单行判据是恒真的，不能当作已验证。
      await page.addStyleTag({
        content:
          '.themebox__row em { display:block !important; width:10px !important; white-space:normal !important; }',
      })
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
