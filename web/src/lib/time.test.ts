/**
 * 时间工具的测试。
 *
 * 重点在 `dueInfo` —— 它和后端 `yechao/domain/days.py` 的 `deadline_level`
 * 是**同一套边界的两份实现**：日报正文由后端渲染时要用，日历要跟着「此刻」变，
 * 所以两边各有一份。
 *
 * 两份实现的风险只有一个：**漂移**。而漂移的症状是「日历上标红了、日报里却
 * 没提」——单看任何一边都正常。所以边界值在这里逐条钉住。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ago, dueInfo, groupLabel, hmText, mdText, midnight, weekdayOf, ymd } from './time'

/** 用本地时间构造，避免用例随机器时区漂移。 */
const at = (y: number, m: number, d: number, h = 0, min = 0): number =>
  Math.floor(new Date(y, m - 1, d, h, min).getTime() / 1000)

const NOW = at(2026, 9, 16, 10)

describe('ymd / midnight', () => {
  it('时间戳换成 YYYY-MM-DD', () => {
    expect(ymd(at(2026, 9, 16, 8, 5))).toBe('2026-09-16')
  })

  it('月与日补零', () => {
    expect(ymd(at(2026, 1, 2))).toBe('2026-01-02')
  })

  it('日期串换回当天零点', () => {
    expect(midnight('2026-09-16')).toBe(at(2026, 9, 16))
  })

  it('往返一致', () => {
    const key = '2026-03-01'
    expect(ymd(midnight(key))).toBe(key)
  })
})

describe('mdText / hmText', () => {
  it('月日', () => {
    expect(mdText(at(2026, 9, 16))).toBe('9 月 16 日')
  })

  it('时刻补零', () => {
    expect(hmText(at(2026, 9, 16, 8, 5))).toBe('08:05')
  })
})

describe('weekdayOf', () => {
  it('2026-09-16 是星期三', () => {
    expect(weekdayOf(at(2026, 9, 16))).toBe('星期三')
  })

  it('星期日不会被写成星期七', () => {
    expect(weekdayOf(at(2026, 9, 20))).toBe('星期日')
  })
})

describe('groupLabel', () => {
  // `groupLabel` 内部读**当前时间**，所以这里必须冻结时钟——
  // 否则用例会随运行日期漂移，今天绿、明天红。
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date(2026, 8, 16, 10))
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('今天与昨天用词说', () => {
    expect(groupLabel('2026-09-16')).toBe('今天')
    expect(groupLabel('2026-09-15')).toBe('昨天')
  })

  it('更早的用日期', () => {
    expect(groupLabel('2026-09-10')).toBe('9 月 10 日')
  })

  it('⚠️ 按日期差判，不按秒差', () => {
    // 23:59 与次日 00:01 只差两分钟，但它们是两天。
    expect(groupLabel('2026-09-14')).not.toBe('今天')
    expect(groupLabel('2026-09-14')).not.toBe('昨天')
  })
})

describe('dueInfo —— 与后端共用的边界', () => {
  it('过期', () => {
    expect(dueInfo(at(2026, 9, 15), NOW * 1000)).toEqual({
      level: 'expired',
      text: '已过期 1 天',
    })
  })

  it('⭐ 边界：0 天与 1 天都是 urgent', () => {
    expect(dueInfo(at(2026, 9, 16), NOW * 1000).level).toBe('urgent')
    expect(dueInfo(at(2026, 9, 17), NOW * 1000).level).toBe('urgent')
  })

  it('⭐ 边界：2 天起离开 urgent', () => {
    expect(dueInfo(at(2026, 9, 18), NOW * 1000).level).toBe('soon')
  })

  it('⭐ 边界：3 天仍是 soon，4 天进入 normal', () => {
    expect(dueInfo(at(2026, 9, 19), NOW * 1000).level).toBe('soon')
    expect(dueInfo(at(2026, 9, 20), NOW * 1000).level).toBe('normal')
  })

  it('⭐ 边界：7 天仍是「还有 N 天」，8 天起回落绝对日期', () => {
    expect(dueInfo(at(2026, 9, 23), NOW * 1000).text).toBe('还有 7 天')
    expect(dueInfo(at(2026, 9, 24), NOW * 1000).text).toBe('9 月 24 日')
  })

  it('⚠️ 按本地日对齐，不按秒差', () => {
    // 今天 10:00 看明天 09:00：只差 23 小时，但它是「明天」。
    expect(dueInfo(at(2026, 9, 17, 9), NOW * 1000).text).toBe('明天截止')
  })
})

describe('ago', () => {
  it('一分钟内说刚刚', () => {
    expect(ago(NOW - 30, NOW * 1000)).toBe('刚刚')
  })

  it('分钟级', () => {
    expect(ago(NOW - 300, NOW * 1000)).toBe('5 分钟前')
  })

  it('小时级', () => {
    expect(ago(NOW - 7200, NOW * 1000)).toBe('2 小时前')
  })

  it('两天内说天数', () => {
    expect(ago(NOW - 86400, NOW * 1000)).toBe('1 天前')
    expect(ago(NOW - 2 * 86400, NOW * 1000)).toBe('2 天前')
  })

  it('超过两天回落日期', () => {
    expect(ago(NOW - 5 * 86400, NOW * 1000)).toBe('9 月 11 日')
  })
})
