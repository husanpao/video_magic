import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

// 产物直接落到 Python 静态目录：vm/web.py 原样托管 vm/static/，不用改服务端路由。
export default defineConfig({
  plugins: [vue()],
  base: '/',
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  build: {
    outDir: '../vm/static/dist',
    emptyOutDir: true,
    chunkSizeWarningLimit: 1500,
  },
  server: {
    port: 5273,
    proxy: {
      '/api': 'http://127.0.0.1:8801',
      '/view': 'http://127.0.0.1:8801',
    },
  },
})
