<script setup>
import { onActivated, onDeactivated, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { AgentsApi, errorMessage } from '../api/client'
import { formatTime, relativeTime } from '../utils/format'
import { useLiveEvents } from '../composables/useLiveEvents'

// 被 keep-alive 缓存（见 AppLayout），和其他三个页面保持一致
defineOptions({ name: 'NodesView' })

const router = useRouter()
const live = useLiveEvents()

const agents = ref([])
const pending = ref([])
const loading = ref(false)
const pendingLoading = ref(false)
// 正在处理的那条申请：只禁用那一行，别让整张表闪
const resolving = ref('')

const deleteTarget = ref(null)
const deleteFiles = ref(false)
const deleting = ref(false)

let off = null

async function load({ silent = false } = {}) {
  if (!silent) loading.value = true
  try {
    const { data } = await AgentsApi.list()
    agents.value = data.items || []
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    loading.value = false
  }
}

async function loadPending({ silent = false } = {}) {
  if (!silent) pendingLoading.value = true
  try {
    const { data } = await AgentsApi.pending()
    pending.value = data.items || []
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    pendingLoading.value = false
  }
}

function browse(agent) {
  router.push({ name: 'browse', query: { agent: agent.id } })
}

function askDelete(agent) {
  deleteFiles.value = false
  deleteTarget.value = agent
}

async function confirmDelete() {
  const agent = deleteTarget.value
  if (!agent) return
  deleting.value = true
  try {
    const { data } = await AgentsApi.remove(agent.id, { deleteFiles: deleteFiles.value })
    ElMessage.success(deleteSummary(data))
    deleteTarget.value = null
    await Promise.all([load(), loadPending()])
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    deleting.value = false
  }
}

/** 把后端返回的计数翻译成人话——用户得知道刚才到底发生了什么。 */
function deleteSummary(data) {
  const parts = [`已删除节点「${data.name}」`]
  if (data.tasks_canceled) parts.push(`取消 ${data.tasks_canceled} 个任务`)
  if (data.files_trashed) parts.push(`${data.files_trashed} 个文件移入回收站`)
  else if (data.files_kept) parts.push(`${data.files_kept} 个文件仍保留在文件库`)
  return parts.join('，')
}

async function approve(item) {
  resolving.value = item.id
  try {
    await AgentsApi.approve(item.id)
    ElMessage.success(`已批准「${item.hostname || item.id.slice(0, 10)}」，对方会在几秒内自动连上`)
    await Promise.all([loadPending(), load()])
  } catch (error) {
    ElMessage.error(errorMessage(error))
    loadPending()
  } finally {
    resolving.value = ''
  }
}

async function reject(item) {
  const name = item.hostname || item.id.slice(0, 10)
  try {
    await ElMessageBox.confirm(
      `拒绝「${name}」的接入申请？对方会收到 403，且不会产生任何凭据。`,
      '拒绝接入',
      { confirmButtonText: '拒绝', cancelButtonText: '取消', type: 'warning' }
    )
  } catch {
    return
  }
  resolving.value = item.id
  try {
    await AgentsApi.reject(item.id)
    ElMessage.success('已拒绝')
    await loadPending()
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    resolving.value = ''
  }
}

// 在线状态与待批准列表都由 A 推过来。进页面开、离开关。
function startWatching() {
  off?.()
  off = live.on((event) => {
    if (event.type === 'agent_online' || event.type === 'agent_offline') load()
    if (event.type === 'agent_removed') Promise.all([load(), loadPending()])
    if (event.type === 'enroll_pending' || event.type === 'enroll_resolved') loadPending()
  })
}

function stopWatching() {
  off?.()
  off = null
}

let firstActivate = true

onMounted(() => {
  load()
  loadPending()
  startWatching()
})

onActivated(() => {
  if (firstActivate) {
    firstActivate = false
    return
  }
  // 回来静默对账：离开期间可能有节点上下线或新的接入申请
  load({ silent: true })
  loadPending({ silent: true })
  startWatching()
})

onDeactivated(() => {
  stopWatching()
  // 对话框是 teleport 到 body 的，页面被缓存时它不会跟着卸载：
  // 不主动关掉，切走了还会挂着一层遮罩。
  deleteTarget.value = null
})
onUnmounted(stopWatching)
</script>

<template>
  <div>
    <div v-if="pending.length" class="panel" style="margin-bottom: 18px">
      <div class="panel-head">
        <h2>
          待批准接入（{{ pending.length }}）
          <el-tag size="small" type="warning" style="margin-left: 6px">需要确认</el-tag>
        </h2>
        <el-button text :loading="pendingLoading" @click="loadPending()">
          <el-icon><Refresh /></el-icon>
          <span style="margin-left: 4px">刷新</span>
        </el-button>
      </div>

      <el-table :data="pending" v-loading="pendingLoading" style="width: 100%">
        <el-table-column label="主机名" min-width="160">
          <template #default="{ row }">
            <span>{{ row.hostname || '未上报' }}</span>
            <div class="cell-path mono">{{ row.id.slice(0, 10) }}</div>
          </template>
        </el-table-column>
        <el-table-column label="共享根目录" min-width="170">
          <template #default="{ row }">
            <span class="mono">{{ row.root_path || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="来源 IP" width="130">
          <template #default="{ row }">
            <span class="mono">{{ row.remote_ip || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="申请时间" width="170">
          <template #default="{ row }">
            <span class="mono">{{ formatTime(row.created_at) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="160" align="right">
          <template #default="{ row }">
            <el-button
              type="primary"
              size="small"
              :loading="resolving === row.id"
              @click="approve(row)"
            >
              批准
            </el-button>
            <el-button size="small" :disabled="resolving === row.id" @click="reject(row)">
              拒绝
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <div v-loading="loading">
      <div v-if="!agents.length" class="panel">
        <div class="panel-body" style="text-align: center; padding: 48px 24px">
          <el-empty description="还没有登记任何节点">
            <p class="muted" style="max-width: 560px; margin: 0 auto 18px">
              在 B 上启动 agent，它会自动向 A 注册；如果准入策略要求人工确认，
              它会先出现在上面的「待批准接入」里。这里不需要做任何配置。
            </p>
            <pre class="mono" style="text-align: left; background: #f7f9fc; padding: 14px 16px; border-radius: 10px; border: 1px solid var(--fb-border); display: inline-block">
# B 端
filebridge-agent --server https://你的域名 --root /srv/share

# 也可以手工签发（跳过注册审批）
filebridge-server issue-token --name b-office</pre>
          </el-empty>
        </div>
      </div>

      <div v-else class="panel">
        <div class="panel-head">
          <h2>节点（{{ agents.length }}）</h2>
          <el-button text @click="load()">
            <el-icon><Refresh /></el-icon>
            <span style="margin-left: 4px">刷新</span>
          </el-button>
        </div>

        <div v-for="agent in agents" :key="agent.id" class="node-card">
          <div class="node-head">
            <div class="node-title">
              <span class="status-dot" :class="{ online: agent.online }" />
              {{ agent.name }}
              <el-tag v-if="agent.revoked" size="small" type="danger">已吊销</el-tag>
              <el-tag v-else-if="agent.online" size="small" type="success">在线</el-tag>
              <el-tag v-else size="small" type="info">离线</el-tag>
            </div>
            <div class="node-actions">
              <el-button :disabled="!agent.online" type="primary" @click="browse(agent)">
                浏览文件
              </el-button>
              <el-button type="danger" plain @click="askDelete(agent)">删除</el-button>
            </div>
          </div>

          <div class="node-meta">
            <div class="meta-item">
              <span class="meta-label">主机名</span>
              <span class="meta-value">{{ agent.hostname || '—' }}</span>
            </div>
            <div class="meta-item">
              <span class="meta-label">系统 / 版本</span>
              <span class="meta-value">{{ agent.os || '—' }} · {{ agent.agent_version || '—' }}</span>
            </div>
            <div class="meta-item">
              <span class="meta-label">最后心跳</span>
              <span class="meta-value">{{ relativeTime(agent.last_seen) }}</span>
            </div>
            <div class="meta-item">
              <span class="meta-label">已取文件</span>
              <span class="meta-value">{{ agent.files }} 个</span>
            </div>
            <div class="meta-item">
              <span class="meta-label">节点 ID</span>
              <span class="meta-value mono">{{ agent.id }}</span>
            </div>
          </div>

          <div>
            <span class="meta-label">共享根目录</span>
            <div style="margin-top: 6px; display: flex; gap: 8px; flex-wrap: wrap">
              <el-tag v-for="root in agent.roots" :key="root" type="info" effect="plain">
                <span class="mono">{{ root }}</span>
              </el-tag>
              <span v-if="!agent.roots?.length" class="muted">—</span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <el-dialog
      :model-value="!!deleteTarget"
      title="删除节点"
      width="480px"
      @update:model-value="deleteTarget = null"
    >
      <template v-if="deleteTarget">
        <p style="margin-top: 0">
          确定删除节点 <strong>{{ deleteTarget.name }}</strong>
          <span class="mono muted">（{{ deleteTarget.id }}）</span>？
        </p>
        <ul class="muted" style="padding-left: 18px; margin: 12px 0 16px; line-height: 1.9">
          <li>立即断开它的控制连接，并吊销 token</li>
          <li>未完成的任务会被取消，占用的临时文件一并清理</li>
          <li>它需要重新注册并再次批准才能接入</li>
        </ul>

        <template v-if="deleteTarget.files">
          <el-checkbox v-model="deleteFiles">
            同时把它的 {{ deleteTarget.files }} 个文件移入回收站
          </el-checkbox>
          <p class="muted" style="margin: 8px 0 0; font-size: 13px">
            不勾选则文件留在文件库（推荐）。勾选也只是进回收站，30 天内可还原。
          </p>
        </template>
        <p v-else class="muted" style="margin: 0">这个节点还没有取过文件，删除不会影响文件库。</p>
      </template>

      <template #footer>
        <el-button :disabled="deleting" @click="deleteTarget = null">取消</el-button>
        <el-button type="danger" :loading="deleting" @click="confirmDelete">删除节点</el-button>
      </template>
    </el-dialog>
  </div>
</template>
