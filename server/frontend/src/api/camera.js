import { api } from './client'

/** 二进制帧头：flags(1) + pts_ms(4, LE) + seq(4, LE) + Annex-B。
 *  与 robot 侧 `app/video/codec.py`、云端 `protocol.py` 的 MEDIA_FLAG_* 一致。 */
export const FRAME_HEADER_LENGTH = 9
export const FRAME_FLAG = {
  KEY: 0x01, // 关键帧
  CONFIG: 0x02, // 配置帧（预留）
  RESET: 0x04, // 上游丢过帧，必须先 reset 解码器
  GOP_START: 0x08 // 降帧率跳帧后重新起步的首帧（P2）
}

/** 浏览器看的直播通道。Cookie 会自动带上，不需要额外 token。 */
export function liveStreamUrl(agentId, { cameraId = '', profile = 'low' } = {}) {
  if (!agentId) return ''
  const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws'
  const query = new URLSearchParams()
  if (cameraId) query.set('cameraId', cameraId)
  if (profile) query.set('profile', profile)
  const suffix = query.toString() ? `?${query}` : ''
  return `${scheme}://${window.location.host}/ws/live/${encodeURIComponent(agentId)}${suffix}`
}

export const CameraApi = {
  // 会话
  streamStatus: (agentId) => api.get(`/api/v1/agents/${agentId}/camera/stream`),
  streamStart: (agentId, payload = {}) =>
    api.post(`/api/v1/agents/${agentId}/camera/stream`, payload),
  streamProfile: (agentId, profile, { cameraId } = {}) =>
    api.post(`/api/v1/agents/${agentId}/camera/stream/profile`, { profile }, {
      params: cameraId ? { cameraId } : undefined
    }),
  streamStop: (agentId, { cameraId } = {}) =>
    api.delete(`/api/v1/agents/${agentId}/camera/stream`, {
      params: cameraId ? { cameraId } : undefined
    }),

  // 云台
  ptz: (agentId, payload) => api.post(`/api/v1/agents/${agentId}/camera/ptz`, payload),

  // 预置点
  presets: (agentId, params) =>
    api.get(`/api/v1/agents/${agentId}/camera/presets`, { params }),
  gotoPreset: (agentId, preset, payload = {}) =>
    api.post(`/api/v1/agents/${agentId}/camera/presets/${preset}/goto`, payload),

  // 快照：拿的是二进制，页面自己生成 object URL
  snapshot: (agentId, payload = {}) =>
    api.post(`/api/v1/agents/${agentId}/camera/snapshot`, payload, {
      responseType: 'blob',
      timeout: 20000
    }),

  // 命令日志：排障时先看这里，是没下发、被拒了，还是 unknown
  commands: (agentId, params) =>
    api.get(`/api/v1/agents/${agentId}/camera/commands`, { params })
}

export const StateApi = {
  // 状态 Twin 的当前值，兼作 REST 全量对账（SSE 不重放）
  get: (agentId) => api.get(`/api/v1/agents/${agentId}/state`),
  // 设备事件：按游标补齐断线期间缺的部分
  events: (params) => api.get('/api/v1/events', { params })
}
