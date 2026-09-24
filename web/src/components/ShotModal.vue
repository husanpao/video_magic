<!--
  镜头详情 / 编辑弹层。行为逐条对应旧 vm/static/index.html：
    · 弹层标记（431-483）：头部 id + 派生状态 + file/index 提示 + 关闭
    · renderDetail()（1052-1140）：预览 video、meta、警告框、台词/旁白 + 字数计数、
      景别/运镜/时长/seed、角色、服装（带 datalist 选项 + 提示行）、六段式编辑器 + 原文模式、
      语音预算提示（超 limits 要警告）
    · E1 锁定按钮（新增于 2026-09-23）：文案随状态变；**没有产物时禁用**并说明原因
    · 底部操作：保存 / 撤销 / 重渲 / 重写提示词 / 拆分 / 合并 / 插入 / 删除
  与旧版的差异：旧版是「右侧抽屉」，这里是**居中聚焦弹层**（不遮住 420px 右栏）。
-->
<template>
  <el-dialog
    v-model="open"
    :title="d ? `${d.id} · ${kindInfo(kind).t}` : '镜头详情'"
    width="min(880px, 94vw)"
    top="4vh"
    class="shotmodal"
    destroy-on-close
    @closed="closeShot"
  >
    <div v-if="loading" class="skel"><i /><i /><i /></div>
    <div v-else-if="err" class="emptyrow">{{ err }}</div>
    <div v-else-if="d" class="mbody">
      <!-- 预览 + 元信息 -->
      <div class="mrow">
        <div class="mleft">
          <video v-if="d.clip.exists" :src="api.playUrl(state.project, d.id)" controls preload="metadata" />
          <div v-else class="novideo">
            {{ d.qc?.verdict === 'error' ? '质检未执行 / 文件坏' : '还没有产物（先渲染这一镜）' }}
          </div>
          <!-- 分镜图（渲染前审片）。与上面的实际片段**并排对照**：
               分镜图是纯文生图，视频是参考图条件生成 —— 能对上"谁/在干什么/什么场景"，
               对不上"从哪个机位、什么光"。并排放是为了让这个差异**一眼可见**。 -->
          <div v-if="sb" class="sbwrap">
            <img :src="api.storyboardUrl(state.project, d.id, sb.at || 0)" alt="" loading="lazy">
            <div class="sblabel small dim">
              分镜图（预演） · 文生图，构图/光线与实际片段不一致属正常
            </div>
          </div>
        </div>
        <div class="mright">
          <div class="meta2">
            <span>文件 <code>{{ d.file }}</code></span>
            <span>序号 <code>{{ d.index }}</code>/{{ total }}</span>
            <span>帧数 <code>{{ d.frames ?? '—' }}</code></span>
            <span>时长 <code>{{ d.sec }}s</code>{{ d.sec ? '' : '' }}</span>
            <span>提示词 <code>{{ d.prompt_len }}</code> 字</span>
            <span>指纹 <code>{{ (d.fp || '').slice(0, 10) || '—' }}</code></span>
          </div>
          <div v-if="d.locked" class="lockrow">
            🔒 已锁定{{ d.locked_by ? `（${d.locked_by}）` : '' }}：渲染不会覆盖这一版
          </div>
          <div v-if="d.prompt_mismatch?.length" class="issues warn">
            <b>提示词与字段不一致</b>
            <ul><li v-for="(m, i) in d.prompt_mismatch" :key="i">{{ m }}</li></ul>
            <div class="hintline">建议点「重写提示词」同步。</div>
          </div>
          <div v-if="d.qc && d.qc.issues?.length" class="issues" :class="toneOf(d.qc.verdict)">
            <b>质检：{{ QC_CN[d.qc.verdict] || d.qc.verdict }}</b>
            <ul><li v-for="(m, i) in d.qc.issues" :key="i">{{ m }}</li></ul>
          </div>
        </div>
      </div>

      <!-- 台词 / 旁白 -->
      <div class="frow2">
        <label>
          台词
          <el-input v-model="f.dialogue" placeholder="本镜台词（没有就留空）" />
          <span class="cnt" :class="{ over: dlgLen > d.limits.dialogue_max }">
            {{ dlgLen }}/{{ d.limits.dialogue_max }}
          </span>
        </label>
        <label>
          旁白
          <el-input v-model="f.narration" placeholder="本镜旁白（没有就留空）" />
          <span class="cnt" :class="{ over: narLen > d.limits.narration_max }">
            {{ narLen }}/{{ d.limits.narration_max }}
          </span>
        </label>
      </div>

      <!-- 元字段 -->
      <div class="frow3">
        <label>景别 <el-input v-model="f.shot_size" placeholder="中景" /></label>
        <label>运镜 <el-input v-model="f.camera" placeholder="缓推（Push In, slow）" /></label>
        <label>时长（秒）<el-input-number v-model="f.sec" :min="d.limits.sec_min" :max="d.limits.sec_max" /></label>
        <label>seed <el-input-number v-model="f.seed" :min="0" /></label>
      </div>
      <div class="frow2">
        <label>角色（逗号分隔）<el-input v-model="f.chars" placeholder="孙悟空,唐僧" /></label>
        <label>
          服装（角色=变体）
          <el-input v-model="f.costume" list="costumeOpts" placeholder="孙悟空=armor" />
          <datalist id="costumeOpts">
            <option v-for="o in costumeOpts" :key="o" :value="o" />
          </datalist>
        </label>
      </div>
      <div class="hintline">
        景别只是记录字段（要生效需「重写提示词」）；运镜会同步重写提示词里的 CAMERA DISCIPLINE 行。
      </div>

      <!-- 六段式 -->
      <div class="sechead">
        <b>六段式提示词</b>
        <small class="dim">{{ raw ? '原文模式' : `${list.length} 段` }}</small>
        <span class="spacer" />
        <el-button size="small" @click="toggleRaw">{{ raw ? '切回分段编辑' : '切到原文编辑' }}</el-button>
      </div>
      <textarea v-if="raw" v-model="f.raw" class="rawbox" spellcheck="false" />
      <div v-else class="secs">
        <div v-for="(k, i) in list" :key="k" class="secrow">
          <div class="seclabel">
            <span>{{ i + 1 }}. {{ SIX_CN[k] || k }}</span>
            <code>{{ k }}</code>
          </div>
          <textarea v-model="f.sections[k]" spellcheck="false" :rows="rowsFor(k)" />
        </div>
        <div v-if="d.missing_sections.length" class="hintline">
          缺少段落：{{ d.missing_sections.join('、') }}（渲染前会被补默认值）
        </div>
      </div>

      <div v-if="budget" class="issues" :class="budget.ok ? '' : 'warn'">
        语音预算：{{ d.speech_chars }} 字需要约 {{ d.seconds_needed }}s，本镜 {{ f.sec }}s
        {{ budget.ok ? '✓' : `（差 ${budget.short}s，H3 会读快或截断）` }}
      </div>
    </div>

    <template #footer>
      <div class="mfoot">
        <el-button
          :type="d?.locked ? 'primary' : undefined"
          :disabled="!d?.clip?.exists || busy"
          :title="d?.clip?.exists ? lockTitle : '还没有产物可锁（先渲染这一镜）'"
          @click="onLock"
        >{{ d?.locked ? '🔒 已锁定（点击解锁）' : '🔓 未锁定（点击锁定这一版）' }}</el-button>
        <span class="spacer" />
        <el-button :disabled="busy" @click="onSave">保存（只标待重渲）</el-button>
        <el-button :disabled="busy" @click="onUndo">撤销上一步</el-button>
        <el-button :disabled="busy || !d?.clip?.exists" @click="onRerender">重渲</el-button>
        <el-button :disabled="busy" @click="onRewrite">重写提示词（1 次 LLM）</el-button>
        <el-button :disabled="busy" @click="onSplit">拆分</el-button>
        <el-button :disabled="busy || !d?.next_id" :title="d?.next_id ? '' : '这是最后一镜'" @click="onMerge">合并下一镜</el-button>
        <el-button :disabled="busy" @click="onInsert">插入新镜</el-button>
        <el-button type="danger" :disabled="busy" @click="onDelete">删除</el-button>
      </div>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api/client'
import type { ShotDetail, SectionMap } from '@/api/types'
import { kindInfo, kindOf as _kindOf, state, storyboardById, type AnyKind } from '@/stores/app'
import { closeShot, ui } from '@/stores/ui'

const SIX_CN: Record<string, string> = {
  subject_definitions: '角色定义',
  summary: '摘要（任务类型）',
  retention_analysis: '保持要求 / 纪律',
  detailed_description: '画面描述',
  overall_soundscape: '环境音',
  non_diegetic_music: '配乐',
}
const QC_CN: Record<string, string> = { pass: '通过', suspicious: '可疑', fail: '不合格', error: '未执行' }
function toneOf(v: string) { return v === 'fail' || v === 'error' ? 'bad' : v === 'suspicious' ? 'warn' : '' }

const d = ref<ShotDetail | null>(null)
const total = ref(0)
const loading = ref(false)
const err = ref('')
const busy = ref(false)
const raw = ref(false)

const open = computed({
  get: () => ui.modalOpen,
  set: (v: boolean) => { if (!v) closeShot() },
})

const kind = computed<AnyKind>(() => {
  const s = d.value
  if (!s) return 'missing'
  // 复用表格那一套派生逻辑，避免弹层和表格显示不一致（旧文件也是同一个 kindOf）
  const row = state.shots.find((x) => x.id === s.id)
  if (row) return _kindOf(row)
  return s.status === 'stale' ? 'stale' : s.clip.exists ? 'current' : 'missing'
})

const f = reactive({
  dialogue: '', narration: '', shot_size: '', camera: '', sec: 0, seed: 0,
  chars: '', costume: '', raw: '', sections: {} as SectionMap,
})

const dlgLen = computed(() => [...f.dialogue].length)
const narLen = computed(() => [...f.narration].length)
const list = computed(() => Object.keys(f.sections))
const costumeOpts = computed(() => {
  const out: string[] = []
  for (const [char, variants] of Object.entries(d.value?.costume_options || {})) {
    for (const v of variants) out.push(`${char}=${v.id}`)
  }
  return out
})
const lockTitle = computed(() =>
  d.value?.locked
    ? `已锁定${d.value.locked_by ? `（${d.value.locked_by}）` : ''}：渲染不会覆盖这一版，编辑也建议先解锁`
    : '锁定后渲染不会覆盖这一版（需显式强制重跑才能改）',
)
/** 语音预算：字数 / 每秒字数 vs 本镜时长 */
/** 该镜的分镜图（有产物才展示）。 */
const sb = computed(() => {
  const r = storyboardById.value[d.value?.id || '']
  return r && (r.status === 'current' || r.status === 'stale') ? r : null
})

const budget = computed(() => {
  const s = d.value
  if (!s) return null
  const need = s.seconds_needed || 0
  const have = f.sec || 0
  const short = Math.max(0, +(need - have).toFixed(2))
  return { ok: short <= 0.4, short }
})

function rowsFor(k: string) { return k === 'detailed_description' ? 8 : k === 'subject_definitions' || k === 'retention_analysis' ? 6 : 3 }

async function load() {
  const id = ui.detailId
  if (!id) return
  loading.value = true
  err.value = ''
  try {
    const j = await api.shot(state.project, id)
    d.value = j.shot
    total.value = j.total
    raw.value = false
    f.dialogue = j.shot.dialogue
    f.narration = j.shot.narration
    f.shot_size = j.shot.shot_size
    f.camera = j.shot.camera
    f.sec = j.shot.sec
    f.seed = j.shot.seed
    f.chars = (j.shot.chars || []).join(',')
    f.costume = Object.entries(j.shot.costume || {}).map(([a, b]) => `${a}=${b}`).join(',')
    f.raw = j.shot.prompt
    f.sections = { ...(j.shot.sections || {}) }
  } catch (e) {
    err.value = (e as Error).message
  } finally {
    loading.value = false
  }
}

watch(() => [ui.modalOpen, ui.detailId], () => { if (ui.modalOpen) load() }, { immediate: true })

function parseChars(s: string) { return s.split(/[,，、\s]+/).map((x) => x.trim()).filter(Boolean) }
function parseCostume(s: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const seg of s.split(/[,，\s]+/)) {
    const m = seg.match(/^(.+?)[=＝:：](.+)$/)
    if (m) out[m[1].trim()] = m[2].trim()
  }
  return out
}

function patch(): Record<string, unknown> {
  if (raw.value) return { prompt: f.raw }
  return {
    dialogue: f.dialogue, narration: f.narration, shot_size: f.shot_size, camera: f.camera,
    sec: f.sec, seed: f.seed, chars: parseChars(f.chars), costume: parseCostume(f.costume),
    sections: { ...f.sections },
  }
}

async function withBusy(fn: () => Promise<void>) {
  busy.value = true
  try { await fn() } catch (e) { ElMessage.error((e as Error).message) } finally { busy.value = false }
}

const onSave = () => withBusy(async () => {
  const r = await api.updateShot(state.project, d.value!.id, patch())
  ElMessage.success((r.message as string) || '已保存（标为待重渲）')
  await load()
})
const onUndo = () => withBusy(async () => {
  const r = await api.undo(state.project)
  ElMessage.success((r.message as string) || '已撤销')
  await load()
})
const onRerender = () => withBusy(async () => {
  await api.rerender(state.project, d.value!.id)
  ElMessage.success('已提交重渲，看顶部进度')
})
const onRewrite = () => withBusy(async () => {
  await ElMessageBox.confirm(
    `只重写 ${d.value!.id} 这一镜的六段式提示词？会调用 1 次 DeepSeek，不重跑整章。`,
    '重写提示词', { type: 'warning' },
  )
  const r = await api.rewriteShot(state.project, d.value!.id)
  ElMessage.success((r.message as string) || '已重写')
  await load()
})
const onSplit = () => withBusy(async () => {
  const v = await ElMessageBox.prompt(
    '拆分点：留空 = 自动按标点找（推荐）；整数 = 台词第 N 个字符处；0~1 小数 = 按比例',
    '拆分镜头', { inputValue: '' },
  ).catch(() => null)
  if (!v) return
  const s = String(v.value || '').trim()
  const at = s === '' ? undefined : Number(s)
  await api.splitShot(state.project, d.value!.id, at)
  ElMessage.success('已拆分')
  await load()
})
const onMerge = () => withBusy(async () => {
  await api.mergeShot(state.project, d.value!.id)
  ElMessage.success('已与下一镜合并')
  await load()
})
const onInsert = () => withBusy(async () => {
  await api.insertShot(state.project, d.value!.id, true)
  ElMessage.success('已在后面插入新镜')
  await load()
})
const onDelete = () => withBusy(async () => {
  await ElMessageBox.confirm(`删除 ${d.value!.id}？可点顶部「撤销上一步」恢复。`, '删除镜头', { type: 'warning' })
  await api.deleteShot(state.project, d.value!.id)
  ElMessage.success('已删除')
  closeShot()
})
/** E1：锁定 / 解锁。锁定后渲染层会跳过它（只有显式强制重跑才能改）。 */
const onLock = () => withBusy(async () => {
  const cur = !!d.value!.locked
  const r = await api.lockShot(state.project, d.value!.id, 'locked', !cur)
  ElMessage.success(r.message)
  await load()
})

function toggleRaw() {
  if (!raw.value) {
    // 分段 → 原文：用服务端的完整 prompt 重新拼，避免丢段
    f.raw = d.value?.prompt || Object.entries(f.sections).map(([k, v]) => `${k}: ${v}`).join('\n')
  } else {
    // 原文 → 分段：按 `段落名: ` 切回来
    const out: SectionMap = {}
    let cur = ''
    for (const line of f.raw.split('\n')) {
      const m = line.match(/^([a-z_]+):\s?(.*)$/)
      if (m) { if (cur) out[cur] = (out[cur] || '') ; cur = m[1]; out[cur] = m[2] }
      else if (cur) out[cur] = `${out[cur] || ''}\n${line}`
    }
    if (Object.keys(out).length) f.sections = out
  }
  raw.value = !raw.value
}
</script>

<style scoped>
.mbody { max-height: 68vh; overflow: auto; padding-right: 4px; }
.mrow { display: grid; grid-template-columns: minmax(0, 1.1fr) minmax(0, 1fr); gap: 12px; }
.mleft video { width: 100%; border-radius: 10px; background: #000; display: block; }
.sbwrap { margin-top: 8px; }
.sbwrap img { width: 100%; border-radius: 10px; display: block; border: 1px solid var(--line); }
.sblabel { margin-top: 3px; }
.novideo {
  aspect-ratio: 16/9; display: flex; align-items: center; justify-content: center;
  background: #eef0f3; border-radius: 10px; color: var(--muted); font-size: 12.5px;
}
.meta2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 3px 12px; font-size: 11.5px; }
.meta2 code { font-size: 11px; }
.lockrow { margin-top: 7px; font-size: 11.5px; color: var(--warn); font-weight: 600; }
.issues { margin-top: 8px; font-size: 11.5px; line-height: 1.6; border-radius: 8px; padding: 6px 8px; background: #f4f7ea; }
.issues.warn { background: #fdf6e3; }
.issues.bad { background: #fdeceb; }
.issues ul { margin: 3px 0 0; padding-left: 16px; }
.frow2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 8px; margin-top: 10px; }
.frow3 { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 8px; margin-top: 8px; }
.frow2 label, .frow3 label { font-size: 11.5px; color: var(--muted); display: block; }
.frow2 label :deep(.el-input), .frow3 label :deep(.el-input-number) { margin-top: 3px; }
.cnt { font-size: 10.5px; color: var(--muted); }
.cnt.over { color: var(--bad); font-weight: 600; }
.sechead { display: flex; align-items: center; gap: 8px; margin: 12px 0 6px; }
.secs { display: flex; flex-direction: column; gap: 8px; }
.seclabel { display: flex; align-items: baseline; gap: 7px; font-size: 11.5px; }
.seclabel code { font-size: 10px; color: var(--muted); }
.secrow textarea, .rawbox {
  width: 100%; font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 11.5px; line-height: 1.55;
  border: 1px solid var(--line); border-radius: 8px; padding: 6px 8px; resize: vertical; margin-top: 3px;
}
.rawbox { min-height: 320px; }
.mfoot { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
</style>
