import type {
  AuditResponse, CharsResponse, LogResponse, ProjectSummary, ProjectsResponse,
  PropsResponse, QcResponse, ScenesResponse, ShotDetailResponse, ShotsResponse,
  AssetsResponse, ChaptersResponse, ChapterDetail, ChapterMutationResult, ChapterTrash,
  CompletenessResponse, CreateProjectResult, ProjectMeta, ProjectDeleteResult, ProjectTrashResponse, QueueResponse, ResetPreview,
  EntitiesResponse, ScriptResponse, Stage, StatusResponse, StoryboardResponse, StyleImpact, StyleInfo,
} from './types'

/**
 * 类型化的 API 客户端。
 *
 * 约定（沿用后端 vm/web.py）：
 * - 成功一律 `{ ok: true, ... }`；失败 `{ ok: false, error, message }` + 4xx/5xx
 * - 错误消息**直接用后端的中文 `message`** —— 后端已经写成给人看的了，前端不要再包一层
 */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly code: string,
    readonly status: number,
    /** 后端的**结构化载荷**。E3 的"暂停等人批"靠它把
     *  已花多少 / 卡在哪步 / 再放行多少 直接摆到界面上（而不是让人去翻日志）。 */
    readonly payload: Record<string, unknown> = {},
  ) {
    super(message)
    this.name = 'ApiError'
  }

  /** E3：这一步超预算、需要人批准才能继续。 */
  get needsApproval(): boolean {
    return this.code === 'needs_approval' || this.payload.needs_approval === true
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(path, init)
  } catch (e) {
    throw new ApiError(`连不上服务（${path}）：${(e as Error).message}`, 'network', 0)
  }
  const text = await res.text()
  let body: unknown = null
  try {
    body = text ? JSON.parse(text) : null
  } catch {
    throw new ApiError(`服务返回的不是 JSON（HTTP ${res.status}）：${text.slice(0, 200)}`, 'bad_json', res.status)
  }
  if (!res.ok) {
    const b = body as (Record<string, unknown> & { message?: string; error?: string }) | null
    throw new ApiError(
      b?.message || `HTTP ${res.status}`,
      b?.error || 'http_error',
      res.status,
      b || {},
    )
  }
  return body as T
}

function q(params: Record<string, string | number | undefined>): string {
  const sp = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== '') sp.set(k, String(v))
  const s = sp.toString()
  return s ? `?${s}` : ''
}

function post<T>(path: string, data: Record<string, unknown>): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  })
}

export interface RunResult {
  ok: boolean
  message?: string
  pid?: number
  stage?: string
}

export interface LockResult {
  ok: boolean
  shot: string
  locked: boolean
  locked_at: number
  locked_by: string
  selected: boolean
  favorite: boolean
  message: string
}

export interface SimpleResult {
  ok: boolean
  message?: string
  [k: string]: unknown
}

export const api = {
  projects: () => request<ProjectsResponse>('/api/projects'),

  status: (project: string, tail = 60) =>
    request<StatusResponse>(`/api/status${q({ project, tail })}`),

  shots: (project: string) => request<ShotsResponse>(`/api/shots${q({ project })}`),

  shot: (project: string, id: string) =>
    request<ShotDetailResponse>(`/api/shot${q({ project, id })}`),

  updateShot: (project: string, id: string, patch: Record<string, unknown>) =>
    post<SimpleResult>('/api/shot/update', { project, id, patch }),

  /** E1 资产锁定。flag ∈ locked | selected | favorite（三个独立位，可同时为真） */
  lockShot: (project: string, id: string, flag: 'locked' | 'selected' | 'favorite', value: boolean) =>
    post<LockResult>('/api/shot/lock', { project, id, flag, value }),

  rewriteShot: (project: string, id: string, feedback = '') =>
    post<SimpleResult>('/api/shot/rewrite', { project, id, feedback }),

  splitShot: (project: string, id: string, at?: number) =>
    post<SimpleResult>('/api/shot/split', { project, id, ...(at === undefined ? {} : { at }) }),

  mergeShot: (project: string, id: string) =>
    post<SimpleResult>('/api/shot/merge', { project, id }),

  /** 在 `afterId` 之后插入一镜。
   *  ★ 契约修正（T5）：后端 `/api/shot/insert` 的 `after` 是**锚点镜头号**（在哪一镜之后插），
   *  不是"插前/插后"的布尔 —— 旧签名 `after = true` 会把布尔值当镜头号发出去
   *  （后端 `str(True)` → 找不到镜「True」），「插入新镜」实际是坏的。
   *  `shot` 可带完整字段（支持**显式 id**）—— 删除/合并的撤销就靠它把镜头原样放回原位。 */
  insertShot: (project: string, afterId: string, shot?: Record<string, unknown>) =>
    post<SimpleResult>('/api/shot/insert', { project, after: afterId, ...(shot ? { shot } : {}) }),

  deleteShot: (project: string, id: string) =>
    post<SimpleResult>('/api/shot/delete', { project, id }),

  undo: (project: string) => post<SimpleResult>('/api/shot/undo', { project }),

  bulk: (project: string, payload: Record<string, unknown>) =>
    post<SimpleResult>('/api/shots/bulk', { project, ...payload }),

  /** 重渲**单个**镜头。后端 `/api/rerender` 只收 `{shot_id}`（单镜），
   *  内部等价于 `_start({stage:'render'}, only=[shot], force=True)`。 */
  rerender: (project: string, shotId: string) =>
    post<RunResult>('/api/rerender', { project, shot_id: shotId }),

  /** 批量重渲：走 `/api/run` 的 `only` 列表（一个任务覆盖多镜，比 N 次单镜更省）。
   *  `only` 后端支持数组或逗号分隔字符串。 */
  rerenderBulk: (project: string, ids: string[]) =>
    post<RunResult>('/api/run', { project, stage: 'render', force: true, only: ids }),

  /** 启动一个阶段。
   *  `chapter`（仅 plan 有意义）= **只拆那一章**：P0.3 之前 `--chapter` 是死参数，
   *  改了或新加一章要把全部章节重跑一遍 LLM。现在前端要能把这个能力点出来，
   *  否则后台修好了也等于没修。 */
  run: (project: string, stage: Stage, opts: { force?: boolean; dry?: boolean; only?: string[]; chapter?: number } = {}) =>
    post<RunResult>('/api/run', { project, stage, ...opts }),

  stop: (project: string) => post<SimpleResult>('/api/stop', { project }),

  /** E3：放行**一次**（下一个任务消费后即失效）。 */
  approve: (project: string, stage: string) =>
    post<SimpleResult>('/api/approve', { project, stage }),

  budget: (project: string, stage = 'all') =>
    request<{ ok: boolean; est: Record<string, number>; check: Record<string, unknown> }>(
      `/api/budget${q({ project, stage })}`,
    ),

  qc: (project: string) => request<QcResponse>(`/api/qc${q({ project })}`),
  audit: (project: string) => request<AuditResponse>(`/api/audit${q({ project })}`),
  chars: (project: string) => request<CharsResponse>(`/api/chars${q({ project })}`),
  scenes: (project: string) => request<ScenesResponse>(`/api/scenes${q({ project })}`),
  props: (project: string) => request<PropsResponse>(`/api/props${q({ project })}`),
  /** 清空预览：**先摆清单再问确定**。 */
  resetPreview: (project: string, scope: string) =>
    request<ResetPreview>(`/api/reset/preview${q({ project, scope })}`),
  /** 清空项目产物（不可逆 —— confirm 必须等于项目名）。 */
  reset: (project: string, scope: string, confirm: string) =>
    post<SimpleResult>('/api/reset', { project, scope, confirm }),

  assets: (project: string) => request<AssetsResponse>(`/api/assets${q({ project })}`),
  /** 改场景实体（description 决定出图提示词）。 */
  sceneUpdate: (project: string, id: string, patch: Record<string, unknown>) =>
    post<SimpleResult>('/api/scene/update', { project, id, patch }),
  propUpdate: (project: string, id: string, patch: Record<string, unknown>) =>
    post<SimpleResult>('/api/prop/update', { project, id, patch }),
  /** 保存角色定妆提示词（决定角色抽卡出什么）。 */
  charPrompt: (project: string, name: string, text: string) =>
    post<SimpleResult>('/api/chars/prompt', { project, name, text }),
  /** ★ 入队即返回（不再同步等出图）—— 用户可以连点十几个然后走开。 */
  queueAdd: (project: string, kind: string, args: Record<string, unknown>) =>
    post<SimpleResult>('/api/queue/add', { project, kind, args }),
  queue: (project: string) => request<QueueResponse>(`/api/queue${q({ project })}`),
  queueCancel: (project: string, id?: string) =>
    post<SimpleResult>('/api/queue/cancel', { project, id }),
  queueClear: (project: string) => post<SimpleResult>('/api/queue/clear', { project }),
  assetAdopt: (project: string, kind: string, id: string, file: string) =>
    post<SimpleResult>('/api/asset/adopt', { project, kind, id, file }),
  /** 上传自有图作为候选（base64）。与抽卡候选同等待遇，可采纳。 */
  assetUpload: (project: string, kind: string, id: string, filename: string, b64: string) =>
    post<SimpleResult>('/api/asset/upload', { project, kind, id, filename, b64 }),
  /** 角色定妆照：上传自有图（后端接口早就有，前端一直没接）。 */
  charUpload: (project: string, name: string, filename: string, b64: string) =>
    post<SimpleResult>('/api/chars/upload', { project, name, filename, b64 }),
  storyboard: (project: string) => request<StoryboardResponse>(`/api/storyboard${q({ project })}`),
  script: (project: string) => request<ScriptResponse>(`/api/script${q({ project })}`),
  completeness: (project: string) => request<CompletenessResponse>(`/api/completeness${q({ project })}`),

  log: (project: string, offset: number) =>
    request<LogResponse>(`/api/log${q({ project, offset })}`),

  gacha: (project: string, name: string, n = 2) =>
    post<RunResult>('/api/chars/gacha', { project, name, n }),
  adopt: (project: string, name: string, file: string) =>
    post<SimpleResult>('/api/chars/adopt', { project, name, file }),

  /* ── P1 前门：项目与章节管理 ─────────────────────────────────────────
     业务全在后台 `vm/proj.py`，这里只是薄薄一层调用（CLI/Web 同一套控制层）。 */
  projectMeta: (project: string) =>
    request<ProjectMeta>(`/api/project/meta${q({ project })}`),
  createProject: (payload: { name: string; title?: string; source_type?: string;
                             logline?: string; style_preset?: string }) =>
    post<CreateProjectResult>('/api/project/create', payload),
  updateProject: (project: string, patch: Partial<ProjectMeta>) =>
    post<{ ok: boolean; changed: string[]; meta: ProjectMeta; message: string }>(
      '/api/project/update', { project, patch }),

  /**
   * 删除项目 = **移进 projects/_trash/**，不是 rm。
   * `confirm` 必须逐字等于项目名（后端还会再校验一遍，这里只是把话带到界面上）。
   */
  deleteProject: (project: string, confirm: string) =>
    post<ProjectDeleteResult>('/api/project/delete', { project, confirm }),
  /** 回收站里有哪些被删掉的项目（可逆性要能被看见） */
  projectTrash: () => request<ProjectTrashResponse>('/api/project/trash'),
  /** 放回一个项目。重名会被后端拒绝（不覆盖用户内容）。 */
  restoreProject: (entry: string) =>
    post<{ ok: boolean; project: string; path: string; message: string }>(
      '/api/project/restore', { entry }),

  /** 章节清单。`detail=0` 退回旧的精简形状（界面默认要 detail，才有 needs_replan/next_step）。 */
  chapters: (project: string, detail = true) =>
    request<ChaptersResponse>(`/api/chapters${q({ project, detail: detail ? undefined : '0' })}`),
  chapter: (project: string, no: number) =>
    request<ChapterDetail>(`/api/chapter${q({ project, no })}`),
  /** 新建（不传 no）或改写（传 no）一章正文。 */
  chapterSave: (project: string, payload: { no?: number; title?: string; text: string }) =>
    post<ChapterMutationResult>('/api/chapter/save', { project, ...payload }),
  chapterImport: (project: string, files: Array<{ filename: string; text: string; overwrite?: boolean }>) =>
    post<ChapterMutationResult>('/api/chapter/import', { project, files }),
  /** 删除是**可逆**的：正文与镜头表进 `novel/_trash/`，不真删。 */
  chapterDelete: (project: string, no: number) =>
    post<ChapterMutationResult>('/api/chapter/delete', { project, no }),
  chapterRename: (project: string, no: number, title: string) =>
    post<ChapterMutationResult>('/api/chapter/rename', { project, no, title }),
  /** 重排只动顺序、不动章号（章号是镜头 id 前缀与成片集数的主键）。 */
  chapterReorder: (project: string, order: number[]) =>
    post<ChapterMutationResult>('/api/chapter/reorder', { project, order }),
  chapterTrash: (project: string) =>
    request<ChapterTrash>(`/api/chapter/trash${q({ project })}`),

  /* ── P2 风格层 ─────────────────────────────────────────────────────── */
  style: (project: string) => request<StyleInfo>(`/api/style${q({ project })}`),
  /** 影响清单：不带参数=按现状；带 preset/sentence=「改成这样会怎样」 */
  styleImpact: (project: string, to: { preset?: string; sentence?: string } = {}) =>
    request<StyleImpact>(`/api/style/impact${q({ project, ...to })}`),
  styleSet: (project: string, payload: { preset?: string; sentence?: string }) =>
    post<{ ok: boolean; style: StyleInfo; impact: StyleImpact; message: string }>(
      '/api/style/set', { project, ...payload }),
  /** 让 LLM 读整本重推。已人工确认时要 force 才会覆盖。 */
  styleRecommend: (project: string, force = false) =>
    post<{ ok: boolean; style: StyleInfo; skipped: boolean; message: string }>(
      '/api/style/recommend', { project, force }),
  /** 按新风格重出受影响的图（入队；不重跑拆镜、不触发镜头重渲） */
  styleRegen: (project: string, opts: { what?: string[]; n?: number } = {}) =>
    post<{ ok: boolean; queued: number; message: string }>(
      '/api/style/regen', { project, ...opts }),

  /* ── P3 实体总表 ───────────────────────────────────────────────────── */
  entities: (project: string) => request<EntitiesResponse>(`/api/entities${q({ project })}`),
  entitiesChapter: (project: string, no: number) =>
    request<{ ok: boolean; chapter: number; shots: number; characters: string[];
              scenes: Array<{ id: string; name: string }>;
              props: Array<{ id: string; name: string }>; }>(
      `/api/entities/chapter${q({ project, no })}`),
  /** 合并场景：**同时改镜头表里的 scene_id 外键**，受影响镜头会变 stale */
  entityMerge: (project: string, payload: { keep: string; drop: string }) =>
    post<{ ok: boolean; kept: string; dropped: string; repointed_shots: number;
           message: string; hint: string; backup: string }>(
      '/api/entities/merge', { project, ...payload }),
  /** 出某角色的**章节专属定妆照**（入队；该章须已登记造型变体） */
  entityPortrait: (project: string, payload: { name: string; chapter: number; n?: number }) =>
    post<{ ok: boolean; queued: number; job_id: string; product: string; message: string }>(
      '/api/entities/portrait', { project, ...payload }),

  thumbUrl: (project: string, shotId: string, mtime = 0) =>
    `/view${q({ project, shot: shotId, thumb: 1, t: mtime })}`,
  /** 分镜图（kind=storyboard）。t 用产物时间戳破缓存。 */
  storyboardUrl: (project: string, shotId: string, t = 0) =>
    `/view${q({ project, kind: 'storyboard', name: shotId, t })}`,
  playUrl: (project: string, shotId: string) => `/view${q({ project, shot: shotId })}`,
  /** 成片播放地址。**必须能指定集** —— 逐章出集后有两集，只给 EP01 会让 EP02 永远播不到。 */
  finalUrl: (project: string, episode = 'EP01') =>
    `/view${q({ project, final: 1, episode })}`,
  finalDownloadUrl: (project: string, episode = 'EP01') =>
    `/view${q({ project, final: 1, download: 1, episode })}`,
}

export type { ProjectSummary }
