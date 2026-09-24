<template>
  <div class="tabroot">
    <div class="summary">
      <template v-if="err">审计数据获取失败：{{ err }}
</template>
      <template v-else-if="!audit">加载中…</template>
      <template v-else-if="!audit.has_audit">还没有审计结果</template>
      <template v-else>
        <span>共 {{ audit.findings.length }} 条</span>
        <el-tag v-if="counts.error" size="small" type="danger">error {{ counts.error }}</el-tag>
        <el-tag v-if="counts.warning" size="small" type="warning">warning {{ counts.warning }}</el-tag>
        <el-tag v-if="counts.info" size="small" type="info">info {{ counts.info }}</el-tag>
        <el-tag size="small" :type="audit.summary.gate_passed ? 'success' : 'danger'">
          gate {{ audit.summary.gate_passed ? '通过' : '未通过' }}
        </el-tag>
        <span class="spacer"></span>
        <span class="small">{{ audit.generated_at }}</span>
      </template>
    </div>

    <div class="tabbody flush">
      <div v-if="err" class="emptyrow">审计数据获取失败：{{ err }}</div>
      <div v-else-if="!audit || !audit.has_audit" class="emptyrow">
        <b>还没有审计结果</b><br>
        跑一次审计（vm/audit，纯 Python 零 GPU）后，这里会列出「对白丢失 / 说话人歧义 / 节奏形状」等发现，
        并按 info · warning · error 三档着色。
      </div>
      <template v-else>
        <!-- ★ 全片级 finding（project_level === true，from_id 为空）单独放最上面，
             标「全片级」，**绝不当成"第 0 镜"或挂到某个镜头上**。 -->
        <template v-if="projFindings.length">
          <div class="secttitle">全片级 · {{ projFindings.length }} 条（不属于任何单个镜头）</div>
          <template v-for="f in projFindings" :key="f.code + '|' + f.message">
            <div class="qcrow project">
              <el-tag size="small" :type="SEV[f.severity]">{{ f.severity }}</el-tag>
              <span class="sid">全片级</span>
              <el-tag size="small" type="info">{{ f.code }}</el-tag>
              <span class="spacer"></span>
              <span class="small">{{ f.module }}</span>
            </div>
            <div class="fmsg">
              {{ f.message }}
              <div v-if="f.rewrite_hint" class="sug">建议：{{ f.rewrite_hint }}</div>
            </div>
          </template>
        </template>

        <div v-if="shotFindings.length" class="secttitle">镜头级 · {{ shotFindings.length }} 条</div>
        <template v-for="f in shotFindings" :key="f.code + '|' + loc(f)">
          <div class="qcrow" @click="go(f)">
            <el-tag size="small" :type="SEV[f.severity]">{{ f.severity }}</el-tag>
            <!-- 位置用 from_id / shot_ids（镜头 id），不用 from_shot（表内 1-based 序号，脆弱） -->
            <span class="sid">{{ loc(f) }}</span>
            <el-tag size="small" type="info">{{ f.code }}</el-tag>
            <span class="spacer"></span>
            <span v-if="f.shot_ids.length > 1" class="small">{{ f.shot_ids.length }} 镜</span>
            <span v-if="f.chars.length" class="small">{{ f.chars.join(' / ') }}</span>
            <el-button size="small" text @click.stop="go(f)">定位</el-button>
          </div>
          <div class="fmsg">
            {{ f.message }}
            <div v-if="f.rewrite_hint" class="sug">建议：{{ f.rewrite_hint }}</div>
          </div>
        </template>

        <div v-if="!audit.findings.length" class="emptyrow">
          审计没有发现问题（gate_passed={{ audit.summary.gate_passed }}）。
        </div>

        <!-- 审计自曝的局限：默认折叠（旧文件是 <details>），但一定要能读到 -->
        <el-collapse v-if="audit.limitations.length" class="limbox">
          <el-collapse-item
            :title="`审计自曝的局限（${audit.limitations.length} 条，建议读一下再下结论）`"
            name="lim"
          >
            <div v-for="(t, i) in audit.limitations" :key="i" class="limtext">{{ t }}</div>
          </el-collapse-item>
        </el-collapse>
      </template>
    </div>
  </div>

    <!-- ── B3：逐镜 7 维完备性矩阵 ──────────────────────────────────────
         比"第 5 镜有问题"信息密度高得多：能直接说「第 5 镜的角色参考图还只是 draft」。
         维度按**我们的真实管线**定义（不是照抄调研原文，否则会有 3 个恒定空的列）。 -->
    <div class="cm">
      <div class="cmhead">
        <b>完备性矩阵</b>
        <span class="spacer" />
        <template v-if="cm">
          <span class="dim small">{{ cm.summary.shots }} 镜 · 可渲染 {{ cm.summary.ready_to_render }}</span>
          <span v-if="cm.summary.blocked_shots" class="tagbad">被前置阻塞 {{ cm.summary.blocked_shots }}</span>
        </template>
      </div>
      <div v-if="!cm" class="dim small">加载中…</div>
      <template v-else>
        <div class="cmlegend dim small">
          <span><i class="sw ok" />齐全</span>
          <span><i class="sw warn" />需注意（待确认 / 旧）</span>
          <span><i class="sw idle" />缺失</span>
          <span class="spacer" />
          <span>悬停格子看说明</span>
        </div>
        <div class="cmwrap">
          <table class="cmtable">
            <thead>
              <tr>
                <th class="cmid">镜头</th>
                <th
                  v-for="d in cm.dimensions"
                  :key="d.key"
                  :title="d.phase + (d.blocks_render ? ' · 前置阻塞项' : '')"
                >
                  {{ d.label }}<span v-if="d.blocks_render" class="blk">*</span>
                </th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="r in cm.shots" :key="r.id" :class="{ blocked: r.blockers.length }">
                <td class="cmid" :title="r.blockers.length ? '前置阻塞：' + r.blockers.join('、') : ''">
                  {{ r.id }}<span v-if="r.blockers.length" class="blk">!</span>
                </td>
                <td v-for="d in cm.dimensions" :key="d.key" :title="r.cells[d.key]?.detail">
                  <i class="sw" :class="r.cells[d.key]?.tone" />
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="cmstats dim small">
          <span v-for="d in cm.dimensions" :key="d.key">{{ d.label }}：<b>{{ dimLine(d.key) }}</b></span>
        </div>
        <div v-for="(n, i) in cm.notes" :key="i" class="hintline">{{ n }}</div>
      </template>
    </div>
</template>

<script setup lang="ts">
/**
 * 审计。
 *
 * 移植自 index.html 的 `refreshAudit()`：
 *   顶部 = 发现计数（error/warning/info）+ gate_passed + 生成时间；
 *   列表按 severity 三档语气着色：error 红 / warning 黄 / **info 用中性色（绝不红色）**
 *   —— 审计层会**主动把不可信的结论降级为 info 并自曝**（shape-borderline /
 *   proxy-low-discrimination），info 不是告警，染红会误导人。
 *   位置一律用 `from_id` / `shot_ids`（镜头 id），不用 `from_shot`（表内 1-based 序号，脆弱）。
 *   `project_level === true`（from_id 为空）的 finding 单独放顶部并标「全片级」。
 *   limitations 用折叠面板展示（旧文件是默认收起的 <details>）。
 */
import { computed, onMounted, ref } from 'vue'
import { ElButton, ElCollapse, ElCollapseItem, ElTag } from 'element-plus'
import type { AuditFinding } from '@/api/types'
import { refreshAudit, refreshCompleteness, state } from '@/stores/app'

/** ★ info 必须是中性色：这些是「不可信结论的降级 + 自曝」，不是告警。 */
const SEV: Record<AuditFinding['severity'], 'danger' | 'warning' | 'info'> = {
  error: 'danger', warning: 'warning', info: 'info',
}

const loading = ref(false)
const err = ref('')

const audit = computed(() => state.audit)
const counts = computed(() => state.audit?.summary.findings ?? { error: 0, warning: 0, info: 0 })
/** 全片级 finding 单独一组：放最上面，且不可点（不是某一镜）。 */
const projFindings = computed<AuditFinding[]>(
  () => state.audit?.findings.filter((f) => f.project_level) ?? [])
const shotFindings = computed<AuditFinding[]>(
  () => state.audit?.findings.filter((f) => !f.project_level) ?? [])

/** 位置：优先 shot_ids（已解析的镜头 id），退化到 from_id；**永远不显示 from_shot**。 */
function loc(f: AuditFinding): string {
  if (f.shot_ids.length) return f.shot_ids.join('、')
  return f.from_id || '—'
}
function target(f: AuditFinding): string {
  return f.from_id || f.shot_ids[0] || ''
}
function go(f: AuditFinding) {
  const id = target(f)
  if (id) emit('select', id)
}

async function load(force = false) {
  if (loading.value) return
  if (!force && state.audit) return
  loading.value = true
  err.value = ''
  try {
    await refreshAudit()
  } catch (e) {
    err.value = (e as Error).message
  } finally {
    loading.value = false
  }
}

const emit = defineEmits<{ select: [id: string] }>()

onMounted(() => {
  void load()
  void refreshCompleteness()
})

// ── B3 完备性矩阵 ──
const cm = computed(() => state.completeness)
const STATE_CN: Record<string, string> = { ok: '齐全', stale: '需重出', draft: '待确认', missing: '缺失' }
function dimLine(key: string): string {
  const c = cm.value?.summary.per_dimension?.[key]
  if (!c) return '-'
  return Object.entries(c).map(([k, v]) => `${STATE_CN[k] || k} ${v}`).join(' / ')
}

</script>

<style scoped>
.limbox {
  margin: 8px 10px;
  border-top: 1px solid var(--line);
}
.limtext {
  font-size: 12px;
  line-height: 1.7;
  color: var(--muted);
  margin-bottom: 8px;
}
</style>

<style scoped>
.cm { margin-top: 12px; border-top: 1px solid var(--line); padding-top: 9px; }
.cmhead { display: flex; align-items: center; gap: 8px; font-size: 12.5px; margin-bottom: 6px; }
.cmlegend { display: flex; align-items: center; gap: 10px; margin-bottom: 5px; }
.sw { display: inline-block; width: 10px; height: 10px; border-radius: 2px; vertical-align: -1px; }
.sw.ok { background: var(--ok); }
.sw.warn { background: var(--warn); }
.sw.idle { background: #d6dae0; }
.tagbad { background: #fdeceb; color: var(--bad); font-size: 9.5px; padding: 0 5px; border-radius: 4px; }
.tag.bad { background: #fdeceb; color: var(--bad); font-size: 9.5px; padding: 0 5px; border-radius: 4px; }
.blk { color: var(--bad); font-weight: 700; margin-left: 2px; }
/* 52×7 用固定行高 + 虚拟滚动太夸张，这里给一个限高容器让它自己滚 */
.cmwrap { max-height: 260px; overflow: auto; border: 1px solid var(--line); border-radius: 8px; }
.cmtable { border-collapse: collapse; font-size: 10.5px; width: 100%; }
.cmtable th {
  position: sticky; top: 0; background: #fbfbfc; font-weight: 600; color: var(--muted);
  padding: 3px 4px; border-bottom: 1px solid var(--line); white-space: nowrap;
}
.cmtable td { padding: 2px 4px; border-bottom: 1px solid var(--line); text-align: center; }
.cmtable tr.blocked { background: #fdf6f5; }
.cmid { text-align: left !important; font-variant-numeric: tabular-nums; white-space: nowrap; }
.cmstats { margin-top: 6px; display: flex; flex-direction: column; gap: 1px; }
</style>
