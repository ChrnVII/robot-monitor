import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发时前端跑 5173，接口转发到后端 8000；生产时 dist 由 FastAPI 直接托管，
// 所以这里的 proxy 只在 `npm run dev` 生效。
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: false },
      '/internal': { target: 'http://127.0.0.1:8000', changeOrigin: false },
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
