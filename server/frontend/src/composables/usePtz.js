// 云台方向键：按住发 start、每 1.5s keepalive、松开发 stop。
//
// 三个刻意的取舍：
//
//   1. **请求串行化。** 前一条没回来就发下一条，弱网下会并发堆起来，
//      最后谁到谁后完全随机。这里用一条 promise 链：永远只有一条在途。
//   2. **stop 必须发出去。** 松手是最高优先级；即使前面的请求失败、超时、
//      页面正在切换设备，也要把 stop 补上——否则球机会一直转到它的
//      deadman（3s）超时。
//   3. **结果未知要如实说。** 504 不代表没执行，代表没等到结果。这时页面
//      该提示"结果未知，可查询当前预置点"，而不是让用户反复点。
import { ref, onBeforeUnmount } from 'vue'
import { CameraApi } from '../api/camera'
import { errorMessage } from '../api/client'

const KEEPALIVE_MS = 1500

export function usePtz({ agentId, sessionId } = {}) {
  const holding = ref('')
  const busy = ref(false)
  const speed = ref(4)
  const lastError = ref('')
  const lastState = ref('') // succeeded | rejected | unknown | failed

  let chain = Promise.resolve()
  let keepaliveTimer = null
  let pendingStop = false

  const currentAgentId = () => (typeof agentId === 'function' ? agentId() : agentId?.value ?? agentId)
  const currentSession = () =>
    typeof sessionId === 'function' ? sessionId() : sessionId?.value ?? sessionId

  function enqueue(task) {
    chain = chain.then(task, task)
    return chain
  }

  async function send(direct, action) {
    const id = currentAgentId()
    if (!id) return
    busy.value = true
    try {
      const { data } = await CameraApi.ptz(id, {
        action,
        direct,
        speed: speed.value,
        sessionId: currentSession() || undefined
      })
      lastState.value = data?.state || 'succeeded'
      lastError.value = ''
    } catch (error) {
      const status = error?.response?.status
      const body = error?.response?.data || {}
      lastState.value = body?.data?.state || (status === 504 ? 'unknown' : 'failed')
      lastError.value =
        status === 504
          ? '云台结果未知：设备可能已经执行，建议查一下当前预置点'
          : errorMessage(error)
    } finally {
      busy.value = false
    }
  }

  function stopKeepalive() {
    if (keepaliveTimer) {
      window.clearInterval(keepaliveTimer)
      keepaliveTimer = null
    }
  }

  function press(direct) {
    if (!direct || holding.value === direct) return
    holding.value = direct
    pendingStop = false
    enqueue(() => send(direct, 'start'))
    stopKeepalive()
    keepaliveTimer = window.setInterval(() => {
      if (!holding.value || pendingStop) return
      // keepalive 只更新 deadline，不改方向：按住不放时不该产生新的"动作"。
      enqueue(() => send(holding.value, 'keepalive'))
    }, KEEPALIVE_MS)
  }

  function release() {
    const direct = holding.value
    holding.value = ''
    stopKeepalive()
    // 没有按住过就不发 stop：避免在别人控制时凭空插一条停止（会抢租约）。
    if (!direct) return
    pendingStop = true
    enqueue(() => send(direct, 'stop'))
  }

  /** 预置点：一次动作、幂等、可重试。 */
  async function gotoPreset(preset) {
    const id = currentAgentId()
    if (!id) return null
    busy.value = true
    try {
      const { data } = await CameraApi.gotoPreset(id, preset, {
        sessionId: currentSession() || undefined
      })
      lastState.value = data?.state || 'succeeded'
      lastError.value = ''
      return data
    } catch (error) {
      const status = error?.response?.status
      lastState.value = status === 504 ? 'unknown' : 'failed'
      lastError.value = status === 504 ? '云台结果未知，建议重新查询预置点' : errorMessage(error)
      return null
    } finally {
      busy.value = false
    }
  }

  onBeforeUnmount(() => {
    release()
    stopKeepalive()
  })

  return { holding, busy, speed, lastError, lastState, press, release, gotoPreset }
}
