<script setup>
import { computed, onActivated, onDeactivated, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { AgentsApi, FilesApi, errorMessage } from '../api/client'
import { dirOf, formatBytes, formatTime } from '../utils/format'
import { useLiveEvents } from '../composables/useLiveEvents'

// 被 keep-alive 缓存：切走再回来，当前目录和树上的展开状态都还在
defineOptions({ name: 'FilesView' })

const { on } = useLiveEvents()

const query = ref('')
const loading = ref(false)

// 节点列表只用来搭树的根和给"来源"列取名字
const agents = ref([])
const agentNames = ref({})

const treeRef = ref(null)
const treeKey = ref(0)

// 当前所在目录。agent + path 是这一页唯一的"位置"状态。
const current = ref({ agent: '', path: '' })
const listing = ref({ dirs: [], files: [], total_files: 0, total_size: 0 })

// 搜索结果：跨目录、跨节点，是一条平铺的路
const results = ref([])
const resultTotal = ref(0)
let searchTimer = null

const searching = computed(() => Boolean(query.value.trim()))
const segments = computed(() => current.value.path.split('/').filter(Boolean))
const emptyText = computed(() => '这个目录是空的')

// --- 删除与撤销 ---------------------------------------------------------
//
// 服务端是真删，没有回收站。反悔的机会只有这里的 8 秒：这段时间里请求**还没发**，
// 撤销就是"什么都不做"。所以：
//   - 点删除先只改界面，让行立刻消失；
//   - 8 秒到了才真的发请求；
//   - 期间又删了别的、或者切走了页面，前一次立即生效（不做静默丢弃）。

const pending = ref(null)
const UNDO_MS = 8000

function scheduleDelete(label, commit) {
  flushPending()
  const timer = setTimeout(() => {
    pending.value = null
    commit()
  }, UNDO_MS)
  pending.value = { label, commit, timer }
}

function flushPending() {
  const job = pending.value
  if (!job) return
  clearTimeout(job.timer)
  pending.value = null
  job.commit()
}

function undoDelete() {
  const job = pending.value
  if (!job) return
  clearTimeout(job.timer)
  pending.value = null
  loadListing({ silent: true })
  ElMessage.success('已撤销')
}

function agentName(id) {
  if (!id) return '—'
  return agentNames.value[id] || `已移除的节点（${id.slice(0, 6)}）`
}

/** 类型块的文字：取扩展名，太长的截到 3 个字符。 */
function extOf(name) {
  const dot = name.lastIndexOf('.')
  if (dot <= 0 || dot === name.length - 1) return 'FILE'
  return name.slice(dot + 1, dot + 4).toUpperCase()
}

// --- 树的懒加载 ---------------------------------------------------------

async function loadAgents() {
  try {
    const { data } = await AgentsApi.list()
    agents.value = data.items || []
    agentNames.value = Object.fromEntries(agents.value.map((item) => [item.id, item.name]))
    if (!current.value.agent && agents.value.length) {
      current.value = { agent: agents.value[0].id, path: '' }
      loadListing()
    }
  } catch (error) {
    ElMessage.error(errorMessage(error))
  }
}

async function loadNode(node, resolve) {
  if (node.level === 0) {
    return resolve(
      agents.value.map((agent) => ({
        key: `${agent.id}:`,
        label: agent.name,
        count: agent.files,
        agent: agent.id,
        path: '',
        leaf: false
      }))
    )
  }

  const { agent, path } = node.data
  try {
    const { data } = await FilesApi.browse({ agent_id: agent, path })
    resolve(
      data.dirs.map((dir) => ({
        key: `${agent}:${dir.path}`,
        label: dir.name,
        count: dir.files,
        agent,
        path: dir.path,
        leaf: false
      }))
    )
  } catch (error) {
    ElMessage.error(errorMessage(error))
    resolve([])
  }
}

// --- 右栏 ---------------------------------------------------------------

async function loadListing({ silent = false } = {}) {
  if (!current.value.agent) return
  if (!silent) loading.value = true
  try {
    const { data } = await FilesApi.browse({
      agent_id: current.value.agent,
      path: current.value.path
    })
    listing.value = data
  } catch (error) {
    ElMessage.error(errorMessage(error))
    listing.value = { dirs: [], files: [], total_files: 0, total_size: 0 }
  } finally {
    loading.value = false
  }
}

function openFolder(agent, path) {
  query.value = ''
  current.value = { agent, path }
  treeRef.value?.setCurrentKey(`${agent}:${path}`)
  loadListing()
}

function onNodeClick(data) {
  openFolder(data.agent, data.path)
}

function pathAt(index) {
  return segments.value.slice(0, index + 1).join('/')
}

// --- 操作 ---------------------------------------------------------------

function downloadDir(path) {
  window.location.href = FilesApi.archiveUrl(current.value.agent, path)
}

function removeFile(file) {
  // 先从界面拿掉：撤销时整页重载就能回来，不需要自己维护"影子行"
  const index = listing.value.files.findIndex((item) => item.id === file.id)
  if (index >= 0) listing.value.files.splice(index, 1)
  listing.value.total_files = Math.max(0, listing.value.total_files - 1)
  listing.value.total_size = Math.max(0, listing.value.total_size - file.size)

  scheduleDelete(`已删除「${file.name}」`, async () => {
    try {
      await FilesApi.remove(file.id)
      loadListing({ silent: true })
    } catch (error) {
      ElMessage.error(errorMessage(error))
      loadListing()
    }
  })
}

function removeDir(dir) {
  const index = listing.value.dirs.findIndex((item) => item.path === dir.path)
  if (index >= 0) listing.value.dirs.splice(index, 1)
  listing.value.total_files = Math.max(0, listing.value.total_files - dir.files)
  listing.value.total_size = Math.max(0, listing.value.total_size - dir.size)

  scheduleDelete(`已删除「${dir.name}」（${dir.files} 个文件）`, async () => {
    try {
      await FilesApi.removeTree(current.value.agent, dir.path)
      loadListing({ silent: true })
    } catch (error) {
      ElMessage.error(errorMessage(error))
      loadListing()
    }
  })
}

// --- 搜索 ---------------------------------------------------------------

function search() {
  clearTimeout(searchTimer)
  if (!query.value.trim()) {
    results.value = []
    return
  }
  searchTimer = setTimeout(async () => {
    loading.value = true
    try {
      const { data } = await FilesApi.list({ q: query.value.trim(), limit: 100 })
      results.value = data.items || []
      resultTotal.value = data.total || 0
    } catch (error) {
      ElMessage.error(errorMessage(error))
    } finally {
      loading.value = false
    }
  }, 250)
}

/** 从搜索结果跳回它所在的目录。 */
function reveal(item) {
  const path = item.rel_path && item.name ? dirOf(item.rel_path, item.name).replace(/\/$/, '') : ''
  openFolder(item.agent_id, path)
}

// --- 生命周期 -----------------------------------------------------------

function startWatching() {
  off?.()
  off = on((event) => {
    // 有未提交的删除时不刷新：否则刚被乐观移除的行会被服务器数据带回来
    if (pending.value) return
    if (event.type === 'files_changed' || event.type === 'file_removed') {
      loadListing({ silent: true })
      if (searching.value) search()
    }
  })
}

function stopWatching() {
  off?.()
  off = null
}

let off = null
let firstActivate = true

onMounted(() => {
  loadAgents()
  startWatching()
})

onActivated(() => {
  if (firstActivate) {
    firstActivate = false
    return
  }
  loadAgents()
  loadListing({ silent: true })
  startWatching()
})

onDeactivated(() => {
  stopWatching()
  clearTimeout(searchTimer)
  // 切走了就把还没到期的删除执行掉：宁可删掉，也不要悄悄什么都没做
  flushPending()
})

onUnmounted(() => {
  stopWatching()
  clearTimeout(searchTimer)
  flushPending()
})
</script>

<template>
  <div class="panel files-panel">
    <div class="panel-head">
      <h2>文件库</h2>
      <el-input
        v-model="query"
        placeholder="搜索文件名"
        clearable
        style="max-width: 280px"
        @input="search"
        @clear="search"
      >
        <template #prefix>
          <el-icon><Search /></el-icon>
        </template>
      </el-input>
    </div>

    <!-- 搜索态：跨目录平铺，点一条跳回它所在的目录 -->
    <div v-if="searching" class="search-pane">
      <el-table :data="results" v-loading="loading" style="width: 100%" empty-text="没有匹配的文件">
        <el-table-column label="文件" min-width="300">
          <template #default="{ row }">
            <div class="file-cell" :title="row.rel_path || row.name">
              <el-icon class="file-icon"><Document /></el-icon>
              <span class="file-dir">{{ dirOf(row.rel_path, row.name) }}</span>
              <span class="file-name">{{ row.name }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="来源节点" width="150">
          <template #default="{ row }">
            <span class="muted">{{ agentName(row.agent_id) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="大小" width="100" align="right">
          <template #default="{ row }">
            <span class="mono">{{ formatBytes(row.size) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="130">
          <template #default="{ row }">
            <div class="row-actions">
              <a class="link-btn" @click="reveal(row)">定位</a>
              <a :href="FilesApi.downloadUrl(row.id)" class="link-btn">下载</a>
            </div>
          </template>
        </el-table-column>
      </el-table>
      <p v-if="resultTotal > results.length" class="muted" style="padding: 8px 20px 14px">
        共 {{ resultTotal }} 条，只显示前 {{ results.length }} 条。加个更具体的关键词试试。
      </p>
    </div>

    <!-- 一台节点都没有时，不要把"空目录"和"没有节点"混在一起说 -->
    <div v-else-if="!agents.length" class="empty-folder files-empty">
      <el-icon><FolderOpened /></el-icon>
      <p>还没有接入任何节点</p>
      <span>在「节点」页等待 B 端 agent 上线并批准接入，这里就会出现它的文件。</span>
    </div>

    <div v-else class="files-body">
      <aside class="files-tree">
        <el-tree
          v-if="agents.length"
          :key="treeKey"
          ref="treeRef"
          lazy
          :load="loadNode"
          node-key="key"
          :props="{ label: 'label', isLeaf: 'leaf' }"
          highlight-current
          @node-click="onNodeClick"
        >
          <template #default="{ data }">
            <span class="tree-node">
              <el-icon class="node-icon dir"><Folder /></el-icon>
              <span class="node-name">{{ data.label }}</span>
              <span class="node-count">{{ data.count }}</span>
            </span>
          </template>
        </el-tree>
        <div v-else class="muted" style="padding: 24px 14px; text-align: center">
          还没有接入任何节点
        </div>
      </aside>

      <section class="files-main" v-loading="loading">
        <div class="files-bar">
          <nav class="crumbs">
            <a @click="openFolder(current.agent, '')">{{ agentName(current.agent) }}</a>
            <template v-for="(segment, index) in segments" :key="segment">
              <span class="crumb-sep">/</span>
              <a
                :class="{ leaf: index === segments.length - 1 }"
                @click="openFolder(current.agent, pathAt(index))"
              >{{ segment }}</a>
            </template>
          </nav>
          <span class="files-total">
            {{ listing.total_files }} 个文件 · {{ formatBytes(listing.total_size) }}
          </span>
        </div>

        <div v-if="listing.dirs.length" class="dir-rows">
          <div
            v-for="dir in listing.dirs"
            :key="dir.path"
            class="dir-row"
            @click="openFolder(current.agent, dir.path)"
          >
            <el-icon class="row-icon"><FolderFilled /></el-icon>
            <span class="row-name">{{ dir.name }}</span>
            <span class="row-meta">{{ dir.files }} 个文件 · {{ formatBytes(dir.size) }}</span>
            <span class="row-actions">
              <a class="link-btn" @click.stop="downloadDir(dir.path)">打包下载</a>
              <a class="danger-link" @click.stop="removeDir(dir)">删除</a>
            </span>
          </div>
        </div>

        <div v-if="listing.files.length" class="file-rows">
          <div v-for="file in listing.files" :key="file.id" class="file-row">
            <span class="type-tile">{{ extOf(file.name) }}</span>
            <span class="row-name" :title="file.path">{{ file.name }}</span>
            <span class="row-size">{{ formatBytes(file.size) }}</span>
            <span class="row-time">{{ formatTime(file.created_at) }}</span>
            <span class="row-actions">
              <a :href="FilesApi.downloadUrl(file.id)" class="link-btn">下载</a>
              <a class="danger-link" @click="removeFile(file)">删除</a>
            </span>
          </div>
        </div>

        <p v-if="listing.files.length >= (listing.limit || 500)" class="muted" style="padding: 10px 20px">
          文件太多，只显示前 {{ listing.limit }} 个。用搜索定位更具体。
        </p>

        <div v-if="!loading && !listing.dirs.length && !listing.files.length" class="empty-folder">
          <el-icon><FolderOpened /></el-icon>
          <p>{{ emptyText }}</p>
        </div>
      </section>
    </div>

    <!-- 撤销条：8 秒后请求才真正发出 -->
    <transition name="el-fade-in">
      <div v-if="pending" class="undo-bar">
        <span>{{ pending.label }}</span>
        <a @click="undoDelete">撤销</a>
      </div>
    </transition>
  </div>
</template>
