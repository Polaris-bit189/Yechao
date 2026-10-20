import { useEffect } from 'react'

/**
 * 左下角问号打开的东西。
 *
 * 只写快捷键与几条边界，不写产品介绍 —— 页面上已经有别人的东西在说
 * 这个产品是什么了，这里再多说一遍是噪音。
 */
export function Help({ onClose }: { onClose: () => void }) {
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="helpwrap" onClick={onClose}>
      <div
        className="helpcard"
        role="dialog"
        aria-label="关于"
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

        <button type="button" onClick={onClose}>
          知道了
        </button>
      </div>
    </div>
  )
}
