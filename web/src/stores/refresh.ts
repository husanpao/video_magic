/**
 * tab → 该 tab 的数据源。**全仓库只有这一张表**（F0.2/F0.3）。
 *
 * 拆之前是两份逐字近似的手工映射：
 *   · `App.vue`   `TAB_REFRESH` —— 给轮询用（多了 `completeness`）
 *   · `RightPanel` `REFRESH`    —— 给"切 tab 时刷一次"用（少了 `completeness`，
 *                                 另外"场景/道具要连 assets 一起刷"写在了函数体里）
 *
 * 两份的差别本身就是 bug 源：`completeness` 只进了轮询表，所以切到完备性 tab 时
 * 反而不会立刻刷新。更糟的是**新增一个数据源要记得改两处**，漏一处就是
 * "切 tab 数据不动"或"运行中这个 tab 不跟着变"。
 *
 * 现在只有这里：每一项声明"进这个 tab 要拉什么"、"轮询时要刷什么"。
 */

import {
  refreshAssets, refreshAudit, refreshChars, refreshChapters, refreshCompleteness,
  refreshProps, refreshQc, refreshScript, refreshScenes, refreshStoryboard,
} from './app'

/**
 * tab 名 → 刷新函数列表。
 * `chapters` 跟着镜头类 tab 走：章计数（每章镜数）与镜头表同源，
 * 只刷镜头会让顶栏"全片口径"与筛选栏"按章口径"两个数字对不上。
 */
const TAB_SOURCES: Record<string, Array<() => Promise<void>>> = {
  log: [],                       // 日志由 App.vue 的增量拉取维护
  chars: [refreshChars],
  script: [refreshScript],
  // 场景/道具合并成一个 tab（F3.1）；tab 里带候选概念图，所以一并刷 assets
  sceneProp: [refreshScenes, refreshProps, refreshAssets],
  storyboard: [refreshStoryboard],
  qc: [refreshQc],
  audit: [refreshAudit],
  completeness: [refreshCompleteness, refreshChapters],
  // 「成片」不再是右栏 tab：它搬到了 W4 审片屏（`views/ReviewView.vue` 自己订阅刷新）。
  // 成片列表本来就随 /api/status 走，所以这里没有它的数据源。
}

export function tabSourceFns(tab: string): Array<() => Promise<void>> {
  return TAB_SOURCES[tab] ?? []
}

/**
 * 刷新某个 tab 的数据源。串行而不是 `Promise.all`：
 * 这些函数都会写同一个 `state`，且底层是同一个单进程 HTTP 服务 ——
 * 并发发过去只会互相排队，却多出交错写入的排查成本。
 * 不抛：`refresh*` 内部已统一 `guard()`（失败回落 + 写全局错误条）。
 */
export async function refreshTab(tab: string): Promise<void> {
  for (const fn of tabSourceFns(tab)) await fn()
}

/** 轮询里"这个 tab 要不要跟着刷"的判据：有数据源才刷。 */
export function tabIsPollable(tab: string): boolean {
  return tabSourceFns(tab).length > 0
}
