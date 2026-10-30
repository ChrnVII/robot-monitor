import axios from 'axios'

// 同源部署时 baseURL 留空即可；开发时由 Vite proxy 转发。
export const api = axios.create({
  baseURL: '',
  timeout: 30000
})

// 401 统一处理：交给 router 的守卫没意义（它不会因为一次请求失败而重新导航），
// 这里直接广播一个事件，由 App 统一跳登录页。
api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      window.dispatchEvent(new CustomEvent('fb:unauthorized'))
    }
    return Promise.reject(error)
  }
)

/** 后端所有错误都是 { code, message } 形状；FastAPI 自身的错误是 { detail: {...} }。 */
export function errorMessage(error) {
  const body = error?.response?.data
  if (!body) return error?.message || '请求失败'
  if (typeof body === 'string') return body
  if (body.message) return body.message
  if (body.detail?.message) return body.detail.message
  if (typeof body.detail === 'string') return body.detail
  return error.message || '请求失败'
}

export const AuthApi = {
  login: (payload) => api.post('/api/v1/auth/login', payload),
  logout: () => api.post('/api/v1/auth/logout'),
  me: () => api.get('/api/v1/auth/me')
}

export const AgentsApi = {
  list: () => api.get('/api/v1/agents'),
  listDir: (agentId, payload) => api.post(`/api/v1/agents/${agentId}/list`, payload),
  // deleteFiles=true 时把该节点的文件一并移入回收站；默认只注销节点、保留文件
  remove: (agentId, { deleteFiles = false } = {}) =>
    api.delete(`/api/v1/agents/${agentId}`, { params: { delete_files: deleteFiles } }),
  pending: () => api.get('/api/v1/agents/pending'),
  approve: (pendingId) => api.post(`/api/v1/agents/pending/${pendingId}/approve`),
  reject: (pendingId) => api.post(`/api/v1/agents/pending/${pendingId}/reject`)
}

export const TasksApi = {
  create: (payload) => api.post('/api/v1/tasks', payload),
  list: (params) => api.get('/api/v1/tasks', { params }),
  get: (taskId) => api.get(`/api/v1/tasks/${taskId}`),
  cancel: (taskId) => api.post(`/api/v1/tasks/${taskId}/cancel`)
}

export const FilesApi = {
  list: (params) => api.get('/api/v1/files', { params }),
  downloadUrl: (fileId) => `/api/v1/files/${fileId}/download`,
  remove: (fileId) => api.delete(`/api/v1/files/${fileId}`),
  restore: (fileId) => api.post(`/api/v1/files/${fileId}/restore`),
  purge: (fileId) => api.delete(`/api/v1/files/${fileId}/purge`)
}

export const SystemApi = {
  stats: () => api.get('/api/v1/system/stats'),
  audit: (limit = 100) => api.get('/api/v1/system/audit', { params: { limit } })
}
