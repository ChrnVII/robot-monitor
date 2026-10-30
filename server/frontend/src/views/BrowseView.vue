<script setup>
import { computed, onActivated, onDeactivated, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { AgentsApi, TasksApi, errorMessage } from '../api/client'
import { formatBytes } from '../utils/format'
import { useLiveEvents } from '../composables/useLiveEvents'

// keep-alive 按 name 点名缓存（见 AppLayout），切到别的页面再回来，
// 展开的目录、滚动位置、勾选的清单都还在。
defineOptions({ name: 'BrowseView' })

const route = useRoute()
const router = useRouter()
const live = useLiveEvents()
let off = null

// 记住"上次在看哪台节点"。两份存储各有分工：
// - URL query：从节点页点「浏览文件」进来时带着它，可刷新、可前进后退；
// - localStorage：从侧边栏点「取件」进来时没有 query，靠它恢复上次的选择。
const STORAGE_KEY = 'fb:browse-agent'

function readRememberedAgent() {
  try {
    return localStorage.getItem(STORAGE_KEY) || ''
  } catch {
    // 隐私模式/禁用存储：退化成"每次都从第一台开始"，不该因此报错
    return ''
  }
}

function rememberAgent(id) {
  try {
    if (id) localStorage.setItem(STORAGE_KEY, id)
    else localStorage.removeItem(STORAGE_KEY)
  } catch {
    /* 同上，记不住就算了 */
  }
}

const agents = ref([])
const agentId = ref(
  (typeof route.query.agent === 'string' ? route.query.agent : '') || readRememberedAgent()
)
const treeKey = ref(0)
// 根目录是否已经加载过。用来区分"树还是空的（等它上线要重建）"和
// "树里已经有用户展开的目录（绝不能重建）"。
const rootLoaded = ref(false)
const selected = ref([])
const submitting = ref(false)
const treeLoading = ref(false)
// 节点列表回来之前不挂载树：否则会拿着"上次记住但已被删掉的 id"先发一次请求，
// 闪一个 409 提示，然后再被纠正过来。
const agentsReady = ref(false)

const currentAgent = computed(() => agents.value.find((item) => item.id === agentId.value) || null)
const online = computed(() => Boolean(currentAgent.value?.online))
const totalBytes = computed(() => selected.value.reduce((sum, item) => sum + (item.size || 0), 0))

/**
 * 页面所处的状态。以前只有 `!online` 一个布尔，于是"还没加载出节点列表"
 * 被当成"节点离线"，进页面会闪一下警告。
 * loading = 列表还没回来；empty = 一台节点都没有；offline = 选中那台掉线。
 */
const viewState = computed(() => {
  if (!agentsReady.value) return 'loading'
  if (!agents.value.length || !agentId.value) return 'empty'
  return online.value ? 'online' : 'offline'
})

async function loadAgents() {
  try {
    const { data } = await AgentsApi.list()
    agents.value = data.items || []

    // 记住的节点可能已经被删了（或者换过机器）。这时才回落到第一台在线节点——
    // 不做这个校验的话，下拉框会停在一个不存在的 id 上，目录永远空着。
    if (!agents.value.some((item) => item.id === agentId.value)) {
      agentId.value = (agents.value.find((item) => item.online) || agents.value[0])?.id || ''
    }
    rememberAgent(agentId.value)
    syncQuery()
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    agentsReady.value = true
  }
}

/** 把当前选择写进 URL。用 replace，避免每次切换节点都往历史里塞一条记录。 */
function syncQuery() {
  // 兜底：这个组件被 keep-alive 缓存着，万一在别的页面时被触发，
  // 不能把用户拽回取件页。
  if (route.name !== 'browse') return
  const inUrl = typeof route.query.agent === 'string' ? route.query.agent : ''
  if (inUrl === agentId.value) return
  router.replace({
    name: 'browse',
    query: agentId.value ? { agent: agentId.value } : {}
  })
}

/** el-tree 的懒加载。目录展开时才请求，大目录也不会一次性拉全。 */
async function loadNode(node, resolve) {
  // 节点不在线就别发请求了：拿到的必然是 409，树下面还有一条黄色提示
  // 说明原因，再弹一个 toast 只是噪音。
  if (!agentId.value || !online.value) return resolve([])
  const path = node.level === 0 ? '' : node.data.rel
  treeLoading.value = true
  try {
    const { data } = await AgentsApi.listDir(agentId.value, { path, limit: 500 })
    const entries = data.entries || []
    const nodes = entries.map((entry) => ({ ...entry, leaf: entry.type !== 'dir' }))
    if (node.level === 0) rootLoaded.value = true

    // 超过一页时明确告诉用户"没显示全"，而不是悄悄截断——
    // 少了文件比报错更难发现。
    if (data.next_cursor) {
      nodes.push({
        name: `还有更多（共 ${data.total} 项，只显示前 ${entries.length} 项）`,
        rel: '',
        type: 'more',
        leaf: true,
        disabled: true
      })
    }
    resolve(nodes)
  } catch (error) {
    ElMessage.error(errorMessage(error))
    resolve([])
  } finally {
    treeLoading.value = false
  }
}

function isSelected(data) {
  return selected.value.some((item) => item.rel === data.rel)
}

function onNodeClick(data) {
  if (data.type !== 'file') return
  toggle(data)
}

function toggle(data) {
  if (data.type !== 'file') return
  const index = selected.value.findIndex((item) => item.rel === data.rel)
  if (index >= 0) selected.value.splice(index, 1)
  else selected.value.push({ rel: data.rel, name: data.name, size: data.size })
}

function removeAt(index) {
  selected.value.splice(index, 1)
}

function refreshTree() {
  treeKey.value += 1
  rootLoaded.value = false
}

function changeAgent() {
  selected.value = []
  refreshTree()
  rememberAgent(agentId.value)
  syncQuery()
}

async function submit() {
  if (!selected.value.length || submitting.value) return
  submitting.value = true

  const failures = []
  for (const item of selected.value) {
    try {
      await TasksApi.create({ agent_id: agentId.value, path: item.rel })
    } catch (error) {
      failures.push(`${item.name}：${errorMessage(error)}`)
    }
  }
  submitting.value = false

  if (failures.length) {
    ElMessage.error(failures.length === 1 ? failures[0] : `${failures[0]}（共 ${failures.length} 个失败）`)
    return
  }
  ElMessage.success(`已提交 ${selected.value.length} 个取件任务`)
  selected.value = []
  router.push({ name: 'tasks' })
}

// 同一条路由只改 query 时组件不会重建（前进/后退、或从别处跳到
// /browse?agent=xxx），所以这里要手动跟随 URL，否则页面会停在旧节点上。
watch(
  () => route.query.agent,
  (value) => {
    const next = typeof value === 'string' ? value : ''
    if (!next || next === agentId.value) return
    if (!agents.value.some((item) => item.id === next)) return
    agentId.value = next
    changeAgent()
  }
)

// 记住的节点可能是"离线但还在库里"的那台：等它上线时目录要自己出来。
// 但只在树还是空的时候重建——树里已经有用户展开的目录时，重建等于把他的
// 操作清空，那比"目录不出来"更烦人。
watch(online, (isOnline) => {
  if (isOnline && !rootLoaded.value) refreshTree()
})

// 订阅 A 推来的在线状态。离开页面时必须退订：这个组件被缓存着，
// 若还在收事件，loadAgents 会顺手把 URL 改回 /browse，等于把用户拽回来。
function startWatching() {
  off?.()
  off = live.on((event) => {
    if (['agent_online', 'agent_offline', 'agent_removed'].includes(event.type)) loadAgents()
  })
}

function stopWatching() {
  off?.()
  off = null
}

let firstActivate = true

onMounted(() => {
  loadAgents()
  startWatching()
})

onActivated(() => {
  // 首次挂载时 activated 紧跟在 mounted 后面，不要重复加载
  if (firstActivate) {
    firstActivate = false
    return
  }
  // 从别的页面回来：只刷新节点在线状态，不动目录树（保留展开状态）
  startWatching()
  loadAgents()
})

onDeactivated(stopWatching)
onUnmounted(stopWatching)
</script>

<template>
  <div>
    <div class="panel" style="margin-bottom: 18px">
      <div class="panel-body" style="display: flex; align-items: center; gap: 14px; flex-wrap: wrap">
        <span class="muted">节点</span>
        <el-select v-model="agentId" style="width: 240px" @change="changeAgent">
          <el-option
            v-for="agent in agents"
            :key="agent.id"
            :label="`${agent.name}${agent.online ? '' : '（离线）'}`"
            :value="agent.id"
            :disabled="!agent.online"
          />
        </el-select>

        <el-tag v-if="currentAgent?.roots?.length" type="info" effect="plain">
          <span class="mono">{{ currentAgent.roots[0] }}</span>
        </el-tag>

        <div style="margin-left: auto; display: flex; gap: 10px">
          <el-button text :disabled="!online" @click="refreshTree">
            <el-icon><Refresh /></el-icon>
            <span style="margin-left: 4px">刷新目录</span>
          </el-button>
        </div>
      </div>
    </div>

    <!-- 两种"没东西可看"要分开说：一台节点都没有，和选中的那台掉线了 -->
    <el-alert
      v-if="viewState === 'offline'"
      type="warning"
      :closable="false"
      show-icon
      title="节点不在线"
      description="取件需要 B 主动连着 A。请确认 B 上的 agent 在运行、token 未吊销。"
      style="margin-bottom: 18px"
    />
    <el-alert
      v-else-if="viewState === 'empty'"
      type="info"
      :closable="false"
      show-icon
      title="还没有可用的节点"
      description="在 B 上启动 agent 后，它会出现在「节点」页；批准接入后就能在这里浏览文件。"
      style="margin-bottom: 18px"
    />

    <div class="browse-grid">
      <div class="panel tree-panel">
        <div class="panel-head">
          <h2>目录</h2>
          <span class="muted">点击文件加入右侧清单</span>
        </div>
        <div class="tree-scroll" v-loading="viewState === 'loading' || treeLoading">
          <el-tree
            v-if="viewState === 'online' || viewState === 'offline'"
            :key="`${agentId}-${treeKey}`"
            :load="loadNode"
            lazy
            :props="{ label: 'name', isLeaf: 'leaf', disabled: 'disabled' }"
            node-key="rel"
            highlight-current
            @node-click="onNodeClick"
          >
            <template #default="{ data }">
              <span class="tree-node" :class="{ selected: isSelected(data) }">
                <el-icon class="node-icon">
                  <Folder v-if="data.type === 'dir'" />
                  <Document v-else-if="data.type === 'file'" />
                  <Link v-else-if="data.type === 'symlink'" />
                  <MoreFilled v-else />
                </el-icon>
                <span class="node-name">{{ data.name }}</span>
                <span v-if="data.type === 'file'" class="node-size">{{ formatBytes(data.size) }}</span>
                <el-icon v-if="isSelected(data)" class="check"><Select /></el-icon>
              </span>
            </template>
          </el-tree>
          <div
            v-if="viewState === 'empty'"
            class="muted"
            style="padding: 40px 12px; text-align: center"
          >
            还没有接入任何节点
          </div>
        </div>
      </div>

      <div class="panel">
        <div class="panel-head">
          <h2>取件清单（{{ selected.length }}）</h2>
          <el-button v-if="selected.length" text @click="selected = []">清空</el-button>
        </div>

        <div class="panel-body">
          <div v-if="!selected.length" class="muted" style="padding: 32px 0; text-align: center">
            左侧点击文件加入清单。目录需要先展开。
          </div>
          <div v-else class="selected-list">
            <div v-for="(item, index) in selected" :key="item.rel" class="selected-row">
              <el-icon style="color: #8f9bb3"><Document /></el-icon>
              <span class="name" :title="item.rel">{{ item.rel }}</span>
              <span class="muted mono">{{ formatBytes(item.size) }}</span>
              <el-button text size="small" @click="removeAt(index)">
                <el-icon><Close /></el-icon>
              </el-button>
            </div>
          </div>
        </div>

        <div class="submit-bar">
          <span class="muted">合计 {{ formatBytes(totalBytes) }}</span>
          <el-button
            type="primary"
            :loading="submitting"
            :disabled="!selected.length || !online"
            @click="submit"
          >
            提交取件
          </el-button>
        </div>
      </div>
    </div>
  </div>
</template>
