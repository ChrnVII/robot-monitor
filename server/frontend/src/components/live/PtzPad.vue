<script setup>
// 云台方向盘：按住持续转、松开停。
//
// 只负责"按下/松开"这个事实，发什么请求由页面决定——因为那里才知道当前
// 会话 id、错误处理和"本地已接管"的提示该落在哪。
const props = defineProps({
  disabled: { type: Boolean, default: false },
  holding: { type: String, default: '' }
})
const emit = defineEmits(['start', 'stop'])

const DIRECTIONS = [
  { key: 'up', icon: 'ArrowUp', label: '上' },
  { key: 'left', icon: 'ArrowLeft', label: '左' },
  { key: 'right', icon: 'ArrowRight', label: '右' },
  { key: 'down', icon: 'ArrowDown', label: '下' }
]

function press(direct) {
  if (props.disabled) return
  emit('start', direct)
}

function release() {
  emit('stop')
}
</script>

<template>
  <div class="ptz">
    <div class="ptz-grid">
      <button
        v-for="item in DIRECTIONS"
        :key="item.key"
        class="ptz-key"
        :class="[`ptz-${item.key}`, { active: holding === item.key }]"
        :disabled="disabled"
        :aria-label="item.label"
        @pointerdown.prevent="press(item.key)"
        @pointerup.prevent="release"
        @pointerleave="release"
        @pointercancel="release"
        @contextmenu.prevent
      >
        <el-icon><component :is="item.icon" /></el-icon>
      </button>
      <div class="ptz-center" :class="{ active: holding === 'stop' }">
        <span>PTZ</span>
      </div>
    </div>

    <div class="ptz-zoom">
      <button
        class="ptz-key ptz-zoom-key"
        :disabled="disabled"
        aria-label="放大"
        @pointerdown.prevent="press('zoom_in')"
        @pointerup.prevent="release"
        @pointerleave="release"
        @pointercancel="release"
      >
        <el-icon><ZoomIn /></el-icon>
      </button>
      <button
        class="ptz-key ptz-zoom-key"
        :disabled="disabled"
        aria-label="缩小"
        @pointerdown.prevent="press('zoom_out')"
        @pointerup.prevent="release"
        @pointerleave="release"
        @pointercancel="release"
      >
        <el-icon><ZoomOut /></el-icon>
      </button>
    </div>
  </div>
</template>

<style scoped>
.ptz {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 14px;
}

.ptz-grid {
  display: grid;
  grid-template-columns: repeat(3, 52px);
  grid-template-rows: repeat(3, 52px);
  gap: 6px;
}

.ptz-key {
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--rm-border);
  background: var(--rm-panel);
  border-radius: 12px;
  color: var(--rm-text);
  font-size: 18px;
  cursor: pointer;
  transition: background 0.12s ease, border-color 0.12s ease, transform 0.08s ease;
  touch-action: none;
}

.ptz-key:hover:not(:disabled) {
  background: var(--rm-hover);
  border-color: var(--rm-accent-line);
}

.ptz-key:active:not(:disabled),
.ptz-key.active {
  background: var(--rm-accent-soft);
  border-color: var(--rm-accent);
  color: var(--rm-accent);
  transform: scale(0.96);
}

.ptz-key:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.ptz-up {
  grid-column: 2;
  grid-row: 1;
}

.ptz-left {
  grid-column: 1;
  grid-row: 2;
}

.ptz-right {
  grid-column: 3;
  grid-row: 2;
}

.ptz-down {
  grid-column: 2;
  grid-row: 3;
}

.ptz-center {
  grid-column: 2;
  grid-row: 2;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  background: var(--rm-panel-soft);
  border: 1px dashed var(--rm-border);
  color: var(--rm-text-faint);
  font-size: 11px;
  letter-spacing: 0.06em;
}

.ptz-zoom {
  display: flex;
  gap: 10px;
}

.ptz-zoom-key {
  width: 52px;
  height: 38px;
  border-radius: 10px;
}
</style>
