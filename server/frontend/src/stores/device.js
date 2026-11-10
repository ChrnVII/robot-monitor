import { defineStore } from 'pinia'
import { AgentsApi } from '../api/client'

// 顶栏那个"节点"下拉框的唯一状态源。
// 与节点强相关的页面（取件、任务，以及以后的状态页）都从这里读，切一次全局生效。
const STORAGE_KEY = 'fb:device'

function readStored() {
  try {
    return localStorage.getItem(STORAGE_KEY) || ''
  } catch {
    // 隐私模式/禁用存储：退化成"每次都用第一台"
    return ''
  }
}

function persist(id) {
  try {
    if (id) localStorage.setItem(STORAGE_KEY, id)
    else localStorage.removeItem(STORAGE_KEY)
  } catch {
    /* 记不住就算了，不影响使用 */
  }
}

export const useDeviceStore = defineStore('device', {
  state: () => ({ id: readStored(), agents: [], loaded: false }),
  getters: {
    current: (state) => state.agents.find((item) => item.id === state.id) || null,
    online: (state) => Boolean(state.agents.find((item) => item.id === state.id)?.online),
    name: (state) => state.agents.find((item) => item.id === state.id)?.name || ''
  },
  actions: {
    async load() {
      try {
        const { data } = await AgentsApi.list()
        this.agents = data.items || []
        // 记住的节点可能已经被删了：回落到第一台在线的，再不行就第一台
        if (!this.agents.some((item) => item.id === this.id)) {
          this.select((this.agents.find((item) => item.online) || this.agents[0])?.id || '')
        }
      } finally {
        this.loaded = true
      }
    },
    select(id) {
      this.id = id || ''
      persist(this.id)
    }
  }
})
