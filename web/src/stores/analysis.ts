import { api } from '@/api/client'
import { guard, state } from './state'

/**
 * 校验与清单域：质检 / 审计 / 完备性 / 剧本 / 章节。
 *
 * 这一域是「不压缩」纪律的**读数面**：台词覆盖、逐字保真、完备性矩阵都从这里出。
 * 它只读不写镜头数据 —— 要修问题是 shots 域的事，这里只负责让问题可见。
 */

export async function refreshQc(): Promise<void> {
  if (!state.project) return
  await guard('质检', async () => {
    const r = await api.qc(state.project)
    state.qc = {
      results: r.results, counts: r.counts, review: r.review, rerender: r.rerender,
      has_result: r.has_result, generated_at: r.generated_at,
    }
  })
}

export async function refreshAudit(): Promise<void> {
  if (!state.project) return
  await guard('审计', async () => {
    state.audit = await api.audit(state.project)
  })
}

export async function refreshCompleteness(): Promise<void> {
  if (!state.project) return
  await guard('完备性', async () => {
    state.completeness = await api.completeness(state.project)
  })
}

export async function refreshScript(): Promise<void> {
  if (!state.project) return
  await guard('剧本', async () => {
    const r = await api.script(state.project)
    state.scripts = r.chapters || []
  })
}

export async function refreshChapters(): Promise<void> {
  if (!state.project) return
  await guard('章列表', async () => {
    const r = await api.chapters(state.project)
    state.chapters = r.chapters || []
  })
}
