import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'

// 一级导航（侧栏）：文件管理、节点。
// 「文件管理」下面挂三个子页面，在顶栏以 tab 出现；「节点」是独立页面。
// 顶栏的节点下拉框是常驻的全局状态，不属于任何单个路由。
const routes = [
  {
    path: '/login',
    name: 'login',
    component: () => import('../views/LoginView.vue'),
    meta: { public: true, title: '登录' }
  },
  {
    path: '/',
    component: () => import('../layout/AppLayout.vue'),
    children: [
      { path: '', redirect: { name: 'nodes' } },
      {
        path: 'nodes',
        name: 'nodes',
        component: () => import('../views/NodesView.vue'),
        meta: { title: '节点' }
      },
      { path: 'manage', redirect: { name: 'browse' } },
      {
        path: 'manage/browse',
        name: 'browse',
        component: () => import('../views/BrowseView.vue'),
        meta: { title: '取件' }
      },
      {
        path: 'manage/tasks',
        name: 'tasks',
        component: () => import('../views/TasksView.vue'),
        meta: { title: '上传任务' }
      },
      {
        path: 'manage/files',
        name: 'files',
        component: () => import('../views/FilesView.vue'),
        meta: { title: '文件库' }
      }
    ]
  },
  { path: '/:pathMatch(.*)*', redirect: '/' }
]

const router = createRouter({
  history: createWebHistory(),
  routes
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  if (!auth.checked) {
    await auth.fetchMe()
  }

  if (to.meta.public) {
    return auth.isLoggedIn && to.name === 'login' ? { name: 'nodes' } : true
  }
  if (!auth.isLoggedIn) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  return true
})

router.afterEach((to) => {
  document.title = to.meta.title ? `${to.meta.title} · Robot Monitor` : 'Robot Monitor'
})

export default router
