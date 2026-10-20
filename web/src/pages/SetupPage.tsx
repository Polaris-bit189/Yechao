import { useEffect, useState } from 'react'
import {
  dropCategory,
  dropGroup,
  dropPerson,
  getCategories,
  getGroups,
  getItems,
  getJob,
  getLlm,
  getPack,
  getPersons,
  newCategory,
  newGroup,
  newPerson,
  patchCategory,
  patchGroup,
  putLlm,
  putPack,
  startReorder,
  stepBackCategory,
  stopJob,
  undoJob,
  type CategoriesResponse,
  type CategoryDetail,
  type GroupRow,
  type GroupsResponse,
  type JobRow,
  type LlmSettings,
  type Person,
} from '../api'
import { longDateOf, shortDateOf } from '../lib/fmt'
import { kindColor } from '../lib/theme'
import './SetupPage.css'

type Tab = 'groups' | 'people' | 'llm' | 'categories' | 'reorder' | 'pack'

const TABS: Array<{ id: Tab; label: string }> = [
  { id: 'groups', label: '群' },
  { id: 'people', label: '人物' },
  { id: 'llm', label: 'LLM' },
  { id: 'categories', label: '类目' },
  { id: 'reorder', label: '重排' },
  { id: 'pack', label: '导入导出' },
]

export function SetupPage({ onChanged }: { onChanged: () => void }) {
  const [tab, setTab] = useState<Tab>('groups')

  return (
    <div className="wrap">
      <header className="phead">
        <div>
          <h1 className="phead__title">配置</h1>
          <div className="phead__tag">settings</div>
        </div>
        <div className="phead__side">数据源 · 模型 · 类目</div>
      </header>

      <nav className="tabs" aria-label="配置分区">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            className={tab === t.id ? 'tabs__btn is-on' : 'tabs__btn'}
            aria-current={tab === t.id ? 'page' : undefined}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </nav>

      {tab === 'groups' && <Groups onChanged={onChanged} />}
      {tab === 'people' && <People onChanged={onChanged} />}
      {tab === 'llm' && <Llm onChanged={onChanged} />}
      {tab === 'categories' && <Categories onChanged={onChanged} />}
      {tab === 'reorder' && <Reorder onChanged={onChanged} />}
      {tab === 'pack' && <Pack />}
    </div>
  )
}

// ── 群 ──────────────────────────────────────────────────────

/**
 * 群清单。
 *
 * 快照还没生成时后端会明说 `available: false` 并给出原因。这时**不能**
 * 画一张空表 —— 空表会被读成「你的库里一个群都没有」，而真相是还没读过。
 */
function Groups({ onChanged }: { onChanged: () => void }) {
  const [data, setData] = useState<GroupsResponse | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const [gid, setGid] = useState('')
  const [gname, setGname] = useState('')
  /**
   * 每个群**抽出了多少条**。
   *
   * ※ 规格明确要这一栏：`326 条` 是「这个群在库里有多少消息」，
   * `抽出 105 条` 才是「这个群值不值得留」的依据 —— 一个全是闲聊的群
   * 消息再多也抽不出东西。两个数放在一起才有意义。
   *
   * ※ 拉不到时**不放 0**：0 与「没抽过」在界面上长得一样，
   * 而它们的意思差得很远。
   */
  const [extracted, setExtracted] = useState<Record<number, number | 'fail'>>({})

  function reload() {
    getGroups()
      .then((g) => {
        setData(g)
        // 每个群拉一次 total。失败记 'fail'，不记 0。
        for (const row of g.groups) {
          getItems({ group: row.id, limit: 1 })
            .then((p) => setExtracted((cur) => ({ ...cur, [row.id]: p.total })))
            .catch(() => setExtracted((cur) => ({ ...cur, [row.id]: 'fail' })))
        }
      })
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : '读取失败'))
  }

  useEffect(reload, [])

  async function act(key: string, fn: () => Promise<unknown>, note: string) {
    setBusy(key)
    setMsg(null)
    try {
      await fn()
      setMsg(note)
      reload()
      onChanged()
    } catch (e) {
      setMsg(e instanceof Error ? e.message : '操作失败')
    } finally {
      setBusy(null)
    }
  }

  async function join() {
    const id = Number(gid.trim())
    if (!Number.isInteger(id) || id <= 0) {
      setMsg('群号要写正整数')
      return
    }
    if (!gname.trim()) {
      // 这一页只负责加群，没有改名的地方 —— 名字填错了只能去动配置文件。
      setMsg('群名不能留空：加进来之后这一页改不了它，只能去改配置文件。')
      return
    }
    await act('join', () => newGroup(id, gname.trim(), 'normal'), `已加入监视：${gname.trim()}`)
    setGid('')
    setGname('')
  }

  if (err) return <div className="fail"><span>{err}</span><button type="button" onClick={reload}>重试</button></div>
  if (!data) return <p className="quiet">读取群清单…</p>

  return (
    <>
      {!data.available ? (
        <div className="note note--warn">
          <strong>还没有群清单</strong>
          <p>{data.reason}</p>
        </div>
      ) : (
        <>
          <p className="hint">
            清单来自 {data.generated_at ? `${longDateOf(data.generated_at)} 的` : ''}
            快照（由 yechao export 生成）。刚加进 QQ 的群要等下次 export 才看得见。
          </p>

          {data.groups.length === 0 ? (
            // 快照读到了、里面确实没有群。与上面那种情况是两句话。
            <p className="quiet">快照里没有群。</p>
          ) : (
            <ul className="rows">
              {data.groups.map((g) => (
                <GroupLine
                  key={g.id}
                  g={g}
                  extracted={extracted[g.id]}
                  busy={busy !== null}
                  onToggle={() =>
                    void act(
                      `t${g.id}`,
                      () => patchGroup(g.id, { enabled: !g.watched }),
                      g.watched ? `已停用 ${g.name || g.id}` : `已启用 ${g.name || g.id}`,
                    )
                  }
                  onJoin={() =>
                    void act(`j${g.id}`, () => newGroup(g.id, g.name, 'normal'), `已加入 ${g.name || g.id}`)
                  }
                  onDrop={() =>
                    void act(`d${g.id}`, () => dropGroup(g.id), `已移除 ${g.name || g.id}`)
                  }
                />
              ))}
            </ul>
          )}
        </>
      )}

      {msg && <p className="msg">{msg}</p>}

      <div className="form">
        <label className="field">
          <span>群号</span>
          <input value={gid} inputMode="numeric" onChange={(e) => setGid(e.target.value)} />
        </label>
        <label className="field">
          <span>群名</span>
          <input value={gname} onChange={(e) => setGname(e.target.value)} />
        </label>
        <button type="button" disabled={busy === 'join'} onClick={() => void join()}>
          加入监视
        </button>
      </div>
    </>
  )
}

/** 一行：群名 群号 条数 最近活跃 抽出数 操作。规格要求这些都要在同一行。 */
function GroupLine({
  g,
  extracted,
  busy,
  onToggle,
  onJoin,
  onDrop,
}: {
  g: GroupRow
  /** 抽出了多少条。`undefined` = 还没读到，`'fail'` = 读不到。 */
  extracted: number | 'fail' | undefined
  busy: boolean
  onToggle: () => void
  onJoin: () => void
  onDrop: () => void
}) {
  return (
    <li className="rows__line">
      <span className="rows__name">{g.name || '（未命名群）'}</span>
      <span className="rows__dim">{g.id}</span>
      <span className="rows__dim">{g.messages} 条</span>
      {/* 0 不是 1970 年，是「没有记录」。 */}
      <span className="rows__dim">最近 {g.last_ts > 0 ? shortDateOf(g.last_ts) : '—'}</span>
      {/* ※ 「抽出多少条」与「库里有多少条」是两件事。
          拉不到时说「抽出 —」，不说 0 —— 0 会被读成「一条都没抽出来」。 */}
      <span className="rows__dim">
        抽出 {extracted === undefined ? '…' : extracted === 'fail' ? '—' : `${extracted} 条`}
      </span>
      <span className="rows__acts">
        {/* 三支判据是 known，不是「看起来像停了」。 */}
        {g.watched ? (
          <>
            <button type="button" disabled={busy} onClick={onToggle}>
              停用
            </button>
            <button type="button" className="is-danger" disabled={busy} onClick={onDrop}>
              移除
            </button>
          </>
        ) : g.known ? (
          <button type="button" disabled={busy} onClick={onToggle}>
            启用
          </button>
        ) : (
          <button type="button" className="is-primary" disabled={busy} onClick={onJoin}>
            加入监视
          </button>
        )}
      </span>
    </li>
  )
}

// ── 人物 ────────────────────────────────────────────────────

function People({ onChanged }: { onChanged: () => void }) {
  const [list, setList] = useState<Person[] | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [uin, setUin] = useState('')
  const [label, setLabel] = useState('')
  const [msg, setMsg] = useState<string | null>(null)

  function reload() {
    getPersons()
      .then((r) => setList(r.persons))
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : '读取失败'))
  }

  useEffect(reload, [])

  async function add() {
    const n = Number(uin.trim())
    if (!Number.isInteger(n) || n <= 0) {
      setMsg('QQ 号只能是正整数，0 不构成一个账号')
      return
    }
    if (!label.trim()) {
      setMsg('填一个名字 —— 群自己的昵称会改，你填的这个不会。')
      return
    }
    try {
      await newPerson(n, label.trim())
      setUin('')
      setLabel('')
      setMsg('已加入名单')
      reload()
      onChanged()
    } catch (e) {
      setMsg(e instanceof Error ? e.message : '加入失败')
    }
  }

  async function remove(id: number) {
    try {
      // 删除返回 204，没有响应体可解。
      await dropPerson(id)
      reload()
      onChanged()
    } catch (e) {
      setMsg(e instanceof Error ? e.message : '移除失败')
    }
  }

  if (err) return <div className="fail"><span>{err}</span><button type="button" onClick={reload}>重试</button></div>
  if (!list) return <p className="quiet">读取名单…</p>

  return (
    <>
      <p className="hint">
        日报里的「@某某」用这份名单。不在这里的昵称，抽取时不会被当成可识别的发送者。
      </p>

      {list.length === 0 ? (
        <p className="quiet">名单是空的。</p>
      ) : (
        <ul className="rows">
          {list.map((p) => (
            <li key={p.account} className="rows__line">
              <span className="rows__name">{p.label}</span>
              <span className="rows__dim">{p.account}</span>
              <span className="rows__dim">{p.count} 条</span>
              <span className="rows__note">{p.note}</span>
              <span className="rows__acts">
                <button type="button" className="is-danger" onClick={() => void remove(p.account)}>
                  移除
                </button>
              </span>
            </li>
          ))}
        </ul>
      )}

      {msg && <p className="msg">{msg}</p>}

      <div className="form">
        <label className="field">
          <span>QQ 号</span>
          <input value={uin} inputMode="numeric" onChange={(e) => setUin(e.target.value)} />
        </label>
        <label className="field">
          <span>名字</span>
          <input value={label} onChange={(e) => setLabel(e.target.value)} />
        </label>
        <button type="button" onClick={() => void add()}>
          加入
        </button>
      </div>
    </>
  )
}

// ── 模型 ────────────────────────────────────────────────────

/** 三态。`omit` 与 `off` 在界面上只差一个词，进报文完全是两回事。 */
type ThinkMode = 'omit' | 'on' | 'off'

const toMode = (v: boolean | null): ThinkMode => (v === null ? 'omit' : v ? 'on' : 'off')
const fromMode = (v: ThinkMode): boolean | null => (v === 'omit' ? null : v === 'on')

const describe = (v: boolean | null) =>
  v === null ? '不发这个字段' : `发 ${v}`

function Llm({ onChanged }: { onChanged: () => void }) {
  const [saved, setSaved] = useState<LlmSettings | null>(null)
  const [base, setBase] = useState('')
  const [model, setModel] = useState('')
  const [mode, setMode] = useState<ThinkMode>('omit')
  const [err, setErr] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    getLlm()
      .then((s) => {
        setSaved(s)
        setBase(s.base_url)
        setModel(s.model)
        setMode(toMode(s.enable_thinking))
      })
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : '读取失败'))
  }, [])

  async function save() {
    setBusy(true)
    setMsg(null)
    try {
      const r = await putLlm({
        base_url: base.trim(),
        model: model.trim(),
        enable_thinking: fromMode(mode),
      })
      // 用服务端回来的值回填 —— 写侧做了归一化，界面上要看得见结果。
      setBase(r.base_url)
      setModel(r.model)
      setMode(toMode(r.enable_thinking))
      setSaved(r)
      setMsg(`已保存：${r.base_url || '（未填）'} · ${r.model || '（未填）'} · ${describe(r.enable_thinking)}`)
      onChanged()
    } catch (e) {
      setMsg(e instanceof Error ? e.message : '保存失败')
    } finally {
      setBusy(false)
    }
  }

  if (err) return <div className="fail"><span>{err}</span><button type="button" onClick={() => location.reload()}>重试</button></div>
  if (!saved) return <p className="quiet">读取模型设置…</p>

  return (
    <>
      <p className="hint">
        密钥不在这里 —— 它走环境变量，不进配置文件，也不会被导出到配置包里。
      </p>

      <div className="form form--col">
        <label className="field">
          <span>端点</span>
          <input value={base} onChange={(e) => setBase(e.target.value)} placeholder="https://…/v1/chat/completions" />
        </label>
        <label className="field">
          <span>模型</span>
          <input value={model} onChange={(e) => setModel(e.target.value)} placeholder="模型名" />
        </label>
        <label className="field">
          <span>思考开关</span>
          <select value={mode} onChange={(e) => setMode(e.target.value as ThinkMode)}>
            <option value="omit">不发这个字段</option>
            <option value="off">发 false</option>
            <option value="on">发 true</option>
          </select>
        </label>
      </div>

      <p className="hint">
        「不发」与「发 false」看上去只差一个词，报文里完全不同：有些通用端点收到 false
        会拒绝，而另一些模型收到「不发」会一直思考、迟迟不写正文。
      </p>

      {msg && <p className="msg">{msg}</p>}

      <button type="button" className="save" disabled={busy} onClick={() => void save()}>
        {busy ? '保存中…' : '保存'}
      </button>
    </>
  )
}

// ── 类目 ────────────────────────────────────────────────────

/**
 * 类目编辑器。
 *
 * ※ `desc` 是**判据**，会逐字进提示词。所以它不是「备注」，
 * 是「告诉模型这一类该怎么办」—— 编辑器里要写清楚这一点，
 * 否则用户会在这里写一句给自己看的说明。
 *
 * ※ 改名走**别名**：旧 slug 的条目不改 `item_id`，只是显示时按新名字。
 * 所以改完要刷新，并且在页面上列出「改过名的」。
 */
function Categories({ onChanged }: { onChanged: () => void }) {
  const [data, setData] = useState<CategoriesResponse | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [editing, setEditing] = useState<string | null>(null)
  const [draft, setDraft] = useState({ slug: '', label: '', desc: '' })
  const [adding, setAdding] = useState(false)
  const [newCat, setNewCat] = useState({ slug: '', label: '', desc: '' })
  const [deleting, setDeleting] = useState<CategoryDetail | null>(null)
  const [migrateTo, setMigrateTo] = useState('')

  function reload() {
    getCategories()
      .then(setData)
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : '读取失败'))
  }

  useEffect(reload, [])

  async function run(fn: () => Promise<unknown>, note: string) {
    setBusy(true)
    setMsg(null)
    try {
      await fn()
      setMsg(note)
      reload()
      onChanged()
    } catch (e) {
      setMsg(e instanceof Error ? e.message : '操作失败')
    } finally {
      setBusy(false)
    }
  }

  if (err) return <div className="fail"><span>{err}</span><button type="button" onClick={reload}>重试</button></div>
  if (!data) return <p className="quiet">读取类目…</p>

  const aliasEntries = Object.entries(data.aliases)

  return (
    <>
      <p className="hint">
        「判据」会**逐字**进提示词，所以它写的是「这一类该怎么判」，
        不是给自己看的备注。改名不会动已有条目，旧名字会留成别名。
      </p>

      <ul className="cats-edit">
        {data.categories.map((c) => (
          <li key={c.slug} className="cats-edit__row">
            {editing === c.slug ? (
              <div className="cats-edit__form">
                <label className="field">
                  <span>中文名</span>
                  <input
                    value={draft.label}
                    onChange={(e) => setDraft({ ...draft, label: e.target.value })}
                  />
                </label>
                <label className="field">
                  <span>slug（改名会留别名）</span>
                  <input
                    value={draft.slug}
                    onChange={(e) => setDraft({ ...draft, slug: e.target.value })}
                  />
                </label>
                <label className="field field--wide">
                  <span>判据（会逐字进提示词）</span>
                  <textarea
                    className="cats-edit__desc"
                    value={draft.desc}
                    onChange={(e) => setDraft({ ...draft, desc: e.target.value })}
                  />
                </label>
                <div className="cats-edit__acts">
                  <button
                    type="button"
                    className="is-primary"
                    disabled={busy}
                    onClick={() =>
                      void run(
                        () => patchCategory(c.slug, draft),
                        draft.slug !== c.slug
                          ? `已把「${c.slug}」改名为「${draft.slug}」`
                          : `已保存「${draft.label}」`,
                      ).then(() => setEditing(null))
                    }
                  >
                    保存
                  </button>
                  <button type="button" onClick={() => setEditing(null)}>
                    取消
                  </button>
                </div>
              </div>
            ) : (
              <>
                <span className="cats-edit__mark" style={{ background: kindColor(c.slug) }} />
                <span className="cats-edit__label" style={{ color: kindColor(c.slug) }}>
                  {c.label}
                </span>
                <span className="rows__dim">{c.slug}</span>
                <span className="rows__dim">{c.count} 条</span>
                <span className="cats-edit__descview">{c.desc}</span>
                <span className="rows__acts">
                  <button
                    type="button"
                    onClick={() => {
                      setEditing(c.slug)
                      setDraft({ slug: c.slug, label: c.label, desc: c.desc })
                    }}
                  >
                    编辑
                  </button>
                  <button
                    type="button"
                    className="is-danger"
                    onClick={() => {
                      setDeleting(c)
                      setMigrateTo('')
                    }}
                  >
                    删除
                  </button>
                </span>
              </>
            )}
          </li>
        ))}
      </ul>

      {aliasEntries.length > 0 && (
        <p className="hint">
          改过名的类目：{aliasEntries.map(([a, b]) => `${a} → ${b}`).join('、')}
        </p>
      )}

      {deleting && (
        <div className="note note--warn">
          <strong>删除「{deleting.label}」</strong>
          {deleting.count > 0 ? (
            <p>
              还有 {deleting.count} 条条目挂在这个类目上。删之前得先选一个去处 ——
              不然那些条目的类目会变成一个查不到的 slug。
            </p>
          ) : (
            <p>没有条目挂在它下面，可以直接删。</p>
          )}

          {deleting.count > 0 && (
            <select
              className="cats-edit__migrate"
              value={migrateTo}
              onChange={(e) => setMigrateTo(e.target.value)}
            >
              <option value="">选一个迁移目标…</option>
              {data.categories
                .filter((x) => x.slug !== deleting.slug)
                .map((x) => (
                  <option key={x.slug} value={x.slug}>
                    {x.label}
                  </option>
                ))}
            </select>
          )}

          <div className="cats-edit__acts">
            <button
              type="button"
              className="is-danger"
              disabled={busy || (deleting.count > 0 && !migrateTo)}
              onClick={() =>
                void run(
                  () => dropCategory(deleting.slug, migrateTo || undefined),
                  `已删除「${deleting.label}」`,
                ).then(() => setDeleting(null))
              }
            >
              确认删除
            </button>
            <button type="button" onClick={() => setDeleting(null)}>
              取消
            </button>
          </div>
        </div>
      )}

      {msg && <p className="msg">{msg}</p>}

      {adding ? (
        <div className="form form--col">
          <label className="field">
            <span>slug（英文/数字/-）</span>
            <input
              value={newCat.slug}
              onChange={(e) => setNewCat({ ...newCat, slug: e.target.value })}
            />
          </label>
          <label className="field">
            <span>中文名</span>
            <input
              value={newCat.label}
              onChange={(e) => setNewCat({ ...newCat, label: e.target.value })}
            />
          </label>
          <label className="field">
            <span>判据（会逐字进提示词）</span>
            <textarea
              className="cats-edit__desc"
              value={newCat.desc}
              onChange={(e) => setNewCat({ ...newCat, desc: e.target.value })}
            />
          </label>
          <div className="cats-edit__acts">
            <button
              type="button"
              className="is-primary"
              disabled={busy}
              onClick={() =>
                void run(
                  () => newCategory(newCat.slug, newCat.label, newCat.desc),
                  `已新增「${newCat.label}」`,
                ).then(() => {
                  setAdding(false)
                  setNewCat({ slug: '', label: '', desc: '' })
                })
              }
            >
              新增
            </button>
            <button type="button" onClick={() => setAdding(false)}>
              取消
            </button>
          </div>
        </div>
      ) : (
        <div className="acts">
          <button type="button" onClick={() => setAdding(true)}>
            新增类目
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() =>
              void run(async () => {
                const r = await stepBackCategory()
                if (!r.undone) throw new Error('撤销栈是空的，没有可回退的类目改动')
              }, '撤销完成 —— 类目和改名映射都退回到改动前的状态')
            }
          >
            撤销类目改动
          </button>
        </div>
      )}
    </>
  )
}

// ── 重排 ────────────────────────────────────────────────────

/**
 * 用当前类目判据重判存量条目。
 *
 * ※ 一次只允许一个重排在跑 —— 并发两个会让事件顺序失去意义，
 * 而撤销回来可能覆盖前一个的结果。
 *
 * ※ 取消是**协作式**的：这个请求只是把意图记下来，手上那批照跑，
 * 真正停没停要看下一次轮询回来的状态。
 */
function Reorder({ onChanged }: { onChanged: () => void }) {
  const [job, setJob] = useState<JobRow | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  // 只在跑的时候轮询。停了就没有理由继续请求。
  useEffect(() => {
    if (!job || job.state !== 'running') return
    const id = setInterval(() => {
      getJob(job.job_id)
        .then(setJob)
        .catch(() => {
          /* 轮询失败不打断界面；下一次再试 */
        })
    }, 1000)
    return () => clearInterval(id)
  }, [job])

  async function start() {
    setBusy(true)
    setMsg(null)
    try {
      const j = await startReorder()
      setJob(j)
      setMsg('已开始重排')
    } catch (e) {
      setMsg(e instanceof Error ? e.message : '没能开始')
    } finally {
      setBusy(false)
    }
  }

  async function cancel() {
    if (!job) return
    try {
      const r = await stopJob(job.job_id)
      setMsg(
        r.cancelled
          ? '已记下取消请求：手上那批会跑完，从下一批起不再开新的'
          : '任务已经跑完了，这次没赶上（取消只对还在跑的作业有效）',
      )
    } catch (e) {
      setMsg(e instanceof Error ? e.message : '取消失败')
    }
  }

  async function undo() {
    if (!job) return
    try {
      const r = await undoJob(job.job_id)
      setMsg(
        r.undone
          ? `已撤销这一批：${r.count} 条回到重排前的类目`
          : '这一批没什么可撤的 —— 可能之前已经撤过了',
      )
      onChanged()
    } catch (e) {
      setMsg(e instanceof Error ? e.message : '撤销失败')
    }
  }

  const running = job?.state === 'running'

  return (
    <>
      <p className="hint">
        用当前的类目判据把**已有条目**重新判一遍。新加的类目只影响以后抽出来的东西，
        已经入库的要跑这一步才会跟上。
      </p>

      <p className="hint">
        ※ 手改过类目的条目**不会被覆盖**。
      </p>

      <div className="acts">
        <button type="button" className="is-primary" disabled={busy || running} onClick={() => void start()}>
          {running ? '正在跑…' : '开始重排'}
        </button>
        {running && (
          <button type="button" disabled={busy} onClick={() => void cancel()}>
            请求取消
          </button>
        )}
        {job && !running && job.actor !== '' && (
          <button type="button" onClick={() => void undo()}>
            撤销这次重排
          </button>
        )}
      </div>

      {job && (
        <div className="job">
          <div className="job__row">
            <span className="job__state">
              {job.state === 'running'
                ? '运行中'
                : job.state === 'done'
                  ? '已完成'
                  : job.state === 'failed'
                    ? '失败'
                    : '已取消'}
            </span>
            <span className="rows__dim">
              {/* ※ `done` 是「最后一批跑成功的批号」，不是已经成功了几批。
                  原值显示，不要 +1。 */}
              {job.total > 0 ? `${job.done}/${job.total}` : '等待第一批进度…'}
            </span>
          </div>
          {job.message && <p className="job__msg">{job.message}</p>}
          {job.error && <p className="job__err">{job.error}</p>}
        </div>
      )}

      {msg && <p className="msg">{msg}</p>}
    </>
  )
}

// ── 导入导出 ────────────────────────────────────────────────

function Pack() {
  const [toml, setToml] = useState('')
  const [result, setResult] = useState<Record<string, unknown> | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function pull() {
    setBusy(true)
    setErr(null)
    try {
      const r = await getPack()
      setToml(r.toml)
    } catch (e) {
      setErr(e instanceof Error ? e.message : '导出失败')
    } finally {
      setBusy(false)
    }
  }

  function download() {
    const blob = new Blob([toml], { type: 'application/toml' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'yechao-config.toml'
    a.click()
    URL.revokeObjectURL(url)
  }

  async function push(apply: boolean) {
    setBusy(true)
    setErr(null)
    setResult(null)
    try {
      setResult(await putPack(toml, apply, true))
    } catch (e) {
      setErr(e instanceof Error ? e.message : '导入失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <p className="hint">
        导出的包里不含密钥。导入时可以选「只校验不写入」—— 它会完整读一遍配置包，
        但一个字节都不落盘。
      </p>

      <div className="acts">
        <button type="button" disabled={busy} onClick={() => void pull()}>
          {busy ? '读取中…' : '导出当前配置'}
        </button>
        {toml && <button type="button" onClick={download}>下载 .toml</button>}
      </div>

      <textarea
        className="toml"
        value={toml}
        spellCheck={false}
        placeholder="点「导出当前配置」把配置读到这里，或者把一份配置包粘进来"
        onChange={(e) => setToml(e.target.value)}
      />

      {toml && (
        <div className="acts">
          <button type="button" disabled={busy} onClick={() => void push(false)}>
            只校验
          </button>
          <button type="button" className="is-primary" disabled={busy} onClick={() => void push(true)}>
            导入并覆盖
          </button>
        </div>
      )}

      {err && <p className="msg msg--bad">{err}</p>}

      {result && (
        <div className="pack">
          <div className="pack__title">
            {result.applied ? '已导入' : '校验通过（没有写入任何东西）'}
          </div>
          <div className="pack__counts">
            群 {numOf(result.groups)} / 类目 {numOf(result.categories)} / 人物 {numOf(result.persons)} /
            别名 {numOf(result.aliases)}
          </div>
          {listOf(result.notes).map((n, i) => (
            <p key={`n${i}`}>{n}</p>
          ))}
          {listOf(result.warnings).map((w, i) => (
            <p key={`w${i}`} className="pack__warn">
              {w}
            </p>
          ))}
        </div>
      )}
    </>
  )
}

/** 服务端没给这个数时显示「—」，不补 0 —— 补 0 会被读成「包是空的」。 */
function numOf(v: unknown): string {
  return typeof v === 'number' ? String(v) : '—'
}

function listOf(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : []
}
