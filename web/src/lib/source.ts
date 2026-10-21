/**
 * 来源 slug → 中文名。
 *
 * ## 为什么在**前端**映射，而不是后端拼好一个 `source_label`
 *
 * 与 `lib/theme.ts` 里 `kindColor` / `kindIcon` 同一条判据：
 *
 *   · 类目名（`kind_label`）由**后端**算 —— 因为它是用户能改的配置，
 *     改名之后存量条目要立刻跟着变，那份「当前配置 + 别名表」只有后端有。
 *   · 源名是**闭集**（`infrastructure/sources/__init__.py` 的注册表），
 *     用户改不了、不会漂移。它的中文名是**展示映射** ——
 *     后端拼一个只有前端看得懂的字段，等于在用户数据里写前端库的命名。
 *
 * ※ 表里的七个 slug 与后端注册表一一对应。加一个源要在这里补一行；
 * 漏了**不会报错**，只会回落到「原样显示 slug」—— 而那个回落看起来是
 * 正常的（屏幕上会出现一个英文词），所以别指望它提醒你。
 */

/** 认不出来的 slug 原样显示 —— 见模块 docstring 最后一段。 */
const SOURCE_LABEL: Record<string, string> = {
  // 合成语料。离线演示那条路用的，名字要能让评委一眼看出「这批不是真源」。
  synth: '合成语料',
  qq: 'QQ 群',
  mail: '邮件',
  meeting: '会议',
  video: '视频',
  subscription: '订阅',
  web: '网页',
}

/**
 * 来源的中文名。
 *
 * ※ 空串**不是**「未知来源」，是「这条消息没标来源」——
 * `messages.source` 的默认值就是空串（见 `schema.py` 那一列的注释）。
 * 两者必须分开说：一个是数据缺一列，一个是有一个我不认识的源。
 */
export function sourceLabel(slug: string): string {
  if (!slug) return '未标来源'
  return SOURCE_LABEL[slug] ?? slug
}
