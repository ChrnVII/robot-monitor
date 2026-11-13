import { onBeforeUnmount, reactive, ref } from 'vue'
import { cameraStreamBlockedReason } from '../api/camera'

// 浏览器端解码器：与机器人本地平台同一套帧格式。
//
// 帧协议（后端 app/video/pipeline.py 定义，云端只转不发）：
//   文本 {"t":"config","codec":"avc1.64001F","t0":<epoch_ms>,"size":[w,h]}
//   二进制 flags(1) + pts_ms(4, LE) + seq(4) + Annex-B
//
// 三条必须保留的行为，少一条都会在真实网络下退化成"越看越卡"：
// 1. 只保留最新一帧，旧的立刻 close()；
// 2. 队列超过 MAX_DECODE_QUEUE 或收到 RESET 时，丢弃直到下一个关键帧；
// 3. 解码器 reset() 之后必须重新 configure()，不能直接 decode。

const FRAME_HEADER_LENGTH = 9
const FRAME_FLAG = { KEY: 0x01, CONFIG: 0x02, RESET: 0x04 }
const MAX_DECODE_QUEUE = 8
const RECONNECT_MIN_DELAY = 2000
const RECONNECT_MAX_DELAY = 10000

export function useLiveStream() {
  const phase = ref('idle') // idle | connecting | live | error
  const error = ref('')
  const codec = ref('')
  const resolution = reactive({ width: 0, height: 0 })
  const metrics = reactive({
    fps: 0,
    kbps: 0,
    received: 0,
    dropped: 0,
    viewers: 0,
    sessionId: ''
  })

  let socket = null
  let decoder = null
  let needReset = true
  let disposed = false
  let url = ''
  let reconnectTimer = null
  let attempts = 0
  let canvas = null
  let context = null
  let pendingFrame = null
  let paintHandle = null
  let framesSinceTick = 0
  let bytesSinceTick = 0
  let lastTick = 0
  let statsTimer = null

  function attach(element) {
    canvas = element || null
    context = canvas ? canvas.getContext('2d') : null
    startPainting()
  }

  function supported() {
    return !cameraStreamBlockedReason()
  }

  function connect(nextUrl) {
    disposed = false
    url = nextUrl
    const blocked = cameraStreamBlockedReason()
    if (blocked) {
      phase.value = 'error'
      error.value = blocked
      return
    }
    open()
  }

  function open() {
    closeSocket()
    if (!url) return
    phase.value = 'connecting'
    error.value = ''
    try {
      socket = new WebSocket(url)
      socket.binaryType = 'arraybuffer'
    } catch (reason) {
      fail(String(reason))
      return
    }

    socket.onopen = () => {
      attempts = 0
    }
    socket.onmessage = (event) => {
      if (typeof event.data === 'string') handleText(event.data)
      else if (event.data instanceof ArrayBuffer) handleBinary(event.data)
    }
    socket.onclose = () => {
      socket = null
      if (disposed) return
      if (phase.value !== 'error') {
        phase.value = 'connecting'
        scheduleReconnect()
      }
    }
    socket.onerror = () => {
      // onerror 之后一定会 onclose，重连逻辑只放在 onclose 一处，避免重复。
    }
  }

  function handleText(raw) {
    let message
    try {
      message = JSON.parse(raw)
    } catch {
      return
    }
    if (message.t === 'config') {
      if (message.codec && message.codec !== codec.value) {
        codec.value = String(message.codec)
        makeDecoder()
      }
      if (Array.isArray(message.size) && message.size.length === 2) {
        resolution.width = Number(message.size[0]) || 0
        resolution.height = Number(message.size[1]) || 0
        sizeCanvas()
      }
      return
    }
    if (message.t === 'session') {
      metrics.sessionId = message.id || message.sessionId || ''
      if (message.viewers) metrics.viewers = Number(message.viewers) || 0
      return
    }
    if (message.t === 'upstream') {
      if (message.state === 'live') phase.value = 'live'
      return
    }
    if (message.t === 'error') {
      // stream_disabled / agent_timeout / camera_busy 都不是"再连一次就好"，
      // 停在错误态让人看到原因，别用自动重连把提示刷掉。
      fail(message.message || '画面不可用')
      return
    }
    if (message.t === 'stop') {
      // 这两种 stop 是"已经被结束了"，不是网络抖动：自动重连会把刚关掉的
      // 会话又拉起来，等于上限和踢人按钮都失效。交给用户点「重连」。
      if (message.reason === 'session_expired') {
        fail('本次观看已达到时长上限，点「重连」可继续')
        return
      }
      if (message.reason === 'stopped_by_user') {
        fail('本次观看已被结束')
        return
      }
      phase.value = 'connecting'
      closeSocket()
      scheduleReconnect()
    }
  }

  function handleBinary(buffer) {
    if (buffer.byteLength <= FRAME_HEADER_LENGTH) return
    const view = new DataView(buffer)
    const flags = view.getUint8(0)
    const pts = view.getUint32(1, true)
    const payload = new Uint8Array(buffer, FRAME_HEADER_LENGTH)

    metrics.received += 1
    framesSinceTick += 1
    bytesSinceTick += buffer.byteLength
    if (flags & FRAME_FLAG.CONFIG) return

    const isKey = Boolean(flags & FRAME_FLAG.KEY)
    if (flags & FRAME_FLAG.RESET || (decoder && decoder.decodeQueueSize > MAX_DECODE_QUEUE)) {
      needReset = true
      metrics.dropped += 1
    }
    if (needReset || !decoder) {
      if (!isKey) return
      makeDecoder()
      if (!decoder || decoder.state !== 'configured') return
      needReset = false
    }

    try {
      decoder.decode(
        new EncodedVideoChunk({
          type: isKey ? 'key' : 'delta',
          timestamp: pts * 1000,
          data: payload
        })
      )
      phase.value = 'live'
    } catch (reason) {
      console.error('[live] decode failed', reason)
      needReset = true
    }
  }

  function makeDecoder() {
    if (!codec.value) return
    if (decoder) {
      try {
        decoder.close()
      } catch {
        /* 已经关了 */
      }
    }
    try {
      decoder = new VideoDecoder({ output: onFrame, error: onDecoderError })
      decoder.configure({ codec: codec.value, optimizeForLatency: true })
      needReset = true
    } catch (reason) {
      onDecoderError(reason)
    }
  }

  function onDecoderError(event) {
    // 单个损坏帧不该让整页停在错误态：标记需要重置，等下一个关键帧。
    console.warn('[live] decoder error', event?.message || event)
    needReset = true
    try {
      decoder?.close()
    } catch {
      /* 已关闭 */
    }
    decoder = null
  }

  function onFrame(frame) {
    if (disposed) {
      frame.close()
      return
    }
    if (pendingFrame) pendingFrame.close()
    pendingFrame = frame
    if (!paintHandle) paintHandle = requestAnimationFrame(paint)
  }

  function sizeCanvas() {
    if (!canvas || !resolution.width || !resolution.height) return
    const ratio = Math.min(window.devicePixelRatio || 1, 2)
    canvas.width = Math.round(resolution.width * ratio)
    canvas.height = Math.round(resolution.height * ratio)
  }

  function paint() {
    paintHandle = null
    if (!context || !canvas) return
    if (!pendingFrame) return
    const frame = pendingFrame
    pendingFrame = null
    const width = frame.displayWidth
    const height = frame.displayHeight
    if (width && height) {
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width
        canvas.height = height
      }
      context.drawImage(frame, 0, 0, canvas.width, canvas.height)
    }
    frame.close()
  }

  function startPainting() {
    if (statsTimer) return
    lastTick = Date.now()
    statsTimer = window.setInterval(() => {
      const now = Date.now()
      const elapsed = Math.max(0.2, (now - lastTick) / 1000)
      metrics.fps = Math.round((framesSinceTick / elapsed) * 10) / 10
      metrics.kbps = Math.round((bytesSinceTick * 8) / elapsed / 1000)
      framesSinceTick = 0
      bytesSinceTick = 0
      lastTick = now
      if (decoder && decoder.decodeQueueSize > MAX_DECODE_QUEUE) metrics.dropped += 1
    }, 1000)
  }

  function scheduleReconnect() {
    if (disposed || reconnectTimer) return
    const delay = Math.min(RECONNECT_MIN_DELAY * 2 ** attempts, RECONNECT_MAX_DELAY)
    attempts += 1
    reconnectTimer = window.setTimeout(() => {
      reconnectTimer = null
      if (!disposed) open()
    }, delay)
  }

  function closeSocket() {
    if (reconnectTimer) {
      window.clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
    if (!socket) return
    const current = socket
    socket = null
    current.onclose = null
    try {
      current.close()
    } catch {
      /* 已经断开 */
    }
  }

  function fail(message) {
    error.value = message
    phase.value = 'error'
    closeSocket()
  }

  function disconnect() {
    disposed = true
    closeSocket()
    if (paintHandle) cancelAnimationFrame(paintHandle)
    paintHandle = null
    if (pendingFrame) pendingFrame.close()
    pendingFrame = null
    if (decoder) {
      try {
        decoder.close()
      } catch {
        /* 已关闭 */
      }
      decoder = null
    }
    phase.value = 'idle'
  }

  function reconnect() {
    attempts = 0
    error.value = ''
    open()
  }

  onBeforeUnmount(() => {
    disconnect()
    if (statsTimer) window.clearInterval(statsTimer)
    statsTimer = null
  })

  return {
    phase,
    error,
    codec,
    resolution,
    metrics,
    supported,
    attach,
    connect,
    disconnect,
    reconnect
  }
}
