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

const stats = ref({ disk_free_bytes: 0, agents_online: 0, files: 0, pending_enrollments: 0 })

// 一级导航只有「文件管理」一项（侧栏）。
// 底下这四个是它的子页面，切换靠顶栏的 tab —— 见 router/index.js 的嵌套结构。
const tabs = [
  { name: 'nodes', label: '节点', icon: 'Monitor' },
  { name: 'browse', label: '取件', icon: 'FolderOpened' },
  { name: 'tasks', label: '任务', icon: 'Tickets' },
  { name: 'files', label: '文件库', icon: 'Box' }
]

// 侧栏那一项在四个子页面里都该是选中态。这里不靠 router-link 的 active-class：
// 它按"目标路由是否被当前路由包含"算，而目标是子页面、当前也是子页面，兄弟之间
// 判不出来。
const inManage = computed(() => route.path.startsWith('/manage'))

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
    if (
      ['agent_online', 'agent_offline', 'file_added', 'enroll_pending', 'enroll_resolved'].includes(
        event.type
      )
    ) {
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
        <router-link :to="{ name: 'nodes' }" class="nav-item" :class="{ active: inManage }">
          <el-icon><Folder /></el-icon>
          <span>文件管理</span>
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
        <nav class="tabs">
          <router-link
            v-for="tab in tabs"
            :key="tab.name"
            :to="{ name: tab.name }"
            class="tab"
            active-class="active"
          >
            {{ tab.label }}
            <span
              v-if="tab.name === 'nodes' && stats.pending_enrollments"
              class="tab-badge"
              :title="`${stats.pending_enrollments} 台设备等待批准`"
            >
              {{ stats.pending_enrollments }}
            </span>
          </router-link>
        </nav>

        <div class="topbar-right">
          <span class="pill">
            <el-icon><Monitor /></el-icon>
            {{ stats.agents_online }} 节点在线
          </span>
          <el-dropdown trigger="click" @command="handleLogout">
            <span class="pill" style="cursor: pointer">
              <el-icon><User /></el-icon>
              {{ auth.user?.name || '管理员' }}
            </span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item disabled>
                  磁盘可用 {{ formatBytes(stats.disk_free_bytes) }}
                </el-dropdown-item>
                <el-dropdown-item command="logout" divided>退出登录</el-dropdown-item>
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
