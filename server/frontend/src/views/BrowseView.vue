<script setup>
import { computed, onDeactivated, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { AgentsApi, TasksApi, errorMessage } from '../api/client'
import { useDeviceStore } from '../stores/device'
import { formatBytes } from '../utils/format'

// keep-alive 按 name 点名缓存（见 AppLayout），切到别的页面再回来，
// 展开的目录和滚动位置都还在。
defineOptions({ name: 'BrowseView' })

const router = useRouter()
// 节点由顶栏那个全局下拉框决定，这里不再自己选，也不再记 localStorage。
const device = useDeviceStore()

const treeKey = ref(0)
// 根目录是否已经加载过。用来区分"树还是空的（等它上线要重建）"和
// "树里已经有用户展开的目录（绝不能重建）"。
const rootLoaded = ref(false)
const treeRef = ref(null)
const treeLoading = ref(false)

const checkedCount = ref(0)
const submitting = ref(false)
const creating = ref(false)
// 预览结果：确认框的数据源。null 表示确认框关闭。
const plan = ref(null)

const agentId = computed(() => device.id)
const currentAgent = computed(() => device.current)
const online = computed(() => device.online)

/**
 * 页面所处的状态。以前只有 `!online` 一个布尔，于是"还没加载出节点列表"
 * 被当成"节点离线"，进页面会闪一下警告。
 * loading = 节点列表还没回来；empty = 一台节点都没有；offline = 选中那台掉线。
 */
const viewState = computed(() => {
  if (!device.loaded) return 'loading'
  if (!device.agents.length || !agentId.value) return 'empty'
  return online.value ? 'online' : 'offline'
})

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

function refreshTree() {
  treeKey.value += 1
  rootLoaded.value = false
  clearSelection()
}

function clearSelection() {
  treeRef.value?.setCheckedKeys([])
  checkedCount.value = 0
}

/** 某个路径的祖先目录里，有没有被勾中的。 */
function hasCheckedAncestor(rel, dirs) {
  let index = rel.lastIndexOf('/')
  while (index > 0) {
    if (dirs.has(rel.slice(0, index))) return true
    index = rel.lastIndexOf('/', index - 1)
  }
  return false
}

/**
 * 当前勾选的路径（文件 + 目录混合）。
 *
 * 勾中的目录直接作为一个路径提交——**不展开成它的子项**：懒加载的树根本
 * 不知道未展开的目录里有什么，只有 B 遍历过才算数。所以父目录被勾中时，
 * 它下面那些"顺带被勾"的项必须丢掉，否则同一批文件会建两次任务。
 */
function pickedPaths() {
  const nodes = treeRef.value?.getCheckedNodes() || []
  if (!nodes.length) return []
  const dirs = new Set(nodes.filter((node) => node.type === 'dir').map((node) => node.rel))
  return nodes
    .filter((node) => node.rel && node.type !== 'more')
    .filter((node) => !hasCheckedAncestor(node.rel, dirs))
    .map((node) => node.rel)
}

function onCheck() {
  checkedCount.value = pickedPaths().length
}

function onNodeClick(data, node) {
  if (data.type !== 'file' || !treeRef.value) return
  treeRef.value.setChecked(data.rel, !node.checked)
  onCheck()
}

/** 提交前先问 A：这些路径到底会变成多少个文件、多大。 */
async function submit() {
  const paths = pickedPaths()
  if (!paths.length || submitting.value) return
  submitting.value = true
  try {
    const { data } = await TasksApi.preview({ agent_id: agentId.value, paths })
    plan.value = data
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    submitting.value = false
  }
}

async function confirmSubmit() {
  if (!plan.value?.files?.length || creating.value) return
  creating.value = true
  try {
    const { data } = await TasksApi.batch({
      agent_id: agentId.value,
      files: plan.value.files
    })
    ElMessage.success(`已提交 ${data.accepted} 个取件任务`)
    plan.value = null
    clearSelection()
    router.push({ name: 'tasks' })
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    creating.value = false
  }
}

// 顶栏换了节点：整棵树重来（旧目录属于另一台机器，留着只会误导）。
watch(agentId, () => refreshTree())

// 记住的节点可能是"离线但还在库里"的那台：等它上线时目录要自己出来。
// 但只在树还是空的时候重建——树里已经有用户展开的目录时，重建等于把他的
// 操作清空，那比"目录不出来"更烦人。
watch(online, (isOnline) => {
  if (isOnline && !rootLoaded.value) refreshTree()
})

onMounted(() => {
  // 从节点页点「浏览文件」进来时，入口已经把当前节点切好了
  refreshTree()
})

// 确认框是 teleport 到 body 的，页面被缓存时它不会跟着卸载
onDeactivated(() => {
  plan.value = null
})
</script>

<template>
  <div>
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

    <div class="panel tree-panel">
      <!-- 根目录与操作挤在同一行，树直接从下面开始，省掉一层标题栏 -->
      <div class="panel-head browse-bar">
        <span v-if="currentAgent?.roots?.length" class="root-path">
          <el-icon><FolderOpened /></el-icon>
          <span class="mono">{{ currentAgent.roots[0] }}</span>
        </span>
        <span v-else class="muted">未上报共享目录</span>

        <div class="bar-actions">
          <el-button text :disabled="!online" @click="refreshTree">
            <el-icon><Refresh /></el-icon>
            <span style="margin-left: 4px">刷新目录</span>
          </el-button>
          <el-button
            type="primary"
            :loading="submitting"
            :disabled="!online || !checkedCount"
            @click="submit"
          >
            提交取件
          </el-button>
        </div>
      </div>

      <div class="tree-scroll" v-loading="viewState === 'loading' || treeLoading">
        <el-tree
          v-if="viewState === 'online' || viewState === 'offline'"
          ref="treeRef"
          :key="`${agentId}-${treeKey}`"
          :load="loadNode"
          lazy
          show-checkbox
          :indent="0"
          :props="{ label: 'name', isLeaf: 'leaf', disabled: 'disabled' }"
          node-key="rel"
          @check="onCheck"
          @node-click="onNodeClick"
        >
          <template #default="{ data }">
            <span class="tree-node">
              <el-icon class="node-icon" :class="data.type">
                <Folder v-if="data.type === 'dir'" />
                <Document v-else-if="data.type === 'file'" />
                <Link v-else-if="data.type === 'symlink'" />
                <MoreFilled v-else />
              </el-icon>
              <span class="node-name">{{ data.name }}</span>
              <!-- 大小列始终占位（目录留空），否则目录行会被挤到右边，就对不齐了 -->
              <span class="node-size">
                {{ data.type === 'file' ? formatBytes(data.size) : '' }}
              </span>
            </span>
          </template>
        </el-tree>
        <div v-if="viewState === 'empty'" class="tree-empty">
          <el-icon><FolderOpened /></el-icon>
          <p>还没有接入任何节点</p>
          <span>在「节点」页等待 B 端 agent 上线并批准接入后，这里会显示它的目录。</span>
        </div>
      </div>
    </div>

    <el-dialog
      :model-value="!!plan"
      title="确认取件"
      width="520px"
      @update:model-value="plan = null"
    >
      <template v-if="plan">
        <p style="margin-top: 0">
          将取 <strong>{{ plan.file_count }}</strong> 个文件，共
          <strong>{{ formatBytes(plan.total_bytes) }}</strong>。
        </p>

        <el-alert
          v-if="plan.truncated"
          type="warning"
          :closable="false"
          show-icon
          title="目录太大，只展开了一部分"
          description="上面这个数量不是全部。请勾选更具体的子目录，分批提交。"
          style="margin-bottom: 12px"
        />

        <div v-if="plan.skipped_count" class="muted" style="font-size: 13px">
          <div style="margin-bottom: 4px">
            跳过 {{ plan.skipped_count }} 项（不参与取件）：
          </div>
          <div
            v-for="item in plan.skipped.slice(0, 5)"
            :key="`${item.rel}-${item.reason}`"
            class="mono"
            style="font-size: 12px; line-height: 1.7; word-break: break-all"
          >
            {{ item.rel || '—' }} · {{ item.reason }}
          </div>
          <div v-if="plan.skipped_count > 5" style="font-size: 12px">
            …另有 {{ plan.skipped_count - 5 }} 项
          </div>
        </div>

        <p v-if="!plan.file_count" class="muted" style="margin-bottom: 0">
          没有可取的文件，换一个目录试试。
        </p>
      </template>

      <template #footer>
        <el-button :disabled="creating" @click="plan = null">取消</el-button>
        <el-button
          type="primary"
          :loading="creating"
          :disabled="!plan?.file_count"
          @click="confirmSubmit"
        >
          开始取件
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>
