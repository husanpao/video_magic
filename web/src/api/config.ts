import { ApiError } from '@/api/client'

/**
 * 配置中心 API（T3/T4）。
 *
 * 为什么不放进 `api/client.ts`：那个文件归 T5（ux-core）维护，写入范围互斥 ——
 * 这里自带同约定的极简请求封装（成功 `{ok:true,…}` / 失败 `{ok:false,error,message}` + 4xx），
 * 错误类直接复用 client.ts 导出的 `ApiError`（只 import 不改它）。
 *
 * 与 vm/config.py 的载荷一一对应：
 * - 值是**扁平点分键**（"llm.base_url" / "budget.max_gpu_minutes"），保存时后端自己落成嵌套 JSON
 * - value=null 表示「从该层删除」= 一键恢复继承
 * - 进镜头指纹的危险项改动必须带 confirm_impact 才会落盘（否则返回 needs_confirm + 影响清单）
 */

/** 配置值：标量 / 字符串数组（str_list 类）。null = 该层未设置（或预算「不设限」）。 */
export type ConfigValue = string | number | boolean | string[] | null

export type Layer = 'env' | 'user' | 'workspace' | 'project' | 'builtin'

export interface ConfigItem {
  key: string
  group: string
  label: string
  kind: 'str' | 'url' | 'path' | 'int' | 'float' | 'bool' | 'enum' | 'color' | 'str_list'
  value: ConfigValue
  default: ConfigValue
  source: Layer
  /** 各层的原始值（null = 该层没设）—— 继承/覆盖可视化就靠它 */
  layers: Partial<Record<'env' | 'user' | 'workspace' | 'project', ConfigValue>>
  overridden: boolean
  /** both = 两层都可设；project = 只许项目层（消费方直读 project.json）；global = 只许全局层（机器级） */
  scope: 'both' | 'project' | 'global'
  nullable: boolean
  enum: string[]
  minimum: number | null
  maximum: number | null
  multiple: number | null
  placeholder: string
  /** 说明文案三段式：是什么 / 改了影响什么 / 会不会让镜头变 stale */
  what: string
  effect: string
  stale: string
  /** 进镜头指纹 → 改动让相关镜头变「需重渲」（危险项，保存要确认） */
  stale_impact: boolean
  /** 影响画面但**不**进指纹 → 改了不会自动重渲（需手动强制重渲） */
  render_only: boolean
  channel: 'params' | 'env' | 'file'
}

export interface ConfigResponse {
  ok: boolean
  project: string
  groups: string[]
  items: ConfigItem[]
  files: { user: string; workspace: string; project: string }
  api_key_present: boolean
  notes: string[]
}

export interface PreflightCheck {
  id: string
  label: string
  ok: boolean
  detail: string
}

export interface ImpactChange {
  key: string
  label: string
  old: ConfigValue
  new: ConfigValue
  stale?: string
  note?: string
}

export interface ImpactInfo {
  changed: string[]
  dangerous: ImpactChange[]
  silent: ImpactChange[]
  stale_shots: string[]
  stale_count: number
  total_shots: number
  message: string
}

export interface ValidateResponse {
  ok: boolean
  errors: Array<{ key: string; message: string }>
  checks: PreflightCheck[]
  impact: ImpactInfo
}

export interface SaveChange {
  key: string
  layer: 'user' | 'workspace' | 'project'
  /** null = 从该层删除（恢复继承） */
  value: ConfigValue
}

export interface SaveResponse {
  ok: boolean
  saved: string[]
  /** true = 这批改动含危险项且未确认，**没有落盘**；摆出 impact 让人确认后带 confirm_impact 重发 */
  needs_confirm: boolean
  impact: ImpactInfo
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
    throw new ApiError(b?.message || `HTTP ${res.status}`, b?.error || 'http_error', res.status, b || {})
  }
  return body as T
}

function post<T>(path: string, data: Record<string, unknown>): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  })
}

export const configApi = {
  /** 全部配置项 + 各层值。project 空 = 只看全局层。 */
  get: (project: string) =>
    request<ConfigResponse>(`/api/config${project ? `?project=${encodeURIComponent(project)}` : ''}`),

  /** 保存一批改动（value=null = 恢复继承）。危险项需 confirm_impact=true 才落盘。 */
  save: (project: string, changes: SaveChange[], confirmImpact = false) =>
    post<SaveResponse>('/api/config', { project, changes, confirm_impact: confirmImpact }),

  /** 保存前预检：schema 错误 + ComfyUI/模型/字体/ffmpeg/Key + 影响清单。 */
  validate: (project: string, values: Record<string, ConfigValue>) =>
    post<ValidateResponse>('/api/config/validate', { project, values }),
}
