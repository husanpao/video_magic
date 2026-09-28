import { ElMessage } from 'element-plus'
import { api } from '@/api/client'
import type { ShotRow } from '@/api/types'
import { guard, state } from './state'

/**
 * 任务域：流水线运行态（status / 日志 / 队列）+ 乐观「生成中」标记。
 *
 * 这一域是**唯一一个会被 2s 轮询反复写**的，所以它单独立一个文件：
 * 其他屏（镜头表、素材）只在任务刚结束时才需要重拉。
 *
 * `logOffset` 保持模块级私有 —— 它是**增量游标**而不是视图状态，
 * 界面从不读它，放进 `state` 只会让"切项目要不要清游标"这种问题显形。
 */
let logOffset = 0

export async function refreshStatus(): Promise<void> {
  if (!state.project) return
  await guard('状态', async () => {
    const st = await api.status(state.project)
    state.task = st.task || {}
    state.progress = st.progress || state.progress
    state.readiness = st.readiness || {}
    state.final = st.final ?? null
    state.finals = st.finals ?? []
    state.running = !!st.running
  })
}

export async function pullLog(): Promise<void> {
  if (!state.project) return
  await guard('日志', async () => {
    const r = await api.log(state.project, logOffset)
    if (r.reset) {
      state.logText = ''
      logOffset = 0
    }
    if (r.text) state.logText += r.text
    logOffset = r.next
    if (state.logText.length > 400_000) state.logText = state.logText.slice(-200_000)
  })
}

export async function refreshQueue(): Promise<void> {
  if (!state.project) return
  await guard('队列', async () => {
    state.queue = await api.queue(state.project)
  })
}

/* ---------------- 乐观「生成中」标记（U2） ---------------- */

/** 渲染提交后**本地先标生成中** —— 后端任务起来前的几秒里表格不能毫无反应
 *  （旧行为：等下一轮 4s 轮询才出现「生成中」，用户以为没点上）。
 *  记录提交时刻：产物 mtime 晚于它 = 这轮已出片，标记自动失效。 */
export function markRendering(ids: string[]): void {
  const now = Date.now()
  const next = { ...state.renderHint }
  for (const id of ids) next[id] = now
  state.renderHint = next
}

/** 清理失效的乐观标记：出片了 / 消失了 / 超时（兜底，防任务卡死时永远「生成中」）。 */
export function pruneRenderHints(rows: ShotRow[]): void {
  const keys = Object.keys(state.renderHint)
  if (!keys.length) return
  const now = Date.now()
  const next = { ...state.renderHint }
  let dirty = false
  for (const id of keys) {
    const at = next[id]
    const s = rows.find((r) => r.id === id)
    if (!s || now - at > 120_000 || (s.clip && s.clip.exists && s.clip.mtime * 1000 >= at)) {
      delete next[id]
      dirty = true
    }
  }
  if (dirty) state.renderHint = next
}

/** 任务结束（或切项目）后整批清掉 —— 没有任务就没有"生成中"。 */
export function clearRenderHints(): void {
  state.renderHint = {}
}

/** 渲染提交的公共出口（单镜 / 表格按钮 / 弹层 / 快捷键 R 共用）。
 *  提交后**本地先标「生成中」**（markRendering），不用等下一轮轮询。 */
export async function submitRerender(id: string): Promise<void> {
  if (!state.project || !id) return
  const r = await api.run(state.project, 'render', { force: true, only: [id] })
  markRendering([id])
  ElMessage.success(r.message || `已开始重渲 ${id}`)
  window.setTimeout(() => { void refreshStatus() }, 300)
}
