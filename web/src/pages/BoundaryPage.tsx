import { useEffect, useState } from 'react'
import { getPrivacy, type PrivacyResponse } from '../api'
import { IcShield, IcWarn } from '../ui/icons'
import './BoundaryPage.css'

/**
 * 隐私边界 —— 出网审计的只读视图。
 *
 * 这一页不是宣传页：数字不好看的时候照原样显示，那才叫可证伪。
 *
 * 措辞有一条硬线：这里能证明的是「出网文本里不含名单上的姓名」，
 * 不能证明「不含任何姓名」—— 模型自己编出来的名字不在名单里，拦不住。
 * 页面底部必须把这句写出来，不能省。
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
      <div className="phead__side">出网审计 · 只读</div>
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
        <p className="quiet">读取出网记录…</p>
      </div>
    )
  }

  return (
    <div className="wrap">
      {head}

      {data.records === 0 ? (
        <div className="bound__none">
          <p>还没有出网记录。</p>
          {data.hint && <p className="bound__hint">{data.hint}</p>}
        </div>
      ) : (
        <>
          <section className={data.clean ? 'verdict' : 'verdict verdict--bad'}>
            <div className="verdict__head">
              {data.clean ? <IcShield size={17} /> : <IcWarn />}
              <strong>{data.clean ? '出网边界已核验' : '检测到出网泄漏'}</strong>
            </div>
            <p className="verdict__text">
              夜抄只把脱敏后的结构化结果送出去。下面这些数字来自每次出网时自动落下的审计记录
              —— 只存计数与指纹，不存内容。
            </p>
          </section>

          <div className="stats">
            <Box
              label="出网次数"
              value={data.records}
              note={`覆盖 ${data.messages.toLocaleString()} 条消息/条目`}
            />
            <Box
              label="出网文本"
              value={`${data.chars.toLocaleString()} 字`}
              note={Object.entries(data.surfaces)
                .map(([k, v]) => `${k} ${v}`)
                .join(' · ')}
            />
            <Box
              label="已知名单姓名命中"
              // 没比对过时写「未检查」，不写 0。0 会被读成「查了，没漏」。
              value={data.names_were_checked ? data.known_name_hits : '未检查'}
              note={
                data.names_were_checked
                  ? `比对过 ${data.names_checked.toLocaleString()} 个姓名`
                  : '这一轮没有可比对的名单'
              }
              tone={
                !data.names_were_checked ? 'idle' : data.known_name_hits > 0 ? 'bad' : 'good'
              }
            />
            <Box
              label="号码类命中"
              value={data.num_hits}
              note={`遮蔽 ${data.num_placeholders.toLocaleString()} 处`}
              tone={data.num_hits > 0 ? 'bad' : 'good'}
            />
          </div>

          <p className="bound__scope">
            上面证明的是「出网文本不含已知名单里的姓名」，不等于「不含任何姓名」。
            名单来自本机群成员昵称表；模型若补出一个不在名单上的名字，这道边界拦不住
            —— 这是本架构已知的边界，如实写在这里。
          </p>

          {data.dirty.length > 0 && (
            <section className="leaks">
              <h2 className="seclabel">以下出网记录命中了检测器</h2>
              <ul>
                {data.dirty.map((r, i) => (
                  <li key={i}>
                    {r.surface} · 第 {r.batch + 1} 批：姓名 {r.name_hits} / 号码 {r.num_hits}
                    （指纹 {r.sha256}）
                  </li>
                ))}
              </ul>
            </section>
          )}

          {/* 结论说没通过、却给不出批次 —— 这两件事要分开说。 */}
          {!data.clean && data.dirty.length === 0 && (
            <p className="bound__scope">
              审计结论是未通过，但没有给出具体批次。这不等于没有问题。
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
