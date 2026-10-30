<script setup>
import { computed, onActivated, onDeactivated, onMounted, onUnmounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { AgentsApi, FilesApi, errorMessage } from '../api/client'
import { dirOf, formatBytes, formatTime } from '../utils/format'
import { useLiveEvents } from '../composables/useLiveEvents'

// 被 keep-alive 缓存（见 AppLayout）：切走再回来，Tab、搜索词、页码都还在
defineOptions({ name: 'FilesView' })

const { on } = useLiveEvents()

const tab = ref('library')
const query = ref('')
const page = ref(1)
const pageSize = ref(20)
const total = ref(0)
const files = ref([])
const loading = ref(false)
const agentNames = ref({})

let off = null
let searchTimer = null

const deleted = computed(() => tab.value === 'trash')

function agentName(id) {
  // 节点被删掉之后，文件仍然留在库里（删节点≠删数据），这里给个能读懂的说法，
  // 否则来源列会突然变成一串十六进制
  if (!id) return '—'
  return agentNames.value[id] || `已移除的节点（${id.slice(0, 6)}）`
}

async function loadAgents() {
  try {
    const { data } = await AgentsApi.list()
    agentNames.value = Object.fromEntries((data.items || []).map((item) => [item.id, item.name]))
  } catch {
    // 名字只是展示用，取不到就显示 id
  }
}

async function load({ silent = false } = {}) {
  if (!silent) loading.value = true
  try {
    const { data } = await FilesApi.list({
      q: query.value.trim() || undefined,
      deleted: deleted.value,
      limit: pageSize.value,
      offset: (page.value - 1) * pageSize.value
    })
    files.value = data.items || []
    total.value = data.total || 0
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    loading.value = false
  }
}

function search() {
  clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    page.value = 1
    load()
  }, 250)
}

async function remove(row) {
  try {
    await ElMessageBox.confirm(`把「${row.name}」移入回收站？`, '删除文件', {
      confirmButtonText: '移入回收站',
      cancelButtonText: '取消',
      type: 'warning'
    })
  } catch {
    return
  }
  try {
    await FilesApi.remove(row.id)
    ElMessage.success('已移入回收站')
    load()
  } catch (error) {
    ElMessage.error(errorMessage(error))
  }
}

async function restore(row) {
  try {
    await FilesApi.restore(row.id)
    ElMessage.success('已还原')
    load()
  } catch (error) {
    ElMessage.error(errorMessage(error))
  }
}

async function purge(row) {
  try {
    await ElMessageBox.confirm(
      `彻底删除「${row.name}」？磁盘上的文件会被删除，无法恢复。`,
      '彻底删除',
      { confirmButtonText: '彻底删除', cancelButtonText: '取消', type: 'error' }
    )
  } catch {
    return
  }
  try {
    await FilesApi.purge(row.id)
    ElMessage.success('已彻底删除')
    load()
  } catch (error) {
    ElMessage.error(errorMessage(error))
  }
}

watch(tab, () => {
  page.value = 1
  load()
})

// 文件事件的订阅：进页面开、离开关（缓存组件不关会在后台白白请求）
function startWatching() {
  off?.()
  off = on((event) => {
    if (['file_added', 'file_removed', 'file_restored'].includes(event.type)) load()
  })
}

function stopWatching() {
  off?.()
  off = null
}

let firstActivate = true

onMounted(() => {
  loadAgents()
  load()
  startWatching()
})

onActivated(() => {
  if (firstActivate) {
    firstActivate = false
    return
  }
  // 回来静默对账：离开期间可能又有文件入库/被删
  loadAgents()
  load({ silent: true })
  startWatching()
})

onDeactivated(() => {
  stopWatching()
  clearTimeout(searchTimer)
})

onUnmounted(() => {
  stopWatching()
  clearTimeout(searchTimer)
})
</script>

<template>
  <div class="panel">
    <div class="panel-head">
      <el-tabs v-model="tab" style="margin-bottom: -14px">
        <el-tab-pane label="文件库" name="library" />
        <el-tab-pane label="回收站" name="trash" />
      </el-tabs>

      <el-input
        v-model="query"
        placeholder="按文件名搜索"
        clearable
        style="max-width: 260px"
        @input="search"
        @clear="search"
      >
        <template #prefix>
          <el-icon><Search /></el-icon>
        </template>
      </el-input>
    </div>

    <el-table :data="files" v-loading="loading" style="width: 100%" :empty-text="deleted ? '回收站是空的' : '还没有文件，去「取件」取一个'">
      <el-table-column label="文件名" min-width="240">
        <template #default="{ row }">
          <!-- 与任务页一致：一行 "路径/文件名"，路径压暗、文件名加深。
               老数据 rel_path 可能为空，那时只显示文件名。 -->
          <div class="file-cell" :title="row.rel_path || row.name">
            <el-icon class="file-icon"><Document /></el-icon>
            <span class="file-dir">{{ dirOf(row.rel_path, row.name) }}</span>
            <span class="file-name">{{ row.name }}</span>
          </div>
        </template>
      </el-table-column>

      <el-table-column label="大小" width="110" align="right">
        <template #default="{ row }">
          <span class="mono">{{ formatBytes(row.size) }}</span>
        </template>
      </el-table-column>

      <el-table-column label="来源节点" width="140">
        <template #default="{ row }">
          <span class="muted">{{ agentName(row.agent_id) }}</span>
        </template>
      </el-table-column>

      <el-table-column :label="deleted ? '删除时间' : '入库时间'" width="170">
        <template #default="{ row }">
          <span class="muted">{{ formatTime(deleted ? row.deleted_at : row.created_at) }}</span>
        </template>
      </el-table-column>

      <el-table-column label="操作" width="150" fixed="right">
        <template #default="{ row }">
          <div class="row-actions">
            <template v-if="!deleted">
              <a :href="FilesApi.downloadUrl(row.id)" class="link-btn">下载</a>
              <span class="danger-link" @click="remove(row)">删除</span>
            </template>
            <template v-else>
              <span class="link-btn" @click="restore(row)">还原</span>
              <span class="danger-link" @click="purge(row)">彻底删除</span>
            </template>
          </div>
        </template>
      </el-table-column>
    </el-table>

    <div style="display: flex; justify-content: flex-end; padding: 14px 20px">
      <el-pagination
        v-model:current-page="page"
        v-model:page-size="pageSize"
        :total="total"
        :page-sizes="[20, 50, 100]"
        layout="total, sizes, prev, pager, next"
        background
        @size-change="load()"
        @current-change="load()"
      />
    </div>
  </div>
</template>
