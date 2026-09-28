<template>
  <TopBar />

  <!-- 全局错误条（U3 / B6）：state.error 的**唯一消费方**。
       之前错误只写不读：网络抖动/首拉失败界面"看起来没事"。现在所有 refresh* 的失败
       都会从这里冒出来，点 ✕ 或同通道恢复成功后消失。 -->
  <div v-if="state.error" class="global-error-bar" data-testid="global-error-bar" role="alert">
    <span class="errtext">{{ state.error }}</span>
    <button class="errx" title="知道了（关闭错误条）" @click="clearError">✕</button>
  </div>

  <!-- 工作区（F1.1）。KeepAlive 让切屏不丢各屏自己的状态：
       工作台要保住滚动位置与选中镜，章节屏要保住正在编辑但没保存的草稿。 -->
  <router-view v-slot="{ Component }">
    <keep-alive :max="4">
      <component :is="Component" :key="$route.name" />
    </keep-alive>
  </router-view>

  <!-- 队列浮层 / 主题与抽屉开关属于**壳**：在任何一屏跑任务都要能看到进度与停止 -->
  <QueueFab />
  <UiToggles />

  <!-- 镜头详情弹层也在壳里（W4 实锤）：它原来挂在 WorkbenchView 内，
       于是在审片屏点质检行的「详情」什么也不发生 —— KeepAlive 把工作台摘下来时
       组件不再渲染，el-dialog 根本没被创建。任何一屏点开一镜都要有它。 -->
  <ShotModal />

  <!-- `?` 快捷键帮助浮层（U5）。内容就是 useHotkeys 里的 HOTKEYS 表 ——
       帮助永远和真实绑定同源，不会"文档说有、实际没绑"。 -->
  <div v-if="helpOpen" class="hotkey-help" data-testid="hotkey-help" @click.self="helpOpen = false">
    <div class="hkbox" role="dialog" aria-label="快捷键帮助">
      <div class="hkhead">
        <b>快捷键</b>
        <span class="spacer" />
        <button class="errx" title="关闭（Esc）" @click="helpOpen = false">✕</button>
      </div>
      <div v-for="k in HOTKEYS" :key="k.keys" class="hkrow">
        <code>{{ k.keys }}</code>
        <span>{{ k.desc }}</span>
      </div>
      <div class="hkfoot dim small">打字时（输入框内）单键快捷键不触发；Ctrl+S / Ctrl+Z 除外。</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, watch } from 'vue'
import TopBar from '@/components/TopBar.vue'
import QueueFab from '@/components/QueueFab.vue'
import UiToggles from '@/components/UiToggles.vue'
import ShotModal from '@/components/ShotModal.vue'
import { HOTKEYS, helpOpen, useHotkeys } from '@/composables/useHotkeys'
import { every } from '@/composables/useTicker'
import { refreshTab } from '@/stores/refresh'
import {
  clearError, clearRenderHints, clearTransient, errText, loadProjects, pullLog, refreshAll,
  refreshAssets, refreshChapters, refreshQueue, refreshScenes,
  refreshScript, refreshShots, refreshStatus, refreshStoryboard, resetProjectScoped, setError, state,
} from '@/stores/app'

let offTick: (() => void) | null = null
let lastShotsAt = 0
let lastTabAt = 0
let wasRunning = false

// 全局快捷键：←/→、E、R、L、/、Ctrl+S、Ctrl+Z、? —— 一处注册（U5）
useHotkeys()

/**
 * ★ 设计目标：**不要死页面**（2026-09-24，用户明确要求）。
 * 之前只有「日志 + 顶栏进度」在轮询，右栏所有 tab 都只在**切换 tab 那一刻**刷新 ——
 * 于是你盯着「质检」tab 时跑完质检，界面上什么都不会变。
 *
 * tab → 数据源那张表搬到了 `stores/refresh.ts`（全仓库唯一一份），
 * 这里和 RightPanel 都用 `refreshTab()`，不会再出现"轮询表里有、切换表里漏"。
 */

/**
 * 每 2 秒轮询。**运行中要让所有会变的地方都跟着走。**
 *
 *  · 镜头表：运行中每 4s（整表 52 行不算轻，不必每 2 秒）
 *  · 当前 tab：运行中每 4s（盯质检就看质检动，盯分镜图就看图出来）
 *  · 空闲时当前 tab 每 15s 兜一次底 —— 别的会话/CLI 改了东西也能看见
 *  · `running` True→False：任务刚结束，**全量补一次**
 *
 *  轮询体本身也兜一层 catch（U3）：任何一处意外异常都要变成界面报错，
 *  而不是让 setInterval 静默死掉 —— 定时器一死整个控制台就"冻结"在旧数据上了。
 */
async function tick(): Promise<void> {
  try {
    await refreshStatus()
    await pullLog()
    const now = Date.now()

    if (state.running && now - lastShotsAt > 4000) {
      lastShotsAt = now
      await refreshShots()
    }
    if (state.running && now - lastTabAt > 4000) {
      lastTabAt = now
      await refreshTab(state.tab)
    }
    if (!state.running && now - lastTabAt > 15000) {
      lastTabAt = now
      await refreshTab(state.tab)
    }

    if (wasRunning && !state.running) {
      wasRunning = false
      lastShotsAt = lastTabAt = now
      clearRenderHints() // 任务结束 → 不存在「生成中」的乐观标记了
      await refreshAll()
    }
    if (state.running) wasRunning = true
  } catch (e) {
    // 兜底：refresh* 都已 guard，走到这里说明是轮询框架自身出错
    setError(`轮询：${errText(e)}`)
  }
}

onMounted(async () => {
  // 每个 refresh* 失败都会自己回落 + 报错（guard），首拉失败不再白屏无提示
  await loadProjects()
  await refreshStatus()
  // 场景实体要在镜头表之前/同时拿到 —— 表格「场景」列和左栏分组都依赖它
  await refreshChapters()
  await refreshScript()
  await refreshScenes()
  await refreshStoryboard()
  await refreshAssets()
  await refreshQueue()
  await refreshShots()
  // F0.2：不再自己 setInterval —— 挂到全控制台唯一的心跳上（见 composables/useTicker.ts）。
  // 注册当拍不发请求，首拉由上面这一串显式调用完成，与拆分前行为一致。
  offTick = every(2000, tick)
})

onUnmounted(() => { offTick?.(); offTick = null })

// ★ 切项目必须**同步清空**所有项目级状态（2026-09-24 修）。
// 原来只清了 shotsLoaded / selected / logText，结果切到新项目的那一瞬间：
// 界面上还挂着**上一个项目的镜头行**（带真实的 clip 与 mtime），而缩略图 URL 里的
// project 已经换成新项目 → 浏览器按旧 mtime 去新项目里找不存在的文件，**刷出一串 404**，
// 同时还会闪一下错误数据。真浏览器 E2E 就是这样抓到的。
watch(
  () => state.project,
  async (p) => {
    // 删掉**最后一个**当前项目时 p 会变成空串：也必须先清旧项目数据，
    // 否则再进工作台会看到已经移进回收站的镜头表（ProjectsView 的删除操作真实会走到这里）。
    // refresh* 才需要项目名，所以只把 return 放在复位之后。
    resetProjectScoped()
    clearTransient()
    if (!p) return
    await refreshStatus()
    await refreshChapters()
    await refreshScript()
    await refreshScenes()
    await refreshStoryboard()
    await refreshShots()
  },
)
</script>

<style scoped>
/* 全局错误条：贴在顶栏下方，不进 .workbench（E2E 断言 .workbench > * 是 4 个） */
.global-error-bar {
  display: flex; align-items: center; gap: 10px;
  padding: 6px 14px; font-size: 12px;
  background: color-mix(in srgb, var(--bad) 8%, var(--card));
  border-bottom: 1px solid color-mix(in srgb, var(--bad) 35%, var(--line));
  color: var(--bad);
}
.global-error-bar .errtext { flex: 1; line-height: 1.5; }
.errx {
  border: none; background: transparent; cursor: pointer; color: var(--muted);
  font: inherit; font-size: 13px; padding: 0 4px;
}
.errx:hover { color: var(--ink); }

/* `?` 帮助浮层 */
.hotkey-help {
  position: fixed; inset: 0; z-index: 3000;
  background: color-mix(in srgb, var(--ink) 35%, transparent);
  display: flex; align-items: center; justify-content: center;
}
.hkbox {
  width: min(440px, 92vw); background: var(--card); color: var(--ink);
  border: 1px solid var(--line); border-radius: var(--radius);
  box-shadow: var(--shadow); padding: 12px 14px;
}
.hkhead { display: flex; align-items: center; margin-bottom: 6px; font-size: 13.5px; }
.hkrow {
  display: flex; align-items: baseline; gap: 12px; padding: 4px 2px;
  border-bottom: 1px solid var(--line); font-size: 12px;
}
.hkrow:last-of-type { border-bottom: none; }
.hkrow code {
  flex: 0 0 128px; font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 11.5px;
  background: var(--hover); border: 1px solid var(--line); border-radius: 6px;
  padding: 1px 6px; text-align: center;
}
.hkfoot { margin-top: 8px; }
</style>
