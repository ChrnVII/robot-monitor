<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { SystemApi } from '../api/client'
import { useAuthStore } from '../stores/auth'
import { useDeviceStore } from '../stores/device'
import { useLiveEvents } from '../composables/useLiveEvents'
import { formatBytes } from '../utils/format'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const device = useDeviceStore()
const { connect, disconnect, on, onReconnect } = useLiveEvents()

const stats = ref({ disk_free_bytes: 0, agents_online: 0, files: 0, pending_enrollments: 0 })

// 侧栏有两项：文件管理（下挂三个 tab）和节点（独立页面）。
// 「节点」不在文件管理下面，所以顶栏 tab 只在文件管理里出现。
const tabs = [
  { name: 'browse', label: '取件', icon: 'FolderOpened' },
  { name: 'tasks', label: '上传任务', icon: 'Tickets' },
  { name: 'files', label: '文件库', icon: 'Files' }
]

// 侧栏那一项在三个子页面里都该是选中态。这里不靠 router-link 的 active-class：
// 它按"目标路由是否被当前路由包含"算，而目标是子页面、当前也是子页面，兄弟之间
// 判不出来。
const inManage = computed(() => route.path.startsWith('/manage'))
const inNodes = computed(() => route.name === 'nodes')

// 顶栏节点选择器只出现在"内容由某台节点决定"的页面（route.meta.device）。
// 节点页是节点列表本身、文件库是全部节点的归档，放上去只会误导。
const showDevice = computed(() => Boolean(route.meta.device))
const selectedDevice = computed({
  get: () => device.id,
  set: (value) => device.select(value)
})

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
  device.load()
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
    if (['agent_online', 'agent_offline', 'agent_removed'].includes(event.type)) {
      device.load()
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
        <span class="brand-mark">RM</span>
        <span>
          Robot Monitor
          <span class="brand-sub">内网设备通道</span>
        </span>
      </div>

      <nav>
        <div class="nav-label">工作区</div>
        <router-link :to="{ name: 'browse' }" class="nav-item" :class="{ active: inManage }">
          <el-icon><Folder /></el-icon>
          <span>文件管理</span>
        </router-link>
        <router-link :to="{ name: 'nodes' }" class="nav-item" :class="{ active: inNodes }">
          <el-icon><Monitor /></el-icon>
          <span>节点</span>
          <span
            v-if="stats.pending_enrollments"
            class="nav-badge"
            :title="`${stats.pending_enrollments} 台设备等待批准`"
          >
            {{ stats.pending_enrollments }}
          </span>
        </router-link>
      </nav>
    </aside>

    <div class="main">
      <header class="topbar">
        <nav v-if="inManage" class="tabs">
          <router-link
            v-for="tab in tabs"
            :key="tab.name"
            :to="{ name: tab.name }"
            class="tab"
            active-class="active"
          >
            <el-icon><component :is="tab.icon" /></el-icon>
            {{ tab.label }}
          </router-link>
        </nav>
        <h1 v-else class="page-heading">{{ route.meta.title }}</h1>

        <div class="topbar-right">
          <!-- 全局节点选择：切一次，取件/任务都跟着换。只在受它影响的页面出现 -->
          <div v-if="showDevice" class="device-picker">
            <span class="device-label">节点</span>
            <span v-if="device.current" class="device-dot" :class="{ online: device.online }" />
            <el-select
              v-model="selectedDevice"
              size="default"
              placeholder="没有可用节点"
              :disabled="!device.agents.length"
            >
              <el-option
                v-for="agent in device.agents"
                :key="agent.id"
                :label="`${agent.name}${agent.online ? '' : '（离线）'}`"
                :value="agent.id"
              />
            </el-select>
          </div>

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
                <el-dropdown-item disabled>
                  {{ stats.agents_online }} 台节点在线
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
