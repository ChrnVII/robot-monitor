import { existsSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发时前端跑 5173，接口转发到后端 8000；生产时 dist 由 FastAPI 直接托管，
// 所以这里的 proxy 只在 `npm run dev` 生效。
//
// HTTPS 是可选的：把证书放到 .cert/{cert.pem,key.pem} 就会自动启用。
// 为什么需要它：WebCodecs 只在安全上下文里暴露 VideoDecoder，用 http 访问
// 公网 IP 时浏览器会直接没有这个 API——不是浏览器不支持，是环境不对。
// 证书只需覆盖浏览器访问用的地址（通常带 SAN=IP），不必是受信证书：
// 浏览器点过一次"继续访问"后，页面就是安全上下文。
const certDir = fileURLToPath(new URL('./.cert/', import.meta.url))
const keyFile = `${certDir}key.pem`
const certFile = `${certDir}cert.pem`
const https =
  existsSync(keyFile) && existsSync(certFile)
    ? { key: readFileSync(keyFile), cert: readFileSync(certFile) }
    : undefined

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    https,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: false },
      '/internal': { target: 'http://127.0.0.1:8000', changeOrigin: false },
      // 三条 WebSocket 都在这一个前缀下：/ws/agent、/ws/live、/ws/agent/media。
      // ws: true 少了任何一条都会表现为"页面能开、画面一直是连接中"。
      '/ws': { target: 'ws://127.0.0.1:8000', ws: true }
    }
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    chunkSizeWarningLimit: 1200,
    rollupOptions: {
      output: {
        // 把不会变的依赖单独切出来：升级业务代码时用户不用重下 1MB 的 Element Plus
        manualChunks: {
          'element-plus': ['element-plus', '@element-plus/icons-vue'],
          vue: ['vue', 'vue-router', 'pinia']
        }
      }
    }
  }
})
