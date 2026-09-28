// 全部类型从**真实 API 响应**提取（不是猜的），见 /tmp/apishapes/*.json。
// 这是引入 TS 的主要收益：这几轮踩的坑（字段改名、字段没返回、flex 算错）
// 有一半以上是类型检查能直接挡住的。

/** 镜头派生状态。**不读数据库状态字段**，按「产物 + 指纹 + 质检 + 任务态」推导。
 *  ★ `stale`（需重渲）是相对 printfilm 多的一档 —— 它的 shotDisplayKind() 没有这档。 */
export type ShotKind = 'missing' | 'rendering' | 'stale' | 'current' | 'qc_failed' | 'failed'

/** 服务端给的三态（指纹判定），比前端派生档少。 */
export type ShotStatus = 'current' | 'stale' | 'missing'

export interface ClipInfo {
  exists: boolean
  size: number
  mtime: number
}

export interface QcResult {
  ok: boolean
  verdict: 'pass' | 'suspicious' | 'fail' | 'error'
  issues: string[]
  metrics: Record<string, number | string>
}

/** GET /api/shots 的一行 */
export interface ShotRow {
  id: string
  sec: number
  chars: string[]
  seed: number
  shot_size: string
  camera: string
  dialogue: string
  narration: string
  costume: Record<string, string>
  prompt_head: string
  prompt_len: number
  frames: number
  status: ShotStatus
  fp: string
  fp_source: string
  clip: ClipInfo
  qc: QcResult | null
  locked: boolean
  selected: boolean
  sec_actual: number | null
  sec_override: number | null
  sec_source: 'sec_actual' | 'planned'
  /** 场景实体 id（如 S1）。**不是 id 里的场次号** —— 那个只是编号。 */
  scene_id: string
}

export interface ShotCounts {
  total: number
  current: number
  stale: number
  missing: number
  qc_fail: number
}

export interface ShotsResponse {
  ok: boolean
  project: string
  source: string
  shots: ShotRow[]
  counts: ShotCounts
  issues: string[]
  shots_dir: string
}

export interface SectionMap {
  subject_definitions?: string
  summary?: string
  retention_analysis?: string
  detailed_description?: string
  overall_soundscape?: string
  non_diegetic_music?: string
  [k: string]: string | undefined
}

export interface CostumeVariant {
  id: string
  label?: string
  prompt?: string
}

/** GET /api/shot 的 detail（注意：字段比 /api/shots 多得多） */
export interface ShotDetail {
  id: string
  sec: number
  chars: string[]
  seed: number
  shot_size: string
  camera: string
  dialogue: string
  narration: string
  action: string
  costume: Record<string, string>
  costume_options: Record<string, CostumeVariant[]>
  costume_configured: boolean
  prompt: string
  sections: SectionMap
  section_order: string[]
  missing_sections: string[]
  frames: number
  status: ShotStatus
  fp: string
  fp_source: string
  clip: ClipInfo
  qc: QcResult | null
  /** E1 资产锁定：三个**独立**布尔位，不是互斥状态 */
  locked: boolean
  locked_at: number
  locked_by: string
  selected: boolean
  favorite: boolean
  file: string
  index: number
  index_in_file: number
  prev_id: string
  next_id: string
  speech_chars: number
  seconds_needed: number
  limits: {
    dialogue_max: number
    narration_max: number
    sec_min: number
    sec_max: number
    chars_per_sec: number
  }
  prompt_len: number
  prompt_mismatch: string[]
  extra_keys: string[]
  thumb_url: string
  play_url: string
}

export interface ShotDetailResponse {
  ok: boolean
  project: string
  shot: ShotDetail
  total: number
  last_edit: null | { action?: string; shot?: string }
}

export interface SceneRow {
  id: string
  name: string
  location: string
  time_of_day: string
  lighting: string
  atmosphere: string
  /** ★ 这段英文会被**逐字注入**该场景每一镜的 detailed_description（跨镜一致性锚点） */
  description: string
  shot_count: number
}

export interface ScenesResponse {
  ok: boolean
  project: string
  scenes: SceneRow[]
  assigned: number
  total_shots: number
  file: string
}

export interface PropRow {
  id: string
  name: string
  owner: string
  description: string
  /** true = 正文没给外观，是保守推断的通用形象，需人工复核 */
  inferred: boolean
  shot_count: number
}

export interface PropsResponse {
  ok: boolean
  project: string
  props: PropRow[]
  assigned: number
  total_shots: number
  shots_with_props: number
  file: string
}

/** 分镜图（渲染前审片的静态关键帧）。由 Qwen-Image 文生图产出。
 *  ⚠️ 已知局限：分镜图是**纯文生图**，而视频是**参考图条件生成**，
 *  所以它们能对上"谁/在干什么/什么场景"，**对不上"从哪个机位、什么光"**。 */
export interface StoryboardRow {
  id: string
  status: 'current' | 'stale' | 'missing' | 'bad-prompt' | string
  file?: string | null
  seconds?: number
  width?: number
  height?: number
  size?: number
  seed?: number
  prompt_id?: string
  note?: string
  at?: number
}

export interface StoryboardResponse {
  ok: boolean
  project: string
  shots: StoryboardRow[]
  counts: Record<string, number>
  model?: { unet: string; clip: string; vae: string; width: number; height: number; steps: number; cfg: number }
  url_base?: string
  dir?: string
  error?: string
}

/** 剧本层（无损结构化视图）。每句对白都有 src_span 指回小说原文，可逐字校验。 */
export interface ScriptChapter {
  no: number
  title: string
  has_script: boolean
  beats: number
  lines: number
  checks: {
    checked?: number; exact_ok?: number; normalized_ok?: number
    span_drift?: number; failed?: number; coverage_pct?: number
    gate_passed?: boolean; source_quotes?: number
    failures?: Array<{ beat: string; quote: string; why: string }>
  }
  text: string
  file?: string
}

export interface ScriptResponse {
  ok: boolean
  project: string
  chapters: ScriptChapter[]
  dir: string
  gate_passed: boolean
}

/** B3 完备性矩阵：每镜每维度的状态。 */
export interface CompletenessCell { state: 'ok' | 'stale' | 'draft' | 'missing'; tone: string; detail: string }
export interface CompletenessDim { key: string; label: string; phase: string; blocks_render: boolean }
export interface CompletenessRow {
  id: string; scene_id: string; chapter: number
  cells: Record<string, CompletenessCell>
  blockers: string[]
}
export interface CompletenessResponse {
  ok: boolean
  project: string
  dimensions: CompletenessDim[]
  shots: CompletenessRow[]
  summary: {
    shots: number
    per_dimension: Record<string, Record<string, number>>
    blocked_shots: number
    ready_to_render: number
  }
  blockers: string[]
  notes: string[]
  text: string
}

export interface ChapterRow {
  index: number
  no: number
  title: string
  novel: string
  novel_chars: number
  shots_file: string
  has_shots: boolean
  shots: number
  /** P1：正文比镜头表新 → 这一章需要重拆。detail=0 时字段不存在，故可选。 */
  needs_replan?: boolean
  needs_sync?: boolean
}

export interface ChaptersResponse {
  ok: boolean
  project: string
  chapters: ChapterRow[]
  total_chapters: number
  total_shots: number
  shot_files: string[]
  /* ↓ P1 章节管理的全景字段。`/api/chapters?detail=0` 会退回没有这些字段的旧形状，
     所以它们都是**可选** —— 界面必须能区分"没有这个字段"与"字段为 0/假"。 */
  has_manifest?: boolean
  conflicts?: string[]
  changed_chapters?: number[]
  meta?: ProjectMeta
  replanned_count?: number
  next_step?: string
}

/** 项目元信息（P1）。`_layer: file|derived` 说明它是真存了还是从现状反推的。 */
export interface ProjectMeta {
  name: string
  title: string
  source_type: 'novel' | 'script'
  logline: string
  style_preset: 'auto' | 'realistic' | 'cg' | 'anime'
  created_at?: number
  _layer?: 'file' | 'derived'
}

export interface ChapterDetail {
  ok: boolean
  no: number
  title: string
  file: string
  text: string
  chars: number
}

/** 章节写操作的公共回执：拿到最新的整份清单，界面不必再单独刷一次。 */
export interface ChapterMutationResult {
  ok: boolean
  message: string
  hint?: string
  no?: number
  title?: string
  file?: string
  added?: boolean
  changed?: boolean
  chars?: number
  chapters?: ChapterRow[]
  imported?: Array<{ no: number; title: string; file: string; chars: number }>
  skipped?: Array<{ filename: string; reason: string }>
  moved?: string[]
}

export interface CreateProjectResult {
  ok: boolean
  project: string
  path: string
  meta: ProjectMeta
  message: string
  next?: string
}

/** 项目删除是移进 projects/_trash/，不是 rm。 */
export interface ProjectDeleteResult {
  ok: boolean
  project: string
  entry: string
  trash: string
  files: number
  size: number
  message: string
  hint: string
}

export interface ProjectTrashItem {
  entry: string
  project: string
  mtime: number
  files: number
  size: number
  chapters: number
  has_novel: boolean
}

export interface ProjectTrashResponse {
  ok: boolean
  root: string
  items: ProjectTrashItem[]
  message: string
}

/** W2 风格屏的数据形状（对应后台 register.summary / style_impact）。 */
export interface StyleInfo {
  ok?: boolean
  project?: string
  preset: string
  sentence: string
  source: string
  confirmed: boolean
  reason: string
  confidence: number | null
  book_chars?: number
  sampled_chapters?: number[]
  presets: Array<{ key: string; label: string; sentence: string; on: boolean }>
  note: string
}

export interface StyleImpact {
  ok?: boolean
  from: { preset: string; sentence: string; source: string }
  to: { preset: string; sentence: string }
  changed: boolean
  images: {
    portraits: { total: number; stale: number; untracked: number; never: number; names: string[] }
    assets: { total: number; stale: number; untracked: number; keys: string[] }
    note: string
  }
  shots: {
    total: number; with_old_style: number; auto_stale: number
    requires: string; note: string
  }
}

/** 实体总表的一行（角色 / 场景 / 道具共用一个形状，按 kind 分叉字段）。 */
export interface EntityRow {
  kind: 'char' | 'scene' | 'prop'
  id: string
  name: string
  /** 出现在哪些章。空数组 + chapters_known=false 才是"未知" —— v1 老项目没登记字段，后端从镜头表反推 */
  chapters: number[]
  shot_count: number
  has_prompt?: boolean
  portrait_variants?: Array<{ path: string; chapter: number | null; kind: string }>
  portrait_chapters?: number[]
  costume_variants?: string[]
  location?: string
  time_of_day?: string
  lighting?: string
  owner?: string
  inferred?: boolean
}

export interface EntitiesResponse {
  ok: boolean
  project: string
  entities: EntityRow[]
  totals: { char: number; scene: number; prop: number }
  suspect_merges: string[]
  never_used: string[]
}

export interface ChapterTrash {
  ok: boolean
  project: string
  items: Array<{ path: string; size: number; mtime: number }>
  message: string
}

export interface EpisodeInfo {
  name: string
  stem: string
  size: number
  mtime: number
  srt: string
  url: string
}

export interface ResetTarget { path: string; kind: string; files: number; bytes: number }
export interface ResetPreview {
  ok: boolean
  project: string
  scope: 'clips' | 'shots' | 'all'
  label: string
  targets: ResetTarget[]
  total_bytes: number
  keep: { path: string; bytes: number }[]
  requires_confirm: string
}

export interface QueueJob {
  id: string
  kind: string
  label: string
  args: Record<string, unknown>
  status: 'pending' | 'running' | 'done' | 'failed' | 'canceled'
  created_at: number
  started_at: number
  finished_at: number
  note: string
}
export interface QueueResponse {
  ok: boolean
  project: string
  total: number
  pending: number
  running: number
  done: number
  failed: number
  canceled: number
  next_label: string
  jobs: QueueJob[]
}

export interface AssetCandidate {
  seed: number; file: string; mtime: number; size: number; exists: boolean; url: string
}
export interface AssetRow {
  kind: 'scene' | 'prop'
  id: string
  name: string
  has_definition: boolean
  prompt: string
  adopted: string
  candidates: AssetCandidate[]
  n: number
}
export interface AssetsResponse {
  ok: boolean
  project: string
  assets: AssetRow[]
  scenes: AssetRow[]
  props: AssetRow[]
  dir: string
}

export interface QcResponse {
  ok: boolean
  project: string
  generated_at: number
  results: Record<string, QcResult>
  rerender: string[]
  review: string[]
  counts: { pass: number; suspicious: number }
  has_result: boolean
  play_url_base: string
}

export interface AuditFinding {
  code: string
  severity: 'error' | 'warning' | 'info'
  module: string
  from_shot: number | null
  to_shot: number | null
  /** 解析后的镜头 id（from_shot 是**表内 1-based 序号**，脆弱的，用 id 更稳） */
  from_id: string | null
  to_id: string | null
  shot_ids: string[]
  project_level: boolean
  chars: string[]
  message: string
  rewrite_hint: string
}

export interface AuditResponse {
  ok: boolean
  project: string
  has_audit: boolean
  generated_at: string
  summary: {
    findings: { error: number; warning: number; info: number }
    total_findings: number
    gate_passed: boolean
    errors: string[]
    warnings: string[]
  }
  findings: AuditFinding[]
  limitations: string[]
  shot_order: string[]
}

/** 候选定妆图。字段从真实 `/api/chars` 响应对齐（不是 `[k:string]: unknown` 那种松类型 ——
 *  松类型挡不住"从错误的对象上取 file"这类 bug，CharsTab 就踩过一次）。 */
export interface CharCandidate {
  file: string
  seed: number
  size: number
  mtime: number
  adopted: boolean
  url: string
  thumb_url: string
}

export interface CharRow {
  name: string
  prompt: string
  prompt_warnings: string[]
  ref_mtime: number
  ref_size: number
  ref_synced: boolean
  candidates: CharCandidate[]
  gacha_raw: number
  ref_url: string
  sheet_url: string
}

export interface CharsResponse {
  ok: boolean
  project: string
  chars: CharRow[]
}

export interface StageReadiness {
  stage: string
  ready: boolean
  missing: string[]
  halffinished: string[]
  message: string
}

export interface TaskState {
  project: string
  running: boolean
  stage: string
  shot: string
  pid: number
  started_at: number
  stopped: boolean
  finished_at: number
  exit_code: number
  only: string[]
  count: number
  force: boolean
  dry: boolean
  error: string
  elapsed: number
}

export interface StatusResponse {
  ok: boolean
  project: string
  path: string
  task: TaskState
  running: boolean
  stage: string
  shot: string
  progress: { stage: string; label: string; done: number; total: number; pct: number; current: string }
  readiness: Record<string, StageReadiness>
  final: { name: string; size: number; mtime: number } | null
  finals?: EpisodeInfo[]
  episode_count?: number
  log_size: number
  log_tail: string[]
  now: number
}

export interface ProjectSummary {
  name: string
  path: string
  has_project_json: boolean
  novel_chapters: number
  shot_files: number
  clips: number
  refs: number
  char_prompts: number
  final: { name: string; size: number; mtime: number } | null
  finals?: EpisodeInfo[]
  running: boolean
  task_stage: string
  progress: { stage: string; label: string; done: number; total: number; pct: number }
  /** P1：随列表一起带回，省掉前端 N+1 次 /api/project/meta */
  meta?: ProjectMeta
}

export interface ProjectsResponse {
  ok: boolean
  root: string
  default: string
  projects: ProjectSummary[]
}

export interface LogResponse {
  ok: boolean
  offset: number
  next: number
  text: string
  size: number
  reset: boolean
}

export type Stage = 'plan' | 'chars' | 'render' | 'qc' | 'assemble' | 'all'
