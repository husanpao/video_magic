<!--
  顶栏：项目选择 + 阶段按钮 + 进度 + 停止。
  逐条对应旧 vm/static/index.html 的：
    · <header class="topbar"> 标记 + <div id="warn" class="banner">（第 288-303 行）
    · renderStageButtons()（676-689）· refreshStatus() 里顶栏那一段（611-647）
    · runStage()（690-701）· $('stop').onclick（702-710）
  进度条改用一个真实数字 + Element Plus 不确定态：
  旧文件在没有 total 时把 5% 画成宽度（假百分比），这里没有真实进度就用 indeterminate。
-->
<template>
  <ResetDialog v-model="showReset" ref="resetDlg" />
  <header class="topbar">
    <h1>漫剧<span>流水线</span></h1>

    <ElSelect
      v-model="state.project"
      class="projsel"
      size="small"
      title="选择项目"
      placeholder="选择项目"
    >
      <ElOption
        v-for="p in state.projects"
        :key="p.name"
        :label="p.name + (p.running ? ' ●' : '')"
        :value="p.name"
      />
    </ElSelect>

    <span class="pill" :class="pill.cls">{{ pill.text }}</span>

    <span class="stages">
      <button
        v-for="s in STAGES"
        :key="s"
        :class="{ primary: s === 'all' }"
        :disabled="stageDisabled(s)"
        :title="stageTitle(s)"
        @click="runStage(s)"
      >
        {{ stageLabel(s) }}
      </button>
    </span>

    <span class="spacer" />

    <span class="pillbox">
      <span class="tabular" title="镜头派生状态汇总">{{ countsText }}</span>
      <ElProgress
        v-if="hasRealProgress"
        class="progw"
        :percentage="pct"
        :stroke-width="8"
        :show-text="false"
      />
      <ElProgress
        v-else-if="state.running"
        class="progw"
        :percentage="0"
        indeterminate
        :duration="1.5"
        :stroke-width="8"
        :show-text="false"
      />
      <span v-else class="progw idletrack" />
      <span class="tabular" :title="progressTitle">{{ barText }}</span>
    </span>

    <ElCheckbox v-model="force" size="small" title="忽略指纹，重跑已有产物的镜头">强制重跑</ElCheckbox>
    <ElCheckbox v-model="dry" size="small" title="只打印将要做什么，不真正执行">试运行</ElCheckbox>

    <button
      class="stopbtn"
      :disabled="!state.running"
      :title="state.running ? '停止当前任务（只对记录的精确 pid 发 SIGTERM）' : '当前没有任务在跑'"
      @click="stopTask"
    >
      ■ 停止
    </button>

    <!-- 清空 / 重置：不可逆操作 —— 弹窗里先摆清单，再要求手输项目名 -->
    <button
      class="resetbtn"
      title="清空 / 重置这个项目的产物（会先自动备份拆镜结果与角色卡）"
      @click="openReset"
    >
      清空重置
    </button>
  </header>

  <div v-if="missingModules.length" class="banner">{{ bannerText }}</div>
</template>

<script setup lang="ts">
import { computed, nextTick, ref } from 'vue'
import { ElCheckbox, ElMessage, ElMessageBox, ElOption, ElProgress, ElSelect } from 'element-plus'
import ResetDialog from '@/components/ResetDialog.vue'
import { api, ApiError } from '@/api/client'
import type { Stage } from '@/api/types'
import { kindCounts, refreshStatus, STAGE_CN, STAGES, state } from '@/stores/app'

/** 「强制重跑」/「试运行」两个开关：旧文件是 #force / #dry 两个 checkbox，语义直接对应 api.run 的 force/dry。 */
const force = ref(false)
const dry = ref(false)

/* ---------------- 任务 pill ---------------- */
// 旧 refreshStatus()：运行中 · 阶段 / 已停止 (rc=) / 结束 (rc=) / 空闲
const pill = computed<{ cls: string; text: string }>(() => {
  const t = state.task
  if (state.running) return { cls: 'run', text: `运行中 · ${STAGE_CN[t.stage || ''] || t.stage || ''}` }
  if (t.stopped) return { cls: 'stop', text: `已停止 (rc=${t.exit_code ?? '?'})` }
  if (t.started_at) return { cls: 'ok', text: `结束 (rc=${t.exit_code ?? '?'})` }
  return { cls: '', text: '空闲' }
})

/* ---------------- 阶段按钮（旧 renderStageButtons） ---------------- */
function stageReady(s: Stage): boolean {
  const r = state.readiness[s]
  return !r || r.ready
}

function stageLabel(s: Stage): string {
  // 旧文件：textContent = STAGE_CN[s]（all 也是「全链」），未就绪再补一个 ⚠
  return STAGE_CN[s] + (stageReady(s) ? '' : ' ⚠')
}

function stageTitle(s: Stage): string {
  if (state.running) return '有任务在跑：同项目同时只允许一个任务，先停止或等它结束'
  const r = state.readiness[s]
  if (r && !r.ready) {
    // 就绪校验失败：直接用后端给的人话原因（缺失模块名兜底）
    const miss = (r.missing || []).map((m) => `vm/${m}.py`).join('、')
    return r.message || `未就绪：缺 ${miss || '依赖模块'}`
  }
  return `启动阶段：${STAGE_CN[s]}`
}

function stageDisabled(s: Stage): boolean {
  return state.running || !stageReady(s)
}

/* ---------------- 进度（旧 refreshStatus 的 progw/bar/barText） ---------------- */
/** 有真实分母才算"真实进度"；没有分母又没在跑就什么都不画（不假造百分比）。 */
const hasRealProgress = computed(() => state.progress.total > 0)
const pct = computed(() => (hasRealProgress.value ? state.progress.pct : 0))

const barText = computed(() => {
  const p = state.progress
  if (hasRealProgress.value) return `${p.pct}% ${p.done}/${p.total}`
  if (state.running) return p.label || '进行中'
  return '—'
})

const progressTitle = computed(() => {
  const p = state.progress
  if (hasRealProgress.value) return `${p.label || p.stage}：${p.done}/${p.total}`
  if (state.running) return '任务在跑，但这一阶段没有可数的分母（不显示假百分比）'
  return '当前没有运行中的任务'
})

/** 旧 renderCounts()：完成 x/y · 进行中 n · 失败 n · 需重渲 n */
const countsText = computed(() => {
  const rows = state.shots
  if (!rows.length) return state.progress.total ? `${state.progress.done}/${state.progress.total}` : '—'
  const c = kindCounts.value
  const fails = (c.qc_failed || 0) + (c.failed || 0)
  return `完成 ${c.current || 0}/${rows.length}`
    + (c.rendering ? ` · 进行中 ${c.rendering}` : '')
    + (fails ? ` · 失败 ${fails}` : '')
    + (c.stale ? ` · 需重渲 ${c.stale}` : '')
})

/* ---------------- 依赖模块未就绪横幅（旧 #warn） ---------------- */
const missingModules = computed(() => {
  const set = new Set<string>()
  for (const s of STAGES) {
    const r = state.readiness[s]
    if (r && !r.ready) (r.missing || []).forEach((m) => set.add(m))
  }
  return [...set].sort()
})

const bannerText = computed(
  () => `依赖模块未就绪：vm/${missingModules.value.join('.py、vm/')}.py —— 对应阶段点了会返回 503（人话原因），UI/服务不受影响。`,
)

/* ---------------- 动作（旧 runStage / #stop.onclick） ---------------- */
async function runStage(s: Stage): Promise<void> {
  if (!state.project) return
  if (!window.confirm(`对项目「${state.project}」启动阶段：${STAGE_CN[s]}？`)) return
  await launch(s, false)
}

/**
 * 启动阶段。**E3：超预算时弹"暂停等人批"对话框，而不是报错。**
 *
 * 调研里那条原则是这一段的全部理由：
 *   「一次『一键成片』内部连着发几十次付费调用，**中途没有任何刹车点**」
 *   「**一道通向不了人的告警，和没有告警是一回事。**」
 * 所以对话框必须把三件事直接摆出来（后端已经算好放在载荷里）：
 *   已经花了多少 / 卡在哪一步 / **再放行多少才能继续**。
 */
async function launch(s: Stage, approved: boolean): Promise<void> {
  try {
    const r = await api.run(state.project, s, { force: force.value, dry: dry.value })
    ElMessage.success(r.message || `已启动：${state.project} / ${STAGE_CN[s]}`)
    state.tab = 'log' // 旧文件跑完阶段自动切到「日志」tab
    window.setTimeout(() => { void refreshStatus(true) }, 300)
  } catch (e) {
    const err = e as ApiError
    if (err.needsApproval && !approved) {
      const p = err.payload as {
        message?: string; reasons?: string[]
        spent?: { runs?: number; gpu_sec?: number; llm_tokens?: number }
        est?: { gpu_min?: number; llm_tokens?: number; shots_pending?: number }
        stage?: string
      }
      const sp = p.spent || {}
      const est = p.est || {}
      const lines = [
        `这一步要花 ${est.gpu_min ?? '?'} 分钟 GPU / 约 ${est.llm_tokens ?? '?'} tokens`,
        `（${est.shots_pending ?? '?'} 镜待处理）`,
        '',
        `已花：今天 ${((sp.gpu_sec || 0) / 60).toFixed(1)} 分钟 GPU、${sp.llm_tokens || 0} tokens（${sp.runs || 0} 次任务）`,
        `卡在：阶段 ${STAGE_CN[p.stage || ''] || p.stage || s}`,
        `再放行 ${est.gpu_min ?? '?'} 分钟 GPU / ${est.llm_tokens ?? '?'} tokens 即可继续`,
        '',
        ...(p.reasons || []).map((r) => `· ${r}`),
      ].join('\n')
      try {
        await ElMessageBox.confirm(lines, '⚠ 超出预算，需要你批准', {
          confirmButtonText: '放行这一次',
          cancelButtonText: '先不跑',
          type: 'warning',
          customClass: 'budget-approve',
        })
      } catch { return }
      try {
        await api.approve(state.project, s)
        ElMessage.info('已放行一次（下一个任务消费后即失效）')
      } catch (e2) {
        ElMessage.error(`放行失败：${(e2 as Error).message}`)
        return
      }
      await launch(s, true)   // 放行后重试一次
      return
    }
    ElMessage.error(`启动失败：${err.message}`)
  }
}

async function stopTask(): Promise<void> {
  if (!state.project) return
  if (!window.confirm(`停止项目「${state.project}」当前任务？（只对记录的精确 pid 发 SIGTERM）`)) return
  try {
    const r = await api.stop(state.project)
    ElMessage.success(r.message || '已发送停止信号')
    window.setTimeout(() => { void refreshStatus(true) }, 500)
  } catch (e) {
    ElMessage.error(`停止失败：${(e as Error).message}`)
  }
}

const showReset = ref(false)
const resetDlg = ref<InstanceType<typeof ResetDialog> | null>(null)

async function openReset() {
  showReset.value = true
  // 等对话框挂载后再拉预览
  await nextTick()
  void resetDlg.value?.load()
}
</script>

<style scoped>
/* 顶栏类名沿用旧文件（theme.css 只给了工作台/表格/胶片条的公共类，这里补齐顶栏自己的） */
.topbar {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  min-height: var(--nav-h); padding: 6px 14px;
  background: var(--card); border-bottom: 1px solid var(--line);
}
.topbar h1 { margin: 0; font-size: 15px; font-weight: 600; }
.topbar h1 span { color: #7bb500; }
.projsel { width: 190px; }

.stages { display: flex; gap: 6px; flex-wrap: wrap; }
.stages button {
  font: inherit; font-size: 12px; padding: 4px 9px; cursor: pointer;
  background: var(--card); border: 1px solid var(--line); border-radius: var(--radius-sm);
}
.stages button:hover:not(:disabled) { border-color: color-mix(in srgb, var(--lime) 70%, var(--ink)); }
.stages button.primary {
  background: var(--lime); border-color: color-mix(in srgb, var(--lime) 70%, var(--ink)); font-weight: 600;
}
.stages button:disabled { opacity: 0.45; cursor: not-allowed; }

.pill { padding: 2px 10px; border-radius: 999px; font-size: 12px; border: 1px solid var(--line); background: #eef0f3; }
.pill.run { background: #e8f1fd; color: var(--run); border-color: #bcd6f7; }
.pill.ok { background: #eef7d8; color: var(--ok); border-color: #cfe4a4; }
.pill.stop { background: #fdeceb; color: var(--bad); border-color: #eec2bf; }

.pillbox {
  display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--muted);
  font-variant-numeric: tabular-nums;
}
.progw { width: 150px; display: inline-block; }
.idletrack { height: 8px; background: #e8eaee; border-radius: 999px; }
.pillbox :deep(.el-progress) { width: 150px; }
.pillbox :deep(.el-progress-bar__outer) { background: #e8eaee; }
.pillbox :deep(.el-progress-bar__inner) { background: linear-gradient(90deg, #8fd000, var(--lime)); }

.stopbtn {
  font: inherit; font-size: 12px; padding: 4px 10px; cursor: pointer; border-radius: var(--radius-sm);
  background: #fff; border: 1px solid #e3b4b0; color: var(--bad);
}
.stopbtn:disabled { opacity: 0.45; cursor: not-allowed; }

.banner {
  margin: 0; padding: 7px 14px; font-size: 12px;
  background: #fdf3e2; border-bottom: 1px solid #f0d9ae; color: var(--warn);
}
.resetbtn {
  font: inherit; font-size: 11.5px; padding: 5px 11px; border-radius: 8px; cursor: pointer;
  border: 1px solid color-mix(in srgb, var(--bad) 45%, var(--line));
  background: transparent; color: var(--bad); font-weight: 600;
}
.resetbtn:hover { background: #fdeceb; border-color: var(--bad); }
</style>
