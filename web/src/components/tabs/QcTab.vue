<template>
  <div class="tabroot">
    <div class="summary">
      <template v-if="err">质检数据获取失败：{{ err }}</template>
      <template v-else-if="!state.qc.has_result">还没有质检结果：先跑一次「质检」阶段。</template>
      <template v-else>
        <!-- ★ 通过数是最重要的信息：2026-09-23 修过一次判据，
             旧「灰度均值差」判据曾把 52/52 全判可疑，绝不能渲染成"全黄"。 -->
        <el-tag size="small" type="success">通过 {{ state.qc.counts.pass }}</el-tag>
        <el-tag v-if="state.qc.counts.suspicious" size="small" type="warning">
          可疑 {{ state.qc.counts.suspicious }}
        </el-tag>
        <el-tag v-if="failCount" size="small" type="danger">不合格 {{ failCount }}</el-tag>
        <el-tag v-if="errorCount" size="small" type="danger">未执行 {{ errorCount }}</el-tag>
        <span v-if="state.qc.rerender.length" class="small">需重渲 {{ state.qc.rerender.length }}</span>
        <!-- ★ U6 行动闭环（2026-09-25）：看到「需重渲 N」就地一键入队，
             不用再去镜头表逐镜点 N 次（B5「有 API 无 UI」的补口）。 -->
        <el-button
          v-if="state.qc.rerender.length"
          data-testid="btn-qc-rerender-all"
          size="small"
          type="danger"
          plain
          :loading="bulkBusy"
          :disabled="bulkBusy || state.running"
          :title="state.running ? '有任务在跑，先等它结束' : `把这 ${state.qc.rerender.length} 个镜头一次性入队重渲（force，忽略指纹直接重跑）`"
          @click="rerenderAll"
        >一键全部入队重渲</el-button>
        <span class="spacer"></span>
        <span class="small">{{ when }}</span>
      </template>
    </div>
    <div class="tabbody flush">
      <template v-if="!err && state.qc.has_result">
        <div class="hintline qchint">
          判据：末尾 25% 窗口内相邻帧的<b>逐像素绝对差</b>均值（2026-09-23 修正）。
          旧判据「灰度均值差」曾把 52/52 全判可疑，已废弃；结果里的 <code>freeze_ratio</code> 只作参考。
        </div>
        <template v-for="row in rows" :key="row.id">
          <div class="qcrow" @click="emit('select', row.id)">
            <el-tag size="small" :type="TAG[row.r.verdict]">{{ QC_CN[row.r.verdict] }}</el-tag>
            <span class="sid">{{ row.id }}</span>
            <span class="small">{{ (row.s.dialogue || row.s.narration || '').slice(0, 16) }}</span>
            <span class="spacer"></span>
            <el-tag v-if="isRerender(row.id)" size="small" type="danger">需重渲</el-tag>
            <el-tag v-else-if="isReview(row.id)" size="small" type="warning">可疑</el-tag>
            <!-- ★ 未质检镜头的兜底结果（verdict=error/未执行）**可能缺 issues/metrics 字段**
                 （服务端占位对象只保证 verdict），一律可选访问 —— 否则打开质检 tab 即崩
                 （test-guard 终验实锤：含未质检镜头的项目必现）。 -->
            <span class="metrics small" :title="METRICS_TITLE">{{ metricsText(row.r.metrics || {}) }}</span>
            <el-button size="small" text @click.stop="emit('select', row.id)">定位</el-button>
          </div>
          <div v-if="row.r.issues?.length" class="qissues">
            <div v-for="(t, i) in row.r.issues" :key="i">{{ t }}</div>
          </div>
        </template>
        <div v-if="!rows.length" class="emptyrow">质检结果里没有可显示的镜头。</div>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 质检。
 *
 * 移植自 index.html 的 `refreshQc()`：
 *   顶部汇总按 verdict 计数（**通过数一定要露出来**）、按严重度排序列出逐镜判定、
 *   每条显示 verdict / 镜头号 / 台词摘要 / metrics（std · 亮度 · 冻结）/ issues，点击定位到该镜。
 *
 * ★ 判据 2026-09-23 修过一次：旧判据「灰度均值差」把 52/52 全判可疑（错的），
 *   新判据是「逐像素绝对差」，所以 `counts.pass` 是可信的正常值，不是"全黄"。
 * `review`（可疑）/ `rerender`（需重渲）是服务端给的两个名单，这里额外打标。
 *
 * ★ U6 行动闭环（2026-09-25）：「需重渲 N」旁给**一键全部入队重渲** ——
 *   走 `/api/run` 的 `only` 列表（一个任务覆盖多镜，比 N 次单镜省），
 *   从"看到问题"到"修问题"不再需要逐镜点 N 次（B5：批量能力有 API 无 UI）。
 */
import { computed, onMounted, ref } from 'vue'
import { ElButton, ElMessage, ElTag } from 'element-plus'
import type { QcResult, ShotRow } from '@/api/types'
import { api } from '@/api/client'
import { confirmAction } from '@/composables/confirmAction'
import { refreshQc, refreshStatus, state } from '@/stores/app'

/** 每条 verdict 的中文与语气（pass 绿 / suspicious 黄 / fail·error 红）。 */
const QC_CN: Record<QcResult['verdict'], string> = {
  pass: '通过', suspicious: '可疑', fail: '不合格', error: '未执行',
}
const TAG: Record<QcResult['verdict'], 'success' | 'warning' | 'danger' | 'info'> = {
  pass: 'success', suspicious: 'warning', fail: 'danger', error: 'danger',
}
/** 旧文件严重度排序：error → fail → suspicious → pass。 */
const ORDER: Record<QcResult['verdict'], number> = { error: 0, fail: 1, suspicious: 2, pass: 3 }
const METRICS_TITLE = 'std=采样帧灰度标准差 / 亮=平均亮度 / 冻结=末尾逐像素差均值'

interface QcRow {
  id: string
  s: ShotRow
  r: QcResult
}

const loading = ref(false)
const err = ref('')

const rows = computed<QcRow[]>(() => {
  const res = state.qc.results
  return state.shots
    .filter((s) => res[s.id])
    .map((s) => ({ id: s.id, s, r: res[s.id] }))
    .sort((a, b) => ORDER[a.r.verdict] - ORDER[b.r.verdict])
})
const failCount = computed(() => Object.values(state.qc.results).filter((r) => r.verdict === 'fail').length)
const errorCount = computed(() => Object.values(state.qc.results).filter((r) => r.verdict === 'error').length)
const when = computed(() =>
  state.qc.generated_at ? new Date(state.qc.generated_at * 1000).toLocaleString() : '')
const isReview = (id: string): boolean => state.qc.review.includes(id)
const isRerender = (id: string): boolean => state.qc.rerender.includes(id)

/** U6：一键把「需重渲」名单全部入队重渲（`/api/run` + `only` 列表，一个任务覆盖多镜）。
 *  入队前把名单摆给人看（连 GPU、不可半路撤单），确认才发。 */
const bulkBusy = ref(false)
async function rerenderAll() {
  const ids = [...state.qc.rerender]
  if (!ids.length || bulkBusy.value) return
  // 统一确认（U10）：先摆名单再确认（confirmAction 的 list），危险动作标红
  const ok = await confirmAction({
    title: '一键全部入队重渲',
    message: `把「需重渲」的 ${ids.length} 个镜头一次性入队重渲？这是 GPU 任务（约 40 秒/镜），提交后按镜头表顺序跑。`,
    list: ids,
    confirmText: '入队重渲',
    cancelText: '再想想',
    danger: true,
  })
  if (!ok) return
  bulkBusy.value = true
  try {
    const r = await api.run(state.project, 'render', { force: true, only: ids })
    ElMessage.success(r.message || `已提交批量重渲 ${ids.length} 镜，进度见「日志」tab`)
    try {
      await refreshStatus() // 让顶栏立刻进入"运行中"，不用等 2s 轮询
    } catch { /* 状态刷新失败不影响任务已提交这一事实 */ }
  } catch (e) {
    ElMessage.error(`批量重渲提交失败：${(e as Error).message}`)
  } finally {
    bulkBusy.value = false
  }
}

/* metrics 是 Record<string, number|string>，字符串数字也要能显示。 */
function mnum(m: Record<string, number | string>, k: string): number | null {
  const v = m[k]
  if (typeof v === 'number') return v
  if (typeof v === 'string' && v.trim() !== '' && !Number.isNaN(Number(v))) return Number(v)
  return null
}
function metricsText(m: Record<string, number | string>): string {
  const parts: string[] = []
  const std = mnum(m, 'std')
  if (std !== null) parts.push(`std ${Math.round(std * 10) / 10}`)
  const mean = mnum(m, 'mean')
  if (mean !== null) parts.push(`亮 ${Math.round(mean * 10) / 10}`)
  const fz = mnum(m, 'freeze_pixel_diff')
  if (fz !== null) parts.push(`冻结 ${Math.round(fz * 100) / 100}`)
  return parts.join(' · ')
}

async function load(force = false) {
  if (loading.value) return
  if (!force && state.qc.has_result) return
  loading.value = true
  err.value = ''
  try {
    await refreshQc()
  } catch (e) {
    err.value = (e as Error).message
  } finally {
    loading.value = false
  }
}

const emit = defineEmits<{ select: [id: string] }>()

onMounted(() => {
  void load()
})
</script>

<style scoped>
.qchint {
  padding: 6px 10px 0;
  margin-top: 0;
}
.metrics {
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}
</style>
