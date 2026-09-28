<template>
  <div class="tabroot">
    <div class="summary">
      <span>{{ summary }}</span>
      <span class="spacer"></span>
      <el-button size="small" text :loading="loading" @click="load(true)">刷新</el-button>
    </div>
    <div class="tabbody">
      <div v-if="err" class="emptyrow">场景/道具数据获取失败：{{ err }}</div>
      <template v-else>
        <SceneList />
        <PropList />
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 场景与道具（合并成一个 tab）。
 *
 * 为什么合并：这两块**本来就是同一件事** —— 「拆镜」从正文抽出的实体，一段权威英文描述
 * 逐字注入用到它的每一镜，外加一组可抽可采纳的概念图。以前分两个 tab，
 * 于是同一个交互要做两遍、右栏 9 个 tab 里占掉 2 个（`docs/全流程整合与实施计划.md:§五 F3.1`：
 * 右栏要收到 ≤7）。现在上下两段并排，一屏看完，不用再切。
 *
 * 取数集中在这里：`refreshScenes()` + `refreshProps()` + `refreshAssets()`
 * （候选概念图挂在 assets 上，所以三者同源一起刷 —— 这条判据以前写在两个 tab 各自体内）。
 */
import { computed, onMounted, ref } from 'vue'
import { ElButton } from 'element-plus'
import PropList from '@/components/tabs/PropList.vue'
import SceneList from '@/components/tabs/SceneList.vue'
import { refreshAssets, refreshProps, refreshScenes, state } from '@/stores/app'

const loading = ref(false)
const err = ref('')

const summary = computed(() => {
  if (err.value) return `场景/道具数据获取失败：${err.value}`
  if (!state.scenes.length && !state.props.length) {
    return loading.value ? '加载中…' : '还没有场景与道具（跑一次「拆镜」会自动抽）'
  }
  return `${state.scenes.length} 个场景 · 归属 ${state.scenesAssigned}/${state.scenesTotal} 镜`
    + ` ｜ ${state.props.length} 个道具 · 出现于 ${state.propsShotsWith} 镜`
})

/** 首次进入拉一次；之后靠 tab 切换刷新或「刷新」按钮，不每次切都重拉整表。 */
async function load(force = false) {
  if (loading.value) return
  if (!force && (state.scenes.length || state.props.length)) return
  loading.value = true
  err.value = ''
  try {
    await refreshScenes()
    await refreshProps()
    await refreshAssets()
  } catch (e) {
    err.value = (e as Error).message
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  void load()
})
</script>
