import { useCallback, useEffect, useState } from 'react'
import { stepBack } from './api'
import { Sky } from './ui/Sky'
import { Remind } from './ui/Remind'
import { Help } from './ui/Help'
import { DayPage } from './pages/DayPage'
import { StreamPage } from './pages/StreamPage'
import { KindsPage } from './pages/KindsPage'
import { MonthPage } from './pages/MonthPage'
import { BoundaryPage } from './pages/BoundaryPage'
import { SetupPage } from './pages/SetupPage'
import { EGG, MENU, paint, persist, stored, watchEgg, type ThemeId } from './lib/theme'
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
// ⚠️ 这一行漏过一次：样式文件写好了、被引用的组件也渲染了，但没人 import
// 这份 CSS —— 于是提醒条、帮助卡、页面头的规则全部不生效。
// 症状是「元素在、样式没了」，而控制台一个错都不报。
import './ui/misc.css'

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

  const refresh = useCallback(() => setTick((n) => n + 1), [])

  useEffect(() => {
    paint(theme)
    persist(theme)
  }, [theme])

  // 空白处敲 katsuji 切到彩蛋。
  useEffect(() => watchEgg(() => setTheme(EGG.id)), [])

  useEffect(() => {
    if (!toast) return
    const id = setTimeout(() => setToast(null), 3200)
    return () => clearTimeout(id)
  }, [toast])

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

      <div className="shell">
        <nav className="rail" aria-label="主导航">
          <div className="rail__brand">
            夜抄
            {/* ⚠️ 这里原来写死成「日报」。规格要的是**页面名徽标** ——
                它得跟着当前页变，否则切到信息流时它还写着「日报」。 */}
            <span>{PAGES.find((x) => x.id === page)?.label ?? ''}</span>
          </div>

          <ul className="rail__nav">
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
          <Remind />

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
        <div className="toast" role="status">
          {toast}
        </div>
      )}

      {helpOpen && <Help onClose={() => setHelpOpen(false)} />}
    </>
  )
}
