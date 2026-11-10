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
      window.dispatchEvent(new CustomEvent('rm:unauthorized'))
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
  // deleteFiles=true 时把该节点的文件一并删掉（真删）；默认只注销节点、保留文件
  remove: (agentId, { deleteFiles = false } = {}) =>
    api.delete(`/api/v1/agents/${agentId}`, { params: { delete_files: deleteFiles } }),
  pending: () => api.get('/api/v1/agents/pending'),
  approve: (pendingId) => api.post(`/api/v1/agents/pending/${pendingId}/approve`),
  reject: (pendingId) => api.post(`/api/v1/agents/pending/${pendingId}/reject`)
}

export const TasksApi = {
  create: (payload) => api.post('/api/v1/tasks', payload),
  // 提交前先统计：勾中目录时，只有 B 遍历过才知道里面有多少文件
  preview: (payload) => api.post('/api/v1/tasks/preview', payload),
  // 真正建任务：立即返回 202，任务在后台逐项创建
  batch: (payload) => api.post('/api/v1/tasks/batch', payload),
  list: (params) => api.get('/api/v1/tasks', { params }),
  get: (taskId) => api.get(`/api/v1/tasks/${taskId}`),
  cancel: (taskId) => api.post(`/api/v1/tasks/${taskId}/cancel`)
}

export const FilesApi = {
  list: (params) => api.get('/api/v1/files', { params }),
  // 按目录列一层：子目录（递归统计）+ 直属文件。目录就是 rel_path 的前缀。
  browse: (params) => api.get('/api/v1/files/browse', { params }),
  // 整目录打包。用浏览器直接下载，不经过 axios——几 GB 的包不该先进内存。
  archiveUrl: (agentId, path) =>
    `/api/v1/files/archive?agent_id=${encodeURIComponent(agentId)}&path=${encodeURIComponent(path)}`,
  removeTree: (agentId, path) => api.delete('/api/v1/files/tree', { params: { agent_id: agentId, path } }),
  downloadUrl: (fileId) => `/api/v1/files/${fileId}/download`,
  remove: (fileId) => api.delete(`/api/v1/files/${fileId}`)
}

export const SystemApi = {
  stats: () => api.get('/api/v1/system/stats')
}
