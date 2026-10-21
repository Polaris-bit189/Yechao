import { Fragment, useEffect, useState } from 'react'
import { acceptConsent, getConsent, type ConsentState } from '../api'
import { parseProtocol, type Inline, type PBlock } from '../lib/protocol'
import { mdhmText } from '../lib/time'
import './Consent.css'

/**
 * 第三方账号授权协议：读 → 签署。
 *
 * ## 为什么要有这一页
 *
 * 需要协议的那几个源（IMAP 信箱、带登录态的订阅、浏览器登录态）在没有
 * 接受记录时**拒绝启动**，而它们给出的下一步是「去签协议」—— 在此之前
 * 那条路**只有命令行**（`python -m yechao consent accept`）。网页上想启用
 * 这几个源的人会撞上一句自己做不到的提示。
 *
 * ## 这一页的形状是**被协议本身决定的**
 *
 * 协议正文**随代码走**（`docs/协议/`，可 review、可逐字核对），接受记录
 * 留在**这台机器上**（`config/consent.toml`）。所以正文由后端读出来带过来，
 * 而不是让用户自己去找一个路径 —— 找不到就等于没读。
 *
 * ## ※ 勾选是**必须**的，且按钮上写着同意的是哪一版
 *
 * 直接给一个「接受」按钮，用户点它的时候并没有读过任何东西，而记录里
 * 会写着「他同意了」。所以：先勾「我已读过上面这一版（1.0）」，按钮才可用；
 * 而且**勾选框上写着版本号** —— 那是用户看到的、也是要回传给服务端比对的那一版
 * （见 `api.ts` 的 `acceptConsent`）。
 */
export function Consent() {
  const [state, setState] = useState<ConsentState | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [read, setRead] = useState(false)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)

  function reload() {
    getConsent()
      .then(setState)
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : '读取协议状态失败'))
  }

  useEffect(reload, [])

  async function accept() {
    if (!state) return
    setBusy(true)
    setMsg(null)
    try {
      // 回传**页面上显示的那一版**。服务端比对，对不上回 400 ——
      // 传「当前版本」会让这个校验形同虚设。
      const r = await acceptConsent(state.version)
      setState(r)
      setRead(false)
      setMsg(
        r.recorded
          ? `已接受协议 ${r.version} —— 需要它的源现在可以启动了。`
          : `已经接受过 ${r.version} 了，没有重复记录。`,
      )
    } catch (e) {
      setMsg(e instanceof Error ? e.message : '签署失败')
    } finally {
      setBusy(false)
    }
  }

  if (err) {
    return (
      <div className="fail">
        <span>{err}</span>
        <button type="button" onClick={reload}>
          重试
        </button>
      </div>
    )
  }
  if (!state) return <p className="quiet">读取协议状态…</p>

  const signed = state.accepted_version === state.version
  const stale = state.accepted_version !== null && !signed

  return (
    <>
      <div className="csign__head">
        <div>
          <div className="csign__title">第三方账号授权协议</div>
          <div className="csign__ver">当前版本 {state.version}</div>
        </div>
        <div className={signed ? 'csign__ok' : 'csign__no'}>
          {signed ? (
            <>
              已接受
              {state.accepted_at !== null && <> · {mdhmText(state.accepted_at)}</>}
            </>
          ) : stale ? (
            `签的是旧版 ${state.accepted_version} —— 要重新接受`
          ) : (
            '还没接受'
          )}
        </div>
      </div>

      {/* ※ 没有接受记录时**必须说清后果**：不是「有个东西没配」，
          而是那几个源**会拒绝启动**。含糊的话，用户签完才发现没解决
          他真正的问题（连不上）。 */}
      {!signed && (
        <p className="csign__why">
          需要它的源（IMAP 信箱、带登录态的订阅、浏览器登录态）在启动时
          <strong>会拒绝</strong>。它们会<strong>用你本人的账号</strong>
          去访问第三方站点，所以要先签这份协议。
        </p>
      )}

      {/* 正文读不到时，把原因与路径摆在正文该在的地方 ——
          不是把整页变成一句报错（版本、签没签这些照样答得出来）。 */}
      {state.doc_md === null ? (
        <div className="csign__warn">
          <strong>读不到协议正文</strong>
          <p>{state.doc_error}</p>
          <p className="csign__path">{state.doc_path}</p>
        </div>
      ) : (
        <article className="csign__doc">
          {parseProtocol(state.doc_md).map((b, i) => (
            <Doc key={i} block={b} />
          ))}
        </article>
      )}

      {!signed && (
        <>
          <label className="csign__agree">
            <input
              type="checkbox"
              checked={read}
              // ※ 正文都读不到时**不许勾**：那个勾的含义是「我读过了」，
              // 而页面上根本没有可读的东西。
              disabled={state.doc_md === null}
              onChange={(e) => setRead(e.target.checked)}
            />
            <span>
              我已读过上面这一版（{state.version}），并同意
              {stale && `（此前接受的是 ${state.accepted_version}，已失效）`}
            </span>
          </label>

          <div className="csign__acts">
            <button
              type="button"
              className="csign__go"
              disabled={busy || !read || state.doc_md === null}
              onClick={() => void accept()}
            >
              {busy ? '记录中…' : `接受协议 ${state.version}`}
            </button>
          </div>
        </>
      )}

      {msg && <p className="csign__msg">{msg}</p>}

      <p className="csign__foot">
        ※ 接受协议<strong>不等于</strong>能访问：登录态仍然要单独配
        （见 <code>sources.toml</code> 与各站的环境变量）。记录写在{' '}
        <code>config/consent.toml</code>，删掉它就回到没签的状态。
      </p>
    </>
  )
}

function Spans({ spans }: { spans: Inline[] }) {
  return (
    <>
      {spans.map((s, i) =>
        s.t === 'b' ? (
          <strong key={i}>{s.text}</strong>
        ) : s.t === 'code' ? (
          <code key={i}>{s.text}</code>
        ) : (
          <Fragment key={i}>{s.text}</Fragment>
        ),
      )}
    </>
  )
}

function Doc({ block }: { block: PBlock }) {
  switch (block.t) {
    case 'h':
      return block.level === 1 ? (
        <h2 className="csign__h1">
          <Spans spans={block.spans} />
        </h2>
      ) : block.level === 2 ? (
        <h3 className="csign__h2">
          <Spans spans={block.spans} />
        </h3>
      ) : (
        <h4 className="csign__h3">
          <Spans spans={block.spans} />
        </h4>
      )
    case 'hr':
      return <hr className="csign__hr" />
    case 'ul':
      return (
        <ul className="csign__ul">
          {block.items.map((it, i) => (
            <li key={i}>
              <Spans spans={it} />
            </li>
          ))}
        </ul>
      )
    case 'ol':
      return (
        <ol className="csign__ol">
          {block.items.map((it, i) => (
            <li key={i}>
              <Spans spans={it} />
            </li>
          ))}
        </ol>
      )
    case 'quote':
      return (
        <blockquote className="csign__quote">
          <Spans spans={block.spans} />
        </blockquote>
      )
    case 'pre':
      return <pre className="csign__pre">{block.text}</pre>
    case 'table':
      return (
        // 窄屏下表格要能横滚 —— 挤成两行比看不全更糟。
        <div className="csign__tablewrap">
          <table className="csign__table">
            <thead>
              <tr>
                {block.head.map((c, i) => (
                  <th key={i}>
                    <Spans spans={c} />
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {block.rows.map((r, i) => (
                <tr key={i}>
                  {r.map((c, j) => (
                    <td key={j}>
                      <Spans spans={c} />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )
    default:
      return (
        <p className="csign__p">
          <Spans spans={block.spans} />
        </p>
      )
  }
}
