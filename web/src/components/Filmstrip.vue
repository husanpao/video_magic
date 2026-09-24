<!--
  底部胶片条。逐条对应旧 vm/static/index.html：
    · 标记（419-426）：.filmbar / .fshead「胶片条 N 镜 · ←/→ 切镜」/ .filmstrip
    · renderStrip()（877-905）：每镜一项；有产物画缩略图，没产物用占位**写状态文字**；
      选中项 .is-active（青柠描边）；点击 = 选中 + 打开详情；选中项自动滚进视野
    · 键盘 ←/→ 切镜（906-918）：gotoNeighbor() 在 visibleShots 上按 delta 走一位并夹住边界；
      打字时不触发 —— ★旧文件只挡 INPUT/TEXTAREA/SELECT，这里补上 isContentEditable
      （台词 / 提示词内联编辑时打字不能触发快捷键，这是调研点名的坑）
  规格指定：每镜一项来自 state.shots（96px 宽，见 theme.css .fs-item），不再随筛选隐藏。
-->
<template>
  <footer class="filmbar">
    <div class="fshead">
      <span class="tabular">胶片条 {{ state.shots.length }} 镜</span>
      <span class="spacer" />
      <span>←/→ 切镜</span>
    </div>

    <div ref="stripEl" class="filmstrip">
      <div v-if="!state.shots.length" class="emptyrow">还没有镜头。</div>

      <div
        v-for="s in state.shots"
        :key="s.id"
        class="fs-item"
        :class="{ 'is-active': state.selected === s.id }"
        :data-id="s.id"
        :title="itemTitle(s)"
        @click="pick(s.id)"
      >
        <img
          v-if="s.clip && s.clip.exists"
          loading="lazy"
          alt=""
          :src="api.thumbUrl(state.project, s.id, s.clip.mtime)"
        >
        <div v-else class="ph">{{ kindInfo(kindOf(s)).t }}</div>

        <div class="fs-cap">
          <span class="dot" :class="`tone-${kindInfo(kindOf(s)).tone}`" />
          <span class="sid">{{ s.id }}</span>
          <span class="spacer" />
          <span class="tabular">{{ s.sec }}s</span>
        </div>
      </div>
    </div>
  </footer>
</template>

<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { api } from '@/api/client'
import type { ShotRow } from '@/api/types'
import { fmtMmSs, kindInfo, kindOf, state, visibleShots } from '@/stores/app'
import { closeShot, openShot, ui } from '@/stores/ui'

const stripEl = ref<HTMLElement | null>(null)

/** 点击 = 选中 + 打开详情（旧 selectShot(id, true) → openShot） */
function pick(id: string): void {
  if (!id) return
  state.selected = id
  openShot(id)
}

function itemTitle(s: ShotRow): string {
  return `${s.id} · ${kindInfo(kindOf(s)).t} · ${fmtMmSs(s.sec)}`
}

/** 打字中？—— INPUT/TEXTAREA/SELECT **外加 contentEditable**（调研点名的坑） */
function isTyping(ev: KeyboardEvent): boolean {
  const t = ev.target as HTMLElement | null
  if (!t) return false
  const tag = (t.tagName || '').toUpperCase()
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || t.isContentEditable === true
}

/** 旧 gotoNeighbor：在**可见**镜头序列上走一位；没选中时落到第一镜；两端夹住。 */
function gotoNeighbor(delta: number): void {
  const ids = visibleShots.value.map((s) => s.id)
  if (!ids.length) return
  let i = ids.indexOf(state.selected)
  if (i < 0) i = 0
  else i = Math.max(0, Math.min(ids.length - 1, i + delta))
  state.selected = ids[i]
}

function onKeydown(e: KeyboardEvent): void {
  // 旧文件的全局 keydown 顺带处理 Esc 关弹层（弹层那边也会自己关，重复调用无副作用）
  if (e.key === 'Escape' && ui.modalOpen) { closeShot(); return }
  if (isTyping(e)) return
  if (e.key === 'ArrowLeft') { e.preventDefault(); gotoNeighbor(-1) }
  else if (e.key === 'ArrowRight') { e.preventDefault(); gotoNeighbor(1) }
}

onMounted(() => window.addEventListener('keydown', onKeydown))
onUnmounted(() => window.removeEventListener('keydown', onKeydown))

/** 选中项滚进视野（旧 renderStrip 末尾对 .fs-item.is-active 做的 scrollIntoView） */
watch(
  () => state.selected,
  async (id) => {
    if (!id) return
    await nextTick()
    const act = stripEl.value
      ? [...stripEl.value.querySelectorAll<HTMLElement>('.fs-item')].find((x) => x.dataset.id === id)
      : undefined
    act?.scrollIntoView({ block: 'nearest', inline: 'nearest' })
  },
)
</script>

<style scoped>
.fshead { font-variant-numeric: tabular-nums; }
.fs-cap .sid { font-weight: 600; color: var(--ink); }
.fs-cap .tabular { color: var(--muted); }

/* theme.css 的 .tone-* 只给文字色，状态点要的是底色（旧文件 .dot.tone-*） */
.dot { width: 8px; height: 8px; border-radius: 50%; flex: 0 0 auto; background: #c7ccd4; }
.dot.tone-ok { background: var(--ok); }
.dot.tone-warn { background: var(--warn); }
.dot.tone-bad { background: var(--bad); }
.dot.tone-run { background: var(--run); }
.dot.tone-idle { background: #c7ccd4; }
</style>
