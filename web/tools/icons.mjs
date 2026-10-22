// 生成 PWA 图标。只用仓库已有的 playwright，不引入图像库。
//
// 为什么用浏览器渲染 SVG：图标源是 SVG，而安装器与 iOS 都要 PNG。
// Chromium 已经在 devDependencies 里（tools/ 下的验收脚本都用它），
// 用它把同一份 SVG 渲染成各尺寸，比再加 sharp / resvg 之类的依赖小。
import { chromium } from 'playwright'
import { mkdir, readFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const root = join(here, '..')
const outDir = join(root, 'public', 'icons')

const anySvg = await readFile(join(root, 'public', 'icon-source.svg'), 'utf8')
const maskableSvg = await readFile(join(root, 'public', 'icon-maskable.svg'), 'utf8')

const jobs = [
  { name: 'icon-192.png', size: 192, svg: anySvg },
  { name: 'icon-512.png', size: 512, svg: anySvg },
  { name: 'icon-maskable-192.png', size: 192, svg: maskableSvg },
  { name: 'icon-maskable-512.png', size: 512, svg: maskableSvg },
  // iOS 的 apple-touch-icon 固定 180，且不要透明底。
  { name: 'apple-touch-icon.png', size: 180, svg: anySvg },
]

await mkdir(outDir, { recursive: true })

const browser = await chromium.launch()
for (const job of jobs) {
  const page = await browser.newPage({
    viewport: { width: job.size, height: job.size },
    deviceScaleFactor: 1,
  })
  await page.setContent(
    `<!doctype html><meta charset="utf-8">
     <style>
       html, body { margin: 0; padding: 0; background: transparent; }
       svg { display: block; width: 100vw; height: 100vh; }
     </style>${job.svg}`,
    { waitUntil: 'load' },
  )
  await page.screenshot({ path: join(outDir, job.name), omitBackground: false })
  await page.close()
  console.log(`${job.name}: ${job.size}×${job.size}`)
}
await browser.close()
