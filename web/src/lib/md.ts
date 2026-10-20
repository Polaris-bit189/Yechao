/**
 * 日报正文的解析。
 *
 * 正文只用到四种记号：`#`/`##`/`###` 标题、`-` 列表、`>` 引用、段落。
 * 为一个这样受限的需求引 Markdown 库，等于让每次 clone 都多一份依赖。
 *
 * 输出结构而不是 HTML 字符串：正文由模型参与生成，拼 HTML 再塞进 DOM
 * 会引入注入面。交给 React 渲染就没有这回事。
 */

export type Block =
  | { t: 'h2' | 'h3'; text: string }
  | { t: 'ul'; items: string[] }
  | { t: 'quote'; text: string }
  | { t: 'p'; text: string }

/**
 * @param md      正文
 * @param preview 后端给的摘要（可能缺）
 *
 * 正文开头有两样在页面上已经出现过：日期大标题（页头就是它）、
 * 紧随的概述段（与 `preview` 是同一句）。两样都跳过，而且**两样都要跳**
 * —— 只跳其中一样，页面上就会出现同一句话两次。
 */
export function parse(md: string, preview?: string): Block[] {
  const chunks = md.split(/\n{2,}/)
  const out: Block[] = []

  let h1Pending = true
  let leadPending = Boolean(preview)
  const head = preview ? preview.slice(0, 12) : ''

  for (const raw of chunks) {
    const c = raw.trim()
    if (!c) continue

    // 开头的日期大标题。只跳第一处 —— 正文中间的一级标题不动。
    if (h1Pending && /^#\s/.test(c)) {
      h1Pending = false
      continue
    }
    h1Pending = false

    // 与预览同源的那一段。判据是开头十来个字对得上。
    //
    // ⚠️ 「是不是块标记」要按 `-# ` 后面**带空格**判，不能只看首字符：
    // `**粗体**` 也以 `*` 开头，但它是一段正文。只看首字符的话，模型把概述
    // 加粗那天这段就跳不过去 —— 页面上同一句话出现两遍，而且不报错。
    if (
      leadPending &&
      !/^(#{1,3}\s|[-*]\s|>)/.test(c) &&
      c.replace(/\*\*/g, '').startsWith(head)
    ) {
      leadPending = false
      continue
    }
    leadPending = false

    out.push(...parseChunk(c))
  }

  return out
}

function parseChunk(chunk: string): Block[] {
  const lines = chunk.split('\n')
  const out: Block[] = []
  let i = 0

  while (i < lines.length) {
    const line = lines[i].trim()
    if (!line) {
      i++
      continue
    }

    const h = /^(#{2,3})\s+(.*)$/.exec(line)
    if (h) {
      out.push({ t: h[1].length === 2 ? 'h2' : 'h3', text: stripLeadMark(h[2]) })
      i++
      continue
    }

    // 跳过逻辑之后仍可能遇到一级标题（模板变了）。当二级处理。
    const h1 = /^#\s+(.*)$/.exec(line)
    if (h1) {
      out.push({ t: 'h2', text: stripLeadMark(h1[1]) })
      i++
      continue
    }

    if (/^[-*]\s+/.test(line)) {
      const items: string[] = []
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*[-*]\s+/, '').trim())
        i++
      }
      out.push({ t: 'ul', items })
      continue
    }

    if (/^>\s?/.test(line)) {
      const buf: string[] = []
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        buf.push(lines[i].replace(/^\s*>\s?/, '').trim())
        i++
      }
      out.push({ t: 'quote', text: buf.join(' ') })
      continue
    }

    const buf: string[] = []
    while (
      i < lines.length &&
      lines[i].trim() &&
      !/^(#{1,3}\s|[-*]\s|>)/.test(lines[i].trim())
    ) {
      buf.push(lines[i].trim())
      i++
    }
    if (buf.length) out.push({ t: 'p', text: buf.join(' ') })
  }

  return out
}

/**
 * 去掉标题前面的符号字符。
 *
 * 模板偶尔在小节标题上挂个图标字符。这个界面的分节已经由字号与留白
 * 表达，再挂符号是多余的。只剥开头那段，正文不动；剥空了还原原样，
 * 免得留下一个空标题。
 */
export function stripLeadMark(text: string): string {
  const cut = text.replace(
    /^(?:[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}\u{2B00}-\u{2BFF}\u{FE0F}\u{200D}\u{1F1E6}-\u{1F1FF}]+|\s)+/u,
    '',
  )
  return cut.trim() || text
}
