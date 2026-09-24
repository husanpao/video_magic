<!--
  底部胶片条。逐条对应旧 vm/static/index.html：
    · 标记（419-426）：.filmbar / .fshead「胶片条 N 镜 · ←/→ 切镜」/ .filmstrip
    · renderStrip()（877-905）：每镜一项；有产物画缩略图，没产物用占位**写状态文字**；
      选中项 .is-active（青柠描边）；点击 = 选中 + 打开详情；选中项自动滚进视野
  规格指定：每镜一项来自 state.shots（96px 宽，见 theme.css .fs-item），不再随筛选隐藏。
  ★ 键盘 ←/→ 切镜已**搬家**到 composables/useHotkeys.ts（U5）：
    快捷键属于整个应用，寄生在胶片条里既找不到、组件一拆就没了。
-->
<template>
  <footer class="filmbar">
    <div class="fshead">
      <span class="tabular">胶片条 {{ state.shots.length }} 镜</span>
      <span class="spacer" />
      <span>←/→ 切镜 · 按 ? 看全部快捷键</span>
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
import { nextTick, ref, watch } from 'vue'
import { api } from '@/api/client'
import type { ShotRow } from '@/api/types'
import { fmtMmSs, kindInfo, kindOf, state } from '@/stores/app'
import { openShot } from '@/stores/ui'

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

/* .dot.tone-* 状态点样式已收编进 theme.css（U13 去重），此处不再各抄一份。 */
</style>
