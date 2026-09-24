import { computed, reactive, readonly } from 'vue'
import { api, ApiError } from '@/api/client'
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
})

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

/** 置位 E1 标记并就地更新状态（不整表重拉）。 */
export async function setFlag(id: string, flag: 'locked' | 'selected' | 'favorite', value: boolean) {
  state.busy = true
  try {
    const r = await api.lockShot(state.project, id, flag, value)
    const s = state.shots.find((x) => x.id === id)
    if (s && (flag === 'locked' || flag === 'selected')) s[flag] = r[flag]
    return r
  } finally {
    state.busy = false
  }
}

export async function loadProjects() {
  const r = await api.projects()
  state.projects = r.projects
  if (!state.project) state.project = r.default || (r.projects[0]?.name ?? '')
}

export async function refreshStatus(silent = false) {
  if (!state.project) return
  try {
    const st = await api.status(state.project)
    state.task = st.task || {}
    state.progress = st.progress || state.progress
    state.readiness = st.readiness || {}
    state.final = st.final ?? null
    state.finals = st.finals ?? []
    state.running = !!st.running
    if (!silent) state.error = ''
  } catch (e) {
    if (!silent) state.error = (e as Error).message
  }
}

export async function refreshShots(): Promise<void> {
  if (!state.project) return
  const r = await api.shots(state.project)
  state.shots = r.shots
  state.counts = r.counts
  state.shotsLoaded = true
}

let logOffset = 0
export async function pullLog() {
  if (!state.project) return
  const r = await api.log(state.project, logOffset)
  if (r.reset) {
    state.logText = ''
    logOffset = 0
  }
  if (r.text) state.logText += r.text
  logOffset = r.next
  if (state.logText.length > 400_000) state.logText = state.logText.slice(-200_000)
  return r.reset
}

export async function refreshQc() {
  if (!state.project) return
  const r = await api.qc(state.project)
  state.qc = {
    results: r.results, counts: r.counts, review: r.review, rerender: r.rerender,
    has_result: r.has_result, generated_at: r.generated_at,
  }
}

export async function refreshAudit() {
  if (!state.project) return
  state.audit = await api.audit(state.project)
}

export async function refreshChars() {
  if (!state.project) return
  const r = await api.chars(state.project)
  state.chars = r.chars
}

export async function refreshScenes() {
  if (!state.project) return
  const r = await api.scenes(state.project)
  state.scenes = r.scenes
  state.scenesAssigned = r.assigned
  state.scenesTotal = r.total_shots
}

export async function refreshProps() {
  if (!state.project) return
  const r = await api.props(state.project)
  state.props = r.props
  state.propsShotsWith = r.shots_with_props ?? 0
}

export async function refreshQueue() {
  if (!state.project) return
  try {
    state.queue = await api.queue(state.project)
  } catch {
    state.queue = null
  }
}

export async function refreshAssets() {
  if (!state.project) return
  try {
    const r = await api.assets(state.project)
    state.assets = r.assets || []
  } catch {
    state.assets = []
  }
}

/** 某个场景/道具的候选（没出过图就返回空数组）。 */
export function assetOf(kind: 'scene' | 'prop', id: string): AssetRow | undefined {
  return state.assets.find((a) => a.kind === kind && a.id === id)
}

export async function refreshCompleteness() {
  if (!state.project) return
  try {
    state.completeness = await api.completeness(state.project)
  } catch {
    state.completeness = null
  }
}

export async function refreshScript() {
  if (!state.project) return
  try {
    const r = await api.script(state.project)
    state.scripts = r.chapters || []
  } catch {
    state.scripts = []
  }
}

export async function refreshChapters() {
  if (!state.project) return
  const r = await api.chapters(state.project)
  state.chapters = r.chapters || []
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

export async function refreshStoryboard() {
  if (!state.project) return
  const r = await api.storyboard(state.project)
  state.storyboard = r.shots || []
  state.storyboardModel = r.model || null
}

export { readonly, ApiError }
