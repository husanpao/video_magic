<template>
  <TopBar />
  <div class="workbench">
    <FilterRail />
    <ShotNav />
    <ShotTable />
    <RightPanel />
  </div>
  <Filmstrip />
  <ShotModal />
  <QueueFab />
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, watch } from 'vue'
import TopBar from '@/components/TopBar.vue'
import FilterRail from '@/components/FilterRail.vue'
import ShotNav from '@/components/ShotNav.vue'
import ShotTable from '@/components/ShotTable.vue'
import RightPanel from '@/components/RightPanel.vue'
import Filmstrip from '@/components/Filmstrip.vue'
import ShotModal from '@/components/ShotModal.vue'
import QueueFab from '@/components/QueueFab.vue'
import {
  loadProjects, pullLog, refreshAudit, refreshChapters, refreshChars, refreshProps,
  refreshQc, refreshScenes, refreshScript, refreshShots, refreshStatus,
  refreshAssets, refreshStoryboard, state,
} from '@/stores/app'

let timer = 0
let lastShotsAt = 0
let lastTabAt = 0
let wasRunning = false

/**
 * 各 tab 对应的数据刷新器。
 *
 * ★ 设计目标：**不要死页面**（2026-09-24，用户明确要求）。
 * 之前只有「日志 + 顶栏进度」在轮询，右栏所有 tab 都只在**切换 tab 那一刻**刷新 ——
 * 于是你盯着「质检」tab 时跑完质检，界面上什么都不会变。
 */
const TAB_REFRESH: Record<string, () => Promise<void>> = {
  chars: refreshChars,
  script: refreshScript,
  scenes: refreshScenes,
  props: refreshProps,
  storyboard: refreshStoryboard,
  qc: refreshQc,
  audit: refreshAudit,
}

async function refreshActiveTab(): Promise<void> {
  const fn = TAB_REFRESH[state.tab]
  if (!fn) return
  try {
    await fn()
  } catch {
    /* 单个 tab 拉取失败不该打断轮询 */
  }
}

/** 任务刚结束：所有可能变了的都补一次（各 tab 自己做了 5s 去重，重复调用很便宜）。 */
async function refreshEverythingChanged(): Promise<void> {
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

/**
 * 每 2 秒轮询。**运行中要让所有会变的地方都跟着走。**
 *
 *  · 镜头表：运行中每 4s（整表 52 行不算轻，不必每 2 秒）
 *  · 当前 tab：运行中每 4s（盯质检就看质检动，盯分镜图就看图出来）
 *  · 空闲时当前 tab 每 15s 兜一次底 —— 别的会话/CLI 改了东西也能看见
 *  · `running` True→False：任务刚结束，**全量补一次**
 */
async function tick(): Promise<void> {
  await refreshStatus(true)
  await pullLog()
  const now = Date.now()

  if (state.running && now - lastShotsAt > 4000) {
    lastShotsAt = now
    await refreshShots()
  }
  if (state.running && now - lastTabAt > 4000) {
    lastTabAt = now
    await refreshActiveTab()
  }
  if (!state.running && now - lastTabAt > 15000) {
    lastTabAt = now
    await refreshActiveTab()
  }

  if (wasRunning && !state.running) {
    wasRunning = false
    lastShotsAt = lastTabAt = now
    await refreshEverythingChanged()
  }
  if (state.running) wasRunning = true
}

onMounted(async () => {
  await loadProjects()
  await refreshStatus()
  // 场景实体要在镜头表之前/同时拿到 —— 表格「场景」列和左栏分组都依赖它
  await refreshChapters()
  await refreshScript()
  await refreshScenes()
  await refreshStoryboard()
  await refreshAssets()
  await refreshShots()
  timer = window.setInterval(tick, 2000)
})

onUnmounted(() => window.clearInterval(timer))

// ★ 切项目必须**同步清空**所有项目级状态（2026-09-24 修）。
// 原来只清了 shotsLoaded / selected / logText，结果切到新项目的那一瞬间：
// 界面上还挂着**上一个项目的镜头行**（带真实的 clip 与 mtime），而缩略图 URL 里的
// project 已经换成新项目 → 浏览器按旧 mtime 去新项目里找不存在的文件，**刷出一串 404**，
// 同时还会闪一下错误数据。真浏览器 E2E 就是这样抓到的。
watch(
  () => state.project,
  async (p) => {
    if (!p) return
    // 先同步清空（不要 await，否则清空前的旧渲染还在）
    state.shots = []
    state.counts = { total: 0, current: 0, stale: 0, missing: 0, qc_fail: 0 }
    state.shotsLoaded = false
    state.selected = ''
    state.logText = ''
    state.chapters = []
    state.chapter = null
    state.scripts = []
    state.scenes = []
    state.props = []
    state.storyboard = []
    state.assets = []
    state.chars = []
    state.qc = { results: {}, counts: { pass: 0, suspicious: 0 }, review: [], rerender: [], has_result: false, generated_at: 0 }
    state.audit = null
    state.final = null
    await refreshStatus()
    await refreshChapters()
    await refreshScript()
    await refreshScenes()
    await refreshStoryboard()
    await refreshShots()
  },
)
</script>
