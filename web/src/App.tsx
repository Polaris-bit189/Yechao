import { useCallback, useEffect, useRef, useState } from 'react'
import { stepBack } from './api'
import { Sky } from './ui/Sky'
import { Remind, remindAnswered } from './ui/Remind'
import { Help } from './ui/Help'
import { DayPage } from './pages/DayPage'
import { StreamPage } from './pages/StreamPage'
import { KindsPage } from './pages/KindsPage'
import { MonthPage } from './pages/MonthPage'
import { BoundaryPage } from './pages/BoundaryPage'
import { SetupPage } from './pages/SetupPage'
import { EGG, MENU, paint, persist, stored, watchEgg, type ThemeId } from './lib/theme'
import { PWA_UPDATE_EVENT, isPwaUpdateEvent } from './pwa/register'
import {
  IcBoundary,
  IcDigest,
  IcHelp,
  IcKinds,
  IcMonth,
  IcPalette,
  IcSetup,
  IcStream,
  IcUndo,
} from './ui/icons'
import './App.css'
// ※ 这一行漏过一次：样式文件写好了、被引用的组件也渲染了，但没人 import
// 这份 CSS —— 于是提醒条、帮助卡、页面头的规则全部不生效。
// 症状是「元素在、样式没了」，而控制台一个错都不报。
import './ui/misc.css'
// 移动端外壳样式。**必须放在最后**：它覆盖 App.css / misc.css 里的桌面与
// 旧窄屏规则，靠的就是引入顺序。见 mobile.css 顶部的说明。
import './styles/mobile.css'

type Page = 'day' | 'stream' | 'kinds' | 'month' | 'boundary' | 'setup'

const PAGES = [
  { id: 'day' as Page, label: '日报', Icon: IcDigest },
  { id: 'stream' as Page, label: '信息流', Icon: IcStream },
  { id: 'kinds' as Page, label: '类目', Icon: IcKinds },
  { id: 'month' as Page, label: '日历', Icon: IcMonth },
  { id: 'boundary' as Page, label: '隐私', Icon: IcBoundary },
  { id: 'setup' as Page, label: '配置', Icon: IcSetup },
]

/**
 * 品牌区的两行字。彩蛋主题下换一套 —— 但**只换这两行**，页面本身、路由、
 * 数据都不动。
 *
 * 为什么是「古登堡 / 活字」：那套主题的视觉是活版印刷（朱砂油墨、宋体、
 * 零圆角、硬边压印），而活字印刷印出来的东西就是报纸 —— 所以「日报」在
 * 那一套里叫「活字」是顺的，不是硬凑的。其余五个页面名不动。
 *
 * ※ 主题 id 仍然是 `katsuji`，敲键口令、测试断言都不受影响 —— 这里换的
 *   只是**显示文本**。
 */
function brandOf(theme: ThemeId): string {
  return theme === EGG.id ? '古登堡' : '夜抄'
}

function badgeOf(page: Page, theme: ThemeId): string {
  if (theme === EGG.id && page === 'day') return '活字'
  return PAGES.find((x) => x.id === page)?.label ?? ''
}

/**
 * 应用外壳：导航 + 内容区 + 浮层。
 *
 * 导航只有一套 DOM。规格明写：桌面与移动两套都放进 DOM，读屏软件会读两遍。
 * 形态差异（侧栏 / 顶栏）全部交给 CSS 断点，结构不变。
 */
export function App() {
  const [page, setPage] = useState<Page>('day')
  const [theme, setTheme] = useState<ThemeId>(stored)
  const [tick, setTick] = useState(0)
  const [menuOpen, setMenuOpen] = useState(false)
  const [helpOpen, setHelpOpen] = useState(false)
  const [toast, setToast] = useState<string | null>(null)
  // 提示条正在淡出。它在卸载前 300ms 置真 —— 直接卸载元素是「凭空消失」。
  const [toastOut, setToastOut] = useState(false)
  // 提醒条问过没有。问过就不显示，顶部那 36px 也不留（见 App.css 的 .is-bare）。
  const [remindDone, setRemindDone] = useState(remindAnswered)
  // 有新版本在等待：由 register.ts 发事件，用户点「更新」才接管。
  const [updateWorker, setUpdateWorker] = useState<ServiceWorker | null>(null)
  const wantReload = useRef(false)
  // 顶部标签条（窄屏下可横滑）。切页时把选中的标签滚进视野。
  const navRef = useRef<HTMLUListElement>(null)

  const refresh = useCallback(() => setTick((n) => n + 1), [])

  useEffect(() => {
    // ※ 传 true：走到这里一定是**用户切了主题**（首帧那次上色在 main.tsx 里，
    //   走默认的不动画）。值没变时 paint 自己会跳过，所以挂载那次不会多一次过渡。
    paint(theme, true)
    persist(theme)
  }, [theme])

  // 空白处敲 katsuji 切到彩蛋。
  useEffect(() => watchEgg(() => setTheme(EGG.id)), [])

  // 切页时把选中的标签滚到标签条中间 —— 否则横滑条停在旧位置，
  // 当前页可能在屏幕外。只在标签条**溢出**时滚；桌面侧栏是纵向排列、
  // 不会溢出，这段逻辑对它是空转。
  // ※ 只有一套导航 DOM（规格硬要求），所以直接查 `.rail__link.is-on`。
  useEffect(() => {
    const nav = navRef.current
    if (!nav) return
    if (nav.scrollWidth <= nav.clientWidth) return
    const on = nav.querySelector<HTMLElement>('.rail__link.is-on')
    if (!on) return
    const navRect = nav.getBoundingClientRect()
    const onRect = on.getBoundingClientRect()
    // 让选中项的中心对齐标签条的中心。
    const delta = onRect.left - navRect.left - (navRect.width - onRect.width) / 2
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    nav.scrollTo({ left: nav.scrollLeft + delta, behavior: reduce ? 'auto' : 'smooth' })
  }, [page])

  useEffect(() => {
    if (!toast) return
    setToastOut(false)
    // 3000ms 开始淡出，3200ms 才卸载 —— 中间那 200ms 留给淡出动画。
    const fade = setTimeout(() => setToastOut(true), 3000)
    const gone = setTimeout(() => setToast(null), 3200)
    return () => {
      clearTimeout(fade)
      clearTimeout(gone)
    }
  }, [toast])

  // 新版本：页面提示，用户点了才接管并刷新一次。
  // ※ 首次安装时 controller 为空，不提示；只有「已经受控 + 发现 waiting」才是更新。
  useEffect(() => {
    if (!('serviceWorker' in navigator)) return
    const onUpdate = (e: Event) => {
      if (isPwaUpdateEvent(e)) setUpdateWorker(e.detail)
    }
    const onController = () => {
      if (wantReload.current) window.location.reload()
    }
    window.addEventListener(PWA_UPDATE_EVENT, onUpdate)
    navigator.serviceWorker.addEventListener('controllerchange', onController)
    return () => {
      window.removeEventListener(PWA_UPDATE_EVENT, onUpdate)
      navigator.serviceWorker.removeEventListener('controllerchange', onController)
    }
  }, [])

  async function undo() {
    try {
      const r = await stepBack()
      setToast(r.undone ? '已撤销上一步' : '没有可以撤销的操作')
      if (r.undone) refresh()
    } catch (e) {
      setToast(e instanceof Error ? e.message : '撤销失败')
    }
  }

  return (
    <>
      <Sky theme={theme} />

      <div className={remindDone ? 'shell is-bare' : 'shell'}>
        <nav className="rail" aria-label="主导航">
          <div className="rail__brand">
            {brandOf(theme)}
            {/* ※ 这里原来写死成「日报」。规格要的是**页面名徽标** ——
                它得跟着当前页变，否则切到信息流时它还写着「日报」。
                ※ 彩蛋主题下它也跟着换（见 badgeOf）。 */}
            <span>{badgeOf(page, theme)}</span>
          </div>

          <ul className="rail__nav" ref={navRef}>
            {PAGES.map(({ id, label, Icon }) => (
              <li key={id}>
                <button
                  type="button"
                  className={page === id ? 'rail__link is-on' : 'rail__link'}
                  aria-current={page === id ? 'page' : undefined}
                  onClick={() => setPage(id)}
                >
                  <Icon />
                  <span>{label}</span>
                </button>
              </li>
            ))}
          </ul>

          <div className="rail__tail">
            <button type="button" className="rail__small" onClick={() => void undo()}>
              <IcUndo />
              撤销
            </button>

            <div className="rail__theme">
              <button
                type="button"
                className="rail__small"
                aria-expanded={menuOpen}
                onClick={() => setMenuOpen((v) => !v)}
              >
                <IcPalette />
                主题
              </button>

              {menuOpen && (
                <div className="themebox" role="menu">
                  {MENU.map((t) => (
                    <button
                      key={t.id}
                      type="button"
                      role="menuitemradio"
                      aria-checked={theme === t.id}
                      className={theme === t.id ? 'themebox__row is-on' : 'themebox__row'}
                      onClick={() => {
                        setTheme(t.id)
                        setMenuOpen(false)
                      }}
                    >
                      <span>{t.name}</span>
                      <em>{t.note}</em>
                    </button>
                  ))}
                </div>
              )}
            </div>

            <button
              type="button"
              className="rail__help"
              aria-label="关于"
              onClick={() => setHelpOpen(true)}
            >
              <IcHelp />
            </button>
          </div>
        </nav>

        <main className="main">
          {/* 提醒条压在内容区顶部，不盖住左侧导航 —— 它说的是这一页的事，
              不是全局状态。 */}
          <Remind onDone={() => setRemindDone(true)} />

          <div className="main__col">
            {page === 'day' && <DayPage key={`d${tick}`} />}
            {page === 'stream' && <StreamPage key={`s${tick}`} onChanged={refresh} />}
            {page === 'kinds' && <KindsPage key={`k${tick}`} />}
            {page === 'month' && <MonthPage key={`m${tick}`} />}
            {page === 'boundary' && <BoundaryPage key={`b${tick}`} />}
            {page === 'setup' && <SetupPage key={`p${tick}`} onChanged={refresh} />}
          </div>
        </main>
      </div>

      {toast && (
        <div className={toastOut ? 'toast is-out' : 'toast'} role="status">
          {toast}
        </div>
      )}

      {updateWorker && (
        <div className="updatebar" role="status">
          <span>新版本可用</span>
          <button
            type="button"
            className="updatebar__go"
            onClick={() => {
              wantReload.current = true
              updateWorker.postMessage({ type: 'SKIP_WAITING' })
              setUpdateWorker(null)
            }}
          >
            更新
          </button>
        </div>
      )}

      {helpOpen && <Help onClose={() => setHelpOpen(false)} />}
    </>
  )
}
