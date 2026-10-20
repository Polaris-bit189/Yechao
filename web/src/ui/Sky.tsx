import type { CSSProperties } from 'react'
import type { ThemeId } from '../lib/theme'

/**
 * 背景粒子。
 *
 * 数量按规格：宵 8 颗星、雾 6 团雾、月 4 片云、樱 14 片花瓣、
 * 幽 10 只萤火、古登堡 6 块活字。
 *
 * 位置与周期**写死**而不是随机：随机每次渲染都变，切一下主题再切回来
 * 所有粒子会跳位置。写死的轨迹切回来还是原样，而稳定本身就是这个界面
 * 想要的质感。
 */

type Dot = { style: CSSProperties }

// [left, top, size, cycle, extra]
const STAR = [
  ['78%', '6%', '11px', '8s'],
  ['86%', '14%', '8px', '11s'],
  ['70%', '19%', '7px', '9.5s'],
  ['91%', '25%', '9px', '13s'],
  ['64%', '31%', '6px', '10s'],
  ['80%', '39%', '8px', '12s'],
  ['88%', '47%', '6px', '14s'],
  ['72%', '53%', '7px', '11.5s'],
] as const

const MIST = [
  ['3%', '18%', '200px', '52px', '96s'],
  ['22%', '36%', '160px', '44px', '124s'],
  ['51%', '24%', '220px', '58px', '108s'],
  ['67%', '50%', '176px', '46px', '138s'],
  ['8%', '60%', '168px', '42px', '113s'],
  ['39%', '72%', '210px', '54px', '131s'],
] as const

const CLOUD = [
  ['12%', '9%', '320px', '28px', '116s'],
  ['38%', '17%', '268px', '24px', '142s'],
  ['59%', '6%', '300px', '26px', '127s'],
  ['20%', '25%', '284px', '26px', '152s'],
] as const

// 花瓣：横摆幅度分三档，交错分配成三条轨迹。
const PETAL = [
  ['5%', '7px', '23s', '22px'],
  ['14%', '5px', '28s', '34px'],
  ['24%', '8px', '25s', '14px'],
  ['33%', '6px', '31s', '30px'],
  ['41%', '7px', '22s', '18px'],
  ['50%', '5px', '29s', '38px'],
  ['58%', '8px', '26s', '12px'],
  ['66%', '6px', '32s', '26px'],
  ['74%', '7px', '24s', '20px'],
  ['82%', '5px', '30s', '36px'],
  ['90%', '8px', '27s', '16px'],
  ['11%', '6px', '34s', '28px'],
  ['47%', '5px', '36s', '32px'],
  ['70%', '7px', '35s', '24px'],
] as const

const FLY = [
  ['11%', '73%', '4px', '9s', '16px'],
  ['21%', '85%', '3px', '13s', '24px'],
  ['30%', '67%', '5px', '11s', '12px'],
  ['39%', '89%', '3px', '15s', '20px'],
  ['48%', '77%', '4px', '10s', '28px'],
  ['57%', '91%', '3px', '14s', '14px'],
  ['65%', '71%', '5px', '12s', '22px'],
  ['74%', '83%', '3px', '16s', '18px'],
  ['83%', '75%', '4px', '11.5s', '26px'],
  ['91%', '87%', '3px', '17s', '15px'],
] as const

const TYPE = [
  ['13%', '11px', '43s'],
  ['27%', '9px', '53s'],
  ['40%', '13px', '47s'],
  ['55%', '10px', '59s'],
  ['68%', '12px', '50s'],
  ['83%', '9px', '56s'],
] as const

function dotsFor(theme: ThemeId): Dot[] {
  const mk = (left: string, top: string, vars: Record<string, string>) => ({
    style: { left, top, ...vars } as CSSProperties,
  })

  switch (theme) {
    case 'yoi':
      return STAR.map(([left, top, w, cycle], i) =>
        mk(left, top, { '--w': w, '--cycle': cycle, '--lag': `${i * 1.3}s`, '--dur': cycle }),
      )
    case 'kiri':
      return MIST.map(([left, top, w, h, cycle], i) =>
        mk(left, top, { '--w': w, '--h': h, '--cycle': cycle, '--lag': `${-i * 14}s` }),
      )
    case 'tsuki':
      return CLOUD.map(([left, top, w, h, cycle], i) =>
        mk(left, top, { '--w': w, '--h': h, '--cycle': cycle, '--lag': `${-i * 29}s` }),
      )
    case 'sakura':
      return PETAL.map(([left, w, cycle, sway], i) =>
        mk(left, '0%', {
          '--w': w,
          '--cycle': cycle,
          '--sway': sway,
          '--lag': `${-i * 2.7}s`,
        }),
      )
    case 'yuu':
      return FLY.map(([left, top, w, cycle, sway], i) =>
        mk(left, top, {
          '--w': w,
          '--cycle': cycle,
          '--sway': sway,
          '--lag': `${-i * 1.9}s`,
        }),
      )
    case 'katsuji':
      return TYPE.map(([left, w, cycle], i) =>
        mk(left, '0%', { '--w': w, '--cycle': cycle, '--lag': `${-i * 9}s` }),
      )
    default:
      return []
  }
}

export function Sky({ theme }: { theme: ThemeId }) {
  const dots = dotsFor(theme)

  return (
    <div className="sky" aria-hidden="true">
      {dots.map((d, i) => (
        <span key={`${theme}-${i}`} className="sky__dot" style={d.style} />
      ))}
    </div>
  )
}
