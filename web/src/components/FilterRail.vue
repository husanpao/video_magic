<!--
  ① 168px 左栏：筛选 pill（带计数）+ 搜索 + 底部说明。
  逐条对应旧 vm/static/index.html：
    · 左栏标记（307-311）：colhead「筛选」+ #filters + .railnote 文案原文
    · renderFilters()（732-751）：计数取 kindOf() 派生值；点 pill 写 state.filter
    · #q 搜索框（325-328）+ 它的 input 处理（1654-1657）：写 state.q（走 store 的 visibleShots 过滤）
  说明：旧文件用 debounce(120ms) 是因为它得手动重渲染；这里 state.q 是响应式的，不需要防抖。
-->
<template>
  <aside class="col">
    <div class="colhead">筛选</div>

    <div class="colbody">
      <!-- 多集/多章：只在 novel/ 下真有 2 章以上时才出现，单章项目不占位置。
           章号从**镜头 id 首段**取（`2-1-03` → 第 2 章），比读文件名稳。 -->
      <template v-if="state.chapters.length > 1">
        <div class="subhead">章节</div>
        <button
          class="fpill"
          :class="{ active: state.chapter === null }"
          :title="`全部 ${state.shots.length} 镜`"
          @click="state.chapter = null"
        >
          <span>全部章</span><span class="n tabular">{{ state.shots.length }}</span>
        </button>
        <button
          v-for="c in state.chapters"
          :key="c.no"
          class="fpill"
          :class="{ active: state.chapter === c.no }"
          :title="c.title + (c.has_shots ? `（${c.shots} 镜）` : '（还没拆镜）')"
          @click="state.chapter = c.no"
        >
          <span>第{{ c.no }}章</span><span class="n tabular">{{ c.shots }}</span>
        </button>
        <div class="subhead">状态</div>
      </template>

      <button
        v-for="[k, label] in FILTERS"
        :key="k"
        class="fpill"
        :class="{ active: state.filter === k }"
        :data-f="k"
        :title="`只看「${label}」的镜头（${countOf(k)} 镜）`"
        @click="pick(k)"
      >
        <span>{{ label }}</span>
        <span class="n tabular">{{ countOf(k) }}</span>
      </button>

      <div class="search" title="在台词 / 旁白 / 镜头号 / 角色 / 景别 / 运镜里搜">
        <span class="dim small">搜索</span>
        <input v-model="state.q" type="text" placeholder="台词 / 镜头号 / 角色">
        <button v-if="state.q" class="ghost small" title="清空搜索" @click="state.q = ''">✕</button>
      </div>
    </div>

    <div class="railnote">
      状态按「产物 + 指纹 + 质检 + 任务态」实时派生，不读数据库状态字段。★「需重渲」= 指纹与产物不一致（这是我们比 printfilm 多的一档）。
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { FILTERS, chapterOf, kindOf, state } from '@/stores/app'

/** 计数：all = 全部镜头数，其余取派生档计数（旧 renderFilters 的 c[k]）。 */
// 计数要**跟着当前章节走**：切到第 2 章时，「已生成 46」这种数字如果还是全片口径，
// 用户会以为第 2 章也有 46 镜。所以这里先按章过滤，再统计状态档。
const chapterScoped = computed(() =>
  state.chapter === null
    ? state.shots
    : state.shots.filter((s) => chapterOf(s.id) === state.chapter),
)

function countOf(k: string): number {
  if (k === 'all') return chapterScoped.value.length
  let n = 0
  for (const s of chapterScoped.value) if (kindOf(s) === k) n++
  return n
}

function pick(k: string): void {
  state.filter = k
}
</script>

<style scoped>
.fpill {
  display: flex; align-items: center; justify-content: space-between; gap: 6px;
  width: 100%; margin-bottom: 2px; padding: 5px 8px; cursor: pointer; text-align: left;
  font: inherit; font-size: 12px; color: inherit;
  border: 1px solid transparent; background: transparent; border-radius: 8px;
}
.fpill:hover { background: var(--hover); }
.fpill.active {
  background: var(--lime); border-color: color-mix(in srgb, var(--lime) 70%, var(--ink)); font-weight: 600;
}
.fpill .n { color: var(--muted); font-variant-numeric: tabular-nums; }
.fpill.active .n { color: var(--ink); }

.search {
  display: flex; align-items: center; gap: 6px; margin-top: 8px;
  border: 1px solid var(--line); border-radius: 999px; padding: 3px 10px; background: var(--card);
}
.search input { flex: 1; min-width: 0; border: none; outline: none; background: transparent; font: inherit; font-size: 12px; }
.search .ghost {
  border: none; background: transparent; color: var(--muted); cursor: pointer; padding: 0 2px; font-size: 12px;
}

.subhead {
  font-size: 10.5px; color: var(--muted); margin: 7px 0 3px; font-weight: 600;
  letter-spacing: .04em;
}
.subhead:first-child { margin-top: 0; }
.railnote {
  flex: 0 0 auto; padding: 8px 10px; font-size: 11px; color: var(--muted);
  line-height: 1.6; border-top: 1px solid var(--line);
}
</style>
