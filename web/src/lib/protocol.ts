/**
 * 协议正文的解析。
 *
 * ## 为什么与 `md.ts` 分开
 *
 * 那个是**日报正文**的解析器，它服务的是「模型按模板写出来的受限记号」：
 * 只有标题、列表、引用、段落，而且开头两样（日期大标题、概述段）要**跳过**
 * —— 页头已经给过它们了。那是为那一份数据裁出来的形状。
 *
 * 这一份服务的是**人手写的协议正文**，它真的用到表格（「cookie 与账号密码
 * 的对照」那张表带着实义）与代码块（环境变量名、一段示例 TOML），还有
 * 分节用的水平线。拿日报那套来渲染，`|` 会原样出现在段落里、
 * ` ``` ` 会变成一个段落 —— 一份**要用户逐条读过才能同意**的文档被渲染成
 * 那个样子，等于把「读一遍协议」这一步从流程里拿掉。
 *
 * ⇒ 两份各自服务一种输入，不合并。合并的代价是给日报那份加上一堆
 * 它永远遇不到的记号，而日报那份的「跳过开头两样」在协议这边是错的。
 *
 * ※ 与 `md.ts` 同一条纪律：**产出结构，不产出 HTML 字符串**。
 * 正文来自仓库里的文件（不是模型），但拼 HTML 再塞进 DOM 仍然会引入
 * 一个注入面，而交给 React 渲染就没有这回事。
 */

/** 一段行内文本：普通文字 / 加粗 / 等宽。 */
export type Inline = { t: 'text' | 'b' | 'code'; text: string }

export type PBlock =
  | { t: 'h'; level: 1 | 2 | 3; spans: Inline[] }
  | { t: 'p'; spans: Inline[] }
  | { t: 'ul'; items: Inline[][] }
  | { t: 'ol'; items: Inline[][] }
  | { t: 'quote'; spans: Inline[] }
  | { t: 'hr' }
  | { t: 'table'; head: Inline[][]; rows: Inline[][][] }
  | { t: 'pre'; text: string }

/**
 * 行内记号：`**加粗**` 与 `` `等宽` ``。
 *
 * ※ 只认这两种。协议正文里没有链接与斜体，而**不认的东西原样保留** ——
 * 认不出来时当普通文字，比猜一个意思好：一份协议里被悄悄改写的字，
 * 读的人不会发现。
 */
export function inline(s: string): Inline[] {
  const out: Inline[] = []
  // 一次切出「加粗」或「等宽」两种片段，其余是普通文字。
  const re = /(\*\*[^*]+\*\*|`[^`]+`)/g
  let last = 0
  let m: RegExpExecArray | null

  while ((m = re.exec(s)) !== null) {
    if (m.index > last) out.push({ t: 'text', text: s.slice(last, m.index) })
    const tok = m[0]
    if (tok.startsWith('**')) out.push({ t: 'b', text: tok.slice(2, -2) })
    else out.push({ t: 'code', text: tok.slice(1, -1) })
    last = m.index + tok.length
  }
  if (last < s.length) out.push({ t: 'text', text: s.slice(last) })

  // 空串也给一个空片段：调用方一律 `spans.map(...)`，返回空数组时
  // 那一格会渲染成什么都没有 —— 表格里那一格就塌了（行高不一致）。
  return out.length ? out : [{ t: 'text', text: '' }]
}

/** `| a | b |` → 两格。分隔行（`|---|---|`）不走这里。
 *
 * ※ 一格是**一串行内片段**（`Inline[]`）不是一段文字 —— 协议那张对照表
 * 里有 `` `X-s` `` 这样的等宽片段，当纯文字渲染会把反引号原样显示出来。 */
function cells(line: string): Inline[][] {
  return line
    .replace(/^\||\|$/g, '')
    .split('|')
    .map((c) => inline(c.trim()))
}

/** 表格分隔行：`|---|---|`、`|:--|--:|` 都算。 */
const TABLE_SEP = /^\|[\s:|-]+\|$/

/** 水平线：三个以上的 `-` / `*` / `_`，**独立成行**。 */
const HR = /^(-{3,}|\*{3,}|_{3,})$/

/** 列表项：无序 `- ` / `* `，有序 `1. ` / `1) `。 */
const UL = /^[-*]\s+(.*)$/
const OL = /^\d+[.)]\s+(.*)$/

/**
 * 协议正文 → 块序列。
 *
 * 按**行**扫而不是按空行切段：表格与代码块内部都有空行/换行，
 * 先按空行切会把它们切碎 —— 而切碎之后每一块看起来都还「像那么回事」。
 */
export function parseProtocol(md: string): PBlock[] {
  const lines = md.split('\n')
  const out: PBlock[] = []
  let para: string[] = []
  let i = 0

  const flush = () => {
    if (para.length) {
      out.push({ t: 'p', spans: inline(para.join(' ')) })
      para = []
    }
  }

  while (i < lines.length) {
    const s = lines[i].trim()

    if (!s) {
      flush()
      i++
      continue
    }

    // 代码块。原样收，不做行内解析 —— 里面的反引号是内容的一部分。
    if (s.startsWith('```')) {
      flush()
      const buf: string[] = []
      i++
      while (i < lines.length && !lines[i].trim().startsWith('```')) {
        buf.push(lines[i])
        i++
      }
      i++ // 吃掉收尾那一行
      out.push({ t: 'pre', text: buf.join('\n') })
      continue
    }

    if (HR.test(s)) {
      flush()
      out.push({ t: 'hr' })
      i++
      continue
    }

    const h = /^(#{1,4})\s+(.*)$/.exec(s)
    if (h) {
      flush()
      // `####` 及更深当三级 —— 这个页面只有三级标题的字号阶。
      const level = Math.min(h[1].length, 3) as 1 | 2 | 3
      out.push({ t: 'h', level, spans: inline(h[2]) })
      i++
      continue
    }

    // 表格：本行以 `|` 开头，**且下一行是分隔行**。
    // 只看本行是不够的 —— 正文里一句以 `|` 开头的句子会被吃成表格头。
    if (s.startsWith('|') && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1].trim())) {
      flush()
      const head = cells(s)
      i += 2
      const rows: Inline[][][] = []
      while (i < lines.length && lines[i].trim().startsWith('|')) {
        rows.push(cells(lines[i].trim()))
        i++
      }
      out.push({ t: 'table', head, rows })
      continue
    }

    if (s.startsWith('>')) {
      flush()
      const buf: string[] = []
      while (i < lines.length && lines[i].trim().startsWith('>')) {
        buf.push(lines[i].trim().replace(/^>\s?/, ''))
        i++
      }
      out.push({ t: 'quote', spans: inline(buf.join(' ')) })
      continue
    }

    const isUl = UL.test(s)
    const isOl = OL.test(s)
    if (isUl || isOl) {
      flush()
      const items: string[][] = []
      const pick = isOl ? OL : UL

      while (i < lines.length) {
        const cur = lines[i].trim()
        if (!cur) break

        const m = pick.exec(cur)
        if (m) {
          items.push([m[1]])
          i++
          continue
        }

        // 续行：接在**上一条**后面。
        //
        // ※ 这是必须的：正文里的列表项是**手写的折行**（一句话写两行），
        // 不接起来的话，每个列表项的第二行都会变成一个独立段落，
        // 而它读起来仍然通顺 —— 只是意思断在中间。
        if (items.length && !UL.test(cur) && !OL.test(cur) && !/^(#{1,4}\s|>|\||```)/.test(cur)) {
          items[items.length - 1].push(cur)
          i++
          continue
        }
        break
      }

      out.push({ t: isOl ? 'ol' : 'ul', items: items.map((x) => inline(x.join(' '))) })
      continue
    }

    para.push(s)
    i++
  }

  flush()
  return out
}
