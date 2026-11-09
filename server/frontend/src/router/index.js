import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'

// 层级：「文件管理」是一级导航（侧栏里那一项），它下面挂四个子页面，
// 子页面在顶栏以 tab 的形式出现（见 layout/AppLayout.vue）。
// 一级那层自己不渲染内容，只负责把那四个收在同一个前缀下。
const routes = [
  {
    path: '/login',
    name: 'login',
    component: () => import('../views/LoginView.vue'),
    meta: { public: true, title: '登录' }
  },
  {
    path: '/manage',
    component: () => import('../layout/AppLayout.vue'),
    meta: { title: '文件管理' },
    children: [
      { path: '', redirect: { name: 'nodes' } },
      {
        path: 'nodes',
        name: 'nodes',
        component: () => import('../views/NodesView.vue'),
        meta: { title: '节点' }
      },
      {
        path: 'browse',
        name: 'browse',
        component: () => import('../views/BrowseView.vue'),
        meta: { title: '取件' }
      },
      {
        path: 'tasks',
        name: 'tasks',
        component: () => import('../views/TasksView.vue'),
        meta: { title: '任务' }
      },
      {
        path: 'files',
        name: 'files',
        component: () => import('../views/FilesView.vue'),
        meta: { title: '文件库' }
      }
    ]
  },
  { path: '/', redirect: { name: 'nodes' } },
  // 改层级之前收藏的书签、以及外部链接：按 name 重定向，不保留旧路径。
  { path: '/nodes', redirect: { name: 'nodes' } },
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
