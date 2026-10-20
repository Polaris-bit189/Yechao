/**
 * 时间。凡是「算哪一天」的判断，一律以**浏览器所在时区**为准。
 *
 * 这不是细枝末节：日报通常在早上七点多翻，那个钟点在 UTC 下还落在前一天，
 * 拿 UTC 去切天，会把刚收到的内容甩到「昨天」那一栏。
 */

const p2 = (n: number) => String(n).padStart(2, '0')

/** 时间戳 → `YYYY-MM-DD`（按本地日切）。 */
export function ymd(ts: number): string {
  const d = new Date(ts * 1000)
  return `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())}`
}

/** 今天的日期串。 */
export function today(): string {
  return ymd(Math.floor(Date.now() / 1000))
}

/** `YYYY-MM-DD` → 当天零点的秒。 */
export function midnight(key: string): number {
  const [y, m, d] = key.split('-').map(Number)
  return Math.floor(new Date(y, m - 1, d).getTime() / 1000)
}

/** `M 月 D 日`。 */
export function mdText(ts: number): string {
  const d = new Date(ts * 1000)
  return `${d.getMonth() + 1} 月 ${d.getDate()} 日`
}

/** `HH:mm`。 */
export function hmText(ts: number): string {
  const d = new Date(ts * 1000)
  return `${p2(d.getHours())}:${p2(d.getMinutes())}`
}

/** `M 月 D 日 HH:mm`。 */
export function mdhmText(ts: number): string {
  return `${mdText(ts)} ${hmText(ts)}`
}

/**
 * 分天标题。
 *
 * 这里比的是**日期的差**，不是秒的差：23:59 和第二天 00:01 只隔两分钟，
 * 却分属两天。
 */
export function groupLabel(key: string): string {
  const t = today()
  if (key === t) return '今天'
  if (midnight(t) - midnight(key) === 86400) return '昨天'
  const [, m, d] = key.split('-').map(Number)
  return `${m} 月 ${d} 日`
}

/** 星期几。 */
export function weekdayOf(ts: number): string {
  return ['星期日', '星期一', '星期二', '星期三', '星期四', '星期五', '星期六'][
    new Date(ts * 1000).getDay()
  ]
}

/**
 * 「多久之前」。
 *
 * 一天之内给相对时间，再远就换成日期。「23 小时前」比一个钟点更好判断新旧，
 * 而「6 天前」读起来反倒不如日期清楚。
 */
export function ago(ts: number, now = Date.now()): string {
  const diff = Math.floor(now / 1000) - ts
  if (diff < 60) return '刚刚'
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  const days = Math.floor(diff / 86400)
  if (days <= 2) return `${days} 天前`
  return mdText(ts)
}

// ── 死线分级 ────────────────────────────────────────────────
//
// 分级边界与后端 `yechao/domain/days.py` 里的 `deadline_level` 对齐：
// <0 已过期 / 0–1 紧急 / 2–3 临近 / 4–7 普通 / >7 回落成日期。
// 前后端各留一份，是权衡之后的选择：日报正文交给后端渲染，那份分级要用得上；
// 日历得随「此刻」走动，只能由前端这一份来算。

export type DueLevel = 'expired' | 'urgent' | 'soon' | 'normal'

export function dueInfo(ts: number, now = Date.now()): { level: DueLevel; text: string } {
  const days = Math.round((midnight(ymd(ts)) - midnight(ymd(Math.floor(now / 1000)))) / 86400)
  if (days < 0) return { level: 'expired', text: `已过期 ${-days} 天` }
  if (days === 0) return { level: 'urgent', text: '今天截止' }
  if (days === 1) return { level: 'urgent', text: '明天截止' }
  if (days <= 3) return { level: 'soon', text: `还有 ${days} 天` }
  if (days <= 7) return { level: 'normal', text: `还有 ${days} 天` }
  return { level: 'normal', text: mdText(ts) }
}
