<template>
  <section class="sect" data-testid="scene-list">
    <div class="secttitle">
      <b>场景</b>
      <span class="dim small">{{ state.scenes.length }} 个 · 已归属 {{ state.scenesAssigned }}/{{ state.scenesTotal }} 镜</span>
    </div>
    <div v-if="!state.scenes.length" class="emptyrow">
      <b>还没有场景实体</b><br>
      场景由「拆镜」阶段从正文抽取（地点/时间/光线/氛围），同场景的镜头共享同一段场景描述。<br>
      它和镜头号里的「场次号」不是一回事 —— 那个只是编号。
    </div>
    <template v-else>
      <div v-for="x in state.scenes" :key="x.id" class="scard">
        <div class="shead">
          <b>{{ x.name }}</b><code>{{ x.id }}</code>
          <span class="spacer"></span>
          <el-tag size="small" type="info">{{ x.shot_count }} 镜</el-tag>
        </div>
        <div class="smeta small dim">{{ meta(x) }}</div>
        <AssetEdit kind="scene" :id="x.id" v-model="editText[x.id]" label="描述（出图提示词来源）" :rows="4" />
        <AssetCandidates kind="scene" :id="x.id" />
        <div class="hintline">这段文字会被逐字注入该场景每一镜的 detailed_description（跨镜一致性锚点）</div>
      </div>
    </template>
  </section>
</template>

<script setup lang="ts">
/**
 * 场景实体清单（W3 右栏「场景与道具」里的上半）。
 *
 * ★ 它原来是一个独立的 tab（ScenesTab），和道具 tab 逐字同构：
 *   同一个 summary 条、同一份 load()/err、同一张卡（描述 + 候选概念图）。
 *   两份各拉各的数据、各改各的空态文案，改一处必须记得改另一处 ——
 *   现在它只是**内容**，取数与错误态由父级 `ScenePropTab` 统一管，
 *   和道具并排显示在同一屏里（都是"实体描述 + 概念图"，没有分成两个 tab 的理由）。
 *
 * 数据源 `/api/scenes`；空态强调场景实体**和镜头号里的「场次号」不是一回事**。
 */
import { ElTag } from 'element-plus'
import { reactive, watch } from 'vue'
import AssetCandidates from '@/components/AssetCandidates.vue'
import AssetEdit from '@/components/AssetEdit.vue'
import type { SceneRow } from '@/api/types'
import { state } from '@/stores/app'

/** 地点 · 时间 · 光线 · 氛围（缺项直接跳过，不留悬空分隔符）。 */
function meta(x: SceneRow): string {
  return [x.location, x.time_of_day, x.lighting, x.atmosphere].filter(Boolean).join(' · ')
}

// 每个场景一份可编辑副本 —— 初值取**实体自己的 description**（不是 assets 里的 prompt）
const editText = reactive<Record<string, string>>({})
watch(
  () => state.scenes,
  (rows) => {
    for (const r of rows) {
      if (editText[r.id] === undefined) editText[r.id] = r.description || ''
    }
  },
  { immediate: true },
)
</script>
