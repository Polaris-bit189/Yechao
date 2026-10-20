import { useEffect, useState } from 'react'
import { getCategories, getItems, type CategoryDetail, type Item } from '../api'
import { shortDateOf } from '../lib/fmt'
import { kindColor, kindIcon } from '../lib/theme'
import './KindsPage.css'

/** 每类拉 12 条，按标题去重后留 3 条做预览。 */
const PULL = 12
const SHOW = 3

/**
 * 类目 —— 七个入口。
 *
 * 关键在每行下面那三条预览：只给「74 条」回答不了「里面到底是什么」，
 * 而这个页面存在的意义就是回答那个问题。
 */
export function KindsPage() {
  const [cats, setCats] = useState<CategoryDetail[] | null>(null)
  const [previews, setPreviews] = useState<Map<string, Item[] | 'fail'>>(new Map())
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    getCategories()
      .then(async (r) => {
        setCats(r.categories)
        // ⚠️ 显式标成 `readonly [string, Item[] | 'fail']` —— 不标的话
        // TS 会把两条分支推成联合元组，`new Map()` 无从收窄。
        const entries: Array<readonly [string, Item[] | 'fail']> = await Promise.all(
          r.categories.map(
            (c): Promise<readonly [string, Item[] | 'fail']> =>
              getItems({ kinds: [c.slug], limit: PULL })
                .then((p) => [c.slug, p.items] as const)
                .catch(() => [c.slug, 'fail'] as const),
          ),
        )
        setPreviews(new Map(entries))
      })
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : '读取失败'))
  }, [])

  if (err) {
    return (
      <div className="fail">
        <span>{err}</span>
        <button type="button" onClick={() => location.reload()}>
          重试
        </button>
      </div>
    )
  }

  if (!cats) return <p className="quiet">读取类目…</p>

  return (
    <div className="wrap">
      <header className="phead">
        <div>
          <h1 className="phead__title">类目</h1>
          <div className="phead__tag">categories</div>
        </div>
        <div className="phead__side">{cats.length} 个类目</div>
      </header>

      <p className="kinds__hint">点一个类目，看该类全部条目</p>

      <div className="kinds">
        {cats.map((c) => {
          const Icon = kindIcon(c.slug)
          const color = kindColor(c.slug)
          const rows = previews.get(c.slug)

          return (
            <section key={c.slug} className="kinds__row" style={{ ['--kc' as string]: color }}>
              <div className="kinds__head">
                <span className="kinds__icon">
                  <Icon size={14} />
                </span>
                <span className="kinds__label">{c.label}</span>
                <span className="kinds__n">{c.count} 条</span>
              </div>

              {/* 三种情况分开：读不到 / 这一类本来就没有 / 有内容。
                  混成一句会把一次读取失败说成一次空结果。 */}
              {c.count === 0 ? (
                <p className="kinds__none">这一类还没有条目</p>
              ) : rows === undefined ? (
                <p className="kinds__none">读取这一类…</p>
              ) : rows === 'fail' ? (
                <p className="kinds__none">读不到这一类的条目</p>
              ) : (
                <ul className="kinds__peek">
                  {dedupe(rows).map((it) => (
                    <li key={it.item_id}>
                      <span className="kinds__peek-title">{it.title}</span>
                      {it.deadline_ts !== null && (
                        <span className="kinds__peek-due">
                          {shortDateOf(it.deadline_ts)} 截止
                        </span>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </section>
          )
        })}
      </div>
    </div>
  )
}

/** 标题相同的只留第一条 —— 同一件事被转发几次，预览里连排三行一样没有意义。 */
function dedupe(rows: Item[]): Item[] {
  const seen = new Set<string>()
  const out: Item[] = []
  for (const it of rows) {
    if (seen.has(it.title)) continue
    seen.add(it.title)
    out.push(it)
    if (out.length === SHOW) break
  }
  return out
}
