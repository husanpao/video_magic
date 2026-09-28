<template>
  <!-- W4 成片与审片屏：播放器 + 问题清单同屏。 -->
  <section class="ws" data-testid="ws-review">
    <header class="wshead">
      <h2>成片与审片</h2>
      <span class="dim small" data-testid="rv-project">{{ state.project || '（未选项目）' }}</span>
      <span v-if="gate" class="badge" :class="gate.cls">{{ gate.text }}</span>
      <span class="spacer" />
      <button class="small" :disabled="busy" data-testid="rv-run-qc" @click="runStage('qc')">
        ▶ 重跑质检
      </button>
      <button class="small" :disabled="busy" data-testid="rv-run-assemble" @click="runStage('assemble')">
        ▶ 重新合成
      </button>
    </header>

    <p v-if="!hasFinal" class="notice">
      还没有成片。这一屏看的是<b>⑤ 合成之后</b>的东西 ——
      上面「重新合成」会把 <code>clips/</code> 里的片段按镜头表拼成 <code>EPNN.mp4</code> 并压字幕；
      也可以先从工作台跑完「渲染」。
    </p>

    <div class="rvgrid">
      <div class="rvmain" data-testid="rv-player">
        <FinalTab ref="finalRef" wide />
      </div>
      <div class="rvside" data-testid="rv-findings">
        <div class="rvhalf rvhalf-qc"><QcTab @select="jump" /></div>
        <div class="rvhalf rvhalf-audit"><AuditTab @select="jump" /></div>
      </div>
    </div>

    <!-- 点了问题却跳不过去时的唯一反馈。不弹 ElMessage：
         审片时手里一直在点，弹窗会把注意力从画面上抢走。 -->
    <p v-if="hint" class="dim small" data-testid="rv-hint">{{ hint }}</p>
  </section>
</template>

<script setup lang="ts">
/**
 * W4「成片与审片」屏 —— 流程的最后一站（`docs/全流程整合与实施计划.md:§五 F3.1`）。
 *
 * 为什么要把这三样凑成一屏，而不是继续在右栏 420px 里翻 tab：
 *   审片这个动作本质是**边播边查问题**。可原来播放器在「成片」tab、
 *   问题在「质检」tab、台词保真在「审计」tab，三个 tab 互斥 ——
 *   看到"1-3-02 台词丢了"就得切 tab、找到那一行、再切回来，播放器还重新加载。
 *   现在播放器占主区（放大到 58vh），右列是质检 + 审计两张清单，
 *   **点任一条 finding 直接播到那一镜**（`FinalTab.seekToShot`）。
 *
 * ★ 三块内容都是**复用现成组件**，不是重写：
 *   `FinalTab`（播放器 + 分段时间线 + hover 预览）、`QcTab`、`AuditTab`（含完备性矩阵）
 *   本来就只读 store、自己 onMounted 首拉，所以可以直接嵌到任何容器里。
 *   复制一份"审片专用播放器"是这类界面最常见的腐烂起点。
 *   屏顶也因此**没有**下载按钮：下载链接必须跟着播放器当前所选的那一集走，
 *   而"当前是哪一集"只有 `FinalTab` 自己知道 —— 在这里再放一个就等于放第二个真相。
 *
 * ★ 这里自己挂一个轮询订阅：`App.vue` 的心跳只刷 `state.tab`（那是**工作台右栏**的 tab），
 *   在本屏跑质检时那份判据完全对不上，于是"跑完了清单不动"。
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import QcTab from '@/components/tabs/QcTab.vue'
import AuditTab from '@/components/tabs/AuditTab.vue'
import FinalTab from '@/components/tabs/FinalTab.vue'
import { api } from '@/api/client'
import type { Stage } from '@/api/types'
import { confirmAction } from '@/composables/confirmAction'
import { requestBudgetApproval } from '@/composables/budgetApproval'
import { every } from '@/composables/useTicker'
import {
  errText, refreshAudit, refreshCompleteness, refreshQc, refreshShots, refreshStatus, setError, state,
} from '@/stores/app'

/** 只用到 seekToShot；用 InstanceType 拿真实暴露类型，不手写接口（手写的那份会漂）。 */
const finalRef = ref<InstanceType<typeof FinalTab> | null>(null)
const busy = ref(false)
const loading = ref(false)
const hint = ref('')

const hasFinal = computed(() => (state.finals || []).length > 0)

const gate = computed<{ cls: string; text: string } | null>(() => {
  const a = state.audit
  if (!a || !a.has_audit) return null
  return a.summary.gate_passed
    ? { cls: 'ok', text: `审计通过 · ${a.findings.length} 条发现` }
    : { cls: 'bad', text: `审计未通过 · ${a.findings.length} 条发现` }
})

/** finding → 播放头。跳不动就说出为什么：一集一章，那一镜所属的章还没有对应成片。 */
function jump(id: string): void {
  if (!id) return
  state.selected = id
  if (finalRef.value?.seekToShot(id)) {
    hint.value = ''
    return
  }
  hint.value = `「${id}」跳不过去 —— 一集一章，第 ${id.split('-')[0]} 章还没有对应的成片`
    + '（先跑「渲染」把这一章的片段出齐，再跑「合成」）。已把它选中，可在工作台处理。'
}

const STAGE_LABEL: Record<string, string> = { qc: '质检', assemble: '合成' }

async function runStage(stage: Stage): Promise<void> {
  const label = STAGE_LABEL[stage] || stage
  const extra = stage === 'assemble'
    ? '\n合成会把全部片段的字幕一起重做（不动镜头提示词，不会触发重渲）。'
    : '\n质检只读已生成的片段，不花 GPU。'
  const ok = await confirmAction({
    title: `跑「${label}」`,
    message: `对「${state.project}」跑一次${label}？${extra}`,
    confirmText: '开始',
  })
  if (!ok) return
  await startStage(stage, false)
}

/** 超预算会回 409 + needs_approval → 批准后**重试同一次**（与 CharsTab 抽卡同一套语义）。 */
async function startStage(stage: Stage, retried: boolean): Promise<void> {
  const label = STAGE_LABEL[stage] || stage
  busy.value = true
  try {
    const r = await api.run(state.project, stage)
    ElMessage.success(r.message || `已启动${label}`)
    await refreshStatus()
  } catch (e) {
    if (!retried && (await requestBudgetApproval(e, state.project, stage)) === 'approved') {
      await startStage(stage, true)
      return
    }
    setError(`${label}：${errText(e)}`)
  } finally {
    busy.value = false
  }
}

let offTick: (() => void) | null = null

/**
 * 首拉。★ 必须跟着 `state.project` 再走一次：
 * 子组件的 onMounted **早于** App.vue 的 `loadProjects()` 落地，
 * 直接打开/刷新 `#/review` 时那一刻项目还是空串 —— 拉到的是"没有质检结果"，
 * 而且之后再也没有人补拉（本屏的轮询只在跑任务时刷）。
 * （与 StyleView / ChaptersView 同一条规矩。）
 */
async function loadAll(): Promise<void> {
  if (loading.value) return
  loading.value = true
  try {
    await refreshStatus() // 成片清单（几集、多大、什么时候合成）
    await refreshShots()  // 播放器分段的输入
    await refreshQc()
    await refreshAudit()
  } finally {
    loading.value = false
  }
}

watch(() => state.project, () => { void loadAll() })

onMounted(() => {
  void loadAll()
  // 运行中每 4s 补一次三张清单（矩阵也在这一屏上）；空闲时不刷（本屏没有会自己变的中间态）
  offTick = every(4000, async () => {
    await refreshQc()
    await refreshAudit()
    await refreshCompleteness()
  }, { skip: () => !state.running })
})

onUnmounted(() => { offTick?.(); offTick = null })
</script>

<style scoped>
.ws {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.wshead { display: flex; align-items: center; gap: 8px; }
.wshead h2 { margin: 0; font-size: 15px; color: var(--ink); }
.spacer { flex: 1 1 auto; }
.badge {
  font-size: 10.5px;
  padding: 1px 7px;
  border-radius: 999px;
  border: 1px solid var(--line);
}
.badge.ok { color: var(--ok); background: var(--ok-bg); border-color: var(--ok-border); }
.badge.bad { color: var(--bad); background: color-mix(in srgb, var(--bad) 10%, var(--card)); border-color: color-mix(in srgb, var(--bad) 35%, var(--card)); }

.notice {
  margin: 0;
  padding: 9px 12px;
  font-size: 12px;
  line-height: 1.7;
  border: 1px solid color-mix(in srgb, var(--warn) 30%, var(--card));
  border-radius: var(--radius-sm);
  background: color-mix(in srgb, var(--warn) 8%, var(--card));
  color: var(--warn);
}
.notice code { font-family: ui-monospace, Menlo, Consolas, monospace; }

.rvgrid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 460px;
  gap: 10px;
  align-items: stretch;
  flex: 1 1 auto;
  min-height: 480px;
}
.rvmain,
.rvhalf {
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  overflow: hidden;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
.rvside {
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-height: 0;
}
/* 两张清单**按内容分配**高度：质检通常只有几行，硬分一半会露出一大片空白，
   而审计那边是 finding + 完备性矩阵，本来就更长。各自内部仍可滚。 */
.rvhalf-qc {
  flex: 0 1 auto;
  max-height: 45%;
  min-height: 120px;
}
.rvhalf-audit {
  flex: 1 1 auto;
  min-height: 180px;
}

@media (max-width: 1100px) {
  .rvgrid { grid-template-columns: minmax(0, 1fr); }
  .rvside { flex-direction: row; }
  .rvhalf { flex: 1 1 0; min-width: 0; }
}
</style>
