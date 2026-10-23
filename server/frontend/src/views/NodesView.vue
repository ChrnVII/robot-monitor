<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { AgentsApi, errorMessage } from '../api/client'
import { relativeTime } from '../utils/format'
import { useLiveEvents } from '../composables/useLiveEvents'

const router = useRouter()
const live = useLiveEvents()

const agents = ref([])
const loading = ref(false)
let off = null

async function load() {
  loading.value = true
  try {
    const { data } = await AgentsApi.list()
    agents.value = data.items || []
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    loading.value = false
  }
}

function browse(agent) {
  router.push({ name: 'browse', query: { agent: agent.id } })
}

onMounted(() => {
  load()
  // 在线状态是 A 推过来的，不需要轮询
  off = live.on((event) => {
    if (event.type === 'agent_online' || event.type === 'agent_offline') load()
  })
})

onUnmounted(() => off?.())
</script>

<template>
  <div v-loading="loading">
    <div v-if="!agents.length" class="panel">
      <div class="panel-body" style="text-align: center; padding: 48px 24px">
        <el-empty description="还没有登记任何节点">
          <p class="muted" style="max-width: 560px; margin: 0 auto 18px">
            在 A 上为每台 B 签发一个设备 token，然后在 B 上启动 agent。
            节点上线后会自动出现在这里，不需要在这里做任何配置。
          </p>
          <pre class="mono" style="text-align: left; background: #f7f9fc; padding: 14px 16px; border-radius: 10px; border: 1px solid var(--fb-border); display: inline-block">
# A 端
filebridge-server issue-token --name b-office

# B 端
filebridge-agent --server https://你的域名 --token fba_... --root /srv/share</pre>
        </el-empty>
      </div>
    </div>

    <div v-else class="panel">
      <div class="panel-head">
        <h2>节点（{{ agents.length }}）</h2>
        <el-button text @click="load">
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
</template>
