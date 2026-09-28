import { computed } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '@/api/client'
import { confirmAction } from '@/composables/confirmAction'
import type { ShotRow } from '@/api/types'
import { errText, guard, state } from './state'
import { KIND_INFO, type AnyKind } from './types'
import { chapterOf, sceneOf } from './format'
import { pruneRenderHints, submitRerender } from './task'
import { pushEdit } from './history'
import { sceneById } from './entities'

/**
 * 镜头域：镜头表 + 派生档 + 筛选 + 单选/多选。
 *
 * ★ `kindOf` 是这一域的核心，也是全项目最值得单独说清的一段判定：
 *   **不信任数据库状态字段**，按「任务态 + 质检 + 产物 + 指纹」实时推导。
 *   优先级经过实测校准：`stale` **优先于** `suspicious` ——
 *   产物过期比质检可疑更该先重渲（质检的是旧产物，结论已经不作数了）。
 */
export function kindOf(s: ShotRow): AnyKind {
  // 乐观「生成中」：渲染刚提交、任务还没被轮询到的那几秒先自己显示（U2）。
  // 产物是新的（mtime 晚于提交时刻）就说明这轮已经出片，别再挡着派生结论。
  const hinted = state.renderHint[s.id]
  if (hinted && (!s.clip || !s.clip.exists || s.clip.mtime * 1000 < hinted)) return 'rendering'
  if (state.running && state.task) {
    if (state.task.shot && state.task.shot === s.id) return 'rendering'
    if ((state.task.only || []).includes(s.id) && !(s.clip && s.clip.exists)) return 'rendering'
  }
  const v = (s.qc && s.qc.verdict) || ''
  if (v === 'error') return 'failed' // 检查未执行 / 文件坏
  if (v === 'fail') return 'qc_failed'
  if (!(s.clip && s.clip.exists)) return 'missing' // 无产物 = 待生成
  if (s.status === 'stale') return 'stale' // ★ 指纹不匹配 = 需重渲
  if (v === 'suspicious') return 'suspicious'
  if (s.status === 'current') return 'current'
  return 'missing'
}

export function kindInfo(k: AnyKind) {
  return KIND_INFO[k as keyof typeof KIND_INFO] ?? KIND_INFO.missing
}

export const visibleShots = computed(() =>
  state.shots.filter((s) => {
    // 多集：按章筛选（null = 全部章）
    if (state.chapter !== null && chapterOf(s.id) !== state.chapter) return false
    if (state.filter !== 'all' && kindOf(s) !== state.filter) return false
    if (!state.q) return true
    const q = state.q.toLowerCase()
    return [s.id, s.dialogue, s.narration, s.camera, s.shot_size, ...(s.chars || [])]
      .join(' ').toLowerCase().includes(q)
  }),
)

/** 某镜的场景显示名。有场景实体用实体名，没有就退回场次号并标注。 */
export function sceneLabel(s: ShotRow): { name: string; sub: string; real: boolean } {
  const sc = s.scene_id ? sceneById.value[s.scene_id] : undefined
  if (sc) return { name: sc.name, sub: sc.time_of_day || '', real: true }
  return { name: sceneOf(s.id), sub: '', real: false }
}

/** 左栏分组：按**场景实体**分（没有实体时退回场次号）。 */
export function groupShots(shots: ShotRow[]): Array<{ key: string; name: string; real: boolean; items: ShotRow[] }> {
  const out: Array<{ key: string; name: string; real: boolean; items: ShotRow[] }> = []
  const idx = new Map<string, number>()
  for (const s of shots) {
    const sc = s.scene_id ? sceneById.value[s.scene_id] : undefined
    const key = sc ? sc.id : sceneOf(s.id)
    const name = sc ? sc.name : `第 ${sceneOf(s.id)} 场`
    let i = idx.get(key)
    if (i === undefined) {
      i = out.length
      idx.set(key, i)
      out.push({ key, name, real: !!sc, items: [] })
    }
    out[i].items.push(s)
  }
  return out
}

export const kindCounts = computed(() => {
  const c: Record<string, number> = {}
  for (const s of state.shots) {
    const k = kindOf(s)
    c[k] = (c[k] || 0) + 1
  }
  return c
})

/** 分段播放器时间轴。★ 必须用 `sec_actual` —— H3 有 17k+5 帧格点，
 *  请求 264s 实际 265.875s，按请求值算累计会导致点第 N 段跳错位置且越往后越偏。 */
export function segTimeline(shots: ShotRow[] = state.shots) {
  const cum: number[] = []
  let t = 0
  for (const s of shots) {
    cum.push(t)
    t += Number(s.sec_actual || s.sec || 0)
  }
  return { cum, total: t }
}

/** 操作按钮**随状态改名**（printfilm / StoryboardPage 的做法）：
 *  同一个按钮在"无产物"时说「生成画面」，在"有产物"时说「重绘画面」。 */
export function opsFor(s: ShotRow) {
  const k = kindOf(s)
  const has = !!(s.clip && s.clip.exists)
  return {
    primary: has ? '重渲' : '渲染',
    primaryDisabled: state.busy || !!state.running,
    primaryTitle: state.running ? '有任务在跑，先等它结束' : `渲染 ${s.id}`,
    kind: k,
  }
}

/* ---------------- 多选（U1） ---------------- */

export function isChecked(id: string): boolean {
  return state.checked.includes(id)
}

/** 翻转一镜的多选状态。`on` 给定时直接置位（全选/复选框受控渲染用）。 */
export function toggleChecked(id: string, on?: boolean): void {
  const has = state.checked.includes(id)
  const want = on ?? !has
  if (want === has) return
  state.checked = want ? [...state.checked, id] : state.checked.filter((x) => x !== id)
}

/** 「全选当前筛选」的三态判断：可见行全选中 = 可一键取消。 */
export const allVisibleChecked = computed(() =>
  visibleShots.value.length > 0 && visibleShots.value.every((s) => state.checked.includes(s.id)),
)

export function toggleCheckAllVisible(): void {
  state.checked = allVisibleChecked.value ? [] : visibleShots.value.map((s) => s.id)
}

/* ---------------- 拉取与就地更新 ---------------- */

export async function refreshShots(): Promise<void> {
  if (!state.project) return
  await guard('镜头表', async () => {
    const r = await api.shots(state.project)
    state.shots = r.shots
    state.counts = r.counts
    state.shotsLoaded = true
    // 表变了，顺手把失效的乐观标记 / 已删镜的选中清掉（不然复选框勾着一行不存在的镜）
    pruneRenderHints(r.shots)
    const alive = new Set(r.shots.map((s) => s.id))
    if (state.checked.some((id) => !alive.has(id))) state.checked = state.checked.filter((id) => alive.has(id))
    if (state.selected && !alive.has(state.selected)) state.selected = ''
  })
}

/** 置位 E1 标记并就地更新状态（不整表重拉）。
 *  `locked` 会进撤销栈（锁定/解锁是可回滚的资产操作）。 */
export async function setFlag(id: string, flag: 'locked' | 'selected' | 'favorite', value: boolean) {
  state.busy = true
  try {
    const r = await api.lockShot(state.project, id, flag, value)
    syncFlag(id, flag, r[flag])
    if (flag === 'locked') {
      const apply = async (v: boolean) => {
        const rr = await api.lockShot(state.project, id, 'locked', v)
        syncFlag(id, 'locked', rr.locked)
      }
      // 逆操作交给 history 域的 pushEdit —— 这里不自己记账，
      // 否则「谁在往栈里写」会有两个出处，撤销顺序就没法解释了。
      pushEdit({
        label: `${value ? '锁定' : '解锁'} ${id}`,
        undo: () => apply(!value),
        redo: () => apply(value),
      })
    }
    return r
  } finally {
    state.busy = false
  }
}

function syncFlag(id: string, flag: 'locked' | 'selected' | 'favorite', value: boolean): void {
  const s = state.shots.find((x) => x.id === id)
  if (s && (flag === 'locked' || flag === 'selected')) s[flag] = value
}

/** 快捷键 L：切换选中镜的锁定。 */
export async function toggleLockShot(s: ShotRow): Promise<void> {
  try {
    const r = await setFlag(s.id, 'locked', !s.locked)
    ElMessage.success(r.message || (r.locked ? `已锁定 ${s.id}` : `已解锁 ${s.id}`))
  } catch (e) {
    ElMessage.error(`锁定失败：${errText(e)}`)
  }
}

/** 单镜渲染 / 重渲（带统一确认）。
 *  ⚠️ 后端 `/api/rerender` 只收单镜；这里用与之完全等价的
 *  `_start({stage:'render'}, only=[shot], force=True)`，并保留「弹确认」的习惯动作。 */
export async function rerenderOne(s: ShotRow): Promise<void> {
  if (state.running) {
    ElMessage.warning('有任务在跑，先等它结束（同项目同时只允许一个任务）')
    return
  }
  const has = !!(s.clip && s.clip.exists)
  const ok = await confirmAction({
    title: has ? '强制重渲' : '渲染',
    message: has
      ? `强制重渲镜头 ${s.id}？会用当前提示词重新生成并替换这一版。`
      : `按当前提示词渲染镜头 ${s.id}？`,
    confirmText: has ? '重渲' : '渲染',
  })
  if (!ok) return
  try {
    await submitRerender(s.id)
  } catch (e) {
    ElMessage.error(`重渲失败：${errText(e)}`)
  }
}

