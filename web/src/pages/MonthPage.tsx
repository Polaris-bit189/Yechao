import { useEffect, useMemo, useState } from 'react'
import { getCalendar, type CalendarPage, type Conflict, type Item } from '../api'
import { midnight, today, ymd } from '../lib/time'
import { shortDateOf } from '../lib/fmt'
import { kindColor } from '../lib/theme'
import { IcWarn } from '../ui/icons'
import './MonthPage.css'

const WEEKDAYS = ['一', '二', '三', '四', '五', '六', '日']

/**
 * 日历 —— 按截止日排。
 *
 * 三件事各说各的，不能合并：
 *   · 活动撞车：同一天两个活动，要去哪个得选
 *   · 课表读不出来：不代表没有课冲突
 *   · 这一天没有条目：真的没有
 * 它们要用户做的事完全不同，所以三句话三个位置。
 */
export function MonthPage() {
  const [data, setData] = useState<CalendarPage | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [month, setMonth] = useState(() => firstOfMonth(new Date()))
  const [picked, setPicked] = useState(today)

  useEffect(() => {
    getCalendar()
      .then(setData)
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : '读取失败'))
  }, [])

  /** 截止日 → 当天的条目。没有核验过的截止日不进日历格。 */
  const byDay = useMemo(() => {
    const m = new Map<string, Item[]>()
    for (const it of data?.items ?? []) {
      if (it.deadline_ts === null) continue
      const k = ymd(it.deadline_ts)
      const arr = m.get(k)
      if (arr) arr.push(it)
      else m.set(k, [it])
    }
    for (const arr of m.values()) arr.sort((a, b) => (a.deadline_ts ?? 0) - (b.deadline_ts ?? 0))
    return m
  }, [data])

  /** 被折叠的条目 → 代表它的那条。折叠的仍在 items 里，只是收起来。 */
  const folded = useMemo(() => {
    const m = new Map<number, number>()
    for (const [child, rep] of Object.entries(data?.duplicates ?? {})) {
      const a = Number(child)
      const b = Number(rep)
      if (Number.isInteger(a) && Number.isInteger(b) && a !== b) m.set(a, b)
    }
    return m
  }, [data])

  const titleOf = useMemo(() => {
    const m = new Map<number, string>()
    for (const it of data?.items ?? []) m.set(it.item_id, it.title)
    return m
  }, [data])

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

  if (!data) return <p className="quiet">读取日历…</p>

  const cells = buildCells(month)
  const todays = byDay.get(picked) ?? []
  const now = today()

  function shift(delta: number) {
    const next = new Date(month.getFullYear(), month.getMonth() + delta, 1)
    setMonth(next)
    const cur = new Date(midnight(picked) * 1000)
    if (cur.getFullYear() !== next.getFullYear() || cur.getMonth() !== next.getMonth()) {
      setPicked(ymd(Math.floor(next.getTime() / 1000)))
    }
  }

  return (
    <div className="wrap">
      <header className="phead">
        <div>
          <h1 className="phead__title">日历</h1>
          <div className="phead__tag">calendar</div>
        </div>
        <div className="phead__side">按截止日排列</div>
      </header>

      <Clashes list={data.conflicts} />
      <CourseState rows={data.course_conflicts} />

      <div className="monthbar">
        <button type="button" className="monthbar__nav" aria-label="上个月" onClick={() => shift(-1)}>
          ‹
        </button>
        <h2 className="monthbar__title">
          {month.getFullYear()}年{month.getMonth() + 1}月
        </h2>
        <button type="button" className="monthbar__nav" aria-label="下个月" onClick={() => shift(1)}>
          ›
        </button>
      </div>

      <div className="grid">
        {WEEKDAYS.map((w) => (
          <div key={w} className="grid__wd">
            {w}
          </div>
        ))}
        {cells.map((c, i) => {
          const has = c.key ? byDay.has(c.key) : false
          // 已过期的日子，点画淡 —— 不是隐藏，只是别再引人注意。
          const past = c.key ? c.key < now : false
          return (
            <button
              key={i}
              type="button"
              disabled={!c.key}
              className={[
                'grid__cell',
                c.key === picked ? 'is-picked' : '',
                c.key === now ? 'is-today' : '',
                past ? 'is-past' : '',
              ]
                .filter(Boolean)
                .join(' ')}
              onClick={() => c.key && setPicked(c.key)}
            >
              <span className="grid__num">{c.day || ''}</span>
              {has && <span className="grid__dot" />}
            </button>
          )
        })}
      </div>

      <section className="due">
        <h2 className="seclabel">{picked} 到期</h2>

        {todays.length === 0 ? (
          <div className="due__empty">
            <p>这一天没有到期的条目。最近的几天：</p>
            <Nearby
              byDay={byDay}
              from={picked > now ? picked : now}
              onJump={(k) => {
                setPicked(k)
                setMonth(firstOfMonth(new Date(midnight(k) * 1000)))
              }}
            />
          </div>
        ) : (
          <ul className="due__list">
            {todays.map((it) => {
              const rep = folded.get(it.item_id)
              return (
                <li key={it.item_id} className="due__row">
                  <span className="due__bar" style={{ background: kindColor(it.kind) }} />
                  <span className="due__name">{it.title}</span>
                  {rep !== undefined && (
                    <span className="due__note">
                      与「{titleOf.get(rep) ?? `条目 ${rep}`}」是同一件事
                    </span>
                  )}
                </li>
              )
            })}
          </ul>
        )}
      </section>
    </div>
  )
}

/**
 * 撞车。只给开始时刻，不给「重叠了多少分钟」——
 * 后者按「活动通常持续 90 分钟」推的，不是从消息里读到的。
 */
function Clashes({ list }: { list: Conflict[] }) {
  const [open, setOpen] = useState(false)
  if (list.length === 0) return null

  const shown = open ? list : list.slice(0, 2)

  return (
    <section className="clash">
      <div className="clash__top">
        <IcWarn />
        <span>{list.length} 个活动时间撞车（要选一个去）</span>
        <button type="button" className="clash__toggle" onClick={() => setOpen((v) => !v)}>
          {open ? '收起' : '展开'}
        </button>
      </div>
      <ul className="clash__list">
        {shown.map((c, i) => (
          <li key={i}>
            {shortDateOf(c.a.start)} {c.a.label}
            <span className="clash__mid">与</span>
            {shortDateOf(c.b.start)} {c.b.label}
          </li>
        ))}
        {!open && list.length > shown.length && (
          <li className="clash__more">还有 {list.length - shown.length} 处</li>
        )}
      </ul>
    </section>
  )
}

/**
 * 课表状态。
 *
 * 读不出来时后端给的是 `[{error}]` 而不是空数组 —— 因为这两件事在界面上
 * 必须能分辨。所以这里也分成两块，而且文案上明写「不代表没有冲突」。
 */
function CourseState({ rows }: { rows: CalendarPage['course_conflicts'] }) {
  if (rows.length === 0) {
    return <p className="course course--ok">课表已读，没有课冲突。</p>
  }

  const head = rows[0]
  if ('error' in head) {
    return (
      <div className="course course--warn">
        <IcWarn />
        <div>
          <p>{head.error}</p>
          <p className="course__sub">这不等于没有课冲突 —— 课表本身没有被读到。</p>
        </div>
      </div>
    )
  }

  return (
    <div className="course course--warn">
      <IcWarn />
      <div>
        <p>{rows.length} 处与课冲突</p>
        <ul className="course__list">
          {rows.map((r, i) => {
            if ('error' in r) return null
            return (
              <li key={i}>
                {shortDateOf(r.a.start)} {r.a.label} 与 {r.b.label} 冲突
              </li>
            )
          })}
        </ul>
      </div>
    </div>
  )
}

/** 最近有事项的四天。按日期去重，不按条数 —— 同一天的几条不是四天。 */
function Nearby({
  byDay,
  from,
  onJump,
}: {
  byDay: Map<string, Item[]>
  from: string
  onJump: (k: string) => void
}) {
  const keys = [...byDay.keys()].filter((k) => k > from).sort()
  if (keys.length === 0) return <p className="due__tail">这天之后没有带截止日的条目。</p>

  return (
    <ul className="near">
      {keys.slice(0, 4).map((k) => {
        const rows = byDay.get(k) ?? []
        return (
          <li key={k}>
            <button type="button" className="near__row" onClick={() => onJump(k)}>
              <span className="near__day">{k}</span>
              <span className="near__n">{rows.length} 条</span>
              <span className="near__names">{rows.map((r) => r.title).join('、')}</span>
            </button>
          </li>
        )
      })}
    </ul>
  )
}

function firstOfMonth(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), 1)
}

/**
 * 一个月占的格子。周一开头，前后不属于本月的格子留空。
 *
 * 不借上/下月的日期 —— 点一个「上个月的 28 号」会跳到看不见的地方。
 */
function buildCells(cursor: Date): Array<{ key: string | null; day: number }> {
  const y = cursor.getFullYear()
  const m = cursor.getMonth()
  const lead = (new Date(y, m, 1).getDay() + 6) % 7
  const total = new Date(y, m + 1, 0).getDate()

  const out: Array<{ key: string | null; day: number }> = []
  for (let i = 0; i < lead; i++) out.push({ key: null, day: 0 })
  for (let d = 1; d <= total; d++) {
    out.push({ key: ymd(Math.floor(new Date(y, m, d).getTime() / 1000)), day: d })
  }
  while (out.length % 7 !== 0) out.push({ key: null, day: 0 })
  return out
}
