<template>
  <!-- ④ 420px 右栏：7 个面板（日志 / 角色定妆 / 场景 / 道具 / 质检 / 审计 / 成片） -->
  <aside class="col col-right">
    <el-tabs class="rptabs" :model-value="state.tab" @tab-change="onTabChange">
      <el-tab-pane label="日志" name="log" lazy><LogTab /></el-tab-pane>
      <el-tab-pane label="角色定妆" name="chars" lazy><CharsTab /></el-tab-pane>
      <el-tab-pane label="剧本" name="script" lazy><ScriptTab /></el-tab-pane>
      <el-tab-pane label="场景" name="scenes" lazy><ScenesTab /></el-tab-pane>
      <el-tab-pane label="道具" name="props" lazy><PropsTab /></el-tab-pane>
      <el-tab-pane label="分镜图" name="storyboard" lazy><StoryboardTab /></el-tab-pane>
      <el-tab-pane label="质检" name="qc" lazy><QcTab @select="onSelect" /></el-tab-pane>
      <el-tab-pane label="审计" name="audit" lazy><AuditTab @select="onSelect" /></el-tab-pane>
      <el-tab-pane label="成片" name="final" lazy><FinalTab /></el-tab-pane>
    </el-tabs>
  </aside>
</template>

<script setup lang="ts">
/**
 * 右栏：tab 容器 + 刷新协调。
 *
 * 移植自 vm/static/index.html 的 renderTabs() 与 `$('tabs')` 的点击处理：
 *   切换 tab → 按需刷新（qc→refreshQc / audit→refreshAudit / chars→refreshChars /
 *   scenes→refreshScenes），props 是后加的同类数据源，一并纳入。
 *
 * `lazy` 让每个 tab 面板**首次被激活时才挂载**，配合各 tab 自己的 onMounted
 * 「首次进入拉一次」，避免每次切回都重复整表拉取。
 */
import { onMounted } from 'vue'
import { ElTabPane, ElTabs } from 'element-plus'
import LogTab from '@/components/tabs/LogTab.vue'
import CharsTab from '@/components/tabs/CharsTab.vue'
import ScenesTab from '@/components/tabs/ScenesTab.vue'
import ScriptTab from '@/components/tabs/ScriptTab.vue'
import PropsTab from '@/components/tabs/PropsTab.vue'
import StoryboardTab from '@/components/tabs/StoryboardTab.vue'
import QcTab from '@/components/tabs/QcTab.vue'
import AuditTab from '@/components/tabs/AuditTab.vue'
import FinalTab from '@/components/tabs/FinalTab.vue'
import {
  refreshAssets,
  refreshAudit,
  refreshChars,
  refreshProps,
  refreshQc,
  refreshScenes,
  refreshScript,
  refreshStoryboard,
  state,
} from '@/stores/app'

/** tab 名 → 该 tab 的数据源刷新函数（log / final 的数据由 App.vue 轮询维护，无需刷新）。 */
const REFRESH: Record<string, () => Promise<void>> = {
  chars: refreshChars,
  scenes: refreshScenes,
  props: refreshProps,
  // 场景/道具 tab 里带候选概念图，所以一并刷 assets

  storyboard: refreshStoryboard,
  script: refreshScript,
  qc: refreshQc,
  audit: refreshAudit,
}

/** 「定位该镜」：写 state.selected，由镜头表 / 胶片条 / 详情弹层响应。 */
function onSelect(id: string) {
  if (id) state.selected = id
}

async function refreshFor(tab: string) {
  if (tab === 'scenes' || tab === 'props') await refreshAssets()
  const fn = REFRESH[tab]
  if (!fn) return
  try {
    await fn()
  } catch {
    // 各 tab 组件自己有就地错误态（首次进入会自己拉一次，失败信息就地显示）；
    // 这里静默，避免同一个失败弹两次。
  }
}

function onTabChange(name: string | number) {
  state.tab = String(name)
  void refreshFor(state.tab)
}

onMounted(() => {
  void refreshFor(state.tab)
})
</script>

<style scoped>
/* el-tabs 撑满右栏；正文区不滚，由各 tab 自己的 .tabbody 滚
   （日志必须自己控制 scrollTop，不能靠外层容器）。 */
.rptabs {
  display: flex;
  flex-direction: column;
  flex: 1 1 auto;
  min-height: 0;
}
.rptabs :deep(.el-tabs__header) {
  margin: 0;
  flex: 0 0 auto;
  background: #fafbfc;
}
.rptabs :deep(.el-tabs__nav-wrap) {
  padding: 0 6px;
}
.rptabs :deep(.el-tabs__item) {
  height: 32px;
  line-height: 32px;
  font-size: 12px;
  padding: 0 8px;
}
.rptabs :deep(.el-tabs__content) {
  display: flex;
  flex: 1 1 auto;
  min-height: 0;
  overflow: hidden;
}
/* ★ tab 内容必须能滚（2026-09-24 修）。
   原来这里只有 `overflow: hidden`，而 pane 是 flex 容器、子组件也没一个在滚 ——
   结果 52 张分镜图被**直接裁掉**，后面的看不见也滚不到。
   `LogTab` 不受影响：它自己的 `pre` 带 overflow:auto，是双层滚动里更内层那个。 */
.rptabs :deep(.el-tab-pane) {
  display: flex;
  flex-direction: column;
  flex: 1 1 auto;
  min-width: 0;
  min-height: 0;
  overflow-y: auto;
  overflow-x: hidden;
}
</style>

<style>
/* 右栏 7 个 tab 共享的排版原语。
   来源：旧单文件 vm/static/index.html 的 .summary / .qcrow / .fmsg / .secttitle 等。
   theme.css 没有提供这几个类，而它是固定契约不能改，所以集中在这里定义一次，
   类名沿用旧文件的命名，避免 7 个组件各抄一份。 */
.tabroot {
  display: flex;
  flex-direction: column;
  flex: 1 1 auto;
  min-height: 0;
  height: 100%;
}
.tabbody {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
  padding: 10px;
}
/* 列表型 tab（质检 / 审计）要旧文件那种整宽行，不要内缩留白 */
.tabbody.flush {
  padding: 0;
}
.summary {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  align-items: center;
  padding: 8px 10px;
  border-bottom: 1px solid var(--line);
  font-size: 12px;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
  flex: 0 0 auto;
  background: var(--card);
}
.chk {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
}
.qcrow {
  display: flex;
  gap: 8px;
  align-items: center;
  padding: 7px 10px;
  border-bottom: 1px solid var(--line);
  cursor: pointer;
}
.qcrow:hover {
  background: var(--hover);
}
/* 全片级 finding：不是某一镜，所以不给可点的指针样式 */
.qcrow.project,
.qcrow.project:hover {
  cursor: default;
  background: #fcfdfe;
}
.qcrow .sid {
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  flex: 0 0 auto;
}
.fmsg {
  padding: 0 10px 8px;
  font-size: 12px;
  line-height: 1.6;
}
.fmsg .sug {
  margin-top: 3px;
  color: var(--muted);
}
.qissues {
  padding: 0 10px 6px;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--warn);
}
.secttitle {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 10px 0 6px;
  color: var(--muted);
  font-size: 12px;
}
.secttitle:first-child {
  margin-top: 0;
}
</style>
