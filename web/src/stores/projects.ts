import { api } from '@/api/client'
import { guard, state } from './state'

/**
 * 项目域：项目列表 + 当前项目。
 *
 * `loadProjects` 是整个控制台的第一次拉取（`App.vue` 的 `onMounted`），
 * 也是"首拉失败白屏"（B6）当年出问题的那个入口 —— 失败必须冒到全局错误条，
 * 所以它走 `guard`，不自己吞异常。
 */
export async function loadProjects(): Promise<void> {
  await guard('项目列表', async () => {
    const r = await api.projects()
    state.projects = r.projects
    if (!state.project) state.project = r.default || (r.projects[0]?.name ?? '')
  })
}
