export function formatBytes(value) {
  const bytes = Number(value || 0)
  if (bytes <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1)
  const size = bytes / Math.pow(1024, index)
  return `${size >= 100 || index === 0 ? size.toFixed(0) : size.toFixed(1)} ${units[index]}`
}

export function formatTime(timestamp) {
  if (!timestamp) return '—'
  const date = new Date(Number(timestamp) * 1000)
  const pad = (n) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours()
  )}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

export function relativeTime(timestamp) {
  if (!timestamp) return '从未'
  const seconds = Math.max(0, Date.now() / 1000 - Number(timestamp))
  if (seconds < 10) return '刚刚'
  if (seconds < 60) return `${Math.floor(seconds)} 秒前`
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`
  return `${Math.floor(seconds / 86400)} 天前`
}

/** 状态 → 文案与标签色。前端只按 status 判断，不猜业务含义。 */
export const TASK_STATUS = {
  pending: { text: '准备中', type: 'info' },
  uploading: { text: '上传中', type: 'primary' },
  verifying: { text: '校验中', type: 'warning' },
  done: { text: '已完成', type: 'success' },
  failed: { text: '失败', type: 'danger' },
  canceled: { text: '已取消', type: 'info' }
}

export function taskStatus(status) {
  return TASK_STATUS[status] || { text: status || '未知', type: 'info' }
}
