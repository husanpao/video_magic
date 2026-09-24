<template>
  <div class="tabroot">
    <div class="summary">
      <span>{{ summary }}</span>
      <span class="spacer"></span>
      <el-button size="small" text :loading="loading" @click="load(true)">刷新</el-button>
    </div>
    <div class="tabbody">
      <div v-if="err" class="emptyrow">道具数据获取失败：{{ err }}</div>
      <div v-else-if="!state.props.length" class="emptyrow">
        <b>还没有道具实体</b><br>
        道具由「拆镜」阶段从正文抽取（名称 / 归属角色 / 外观描述）；
        同一道具在所有镜头里复用<b>同一段</b>外观文字，避免「金箍棒每镜长得不一样」。<br>
        正文没给外观的道具会被标成「⚠️ 推断」（保守推断的通用形象），需要人工复核。
      </div>
      <template v-else>
        <div v-for="x in state.props" :key="x.id" class="scard">
          <div class="shead">
            <b>{{ x.name }}</b><code>{{ x.id }}</code>
            <el-tag v-if="x.inferred" size="small" type="warning">⚠️ 推断</el-tag>
            <span class="spacer"></span>
            <el-tag v-if="x.owner" size="small">{{ x.owner }}</el-tag>
            <el-tag size="small" type="info">{{ x.shot_count }} 镜</el-tag>
          </div>
          <div v-if="x.inferred" class="stylewarn">
            <b>⚠️ 推断</b>：正文没给这个道具的外观，下面是保守推断的通用形象 ——
            <b>需人工复核</b>（错了会一路污染用到它的每一镜）。
          </div>
          <AssetEdit kind="prop" :id="x.id" v-model="editText[x.id]" label="描述（出图提示词来源）" :rows="4" />
      <AssetCandidates kind="prop" :id="x.id" />
        </div>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 道具（新功能，数据源 /api/props）。
 *
 * 与「场景」同构：`plan.py` 的 `_extract_props()` 从正文抽出关键道具 → 一段权威英文外观，
 * 用到该道具的镜头注入同一段文字（跨镜一致性锚点）。
 * ★ `inferred === true` = 正文没给外观、是我们保守推断的通用形象 → 显著标出，需人工复核。
 */
import { computed, onMounted, ref } from 'vue'
import { ElButton, ElTag } from 'element-plus'
import { refreshProps, state } from '@/stores/app'

const loading = ref(false)
const err = ref('')

const summary = computed(() => {
  if (err.value) return `道具数据获取失败：${err.value}`
  if (!state.props.length) return loading.value ? '加载中…' : '本章还没抽出道具（跑一次「拆镜」会自动抽）'
  return `${state.props.length} 个道具 · 出现于 ${state.propsShotsWith} 镜`
})

/** 首次进入拉一次；之后靠切换刷新或「刷新」按钮。 */
async function load(force = false) {
  if (loading.value) return
  if (!force && state.props.length) return
  loading.value = true
  err.value = ''
  try {
    await refreshProps()
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

// 每个prop一份可编辑副本 —— 初值取**实体自己的 description**（不是 assets 里的 prompt）
const editText = reactive<Record<string, string>>({})
watch(
  () => state.props,
  (rows) => {
    for (const r of rows) {
      if (editText[r.id] === undefined) editText[r.id] = r.description || ''
    }
  },
  { immediate: true },
)
</script>

<style scoped>
.stylewarn {
  margin-top: 6px;
  padding: 7px 10px;
  border-radius: var(--radius-sm);
  font-size: 11.5px;
  line-height: 1.6;
  background: color-mix(in srgb, var(--warn) 10%, var(--card));
  border: 1px solid color-mix(in srgb, var(--warn) 28%, var(--card));
  color: var(--warn);
}
</style>
