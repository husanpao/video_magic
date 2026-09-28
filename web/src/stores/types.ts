import type { ShotKind } from '@/api/types'

/**
 * 全局常量与共享类型。
 *
 * 单列一个文件的原因：`AnyKind` / `KIND_INFO` 被 `shots.ts`（派生档）、
 * `FilterRail` / `ShotTable` / `RightPanel` 等同时引用。放在任何一个域模块里
 * 都会让别的域模块为了一个类型去 import 它，白白制造依赖边。
 */

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
