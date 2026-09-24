<!--
  场景 / 道具的**可编辑描述**。
  · description 就是出图提示词的来源 —— 不让改的话，抽卡只能抽到"系统以为的样子"。
  · ★ 改完必须说清影响面（用户会以为改了就生效）：
      description 是**拆镜时**逐字注入每镜提示词的，改它**不动**已有的 shots/*.json；
      要让锚定生效得重跑「拆镜」（届时相关镜头会变「需重渲」）。
      但它**立即**影响抽卡出的概念图。
-->
<template>
  <div class="ae">
    <div class="aerow">
      <label>{{ label }}</label>
      <button class="aebtn" :disabled="!dirty || busy" @click="save">
        {{ dirty ? '保存' : '已保存' }}
      </button>
      <button v-if="dirty" class="aebtn ghost" :disabled="busy" @click="reset">撤销</button>
    </div>
    <textarea
      v-model="text"
      class="aebox"
      spellcheck="false"
      :rows="rows"
      :placeholder="placeholder"
      @keydown.stop
    />
    <div v-if="dirty" class="aehint">
      ⚠ 改 description 只影响**后续拆镜与出图**；已生成的镜头提示词不变，
      要让锚定生效需重跑「拆镜」（相关镜头届时会变「需重渲」）。
    </div>
    <div v-else-if="saved" class="aehint ok">{{ saved }}</div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '@/api/client'
import { refreshProps, refreshScenes, state } from '@/stores/app'

const props = defineProps<{
  kind: 'scene' | 'prop'
  id: string
  modelValue: string
  label?: string
  rows?: number
  placeholder?: string
}>()
const emit = defineEmits<{ (e: 'update:modelValue', v: string): void }>()

const busy = ref(false)
const saved = ref('')
const original = ref(props.modelValue)
const text = computed({
  get: () => props.modelValue,
  set: (v: string) => emit('update:modelValue', v),
})
const dirty = computed(() => text.value.trim() !== original.value.trim())

watch(() => props.modelValue, (v) => { if (!dirty.value) original.value = v })

function reset() {
  text.value = original.value
  saved.value = ''
}

async function save() {
  busy.value = true
  try {
    const fn = props.kind === 'scene' ? api.sceneUpdate : api.propUpdate
    const r = await fn(state.project, props.id, { description: text.value })
    original.value = text.value
    saved.value = String(r.message || '已保存')
    ElMessage.success(saved.value)
    // 描述变了 → 出图提示词也变了，刷新素材状态
    if (props.kind === 'scene') await refreshScenes()
    else await refreshProps()
  } catch (e) {
    ElMessage.error(`保存失败：${(e as Error).message}`)
  } finally {
    busy.value = false
  }
}
</script>

<style scoped>
.ae { margin-top: 7px; }
.aerow { display: flex; align-items: center; gap: 6px; margin-bottom: 4px; }
.aerow label { font-size: 11px; color: var(--muted); }
.aebtn {
  font: inherit; font-size: 11px; padding: 2px 9px; border-radius: 6px; cursor: pointer;
  border: 1px solid var(--line); background: var(--card); color: var(--ink);
}
.aebtn.ghost { background: transparent; color: var(--muted); }
.aebtn:hover:not(:disabled) { border-color: color-mix(in srgb, var(--lime) 70%, var(--ink)); }
.aebtn:disabled { opacity: .5; cursor: not-allowed; }
.aebox {
  width: 100%; font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 11px;
  line-height: 1.5; border: 1px solid var(--line); border-radius: 8px; padding: 6px 8px;
  resize: vertical; color: #374151;
}
.aehint { margin-top: 4px; font-size: 10.5px; line-height: 1.5; color: var(--warn); }
.aehint.ok { color: var(--ok); }
</style>
