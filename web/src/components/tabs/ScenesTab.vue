<template>
  <div class="tabroot">
    <div class="summary">
      <span>{{ summary }}</span>
      <span class="spacer"></span>
      <el-button size="small" text :loading="loading" @click="load(true)">刷新</el-button>
    </div>
    <div class="tabbody">
      <div v-if="err" class="emptyrow">场景加载失败：{{ err }}</div>
      <div v-else-if="!state.scenes.length" class="emptyrow">
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
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 场景（新功能，数据源 /api/scenes）。
 *
 * 移植自 index.html 的 `refreshScenes()` / `renderScenes()`：
 *   汇总「N 个场景 · 已归属 A/T 镜」，每场景一张卡（名称 + id + 归属镜数 + 地点/时间/光线/氛围
 *   + **完整英文 description**），卡底提示这段文字会被逐字注入。
 * 空态强调：场景来自「拆镜」抽取，**和镜头号里的「场次号」不是一回事**。
 */
import { computed, onMounted, ref } from 'vue'
import { ElButton, ElTag } from 'element-plus'
import type { SceneRow } from '@/api/types'
import { refreshScenes, state } from '@/stores/app'

const loading = ref(false)
const err = ref('')

const summary = computed(() => {
  if (err.value) return `场景加载失败：${err.value}`
  if (!state.scenes.length) return loading.value ? '加载中…' : '本章还没抽出场景（跑一次「拆镜」会自动抽）'
  return `${state.scenes.length} 个场景 · 已归属 ${state.scenesAssigned}/${state.scenesTotal} 镜`
})

/** 地点 · 时间 · 光线 · 氛围（缺项直接跳过，不留悬空分隔符）。 */
function meta(x: SceneRow): string {
  return [x.location, x.time_of_day, x.lighting, x.atmosphere].filter(Boolean).join(' · ')
}

/** 首次进入拉一次；之后靠切换刷新或「刷新」按钮。 */
async function load(force = false) {
  if (loading.value) return
  if (!force && state.scenes.length) return
  loading.value = true
  err.value = ''
  try {
    await refreshScenes()
  } catch (e) {
    err.value = (e as Error).message
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  void load()
})
import AssetCandidates from '@/components/AssetCandidates.vue'
import { reactive, watch } from 'vue'
import AssetEdit from '@/components/AssetEdit.vue'

// 每个scene一份可编辑副本 —— 初值取**实体自己的 description**（不是 assets 里的 prompt）
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
