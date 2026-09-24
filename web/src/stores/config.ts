import { computed, reactive } from 'vue'
import { configApi } from '@/api/config'
import type { ConfigItem, ConfigValue, ImpactInfo, PreflightCheck, SaveChange } from '@/api/config'
import { ApiError } from '@/api/client'

/**
 * 设置面板的专属 store（T4）。
 *
 * 为什么单独开：`stores/app.ts` 归 T5 且写入范围互斥；配置面板只需要自己的
 * 「加载 → 草稿编辑 → 预检 → 保存（危险项确认）」闭环，保存成功后调用 app store
 * 既有的 refreshShots/refreshStatus 刷新镜头状态（只调用，不改它）。
 * 风格与 stores/ui.ts 一致：模块级 reactive，不引 pinia（main.ts 也没装 pinia）。
 */

export type EditLayer = 'project' | 'workspace'

export interface DraftEntry {
  layer: EditLayer
  /** null = 从该层删除（一键恢复继承 / 清除全局设置） */
  value: ConfigValue
}

export interface CfgState {
  loading: boolean
  saving: boolean
  validating: boolean
  project: string
  /** 编辑目标层：project=本项目覆盖；workspace=全局默认（projects/config.json） */
  mode: 'project' | 'global'
  groups: string[]
  items: ConfigItem[]
  files: { user: string; workspace: string; project: string }
  apiKeyPresent: boolean
  notes: string[]
  draft: Record<string, DraftEntry>
  errors: Array<{ key: string; message: string }>
  checks: PreflightCheck[]
  impact: ImpactInfo | null
}

export const cfg = reactive<CfgState>({
  loading: false, saving: false, validating: false,
  project: '', mode: 'project',
  groups: [], items: [],
  files: { user: '', workspace: '', project: '' },
  apiKeyPresent: false, notes: [],
  draft: {}, errors: [], checks: [], impact: null,
})

export const editLayer = computed<EditLayer>(() => (cfg.mode === 'project' ? 'project' : 'workspace'))

export const itemsByGroup = computed(() => {
  const map = new Map<string, ConfigItem[]>()
  for (const g of cfg.groups) map.set(g, [])
  for (const it of cfg.items) {
    const list = map.get(it.group)
    if (list) list.push(it)
    else map.set(it.group, [it])
  }
  return [...map.entries()]
})

export const dirtyKeys = computed(() => Object.keys(cfg.draft))

export const dirtyCount = computed(() => dirtyKeys.value.length)

/** 当前编辑层能否编辑这一项（预算=只许项目层；帧网格=只许全局层）。 */
export function editable(it: ConfigItem): boolean {
  return it.scope === 'both' || (it.scope === 'project' && cfg.mode === 'project')
    || (it.scope === 'global' && cfg.mode === 'global')
}

/** 表单上该显示的值：草稿优先，否则生效值。 */
export function shownValue(it: ConfigItem): ConfigValue {
  const d = cfg.draft[it.key]
  return d ? d.value : it.value
}

export function setDraft(key: string, value: ConfigValue): void {
  cfg.draft[key] = { layer: editLayer.value, value }
  // 改回与生效值一致 = 不算改动（避免"啥也没改却弹危险确认"）
  const it = cfg.items.find((x) => x.key === key)
  if (it && JSON.stringify(value) === JSON.stringify(it.value)) delete cfg.draft[key]
}

export function clearDraft(key: string): void {
  delete cfg.draft[key]
}

/**
 * 一键恢复继承：把这一项从**项目层**删掉，回到全局默认。
 * 全局模式下则是「清除全局设置」（回到内置默认/更低层）。
 */
export function inherit(key: string): void {
  cfg.draft[key] = { layer: editLayer.value, value: null }
}

export function resetAllDrafts(): void {
  cfg.draft = {}
  cfg.errors = []
  cfg.checks = []
  cfg.impact = null
}

export function buildChanges(): SaveChange[] {
  return Object.entries(cfg.draft).map(([key, d]) => ({ key, layer: d.layer, value: d.value }))
}

function errorList(e: unknown): Array<{ key: string; message: string }> {
  if (e instanceof ApiError) {
    const issues = (e.payload as { issues?: Array<{ key: string; message: string }> }).issues
    if (issues && issues.length) return issues
    return [{ key: '', message: e.message }]
  }
  return [{ key: '', message: (e as Error).message || String(e) }]
}

export async function loadConfig(project: string): Promise<void> {
  cfg.loading = true
  cfg.project = project
  cfg.errors = []
  try {
    const r = await configApi.get(project)
    cfg.groups = r.groups
    cfg.items = r.items
    cfg.files = r.files
    cfg.apiKeyPresent = r.api_key_present
    cfg.notes = r.notes
    resetAllDrafts()
  } catch (e) {
    cfg.errors = errorList(e)
  } finally {
    cfg.loading = false
  }
}

/** 保存前预检：schema 预演 + ComfyUI/模型/字体/ffmpeg/Key + 影响清单（只读，不落盘）。 */
export async function runValidate(): Promise<void> {
  cfg.validating = true
  cfg.errors = []
  try {
    const values: Record<string, ConfigValue> = {}
    for (const [key, d] of Object.entries(cfg.draft)) {
      // null（恢复继承）不进预演值 —— 后端校验只收真实值；它的影响由保存时的
      // needs_confirm 精确兜底（恢复继承同样会过危险项确认）
      if (d.value !== null) values[key] = d.value
    }
    const r = await configApi.validate(cfg.project, values)
    cfg.errors = r.errors
    cfg.checks = r.checks
    cfg.impact = r.impact
  } catch (e) {
    cfg.errors = errorList(e)
    cfg.checks = []
    cfg.impact = null
  } finally {
    cfg.validating = false
  }
}

export interface SaveOutcome {
  ok: boolean
  needsConfirm: boolean
  message: string
}

/**
 * 保存草稿。危险项（进镜头指纹）改动第一次调用（confirm=false）不会落盘，
 * 只返回 needsConfirm + 影响清单 —— 弹确认框后带 confirm=true 重发。
 */
export async function saveDraft(confirm: boolean): Promise<SaveOutcome> {
  cfg.saving = true
  cfg.errors = []
  try {
    const r = await configApi.save(cfg.project, buildChanges(), confirm)
    cfg.impact = r.impact
    if (r.needs_confirm) {
      return { ok: false, needsConfirm: true, message: r.impact.message }
    }
    resetAllDrafts()
    await loadConfig(cfg.project)
    return { ok: true, needsConfirm: false, message: `已保存 ${r.saved.length} 项` }
  } catch (e) {
    cfg.errors = errorList(e)
    return { ok: false, needsConfirm: false, message: (e as Error).message }
  } finally {
    cfg.saving = false
  }
}
