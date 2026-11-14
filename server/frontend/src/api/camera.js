import { api } from './client'

// 远程画面与云台。走同源 Cookie 会话：REST 用 axios，WS 用浏览器自带的 cookie。
// cameraId 是可选参数：不传表示"机器人默认的那台球形摄像头"。
// 云端不写任何默认编号——编号来自硬件配置，猜一次就会在换机型时错一次。
function cameraParams(cameraId) {
  return cameraId ? { cameraId } : undefined
}

export const CameraApi = {
  status: (agentId, cameraId) =>
    api.get(`/api/v1/agents/${agentId}/camera/stream`, { params: cameraParams(cameraId) }),
  stop: (agentId, cameraId) =>
    api.delete(`/api/v1/agents/${agentId}/camera/stream`, { params: cameraParams(cameraId) }),
  ptz: (agentId, payload) => api.post(`/api/v1/agents/${agentId}/camera/ptz`, payload),
  presets: (agentId, cameraId) =>
    api.get(`/api/v1/agents/${agentId}/camera/presets`, { params: cameraParams(cameraId) }),
  gotoPreset: (agentId, preset, params = {}) =>
    api.post(`/api/v1/agents/${agentId}/camera/presets/${preset}/goto`, null, { params })
}

/**
 * 观众端 WebSocket 地址。
 *
 * 不带 token：鉴权用同源 Cookie（会话），这比把会话 token 拼进 URL 更安全，
 * 也不会出现在代理日志里。
 */
export function liveSocketUrl(agentId, cameraId = '') {
  const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws'
  const base = `${scheme}://${window.location.host}/ws/live/${encodeURIComponent(agentId)}`
  return cameraId ? `${base}?${new URLSearchParams({ cameraId })}` : base
}

export function cameraStreamBlockedReason() {
  if (typeof window === 'undefined') return '当前环境不支持摄像头画面'
  if (typeof window.VideoDecoder !== 'function') {
    return '当前浏览器不支持 WebCodecs，无法播放实时画面（请用 Chrome/Edge 较新版本）'
  }
  if (window.isSecureContext === false) {
    return 'WebCodecs 需要 HTTPS 或 localhost，请通过 HTTPS 访问本系统'
  }
  return ''
}
