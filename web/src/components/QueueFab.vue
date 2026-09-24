<!--
  右下角作业队列面板（FAB 展开）。

  ★ 2026-09-24 改为**真正的队列**（用户要求）。
  背景：抽卡类操作单次几十秒（Qwen-Image 10s/张、H3 定妆 ~40s/张），
  但用户想**连着点十几个**并且**不想盯着**。原来是同步 HTTP —— 点完一个按钮就禁用，
  人得一直等。现在点击 = 入队立即返回，由 `--stage queue` 的 drainer 顺序抽干。

  为什么之前判断错了：我曾以「同一项目同时只允许一个任务」为由认为队列是过度设计。
  那是**按渲染阶段想的**，没考虑抽卡 —— 队列解决"排队"，任务锁解决"互斥"，是两件事。

  面板里能看到：待处理 / 进行中 / 已完成 / 失败，以及最近 12 条作业（待处理的能单项取消）。
  **不画假进度**：进度来自 state.progress（后端真实上报）；没有总数时用不确定态动画。
-->
<template>
  <div class="fab" :class="{ busy: running || pending > 0 }">
    <button
      class="fabbtn"
      :title="`作业队列：待处理 ${pending} / 进行中 ${runningN}`"
      @click="expanded = !expanded"
    >
      <span class="dot" :class="{ idle: !running && !pending }" />
      <span class="txt">{{ btnLabel }}</span>
      <span v-if="pending" class="badge num">{{ pending }}</span>
    </button>

    <div v-if="expanded" class="panel">
      <div class="phead">
        <b>{{ running ? '进行中' : pending ? '排队中' : '空闲' }}</b>
        <span class="spacer" />
        <button class="x" @click="expanded = false">✕</button>
      </div>

      <template v-if="running || pending">
        <div class="prow">
          <span class="dim small">{{ running ? (progress.label || '任务') : '等待 worker' }}</span>
          <span class="spacer" />
          <span class="num small">{{ elapsed }}<template v-if="progress.total"> · {{ progress.done }}/{{ progress.total }}</template></span>
        </div>
        <el-progress
          v-if="progress.total"
          :percentage="progress.pct"
          :stroke-width="8"
          :show-text="false"
        />
        <div v-else class="indet"><i /></div>
        <!-- ★ 实时日志行（2026-09-24）：抽卡时你在「场景」tab，看不到「日志」tab 里的记录。
             所以把当前作业的最后一行日志直接摆到这里 —— 不用切 tab 就知道在做什么。 -->
        <div v-if="lastLine" class="lastlog" :title="lastLine">{{ lastLine }}</div>
      </template>

      <!-- 队列本体：点击抽卡后这里会排起来。用户不必盯着，回来一看就知道做到哪了。 -->
      <div v-if="jobs.length" class="jobs">
        <div v-for="j in jobs" :key="j.id" class="job" :class="j.status">
          <span class="jsym">{{ SYM[j.status] || '·' }}</span>
          <span class="jlabel" :title="j.note || j.label">{{ j.label }} {{ j.args.id || j.args.name || '' }}</span>
          <span class="spacer" />
          <span class="jstat dim">{{ CN[j.status] || j.status }}</span>
          <button v-if="j.status === 'pending'" class="jx" title="取消这一项" @click="cancel(j.id)">✕</button>
        </div>
      </div>
      <div v-else class="dim small">
        队列是空的。场景 / 道具的「抽卡」点一下就入队，可以连点十几个然后走开。
      </div>

      <div v-if="failed" class="dim small">失败 {{ failed }} 项（鼠标移到行上看原因）</div>

      <el-button v-if="running" size="small" type="danger" plain style="width: 100%" @click="onStop">
        ■ 停止当前任务
      </el-button>
      <el-button
        v-if="done || failed || canceled"
        size="small"
        style="width: 100%"
        @click="clear"
      >
        清空已完成记录（{{ done + failed + canceled }}）
      </el-button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '@/api/client'
import { refreshQueue, state } from '@/stores/app'
import { confirmAction } from '@/composables/confirmAction'

const expanded = ref(false)
const now = ref(Date.now())
const timer = window.setInterval(() => { now.value = Date.now() }, 1000)
// 队列状态每 3 秒拉一次 —— 别跟主轮询（2s）挤在一起
const poll = window.setInterval(() => { void refreshQueue() }, 3000)
onUnmounted(() => { window.clearInterval(timer); window.clearInterval(poll) })

const CN: Record<string, string> = {
  pending: '排队', running: '进行中', done: '完成', failed: '失败', canceled: '已取消',
}
const SYM: Record<string, string> = {
  pending: '⋯', running: '▶', done: '✓', failed: '✕', canceled: '⊘',
}

const q = computed(() => state.queue)
const running = computed(() => state.running)
const pending = computed(() => q.value?.pending ?? 0)
const runningN = computed(() => q.value?.running ?? 0)
const failed = computed(() => q.value?.failed ?? 0)
const done = computed(() => q.value?.done ?? 0)
const canceled = computed(() => q.value?.canceled ?? 0)
const progress = computed(() => state.progress)
const jobs = computed(() => (q.value?.jobs || []).slice(-12).reverse())

const elapsed = computed(() => {
  const started = (state.task.started_at || 0) * 1000
  if (!running.value || !started) return '—'
  const sec = Math.max(0, Math.floor((now.value - started) / 1000))
  const m = Math.floor(sec / 60)
  return m > 0 ? `${m}分${sec % 60}秒` : `${sec}秒`
})

const lastLine = computed(() => {
  const t = state.logText || ''
  const lines = t.split('\n').map((x) => x.trim()).filter(Boolean)
  return lines.length ? lines[lines.length - 1] : ''
})

const btnLabel = computed(() => {
  if (running.value) return '处理中'
  if (pending.value) return `排队 ${pending.value}`
  return '空闲'
})

async function cancel(id: string) {
  try {
    const r = await api.queueCancel(state.project, id)
    ElMessage.success((r.message as string) || '已取消')
    await refreshQueue()
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}

async function clear() {
  try {
    await api.queueClear(state.project)
    await refreshQueue()
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}

async function onStop() {
  // 统一确认（U10）：打断任务是破坏性动作，确认框说清"已渲染完的会保留"
  const ok = await confirmAction({
    title: '停止任务',
    message: '停止当前任务？已渲染完的片段会保留（有断点续渲）。',
    confirmText: '停止',
    danger: true,
  })
  if (!ok) return
  try {
    const r = await api.stop(state.project)
    ElMessage.success((r.message as string) || '已停止')
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}
</script>

<style scoped>
.fab {
  position: fixed; right: 16px; bottom: 16px; z-index: 2000;
  display: flex; flex-direction: column; align-items: flex-end; gap: 8px;
}
.fabbtn {
  display: flex; align-items: center; gap: 7px; padding: 8px 13px; border-radius: 22px;
  border: 1px solid var(--line); background: var(--card); box-shadow: var(--shadow);
  cursor: pointer; font: inherit; font-size: 12.5px; font-weight: 600; color: var(--ink);
}
.fab.busy .fabbtn { border-color: color-mix(in srgb, var(--lime) 60%, var(--ink)); }
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--run); }
.fab.busy .dot { background: var(--lime); animation: pulse 1.4s ease-in-out infinite; }
.dot.idle { background: var(--dot-idle); animation: none; }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: .35; } }
.badge {
  background: var(--bad); color: var(--card); border-radius: 9px; padding: 0 6px; font-size: 10.5px;
  font-weight: 700; min-width: 16px; text-align: center;
}
.panel {
  width: 292px; background: var(--card); border: 1px solid var(--line);
  border-radius: var(--radius); box-shadow: var(--shadow); padding: 10px 11px;
  display: flex; flex-direction: column; gap: 7px;
  animation: p-in .22s cubic-bezier(.22, 1, .36, 1);
}
@keyframes p-in { from { opacity: 0; transform: translateY(6px) scale(.98); } to { opacity: 1; transform: none; } }
.phead { display: flex; align-items: center; font-size: 12.5px; }
.phead .x { border: 0; background: transparent; cursor: pointer; font: inherit; color: var(--muted); }
.prow { display: flex; align-items: center; gap: 6px; }
/* 不确定态：左右滑动的条，不假装知道进度 */
.indet { height: 8px; border-radius: 4px; background: var(--surface-3); overflow: hidden; position: relative; }
.indet i {
  position: absolute; inset: 0; width: 40%; border-radius: 4px; background: var(--lime);
  animation: indet 1.3s ease-in-out infinite;
}
@keyframes indet { 0% { left: -40%; } 100% { left: 100%; } }
.lastlog {
  font-size: 10.5px; color: var(--text-3); background: var(--surface-2); border-radius: 6px;
  padding: 3px 6px; line-height: 1.45; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.jobs { display: flex; flex-direction: column; gap: 2px; max-height: 220px; overflow: auto; }
.job { display: flex; align-items: center; gap: 5px; font-size: 11px; padding: 2px 3px; border-radius: 5px; }
.job.running { background: var(--run-bg); }
.job.done { color: var(--muted); }
.job.failed { background: var(--bad-bg); color: var(--bad); }
.job.canceled { color: var(--muted); text-decoration: line-through; }
.jsym { width: 12px; text-align: center; }
.jlabel { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 152px; }
.jstat { font-size: 10px; }
.jx { border: 0; background: transparent; cursor: pointer; font: inherit; color: var(--muted); }
</style>
