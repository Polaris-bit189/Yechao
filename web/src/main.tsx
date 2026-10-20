import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './App'
import { paint, stored } from './lib/theme'
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
