/**
 * 工作区路由（F1.1）—— 导航即流程。
 *
 * W0 项目 → W1 章节 → W2 实体与风格 → **W3 镜头工作台（默认屏）** → W4 成片与审片
 *
 * ★ 用 **hash 模式**而不是 history 模式。
 * 后端 `vm/web.py` 是纯标准库服务、路由是**精确匹配**（`ROUTES_GET`），
 * history 模式要求"未知路径回落到 index.html"，那就得开一个前缀兜底口子 ——
 * 而 `web.py:393` 的注释明写着"前缀路由是目录穿越的高发区，只留 `/assets/` 一个口子"。
 * 为了几个内部链接去动那条安全纪律不值。hash 模式还顺带满足"可分享/可回退"。
 *
 * ★ 默认屏是 **W3 工作台**，不是 W0。
 * 老用户打开控制台是要看镜头表的；落到项目列表等于每次都多两步。
 * 只有"项目还没有任何章节"时才由 `App.vue` 的守卫把新手指到 W1（见 F1.4 空态向导）。
 */

import { createRouter, createWebHashHistory } from 'vue-router'
import ProjectsView from '@/views/ProjectsView.vue'
import ChaptersView from '@/views/ChaptersView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'
import StyleView from '@/views/StyleView.vue'
import ReviewView from '@/views/ReviewView.vue'

/** 工作区常量：TopBar 的切换器与路由表**共用这一份**，避免两处名单漂。
 *  `ready: false` 的项在切换器上会显示但禁用，并写明它属于哪一期 ——
 *  比"藏起来"好：用户能看见路线图；比"能点进去看空白"好：不会被误以为自己操作错。 */
export type WorkspaceKey = 'projects' | 'chapters' | 'style' | 'workbench' | 'review'

export interface WorkspaceDef {
  key: WorkspaceKey
  label: string
  path: string
  hint: string
  ready: boolean
}

export const WORKSPACES: WorkspaceDef[] = [
  { key: 'projects', label: '项目', path: '/projects', hint: '新建项目、切换项目', ready: true },
  { key: 'chapters', label: '章节', path: '/chapters', hint: '导入/编辑章节正文，管理章节顺序', ready: true },
  { key: 'style', label: '风格', path: '/style', hint: '整本小说的画面风格；改之前会先摊开影响清单', ready: true },
  { key: 'workbench', label: '镜头', path: '/workbench', hint: '镜头表 / 渲染 / 质检 —— 日常工作台', ready: true },
  { key: 'review', label: '成片', path: '/review', hint: '播放器 + 质检/审计清单：点一条问题直接播到那一镜', ready: true },
]

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/workbench' },
    { path: '/projects', name: 'projects', component: ProjectsView },
    { path: '/chapters', name: 'chapters', component: ChaptersView },
    { path: '/style', name: 'style', component: StyleView },
    { path: '/workbench', name: 'workbench', component: WorkbenchView },
    { path: '/review', name: 'review', component: ReviewView },
    { path: '/:pathMatch(.*)*', redirect: '/workbench' },
  ],
})

export default router
