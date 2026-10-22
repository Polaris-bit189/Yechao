import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import {
  clearOfflineCaches,
  getInstallPrompt,
  installHint,
  promptInstall,
  subscribeInstall,
} from '../pwa/install'

/**
 * 左下角问号打开的东西。
 *
 * 只写快捷键与几条边界，不写产品介绍 —— 页面上已经有别人的东西在说
 * 这个产品是什么了，这里再多说一遍是噪音。
 *
 * ※ 它是一块**模态浮层**（`aria-modal`），所以光有个 Esc 关闭不够：
 *   · 打开时把焦点移进卡片，否则 Tab 会落到浮层后面的页面上；
 *   · Tab/Shift+Tab 在卡片内**循环**，焦点跑不出去；
 *   · Esc 与点遮罩都能关（遮罩那一层由 `.helpwrap` 的 onClick 负责）。
 */
export function Help({ onClose }: { onClose: () => void }) {
  const cardRef = useRef<HTMLDivElement>(null)
  // 订阅安装提示事件：`beforeinstallprompt` 可能晚于打开帮助才到。
  useSyncExternalStore(subscribeInstall, getInstallPrompt)
  const hint = installHint()
  const [cacheCleared, setCacheCleared] = useState(false)

  useEffect(() => {
    const card = cardRef.current
    // 卡片里的可聚焦元素。现在只有一个「知道了」按钮，但不写死。
    const focusables = () =>
      card
        ? Array.from(
            card.querySelectorAll<HTMLElement>(
              'a[href], button:not([disabled]), input:not([disabled]), select, textarea, [tabindex]:not([tabindex="-1"])',
            ),
          )
        : []

    // 打开就把焦点移进卡片。
    focusables()[0]?.focus()

    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        onClose()
        return
      }
      if (e.key !== 'Tab') return
      const items = focusables()
      if (items.length === 0) {
        e.preventDefault()
        return
      }
      const cur = document.activeElement as HTMLElement | null
      const idx = cur ? items.indexOf(cur) : -1
      // 到尾部再 Tab 回头部，到头部再 Shift+Tab 去尾部 —— 焦点圈在卡片里。
      const next = e.shiftKey
        ? idx <= 0
          ? items.length - 1
          : idx - 1
        : idx === -1 || idx === items.length - 1
          ? 0
          : idx + 1
      e.preventDefault()
      items[next]?.focus()
    }

    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="helpwrap" onClick={onClose}>
      <div
        className="helpcard"
        role="dialog"
        aria-modal="true"
        aria-label="关于"
        ref={cardRef}
        onClick={(e) => e.stopPropagation()}
      >
        <h2>关于</h2>
        <p>
          原文在本机完成抽取，只有脱敏后的结构化结果离开这台机器。
          日报正文由本机模型生成；模型没跑成时改用机械整理，并在页面上写明。
        </p>

        <dl>
          <dt>切换主题</dt>
          <dd>左下角「主题」</dd>
          <dt>彩蛋</dt>
          <dd>在页面空白处依次敲 k a t s u j i</dd>
          <dt>撤销</dt>
          <dd>左下角「撤销」，一次退一步</dd>
        </dl>

        <div className="helpcard__install">
          <h3>装到桌面</h3>
          {hint === 'installed' && <p>已经在独立窗口里运行。</p>}
          {hint === 'prompt' && (
            <button type="button" onClick={() => void promptInstall()}>
              安装到桌面
            </button>
          )}
          {hint === 'ios' && <p>在 Safari 里点「分享」→「添加到主屏幕」。</p>}
          {hint === 'other' && (
            <p>用浏览器菜单里的「添加到主屏幕」；鸿蒙浏览器同样按浏览器菜单操作。</p>
          )}
          <button
            type="button"
            onClick={() => {
              void clearOfflineCaches().then(() => setCacheCleared(true))
            }}
          >
            清除离线缓存
          </button>
          {cacheCleared && <p>已清除，刷新后生效。</p>}
        </div>

        <button type="button" onClick={onClose}>
          知道了
        </button>
      </div>
    </div>
  )
}
