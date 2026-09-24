import type {
  AuditResponse, CharsResponse, LogResponse, ProjectSummary, ProjectsResponse,
  PropsResponse, QcResponse, ScenesResponse, ShotDetailResponse, ShotsResponse,
  AssetsResponse, ChaptersResponse, CompletenessResponse, QueueResponse, ResetPreview,
  ScriptResponse, Stage, StatusResponse, StoryboardResponse,
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

  insertShot: (project: string, id: string, after = true) =>
    post<SimpleResult>('/api/shot/insert', { project, id, after }),

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

  run: (project: string, stage: Stage, opts: { force?: boolean; dry?: boolean; only?: string[] } = {}) =>
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
  chapters: (project: string) => request<ChaptersResponse>(`/api/chapters${q({ project })}`),
  script: (project: string) => request<ScriptResponse>(`/api/script${q({ project })}`),
  completeness: (project: string) => request<CompletenessResponse>(`/api/completeness${q({ project })}`),

  log: (project: string, offset: number) =>
    request<LogResponse>(`/api/log${q({ project, offset })}`),

  gacha: (project: string, name: string, n = 2) =>
    post<RunResult>('/api/chars/gacha', { project, name, n }),
  adopt: (project: string, name: string, file: string) =>
    post<SimpleResult>('/api/chars/adopt', { project, name, file }),

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
