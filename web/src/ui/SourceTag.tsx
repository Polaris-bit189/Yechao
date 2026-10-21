import { sourceLabel } from '../lib/source'

/**
 * 一枚来源标记 —— 这条消息是从哪儿读来的。
 *
 * ## 为什么要有它
 *
 * 库里同时躺着七个源的消息（QQ 群、邮件、会议、视频、订阅、网页、合成语料），
 * 但界面上**分不出哪条来自哪个** —— 「多源」这件事在 UI 上是隐形的，
 * 只能靠 README 里的一句话。一枚标记把它变成看得见的事实。
 *
 * ※ 它是**中性**的：不按来源上色。按来源配色会让七个源争抢注意力，
 * 而这一屏真正要区分的是**类目**（那个已经有色了）——
 * 两套配色叠在一起，两边都读不出来。
 */
export function SourceTag({ source }: { source: string }) {
  return (
    <span className={source ? 'src' : 'src src--none'} title={source || '这条消息没标来源'}>
      {sourceLabel(source)}
    </span>
  )
}
