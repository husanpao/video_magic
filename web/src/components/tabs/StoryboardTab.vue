<!--
  分镜图 tab（渲染前审片）。
  · 数据：`state.storyboard`（由 vm/storyboard.py 的指纹索引派生）
  · 为什么值钱：52 镜渲一遍视频是 **35 分钟 GPU**；先出 52 张静态图只要 **约 9 分钟**
    （实测单图 10s vs 单段视频 40s）。**在花钱渲视频前先看画面**。
  · ⚠️ **必须如实标注局限**：分镜图是**纯文生图**，而视频是**参考图条件生成**。
    两者能对上「谁 / 在干什么 / 什么场景」，**对不上「从哪个机位、什么光」**。
    不标出来的话，用户会以为"图上什么样，片里就什么样"，反而误判。
-->
<template>
  <div class="sbtab">
    <div class="summary">
      <span>{{ summary }}</span>
      <span class="spacer" />
      <el-button size="small" :loading="loading" @click="load(true)">刷新</el-button>
    </div>

    <div v-if="state.storyboardModel" class="hintline model">
      模型 <code>{{ state.storyboardModel.unet }}</code> ·
      {{ state.storyboardModel.width }}×{{ state.storyboardModel.height }} ·
      {{ state.storyboardModel.steps }} 步
    </div>

    <el-alert type="warning" :closable="false" class="lim">
      <template #title>
        分镜图是<strong>纯文生图</strong>，视频是<strong>参考图条件生成</strong> —— 两者能对上「谁 / 在干什么 / 什么场景」，
        <strong>对不上「从哪个机位、什么光」</strong>。用它查粗错（角色错、动作错、场景错），别拿它当成片预览。
      </template>
    </el-alert>

    <div v-if="!state.storyboard.length" class="emptyrow">
      <b>还没有分镜图</b><br>
      分镜图由 Qwen-Image 按镜头的画面段逐镜生成，用于<strong>渲染视频之前</strong>审片。<br>
      命令行：<code>python3 -m vm.storyboard 西游记 --only 1-1-01</code>（去掉 <code>--only</code> 跑全部）
    </div>

    <div v-else class="grid">
      <div v-for="r in rows" :key="r.id" class="cell" :class="{ missing: r.status === 'missing' }">
        <img
          v-if="r.status === 'current' || r.status === 'stale'"
          :src="api.storyboardUrl(state.project, r.id, r.at || 0)"
          loading="lazy" alt=""
        >
        <div v-else class="ph">{{ r.status === 'bad-prompt' ? '提示词不合格' : '未出图' }}</div>
        <div class="cap">
          <span class="sid">{{ r.id }}</span>
          <span class="spacer" />
          <span v-if="r.status === 'current'" class="tag ok">已出图</span>
          <span v-else-if="r.status === 'stale'" class="tag warn">需重出</span>
          <span v-else-if="r.status === 'bad-prompt'" class="tag bad">提示词</span>
          <span v-else class="tag idle">未出图</span>
        </div>
        <div v-if="r.seconds" class="small dim num">{{ r.seconds.toFixed(1) }}s</div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api } from '@/api/client'
import { state, refreshStoryboard } from '@/stores/app'

const loading = ref(false)

const rows = computed(() => {
  // 按镜头表顺序展示（索引里可能没有的镜头也要出现，标 missing）
  const byId = new Map(state.storyboard.map((r) => [r.id, r]))
  return state.shots.map(
    (s) => byId.get(s.id) ?? ({ id: s.id, status: 'missing' as const }),
  )
})

const summary = computed(() => {
  if (!state.storyboard.length) return '还没有分镜图'
  const c = state.storyboard.reduce<Record<string, number>>((a, r) => {
    a[r.status] = (a[r.status] || 0) + 1
    return a
  }, {})
  const parts = Object.entries(c).map(([k, v]) => `${STATUS_CN[k] || k} ${v}`)
  return `${state.storyboard.length} 张 · ${parts.join(' · ')}`
})

const STATUS_CN: Record<string, string> = {
  current: '已出图', stale: '需重出', missing: '未出图', 'bad-prompt': '提示词不合格',
}

async function load(force = false) {
  if (loading.value) return
  loading.value = true
  try {
    if (force || !state.storyboard.length) await refreshStoryboard()
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.sbtab { display: flex; flex-direction: column; gap: 8px; }
.summary { display: flex; align-items: center; gap: 8px; font-size: 12px; }
.model code { font-size: 10.5px; }
.lim { font-size: 11.5px; line-height: 1.6; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); gap: 8px; }
.cell { border: 1px solid var(--line); border-radius: 8px; overflow: hidden; background: #fbfbfc; }
.cell.missing { opacity: .62; }
.cell img { width: 100%; aspect-ratio: 16/9; object-fit: cover; display: block; }
.cell .ph {
  width: 100%; aspect-ratio: 16/9; display: flex; align-items: center; justify-content: center;
  background: #eef0f3; color: var(--muted); font-size: 11px;
}
.cap { display: flex; align-items: center; gap: 4px; padding: 3px 5px; font-size: 10.5px; }
.cap .sid { font-variant-numeric: tabular-nums; }
.tag { font-size: 9.5px; padding: 0 4px; border-radius: 4px; }
.tag.ok { background: #e6f2d0; color: var(--ok); }
.tag.warn { background: #fdf3df; color: var(--warn); }
.tag.bad { background: #fdeceb; color: var(--bad); }
.tag.idle { background: #eef0f3; color: var(--muted); }
.cell > .small { padding: 0 5px 4px; }
</style>
