import { useEffect, useState } from 'react'
import { getPrivacy, type PrivacyResponse } from '../api'
import { IcShield, IcWarn } from '../ui/icons'
import './BoundaryPage.css'

/**
 * 隐私边界 —— 把出网审计摊开给人看的只读页面。
 *
 * 这不是宣传页：核验没过的数字，原样摆出来，那才算得上可证伪。
 *
 * 措辞上有一条不能越的线 —— 这里能证明的只是「出去的文本没有撞上名单里的
 * 姓名」，证明不了「一个姓名都没出去」：模型自己写出来的名字不在这份名单上，
 * 检查看不到它。页面底部要把这层意思写出来，不许省。
 */
export function BoundaryPage() {
  const [data, setData] = useState<PrivacyResponse | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    getPrivacy()
      .then(setData)
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : '读取失败'))
  }, [])

  const head = (
    <header className="phead">
      <div>
        <h1 className="phead__title">隐私边界</h1>
        <div className="phead__tag">privacy</div>
      </div>
      <div className="phead__side">出网留痕 · 只读视图</div>
    </header>
  )

  if (err) {
    return (
      <div className="wrap">
        {head}
        <div className="fail">
          <span>读取审计失败：{err}</span>
          <button type="button" onClick={() => location.reload()}>
            重试
          </button>
        </div>
      </div>
    )
  }

  if (!data) {
    return (
      <div className="wrap">
        {head}
        <p className="quiet">正在读取留痕…</p>
      </div>
    )
  }

  return (
    <div className="wrap">
      {head}

      {data.records === 0 ? (
        <div className="bound__none">
          <p>还没有留下出网痕迹。</p>
          {data.hint && <p className="bound__hint">{data.hint}</p>}
        </div>
      ) : (
        <>
          <section className={data.clean ? 'verdict' : 'verdict verdict--bad'}>
            <div className="verdict__head">
              {data.clean ? <IcShield size={17} /> : <IcWarn />}
              <strong>{data.clean ? '出网文本核验通过' : '出网文本查出泄漏'}</strong>
            </div>
            <p className="verdict__text">
              夜抄送到外面的，只有脱敏之后的结构化结果。下面这些数字是每次出网时自动记下的账
              —— 记的是条数与指纹，正文本身一个字都不留。
            </p>
          </section>

          <div className="stats">
            <Box
              label="出网批数"
              value={data.records}
              note={`涉及 ${data.messages.toLocaleString()} 条消息或条目`}
            />
            <Box
              label="出网文本量"
              value={`${data.chars.toLocaleString()} 字`}
              note={Object.entries(data.surfaces)
                .map(([k, v]) => `${k} ${v}`)
                .join(' · ')}
            />
            <Box
              label="名单内姓名命中"
              // 没有名单可比对时写「未检查」，别写 0 —— 0 读起来像「查过了，干净」。
              value={data.names_were_checked ? data.known_name_hits : '未检查'}
              note={
                data.names_were_checked
                  ? `已对照 ${data.names_checked.toLocaleString()} 个姓名`
                  : '本轮没有可供对照的名单'
              }
              tone={
                !data.names_were_checked ? 'idle' : data.known_name_hits > 0 ? 'bad' : 'good'
              }
            />
            <Box
              label="号码命中"
              value={data.num_hits}
              note={`已遮蔽 ${data.num_placeholders.toLocaleString()} 处`}
              tone={data.num_hits > 0 ? 'bad' : 'good'}
            />
          </div>

          <p className="bound__scope">
            这一页证明的是「出网文本里没有出现已知名单上的姓名」，并不等于「没有任何姓名出去」。
            名单取自本机群成员的昵称表；模型要是自己写出一个表上没有的名字，这道检查就拦不住它
            —— 这是本架构的已知边界，所以照实写在这里。
          </p>

          {data.dirty.length > 0 && (
            <section className="leaks">
              <h2 className="seclabel">下面这几条出网记录被检测器命中</h2>
              <ul>
                {data.dirty.map((r, i) => (
                  <li key={i}>
                    {r.surface} · 批次 {r.batch + 1}：姓名 {r.name_hits} 处 / 号码 {r.num_hits} 处
                    （指纹 {r.sha256}）
                  </li>
                ))}
              </ul>
            </section>
          )}

          {/* 结论判了未通过，却没说是哪一批 —— 这两件事得分开说。 */}
          {!data.clean && data.dirty.length === 0 && (
            <p className="bound__scope">
              核验结论是未通过，但记录里没有指明具体批次。没指明，不等于就没事。
            </p>
          )}
        </>
      )}
    </div>
  )
}

function Box({
  label,
  value,
  note,
  tone = 'idle',
}: {
  label: string
  value: string | number
  note: string
  tone?: 'good' | 'bad' | 'idle'
}) {
  return (
    <div className={`stat stat--${tone}`}>
      <div className="stat__label">{label}</div>
      <div className="stat__value">{value}</div>
      <div className="stat__note">{note}</div>
    </div>
  )
}
