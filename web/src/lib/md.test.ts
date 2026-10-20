/**
 * 日报正文解析的测试。
 *
 * 这个模块输出的**是结构而不是 HTML 字符串**——正文里有模型参与生成的成分，
 * 拼 HTML 再塞进 DOM 会引入注入面。所以测试断言的也应该是结构。
 *
 * 两个「跳过」的判据最容易错，各有一组用例：
 *   1. 开头的日期大标题（页头已经显示过）
 *   2. 与预览同源的概述段（页面上已经显示过）
 * 只跳其中一样，页面上就会出现同一句话两次。
 */
import { describe, expect, it } from 'vitest'

import { parse } from './md'

describe('标题', () => {
  it('## 与 ### 分别映射到 h2 / h3', () => {
    const blocks = parse('## 通知\n\n### 小节')
    expect(blocks).toEqual([
      { t: 'h2', text: '通知' },
      { t: 'h3', text: '小节' },
    ])
  })

  it('※ 只跳开头的第一处一级标题', () => {
    // 正文中间的一级标题不该被吃掉——模板变了的时候还得显示出来。
    const blocks = parse('# 夜抄日报\n\n正文\n\n# 中途的标题')
    expect(blocks).toEqual([
      { t: 'p', text: '正文' },
      { t: 'h2', text: '中途的标题' },
    ])
  })

  it('没有开头标题时，第一个一级标题也不会被跳', () => {
    const blocks = parse('正文\n\n# 后面的标题')
    expect(blocks).toEqual([
      { t: 'p', text: '正文' },
      { t: 'h2', text: '后面的标题' },
    ])
  })
})

describe('列表', () => {
  it('连续的 - 行合成一个列表', () => {
    const blocks = parse('## 节\n\n- 第一条\n- 第二条\n- 第三条')
    expect(blocks).toEqual([
      { t: 'h2', text: '节' },
      { t: 'ul', items: ['第一条', '第二条', '第三条'] },
    ])
  })

  it('* 与 - 等价', () => {
    const blocks = parse('- 甲\n* 乙')
    expect(blocks).toEqual([{ t: 'ul', items: ['甲', '乙'] }])
  })

  it('列表被别的块打断时就分开', () => {
    const blocks = parse('- 甲\n\n段落\n\n- 乙')
    expect(blocks).toEqual([
      { t: 'ul', items: ['甲'] },
      { t: 'p', text: '段落' },
      { t: 'ul', items: ['乙'] },
    ])
  })
})

describe('引用', () => {
  it('连续的 > 行合成一段', () => {
    const blocks = parse('> 第一行\n> 第二行')
    expect(blocks).toEqual([{ t: 'quote', text: '第一行 第二行' }])
  })
})

describe('段落', () => {
  it('同一块里的多行合成一段', () => {
    const blocks = parse('第一行\n第二行')
    expect(blocks).toEqual([{ t: 'p', text: '第一行 第二行' }])
  })

  it('空行分段', () => {
    const blocks = parse('甲\n\n乙')
    expect(blocks).toEqual([
      { t: 'p', text: '甲' },
      { t: 'p', text: '乙' },
    ])
  })
})

describe('※ 跳过与预览同源的概述段', () => {
  const PREVIEW = '今天有三件事需要你注意：体检、报名、讲座。'

  it('开头对得上的那段被跳过', () => {
    const md = ['# 夜抄日报', PREVIEW, '## 通知', '- 体检'].join('\n\n')
    const blocks = parse(md, PREVIEW)
    expect(blocks).toEqual([
      { t: 'h2', text: '通知' },
      { t: 'ul', items: ['体检'] },
    ])
  })

  it('**粗体**不影响判定', () => {
    const md = `# 夜抄日报\n\n**${PREVIEW}**\n\n正文`
    const blocks = parse(md, PREVIEW)
    expect(blocks).toEqual([{ t: 'p', text: '正文' }])
  })

  it('不同源的段落不跳', () => {
    const md = '# 夜抄日报\n\n完全不同的概述。\n\n正文'
    const blocks = parse(md, PREVIEW)
    expect(blocks).toEqual([
      { t: 'p', text: '完全不同的概述。' },
      { t: 'p', text: '正文' },
    ])
  })

  it('没有 preview 时不跳任何段', () => {
    const blocks = parse('# 夜抄日报\n\n第一段\n\n第二段')
    expect(blocks).toEqual([
      { t: 'p', text: '第一段' },
      { t: 'p', text: '第二段' },
    ])
  })

  it('※ 两样都要跳，否则同一句话会出现两次', () => {
    const md = ['# 夜抄日报 · 9月16日', PREVIEW, '## 通知'].join('\n\n')
    const blocks = parse(md, PREVIEW)
    // 标题与概述都不该出现在结果里。
    expect(blocks.some((b) => b.t === 'p')).toBe(false)
  })
})

describe('空输入', () => {
  it('空串给空数组', () => {
    expect(parse('')).toEqual([])
  })

  it('只有空白给空数组', () => {
    expect(parse('\n\n   \n\n')).toEqual([])
  })
})
