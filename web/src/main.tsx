import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './App'
import { paint, stored } from './lib/theme'
import { initInstall } from './pwa/install'
import { registerPwa } from './pwa/register'
import './styles/theme.css'
import './styles/base.css'

// 先上色再挂载。晚一帧会闪一下默认主题，早上刚睁眼时那一下很刺。
paint(stored())

const host = document.getElementById('root')
if (!host) throw new Error('#root 不存在，检查 index.html')

createRoot(host).render(
  <StrictMode>
    <App />
  </StrictMode>,
)

// 生产构建才真正注册；开发与局域网 http 下 registerPwa 自己会跳过。
initInstall()
registerPwa()
