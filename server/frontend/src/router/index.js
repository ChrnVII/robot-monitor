import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'

// 一级导航（侧栏）：文件管理、节点。
// 「文件管理」下面挂三个子页面，在顶栏以 tab 出现；「节点」是独立页面。
// meta.device = true 的页面受顶栏那个全局节点下拉框影响（见 layout/AppLayout.vue）。
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
      { path: 'manage/nodes', redirect: { name: 'nodes' } },
      {
        path: 'manage/browse',
        name: 'browse',
        component: () => import('../views/BrowseView.vue'),
        meta: { title: '取件', device: true }
      },
      {
        path: 'manage/tasks',
        name: 'tasks',
        component: () => import('../views/TasksView.vue'),
        meta: { title: '上传任务', device: true }
      },
      {
        path: 'manage/files',
        name: 'files',
        component: () => import('../views/FilesView.vue'),
        meta: { title: '文件库' }
      }
    ]
  },
  // 改层级之前收藏的书签、以及外部链接：按 name 重定向，不保留旧路径。
  { path: '/browse', redirect: { name: 'browse' } },
  { path: '/tasks', redirect: { name: 'tasks' } },
  { path: '/files', redirect: { name: 'files' } },
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
  document.title = to.meta.title ? `${to.meta.title} · FileBridge` : 'FileBridge'
})

export default router
