import { readonly, ref } from 'vue'

/**
 * 全局 SSE 连接（单例）。
 *
 * 为什么不在每个页面各开一条：EventSource 是有限的连接资源（HTTP/1.1 下浏览器
 * 每域 6 条），而且任务页和文件页都要听同一批事件。一条连接 + 订阅回调最省。
 *
 * 断线由浏览器自动重连（服务端发的 retry: 3000 生效），但**不会重放错过的消息**，
 * 所以订阅方在 connected 变回 true 时必须自己拉一次全量对账。这不是可选优化，
 * 是这条协议的一部分。
 */
const connected = ref(false)
const listeners = new Set()
let source = null
let reconnectHook = null

function dispatch(payload) {
  for (const listener of listeners) {
    try {
      listener(payload)
    } catch (error) {
      console.error('SSE 订阅回调整理失败', error)
    }
  }
}

export function useLiveEvents() {
  function connect() {
    if (source) return
    source = new EventSource('/api/events')

    source.onopen = () => {
      connected.value = true
      if (reconnectHook) reconnectHook()
    }

    source.onerror = () => {
      // 浏览器会自己重连，这里只反映状态，不手动 close
      connected.value = false
    }

    source.onmessage = (event) => {
      try {
        dispatch(JSON.parse(event.data))
      } catch (error) {
        console.warn('收到无法解析的 SSE 事件', error)
      }
    }
  }

  function disconnect() {
    if (source) {
      source.close()
      source = null
    }
    connected.value = false
  }

  function on(listener) {
    listeners.add(listener)
    return () => listeners.delete(listener)
  }

  /** 重连成功后要做的全量对账动作。 */
  function onReconnect(hook) {
    reconnectHook = hook
  }

  return { connected: readonly(connected), connect, disconnect, on, onReconnect }
}
