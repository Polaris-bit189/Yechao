/**
 * PWA 静态检查：manifest、图标、index.html、生成的 sw.js。
 *
 * 不连浏览器、不联网；跑在 `npm run build` 之后。
 * 失败退出码非零。
 */
import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const web = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const dist = join(web, 'dist')
const errors = []
const ok = (msg) => console.log('  ✓', msg)
const bad = (msg) => {
  errors.push(msg)
  console.log('  ✗', msg)
}

function readJson(path) {
  return JSON.parse(readFileSync(path, 'utf8'))
}

function pngSize(path) {
  const b = readFileSync(path)
  const sig = b.subarray(0, 8).toString('hex')
  if (sig !== '89504e470d0a1a0a') throw new Error(`不是 PNG：${path}`)
  return { width: b.readUInt32BE(16), height: b.readUInt32BE(20) }
}

if (!existsSync(join(dist, 'index.html'))) {
  console.error('dist 不存在：先 cd web && npm run build')
  process.exit(1)
}

console.log('manifest')
const manifestPath = join(dist, 'manifest.webmanifest')
if (!existsSync(manifestPath)) bad('dist/manifest.webmanifest 不存在')
else {
  const m = readJson(manifestPath)
  const fields = {
    name: '夜抄',
    short_name: '夜抄',
    lang: 'zh-CN',
    start_url: './',
    scope: './',
    display: 'standalone',
  }
  for (const [k, v] of Object.entries(fields)) {
    if (m[k] !== v) bad(`manifest.${k}=${JSON.stringify(m[k])}，期望 ${JSON.stringify(v)}`)
  }
  if (!m.description) bad('manifest.description 为空')
  if (!Array.isArray(m.icons) || m.icons.length < 4) bad('manifest.icons 少于 4 个')

  const want = [
    ['192x192', 'any'],
    ['512x512', 'any'],
    ['192x192', 'maskable'],
    ['512x512', 'maskable'],
  ]
  for (const [size, purpose] of want) {
    const hit = (m.icons ?? []).find(
      (i) => i.sizes === size && String(i.purpose ?? 'any').includes(purpose),
    )
    if (!hit) {
      bad(`manifest 缺图标 ${size} ${purpose}`)
      continue
    }
    const path = join(dist, hit.src.replace(/^\.\//, ''))
    if (!existsSync(path)) {
      bad(`图标不存在 ${hit.src}`)
      continue
    }
    const dim = pngSize(path)
    const [w, h] = size.split('x').map(Number)
    if (dim.width !== w || dim.height !== h) {
      bad(`${hit.src} 尺寸 ${dim.width}×${dim.height}，期望 ${size}`)
    } else {
      ok(`${size} ${purpose}`)
    }
  }
  const apple = join(dist, 'icons', 'apple-touch-icon.png')
  if (!existsSync(apple)) bad('缺 apple-touch-icon.png')
  else {
    const dim = pngSize(apple)
    if (dim.width !== 180 || dim.height !== 180) bad(`apple-touch-icon ${dim.width}×${dim.height}`)
    else ok('apple-touch-icon 180×180')
  }
}

console.log('index.html')
const html = readFileSync(join(dist, 'index.html'), 'utf8')
for (const [needle, label] of [
  ['viewport-fit=cover', 'viewport-fit=cover'],
  ['rel="manifest"', 'manifest link'],
  ['apple-touch-icon', 'apple-touch-icon link'],
  ['id="theme-color"', 'theme-color id'],
]) {
  if (html.includes(needle)) ok(label)
  else bad(`index.html 缺 ${label}`)
}
if (!html.includes('./manifest.webmanifest')) bad('manifest href 不是相对路径 ./manifest.webmanifest')

console.log('dist/sw.js')
const swPath = join(dist, 'sw.js')
if (!existsSync(swPath)) {
  bad('dist/sw.js 不存在：检查 vite 插件是否运行')
} else {
  const sw = readFileSync(swPath, 'utf8')
  if (sw.includes('__VERSION__') || sw.includes('__PRECACHE__')) bad('sw.js 仍有未替换占位符')
  const version = /const VERSION = '([^']+)'/.exec(sw)?.[1]
  if (!version || version.length < 6) bad('sw.js 版本号异常')
  else ok(`版本 ${version}`)

  const precacheRaw = /const PRECACHE = (\[[\s\S]*?\])\nconst SHELL_CACHE/.exec(sw)?.[1]
  let precache = []
  try {
    precache = JSON.parse(precacheRaw)
  } catch {
    bad('sw.js 的 PRECACHE 不是合法 JSON')
  }
  if (precache.length < 10) bad(`预缓存只有 ${precache.length} 条`)
  for (const entry of precache) {
    const rel = entry.replace(/^\.\//, '')
    if (rel === '') continue
    if (!existsSync(join(dist, rel))) bad(`预缓存条目在 dist 不存在：${entry}`)
  }
  const assets = existsSync(join(dist, 'assets')) ? readdirSync(join(dist, 'assets')) : []
  for (const a of assets) {
    if (!precache.includes(`./assets/${a}`)) bad(`assets/${a} 没进预缓存`)
  }
  if (sw.includes("request.method !== 'GET'")) ok('非 GET 守卫')
  else bad('sw.js 缺非 GET 守卫')
  if (sw.includes('yechao-api-v1') && sw.includes('X-Yechao-Cached-At')) ok('API 缓存与时间头')
  else bad('sw.js 缺 API 缓存或缓存时间头')
}

if (errors.length) {
  console.error(`\nPWA 检查失败：${errors.length} 条`)
  process.exitCode = 1
} else {
  console.log('\nPWA 检查通过')
}
