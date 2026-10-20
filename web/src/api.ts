/**
 * 后端接口。
 *
 * 字段形状全部取自 `yechao/interfaces/view.py` 与 `api.py` —— 读的是
 * 本仓库自己的后端代码，不是别处。
 */

export interface Item {
  item_id: number
  /** 类目 slug。判据是「这条消息**要读者做什么**」—— 见后端 categories.py。 */
  kind: string
  kind_label: string
  title: string
  detail: string | null
  event_ts: number
  deadline_ts: number | null
  group_id: number
  group_name: string
  actor: string | null
  /** 账号（源系统里的标识）。匿名条目为 null。 */
  actor_account: number | null
  place: string | null
  amount: string | null
  links: string[]
  source_count: number
  start_ts: number | null
}

export interface Source {
  msg_id: number
  ts: number
  sender: string
  group_name: string
  content: string
}

export interface ItemDetail extends Item {
  sources: Source[]
}

export interface ItemPage {
  items: Item[]
  total: number
  limit: number
  offset: number
}

export interface ConflictSide {
  label: string
  start: number
  end: number
  item_id: number | null
  kind: 'activity' | 'course' | 'task'
}

export interface Conflict {
  a: ConflictSide
  b: ConflictSide
  /** 重叠分钟数 —— 按「假定活动 90 分钟」推的，界面不显示。 */
  minutes: number
}

export interface CalendarPage {
  items: Item[]
  total: number
  duplicates: Record<string, number>
  conflicts: Conflict[]
  course_conflicts: Array<Conflict | { error: string }>
}

export interface UrgentPage {
  items: Item[]
  total: number
}

export interface CategoryDetail {
  slug: string
  label: string
  /** 判据，逐字进提示词。 */
  desc: string
  count: number
}

export interface CategoriesResponse {
  categories: CategoryDetail[]
  aliases: Record<string, string>
}

export interface DigestSummary {
  digest_id: number
  day: string
  window_from: number
  window_to: number
  created_at: number
  item_count: number
  preview?: string
}

export interface DigestDetail extends DigestSummary {
  body_md: string
  items: Item[]
}

export interface Person {
  /** 账号（源系统里的标识）。后端叫 `account`，不叫某个平台的原生词。 */
  account: number
  label: string
  note: string
  count: number
}

export interface GroupRow {
  id: number
  name: string
  messages: number
  last_ts: number
  watched: boolean
  known: boolean
}

export interface GroupsResponse {
  available: boolean
  reason: string
  generated_at: number | null
  groups: GroupRow[]
}

export interface LlmSettings {
  base_url: string
  model: string
  /** null = 请求里不带这个字段。与 false 不是一回事。 */
  enable_thinking: boolean | null
}

export interface JobRow {
  job_id: string
  kind: string
  state: 'running' | 'done' | 'failed' | 'cancelled'
  total: number
  /** 最后成功那一批的序号，不是已完成批次数。 */
  done: number
  message: string
  error: string
  actor: string
  started_at: number
  finished_at: number | null
}

export interface PrivacyDirty {
  surface: string
  batch: number
  name_hits: number
  num_hits: number
  sha256: string
}

export interface PrivacyResponse {
  records: number
  files: number
  surfaces: Record<string, number>
  messages: number
  chars: number
  known_name_hits: number
  names_checked: number
  names_were_checked: boolean
  num_hits: number
  name_placeholders: number
  num_placeholders: number
  clean: boolean
  dirty: PrivacyDirty[]
  hint: string | null
}

export interface ItemQuery {
  kinds?: string[]
  persons?: number[]
  since?: string
  until?: string
  group?: number
  q?: string
  limit?: number
  offset?: number
}

export class ApiError extends Error {
  readonly status: number
  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

/* 只有 5xx 与网络层失败算后端不可达。4xx 说明服务器是活的，
 * 只是这个请求有问题 —— 把它算成掉线，用户会在一次 404 之后
 * 看到一个莫名其妙的离线提示。 */
let alive = true
const watchers = new Set<(v: boolean) => void>()

export function backendAlive(): boolean {
  return alive
}

export function watchBackend(fn: (v: boolean) => void): () => void {
  watchers.add(fn)
  return () => {
    watchers.delete(fn)
  }
}

function setAlive(v: boolean): void {
  if (alive === v) return
  alive = v
  for (const fn of watchers) fn(v)
}

async function call<T>(url: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(url, init)
  } catch {
    setAlive(false)
    throw new ApiError('请求没能发出去，检查服务是否在运行', 0)
  }

  setAlive(res.status < 500)

  if (!res.ok) {
    let text = `HTTP ${res.status}`
    try {
      const body = (await res.json()) as { detail?: unknown }
      if (typeof body.detail === 'string') text = body.detail
    } catch {
      // 后端没按 JSON 回错误，用状态码。
    }
    throw new ApiError(text, res.status)
  }

  // 204 没有响应体，解析会抛。
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

const get = <T>(url: string) => call<T>(url)

function send<T>(url: string, method: string, body?: unknown): Promise<T> {
  // 没有 body 时不要带 Content-Type：声明了 JSON 却给空体，
  // 服务端读体时抛的错和真实原因无关。
  const has = body !== undefined
  return call<T>(url, {
    method,
    headers: has ? { 'Content-Type': 'application/json' } : undefined,
    body: has ? JSON.stringify(body) : undefined,
  })
}

export function getItems(q: ItemQuery): Promise<ItemPage> {
  const sp = new URLSearchParams()
  // 同一维度多个值必须 append。用 set 会让最后一个覆盖前面的，
  // 「选了三个类目」发出去变成「只筛最后一个」，而页面不报错。
  for (const k of q.kinds ?? []) if (k) sp.append('kind', k)
  for (const p of q.persons ?? []) sp.append('person', String(p))
  if (q.since) sp.set('since', q.since)
  if (q.until) sp.set('until', q.until)
  if (q.group !== undefined) sp.set('group', String(q.group))
  if (q.q) sp.set('q', q.q)
  if (q.limit !== undefined) sp.set('limit', String(q.limit))
  if (q.offset !== undefined) sp.set('offset', String(q.offset))
  return get<ItemPage>(`/api/items?${sp.toString()}`)
}

export const getItem = (id: number) => get<ItemDetail>(`/api/items/${id}`)

export const patchKind = (id: number, kind: string) =>
  send<{ edit_id: number }>(`/api/items/${id}`, 'PATCH', { kind })

export const dropItem = (id: number) => send<{ edit_id: number }>(`/api/items/${id}`, 'DELETE')

export const stepBack = (editId?: number) =>
  send<{ undone: boolean }>('/api/undo', 'POST', { edit_id: editId ?? null })

export const bulkPatchKind = (ids: number[], kind: string) =>
  send<{ actor: string; count: number }>('/api/items/batch-kind', 'POST', {
    item_ids: ids,
    kind,
  })

export const bulkStepBack = (actor: string) =>
  send<{ undone: boolean; count: number }>('/api/undo/batch', 'POST', { actor })

export function getCalendar(range?: { since?: string; until?: string }): Promise<CalendarPage> {
  const sp = new URLSearchParams()
  if (range?.since) sp.set('since', range.since)
  if (range?.until) sp.set('until', range.until)
  const tail = sp.toString()
  return get<CalendarPage>(`/api/calendar${tail ? `?${tail}` : ''}`)
}

export const getUrgent = () => get<UrgentPage>('/api/urgent')

export const getCategories = () => get<CategoriesResponse>('/api/categories')

export const newCategory = (slug: string, label: string, desc: string) =>
  send<{ slug: string }>('/api/categories', 'POST', { slug, label, desc })

// slug 在路径里，要转义。同一组的 dropCategory 也必须同样处理 ——
// 一处编码一处不编码，照着哪一处写都说得通。
export const patchCategory = (
  slug: string,
  patch: { slug?: string; label?: string; desc?: string },
) => send<{ slug: string }>(`/api/categories/${encodeURIComponent(slug)}`, 'PUT', patch)

export const dropCategory = (slug: string, migrateTo?: string) => {
  const tail = migrateTo ? `?migrate_to=${encodeURIComponent(migrateTo)}` : ''
  return send<void>(`/api/categories/${encodeURIComponent(slug)}${tail}`, 'DELETE')
}

export const stepBackCategory = () =>
  send<{ undone: boolean }>('/api/categories/undo', 'POST')

export const getDigests = () => get<{ digests: DigestSummary[] }>('/api/digests')

export const getDigest = (id: number) => get<DigestDetail>(`/api/digests/${id}`)

export const getPersons = () => get<{ persons: Person[] }>('/api/persons')

export const newPerson = (account: number, label: string, note = '') =>
  send<{ account: number; label: string }>('/api/persons', 'POST', {
    account,
    label,
    note,
  })

export const dropPerson = (account: number) =>
  send<void>(`/api/persons/${account}`, 'DELETE')

export const getGroups = () => get<GroupsResponse>('/api/groups')

export const newGroup = (id: number, name: string, tier: string) =>
  send<{ id: number }>('/api/groups', 'POST', { id, name, tier })

export const patchGroup = (
  id: number,
  patch: { name?: string; enabled?: boolean; tier?: string },
) => send<{ id: number }>(`/api/groups/${id}`, 'PATCH', patch)

export const dropGroup = (id: number) => send<void>(`/api/groups/${id}`, 'DELETE')

export const getLlm = () => get<LlmSettings>('/api/llm')

export const putLlm = (s: LlmSettings) => send<LlmSettings>('/api/llm', 'PUT', s)

export const startReorder = () => send<JobRow>('/api/jobs', 'POST')

export const getJob = (id: string) => get<JobRow>(`/api/jobs/${id}`)

// 这个接口回的是 {cancelled, state}，不是完整 JobRow。
export const stopJob = (id: string) =>
  send<{ cancelled: boolean; state: JobRow['state'] }>(`/api/jobs/${id}/cancel`, 'POST')

export const undoJob = (id: string) =>
  send<{ undone: boolean; count: number }>(`/api/jobs/${id}/undo`, 'POST')

export const getPack = () => get<{ toml: string }>('/api/config/export')

export const putPack = (toml: string, apply: boolean, keepSettings: boolean) =>
  send<Record<string, unknown>>('/api/config/import', 'POST', {
    toml,
    apply,
    keep_settings: keepSettings,
  })

export const getPrivacy = () => get<PrivacyResponse>('/api/privacy')
