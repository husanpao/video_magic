<!--
  剧本 tab：**无损**结构化剧本视图。
  · 每句对白带 `src_span` 指回小说原文，后端 `verify()` 做确定性逐字校验。
  · ★ 这一层**不替代小说**作为对白来源 —— 调研负面清单明确写着
    「storyforge 的摘要式链路」是架构级错误（原文第 1 步就丢，对白必丢）。
    所以它是**旁路产物**：拆镜仍直接从小说来；丢了它流水线照跑。
  · 页首把校验结论摆在最显眼处：覆盖率 / 失败数 / 门禁。**失败 > 0 时用红色，不粉饰。**
-->
<template>
  <div class="sctab">
    <div class="summary">
      <span>{{ summary }}</span>
      <span class="spacer" />
      <el-button size="small" :loading="loading" @click="load(true)">刷新</el-button>
    </div>

    <el-alert
      v-if="state.scripts.some((c) => c.has_script)"
      :type="gateOk ? 'success' : 'error'"
      :closable="false"
      class="gate"
    >
      <template #title>
        <span v-if="gateOk">
          无损校验通过：所有对白都能在小说原文里<strong>逐字</strong>找到（覆盖率 {{ overallCov }}%）
        </span>
        <span v-else>
          <strong>无损校验未通过</strong>：有对白在原文里找不到 —— 说明被改写或凭空生成了
        </span>
      </template>
    </el-alert>

    <div v-if="!hasAny" class="emptyrow">
      <b>还没有剧本</b><br>
      剧本层把小说章节整理成「场次 → 节拍 → 人物 / 对白 / 舞台指示」的<strong>可读视图</strong>。<br>
      命令行：<code>python3 -m vm.script 西游记</code><br>
      <span class="dim">它是旁路产物：拆镜仍然直接从小说走，剧本层丢了流水线照跑。</span>
    </div>

    <div v-for="c in state.scripts.filter((x) => x.has_script)" :key="c.no" class="ch">
      <div class="chhead">
        <b>第 {{ c.no }} 章 · {{ c.title }}</b>
        <span class="spacer" />
        <span class="tag info">{{ c.beats }} 节拍 / {{ c.lines }} 句</span>
        <span class="tag" :class="c.checks.gate_passed ? 'ok' : 'bad'">
          覆盖 {{ c.checks.coverage_pct ?? '-' }}%
        </span>
        <span v-if="c.checks.span_drift" class="tag warn" :title="'LLM 数错偏移，已就地修正（不算失败）'">
          偏移修正 {{ c.checks.span_drift }}
        </span>
      </div>
      <pre class="scripttext">{{ c.text }}</pre>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { refreshScript, state } from '@/stores/app'

const loading = ref(false)

const hasAny = computed(() => state.scripts.some((c) => c.has_script))
const gateOk = computed(() =>
  state.scripts.filter((c) => c.has_script).every((c) => c.checks.gate_passed !== false),
)
const overallCov = computed(() => {
  const cs = state.scripts.filter((c) => c.has_script && c.checks.coverage_pct != null)
  if (!cs.length) return '-'
  return Math.round(cs.reduce((a, c) => a + (c.checks.coverage_pct || 0), 0) / cs.length)
})
const summary = computed(() => {
  const cs = state.scripts.filter((c) => c.has_script)
  if (!cs.length) return '还没有剧本'
  const lines = cs.reduce((a, c) => a + c.lines, 0)
  return `${cs.length} 章 / ${lines} 句对白`
})

async function load(force = false) {
  if (loading.value) return
  loading.value = true
  try {
    if (force || !state.scripts.length) await refreshScript()
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.sctab { display: flex; flex-direction: column; gap: 8px; }
.summary { display: flex; align-items: center; gap: 8px; font-size: 12px; }
.gate { font-size: 11.5px; line-height: 1.6; }
.ch { border: 1px solid var(--line); border-radius: 10px; overflow: hidden; background: #fbfbfc; }
.chhead {
  display: flex; align-items: center; gap: 6px; padding: 6px 8px; font-size: 11.5px;
  background: #f4f6f2; border-bottom: 1px solid var(--line); flex-wrap: wrap;
}
.tag { font-size: 9.5px; padding: 0 5px; border-radius: 4px; }
.tag.ok { background: #e6f2d0; color: var(--ok); }
.tag.warn { background: #fdf3df; color: var(--warn); }
.tag.bad { background: #fdeceb; color: var(--bad); }
.tag.info { background: #eef0f3; color: var(--muted); }
/* 剧本正文用等宽，逐行的 ⟨start-end⟩ 偏移才能对齐着看 */
.scripttext {
  margin: 0; padding: 8px 10px; font-size: 11.5px; line-height: 1.62;
  font-family: ui-monospace, Menlo, Consolas, monospace; white-space: pre-wrap; word-break: break-word;
  max-height: 420px; overflow: auto;
}
</style>
