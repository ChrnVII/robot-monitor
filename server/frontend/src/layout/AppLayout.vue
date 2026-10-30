<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { SystemApi } from '../api/client'
import { useAuthStore } from '../stores/auth'
import { useLiveEvents } from '../composables/useLiveEvents'
import { formatBytes } from '../utils/format'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const { connected, connect, disconnect, on, onReconnect } = useLiveEvents()

const stats = ref({ disk_free_bytes: 0, agents_online: 0, files: 0 })
const title = computed(() => route.meta.title || 'FileBridge')

const menu = [
  { name: 'nodes', label: '节点', icon: 'Monitor' },
  { name: 'browse', label: '取件', icon: 'FolderOpened' },
  { name: 'tasks', label: '任务', icon: 'Tickets' },
  { name: 'files', label: '文件库', icon: 'Box' }
]

let timer = null
let offEvent = null

async function loadStats() {
  try {
    const { data } = await SystemApi.stats()
    stats.value = data
  } catch {
    // 顶栏统计失败不该弹提示，下一次轮询会补上
  }
}

async function handleLogout() {
  await auth.logout()
  ElMessage.success('已退出登录')
  router.push({ name: 'login' })
}

onMounted(() => {
  loadStats()
  timer = setInterval(loadStats, 30000)
  connect()
  onReconnect(loadStats)
  offEvent = on((event) => {
    if (['agent_online', 'agent_offline', 'file_added'].includes(event.type)) {
      loadStats()
    }
  })
})

onUnmounted(() => {
  clearInterval(timer)
  offEvent?.()
  disconnect()
})
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="brand">
        <span class="brand-mark">FB</span>
        <span>FileBridge</span>
      </div>

      <nav>
        <router-link
          v-for="item in menu"
          :key="item.name"
          :to="{ name: item.name }"
          class="nav-item"
          active-class="active"
        >
          <el-icon><component :is="item.icon" /></el-icon>
          <span>{{ item.label }}</span>
        </router-link>
      </nav>

      <div class="sidebar-foot">
        <div class="sse" :class="{ on: connected }">
          <span class="dot" />
          {{ connected ? '实时连接正常' : '正在重连…' }}
        </div>
      </div>
    </aside>

    <div class="main">
      <header class="topbar">
        <h1>{{ title }}</h1>
        <div class="topbar-right">
          <span class="pill">
            <el-icon><Monitor /></el-icon>
            {{ stats.agents_online }} 节点在线
          </span>
          <span class="pill">
            <el-icon><Coin /></el-icon>
            {{ formatBytes(stats.disk_free_bytes) }} 可用
          </span>
          <el-dropdown trigger="click" @command="handleLogout">
            <span class="pill" style="cursor: pointer">
              <el-icon><User /></el-icon>
              {{ auth.user?.name || '管理员' }}
            </span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="logout">退出登录</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </header>

      <main class="content">
        <!-- 四个页面都缓存：取件页保"展开的目录/勾选清单"，文件库保 Tab 与
             搜索词，任务页保滚动位置，节点页保待批准列表的展开状态。
             规矩是：缓存可以，但每个页面必须在 onActivated 里静默对一次账，
             否则看到的是离开那一刻的旧数据。 -->
        <router-view v-slot="{ Component }">
          <keep-alive :include="['NodesView', 'BrowseView', 'TasksView', 'FilesView']">
            <component :is="Component" />
          </keep-alive>
        </router-view>
      </main>
    </div>
  </div>
</template>
