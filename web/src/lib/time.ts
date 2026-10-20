/**
 * 时间。所有「哪一天」的判断都按**浏览器本地时区**。
 *
 * 这一点不是细节：日报在早上七点多读，那个时段在 UTC 下还算前一天，
 * 用 UTC 切天会把人刚收到的东西排到「昨天」。
 */

const p2 = (n: number) => String(n).padStart(2, '0')

/** 时间戳 → `YYYY-MM-DD`（本地日）。 */
export function ymd(ts: number): string {
  const d = new Date(ts * 1000)
  return `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())}`
}

/** 今天。 */
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
 * 判据是**日期差**而不是秒差：23:59 与次日 00:01 只差两分钟，
 * 但它们属于两天。
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
 * 一天以内说相对时间，超过说日期。「23 小时前」比钟点更容易判断新旧，
 * 而「6 天前」就不如日期好读。
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
// 边界与后端 `yechao/domain/days.py` 的 `deadline_level` 一致：
// <0 已过期 / 0–1 紧急 / 2–3 临近 / 4–7 普通 / >7 回落日期。
// 两边各写一份是知情取舍：日报正文由后端渲染时要用，日历要跟「此刻」变。

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
