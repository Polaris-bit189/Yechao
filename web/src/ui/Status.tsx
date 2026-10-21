import { useEffect, useState } from 'react'
import { getStatus, type RunRow, type StatusResponse } from '../api'
import { sourceLabel } from '../lib/source'
import { ago, mdhmText } from '../lib/time'
import './Status.css'

/**
 * 数据源状态。
 *
 * ## 为什么要有它
 *
 * 「信息流每十分钟在更新、内容是从哪儿来的」原来只写在 README 里 ——
 * 那是一句**声明**。而 `runs` / `run_entries` 里已经躺着这件事的**证据**
 * （哪一轮、什么时候跑的、读到几条、哪个源没读到）。把它显示出来，
 * 声明就变成了事实。
 *
 * ## 两个组件、同一份数据
 *
 * · `StatusStrip` —— 一行，挂在信息流页顶上。**默认就看得见**，不用点。
 * · `StatusPanel` —— 展开版，配置页的「数据源」页签里。
 *
 * 拆成两个而不是「一个带 compact 开关」：它们的**信息取舍**不一样，
 * 不是一个东西的两种尺寸。一行里放不下的那些（每一轮的明细、失败账的
 * 原因码）在面板里是主体。
 */

const RUN_KIND: Record<string, string> = {
  ingest: '摄取',
  refine: '抽取',
  digest: '日报',
}

function runKind(kind: string): string {
  return RUN_KIND[kind] ?? kind
}

/** 最近一轮**摄取**。没有就是 `null` —— 与「跑了但读到 0 条」不是一回事。 */
function lastIngest(s: StatusResponse): RunRow | null {
  return s.runs.find((r) => r.kind === 'ingest') ?? null
}

/** 拉一次状态。失败返回 `null` 并把消息交给调用方 —— 不静默吞掉。 */
function useStatus(): { data: StatusResponse | null; err: string | null } {
  const [data, setData] = useState<StatusResponse | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    getStatus()
      .then((d) => {
        if (live) setData(d)
      })
      .catch((e: unknown) => {
        if (live) setErr(e instanceof Error ? e.message : '读取数据源状态失败')
      })
    return () => {
      live = false
    }
  }, [])

  return { data, err }
}

/**
 * 一行状态条。
 *
 * ※ 措辞按**口径**写，不图顺口：
 *   · 「最近一轮摄取」是 `runs` 里那一轮的事
 *   · 「库里 N 条来自 M 个源」是**累计**的分布（每轮各读多少没落库）
 * 写成「本轮读了 N 条」就是一句编出来的话 —— 见 `api.py` 的 `status`。
 */
export function StatusStrip() {
  const { data, err } = useStatus()

  if (err) {
    // ※ 状态条拉不到时**不消失**：它消失之后，这一页看起来与
    // 「一切正常」完全一样，而那正是它要防的事。
    return (
      <div className="stat">
        <span className="stat__bad">数据源状态读不到：{err}</span>
      </div>
    )
  }
  if (!data) return <div className="stat"><span className="stat__dim">读取数据源状态…</span></div>

  const last = lastIngest(data)
  const live = data.by_source.filter((s) => s.source)
  const failed = data.unreadable.length

  return (
    <div className="stat">
      {last ? (
        <>
          <span className="stat__dim">最近一轮摄取</span>
          <span>{ago(last.started_at)}</span>
          <span className="stat__dim">·</span>
          <span>读到 {last.seen} 条</span>
          {/* `status` 是后端记的闭集，不是我们按条数猜的 ——
              「读到 0 条」既可能是安静、也可能是失败，只有它分得开。 */}
          {last.status !== 'ok' && <span className="stat__bad">这一轮有源没读到</span>}
        </>
      ) : (
        <span className="stat__dim">还没有跑过摄取</span>
      )}

      <span className="stat__dim">·</span>

      <span className="stat__dim">库里 {data.messages} 条来自</span>
      <span>
        {live.length} / {data.known_sources.length} 个源
      </span>

      {failed > 0 && (
        <>
          <span className="stat__dim">·</span>
          <span className="stat__bad">{failed} 个源没读到</span>
        </>
      )}
    </div>
  )
}

/** 展开版。配置页「数据源」页签。 */
export function StatusPanel() {
  const { data, err } = useStatus()

  if (err) return <div className="fail"><span>{err}</span></div>
  if (!data) return <p className="quiet">读取数据源状态…</p>

  const last = lastIngest(data)
  const live = data.by_source.filter((s) => s.source)

  return (
    <>
      <p className="sthint">
        每一条消息都记着它<strong>从哪个源读来的</strong>，所以「多源」不是一句说明，
        是这一页上的几个数字。点的那个「摄取」按钮（或定时器）跑一轮，
        这里就跟着变。
      </p>

      <div className="stgrid">
        <div className="stcard">
          <div className="stcard__n">{data.messages}</div>
          <div className="stcard__k">库里消息</div>
        </div>
        <div className="stcard">
          <div className="stcard__n">
            {live.length} / {data.known_sources.length}
          </div>
          <div className="stcard__k">出现过的源 / 已注册</div>
        </div>
        <div className="stcard">
          <div className="stcard__n">{last ? ago(last.started_at) : '—'}</div>
          <div className="stcard__k">最近一轮摄取</div>
        </div>
      </div>

      {/* ── 按来源 ─────────────────────────────────────────────
          ※ 标题里必须带「库里」两个字：这是**累计**分布，
          不是「上一轮各读了多少条」（那个没落库）。 */}
      <h2 className="seclabel">库里消息 · 按来源</h2>
      {data.by_source.length === 0 ? (
        <p className="quiet">库里还没有消息。</p>
      ) : (
        <ul className="strows">
          {data.by_source.map((s) => (
            <li key={s.source || '(none)'} className="strows__line">
              <span className="strows__name">{sourceLabel(s.source)}</span>
              {/* 空串是「这条消息没标来源」，把它显示成「未标来源」而不是
                  藏起来 —— 藏起来会让下面几行的和与总数对不上。 */}
              {s.source && <span className="strows__dim">{s.source}</span>}
              <span className="strows__dim">{s.messages} 条</span>
              <span className="strows__dim">
                最新 {s.last_ts > 0 ? mdhmText(s.last_ts) : '—'}
              </span>
            </li>
          ))}
        </ul>
      )}

      {/* ── 没读到的源 ───────────────────────────────────────── */}
      <h2 className="seclabel">没读到的源</h2>
      {data.unreadable.length === 0 ? (
        // ※ 「一个都没有」是一句**有信息量的话**（最近几轮都读到了），
        // 不是空状态 —— 所以要说出来，而不是不渲染这一块。
        <p className="quiet">最近几轮里没有读不到的源。</p>
      ) : (
        <ul className="strows">
          {data.unreadable.map((u, i) => (
            <li key={`${u.run_id}-${i}`} className="strows__line">
              <span className="strows__name">{u.source ? sourceLabel(u.source) : '（认不出源名）'}</span>
              {/* 原因码原样显示：它是**短码**（`qq:SourceError`），
                  后端刻意不往里塞正文（见 schema.py 的 CHECK）。 */}
              <span className="strows__dim">{u.why || u.reason}</span>
              <span className="strows__dim">{ago(u.run_started_at)}</span>
            </li>
          ))}
        </ul>
      )}

      {/* ── 最近几轮 ─────────────────────────────────────────── */}
      <h2 className="seclabel">最近几轮</h2>
      {data.runs.length === 0 ? (
        <p className="quiet">还没有跑过任何一轮。</p>
      ) : (
        <ul className="strows">
          {data.runs.map((r) => (
            <li key={r.run_id} className="strows__line">
              <span className="strows__name">{runKind(r.kind)}</span>
              <span className={r.status === 'ok' ? 'strows__dim' : 'stbad'}>
                {r.status === 'ok' ? '正常' : '有失败'}
              </span>
              <span className="strows__dim">{mdhmText(r.started_at)}</span>
              {/* 「读到」与「写入」两个数都报：只报一个的话，
                  「今天怎么只有这些」与「今天读到的都重复了」分不开。 */}
              <span className="strows__dim">
                读到 {r.seen} · 写入 {r.produced}
              </span>
              {/* 只有抽取那一轮才有模型覆盖率这两个数，别的 kind 是 0
                  —— 0 在这里的含义是「这一轮没有模型那一侧的事」，
                  不是「一条都没发出去」。 */}
              {r.kind === 'refine' && r.model_eligible > 0 && (
                <span className="strows__dim">
                  送模型 {r.model_sent}/{r.model_eligible}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}

      <p className="sthint">
        ※ 「读到多少条」是<strong>这一轮从源里读到的</strong>，
        「库里多少条」是<strong>现在存着的</strong>。每轮各源分别读了多少条
        只写在 watcher 的日志里，没有落库 —— 所以上面那张表是累计分布，
        不是某一轮的分项。
      </p>
    </>
  )
}
