<template>
  <div class="tabroot">
    <div class="summary">
      <label class="chk"><input v-model="follow" type="checkbox"> 跟随</label>
      <el-button size="small" text @click="clear">清屏</el-button>
      <span class="spacer"></span>
      <span class="small">日志 {{ fmtSize(bytes) }}</span>
    </div>
    <pre ref="preEl" class="logpre">{{ state.logText }}</pre>
  </div>
</template>

<script setup lang="ts">
/**
 * 日志面板。
 *
 * 移植自 index.html：`pullLog()` 的「跟随 / 大小」、`$('logClear')` 的清屏、`#log` 的等宽样式。
 * ★ `state.logText` 由 App.vue 的轮询（pullLog）持续追加，这里**只负责渲染与滚动**。
 */
import { computed, nextTick, ref, watch } from 'vue'
import { ElButton } from 'element-plus'
import { fmtSize, state } from '@/stores/app'

/** 跟随（默认开）：新日志自动滚到底。 */
const follow = ref(true)
const preEl = ref<HTMLPreElement | null>(null)

/** 已载入日志的字节数（UTF-8）。旧文件显示的是服务端日志文件大小。 */
const bytes = computed(() => new TextEncoder().encode(state.logText).length)

async function scrollToBottom() {
  await nextTick()
  const el = preEl.value
  if (el) el.scrollTop = el.scrollHeight
}

watch(
  () => state.logText,
  () => {
    if (follow.value) void scrollToBottom()
  },
)
// 重新打开跟随时立刻贴底，不用等下一行日志
watch(follow, (on) => {
  if (on) void scrollToBottom()
})

/** 旧文件 $('logClear').onclick：只清显示，不动服务端 offset（后续日志继续追加）。 */
function clear() {
  state.logText = ''
}
</script>

<style scoped>
.logpre {
  flex: 1 1 auto;
  min-height: 200px;
  margin: 0;
  padding: 8px 10px;
  overflow: auto;
  font: 11.5px/1.55 ui-monospace, Menlo, Consolas, monospace;
  white-space: pre-wrap;
  word-break: break-all;
  color: var(--text-2);
  background: var(--surface-2);
}
</style>
