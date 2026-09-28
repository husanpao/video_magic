import { computed } from 'vue'
import { api } from '@/api/client'
import type { AssetRow, SceneRow, StoryboardRow } from '@/api/types'
import { guard, state } from './state'

/**
 * 素材实体域：角色定妆 / 场景 / 道具 / 分镜图 / 概念图候选。
 *
 * 这一域是「抽卡 → 采纳」那条交互闭环的数据面，特点是**写多读也多**：
 * 每个 tab 首次进入拉一次，点采纳后再拉一次。所以每个 refresh 都独立，
 * 不互相牵连（`refreshScenes` 失败不该让角色卡变空）。
 *
 * 与镜头域的关系是**单向**的：`shots.ts` 用这里的 `sceneById` 给镜头表
 * 的「场景」列取实体名，这里绝不反向读镜头 —— 否则两个域互相 import，
 * 又变回一个文件。
 */

export async function refreshChars(): Promise<void> {
  if (!state.project) return
  await guard('角色', async () => {
    const r = await api.chars(state.project)
    state.chars = r.chars
  })
}

export async function refreshScenes(): Promise<void> {
  if (!state.project) return
  await guard('场景', async () => {
    const r = await api.scenes(state.project)
    state.scenes = r.scenes
    state.scenesAssigned = r.assigned
    state.scenesTotal = r.total_shots
  })
}

export async function refreshProps(): Promise<void> {
  if (!state.project) return
  await guard('道具', async () => {
    const r = await api.props(state.project)
    state.props = r.props
    state.propsShotsWith = r.shots_with_props ?? 0
  })
}

export async function refreshAssets(): Promise<void> {
  if (!state.project) return
  await guard('素材', async () => {
    const r = await api.assets(state.project)
    state.assets = r.assets || []
  })
}

/** 分镜图：id → 该镜的分镜图状态。表格/弹层要用它决定是否加载图。 */
export const storyboardById = computed(() => {
  const m: Record<string, StoryboardRow> = {}
  for (const x of state.storyboard) m[x.id] = x
  return m
})

export async function refreshStoryboard(): Promise<void> {
  if (!state.project) return
  await guard('分镜图', async () => {
    const r = await api.storyboard(state.project)
    state.storyboard = r.shots || []
    state.storyboardModel = r.model || null
  })
}

/** 场景实体：id → SceneRow。用于表格「场景」列与左栏分组。
 *  ⚠️ 与 id 里的场次号（`sceneOf`）**不是一回事**：本片 52 镜被分成 47 个场次号，
 *  那只是编号；场景实体才是"同一个地点"（本片 3 个）。 */
export const sceneById = computed(() => {
  const m: Record<string, SceneRow> = {}
  for (const x of state.scenes) m[x.id] = x
  return m
})

/** 某个场景/道具的候选（没出过图就返回空数组）。 */
export function assetOf(kind: 'scene' | 'prop', id: string): AssetRow | undefined {
  return state.assets.find((a) => a.kind === kind && a.id === id)
}
