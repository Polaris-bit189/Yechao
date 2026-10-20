import { useState } from 'react'
import { IcBell } from './icons'

const ASKED = 'yechao:remind-asked'

/**
 * 顶部那条提醒权限询问。
 *
 * 只在没问过的时候出现，问完就记下来。
 *
 * 用词注意：这只是一次页面内轮询，关掉页面就不存在了。
 * 不能把它说成「推送」——那会让人以为关掉浏览器也收得到。
 */
export function Remind() {
  const [gone, setGone] = useState(() => {
    try {
      return localStorage.getItem(ASKED) === '1'
    } catch {
      return false
    }
  })
  const [reply, setReply] = useState<string | null>(null)

  function done(text: string) {
    try {
      localStorage.setItem(ASKED, '1')
    } catch {
      /* 存不住最多下次再问一遍 */
    }
    setReply(text)
    setTimeout(() => setGone(true), 1800)
  }

  async function allow() {
    if (!('Notification' in window)) {
      done('这个浏览器不支持系统通知')
      return
    }
    try {
      const perm = await Notification.requestPermission()
      done(perm === 'granted' ? '已开启：有紧急条目时提醒你' : '没有授予权限，之后可以在浏览器设置里改')
    } catch {
      done('没能开启提醒')
    }
  }

  if (gone) return null

  return (
    <div className="remind">
      <IcBell />
      <span>{reply ?? '要我在紧急条目出现时弹系统通知吗？'}</span>
      {!reply && (
        <span className="remind__btns">
          <button type="button" className="remind__yes" onClick={() => void allow()}>
            开启提醒
          </button>
          <button type="button" className="remind__no" onClick={() => done('好的，不提醒')}>
            不用
          </button>
        </span>
      )}
    </div>
  )
}
