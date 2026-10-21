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
 * 古登堡的十字星屑坐标（1440×1000 的画布上）。
 *
 * 写死而不是随机 —— 与粒子同一个理由：随机每次渲染都变，切一下主题再切回来
 * 位置就跳了，而稳定本身就是这个界面想要的质感。
 */
// [x, y, 半径]
//
// ※ 尺寸收过一轮：原来 4–9（直径 8–18px），单颗在屏幕上比一行小字还宽，
//   读起来是「一块块的亮点」而不是星屑。现在 3–6.5。
const STARDUST = [
  [104, 268, 6.5], [286, 132, 3], [430, 356, 5], [572, 196, 3.5],
  [716, 442, 6], [858, 288, 3], [1002, 158, 4.5], [1146, 372, 3.5],
  [1290, 226, 6.5], [1396, 456, 3], [176, 552, 4], [352, 668, 5.5],
  [516, 588, 3], [678, 726, 5], [840, 618, 3.5], [1004, 752, 6],
  [1168, 648, 3], [1332, 786, 4.5],
] as const


/**
 * 流星群。五条大致平行的弧，各自错开 1.8 秒 —— 单颗流星在 9 秒里只露脸
 * 一秒出头，观感上就是「偶尔划一下」；成群之后才有「一场」的感觉。
 *
 * 终点 `end` 是数组里的百分比，专门给对应的十字星用：流星在哪儿熄灭，
 * 十字星就在哪儿亮，两者不能对不上。
 */
const METEORS = [
  { track: 'M 620 860 Q 980 500 1220 180', delay: '0s', end: ['84.7%', '18%'], w: 200, h: 13 },
  { track: 'M 760 920 Q 1120 560 1330 140', delay: '1.8s', end: ['92.4%', '14%'], w: 120, h: 7 },
  { track: 'M 500 780 Q 880 420 1140 130', delay: '3.6s', end: ['79.2%', '13%'], w: 250, h: 16 },
  { track: 'M 860 880 Q 1200 520 1380 220', delay: '5.4s', end: ['95.8%', '22%'], w: 155, h: 9 },
  { track: 'M 660 700 Q 1000 380 1200 100', delay: '7.2s', end: ['83.3%', '10%'], w: 95, h: 6 },
] as const

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
  /** 低矮平缓 —— 雾。雾里的山看不真切，轮廓几乎被吃掉，只剩一条起伏。 */
  mist: {
    back: 'M0 302 C 180 288 340 298 520 292 S 880 284 1080 294 S 1320 288 1440 296 L1440 420 L0 420 Z',
    front:
      'M0 346 C 200 334 380 342 580 336 S 940 330 1140 340 S 1360 334 1440 342 L1440 420 L0 420 Z',
  },
  /** 尖峰 —— 月。清辉下轮廓分明，比 rolling 陡，山形更硬。 */
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
      return 'mist'
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
 * 右侧留白里的**中景摆件** —— 每套主题各有一件，不通用。
 *
 * ※ 位置挑在内容区右边的留白里。卡片盖不到那儿，所以它真的看得见 ——
 *   上一版的远山栽的就是这一点：画得再用心，被卡片压住等于没画。
 *   窄屏没有这块留白，由 CSS 收掉。
 *
 * ※ **每套各画各的**，这是这套界面的老规矩（主题换的是气质，不是色号）。
 *   第一版我在这里放了朱印加符箓、五套共用 —— 那等于把五套拉平成一样，
 *   已经拿掉了。
 *
 * ※ 画的东西都是**中华物件**，但各自贴着自己那套的意象走：
 *   星图配「宵」、灯笼配「雾」、樱枝配「樱」、水波配「幽」。
 *   月那套已经有竹影，不再加；古登堡是特别的一类，先不动。
 */
function Corner({ theme }: { theme: ThemeId }) {
  switch (theme) {
    // 宵：星图。北斗七星 —— 中华天文里最有名的一组，也是夜里抬头看得见的。
    case 'yoi':
      return (
        <svg className="sky__obj sky__obj--stars" viewBox="0 0 120 74" aria-hidden="true">
          <path d="M10 52 L34 44 L52 49 L68 27 L88 19 L104 29 L114 16" />
          <circle cx="10" cy="52" r="3" />
          <circle cx="34" cy="44" r="2.4" />
          <circle cx="52" cy="49" r="2.4" />
          <circle cx="68" cy="27" r="2.4" />
          <circle cx="88" cy="19" r="2.8" />
          <circle cx="104" cy="29" r="2.4" />
          <circle cx="114" cy="16" r="2.4" />
        </svg>
      )
    // 雾：灯笼。赤夜里的红雾，配一盏没点亮的灯。
    case 'kiri':
      return (
        <svg className="sky__obj sky__obj--lantern" viewBox="0 0 64 104" aria-hidden="true">
          <path d="M32 4v10" />
          <rect x="14" y="14" width="36" height="6" rx="1" />
          <path d="M16 20c-6 8-8 18-8 28s2 20 8 28h32c6-8 8-18 8-28s-2-20-8-28z" />
          <path d="M32 20v56M22 22c-3 8-4 17-4 26s1 18 4 26M42 22c3 8 4 17 4 26s-1 18-4 26" />
          <rect x="14" y="76" width="36" height="6" rx="1" />
          <path d="M32 82v14M27 96h10" />
        </svg>
      )
    // 樱：樱枝。一枝斜出，几朵花。
    case 'sakura':
      return (
        <svg className="sky__obj sky__obj--branch" viewBox="0 0 132 96" aria-hidden="true">
          <path d="M4 88C34 78 62 62 88 38c10-10 18-22 24-34" />
          <path d="M46 70c10 2 20 0 28-6M70 50c-2-10-8-18-16-24M96 30c8 4 18 4 26 0" />
          <circle cx="60" cy="62" r="5" />
          <circle cx="84" cy="40" r="4.4" />
          <circle cx="46" cy="34" r="4.4" />
          <circle cx="114" cy="26" r="4" />
        </svg>
      )
    // 古登堡：十字星屑，**铺满整屏**。
    // ※ 上一版把它们塞在右侧留白的一小块里（148×108），看着像贴了张贴纸 ——
    //   星屑就该是散开的，散布在整个失重空间里。
    // ※ 它们不是稳定的星星：歌里它们「溶けてゆく」，也会「流れ星に」，
    //   所以做成会自己消失的东西（明灭，不是常亮）。
    case 'katsuji':
      return (
        <svg
          className="sky__obj sky__obj--stardust"
          viewBox="0 0 1440 1000"
          // ※ **不是 `none`。** `none` 会把 1440×1000 的画布硬拉成视口比例，
          // 于是 16:9 的屏上每颗星被横向拉宽 23%（实测 1.23:1）——
          // 星是圆的，拉宽就是「胖」。
          // `slice` 等比放大到铺满、多出来的部分裁掉。桌面尺寸下
          // 1440×1000 / 1280×800 / 1920×1080 都算过，18 颗一颗不丢。
          preserveAspectRatio="xMidYMid slice"
          aria-hidden="true"
        >
          {STARDUST.map(([x, y, r], i) => (
            <path
              key={i}
              d={[
                // 四角尖星。肩点（两臂之间的凹口）在 **0.07r** 处 ——
                // 原来是 0.3r（臂和身一样宽，裁出来是个菱形），
                // 上一版收到 0.12r，还是偏胖：这块画布是 `slice` 铺满的，
                // 1920 宽的屏上缩放约 1.33 倍，r=6.5 的那几颗臂基约 2px，
                // 四个角之间几乎连成一块。
                // 0.07r ⇒ 臂基 0.14r、臂长 0.93r：跨距不变，看着细一截。
                `M${x} ${y - r}`,
                `L${x + r * 0.07} ${y - r * 0.07}`,
                `L${x + r} ${y}`,
                `L${x + r * 0.07} ${y + r * 0.07}`,
                `L${x} ${y + r}`,
                `L${x - r * 0.07} ${y + r * 0.07}`,
                `L${x - r} ${y}`,
                `L${x - r * 0.07} ${y - r * 0.07}`,
                'Z',
              ].join(' ')}
              style={{ animationDelay: `${i * 1.3}s` }}
            />
          ))}
        </svg>
      )
    // 幽：水波。幽明微光落在水面上。
    case 'yuu':
      return (
        <svg className="sky__obj sky__obj--ripples" viewBox="0 0 140 70" aria-hidden="true">
          <path d="M4 16q14-10 28 0t28 0 28 0 28 0 20 0" />
          <path d="M12 34q14-10 28 0t28 0 28 0 28 0" />
          <path d="M4 52q14-10 28 0t28 0 28 0 28 0 20 0" />
        </svg>
      )
    default:
      return null
  }
}

export function Sky({ theme }: { theme: ThemeId }) {
  const dots = dotsFor(theme)

  return (
    <div className="sky" aria-hidden="true">
      <Scene theme={theme} />
      <Corner theme={theme} />

      {dots.map((d, i) => (
        <span key={`${theme}-${i}`} className="sky__dot" style={d.style} />
      ))}

      {/* 宣纸颗粒 + 暗角。放在最后 = 压在最上层，六套主题共用，
          所以它不参与「每套主题各不相同」那件事。 */}
      <div className="sky__grain" />

      {/* 流星群 + 熄灭时的十字星。
          ※ 为什么是独立元素而不是伪元素：流星线要用 `scaleX` 从尾往头收短，
            线一收，挂在线上的伪元素也跟着缩 —— 闪不出一个干净的十字。
            而且要求是「等拖尾完全消失后再闪」，两者必须分开计时。
          ※ `--track` 走 CSS 变量，五条弧共用一套规则、各自一条路径。

          ※ 外面那层 `.sky__meteors` 是**参考画布**（1440×1000，钉右上角）：
            路径的坐标是绝对 px，十字星的位置是百分比，只有放进同一块
            画布，两者才永远指向同一个点。理由写在 theme.css 那一节。 */}
      {theme === 'katsuji' && (
        <div className="sky__meteors">
          {METEORS.map((m, i) => (
            <div
              key={`m${i}`}
              className="sky__meteor"
              style={
                {
                  '--track': `path('${m.track}')`,
                  animationDelay: m.delay,
                  width: `${m.w}px`,
                  height: `${m.h}px`,
                } as CSSProperties
              }
            />
          ))}
          {METEORS.map((m, i) => (
            <div
              key={`s${i}`}
              className="sky__spark"
              style={
                {
                  left: m.end[0],
                  top: m.end[1],
                  animationDelay: m.delay,
                } as CSSProperties
              }
            />
          ))}
        </div>
      )}
    </div>
  )
}
