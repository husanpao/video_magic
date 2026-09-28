import { reactive } from 'vue'
import type {
  AuditResponse, CharRow, ProjectSummary, PropRow, QcResponse, SceneRow,
  AssetRow, ChapterRow, CompletenessResponse, EpisodeInfo, QueueResponse,
  ScriptChapter, ShotRow, StageReadiness, StatusResponse, StoryboardRow,
} from '@/api/types'
import type { AnyKind, EditEntry } from './types'

/**
 * 全局状态的**唯一容器**（各域模块共用）。
 *
 * 为什么是一个对象而不是六个 store：本仓库已写明的约定是「模块级 reactive，不引 pinia」
 * （见 `stores/config.ts` 的文件头）。`package.json` 里的 pinia 至今没装进 `main.ts`，
 * 那是决定不是遗漏 —— 换状态框架属于跨 `app.ts`/`ui.ts`/`config.ts` 三处的统一决策，
 * 不该在一次"拆文件"的重构里被顺带改掉。
 *
 * 所以这里的分域方式是把**行为**（refresh / mutation / 派生）按域切开，
 * 状态形状一个字都不动。收益是"改任何一屏都要路过别人的状态"变成
 * "改镜头表只读 `shots.ts`"；而 reactivity 语义与拆之前**逐字节相同**
 * —— 这是本项重构唯一的验收标准（视觉零变化 + `ui-e2e.mjs` 97✅）。
 */
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

/**
 * 状态的初始值。**必须是函数**而不是常量 ——
 * 里面的对象/数组是引用，常量复用会让"重置"和上一个项目共享同一个数组。
 */
export function initialState(): State {
  return {
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
  }
}

export const state = reactive<State>(initialState())

/**
 * 切项目时**必须清掉的**字段（项目作用域）。
 *
 * ★ 为什么不直接写一串 `state.xxx = []` 在 App.vue 里：
 *   原来就是这么写的，漏了 `finals` —— 于是切到一个没有成片的新项目时，
 *   FinalTab 还挂着**上一个项目的集数列表**，拿 `EP02` 去新项目要文件，
 *   直接 404（P1 的新 E2E 用例把它照出来了）。同理漏过的还有
 *   `completeness` / `storyboardModel` / `scenesAssigned` / `propsShotsWith`。
 *   现在从 `initialState()` 取初值：**新增字段不可能被忘记重置**，
 *   因为它只有一个来源。
 */
export const PROJECT_SCOPED: Array<keyof State> = [
  'shots', 'counts', 'shotsLoaded', 'selected', 'logText',
  'chapters', 'chapter', 'scripts', 'scenes', 'scenesAssigned', 'scenesTotal',
  'props', 'propsShotsWith', 'storyboard', 'storyboardModel', 'assets', 'chars',
  'qc', 'audit', 'completeness', 'final', 'finals',
]

/** 就地把所有项目作用域字段复位成初值（不 await、不拉网络）。 */
export function resetProjectScoped(): void {
  const init = initialState()
  for (const k of PROJECT_SCOPED) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (state as any)[k] = (init as any)[k]
  }
}

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

export async function guard(channel: string, fn: () => Promise<void>): Promise<void> {
  try {
    await fn()
    // 同通道的旧错误在恢复成功后自清（error 格式固定为「通道：详情」）
    if (state.error.startsWith(`${channel}：`)) state.error = ''
  } catch (e) {
    // 回落 = 上一次的数据原样留在界面上，这里只负责让失败**可见**
    setError(`${channel}：${errText(e)}`)
  }
}

export type { AnyKind, EditEntry }
