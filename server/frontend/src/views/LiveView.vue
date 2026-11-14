<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { CameraApi, cameraStreamBlockedReason, liveSocketUrl } from '../api/camera'
import { errorMessage } from '../api/client'
import PtzPad from '../components/live/PtzPad.vue'
import { useLiveStream } from '../composables/useLiveStream'
import { useDeviceStore } from '../stores/device'

// 被 keep-alive 排除（见 AppLayout 的 include 列表）：离开页面就要停流，
// 不能像列表页那样缓存着——那会让相机一直有人"在看"。
defineOptions({ name: 'LiveView' })

const device = useDeviceStore()
const stream = useLiveStream()

const canvasRef = ref(null)
const status = ref(null)
const presets = ref([])
const speed = ref(0.5)
const holding = ref('')
const sending = ref(false)
const takeover = ref(false)
const statusLoading = ref(false)

const agentId = computed(() => device.id)
const blockedReason = computed(() => cameraStreamBlockedReason())
const online = computed(() => status.value?.agentOnline ?? device.online)
const supported = computed(() => status.value?.supported !== false)
const sessionId = computed(() => stream.metrics.sessionId || status.value?.stream?.sessionId || '')
const viewers = computed(() => status.value?.stream?.viewers || 0)

let holdTimer = null
let pollTimer = null
let takeoverTimer = null
let disposed = false

watch(agentId, () => {
  if (!disposed) restart()
})

function clearHold() {
  if (holdTimer) {
    clearInterval(holdTimer)
    holdTimer = null
  }
  holding.value = ''
}

async function refreshStatus() {
  if (!agentId.value) return
  statusLoading.value = true
  try {
    const { data } = await CameraApi.status(agentId.value, 'ptz')
    status.value = data
  } catch (error) {
    status.value = null
    if (error?.response?.status !== 404) {
      // 轮询失败不打断观看，只在首次加载时提示
      if (!stream.metrics.sessionId) ElMessage.error(errorMessage(error))
    }
  } finally {
    statusLoading.value = false
  }
}

async function loadPresets() {
  if (!agentId.value) return
  try {
    const { data } = await CameraApi.presets(agentId.value, 'ptz')
    presets.value = data.items || []
  } catch {
    presets.value = []
  }
}

function start() {
  if (!agentId.value) return
  if (blockedReason.value) return
  stream.attach(canvasRef.value)
  stream.connect(liveSocketUrl(agentId.value, 'ptz'))
}

function restart() {
  clearHold()
  presets.value = []
  stream.disconnect()
  // connect() 会自己重置 disposed，所以这里直接重新 attach + connect，
  // 不走 composable 内部的 reconnect（那是"同一条流断了重连"）。
  setTimeout(() => {
    start()
    refreshStatus()
    loadPresets()
  }, 0)
}

async function send(direct, action) {
  if (!agentId.value) return false
  sending.value = true
  try {
    await CameraApi.ptz(agentId.value, {
      cameraId: 'ptz',
      sessionId: sessionId.value || undefined,
      action,
      direct,
      speed: speed.value
    })
    if (takeover.value) {
      takeover.value = false
      if (takeoverTimer) clearTimeout(takeoverTimer)
    }
    return true
  } catch (error) {
    const code = error?.response?.data?.code
    if (code === 'control_conflict') {
      // 本地人工接管：不是故障，是优先级规则。给明确提示，不要报"网络错误"。
      takeover.value = true
      if (takeoverTimer) clearTimeout(takeoverTimer)
      takeoverTimer = setTimeout(() => {
        takeover.value = false
      }, 8000)
    } else if (code === 'no_active_session') {
      ElMessage.warning('画面还没就绪，稍等一下再操作云台')
    } else {
      ElMessage.error(errorMessage(error))
    }
    return false
  } finally {
    sending.value = false
  }
}

async function startMove(direct) {
  if (takeover.value || sending.value || !sessionId.value) return
  clearHold()
  holding.value = direct
  const ok = await send(direct, 'start')
  if (!ok) {
    clearHold()
    return
  }
  // 每秒补一条 start：agent 侧 3 秒没新指令会自动停（看护），
  // 这是"按住不放"能持续转的原因，也是断网时球机会自己停的原因。
  holdTimer = setInterval(() => {
    if (!holding.value) return
    send(holding.value, 'start')
  }, 1000)
}

async function stopMove() {
  const direct = holding.value
  clearHold()
  if (!direct) return
  await send('stop', 'stop')
}

function onKeyDown(event) {
  if (event.repeat) return
  const map = {
    ArrowUp: 'up',
    ArrowDown: 'down',
    ArrowLeft: 'left',
    ArrowRight: 'right',
    '+': 'zoom_in',
    '=': 'zoom_in',
    '-': 'zoom_out'
  }
  const direct = map[event.key]
  if (!direct) return
  const tag = (event.target?.tagName || '').toLowerCase()
  if (['input', 'textarea', 'select'].includes(tag)) return
  event.preventDefault()
  const action = event.type === 'keydown' ? startMove : stopMove
  action(direct)
}

async function gotoPreset(preset) {
  if (!agentId.value) return
  try {
    await CameraApi.gotoPreset(agentId.value, preset.presetId, {
      cameraId: 'ptz',
      sessionId: sessionId.value || undefined
    })
    ElMessage.success(`已跳转到「${preset.name}」`)
  } catch (error) {
    ElMessage.error(errorMessage(error))
  }
}

async function stopWatching() {
  const wasHolding = Boolean(holding.value)
  clearHold()
  // 正在转的时候离开页面，先补一条 stop，不要等 agent 的 3 秒看护。
  if (wasHolding) await send('stop', 'stop')
  stream.disconnect()
  if (agentId.value) {
    try {
      await CameraApi.stop(agentId.value, 'ptz')
    } catch {
      // 停流失败没有可补救的动作：上游会在空闲超时后自己关
    }
  }
  await refreshStatus()
}

onMounted(async () => {
  if (!device.loaded) await device.load()
  window.addEventListener('keydown', onKeyDown)
  window.addEventListener('keyup', onKeyDown)
  start()
  await refreshStatus()
  await loadPresets()
  pollTimer = setInterval(refreshStatus, 5000)
})

onBeforeUnmount(() => {
  disposed = true
  window.removeEventListener('keydown', onKeyDown)
  window.removeEventListener('keyup', onKeyDown)
  clearHold()
  if (pollTimer) clearInterval(pollTimer)
  if (takeoverTimer) clearTimeout(takeoverTimer)
  stopWatching()
})
</script>

<template>
  <div class="live">
    <el-alert
      v-if="!agentId"
      type="info"
      :closable="false"
      show-icon
      title="还没有选择节点"
      description="在顶栏「节点」下拉框里选一台机器人，或者在「节点」页点进来。"
      style="margin-bottom: 16px"
    />
    <el-alert
      v-else-if="!online"
      type="warning"
      :closable="false"
      show-icon
      title="节点不在线"
      description="远程画面需要 cloud-agent 保持与云端连接。请确认机器人上的 cloud-agent 在运行。"
      style="margin-bottom: 16px"
    />
    <el-alert
      v-else-if="blockedReason"
      type="warning"
      :closable="false"
      show-icon
      title="当前浏览器无法播放"
      :description="blockedReason"
      style="margin-bottom: 16px"
    />
    <el-alert
      v-else-if="status && !supported"
      type="info"
      :closable="false"
      show-icon
      title="这台机器人还不支持远程画面"
      description="cloud-agent 版本较旧，升级后会自动出现在这里。"
      style="margin-bottom: 16px"
    />
    <el-alert
      v-if="takeover"
      type="warning"
      :closable="false"
      show-icon
      title="本地已接管"
      description="现场有人在机器人本地控制云台，远程操作已暂停。本地松开后几秒会自动恢复。"
      style="margin-bottom: 16px"
    />

    <div class="live-grid">
      <section class="panel stage-panel">
        <div class="panel-head">
          <h2>实时画面</h2>
          <div class="stage-meta">
            <span class="chip" :class="stream.phase.value">
              {{
                stream.phase.value === 'live'
                  ? '直播中'
                  : stream.phase.value === 'connecting'
                    ? '连接中'
                    : stream.phase.value === 'error'
                      ? '不可用'
                      : '未开始'
              }}
            </span>
            <span class="chip">{{ viewers }} 人观看</span>
            <span v-if="stream.codec.value" class="chip">{{ stream.codec.value }}</span>
          </div>
          <div class="stage-actions">
            <el-button size="small" :loading="statusLoading" @click="refreshStatus">刷新</el-button>
            <el-button size="small" @click="restart">重连</el-button>
            <el-button size="small" type="danger" plain @click="stopWatching">停止观看</el-button>
          </div>
        </div>

        <div class="stage">
          <canvas ref="canvasRef" class="stage-canvas" />
          <div v-if="stream.phase.value === 'connecting'" class="stage-overlay">
            <el-icon class="spin"><Loading /></el-icon>
            <span>正在建立画面…</span>
          </div>
          <div v-else-if="stream.phase.value === 'error'" class="stage-overlay error">
            <el-icon><WarningFilled /></el-icon>
            <span>{{ stream.error.value }}</span>
          </div>
          <div v-else-if="stream.phase.value === 'idle'" class="stage-overlay">
            <el-icon><VideoCamera /></el-icon>
            <span>尚未开始</span>
          </div>
        </div>

        <div class="stage-foot">
          <span>{{ stream.resolution.width }} × {{ stream.resolution.height }}</span>
          <span>{{ stream.metrics.fps }} fps</span>
          <span>{{ stream.metrics.kbps }} kbps</span>
          <span>丢帧 {{ stream.metrics.dropped }}</span>
          <span v-if="sessionId" class="mono">{{ sessionId }}</span>
        </div>
      </section>

      <aside class="side">
        <section class="panel">
          <div class="panel-head">
            <h2>云台</h2>
          </div>
          <div class="panel-body ptz-body">
            <PtzPad
              :disabled="!sessionId || takeover || stream.phase.value !== 'live'"
              :holding="holding"
              @start="startMove"
              @stop="stopMove"
            />
            <div class="speed">
              <span>速度</span>
              <el-slider v-model="speed" :min="0.15" :max="1" :step="0.05" size="small" />
              <span class="mono">{{ speed.toFixed(2) }}</span>
            </div>
            <p class="hint muted">按住方向键持续转动，松开即停；断网 3 秒内也会自动停。</p>
          </div>
        </section>

        <section class="panel">
          <div class="panel-head">
            <h2>预置点</h2>
            <el-button size="small" text @click="loadPresets">刷新</el-button>
          </div>
          <div class="panel-body">
            <div v-if="!presets.length" class="muted empty">暂无预置点</div>
            <div v-else class="presets">
              <button
                v-for="item in presets"
                :key="item.presetId"
                class="preset"
                :disabled="takeover || !sessionId"
                @click="gotoPreset(item)"
              >
                <span class="preset-id">{{ item.presetId }}</span>
                <span>{{ item.name }}</span>
              </button>
            </div>
          </div>
        </section>
      </aside>
    </div>
  </div>
</template>

<style scoped>
.live-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 320px;
  gap: 16px;
  align-items: start;
}

@media (max-width: 1080px) {
  .live-grid {
    grid-template-columns: minmax(0, 1fr);
  }
}

.stage-panel {
  overflow: hidden;
}

.stage-meta {
  display: flex;
  gap: 8px;
  margin-left: 12px;
}

.chip {
  display: inline-flex;
  align-items: center;
  height: 24px;
  padding: 0 10px;
  border-radius: 999px;
  border: 1px solid var(--rm-border);
  background: var(--rm-panel-soft);
  color: var(--rm-text-muted);
  font-size: 12px;
}

.chip.live {
  border-color: rgba(18, 161, 80, 0.35);
  background: var(--rm-success-soft);
  color: var(--rm-success);
}

.chip.error {
  border-color: rgba(224, 71, 63, 0.35);
  color: var(--rm-danger);
}

.stage-actions {
  margin-left: auto;
  display: flex;
  gap: 8px;
}

.stage {
  position: relative;
  aspect-ratio: 16 / 9;
  background: #10141c;
}

.stage-canvas {
  width: 100%;
  height: 100%;
  display: block;
}

.stage-overlay {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
  align-items: center;
  justify-content: center;
  color: #cbd5e1;
  font-size: 13px;
  background: rgba(16, 20, 28, 0.72);
  text-align: center;
  padding: 24px;
}

.stage-overlay.error {
  color: #fca5a5;
}

.stage-overlay .el-icon {
  font-size: 22px;
}

.stage-foot {
  display: flex;
  gap: 16px;
  padding: 10px 16px;
  border-top: 1px solid var(--rm-border);
  color: var(--rm-text-faint);
  font-size: 12px;
}

.mono {
  font-family: var(--rm-mono);
}

.side {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.ptz-body {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 16px;
}

.speed {
  width: 100%;
  display: grid;
  grid-template-columns: 32px 1fr 40px;
  align-items: center;
  gap: 10px;
  color: var(--rm-text-muted);
  font-size: 12px;
  text-align: right;
}

.hint {
  font-size: 12px;
  line-height: 1.6;
  text-align: center;
}

.presets {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.preset {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  border: 1px solid var(--rm-border);
  background: var(--rm-panel);
  border-radius: 10px;
  cursor: pointer;
  font-size: 13px;
  color: var(--rm-text);
  text-align: left;
}

.preset:hover:not(:disabled) {
  background: var(--rm-hover);
  border-color: var(--rm-accent-line);
}

.preset:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.preset-id {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border-radius: 6px;
  background: var(--rm-accent-soft);
  color: var(--rm-accent);
  font-size: 11px;
  font-family: var(--rm-mono);
}

.empty {
  padding: 12px 0;
  font-size: 13px;
}

.spin {
  animation: spin 1s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
