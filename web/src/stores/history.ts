import { computed } from 'vue'
import { state } from './state'
import type { EditEntry } from './types'

/**
 * 撤销 / 重做栈（U4）。
 *
 * 这一域**只管记账，不管执行**：`undoEdit()` / `redoEdit()` 那两个真正跑逆操作、
 * 跑完要刷新镜头表的编排在 barrel（`app.ts`）里。原因是逆操作执行完必须同时刷新
 * 镜头域与分析域 —— 那需要 import 两个域，而这两个域反过来都要 `pushEdit`。
 * 把编排放在唯一不认识任何人的那一层，依赖才不会绕成圈。
 */
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
