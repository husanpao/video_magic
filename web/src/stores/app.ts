import { computed, reactive, readonly } from 'vue'
import { ElMessage } from 'element-plus'
import { api, ApiError } from '@/api/client'
import { confirmAction } from '@/composables/confirmAction'
import type {
  AuditResponse, CharRow, ProjectSummary, PropRow, QcResponse, SceneRow,
  AssetRow, ChapterRow, CompletenessResponse, EpisodeInfo, QueueResponse, ScriptChapter, ShotKind, ShotRow, StageReadiness, StatusResponse, StoryboardRow,
} from '@/api/types'

/** 派生状态 + 服务端不区分但界面要区分的 `suspicious`。 */
export type AnyKind = ShotKind | 'suspicious'

/** 派生状态的展示信息。★ `stale`（需重渲）是相对 printfilm 多的一档。 */
// 注意：这里必须是 `AnyKind` 而不是 `ShotKind` ——
// KIND_INFO 有 7 档（比服务端三态多出 suspicious），用窄类型会报 TS2353。
export const KIND_INFO: Record<AnyKind, { t: string; tone: 'run' | 'ok' | 'warn' | 'bad' | 'idle'; ck: boolean }> = {
  rendering: { t: '生成中', tone: 'run', ck: false },
  current: { t: '已生成', tone: 'ok', ck: true },
  suspicious: { t: '质检可疑', tone: 'warn', ck: false },
  stale: { t: '需重渲', tone: 'warn', ck: false },
  missing: { t: '待生成', tone: 'idle', ck: false },
  qc_failed: { t: '质检不合格', tone: 'bad', ck: false },
  failed: { t: '失败', tone: 'bad', ck: false },
}

export const FILTERS: Array<[string, string]> = [
  ['all', '全部'], ['missing', '待生成'], ['stale', '需重渲'], ['rendering', '生成中'],
  ['qc_failed', '质检不合格'], ['suspicious', '质检可疑'], ['current', '已生成'],
]

export const STAGES = ['plan', 'chars', 'render', 'qc', 'assemble', 'all'] as const
export const STAGE_CN: Record<string, string> = {
  plan: '拆镜', chars: '定妆', render: '渲染', qc: '质检', assemble: '合成', all: '全链',
}

/** 全局撤销/重做栈的一条记录（U4）。
 *
 *  为什么把逆操作做成**闭包**而不是指望后端多级撤销：后端 `api.undo` 只有一步
 *  （shots/*.json.bak 单级备份，见 taskctl._backup_file），前端必须自己记账才谈得上"栈"。
 *  每个 mutation 落地时把自己的**逆操作**（undo）与**正操作**（redo）一起交进来 ——
 *  字段类编辑（保存/锁定/重写）用编辑接口互逆，天然多级、可重做；
 *  结构类编辑（拆/合/插/删）也用编辑接口互逆（insert 支持显式 id，能把删掉的镜头原样放回）。
 */
export interface EditEntry {
  /** 「保存 1-2-03」这种人话标签，顶栏撤销按钮上会显示 */
  label: string
  undo: () => Promise<unknown>
  /** 可安全重放的正操作；没有它「重做」按钮对这条记录禁用 */
  redo?: () => Promise<unknown>
}

export interface State {
  project: string
  projects: ProjectSummary[]
  shots: ShotRow[]
  counts: { total: number; current: number; stale: number; missing: number; qc_fail: number }
  task: Partial<StatusResponse['task']>
  progress: { stage: string; label: string; done: number; total: number; pct: number; current: string }
  readiness: Record<string, StageReadiness>
  final: { name: string; size: number; mtime: number } | null
  finals: EpisodeInfo[]
  running: boolean
  shotsLoaded: boolean
  filter: string
  q: string
  selected: string
  tab: string
  chars: CharRow[]
  scenes: SceneRow[]
  scenesAssigned: number
  scenesTotal: number
  props: PropRow[]
  propsShotsWith: number
  storyboard: StoryboardRow[]
  storyboardModel: Record<string, unknown> | null
  chapters: ChapterRow[]
  chapter: number | null            // 当前筛选的章号（null = 全部）
  scripts: ScriptChapter[]
  completeness: CompletenessResponse | null
  assets: AssetRow[]
  queue: QueueResponse | null
  qc: Pick<QcResponse, 'results' | 'counts' | 'review' | 'rerender' | 'has_result' | 'generated_at'>
  audit: AuditResponse | null
  logText: string
  busy: boolean
  error: string
  /** 多选集合（批量操作栏的数据源，U1）。与单选 `selected` 是两回事。 */
  checked: string[]
  /** 乐观「生成中」标记：id → 提交渲染的时刻(ms)。见 markRendering / kindOf。 */
  renderHint: Record<string, number>
  /** 全局撤销 / 重做栈（U4） */
  undoStack: EditEntry[]
  redoStack: EditEntry[]
  /** 撤销/重做成功后自增 —— 详情弹层等监听它重载自身内容 */
  historyRev: number
}

export const state = reactive<State>({
  project: '', projects: [], shots: [], counts: { total: 0, current: 0, stale: 0, missing: 0, qc_fail: 0 },
  task: {}, progress: { stage: '', label: '', done: 0, total: 0, pct: 0, current: '' },
  readiness: {}, final: null, finals: [], running: false, shotsLoaded: false,
  filter: 'all', q: '', selected: '', tab: 'log',
  chars: [], scenes: [], scenesAssigned: 0, scenesTotal: 0,
  props: [], propsShotsWith: 0,
  storyboard: [], storyboardModel: null,
  chapters: [], chapter: null, scripts: [], completeness: null, assets: [], queue: null,
  qc: { results: {}, counts: { pass: 0, suspicious: 0 }, review: [], rerender: [], has_result: false, generated_at: 0 },
  audit: null, logText: '', busy: false, error: '',
  checked: [], renderHint: {}, undoStack: [], redoStack: [], historyRev: 0,
})

/* ---------------- 统一错误 / 加载通道（U3 / B6） ----------------
 *
 * 之前 `state.error` 只写不读（grep 可证）—— 网络抖动、首拉失败时界面"看起来没事"。
 * 现在约定：**所有** refresh* 都走 guard()：
 *   · 失败 → 统一回落（保留上一次的数据）+ 写 state.error，由 App.vue 的全局错误条显示；
 *   · 同通道恢复成功 → 自动清掉自己那条错误（否则一次抖动会让错误条挂到天荒地老）。
 */
export function setError(msg: string): void {
  const m = (msg || '').trim()
  if (m && state.error !== m) state.error = m
}

export function clearError(): void {
  state.error = ''
}

export function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

async function guard(channel: string, fn: () => Promise<void>): Promise<void> {
  try {
    await fn()
    // 同通道的旧错误在恢复成功后自清（error 格式固定为「通道：详情」）
    if (state.error.startsWith(`${channel}：`)) state.error = ''
  } catch (e) {
    // 回落 = 上一次的数据原样留在界面上，这里只负责让失败**可见**
    setError(`${channel}：${errText(e)}`)
  }
}

/** 派生镜头档。
 *
 *  **不信任数据库状态字段**，按「任务态 + 质检 + 产物 + 指纹」实时推导。
 *  优先级经过实测校准：`stale` **优先于** `suspicious` ——
 *  产物过期比质检可疑更该先重渲（质检的是旧产物，结论已经不作数了）。
 *
 *  ⚠️ 任务态**只影响它自己那一镜**，绝不共用一个全局 pending ——
 *  ai-drama-generator 就是所有卡片共享一个 `isPending`，点一镜全表转圈。
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
  return KIND_INFO[k as ShotKind] ?? KIND_INFO.missing
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

/** 场景实体：id → SceneRow。用于表格「场景」列与左栏分组。
 *  ⚠️ 与 id 里的场次号（`sceneOf`）**不是一回事**：本片 52 镜被分成 47 个场次号，
 *  那只是编号；场景实体才是"同一个地点"（本片 3 个）。 */
export const sceneById = computed(() => {
  const m: Record<string, SceneRow> = {}
  for (const x of state.scenes) m[x.id] = x
  return m
})

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

export function sceneOf(id: string): string {
  const n = String(id || '').split('-')
  return n.length >= 2 ? `${n[0]}-${n[1]}` : String(id || '')
}

export function fmtMmSs(sec: number): string {
  const s = Math.max(0, Math.round(sec || 0))
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

export function fmtSize(n: number): string {
  if (!n) return '—'
  const u = ['B', 'KB', 'MB', 'GB']
  let i = 0
  let v = n
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++ }
  return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)}${u[i]}`
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
function pruneRenderHints(rows: ShotRow[]): void {
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

/* ---------------- 全局撤销 / 重做（U4） ---------------- */

const HISTORY_CAP = 100

export function pushEdit(entry: EditEntry): void {
  state.undoStack.push(entry)
  if (state.undoStack.length > HISTORY_CAP) state.undoStack.shift()
  // 标准语义：新操作让「重做」失效（重做栈里的操作已经不在"当前"的延长线上）
  state.redoStack = []
}

export const canUndo = computed(() => state.undoStack.length > 0)
export const canRedo = computed(() => state.redoStack.length > 0)
export const undoLabel = computed(() => state.undoStack[state.undoStack.length - 1]?.label || '')
export const redoLabel = computed(() => state.redoStack[state.redoStack.length - 1]?.label || '')

/** 撤销栈顶操作。**永不抛出**（快捷键里是 fire-and-forget 调用）；
 *  逆操作失败要把条目放回栈顶 —— 不放回去这一操作就"既没撤销也找不回"了。 */
export async function undoEdit(): Promise<void> {
  const e = state.undoStack.pop()
  if (!e) {
    ElMessage.info('没有可撤销的操作')
    return
  }
  try {
    await e.undo()
    state.redoStack.push(e)
    state.historyRev++
    ElMessage.success(`已撤销：${e.label}`)
    await refreshAfterEdit()
  } catch (err) {
    state.undoStack.push(e)
    setError(`撤销失败：${errText(err)}`)
    ElMessage.error(`撤销失败：${errText(err)}`)
  }
}

/** 重做栈顶操作。与 undoEdit 对称。 */
export async function redoEdit(): Promise<void> {
  const e = state.redoStack.pop()
  if (!e) {
    ElMessage.info('没有可重做的操作')
    return
  }
  if (!e.redo) {
    state.redoStack.push(e)
    ElMessage.warning(`「${e.label}」没有可安全重放的正操作`)
    return
  }
  try {
    await e.redo()
    state.undoStack.push(e)
    state.historyRev++
    ElMessage.success(`已重做：${e.label}`)
    await refreshAfterEdit()
  } catch (err) {
    state.redoStack.push(e)
    setError(`重做失败：${errText(err)}`)
    ElMessage.error(`重做失败：${errText(err)}`)
  }
}

/** 切项目 / 清空重置时把**会串项目**的瞬态全清掉：
 *  多选（旧 id 对不上新表）、撤销栈（逆操作指向旧项目）、乐观标记、错误条。 */
export function clearTransient(): void {
  state.checked = []
  state.undoStack = []
  state.redoStack = []
  state.renderHint = {}
  state.error = ''
}

/* ---------------- mutation 出口 ---------------- */

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

/** 渲染提交的公共出口（单镜 / 表格按钮 / 弹层 / 快捷键 R 共用）。
 *  提交后**本地先标「生成中」**（markRendering），不用等下一轮轮询。 */
export async function submitRerender(id: string): Promise<void> {
  if (!state.project || !id) return
  const r = await api.run(state.project, 'render', { force: true, only: [id] })
  markRendering([id])
  ElMessage.success(r.message || `已开始重渲 ${id}`)
  window.setTimeout(() => { void refreshStatus() }, 300)
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

/** 写后即刷（U2 / B4）：镜头编辑成功后**立即**刷新镜头表与章计数。
 *  以前拆分/合并/插入/保存只重载弹层自身，App 空闲时不轮询镜头表 →
 *  表格长时间显示旧数据，用户以为没生效。 */
export async function refreshAfterEdit(): Promise<void> {
  await refreshShots()
  await refreshChapters()
}

/* ---------------- 拉取（全部走 guard：失败统一回落 + 界面报错） ---------------- */

export async function loadProjects(): Promise<void> {
  await guard('项目列表', async () => {
    const r = await api.projects()
    state.projects = r.projects
    if (!state.project) state.project = r.default || (r.projects[0]?.name ?? '')
  })
}

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

let logOffset = 0
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

export async function refreshQueue(): Promise<void> {
  if (!state.project) return
  await guard('队列', async () => {
    state.queue = await api.queue(state.project)
  })
}

export async function refreshAssets(): Promise<void> {
  if (!state.project) return
  await guard('素材', async () => {
    const r = await api.assets(state.project)
    state.assets = r.assets || []
  })
}

/** 某个场景/道具的候选（没出过图就返回空数组）。 */
export function assetOf(kind: 'scene' | 'prop', id: string): AssetRow | undefined {
  return state.assets.find((a) => a.kind === kind && a.id === id)
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

/** 镜头属于第几章 —— 从 **id 首段**取（`2-1-03` → 2）。
 *  比读文件名稳：镜头表被手工改名也对得上。 */
export function chapterOf(id: string): number {
  const seg = String(id || '').split('-')[0]
  const n = Number(seg)
  return Number.isFinite(n) && n > 0 ? n : 0
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

/** 全量补刷（任务刚结束 / 清空重置后调一次）。
 *  各 tab 有自己的去重，重复调用很便宜。 */
export async function refreshAll(): Promise<void> {
  await refreshShots()
  await refreshChapters()
  await refreshScenes()
  await refreshScript()
  await refreshStoryboard()
  await refreshAssets()
  await refreshQc()
  await refreshAudit()
  await refreshChars()
}

export { readonly, ApiError }
