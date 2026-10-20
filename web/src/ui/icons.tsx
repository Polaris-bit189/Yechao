/**
 * 线性图标。统一 1.7 描边、16×16 视口、`currentColor` 取色。
 *
 * 全站**零 emoji**：emoji 当设计元素在不同平台上渲染形状各异，
 * 而一套没对齐的图标会让界面看起来是拼起来的。
 *
 * 类目图标按 slug 映射到这里；用户新建的未知类目回落到 `Tag`——
 * 回落成 emoji 会让同一屏混进两种图标语言。
 */

type Props = { size?: number; className?: string }

const base = (size: number) => ({
  width: size,
  height: size,
  viewBox: '0 0 16 16',
  fill: 'none' as const,
  stroke: 'currentColor',
  strokeWidth: 1.7,
  strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const,
  'aria-hidden': true,
})

const mk = (size: number, className: string | undefined, children: React.ReactNode) => (
  <svg {...base(size)} className={className}>
    {children}
  </svg>
)

// ── 导航 ────────────────────────────────────────────────────

export const IcDigest = ({ size = 15, className }: Props) =>
  mk(size, className, (
    <>
      <path d="M3.5 2.5h6l3 3v8h-9z" />
      <path d="M9.5 2.5v3h3" />
      <path d="M5.8 8.4h4.4M5.8 10.8h2.8" />
    </>
  ))

export const IcStream = ({ size = 15, className }: Props) =>
  mk(size, className, <path d="M2.5 4.6h11M2.5 8h11M2.5 11.4h6.5" />)

export const IcKinds = ({ size = 15, className }: Props) =>
  mk(size, className, (
    <>
      <rect x="2.6" y="2.6" width="4.4" height="4.4" rx="1" />
      <rect x="9" y="2.6" width="4.4" height="4.4" rx="1" />
      <rect x="2.6" y="9" width="4.4" height="4.4" rx="1" />
      <rect x="9" y="9" width="4.4" height="4.4" rx="1" />
    </>
  ))

export const IcMonth = ({ size = 15, className }: Props) =>
  mk(size, className, (
    <>
      <rect x="2.5" y="3.6" width="11" height="9.9" rx="1.4" />
      <path d="M2.5 6.6h11M5.5 2.2v2.6M10.5 2.2v2.6" />
    </>
  ))

export const IcBoundary = ({ size = 15, className }: Props) =>
  mk(size, className, (
    <>
      <path d="M8 2.2 13 4.1v4.2c0 2.7-2 4.5-5 5.1-3-.6-5-2.4-5-5.1V4.1z" />
      <path d="m5.9 7.7 1.5 1.5 2.9-2.9" />
    </>
  ))

export const IcSetup = ({ size = 15, className }: Props) =>
  mk(size, className, (
    <>
      <path d="M2.5 5.6h11M2.5 10.4h11" />
      <circle cx="6" cy="5.6" r="1.6" />
      <circle cx="10.4" cy="10.4" r="1.6" />
    </>
  ))

// ── 导航底部 ────────────────────────────────────────────────

export const IcUndo = ({ size = 14, className }: Props) =>
  mk(size, className, (
    <>
      <path d="M3 7.4h6.8a2.9 2.9 0 0 1 0 5.8H6" />
      <path d="m5.4 4.9-2.4 2.5 2.4 2.5" />
    </>
  ))

export const IcPalette = ({ size = 14, className }: Props) =>
  mk(size, className, (
    <>
      <path d="M8 2.4a5.6 5.6 0 1 0 0 11.2c1 0 1.5-.6 1.5-1.3 0-.9-.9-1.2-.9-2 0-.6.5-1 1.2-1h.9a2.5 2.5 0 0 0 2.5-2.5C13.2 4.3 10.8 2.4 8 2.4z" />
      <circle cx="5.9" cy="6.3" r="0.55" fill="currentColor" stroke="none" />
      <circle cx="8.3" cy="4.8" r="0.55" fill="currentColor" stroke="none" />
    </>
  ))

export const IcHelp = ({ size = 14, className }: Props) =>
  mk(size, className, (
    <>
      <circle cx="8" cy="8" r="5.5" />
      <path d="M6.5 6.3a1.6 1.6 0 1 1 2 1.5c-.4.2-.6.5-.6.9v.2" />
      <circle cx="7.9" cy="11.1" r="0.45" fill="currentColor" stroke="none" />
    </>
  ))

// ── 卡片内文 ────────────────────────────────────────────────

export const IcClock = ({ size = 12, className }: Props) =>
  mk(size, className, (
    <>
      <circle cx="8" cy="8" r="5.5" />
      <path d="M8 5.1v3.1l2 1.3" />
    </>
  ))

export const IcPin = ({ size = 12, className }: Props) =>
  mk(size, className, (
    <>
      <path d="M8 13.8s4-3.9 4-6.8a4 4 0 0 0-8 0c0 2.9 4 6.8 4 6.8z" />
      <circle cx="8" cy="6.9" r="1.4" />
    </>
  ))

export const IcMap = ({ size = 12, className }: Props) =>
  mk(size, className, (
    <>
      <path d="M2.6 4.3 6 3.1l4 1.2 3.4-1.2v8.5L10 12.8 6 14l-3.4-.8z" />
      <path d="M6 3.1v10.9M10 4.3v8.5" />
    </>
  ))

export const IcSearch = ({ size = 13, className }: Props) =>
  mk(size, className, (
    <>
      <circle cx="7" cy="7" r="4.2" />
      <path d="m10.2 10.2 3 3" />
    </>
  ))

export const IcBell = ({ size = 14, className }: Props) =>
  mk(size, className, (
    <>
      <path d="M4.1 10.4V7a3.9 3.9 0 0 1 7.8 0v3.4l1.1 1.4H3z" />
      <path d="M6.9 12.9a1.35 1.35 0 0 0 2.2 0" />
    </>
  ))

export const IcShield = ({ size = 16, className }: Props) =>
  mk(size, className, (
    <>
      <path d="M8 2.1 13 4v4.2c0 2.7-2 4.5-5 5.1-3-.6-5-2.4-5-5.1V4z" />
      <path d="m5.9 7.7 1.5 1.5 2.9-2.9" />
    </>
  ))

export const IcWarn = ({ size = 14, className }: Props) =>
  mk(size, className, (
    <>
      <path d="M8 2.7 14 13H2z" />
      <path d="M8 6.4v3.1" />
      <circle cx="8" cy="11.2" r="0.45" fill="currentColor" stroke="none" />
    </>
  ))

export const IcChevron = ({ size = 14, className }: Props) =>
  mk(size, className, <path d="m6 4 4 4-4 4" />)

export const IcTag = ({ size = 13, className }: Props) =>
  mk(size, className, (
    <>
      <path d="M8.4 2.5H13v4.6l-6 6-4.6-4.6z" />
      <circle cx="10.8" cy="5.2" r="0.7" />
    </>
  ))

// ── 七个类目的图标 ──────────────────────────────────────────

export const IcFileText = ({ size = 13, className }: Props) =>
  mk(size, className, (
    <>
      <path d="M3.8 2.5h5.4l3 3v8H3.8z" />
      <path d="M9.2 2.5v3h3M5.9 8.3h4.2M5.9 10.6h2.8" />
    </>
  ))

export const IcCap = ({ size = 13, className }: Props) =>
  mk(size, className, (
    <>
      <path d="M1.8 6.2 8 3.3l6.2 2.9L8 9.1z" />
      <path d="M4.3 7.6v3c0 .9 1.7 1.6 3.7 1.6s3.7-.7 3.7-1.6v-3" />
    </>
  ))

export const IcMic = ({ size = 13, className }: Props) =>
  mk(size, className, (
    <>
      <rect x="6.2" y="2.2" width="3.6" height="6.4" rx="1.8" />
      <path d="M3.8 7.6a4.2 4.2 0 0 0 8.4 0M8 11.8v2" />
    </>
  ))

export const IcHome = ({ size = 13, className }: Props) =>
  mk(size, className, (
    <>
      <path d="M2.6 7.4 8 2.8l5.4 4.6v6.1H2.6z" />
      <path d="M6.3 13.5V9.7h3.4v3.8" />
    </>
  ))

export const IcBox = ({ size = 13, className }: Props) =>
  mk(size, className, (
    <>
      <path d="M8 2.3 13.4 5v6L8 13.7 2.6 11V5z" />
      <path d="M2.6 5 8 7.7 13.4 5M8 7.7v6" />
    </>
  ))

export const IcFileSearch = ({ size = 13, className }: Props) =>
  mk(size, className, (
    <>
      <path d="M3.8 2.5h5.4l3 3v8H3.8z" />
      <path d="M9.2 2.5v3h3" />
      <circle cx="7.3" cy="8.9" r="1.9" />
      <path d="m8.7 10.3 1.4 1.4" />
    </>
  ))

export const IcBriefcase = ({ size = 13, className }: Props) =>
  mk(size, className, (
    <>
      <rect x="2.4" y="5.4" width="11.2" height="8" rx="1.3" />
      <path d="M5.9 5.4V4a1.2 1.2 0 0 1 1.2-1.2h1.8A1.2 1.2 0 0 1 10.1 4v1.4" />
      <path d="M2.4 8.9h11.2" />
    </>
  ))
