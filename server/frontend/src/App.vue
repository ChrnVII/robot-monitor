<script setup>
import { onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from './stores/auth'

const router = useRouter()
const auth = useAuthStore()

// 任何一个请求拿到 401 都说明会话没了。集中跳登录页，避免每个页面各写一遍。
function handleUnauthorized() {
  const current = router.currentRoute.value
  auth.user = null
  if (current.name === 'login') return
  router.push({ name: 'login', query: { redirect: current.fullPath } })
}

onMounted(() => window.addEventListener('fb:unauthorized', handleUnauthorized))
onUnmounted(() => window.removeEventListener('fb:unauthorized', handleUnauthorized))
</script>

<template>
  <router-view />
</template>
