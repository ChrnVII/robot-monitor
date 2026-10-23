import { defineStore } from 'pinia'
import { AuthApi } from '../api/client'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    user: null,
    checked: false
  }),
  getters: {
    isLoggedIn: (state) => Boolean(state.user)
  },
  actions: {
    async fetchMe() {
      try {
        const { data } = await AuthApi.me()
        this.user = { name: data.username, expiresAt: data.expires_at }
      } catch {
        // 401 是正常状态（还没登录），不当作错误
        this.user = null
      } finally {
        this.checked = true
      }
      return this.user
    },
    async login(username, password) {
      const { data } = await AuthApi.login({ username, password })
      this.user = { name: data.username, expiresAt: data.expires_at }
      this.checked = true
      return data
    },
    async logout() {
      try {
        await AuthApi.logout()
      } catch {
        // 会话可能已经过期，登出失败不影响本地清理
      }
      this.user = null
    }
  }
})
