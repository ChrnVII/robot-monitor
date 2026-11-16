<script setup>
// 远程监控：画面 + 云台 + 预置点 + 四指示器。
//
// 页面只做三件事：把 composable 的状态显示出来、把用户操作转成请求、
// 在画面不可用时给出**具体原因**。不在这里做重连、退避、解码——
// 那些在 useMediaStream 里，因为它们要和 watchdog 一起判断。
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { CameraApi, StateApi } from '../api/camera'
import { errorMessage } from '../api/client'
import { useDeviceStore } from '../stores/device'
import { useMediaStream } from '../composables/useMediaStream'
import { usePtz } from '../composables/usePtz'
import { useLiveEvents } from '../composables/useLiveEvents'

const device = useDeviceStore()
const canvasRef = ref(null)
const snapshotUrl = ref('')
const snapshotAt = ref(null)
const snapshotLoading = ref(false)
const session = ref(null)
const presets = ref([])
const presetsLoading = ref(false)
const presetsError = ref('')
const streamError = ref('')
const stateView = ref(null)
// 自动降档：判据看三个（fps / 延迟 / 是否冻结），单看一个会被抖动骗。
const profile = ref('low')
const profileBusy = ref(false)
const PROFILE_ORDER = ['tiny', 'low', 'high']   // 低 → 高
let badSamples = 0
let goodSamples = 0

const agentId = computed(() => device.id)
const agentName = computed(() => device.current?.name || '')
const online = computed(() => Boolean(device.current?.online))
const capabilities = computed(() => device.current?.capabilities || [])
const canWatch = computed(() => capabilities.value.includes('media.ws.v1'))
const canControl = computed(() => capabilities.value.includes('camera.ptz'))

// 不传 cameraId：留空表示"这台设备那台球形相机"，具体编号由机器人侧决定。
const stream = useMediaStream({ agentId })
const ptz = usePtz({ agentId, sessionId: () => session.value?.session_id || '' })
const { on: onLiveEvent } = useLiveEvents()

const playerPhase = computed(() => stream.phase.value)
const showSnapshot = computed(
  () => snapshotUrl.value && (playerPhase.value === 'frozen' || playerPhase.value === 'stream_disabled')
)
const frozenAgo = computed(() => {
  const ms = stream.metrics.lastFrameAgoMs
  if (!ms) return null
  return Math.round(ms / 1000)
})

/* ---------------------------- 会话状态 ---------------------------- */

/**
 * 每 5s 评估一次档位。
 *
 * 降档：连续一次采样里出现"冻结 / fps < 5 / 延迟 > 3s"就降到下一档。
 * 升档：连续 4 次采样（20s）都"fps ≥ 12 且延迟 < 800ms"才升一档——
 * 升档比降档保守，否则会在临界点上反复横跳，每次跳都伴随一段关键帧等待。
 *
 * 只调云端接口，真正的抽帧发生在机器人侧（按 GOP 整段丢，不随机抽帧）。
 */
async function evaluateProfile() {
  if (!agentId.value || profileBusy.value) return
  const fps = stream.metrics.fps
  const latency = stream.metrics.latencyMs
  const frozen = playerPhase.value === 'frozen'

  const bad = frozen || (fps > 0 && fps < 5) || (latency !== null && latency > 3000)
  const good = !frozen && fps >= 12 && (latency === null || latency < 800)

  if (bad) {
    goodSamples = 0
    badSamples += 1
    if (badSamples >= 1) await shiftProfile(-1)
  } else if (good) {
    badSamples = 0
    goodSamples += 1
    if (goodSamples >= 4) await shiftProfile(1)
  } else {
    badSamples = Math.max(0, badSamples - 1)
    goodSamples = 0
  }
}

async function shiftProfile(direction) {
  const index = PROFILE_ORDER.indexOf(profile.value)
  const next = PROFILE_ORDER[index + direction]
  if (!next) return
  profileBusy.value = true
  try {
    const { data } = await CameraApi.streamProfile(agentId.value, next)
    profile.value = data.profile || next
    badSamples = 0
    goodSamples = 0
    ElMessage.info(`已切到${next === 'tiny' ? '低' : next === 'low' ? '标准' : '高清'}档`)
  } catch (error) {
    // 切档失败不该打断观看：保持当前档位，下一次采样再试。
    console.warn('[monitor] 切档失败', errorMessage(error))
  } finally {
    profileBusy.value = false
  }
}

async function loadState() {
  if (!agentId.value) {
    stateView.value = null
    return
  }
  try {
    const { data } = await StateApi.get(agentId.value)
    stateView.value = data
  } catch {
    // 拉不到状态不影响画面：状态和媒体是两条独立链路，页面也不该一起坏掉。
    stateView.value = null
  }
}

/**
 * 状态走 SSE 实时推（方案 §3.3：状态订阅用 SSE，REST 只做全量对账）。
 *
 * 只做一件事：把**更新版本**的状态盖上去。SSE 与 5s 轮询可能交错到达，
 * 而且 SSE 不重放——所以轮询保留，断线期间的变化靠它补齐（与文件任务靠
 * `GET /tasks` 对账是同一套约定）。
 *
 * `age_ms` / `freshness` 是服务端在**推送那一刻**算的，这里不重算（前端的时钟
 * 不可信，也不该出现第二套新鲜度算法）。设备停止上报时，最迟在下一次 5s
 * 对账里降级——这个上界由轮询周期决定，不是由 SSE 决定。
 */
function applyStatePush(view) {
  if (!view || view.agent_id !== agentId.value) return
  const incoming = view.state_version
  const current = stateView.value?.state_version
  if (typeof incoming === 'number' && typeof current === 'number' && incoming <= current) {
    return
  }
  stateView.value = view
}

async function loadSession() {
  if (!agentId.value) return
  try {
    const { data } = await CameraApi.streamStatus(agentId.value)
    session.value = data.items?.[0] || null
    streamError.value = ''
  } catch (error) {
    session.value = null
    streamError.value = errorMessage(error)
  }
}

/** 四个指示器分开显示，绝不压成一个"在线/离线"。 */
const freshness = computed(() => stateView.value?.freshness || 'unknown')
const ageSeconds = computed(() => {
  const ms = stateView.value?.age_ms
  return ms === null || ms === undefined ? null : Math.round(ms / 1000)
})
const eventLag = computed(() => stateView.value?.state?.link?.event_lag_seq ?? null)

const indicators = computed(() => [
  {
    label: '控制连接',
    value: online.value ? '在线' : '离线',
    tone: online.value ? 'ok' : 'bad'
  },
  {
    // 新鲜度只按 observed_at 算：超时显示"—"，绝不回退到"上次在线"。
    label: '状态新鲜度',
    value: freshnessLabel.value,
    tone: freshnessTone.value,
    hint: '按设备采集时刻计算，不是按"收到时刻"'
  },
  {
    label: '媒体会话',
    value: mediaLabel.value,
    tone: mediaTone.value,
    hint: session.value?.session_id ? `会话 ${session.value.session_id}` : ''
  },
  {
    label: '事件补传',
    value: eventLag.value === null ? '—' : eventLag.value > 0 ? `落后 ${eventLag.value} 条` : '已追平',
    tone: eventLag.value === null ? 'muted' : eventLag.value > 50 ? 'bad' : eventLag.value > 0 ? 'warn' : 'ok',
    hint: '事件走独立 HTTP 通道补传，落后量由设备侧上报'
  }
])

const freshnessLabel = computed(() => {
  if (freshness.value === 'unknown' || ageSeconds.value === null) return '—'
  if (freshness.value === 'fresh') return '新鲜'
  return `${ageSeconds.value} 秒前`
})

const freshnessTone = computed(() => {
  if (freshness.value === 'fresh') return 'ok'
  if (freshness.value === 'stale') return 'warn'
  return 'muted'
})

const robotState = computed(() => stateView.value?.state?.robot || null)
const cameraState = computed(() => stateView.value?.state?.camera || null)

const mediaLabel = computed(() => {
  switch (playerPhase.value) {
    case 'live':
      return '直播中'
    case 'stale':
      return `卡顿 ${frozenAgo.value ?? 0}s`
    case 'frozen':
      return `停滞 ${frozenAgo.value ?? 0}s`
    case 'reconnecting':
      return '重连中'
    case 'waiting':
    case 'connecting':
      return '等待首帧'
    case 'agent_offline':
      return '设备离线'
    case 'stream_disabled':
      return '不可用'
    case 'unsupported_codec':
      return '编码不支持'
    default:
      return '未开始'
  }
})

const mediaTone = computed(() => {
  if (playerPhase.value === 'live') return 'ok'
  if (['stale', 'reconnecting', 'waiting', 'connecting'].includes(playerPhase.value)) return 'warn'
  if (['frozen', 'stream_disabled', 'unsupported_codec', 'agent_offline'].includes(playerPhase.value))
    return 'bad'
  return 'muted'
})

/* ------------------------------ 快照 ------------------------------ */

async function takeSnapshot({ silent = false } = {}) {
  if (!agentId.value || snapshotLoading.value) return
  snapshotLoading.value = true
  try {
    const response = await CameraApi.snapshot(agentId.value, {
      sessionId: session.value?.session_id || undefined
    })
    const blob = response.data
    if (snapshotUrl.value) URL.revokeObjectURL(snapshotUrl.value)
    snapshotUrl.value = URL.createObjectURL(blob)
    snapshotAt.value = Date.now()
    if (!silent) ElMessage.success('已拍摄一张')
  } catch (error) {
    if (!silent) ElMessage.error(errorMessage(error))
  } finally {
    snapshotLoading.value = false
  }
}

let snapshotTimer = null

function stopSnapshotLoop() {
  if (snapshotTimer) {
    window.clearInterval(snapshotTimer)
    snapshotTimer = null
  }
}

// 冻结超过 5s：自动切"每 10s 一张"。这是 50kbps 以下唯一还能用的方案。
watch(playerPhase, (phase) => {
  if (phase === 'frozen') {
    if (!snapshotTimer) {
      takeSnapshot({ silent: true })
      snapshotTimer = window.setInterval(() => takeSnapshot({ silent: true }), 10000)
    }
  } else {
    stopSnapshotLoop()
  }
})

/* ------------------------------ 预置点 ------------------------------ */

async function loadPresets() {
  if (!agentId.value) return
  presetsLoading.value = true
  presetsError.value = ''
  try {
    const { data } = await CameraApi.presets(agentId.value, {
      sessionId: session.value?.session_id || undefined
    })
    presets.value = data.items || []
  } catch (error) {
    presets.value = []
    presetsError.value = errorMessage(error)
  } finally {
    presetsLoading.value = false
  }
}

async function gotoPreset(preset) {
  const result = await ptz.gotoPreset(preset.presetId ?? preset.id)
  if (result) ElMessage.success(`已转到 ${preset.name || preset.presetId}`)
  else if (ptz.lastError.value) ElMessage.warning(ptz.lastError.value)
}

/* ------------------------------ 云台 ------------------------------ */

function hold(direct) {
  if (!canControl.value) return
  ptz.press(direct)
}

function letGo() {
  ptz.release()
}

function nudge(direct) {
  // 单击（不按住）：发一次 start 立刻 stop，球机转一小步就停。
  ptz.press(direct)
  window.setTimeout(() => ptz.release(), 220)
}

function handleClick(direct, event) {
  // 鼠标/触摸点击已经被 pointerdown/pointerup 处理过（start → stop），
  // 这里再发一次 nudge 就会变成一对多余的 start/stop——表现是"点一下转两下"。
  // 只接键盘触发的 click（detail === 0，Enter/Space），那是无障碍路径。
  if (event?.detail === 0) nudge(direct)
}

/* ------------------------------ 生命周期 ------------------------------ */

let pollTimer = null
//: SSE 的退订函数。全局连接由 AppLayout 持有，这里只订阅自己关心的那类事件。
let offState = null

function attachCanvas() {
  // 画面可用才连：不支持媒体上行的设备不该出现一条永远转圈的 WS。
  if (canWatch.value && online.value && canvasRef.value) {
    stream.attach(canvasRef.value)
  }
}

onMounted(async () => {
  await device.load()
  attachCanvas()
  offState = onLiveEvent((event) => {
    if (event.type === 'state') applyStatePush(event.data)
  })
  loadState()
  loadSession()
  loadPresets()
  pollTimer = window.setInterval(() => {
    loadState()
    loadSession()
    device.load()
    evaluateProfile()
  }, 5000)
})

onBeforeUnmount(() => {
  if (pollTimer) window.clearInterval(pollTimer)
  offState?.()
  offState = null
  stopSnapshotLoop()
  if (snapshotUrl.value) URL.revokeObjectURL(snapshotUrl.value)
})

watch(agentId, () => {
  session.value = null
  stateView.value = null
  presets.value = []
  if (snapshotUrl.value) {
    URL.revokeObjectURL(snapshotUrl.value)
    snapshotUrl.value = ''
  }
  stream.reset()
  loadSession()
  loadPresets()
})

watch(online, (value) => {
  // 离线时断开（省掉一条必然失败的 WS），回来时重新挂画布并连接。
  if (value) {
    attachCanvas()
    return
  }
  stream.detach()
  // 只 detach 不改状态的话，页面会一直显示"直播中"——画布还在、最后一帧还在，
  // 但连接已经断了。这正是"画面冻了页面还说在播"的老问题。
  stream.phase.value = 'agent_offline'
})
</script>

<template>
  <section class="monitor">
    <header class="head">
      <div>
        <h2>远程监控</h2>
        <p class="sub">
          {{ agentName || '未选择设备' }}
          <span class="chip">档位 {{ profile }}</span>
          <span v-if="stream.resolution.width" class="chip">
            {{ stream.resolution.width }}×{{ stream.resolution.height }}
          </span>
        </p>
      </div>
      <div class="head-actions">
        <el-button size="small" :loading="snapshotLoading" @click="takeSnapshot()">拍一张</el-button>
        <el-button size="small" @click="stream.reconnect()">刷新画面</el-button>
      </div>
    </header>

    <div v-if="!online" class="notice bad">设备离线：控制连接已经断开，画面无法建立。</div>
    <div v-else-if="!canWatch" class="notice">
      该设备没有声明 media.ws.v1 能力，不支持实时画面。可以先用「拍一张」看现场。
    </div>

    <div class="grid">
      <div class="player-wrap">
        <canvas ref="canvasRef" class="player" />
        <!-- 保留最后一帧：冻结时不要黑屏，叠一行"最后画面 N 秒前" -->
        <img v-if="showSnapshot" :src="snapshotUrl" class="player snapshot" alt="快照" />
        <div class="overlay">
          <span class="dot" :class="mediaTone" />
          <span>{{ stream.statusText.value }}</span>
          <span v-if="snapshotAt && showSnapshot" class="overlay-sub">
            · 快照 {{ Math.round((Date.now() - snapshotAt) / 1000) }} 秒前
          </span>
        </div>
        <div class="metrics">
          <span>{{ stream.metrics.fps }} fps</span>
          <span v-if="stream.metrics.latencyMs !== null">· {{ stream.metrics.latencyMs }} ms</span>
          <span v-if="stream.metrics.bitrateKbps !== null">· {{ stream.metrics.bitrateKbps }} kbps</span>
          <span v-if="stream.metrics.droppedFrames">· 丢 {{ stream.metrics.droppedFrames }} 帧</span>
        </div>
      </div>

      <aside class="side">
        <div class="indicators">
          <div v-for="item in indicators" :key="item.label" class="indicator" :title="item.hint">
            <span class="dot" :class="item.tone" />
            <span class="indicator-label">{{ item.label }}</span>
            <span class="indicator-value">{{ item.value }}</span>
          </div>
        </div>

        <div class="block">
          <div class="block-title">设备状态</div>
          <dl class="facts">
            <template v-if="robotState">
              <dt>模式</dt><dd>{{ robotState.mode || '—' }}</dd>
              <dt>电量</dt>
              <dd>{{ robotState.battery === undefined || robotState.battery === null ? '—' : robotState.battery + '%' }}</dd>
            </template>
            <template v-if="cameraState">
              <dt>相机</dt>
              <dd>{{ cameraState.online ? '在线' : '不可用' }}</dd>
            </template>
            <template v-if="!robotState && !cameraState">
              <dt>数据</dt><dd class="muted">设备尚未上报状态</dd>
            </template>
          </dl>
        </div>

        <div class="block">
          <div class="block-title">
            云台
            <span v-if="!canControl" class="muted">该设备不支持云台</span>
          </div>
          <div class="pad" :class="{ disabled: !canControl }">
            <button
              class="pad-btn up"
              @pointerdown="hold('up')"
              @pointerup="letGo"
              @pointerleave="letGo"
              @click="handleClick('up', $event)"
            >
              ↑
            </button>
            <button
              class="pad-btn left"
              @pointerdown="hold('left')"
              @pointerup="letGo"
              @pointerleave="letGo"
              @click="handleClick('left', $event)"
            >
              ←
            </button>
            <button class="pad-btn center" @click="letGo">停</button>
            <button
              class="pad-btn right"
              @pointerdown="hold('right')"
              @pointerup="letGo"
              @pointerleave="letGo"
              @click="handleClick('right', $event)"
            >
              →
            </button>
            <button
              class="pad-btn down"
              @pointerdown="hold('down')"
              @pointerup="letGo"
              @pointerleave="letGo"
              @click="handleClick('down', $event)"
            >
              ↓
            </button>
            <button
              class="pad-btn zoom-in"
              @pointerdown="hold('zoom_in')"
              @pointerup="letGo"
              @pointerleave="letGo"
              @click="handleClick('zoom_in', $event)"
            >
              ＋
            </button>
            <button
              class="pad-btn zoom-out"
              @pointerdown="hold('zoom_out')"
              @pointerup="letGo"
              @pointerleave="letGo"
              @click="handleClick('zoom_out', $event)"
            >
              －
            </button>
          </div>
          <div class="speed">
            <span>速度</span>
            <el-slider v-model="ptz.speed.value" :min="1" :max="7" :step="1" size="small" />
          </div>
          <p v-if="ptz.lastError.value" class="inline-error">{{ ptz.lastError.value }}</p>
        </div>

        <div class="block">
          <div class="block-title">
            预置点
            <el-button size="small" text :loading="presetsLoading" @click="loadPresets">刷新</el-button>
          </div>
          <p v-if="presetsError" class="inline-error">{{ presetsError }}</p>
          <div v-else-if="!presets.length" class="muted">相机上没有预置点</div>
          <div v-else class="presets">
            <button
              v-for="preset in presets"
              :key="preset.presetId || preset.id"
              class="preset"
              :disabled="!canControl"
              @click="gotoPreset(preset)"
            >
              {{ preset.name || `预置点 ${preset.presetId}` }}
            </button>
          </div>
        </div>
      </aside>
    </div>
  </section>
</template>

<style scoped>
.monitor {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}
.head h2 {
  margin: 0;
  font-size: 18px;
}
.sub {
  margin: 4px 0 0;
  color: var(--rm-text-muted);
  font-size: 13px;
}
.chip {
  margin-left: 8px;
  padding: 1px 8px;
  border-radius: 999px;
  background: var(--rm-accent-soft);
  color: var(--rm-accent);
  font-size: 12px;
}
.head-actions {
  display: flex;
  gap: 8px;
}
.notice {
  padding: 10px 14px;
  border-radius: 12px;
  background: var(--rm-panel-soft);
  border: 1px solid var(--rm-border);
  color: var(--rm-text-muted);
  font-size: 13px;
}
.notice.bad {
  border-color: rgba(224, 71, 63, 0.35);
  color: var(--rm-danger);
}
.grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 320px;
  gap: 14px;
  align-items: start;
}
.player-wrap {
  position: relative;
  aspect-ratio: 16 / 9;
  border-radius: var(--rm-radius);
  overflow: hidden;
  background: #0b1020;
  border: 1px solid var(--rm-border);
}
.player {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: contain;
}
.snapshot {
  opacity: 0.92;
}
.overlay {
  position: absolute;
  left: 12px;
  bottom: 12px;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  border-radius: 999px;
  background: rgba(11, 16, 32, 0.72);
  color: #f5f8fd;
  font-size: 12px;
  backdrop-filter: blur(6px);
}
.overlay-sub {
  color: rgba(245, 248, 253, 0.7);
}
.metrics {
  position: absolute;
  right: 12px;
  bottom: 12px;
  display: flex;
  gap: 6px;
  padding: 6px 10px;
  border-radius: 999px;
  background: rgba(11, 16, 32, 0.55);
  color: rgba(245, 248, 253, 0.85);
  font-size: 12px;
}
.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--rm-text-faint);
}
.dot.ok {
  background: var(--rm-success);
}
.dot.warn {
  background: var(--rm-warning);
}
.dot.bad {
  background: var(--rm-danger);
}
.dot.muted {
  background: var(--rm-border-strong);
}
.side {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.indicators {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}
.indicator {
  display: grid;
  grid-template-columns: auto 1fr;
  grid-template-rows: auto auto;
  gap: 2px 6px;
  padding: 10px 12px;
  border-radius: 12px;
  border: 1px solid var(--rm-border);
  background: var(--rm-panel);
}
.indicator .dot {
  grid-row: 1;
  align-self: center;
}
.indicator-label {
  font-size: 12px;
  color: var(--rm-text-muted);
}
.indicator-value {
  grid-column: 1 / -1;
  font-size: 15px;
  font-weight: 600;
}
.block {
  padding: 12px;
  border-radius: 12px;
  border: 1px solid var(--rm-border);
  background: var(--rm-panel);
}
.block-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-weight: 600;
  font-size: 13px;
  margin-bottom: 10px;
}
.muted {
  color: var(--rm-text-faint);
  font-size: 12px;
  font-weight: 400;
}
.pad {
  display: grid;
  grid-template-columns: repeat(3, 44px);
  grid-template-rows: repeat(3, 44px);
  gap: 6px;
  justify-content: center;
  user-select: none;
}
.pad.disabled {
  opacity: 0.45;
  pointer-events: none;
}
.pad-btn {
  border: 1px solid var(--rm-border-strong);
  background: var(--rm-panel-soft);
  border-radius: 10px;
  font-size: 16px;
  cursor: pointer;
  touch-action: none;
}
.pad-btn:active {
  background: var(--rm-accent-soft);
  border-color: var(--rm-accent-line);
}
.pad-btn.up {
  grid-area: 1 / 2;
}
.pad-btn.left {
  grid-area: 2 / 1;
}
.pad-btn.center {
  grid-area: 2 / 2;
  font-size: 13px;
  color: var(--rm-danger);
  border-color: rgba(224, 71, 63, 0.35);
}
.pad-btn.right {
  grid-area: 2 / 3;
}
.pad-btn.down {
  grid-area: 3 / 2;
}
.pad-btn.zoom-in {
  grid-area: 1 / 3;
  font-size: 12px;
}
.pad-btn.zoom-out {
  grid-area: 3 / 3;
  font-size: 12px;
}
.speed {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 8px;
  font-size: 12px;
  color: var(--rm-text-muted);
}
.speed :deep(.el-slider) {
  flex: 1;
}
.facts {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 6px 12px;
  margin: 0;
  font-size: 13px;
}
.facts dt {
  color: var(--rm-text-muted);
}
.facts dd {
  margin: 0;
  font-weight: 600;
}
.inline-error {
  margin: 8px 0 0;
  font-size: 12px;
  color: var(--rm-warning);
}
.presets {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.preset {
  padding: 6px 12px;
  border-radius: 999px;
  border: 1px solid var(--rm-border);
  background: var(--rm-panel-soft);
  font-size: 12px;
  cursor: pointer;
}
.preset:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
@media (max-width: 1080px) {
  .grid {
    grid-template-columns: 1fr;
  }
}
</style>
