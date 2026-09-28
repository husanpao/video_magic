<template>
  <!-- W3 镜头工作台：四栏 + 胶片条 + 镜头详情弹层。
       这块是**原样搬过来**的，不是重写 —— 它对应 v0.2 已验证的交互，
       重构约束（`docs/全流程整合与实施计划.md:§6.2`）明确要求工作台形状不动。 -->
  <div class="workbench">
    <FilterRail />
    <ShotNav />
    <ShotTable />
    <RightPanel />
  </div>
  <Filmstrip />
</template>

<script setup lang="ts">
/**
 * WorkbenchView —— 控制台的主屏（W3），也是**默认落地屏**。
 *
 * 为什么工作台要整体做成一个"屏"而不是页面本身：
 * F1 要在它前面加"项目 / 章节 / 风格"这些**镜头还不存在时**要做的界面。
 * 如果直接往右栏继续叠 tab，就会从 9 个变 12 个平铺 tab —— 那正是
 * 「一切以镜头为中心」的界面结构撑不住新流程的表现。
 *
 * ★ 轮询、快捷键、队列浮层、全局错误条、**镜头详情弹层**都留在 `App.vue`（壳）里，不在这里：
 *   它们在**任何一屏**都该工作（在章节屏跑着拆镜也得能看到进度与停止按钮；
 *   在审片屏从质检行点「详情」也要能改那一镜 —— W4 实测就是这样抓出它原来挂在屏里的）。
 */
import FilterRail from '@/components/FilterRail.vue'
import ShotNav from '@/components/ShotNav.vue'
import ShotTable from '@/components/ShotTable.vue'
import RightPanel from '@/components/RightPanel.vue'
import Filmstrip from '@/components/Filmstrip.vue'
</script>
