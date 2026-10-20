import { useEffect, useMemo, useState } from 'react'
import {
  getDigest,
  getDigests,
  getItem,
  type DigestDetail,
  type DigestSummary,
  type Item,
  type Source,
} from '../api'
import { parse, type Block } from '../lib/md'
import { ago, mdhmText, weekdayOf } from '../lib/time'
import { kindColor } from '../lib/theme'
import { IcChevron, IcPin } from '../ui/icons'
import './DayPage.css'

/**
 * 日报 —— 打开应用第一眼看到的东西。
 *
 * 三段：这一篇、它引用了哪些原始条目、更早的几篇。
 *
 * 卡片只包正文。引用条目在卡片外面 —— 它们是依据，不是正文的一部分；
 * 包进同一张卡片会让读者以为两者同属一份文档。
 *
 * 节奏上要求「内容少页面就该短」，所以不给内容区设最小高度，
 * 也不为了页面看起来饱满而加留白。
 */
export function DayPage() {
  const [list, setList] = useState<DigestSummary[] | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [picked, setPicked] = useState<number | null>(null)
  const [detail, setDetail] = useState<DigestDetail | null>(null)

  useEffect(() => {
    getDigests()
      .then((r) => {
        setList(r.digests)
        // 默认摊开最新一篇。列表本身按时间倒序，不再排序。
        if (r.digests.length > 0) setPicked(r.digests[0].digest_id)
      })
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : '读取失败'))
  }, [])

  useEffect(() => {
    if (picked === null) return
    let live = true
    setDetail(null)
    getDigest(picked)
      .then((d) => {
        if (live) setDetail(d)
      })
      .catch((e: unknown) => {
        if (live) setErr(e instanceof Error ? e.message : '读取失败')
      })
    return () => {
      live = false
    }
  }, [picked])

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

  if (!list) return <p className="quiet">读取日报…</p>

  if (list.length === 0) {
    return (
      <p className="quiet">
        还没有日报。生成方式：<code>python -m yechao digest</code>
      </p>
    )
  }

  const older = list.filter((x) => x.digest_id !== picked)

  return (
    <div className="day">
      <header className="day__head">
        <div>
          <h1 className="day__date">{detail?.day ?? list[0].day}</h1>
          <div className="day__note">
            群聊日报{detail ? ` · 提炼 ${detail.item_count} 条` : ''}
          </div>
        </div>
        <div className="day__week">
          {detail ? weekdayOf(detail.window_from) : ''}
        </div>
      </header>

      {detail ? <Paper digest={detail} /> : <p className="quiet">读取这一篇…</p>}

      {detail && detail.items.length > 0 && (
        <section className="cited">
          <h2 className="seclabel">本篇引用的 {detail.items.length} 条原始条目</h2>
          {detail.items.map((it) => (
            <Cited key={it.item_id} item={it} />
          ))}
        </section>
      )}

      {older.length > 0 && (
        <section className="past">
          <h2 className="seclabel">往期</h2>
          {older.map((x) => (
            <button
              key={x.digest_id}
              type="button"
              className="past__row"
              onClick={() => setPicked(x.digest_id)}
            >
              <span className="past__day">{x.day}</span>
              <span className="past__peek">{x.preview ?? ''}</span>
              <span className="past__n">{x.item_count} 条</span>
              <IcChevron size={12} />
            </button>
          ))}
        </section>
      )}
    </div>
  )
}

/** 正文卡片。开头的日期标题与概述段在这里去掉 —— 页头与摘要已经给过了。 */
function Paper({ digest }: { digest: DigestDetail }) {
  const blocks = useMemo(
    () => parse(digest.body_md, digest.preview),
    [digest.body_md, digest.preview],
  )

  return (
    <article className="paper">
      {blocks.map((b, i) => (
        <Piece key={i} block={b} />
      ))}
    </article>
  )
}

function Piece({ block }: { block: Block }) {
  switch (block.t) {
    case 'h2':
      return <h2 className="paper__h2">{block.text}</h2>
    case 'h3':
      return <h3 className="paper__h3">{block.text}</h3>
    case 'ul':
      return (
        <ul className="paper__ul">
          {block.items.map((t, i) => (
            <li key={i}>{t}</li>
          ))}
        </ul>
      )
    case 'quote':
      return <blockquote className="paper__quote">{block.text}</blockquote>
    default:
      return <p className="paper__p">{block.text}</p>
  }
}

/**
 * 一条被引用的原始条目。
 *
 * 右上角给「多久之前」而不是钟点：读日报时想知道的是这条新不新，
 * 不是它几点几分发的。
 *
 * ⚠️ 规格要求「**可展开看源消息**」。展开时才去拉详情 ——
 * 一页十条条目各拉一次详情会让首屏慢一拍，而大多数时候没人会点开。
 */
function Cited({ item }: { item: Item }) {
  const [open, setOpen] = useState(false)
  const [srcs, setSrcs] = useState<Source[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  async function toggle() {
    if (open) {
      setOpen(false)
      return
    }
    setOpen(true)
    if (srcs !== null || busy) return
    setBusy(true)
    setErr(null)
    try {
      const d = await getItem(item.item_id)
      setSrcs(d.sources)
    } catch (e) {
      setErr(e instanceof Error ? e.message : '读取源消息失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="cited__row">
      <div className="cited__head">
        <span className="cited__dot" style={{ background: kindColor(item.kind) }} />
        <span style={{ color: kindColor(item.kind) }}>{item.kind_label}</span>
        <span className="cited__dim">·</span>
        <span className="cited__dim">{item.group_name}</span>
        <span className="cited__ago">{ago(item.event_ts)}</span>
      </div>

      <h3 className="cited__title">
        <button type="button" className="cited__open" onClick={() => void toggle()}>
          {item.title}
        </button>
      </h3>
      {item.detail && <p className="cited__text">{item.detail}</p>}

      <div className="cited__meta">
        {item.place && (
          <span>
            <IcPin />
            {item.place}
          </span>
        )}
        {item.actor && <span>@{item.actor}</span>}
        {item.source_count > 0 && (
          <span>
            <span className="cited__dim">·</span> {item.source_count} 条源消息
          </span>
        )}
        <button type="button" className="cited__toggle" onClick={() => void toggle()}>
          {open ? '收起源消息' : '看源消息'}
        </button>
      </div>

      {open && (
        <div className="cited__srcs">
          {busy && <p className="cited__dim">读取源消息…</p>}
          {err && <p className="cited__srcs-err">{err}</p>}
          {srcs && srcs.length === 0 && (
            <p className="cited__dim">这条没有可回溯的源消息（抽取时没记下来源）。</p>
          )}
          {srcs?.map((s) => (
            <blockquote key={s.msg_id} className="cited__src">
              <div className="cited__src-head">
                <span>{s.group_name}</span>
                <span>·</span>
                <span>{s.sender}</span>
                <span className="cited__ago">{mdhmText(s.ts)}</span>
              </div>
              <p>{s.content}</p>
            </blockquote>
          ))}
        </div>
      )}
    </div>
  )
}
