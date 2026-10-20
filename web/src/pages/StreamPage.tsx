import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  dropItem,
  getCategories,
  getItems,
  getPersons,
  patchKind,
  type CategoryDetail,
  type Item,
  type ItemPage,
  type Person,
} from '../api'
import { ago, dueInfo, groupLabel, hmText, mdhmText, ymd } from '../lib/time'
import { kindColor, kindIcon } from '../lib/theme'
import { IcClock, IcMap, IcPin, IcSearch } from '../ui/icons'
import './StreamPage.css'

const STEP = 50

/**
 * 信息流 —— 全部条目的时间线。
 *
 * 这是「翻查」用的页面，密度优先：一屏要能看到五条以上。所以每条只占
 * 两三行，元信息横排一行，详情不折行成段。
 *
 * 三处数量说法分开：读取中说「读取中」，没拿到说「条目数未知」，
 * 拿到了才说「共 N 条」。0 只配在确认读到空结果时用。
 */
export function StreamPage({ onChanged }: { onChanged: () => void }) {
  const [cats, setCats] = useState<CategoryDetail[]>([])
  const [people, setPeople] = useState<Person[]>([])
  const [kinds, setKinds] = useState<string[]>([])
  const [persons, setPersons] = useState<number[]>([])
  const [typing, setTyping] = useState('')
  const [q, setQ] = useState('')
  const [peopleOpen, setPeopleOpen] = useState(false)
  const [page, setPage] = useState<ItemPage | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [sheet, setSheet] = useState<Item | null>(null)

  // 停下再请求。每敲一下都发的话，输入会一顿一顿。
  useEffect(() => {
    const id = setTimeout(() => setQ(typing.trim()), 260)
    return () => clearTimeout(id)
  }, [typing])

  useEffect(() => {
    getCategories()
      .then((r) => setCats(r.categories))
      .catch(() => setCats([]))
    getPersons()
      .then((r) => setPeople(r.persons))
      .catch(() => setPeople([]))
  }, [])

  const load = useCallback(async () => {
    setBusy(true)
    setErr(null)
    try {
      const r = await getItems({
        kinds: kinds.length ? kinds : undefined,
        persons: persons.length ? persons : undefined,
        q: q || undefined,
        limit: STEP,
      })
      setPage(r)
    } catch (e) {
      // 失败时置空，不让上一轮结果冒充这一轮。
      setPage(null)
      setErr(e instanceof Error ? e.message : '读取失败')
    } finally {
      setBusy(false)
    }
  }, [kinds, persons, q])

  useEffect(() => {
    void load()
  }, [load])

  async function more() {
    if (!page) return
    setBusy(true)
    try {
      const r = await getItems({
        kinds: kinds.length ? kinds : undefined,
        persons: persons.length ? persons : undefined,
        q: q || undefined,
        limit: STEP,
        offset: page.items.length,
      })
      // 两次请求之间可能有新条目插进来导致分页错位，去一下重。
      const seen = new Set(page.items.map((x) => x.item_id))
      const fresh = r.items.filter((x) => !seen.has(x.item_id))
      setPage({ ...r, items: [...page.items, ...fresh] })
    } catch (e) {
      setErr(e instanceof Error ? e.message : '读取失败')
    } finally {
      setBusy(false)
    }
  }

  function toggle<T>(list: T[], v: T): T[] {
    return list.includes(v) ? list.filter((x) => x !== v) : [...list, v]
  }

  async function recolor(id: number, kind: string) {
    try {
      await patchKind(id, kind)
      await load()
      onChanged()
    } catch (e) {
      setErr(e instanceof Error ? e.message : '改类目失败')
    }
  }

  async function remove(id: number) {
    try {
      await dropItem(id)
      await load()
      onChanged()
    } catch (e) {
      setErr(e instanceof Error ? e.message : '删除失败')
    }
  }

  const days = useMemo(() => {
    if (!page) return []
    const out: Array<{ key: string; rows: Item[] }> = []
    for (const it of page.items) {
      const k = ymd(it.event_ts)
      const tail = out[out.length - 1]
      if (tail && tail.key === k) tail.rows.push(it)
      else out.push({ key: k, rows: [it] })
    }
    return out
  }, [page])

  const count = page ? `共 ${page.total} 条` : busy ? '读取中…' : '条目数未知'

  const empty = q
    ? `没有匹配「${q}」的条目。`
    : kinds.length || persons.length
      ? '当前筛选下还没有条目。'
      : '还没有任何条目。'

  return (
    <div className="wrap">
      <header className="phead">
        <div>
          <h1 className="phead__title">信息流</h1>
          <div className="phead__tag">feed</div>
        </div>
        <div className="phead__side">{count}</div>
      </header>

      <label className="find">
        <IcSearch />
        <input
          type="search"
          value={typing}
          placeholder="搜标题或详情（两个字也能搜）"
          aria-label="搜索条目"
          onChange={(e) => setTyping(e.target.value)}
        />
      </label>

      <div className="chips" role="group" aria-label="按类目筛选">
        <button
          type="button"
          className={kinds.length === 0 ? 'chip is-on' : 'chip'}
          onClick={() => setKinds([])}
        >
          全部
        </button>
        {cats.map((c) => {
          const Icon = kindIcon(c.slug)
          return (
            <button
              key={c.slug}
              type="button"
              className={kinds.includes(c.slug) ? 'chip is-on' : 'chip'}
              aria-pressed={kinds.includes(c.slug)}
              onClick={() => setKinds((cur) => toggle(cur, c.slug))}
            >
              <span style={{ color: kindColor(c.slug) }}>
                <Icon />
              </span>
              {c.label}
            </button>
          )
        })}
      </div>

      {people.length > 0 && (
        <div className="people">
          <button
            type="button"
            className={persons.length ? 'people__btn is-on' : 'people__btn'}
            aria-expanded={peopleOpen}
            onClick={() => setPeopleOpen((v) => !v)}
          >
            按人物筛选{persons.length ? `（已选 ${persons.length}）` : ''}
          </button>
          {peopleOpen && (
            <div className="people__list">
              {people.map((p) => (
                <button
                  key={p.uin}
                  type="button"
                  className={persons.includes(p.uin) ? 'chip is-on' : 'chip'}
                  aria-pressed={persons.includes(p.uin)}
                  onClick={() => setPersons((cur) => toggle(cur, p.uin))}
                >
                  {p.label}
                  <span className="chip__n">{p.count}</span>
                </button>
              ))}
            </div>
          )}
        </div>
      )}

      {err && (
        <div className="fail">
          <span>{err}</span>
          <button type="button" onClick={() => void load()}>
            重试
          </button>
        </div>
      )}

      {page && page.items.length === 0 && !busy && <p className="quiet">{empty}</p>}

      {days.map((g) => (
        <section key={g.key} className="daygroup">
          <h2 className="daygroup__label">
            <span>{groupLabel(g.key)}</span>
            {/* 组头后面一条延伸的发丝线，把这一天的条目框在一起。 */}
            <span className="daygroup__rule" />
          </h2>
          {g.rows.map((it) => (
            <Row
              key={it.item_id}
              item={it}
              kinds={cats}
              onOpen={() => setSheet(it)}
              onRecolor={(k) => void recolor(it.item_id, k)}
              onRemove={() => void remove(it.item_id)}
            />
          ))}
        </section>
      ))}

      {page && page.items.length > 0 && page.items.length < page.total && (
        <div className="more">
          <button type="button" disabled={busy} onClick={() => void more()}>
            {busy ? '读取中…' : `加载更多（还有 ${page.total - page.items.length} 条）`}
          </button>
        </div>
      )}

      {sheet && (
        <Sheet
          item={sheet}
          kinds={cats}
          onClose={() => setSheet(null)}
          onRecolor={(k) => {
            void recolor(sheet.item_id, k)
            setSheet(null)
          }}
          onRemove={() => {
            void remove(sheet.item_id)
            setSheet(null)
          }}
        />
      )}
    </div>
  )
}

/** 一条。信息横排、详情单行，密度优先。 */
function Row({
  item,
  kinds,
  onOpen,
  onRecolor,
  onRemove,
}: {
  item: Item
  kinds: CategoryDetail[]
  onOpen: () => void
  onRecolor: (kind: string) => void
  onRemove: () => void
}) {
  const due = item.deadline_ts !== null ? dueInfo(item.deadline_ts) : null
  const Icon = kindIcon(item.kind)
  const color = kindColor(item.kind)

  return (
    <article className="row">
      <span className="row__bar" style={{ background: color }} aria-hidden="true" />

      <div className="row__body">
        <div className="row__top">
          <span className="row__kind" style={{ color }}>
            <Icon />
            {item.kind_label}
          </span>
          <span className="row__dim">·</span>
          <span className="row__dim">{item.group_name}</span>
          <span className="row__time">{hmText(item.event_ts)}</span>
        </div>

        <h3 className="row__title">
          <button type="button" onClick={onOpen}>
            {item.title}
          </button>
        </h3>

        {item.detail && <p className="row__text">{item.detail}</p>}

        <div className="row__meta">
          {item.start_ts !== null && (
            <span>
              <IcClock />
              {mdhmText(item.start_ts)}
            </span>
          )}
          {due && (
            <span className={`row__due row__due--${due.level}`}>
              {due.level === 'expired' ? due.text : `${mdTextShort(item.deadline_ts!)} 截止`}
            </span>
          )}
          {item.place && (
            <span>
              <IcPin />
              {item.place}
            </span>
          )}
          {item.actor && <span>@{item.actor}</span>}
          {item.source_count > 0 && <span>{item.source_count} 条源消息</span>}
        </div>

        {item.place && (
          <a
            className="row__map"
            href={`https://www.openstreetmap.org/search?query=${encodeURIComponent(item.place)}`}
            target="_blank"
            rel="noreferrer noopener"
          >
            <IcMap />
            在地图中查看
          </a>
        )}
      </div>

      <div className="row__tools">
        <select aria-label="改类目" value={item.kind} onChange={(e) => onRecolor(e.target.value)}>
          {kinds.map((k) => (
            <option key={k.slug} value={k.slug}>
              {k.label}
            </option>
          ))}
        </select>
        <button type="button" onClick={onRemove}>
          删除
        </button>
      </div>
    </article>
  )
}

function mdTextShort(ts: number): string {
  const d = new Date(ts * 1000)
  return `${d.getMonth() + 1}月${d.getDate()}日`
}

/** 详情抽屉。翻列表时点开一条，看完就回去接着翻。 */
function Sheet({
  item,
  kinds,
  onClose,
  onRecolor,
  onRemove,
}: {
  item: Item
  kinds: CategoryDetail[]
  onClose: () => void
  onRecolor: (kind: string) => void
  onRemove: () => void
}) {
  const box = useRef<HTMLDivElement>(null)

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    box.current?.focus()
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="sheetbg" onClick={onClose}>
      <aside
        ref={box}
        className="sheet"
        role="dialog"
        aria-label={item.title}
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="sheet__top">
          <span style={{ color: kindColor(item.kind) }}>{item.kind_label}</span>
          <button type="button" aria-label="关闭" onClick={onClose}>
            ✕
          </button>
        </div>

        <h2 className="sheet__title">{item.title}</h2>
        {item.detail && <p className="sheet__text">{item.detail}</p>}

        <dl className="sheet__facts">
          <dt>群</dt>
          <dd>{item.group_name}</dd>
          <dt>发出</dt>
          <dd>{ago(item.event_ts)}</dd>
          {item.place && (
            <>
              <dt>地点</dt>
              <dd>{item.place}</dd>
            </>
          )}
          {item.amount && (
            <>
              <dt>金额</dt>
              <dd>{item.amount}</dd>
            </>
          )}
          {item.start_ts !== null && (
            <>
              <dt>开始</dt>
              <dd>{mdhmText(item.start_ts)}</dd>
            </>
          )}
          {item.deadline_ts !== null && (
            <>
              <dt>截止</dt>
              <dd>{mdhmText(item.deadline_ts)}</dd>
            </>
          )}
          <dt>来源</dt>
          <dd>{item.source_count} 条消息</dd>
        </dl>

        <div className="sheet__acts">
          <select value={item.kind} onChange={(e) => onRecolor(e.target.value)}>
            {kinds.map((k) => (
              <option key={k.slug} value={k.slug}>
                {k.label}
              </option>
            ))}
          </select>
          <button type="button" className="sheet__drop" onClick={onRemove}>
            删除
          </button>
        </div>
      </aside>
    </div>
  )
}
