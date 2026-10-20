/** `10月20日` —— 紧凑、不带年份、给同一年内的日期用。 */
export function shortDateOf(ts: number): string {
  const d = new Date(ts * 1000)
  return `${d.getMonth() + 1}月${d.getDate()}日`
}

/** `2026年10月19日` —— 需要明确年份时用（比如群清单的「最近活跃」）。 */
export function longDateOf(ts: number): string {
  const d = new Date(ts * 1000)
  return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日`
}
