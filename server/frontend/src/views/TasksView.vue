<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { TasksApi, errorMessage } from '../api/client'
import { dirOf, formatBytes, formatTime, taskStatus } from '../utils/format'
import { useLiveEvents } from '../composables/useLiveEvents'

const { on, onReconnect } = useLiveEvents()

const tasks = ref([])
const loading = ref(false)
let timer = null
let off = null

const ACTIVE = new Set(['pending', 'uploading', 'verifying'])

async function load() {
  loading.value = true
  try {
    const { data } = await TasksApi.list({ limit: 200 })
    tasks.value = data.items || []
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    loading.value = false
  }
}

async function refreshOne(taskId) {
  try {
    const { data } = await TasksApi.get(taskId)
    const index = tasks.value.findIndex((item) => item.id === taskId)
    if (index >= 0) tasks.value[index] = data
    else tasks.value.unshift(data)
  } catch {
    // 任务可能刚被清理，忽略
  }
}

function applyEvent(event) {
  if (event.type === 'task_progress') {
    const data = event.data
    const item = tasks.value.find((task) => task.id === data.task_id)
    if (!item) return
    item.transferred_bytes = data.bytes
    item.progress = data.progress
    item.status = 'uploading'
  } else if (event.type === 'task_update') {
    refreshOne(event.data.task_id)
  }
}

async function cancel(task) {
  try {
    await ElMessageBox.confirm(`取消「${task.name}」的取件任务？已上传的分片会被清理。`, '取消任务', {
      confirmButtonText: '取消任务',
      cancelButtonText: '再等等',
      type: 'warning'
    })
  } catch {
    return
  }
  try {
    await TasksApi.cancel(task.id)
    ElMessage.success('已取消')
    refreshOne(task.id)
  } catch (error) {
    ElMessage.error(errorMessage(error))
  }
}

async function retry(task) {
  try {
    await TasksApi.create({ agent_id: task.agent_id, path: task.path })
    ElMessage.success('已重新提交')
    load()
  } catch (error) {
    ElMessage.error(errorMessage(error))
  }
}

onMounted(() => {
  load()
  // 兜底轮询：SSE 断线期间错过的终态事件靠它补上（事件流不重放）
  timer = setInterval(load, 5000)
  off = on(applyEvent)
  onReconnect(load)
})

onUnmounted(() => {
  clearInterval(timer)
  off?.()
})
</script>

<template>
  <div class="panel">
    <div class="panel-head">
      <h2>取件任务（{{ tasks.length }}）</h2>
      <el-button text @click="load">
        <el-icon><Refresh /></el-icon>
        <span style="margin-left: 4px">刷新</span>
      </el-button>
    </div>

    <el-table :data="tasks" v-loading="loading" style="width: 100%" empty-text="还没有任务">
      <el-table-column label="文件" min-width="240">
        <template #default="{ row }">
          <!-- 一行显示 "路径/文件名"：路径压暗、文件名加深加粗。
               两行版本里 filename 和 path 的尾部是同一串字符，重复了。 -->
          <div class="file-cell" :title="row.path">
            <span class="file-dir">{{ dirOf(row.path, row.name) }}</span>
            <span class="file-name">{{ row.name }}</span>
          </div>
        </template>
      </el-table-column>

      <el-table-column label="文件大小" width="110" align="right">
        <template #default="{ row }">
          <!-- 建任务前 stat 还没回来时 total_bytes 是空的，别显示成 "0 B" -->
          <span v-if="row.total_bytes" class="mono">{{ formatBytes(row.total_bytes) }}</span>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>

      <el-table-column label="创建时间" width="170">
        <template #default="{ row }">
          <span class="muted">{{ formatTime(row.created_at) }}</span>
        </template>
      </el-table-column>

      <el-table-column label="进度" min-width="190">
        <template #default="{ row }">
          <!-- 完成的任务画一根 100% 的条没有任何信息量。只留一个勾：
               右边的"状态"列已经写了已完成，这里再写一遍只是重复。
               失败的红条保留——能从停在哪儿看出断点位置。大小已在独立列里。 -->
          <div v-if="row.status === 'done'" class="done-cell">
            <el-icon><CircleCheckFilled /></el-icon>
          </div>
          <div v-else class="progress-cell">
            <el-progress
              :percentage="Math.min(100, Math.round(row.progress || 0))"
              :status="row.status === 'failed' ? 'exception' : undefined"
              :stroke-width="6"
            />
          </div>
        </template>
      </el-table-column>

      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tag :type="taskStatus(row.status).type" size="small" effect="light">
            {{ taskStatus(row.status).text }}
          </el-tag>
        </template>
      </el-table-column>

      <el-table-column label="失败原因" min-width="200">
        <template #default="{ row }">
          <span v-if="row.error" class="muted">{{ row.error }}</span>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>

      <el-table-column label="操作" width="100" fixed="right">
        <template #default="{ row }">
          <div class="row-actions">
            <el-button v-if="ACTIVE.has(row.status)" text size="small" @click="cancel(row)">
              取消
            </el-button>
            <el-button v-else-if="row.status === 'failed'" text size="small" @click="retry(row)">
              重试
            </el-button>
            <span v-else class="muted">—</span>
          </div>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>
