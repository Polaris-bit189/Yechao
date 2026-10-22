// 构建末尾生成 dist/sw.js。
//
// 为什么不用 Workbox / vite-plugin-pwa：这个站点只有一页、一份 CSS、一份 JS，
// 预缓存清单就是「dist 里有什么」。用几十行把清单读出来写进模板，比再加一层
// 运行时依赖更容易解释，也更容易在评审时复现。
import { createHash } from 'node:crypto'
import { readdir, readFile, stat, writeFile } from 'node:fs/promises'
import { dirname, join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))

const REQUIRED = [
  'index.html',
  'manifest.webmanifest',
  'favicon.svg',
  'icons/icon-192.png',
  'icons/icon-512.png',
  'icons/icon-maskable-192.png',
  'icons/icon-maskable-512.png',
  'icons/apple-touch-icon.png',
]

async function walk(dir) {
  const out = []
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const p = join(dir, entry.name)
    if (entry.isDirectory()) out.push(...(await walk(p)))
    else out.push(p)
  }
  return out
}

export default function yechaoPwa() {
  return {
    name: 'yechao-pwa',
    apply: 'build',
    async closeBundle() {
      const dist = join(process.cwd(), 'dist')
      const files = (await walk(dist))
        .map((p) => relative(dist, p).replaceAll('\\', '/'))
        .filter((p) => p !== 'sw.js')
        .sort()

      for (const need of REQUIRED) {
        if (!files.includes(need)) {
          throw new Error(`PWA 预缓存缺少 ${need}：先确认 web/public 与构建产物`)
        }
      }

      // 版本 = 文件名 + 大小。带哈希的 JS/CSS 变了名字就变；index.html 与
      // manifest 的内容变化会改变大小。够用，且不需要读全部文件内容。
      const fingerprint = []
      for (const f of files) {
        const s = await stat(join(dist, f))
        fingerprint.push(`${f}:${s.size}`)
      }
      const version = createHash('sha256').update(fingerprint.join('\n')).digest('hex').slice(0, 12)

      const precache = ['./', ...files.map((f) => `./${f}`)]
      const template = await readFile(join(here, 'sw-template.js'), 'utf8')
      const sw = template
        .replaceAll('__VERSION__', version)
        .replaceAll('__PRECACHE__', JSON.stringify(precache, null, 2))

      await writeFile(join(dist, 'sw.js'), sw)
      console.log(`pwa: dist/sw.js ${version} (${precache.length} files)`)
    },
  }
}
