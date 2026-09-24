<!--
  全局小浮钮：主题切换 + 小屏抽屉开合（T7 / U11、U12 的用户入口）。
  自包含（Lead 方案 A 的护栏）：显隐状态、localStorage 持久化、抽屉遮罩全在本组件内，
  外部只需 <UiToggles /> 一行挂载；布局怎么动由 theme.css 按 body 类响应。

  E2E 契约（给 test-guard）：
  · [data-testid=theme-toggle]：点一次 = 反转**当前生效**主题（亮↔暗），
    结果写 documentElement[data-theme='light'|'dark'] 并持久化 localStorage['vm-theme-mode']。
    首次访问（无存储）=「跟随系统」：按 prefers-color-scheme 解析并**跟随系统实时变化**，
    此时 localStorage 里没有值，第一次点击后转为显式选择。
  · [data-testid=drawer-toggle]：点一次 = 切换 body.ui-drawers-open（左右抽屉开合）；
    遮罩 .uid-scrim 或 Esc 关闭。≥1280px 没有抽屉，但按钮仍可点（类照切，无副作用），
    这样断言不必先改视口。
-->
<template>
  <div class="uitools">
    <button
      class="uibtn"
      data-testid="theme-toggle"
      aria-label="切换亮 / 暗主题"
      :title="themeTitle"
      @click="toggleTheme"
    >{{ themeIcon }}</button>
    <button
      class="uibtn"
      data-testid="drawer-toggle"
      aria-label="展开 / 收起侧栏"
      :class="{ on: drawersOpen }"
      :title="drawersOpen ? '收起侧栏（筛选 / 镜头导航 / 右侧 tab）' : '展开侧栏（筛选 / 镜头导航 / 右侧 tab）'"
      @click="toggleDrawers"
    >☰</button>
  </div>
  <!-- 抽屉遮罩：只在小屏有意义（theme.css 在 ≥1280px 把它藏掉），点一下收抽屉 -->
  <div v-if="drawersOpen" class="uid-scrim" @click="drawersOpen = false"></div>
</template>

<script setup lang="ts">
/**
 * T7 主题 + 抽屉控制。
 *
 * 为什么主题默认「跟随系统」而点击后转显式：夜间审片的人系统多半已是暗的，
 * 跟随系统零操作就正确；而"切换持久化"保证手动选过一次后不再被系统翻来翻去
 * （localStorage['vm-theme-mode'] ∈ light | dark，删掉即回到跟随系统）。
 * data-theme 一律**显式解析后写在 <html> 上**（不写 media-only），
 * 这样 theme.css 的暗色变量组只需要维护一份（[data-theme='dark']），
 * prefers-color-scheme 的实时跟随由这里的 matchMedia 监听承担。
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'

const STORAGE_KEY = 'vm-theme-mode'
type Mode = 'light' | 'dark' | 'system'

const mq = window.matchMedia('(prefers-color-scheme: dark)')
/** 存储的手动选择；'system' = 跟随系统（默认）。 */
const mode = ref<Mode>('system')
/** 抽屉开合（<1280px 的左右栏；宽屏下类照切但没有可见效果）。 */
const drawersOpen = ref(false)

function effective(): 'light' | 'dark' {
  return mode.value === 'system' ? (mq.matches ? 'dark' : 'light') : mode.value
}

function applyTheme(): void {
  // data-theme 永远显式 —— theme.css 只认 [data-theme='dark']，不认 media（单源调色板）
  document.documentElement.dataset.theme = effective()
}

const themeIcon = computed(() => (effective() === 'dark' ? '🌙' : '☀'))
const themeLabel = computed(() =>
  mode.value === 'system' ? `跟随系统·${effective() === 'dark' ? '暗' : '亮'}` : (effective() === 'dark' ? '暗色' : '亮色'))
const themeTitle = computed(() =>
  `主题：${themeLabel.value}。点击切换亮 / 暗（记住这次选择；当前${mode.value === 'system' ? '跟随系统，删掉本地存储可恢复' : '手动指定'}）`)

/** 点击 = 反转当前**生效**主题（不管来自系统还是手动），并转为显式选择持久化。 */
function toggleTheme(): void {
  const next: Mode = effective() === 'dark' ? 'light' : 'dark'
  mode.value = next
  try {
    localStorage.setItem(STORAGE_KEY, next)
  } catch { /* 隐私模式禁存储也不影响本次切换 */ }
  applyTheme()
}

function toggleDrawers(): void {
  drawersOpen.value = !drawersOpen.value
}

// 抽屉开合同步到 body —— theme.css 的小屏布局按 body.ui-drawers-open 响应
watch(drawersOpen, (on) => {
  document.body.classList.toggle('ui-drawers-open', on)
})

// 跟随系统：显式选择过就不再跟；没选过时系统怎么变就怎么变
function onSysChange(): void {
  if (mode.value === 'system') applyTheme()
}

function onKey(e: KeyboardEvent): void {
  if (e.key === 'Escape' && drawersOpen.value) drawersOpen.value = false
}

onMounted(() => {
  let stored = ''
  try {
    stored = localStorage.getItem(STORAGE_KEY) || ''
  } catch { /* 拿不到就当跟随系统 */ }
  mode.value = stored === 'light' || stored === 'dark' ? stored : 'system'
  applyTheme()
  mq.addEventListener('change', onSysChange)
  window.addEventListener('keydown', onKey)
})

onUnmounted(() => {
  mq.removeEventListener('change', onSysChange)
  window.removeEventListener('keydown', onKey)
  document.body.classList.remove('ui-drawers-open')
})
</script>

<style scoped>
/* ── 定位与热区纪律（test-guard 终验 D① 修）────────────────────────────
   浮动层曾是「右上角两个带文字的宽按钮」，正好压住右栏 tab 头（审计/成片）的
   中心热区，E2E 只能 force 点击绕过。修法：
   · 贴**右缘细条**（图标化 22px 宽，文字进 title / aria-label）+ **垂直居中** ——
     右缘纵向两端是热区密集区（顶栏按钮 / tab 头 / 胶片条缩略图），中间一档
     只有行内容的右侧内边距（宽屏：栏外留白 10px + 边框 + colbody 8px ≈ 纯内边距），
     细条只蹭到行/按钮最外缘几个像素，任何可点元素的中心热区零遮挡。
   · wrapper pointer-events: none：缝隙、圆角、阴影**永不**拦截点击；
     只有按钮自己收事件（pointer-events: auto）。
   · z-index 50 保持在抽屉遮罩（35）之上 —— 抽屉开着也能点「收起」。 */
.uitools {
  position: fixed;
  right: 0;
  top: 50%;
  transform: translateY(-50%);
  z-index: 50;
  display: flex;
  flex-direction: column;
  gap: 6px;
  pointer-events: none;
}
.uibtn {
  pointer-events: auto;
  width: 22px;
  height: 30px;
  padding: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font: inherit;
  font-size: 12px;
  cursor: pointer;
  border: 1px solid var(--line);
  border-right: none;
  border-radius: 8px 0 0 8px;
  background: var(--card);
  color: var(--ink);
  box-shadow: var(--shadow);
}
.uibtn:hover { border-color: var(--lime); }
.uibtn.on { border-color: var(--lime); background: var(--hover); }

/* 抽屉遮罩：只在小屏出现（宽屏没有抽屉，遮了也没东西可看） */
.uid-scrim {
  display: none;
}
@media (max-width: 1279px) {
  .uid-scrim {
    display: block;
    position: fixed;
    inset: 0;
    z-index: 35;
    background: color-mix(in srgb, var(--ink) 35%, transparent);
  }
}
</style>
