import { useState } from 'react'
import { IcBell } from './icons'

const ASKED = 'yechao:remind-asked'

/**
 * 这条提示问过没有。
 *
 * 外壳靠它判断**顶部那 36px 要不要留** —— 提示条是 `position: fixed` 的，
 * 不留白，页面内容就会钻到它底下；可一旦问过，它就不再出现，那 36px 便成了
 * 每页头顶一条常驻的空白。
 */
export function remindAnswered(): boolean {
  try {
    return localStorage.getItem(ASKED) === '1'
  } catch {
    return false
  }
}

/**
 * 顶部那条提醒权限询问。
 *
 * 没问过时才冒出来，问完就记下 —— 它只管一次，不是常驻的横幅。
 *
 * 措辞上要留神：它不过是页面开着时的一次轮询，页面一关就没了。
 * 别把它说成「推送」—— 那会让人以为浏览器关掉照样能收到。
 */
export function Remind({ onDone }: { onDone?: () => void }) {
  const [gone, setGone] = useState(remindAnswered)
  const [reply, setReply] = useState<string | null>(null)

  function done(text: string) {
    try {
      localStorage.setItem(ASKED, '1')
    } catch {
      /* 存不住最多下次再问一遍 */
    }
    setReply(text)
    onDone?.()
    setTimeout(() => setGone(true), 1800)
  }

  async function allow() {
    if (!('Notification' in window)) {
      done('当前浏览器不支持系统通知')
      return
    }
    try {
      const perm = await Notification.requestPermission()
      done(perm === 'granted' ? '已开启：遇到紧急条目会提醒你' : '没有授权，回头可以在浏览器设置里改')
    } catch {
      done('提醒没能开启')
    }
  }

  if (gone) return null

  return (
    <div className="remind">
      <IcBell />
      <span>{reply ?? '出现紧急条目时，要我弹一条系统通知吗？'}</span>
      {!reply && (
        <span className="remind__btns">
          <button type="button" className="remind__yes" onClick={() => void allow()}>
            开启提醒
          </button>
          <button type="button" className="remind__no" onClick={() => done('好，不提醒')}>
            不用
          </button>
        </span>
      )}
    </div>
  )
}
