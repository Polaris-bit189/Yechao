import { useEffect, useMemo, useState } from 'react'
import { getCalendar, type CalendarPage, type Conflict, type Item } from '../api'
import { midnight, today, ymd } from '../lib/time'
import { shortDateOf } from '../lib/fmt'
import { kindColor } from '../lib/theme'
import { IcWarn } from '../ui/icons'
import './MonthPage.css'

const WEEKDAYS = ['一', '二', '三', '四', '五', '六', '日']

/**
 * 日历 —— 按截止日排列。
 *
 * 三件事各说各的，不并成一句：
 *   · 活动撞车：一天里两个活动，去哪个得自己挑
 *   · 课表没读出来：不等于就没有课冲突
 *   · 这天没有条目：那是真没有
 * 它们要用户采取的行动互不相干，所以分成三处、三种说法。
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

  /** 截止日 → 当天条目。没经过核验的截止日，不往日历格里放。 */
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

  /** 被折叠的条目 → 替它露面的那条。折叠的条目依旧在 items 里，只是被收了起来。 */
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

  /**
   * 当月里**带截止日**的那些天，日期从早到晚。
   *
   * 只摆有事的几天 —— 30 天全列出来，这一栏就跟第二份日历没两样，
   * 「哪几天有事」反倒看不出来了。
   *
   * ※ 这个 hook 得待在上面那句 `if (err) return` 前头：挪到后面就变成
   * 有条件的 hook 调用，React 会在「出错」和「没出错」两次渲染之间抛
   * 「Rendered more hooks than during the previous render」。
   */
  const monthDays = useMemo(() => {
    const head = `${month.getFullYear()}-${String(month.getMonth() + 1).padStart(2, '0')}`
    return [...byDay.entries()]
      .filter(([k]) => k.startsWith(head))
      .sort((a, b) => (a[0] < b[0] ? -1 : 1))
  }, [byDay, month])

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

  /** 点右侧清单里的一天：选中，并把日历翻到它所在的月份。 */
  function jump(key: string) {
    setPicked(key)
    setMonth(firstOfMonth(new Date(midnight(key) * 1000)))
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

      {/* 日历和「当月清单」并排摆。
          日历管的是「哪几天有事」，清单管的是「到底是什么事」——
          放在同一屏，用户就不必一格一格点开去找。 */}
      <div className="monthwrap">
        <div className="monthwrap__cal">
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
              // 不属于本月的格子渲染成 `div`，而不是 disabled 的 button：
              // 按钮一旦 disabled 会多一层 opacity，在深色底上比留白还扎眼，
              // 活像一只「空盒子」。这里要的效果是它彻底不见。
              if (c.key === null) return <div key={i} className="grid__cell is-void" />
              // 先收进 const：`c.key` 是会变的属性，窄化传不进下面那个闭包。
              const key = c.key
              const has = byDay.has(key)
              // 已经过去的日子，点画得淡一些 —— 不是藏起来，只是别再抢注意力。
              const past = key < now
              return (
                <button
                  key={i}
                  type="button"
                  className={[
                    'grid__cell',
                    key === picked ? 'is-picked' : '',
                    key === now ? 'is-today' : '',
                    past ? 'is-past' : '',
                  ]
                    .filter(Boolean)
                    .join(' ')}
                  onClick={() => setPicked(key)}
                >
                  <span className="grid__num">{c.day}</span>
                  {has && <span className="grid__dot" />}
                </button>
              )
            })}
          </div>
        </div>

        <section className="monthwrap__day due">
          <h2 className="seclabel">{picked} 到期</h2>

          {todays.length === 0 ? (
            <div className="due__empty">
              <p>这天没有要到期的东西。换一个近些的日期看看：</p>
              <Nearby byDay={byDay} from={picked > now ? picked : now} onJump={jump} />
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

        <aside className="monthwrap__side">
          <h2 className="seclabel">本月有事项的日子</h2>

          {monthDays.length === 0 ? (
            <p className="mside__none">本月没有条目挂着截止日。</p>
          ) : (
            <ul className="mside">
              {monthDays.map(([k, rows]) => (
                <li key={k}>
                  <button
                    type="button"
                    className={k === picked ? 'mside__row is-on' : 'mside__row'}
                    onClick={() => jump(k)}
                  >
                    <span className="mside__d">{k}</span>
                    <span className="mside__wd">{weekdayShort(k)}</span>
                    <span className="mside__n">{rows.length} 条</span>
                  </button>
                  <ul className="mside__items">
                    {rows.map((it) => (
                      <li key={it.item_id}>
                        <span className="mside__bar" style={{ background: kindColor(it.kind) }} />
                        <span className="mside__t">{it.title}</span>
                      </li>
                    ))}
                  </ul>
                </li>
              ))}
            </ul>
          )}
        </aside>
      </div>
    </div>
  )
}

/** `周一`。清单里挨着日期的那一个字，够用了。 */
function weekdayShort(key: string): string {
  const d = new Date(midnight(key) * 1000)
  return `周${WEEKDAYS[(d.getDay() + 6) % 7]}`
}

/**
 * 撞车。只报开始时刻，不报「重叠了多少分钟」——
 * 后者是拿「活动一般持续 90 分钟」估的，并非消息里读到的。
 */
function Clashes({ list }: { list: Conflict[] }) {
  const [open, setOpen] = useState(false)
  if (list.length === 0) return null

  const shown = open ? list : list.slice(0, 2)

  return (
    <section className="clash">
      <div className="clash__top">
        <IcWarn />
        <span>{list.length} 个活动撞了时间（得挑一个去）</span>
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
 * 没读出来时，后端给的是 `[{error}]` 而非空数组 —— 因为这两种情况在界面上
 * 必须分得清。所以这里也拆成两块，措辞上挑明「不代表没有冲突」。
 */
function CourseState({ rows }: { rows: CalendarPage['course_conflicts'] }) {
  if (rows.length === 0) {
    return <p className="course course--ok">课表读到了，没有课冲突。</p>
  }

  const head = rows[0]
  if ('error' in head) {
    return (
      <div className="course course--warn">
        <IcWarn />
        <div>
          <p>{head.error}</p>
          <p className="course__sub">这并不等于没有课冲突 —— 课表本身就没读出来。</p>
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

/** 往后最近有事项的四天。按日期去重，不看条数 —— 同一天的几条凑不成四天。 */
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
  if (keys.length === 0) return <p className="due__tail">这天往后没有条目带截止日。</p>

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
 * 一个月铺开的格子。周一起头，首尾不属于本月的格子空着。
 *
 * 不去借上/下月的日期 —— 点一下「上月的 28 号」，会跳到看不见的地方去。
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
