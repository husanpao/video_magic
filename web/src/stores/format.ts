/**
 * 与状态无关的纯格式化 / 解析函数。
 *
 * 单列一个文件：它们被 6 个域模块与十几个组件同时用，放进任何域都会
 * 让那条依赖边变成"为了一个 `fmtSize` 去 import 整个镜头域"。
 */

/** 镜头属于第几章 —— 从 **id 首段**取（`2-1-03` → 2）。
 *  比读文件名稳：镜头表被手工改名也对得上。 */
export function chapterOf(id: string): number {
  const seg = String(id || '').split('-')[0]
  const n = Number(seg)
  return Number.isFinite(n) && n > 0 ? n : 0
}

/** 场次号（`1-3-02` → `1-3`）。★ 与**场景实体**不是一回事 —— 那是 `sceneById`。 */
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
