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

/**
 * 远山。
 *
 * ※ 为什么用 SVG 元素而不是 CSS 的 `background-image: url("data:...")`：
 *   data-URI 里的颜色**没法跟随 CSS 变量** —— 那样六套主题就得写六份山。
 *   SVG 元素的 `fill: currentColor` 让颜色由 CSS 的 `color` 决定，
 *   一份路径数据、六种山色。
 *
 * ※ `preserveAspectRatio="none"` 是刻意的：山脊的起伏是相对的，被拉宽一点
 *   不影响观感，但**必须**铺满 —— 露出一条边会像贴纸。
 *
 * 每套两层（`-b` 远、`-a` 近），用透明度做空气透视。这是水墨里
 * 「远山如黛」的画法，也是让背景**长出纵深**的关键：原来六套主题都只有
 * 一层渐变 + 粒子，等于只有近景，所以看着平。
 */
const RIDGE = {
  /** 连绵起伏 —— 宵、樱。 */
  rolling: {
    back: 'M0 262 C 150 214 250 246 372 222 S 574 178 716 212 S 918 254 1062 216 S 1286 182 1440 226 L1440 420 L0 420 Z',
    front:
      'M0 312 C 168 278 268 302 412 282 S 636 248 780 280 S 984 312 1128 288 S 1348 262 1440 292 L1440 420 L0 420 Z',
  },
  /** 尖峰 —— 雾、月。比 rolling 陡，山形更硬。 */
  peaks: {
    back: 'M0 286 L 186 196 L 306 254 L 468 156 L 628 244 L 790 184 L 968 262 L 1128 204 L 1306 268 L1440 224 L1440 420 L0 420 Z',
    front:
      'M0 334 L 204 268 L 344 308 L 506 238 L 686 302 L 846 252 L 1024 316 L 1184 272 L 1362 322 L1440 296 L1440 420 L0 420 Z',
  },
  /** 平缓地平线 —— 幽。那里是水面，不该有山。 */
  flat: {
    back: 'M0 330 C 240 312 420 326 720 318 S 1200 306 1440 320 L1440 420 L0 420 Z',
    front:
      'M0 366 C 260 352 480 362 760 356 S 1220 348 1440 358 L1440 420 L0 420 Z',
  },
} as const

type RidgeKind = keyof typeof RIDGE

/** 主题 → 画哪套山。古登堡返回 null：它已经有走纸格线那一层了。 */
function sceneOf(theme: ThemeId): RidgeKind | null {
  switch (theme) {
    case 'yoi':
    case 'sakura':
      return 'rolling'
    case 'kiri':
    case 'tsuki':
      return 'peaks'
    case 'yuu':
      return 'flat'
    default:
      return null
  }
}

function Scene({ theme }: { theme: ThemeId }) {
  const kind = sceneOf(theme)
  if (!kind) return null
  const r = RIDGE[kind]
  return (
    <svg
      className="sky__far"
      viewBox="0 0 1440 420"
      preserveAspectRatio="none"
      aria-hidden="true"
    >
      <path className="sky__far-b" d={r.back} />
      <path className="sky__far-a" d={r.front} />
    </svg>
  )
}

/**
 * 角落里的两件东西：一方朱印、两道符箓。
 *
 * ※ 为什么挑这两样 —— 它们是**中华道教的物件**（印章、符箓），同时也正是
 *   那部作品里最反复出现的东西。不知情的人看到的是「国风」，知情的人看到的
 *   是别的。这套界面要的就是这个：**中华元素是真的，梗藏在底下**。
 *
 * ※ 位置在**内容区右侧的留白**里。卡片盖不到那儿，所以它真的看得见 ——
 *   上一版的远山栽的就是这一点：画得再用心，被卡片压住等于没画。
 *   窄屏没有这块留白，由 CSS 收掉。
 */
function Corner() {
  return (
    <div className="sky__corner" aria-hidden="true">
      <svg className="sky__talisman sky__talisman--a" viewBox="0 0 26 92">
        <rect x="0.5" y="0.5" width="25" height="91" rx="1.5" />
        <path d="M5 16h16M5 26h16M5 40h16M5 50h16M5 64h16M5 74h16" />
        <path d="M13 16v58" />
      </svg>
      <svg className="sky__talisman sky__talisman--b" viewBox="0 0 26 92">
        <rect x="0.5" y="0.5" width="25" height="91" rx="1.5" />
        <path d="M5 20h16M5 34h16M5 48h16M5 68h16" />
        <path d="M9 20v48M17 20v48" />
      </svg>
      <div className="sky__seal">夜抄</div>
    </div>
  )
}

export function Sky({ theme }: { theme: ThemeId }) {
  const dots = dotsFor(theme)

  return (
    <div className="sky" aria-hidden="true">
      <Scene theme={theme} />
      <Corner />

      {dots.map((d, i) => (
        <span key={`${theme}-${i}`} className="sky__dot" style={d.style} />
      ))}

      {/* 宣纸颗粒 + 暗角。放在最后 = 压在最上层，六套主题共用，
          所以它不参与「每套主题各不相同」那件事。 */}
      <div className="sky__grain" />
    </div>
  )
}
