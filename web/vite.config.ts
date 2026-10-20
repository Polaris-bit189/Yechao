import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// `base: './'` 而不是默认的 `/`：产物要被 `serve` 从任何路径挂载。
export default defineConfig({
  plugins: [react()],
  base: './',
  server: {
    // 开发期把 /api 代理到后端，否则前端 5173、后端 8787，请求全 404。
    proxy: { '/api': 'http://127.0.0.1:8787' },
  },
  build: { outDir: 'dist', emptyOutDir: true },
})
