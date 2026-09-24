<!--
  顶栏：项目选择 + 阶段 stepper + 进度 + 撤销/重做 + 停止 + 清空重置。
  逐条对应旧 vm/static/index.html 的：
    · <header class="topbar"> 标记 + <div id="warn" class="banner">（第 288-303 行）
    · renderStageButtons()（676-689）· refreshStatus() 里顶栏那一段（611-647）
    · runStage()（690-701）· $('stop').onclick（702-710）
  进度条改用一个真实数字 + Element Plus 不确定态：
  旧文件在没有 total 时把 5% 画成宽度（假百分比），这里没有真实进度就用 indeterminate。

  本版改动（U7 / U4）：
  · 6 个孤立按钮 → **阶段 stepper**：一眼看到流水线走到哪一步、卡在缺什么
  （readiness 的 per-stage 缺失信息直接挂在步进节点的 title 上），
  运行中当前阶段高亮并挂**真实** done/total（不造假百分比）。
  · 常驻「撤销 / 重做」按钮 + 顶栏右侧 `?` 快捷键帮助（useHotkeys 的自述表）。
-->
<template>
  <ResetDialog v-model="showReset" ref="resetDlg" />
  <SettingsDialog v-model="settingsOpen" :project="state.project" />
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

    <!-- 阶段 stepper（U7）。每一步都可点（= 旧的阶段按钮），未就绪的挂 ⚠ 与原因 title。 -->
    <span class="stepper" data-testid="stage-stepper">
      <template v-for="(st, i) in steps" :key="st.stage">
        <span v-if="i" class="steplink" :class="{ done: st.linkDone }" />
        <button
          class="step"
          :class="{ active: st.active, warn: st.warn }"
          :disabled="stageDisabled(st.stage)"
          :title="stageTitle(st.stage)"
          @click="runStage(st.stage)"
        >
          <span class="stepno tabular">{{ st.no }}</span>
          <span class="steplabel">{{ st.label }}</span>
          <span v-if="st.warn" class="stepwarn">⚠</span>
          <span v-if="st.progText" class="stepprog tabular">{{ st.progText }}</span>
        </button>
      </template>
    </span>
    <button
      class="stages-all"
      :disabled="stageDisabled('all')"
      :title="stageTitle('all')"
      @click="runStage('all')"
    >全链</button>

    <span class="spacer" />

    <!-- 全局撤销 / 重做（U4）。常驻 —— 删除提示里那句「可点顶部撤销」终于有地方可点了。 -->
    <span class="histbtns">
      <button
        class="undobtn"
        data-testid="btn-undo"
        :disabled="!canUndo"
        :title="canUndo ? `撤销：${undoLabel}（Ctrl+Z）` : '没有可撤销的操作（Ctrl+Z）'"
        @click="onUndo"
      >↶ 撤销</button>
      <button
        class="undobtn"
        data-testid="btn-redo"
        :disabled="!canRedo"
        :title="canRedo ? `重做：${redoLabel}（Ctrl+Shift+Z）` : '没有可重做的操作（Ctrl+Shift+Z）'"
        @click="onRedo"
      >↷ 重做</button>
    </span>

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

    <!-- 设置入口（Lead 集成接线）：打开 SettingsDialog —— 全部后台硬编码配置的用户入口 -->
    <button
      class="setbtn"
      data-testid="btn-settings"
      title="设置（连接/渲染/模型/字幕/预算/风格——全部配置）"
      @click.stop="settingsOpen = true"
    >⚙ 设置</button>

    <button class="helpbtn" title="快捷键帮助（?）" @click="helpOpen = !helpOpen">?</button>
  </header>

  <div v-if="missingModules.length" class="banner">{{ bannerText }}</div>
</template>

<script setup lang="ts">
import { computed, nextTick, ref } from 'vue'
import { ElCheckbox, ElMessage, ElMessageBox, ElOption, ElProgress, ElSelect } from 'element-plus'
import ResetDialog from '@/components/ResetDialog.vue'
import { api, ApiError } from '@/api/client'
import type { Stage } from '@/api/types'
import {
  canRedo, canUndo, kindCounts, redoEdit, redoLabel, refreshStatus,
  STAGE_CN, STAGES, state, undoEdit, undoLabel,
} from '@/stores/app'
import { helpOpen } from '@/composables/useHotkeys'
import { confirmAction } from '@/composables/confirmAction'
import SettingsDialog from '@/components/SettingsDialog.vue'

/** 「强制重跑」/「试运行」两个开关：旧文件是 #force / #dry 两个 checkbox，语义直接对应 api.run 的 force/dry。 */
const force = ref(false)
const dry = ref(false)
/** 设置面板显隐（T4，Lead 集成接线）。 */
const settingsOpen = ref(false)

/* ---------------- 任务 pill ---------------- */
// 旧 refreshStatus()：运行中 · 阶段 / 已停止 (rc=) / 结束 (rc=) / 空闲
const pill = computed<{ cls: string; text: string }>(() => {
  const t = state.task
  if (state.running) return { cls: 'run', text: `运行中 · ${STAGE_CN[t.stage || ''] || t.stage || ''}` }
  if (t.stopped) return { cls: 'stop', text: `已停止 (rc=${t.exit_code ?? '?'})` }
  if (t.started_at) return { cls: 'ok', text: `结束 (rc=${t.exit_code ?? '?'})` }
  return { cls: '', text: '空闲' }
})

/* ---------------- 阶段 stepper（旧 renderStageButtons → U7） ---------------- */
/** stepper 的 5 个真实阶段（`all` 是「全链」按钮，不算一步）。 */
const STEPS: Stage[] = ['plan', 'chars', 'render', 'qc', 'assemble']

function stageReady(s: Stage): boolean {
  const r = state.readiness[s]
  return !r || r.ready
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

/** stepper 节点派生值：序号 / 未就绪⚠ / 当前阶段高亮 / 真实 done/total。
 *  ★ done/total 只挂在**当前阶段**上，且必须有真实分母 —— 没有就什么都不显示
 *  （不造假百分比，这是老文件踩过的坑）。 */
const steps = computed(() =>
  STEPS.map((s, i) => {
    const running = state.running && (state.task.stage === s || state.progress.stage === s)
    const total = state.progress.total
    return {
      stage: s,
      no: i + 1,
      label: STAGE_CN[s],
      warn: !stageReady(s),
      active: running,
      progText: running && total > 0 ? `${state.progress.done}/${total}` : '',
      // 连接线：当前阶段之前的段落视为"已走完"
      linkDone: state.running
        ? STEPS.findIndex((x) => x === state.task.stage || x === state.progress.stage) >= i
        : false,
    }
  }),
)

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
/**
 * 启动阶段：**点了就跑**，不再弹前置确认。
 *
 * 为什么去掉原来那句 window.confirm：启动阶段不是破坏性操作（渲染有检查点、编辑可撤销），
 * 真正的刹车点在 E3 预算护栏 —— 超预算时它会弹「已花多少 / 卡在哪 / 再放行多少」等人批。
 * 每次跑阶段前都问一句"确定吗"，问到最后只会闭眼点确定；确认只留给**不可逆**动作
 * （停止 / 删除 / 清空重置，见 confirmAction 的用武之地）。
 */
async function runStage(s: Stage): Promise<void> {
  if (!state.project) return
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
    window.setTimeout(() => { void refreshStatus() }, 300)
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
  // 停止会打断正在跑的任务 —— 用统一确认（U10），不再 window.confirm
  const ok = await confirmAction({
    title: '停止任务',
    message: `停止项目「${state.project}」当前任务？（只对记录的精确 pid 发 SIGTERM，已渲染完的片段会保留）`,
    confirmText: '停止',
    danger: true,
  })
  if (!ok) return
  try {
    const r = await api.stop(state.project)
    ElMessage.success(r.message || '已发送停止信号')
    window.setTimeout(() => { void refreshStatus() }, 500)
  } catch (e) {
    ElMessage.error(`停止失败：${(e as Error).message}`)
  }
}

/* ---------------- 全局撤销 / 重做（U4） ---------------- */
async function onUndo(): Promise<void> { await undoEdit() }
async function onRedo(): Promise<void> { await redoEdit() }

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
.topbar h1 span { color: var(--lime-deep); }
.projsel { width: 190px; }

/* ---- 阶段 stepper（U7） ---- */
.stepper { display: flex; align-items: center; gap: 0; flex-wrap: wrap; }
.steplink {
  width: 14px; height: 2px; background: var(--line); flex: 0 0 auto;
}
.steplink.done { background: color-mix(in srgb, var(--lime) 70%, var(--ink)); }
.step {
  display: inline-flex; align-items: center; gap: 5px;
  font: inherit; font-size: 12px; padding: 4px 9px; cursor: pointer;
  background: var(--card); border: 1px solid var(--line); border-radius: 999px;
  color: inherit;
}
.step:hover:not(:disabled) { border-color: color-mix(in srgb, var(--lime) 70%, var(--ink)); }
.step:disabled { opacity: 0.55; cursor: not-allowed; }
.step .stepno {
  width: 15px; height: 15px; border-radius: 50%; flex: 0 0 auto;
  background: var(--hover); color: var(--muted);
  display: inline-flex; align-items: center; justify-content: center; font-size: 10px;
}
.step.active {
  border-color: color-mix(in srgb, var(--lime) 70%, var(--ink));
  background: var(--hover); font-weight: 600;
}
.step.active .stepno { background: var(--lime); color: var(--ink); }
.step.warn .stepwarn { color: var(--warn); font-size: 11px; }
.step .stepprog { color: var(--run); font-size: 11px; }
.stages-all {
  font: inherit; font-size: 12px; padding: 4px 11px; cursor: pointer; font-weight: 600;
  background: var(--lime); border: 1px solid color-mix(in srgb, var(--lime) 70%, var(--ink));
  border-radius: 999px; color: inherit;
}
.stages-all:disabled { opacity: 0.45; cursor: not-allowed; }

/* ---- 撤销 / 重做 ---- */
.histbtns { display: flex; gap: 4px; }
.undobtn {
  font: inherit; font-size: 11.5px; padding: 4px 8px; cursor: pointer;
  background: var(--card); border: 1px solid var(--line); border-radius: var(--radius-sm); color: inherit;
}
.undobtn:hover:not(:disabled) { border-color: color-mix(in srgb, var(--lime) 70%, var(--ink)); }
.undobtn:disabled { opacity: 0.4; cursor: not-allowed; }

.pill { padding: 2px 10px; border-radius: 999px; font-size: 12px; border: 1px solid var(--line); background: var(--surface-3); }
.pill.run { background: var(--run-bg); color: var(--run); border-color: var(--run-border); }
.pill.ok { background: var(--ok-bg); color: var(--ok); border-color: var(--ok-border); }
.pill.stop { background: var(--bad-bg); color: var(--bad); border-color: var(--bad-border); }

.pillbox {
  display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--muted);
  font-variant-numeric: tabular-nums;
}
.progw { width: 150px; display: inline-block; }
.idletrack { height: 8px; background: var(--track); border-radius: 999px; }
.pillbox :deep(.el-progress) { width: 150px; }
.pillbox :deep(.el-progress-bar__outer) { background: var(--track); }
.pillbox :deep(.el-progress-bar__inner) { background: linear-gradient(90deg, var(--lime-deep), var(--lime)); }

.stopbtn {
  font: inherit; font-size: 12px; padding: 4px 10px; cursor: pointer; border-radius: var(--radius-sm);
  background: var(--card); border: 1px solid var(--bad-border); color: var(--bad);
}
.stopbtn:disabled { opacity: 0.45; cursor: not-allowed; }

.banner {
  margin: 0; padding: 7px 14px; font-size: 12px;
  background: var(--warn-bg); border-bottom: 1px solid var(--warn-border); color: var(--warn);
}
.resetbtn {
  font: inherit; font-size: 11.5px; padding: 5px 11px; border-radius: 8px; cursor: pointer;
  border: 1px solid color-mix(in srgb, var(--bad) 45%, var(--line));
  background: transparent; color: var(--bad); font-weight: 600;
}
.resetbtn:hover { background: var(--bad-bg); border-color: var(--bad); }

/* 设置入口（T4 预留位）：中性样式，等接线后不需要再调 */
.setbtn {
  font: inherit; font-size: 11.5px; padding: 5px 10px; border-radius: 8px; cursor: pointer;
  border: 1px solid var(--line); background: var(--card); color: var(--ink);
}
.setbtn:hover { border-color: color-mix(in srgb, var(--lime) 70%, var(--ink)); }

.helpbtn {
  font: inherit; font-size: 12px; width: 26px; height: 26px; padding: 0; cursor: pointer;
  border-radius: 50%; border: 1px solid var(--line); background: var(--card); color: var(--muted);
}
.helpbtn:hover { color: var(--ink); border-color: color-mix(in srgb, var(--lime) 70%, var(--ink)); }
</style>
