import { ElMessage } from 'element-plus'
import { state, errText, setError } from './state'
import { clearRenderHints } from './task'
import { refreshShots } from './shots'
import {
  refreshChars, refreshScenes, refreshAssets, refreshStoryboard,
} from './entities'
import {
  refreshQc, refreshAudit, refreshScript, refreshChapters,
} from './analysis'

/**
 * stores/app.ts —— 全局状态的**组合出口**（barrel）。
 *
 * 2026-09-27 拆分（F0.1）：原先这个文件 667 行，把 9 个域的状态、13 个 refresh、
 * 派生判定、撤销栈、格式化函数全装在一个 `interface State` 里 —— 症状是
 * "改任何一屏都要路过别人的状态"，和后台 `taskctl.py`（4300+ 行 / 10 个子系统）同一个病。
 *
 * 现在状态容器与按域的行为分别在：
 *   · `state.ts`    唯一的状态对象 + 统一错误通道 `guard`
 *   · `types.ts`    AnyKind / KIND_INFO / FILTERS / STAGES / EditEntry（多域共用的常量）
 *   · `format.ts`   与状态无关的纯函数（fmtSize / fmtMmSs / sceneOf / chapterOf）
 *   · `projects.ts` 项目列表与当前项目
 *   · `task.ts`     运行态：status / 日志 / 队列 / 乐观「生成中」
 *   · `shots.ts`    镜头表 + `kindOf` 派生档 + 筛选 + 单选/多选
 *   · `entities.ts` 角色 / 场景 / 道具 / 分镜图 / 概念图候选
 *   · `analysis.ts` 质检 / 审计 / 完备性 / 剧本 / 章节
 *   · `history.ts`  撤销栈记账
 *
 * 这个文件只留两类东西：
 *   1. **转出口** —— 25 个文件、55 个符号一直是从这里 import 的，
 *      保持签名不变，组件才需要一个字都不改（本次重构的验收标准是视觉零变化）。
 *   2. **跨域编排** —— `refreshAfterEdit` / `refreshAll` / `undoEdit` / `redoEdit` /
 *      `clearTransient`。这几支各自要路过两个以上的域，只能放在唯一不认识任何人的这一层；
 *      放进任何域都会让两个域互相 import，绕回一个文件。
 *
 * ★ 刻意**没有**引入 pinia。仓库已写明的约定是「模块级 reactive，不引 pinia」
 *   （见 `config.ts` 文件头），`package.json` 里那个 pinia 至今没装进 `main.ts`
 *   是决定不是遗漏。换状态框架要连 `ui.ts` / `config.ts` 一起改，那是独立决策。
 */

export { state, setError, clearError, errText, guard, resetProjectScoped, PROJECT_SCOPED, initialState } from './state'
export type { State } from './state'
export { KIND_INFO, FILTERS, STAGES, STAGE_CN } from './types'
export type { AnyKind, EditEntry } from './types'
export { fmtMmSs, fmtSize, sceneOf, chapterOf } from './format'
export { loadProjects } from './projects'
export {
  refreshStatus, pullLog, refreshQueue, markRendering, clearRenderHints, submitRerender,
} from './task'
export {
  kindOf, kindInfo, visibleShots, kindCounts, sceneLabel, groupShots, segTimeline, opsFor,
  isChecked, toggleChecked, allVisibleChecked, toggleCheckAllVisible,
  refreshShots, setFlag, toggleLockShot, rerenderOne,
} from './shots'
export {
  refreshChars, refreshScenes, refreshProps, refreshAssets, refreshStoryboard,
  sceneById, storyboardById, assetOf,
} from './entities'
export {
  refreshQc, refreshAudit, refreshCompleteness, refreshScript, refreshChapters,
} from './analysis'
export { pushEdit, canUndo, canRedo, undoLabel, redoLabel } from './history'

/* ---------------- 跨域编排 ---------------- */

/** 写后即刷（U2 / B4）：镜头编辑成功后**立即**刷新镜头表与章计数。
 *  以前拆分/合并/插入/保存只重载弹层自身，App 空闲时不轮询镜头表 →
 *  表格长时间显示旧数据，用户以为没生效。 */
export async function refreshAfterEdit(): Promise<void> {
  await refreshShots()
  await refreshChapters()
}

/** 全量补刷（任务刚结束 / 清空重置后调一次）。
 *  各 tab 有自己的去重，重复调用很便宜。 */
export async function refreshAll(): Promise<void> {
  await refreshShots()
  await refreshChapters()
  await refreshScenes()
  await refreshScript()
  await refreshStoryboard()
  await refreshAssets()
  await refreshQc()
  await refreshAudit()
  await refreshChars()
}

/** 撤销栈顶操作。**永不抛出**（快捷键里是 fire-and-forget 调用）；
 *  逆操作失败要把条目放回栈顶 —— 不放回去这一操作就"既没撤销也找不回"了。 */
export async function undoEdit(): Promise<void> {
  const e = state.undoStack.pop()
  if (!e) {
    ElMessage.info('没有可撤销的操作')
    return
  }
  try {
    await e.undo()
    state.redoStack.push(e)
    state.historyRev++
    ElMessage.success(`已撤销：${e.label}`)
    await refreshAfterEdit()
  } catch (err) {
    state.undoStack.push(e)
    setError(`撤销失败：${errText(err)}`)
    ElMessage.error(`撤销失败：${errText(err)}`)
  }
}

/** 重做栈顶操作。与 undoEdit 对称。 */
export async function redoEdit(): Promise<void> {
  const e = state.redoStack.pop()
  if (!e) {
    ElMessage.info('没有可重做的操作')
    return
  }
  if (!e.redo) {
    state.redoStack.push(e)
    ElMessage.warning(`「${e.label}」没有可安全重放的正操作`)
    return
  }
  try {
    await e.redo()
    state.undoStack.push(e)
    state.historyRev++
    ElMessage.success(`已重做：${e.label}`)
    await refreshAfterEdit()
  } catch (err) {
    state.redoStack.push(e)
    setError(`重做失败：${errText(err)}`)
    ElMessage.error(`重做失败：${errText(err)}`)
  }
}

/** 切项目 / 清空重置时把**会串项目**的瞬态全清掉：
 *  多选（旧 id 对不上新表）、撤销栈（逆操作指向旧项目）、乐观标记、错误条。 */
export function clearTransient(): void {
  state.checked = []
  state.undoStack = []
  state.redoStack = []
  clearRenderHints()
  state.error = ''
}
