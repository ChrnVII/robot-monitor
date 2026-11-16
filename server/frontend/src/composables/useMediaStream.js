// 云端实时画面：WebSocket 收 Annex-B → WebCodecs 硬解 → canvas。
//
// 移植自 robot-frontend 的 useCameraStream.js（解码与渲染那套是对的，不重写），
// 补上云端特有的三件事：
//
//   1. **状态机要说真话。** connecting → waiting → live → stale(2s 无帧)
//      → frozen(5s 无帧) → reconnecting。旧版远程监控最误导人的地方就是
//      画面早就冻了，页面还写着"直播中"。
//   2. **保留最后一帧。** 冻结时不要黑屏——叠一行"最后画面 3 秒前"，
//      用户至少还能看到现场。
//   3. **退避带抖动，且只在"稳定 10s + 收到过首帧"之后清零。** 用 onopen
//      清零会让"连上就断"以 1s 的节奏无限重试。
import { computed, onBeforeUnmount, reactive, readonly, ref } from 'vue'
import { FRAME_FLAG, FRAME_HEADER_LENGTH, liveStreamUrl } from '../api/camera'

const STALE_AFTER = 2000
const FROZEN_AFTER = 5000
const RECONNECT_BASE = 1000
const RECONNECT_MAX = 30000
const STABLE_AFTER = 10000
const MAX_DECODE_QUEUE = 8

const painters = new Set()
let sharedFrame = null

function scheduleFrame() {
  if (sharedFrame !== null) return
  sharedFrame = window.requestAnimationFrame(runFrame)
}

function runFrame(now) {
  sharedFrame = null
  for (const paint of painters) {
    try {
      paint(now)
    } catch (error) {
      console.error('[media-stream] render failed', error)
    }
  }
  if (painters.size) scheduleFrame()
}

/** WebCodecs 不可用的原因（不满足时页面直接说明，不要转圈）。 */
export function mediaBlockReason() {
  if (!window.isSecureContext) return '需要 HTTPS 才能解码实时画面'
  if (typeof window.VideoDecoder === 'undefined') return '当前浏览器不支持 WebCodecs'
  return ''
}

export function useMediaStream(options = {}) {
  // idle | connecting | waiting | live | stale | frozen | reconnecting |
  // agent_offline | stream_disabled | unsupported_codec
  const phase = ref('idle')
  const errorText = ref('')
  const blockReason = ref('')
  const codec = ref('')
  const profile = ref('')
  const resolution = reactive({ width: 0, height: 0 })
  const metrics = reactive({
    fps: 0,
    latencyMs: null,
    droppedFrames: 0,
    receivedFrames: 0,
    decodedFrames: 0,
    receivedKeyframes: 0,
    bitrateKbps: null,
    lastFrameAgoMs: null
  })

  let socket = null
  let decoder = null
  let videoContext = null
  let videoCanvas = null
  let painting = false
  let framePending = null
  let needReset = true
  let lastFrameAt = 0
  let firstFrameAt = 0
  let disposed = false
  let reconnectAttempts = 0
  let reconnectTimer = null
  let stableTimer = null
  let timelineEpoch = 0
  let lastSampleAt = 0
  let lastSampleFrames = 0
  let onServerMessage = null
  let currentUrl = ''

  const permanent = computed(
    () => phase.value === 'unsupported_codec' || phase.value === 'stream_disabled'
  )

  const statusText = computed(() => {
    if (blockReason.value) return blockReason.value
    switch (phase.value) {
      case 'connecting':
        return '正在连接实时画面…'
      case 'waiting':
        return '等待摄像头画面…'
      case 'live':
        return `实时画面 · ${codec.value || '解码中'}`
      case 'stale':
        return `画面卡顿 · 最后帧 ${Math.round((metrics.lastFrameAgoMs || 0) / 1000)} 秒前`
      case 'frozen':
        return `画面停滞 · 最后帧 ${Math.round((metrics.lastFrameAgoMs || 0) / 1000)} 秒前`
      case 'reconnecting':
        return '画面中断，正在重连…'
      case 'agent_offline':
        return '设备离线'
      case 'stream_disabled':
        return errorText.value || '该设备无法提供实时画面'
      case 'unsupported_codec':
        return errorText.value || '当前环境不支持这个编码'
      default:
        return '实时画面待启用'
    }
  })

  const statusType = computed(() => {
    if (blockReason.value) return 'warning'
    if (phase.value === 'live') return 'success'
    if (phase.value === 'stale') return 'warning'
    if (['frozen', 'reconnecting'].includes(phase.value)) return 'warning'
    if (['stream_disabled', 'unsupported_codec', 'agent_offline'].includes(phase.value)) return 'danger'
    return 'info'
  })

  /* ------------------------------ 解码器 ------------------------------ */

  function makeDecoder() {
    if (!codec.value) return
    const reason = mediaBlockReason()
    if (reason) {
      blockReason.value = reason
      return
    }
    if (decoder) {
      try {
        decoder.close()
      } catch {
        /* 已关闭 */
      }
    }
    errorText.value = ''
    try {
      decoder = new VideoDecoder({ output: onDecodedFrame, error: onDecoderError })
      decoder.configure({ codec: codec.value, optimizeForLatency: true })
      needReset = true
    } catch (reason) {
      // 编码字符串不被支持是**永久错误**：重连一万次也没用，如实告诉用户。
      errorText.value = `解码器无法使用 ${codec.value}：${reason?.message || reason}`
      phase.value = 'unsupported_codec'
    }
  }

  function onDecoderError(event) {
    console.error('[media-stream] decoder error', event)
    try {
      decoder?.close()
    } catch {
      /* 已关闭 */
    }
    decoder = null
    needReset = true // 等下一个关键帧重建
  }

  function onDecodedFrame(frame) {
    if (disposed) {
      frame.close()
      return
    }
    if (framePending) framePending.close()
    framePending = frame
  }

  /* ------------------------------ 渲染 ------------------------------ */

  function paint(now) {
    if (!videoContext || !videoCanvas) return
    if (framePending) {
      const frame = framePending
      framePending = null
      const width = frame.displayWidth
      const height = frame.displayHeight
      if (width && height) {
        const rect = videoCanvas.getBoundingClientRect()
        const ratio = Math.min(window.devicePixelRatio || 1, 2)
        const target = Math.min(
          (rect.width || width) * ratio / width,
          (rect.height || height) * ratio / height,
          1
        )
        const drawWidth = Math.max(1, Math.round(width * target))
        const drawHeight = Math.max(1, Math.round(height * target))
        if (videoCanvas.width !== drawWidth || videoCanvas.height !== drawHeight) {
          videoCanvas.width = drawWidth
          videoCanvas.height = drawHeight
        }
        videoContext.drawImage(frame, 0, 0, drawWidth, drawHeight)
        resolution.width = width
        resolution.height = height

        const pts = Math.round(frame.timestamp / 1000)
        if (timelineEpoch) {
          const latency = Date.now() - (timelineEpoch + pts)
          metrics.latencyMs = latency >= 0 && latency < 60000 ? latency : null
        }
        lastFrameAt = now
        metrics.decodedFrames += 1
        if (!firstFrameAt) firstFrameAt = now
        // 只有真的画出了帧才算 live —— 连接开着不算。
        if (['waiting', 'stale', 'frozen', 'reconnecting'].includes(phase.value)) {
          phase.value = 'live'
        }
      }
      frame.close()
    }

    metrics.lastFrameAgoMs = lastFrameAt ? now - lastFrameAt : null
    if (phase.value === 'live' && lastFrameAt && now - lastFrameAt > FROZEN_AFTER) {
      phase.value = 'frozen'
    } else if (phase.value === 'live' && lastFrameAt && now - lastFrameAt > STALE_AFTER) {
      phase.value = 'stale'
    }

    if (now - lastSampleAt >= 1000) {
      const elapsed = now - lastSampleAt || 1000
      metrics.fps =
        Math.round((((metrics.decodedFrames - lastSampleFrames) * 1000) / elapsed) * 10) / 10
      lastSampleFrames = metrics.decodedFrames
      lastSampleAt = now
    }
  }

  /* ------------------------------ WS ------------------------------ */

  function clearReconnect() {
    if (reconnectTimer) {
      window.clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
  }

  function scheduleReconnect(reason) {
    if (disposed || permanent.value) return
    clearReconnect()
    const jitter = 0.5 + Math.random() // base * 2^n * (0.5~1.5)
    const delay = Math.min(RECONNECT_BASE * 2 ** reconnectAttempts * jitter, RECONNECT_MAX)
    reconnectAttempts += 1
    phase.value = reason || 'reconnecting'
    reconnectTimer = window.setTimeout(() => {
      reconnectTimer = null
      connect()
    }, delay)
  }

  function handleText(raw) {
    let message
    try {
      message = JSON.parse(raw)
    } catch {
      return
    }
    if (message.t === 'config') {
      timelineEpoch = Number(message.t0) || 0
      if (Array.isArray(message.size) && message.size.length === 2) {
        resolution.width = Number(message.size[0]) || 0
        resolution.height = Number(message.size[1]) || 0
      }
      profile.value = message.profile || ''
      if (message.codec && message.codec !== codec.value) {
        codec.value = String(message.codec)
        makeDecoder()
      }
      return
    }
    if (message.t === 'error') {
      const code = message.code || 'stream_disabled'
      errorText.value = message.message || '实时画面不可用'
      if (code === 'agent_offline') {
        // 设备离线是暂时状态：等设备回来自己能连上，但不能高频重试。
        phase.value = 'agent_offline'
        closeSocket()
        scheduleReconnect('agent_offline')
        return
      }
      // 其余（stream_disabled / unknown_session / camera_unavailable）都不自动重连：
      // 重连一万次也不会变好，页面该显示原因而不是转圈。
      phase.value = 'stream_disabled'
      closeSocket()
      return
    }
    if (message.t === 'stats') {
      metrics.bitrateKbps = message.bitrate_kbps ?? null
    }
    onServerMessage?.(message)
  }

  function handleBinary(buffer) {
    if (buffer.byteLength <= FRAME_HEADER_LENGTH) return
    const view = new DataView(buffer)
    const flags = view.getUint8(0)
    const pts = view.getUint32(1, true)
    const payload = new Uint8Array(buffer, FRAME_HEADER_LENGTH)

    metrics.receivedFrames += 1
    if (flags & FRAME_FLAG.KEY) metrics.receivedKeyframes += 1
    if (flags & FRAME_FLAG.CONFIG) return

    const key = Boolean(flags & FRAME_FLAG.KEY)
    if (flags & FRAME_FLAG.RESET || (decoder && decoder.decodeQueueSize > MAX_DECODE_QUEUE)) {
      needReset = true
      metrics.droppedFrames += 1
    }
    if (needReset || !decoder) {
      if (!key) return
      makeDecoder()
      if (!decoder || decoder.state !== 'configured') return
      needReset = false
    }

    try {
      decoder.decode(
        new EncodedVideoChunk({
          type: key ? 'key' : 'delta',
          timestamp: pts * 1000, // WebCodecs 要微秒
          data: payload
        })
      )
    } catch (error) {
      console.error('[media-stream] decode failed', error)
      needReset = true
    }
  }

  function closeSocket() {
    if (!socket) return
    const current = socket
    socket = null
    current.onopen = null
    current.onmessage = null
    current.onerror = null
    current.onclose = null
    try {
      current.close()
    } catch {
      /* 已关闭 */
    }
  }

  function connect() {
    if (disposed || !videoCanvas) return
    const url = options.url ? options.url() : liveStreamUrl(options.agentId?.value ?? options.agentId, {
      cameraId: options.cameraId?.value ?? options.cameraId,
      profile: 'low'
    })
    if (!url) {
      phase.value = 'stream_disabled'
      errorText.value = '还没有选择设备'
      return
    }
    currentUrl = url
    closeSocket()
    needReset = true
    if (framePending) {
      framePending.close()
      framePending = null
    }
    clearReconnect()
    phase.value = reconnectAttempts ? 'reconnecting' : 'connecting'
    try {
      socket = new WebSocket(url)
    } catch (reason) {
      errorText.value = String(reason)
      scheduleReconnect('reconnecting')
      return
    }
    socket.binaryType = 'arraybuffer'
    socket.onopen = () => {
      phase.value = 'waiting'
      // 注意：这里**不**清零退避。清零的条件是"稳定 10s 并且真的在放"——
      // 用 onopen 清零会让"连上就断"以 1s 的节奏无限重试。
      if (stableTimer) window.clearTimeout(stableTimer)
      stableTimer = window.setTimeout(() => {
        if (!disposed && firstFrameAt && phase.value === 'live') reconnectAttempts = 0
      }, STABLE_AFTER)
    }
    socket.onmessage = (event) => {
      if (typeof event.data === 'string') handleText(event.data)
      else if (event.data instanceof ArrayBuffer) handleBinary(event.data)
    }
    socket.onerror = () => {
      if (!permanent.value) errorText.value = '实时画面连接错误'
    }
    socket.onclose = () => {
      socket = null
      if (disposed || permanent.value) return
      needReset = true
      metrics.latencyMs = null
      scheduleReconnect('reconnecting')
    }
  }

  /* ------------------------------ 对外 ------------------------------ */

  function startPainting() {
    if (painting || disposed) return
    painting = true
    lastSampleAt = performance.now()
    painters.add(paint)
    scheduleFrame()
  }

  function attach(canvas) {
    if (!canvas) return
    videoCanvas = canvas
    videoContext = canvas.getContext('2d', { alpha: false, desynchronized: true })
    startPainting()

    const reason = mediaBlockReason()
    if (reason) {
      blockReason.value = reason
      phase.value = 'unsupported_codec'
      return
    }
    if (!socket) connect()
  }

  function detach() {
    closeSocket()
    clearReconnect()
    if (stableTimer) {
      window.clearTimeout(stableTimer)
      stableTimer = null
    }
    if (framePending) {
      try {
        framePending.close()
      } catch {
        /* 已释放 */
      }
      framePending = null
    }
    if (decoder) {
      try {
        decoder.close()
      } catch {
        /* 已关闭 */
      }
      decoder = null
    }
    painters.delete(paint)
    painting = false
    videoContext = null
    videoCanvas = null
  }

  function dispose() {
    disposed = true
    detach()
  }

  /** 手动重连（页面「刷新画面」按钮）。 */
  function reconnect() {
    if (permanent.value) return
    reconnectAttempts = 0
    connect()
  }

  /** 换设备/换相机时调用：断开重来。 */
  function reset() {
    firstFrameAt = 0
    lastFrameAt = 0
    reconnectAttempts = 0
    metrics.receivedFrames = 0
    metrics.receivedKeyframes = 0
    metrics.decodedFrames = 0
    metrics.droppedFrames = 0
    codec.value = ''
    phase.value = 'idle'
    closeSocket()
    connect()
  }

  onBeforeUnmount(dispose)

  return {
    phase,
    statusText,
    statusType,
    errorText: readonly(errorText),
    blockReason: readonly(blockReason),
    codec: readonly(codec),
    profile: readonly(profile),
    resolution,
    metrics,
    attach,
    detach,
    dispose,
    reconnect,
    reset
  }
}
