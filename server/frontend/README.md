# filebridge-frontend

Vue 3 + JavaScript + Vite + Element Plus。五个页面：登录、节点、取件、任务、文件库。

## 开发

```bash
npm install
npm run dev        # 5173，/api /internal /ws 代理到 127.0.0.1:8000
```

## 构建

```bash
npm run build      # 产出 dist/，由 backend 直接托管
```

生产环境服务器上不需要 Node：构建产物随代码一起部署即可。

## 目录

```
src/
├── api/client.js              axios 实例 + 401 广播 + 按 code 取错误文案
├── composables/useLiveEvents.js  SSE 单例（断线重连 + 重连后对账钩子）
├── layout/AppLayout.vue       侧栏 + 顶栏（在线数、磁盘余量、实时连接状态）
├── stores/auth.js             会话状态
├── views/                     Login / Nodes / Browse / Tasks / Files
└── utils/format.js            字节、时间、任务状态文案
```

## 两个刻意的取舍

1. **下载用 `<a href>` 直链**，不用 axios 拉 blob 再触发下载——后者会把整个文件
   读进内存，几个 G 的文件直接崩标签页。
2. **任务列表保留 5s 轮询**。SSE 断线期间的事件不会重放，终态事件只能靠全量
   对账补上；轮询就是那个兜底。
