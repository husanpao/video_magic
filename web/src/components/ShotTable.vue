<!--
  ③ 1fr 中栏：7 列表格（核心）。**整表的渲染都在这里，行为逐条照旧 vm/static/index.html 移植**：
    · 标记 + colgroup 宽度 + 表头（350-362）
    · renderTable()（798-851）：三种空态 / ①缩略图格子里直接写字 / ②状态三色 + 完成态青柠 ✓ 圆标 /
      ③操作按钮随状态改名 / 🔒 锁定标记 / 缩略图懒加载 / table-layout:fixed
    · tbody 事件委托（853-862）：**一个** listener，靠 data-act / data-id 分发，不给每行挂 handler
    · opsFor()（786-797）：无产物「生成/渲染」、有产物「重渲」、disabled 一律带 title 说明原因
    · selectShot()（866-874）：行点击 = 选中 + 打开详情（旧文件是 selectShot(id, true)）
    · #shotsRefresh（712）：右上角「刷新」
  改动点（都在注释里标了）：旧文件用 innerHTML 拼字符串，这里全部换成模板 + 派生数据。
-->
<template>
  <section class="col">
    <div class="mainhead">
      <b>镜头表</b>
      <span class="dim small">{{ subText }}</span>
      <span class="spacer" />
      <button class="ghost" title="重新拉取镜头表（/api/shots）" @click="reload">刷新</button>
    </div>

    <div class="tablewrap">
      <table class="shots">
        <colgroup>
          <!-- 场景列从 3.4rem 放宽到 5.6rem：现在显示的是**真场景名**（如「云隐寺山门」5 个字），
               3.4rem 会折成「山路枯 / 林」。相应把两句弹性列各让出 2%。 -->
          <col style="width:5.6rem"><col style="width:6.6rem"><col style="width:20%">
          <col style="width:28%"><col style="width:4rem"><col style="width:5.6rem"><col style="width:8.4rem">
        </colgroup>
        <thead>
          <tr>
            <th>场景</th><th>画面</th><th>台词 / 旁白</th><th>景别 / 运镜</th>
            <th>时长</th><th>状态</th><th>操作</th>
          </tr>
        </thead>

        <!-- 事件委托：整个 tbody 一个 listener -->
        <tbody ref="tbodyEl" @click="onTableClick">
          <!-- 空态①：还没加载过 → 骨架屏 -->
          <tr v-if="!state.shotsLoaded">
            <td colspan="7"><div class="skel"><i /><i /><i /><i /></div></td>
          </tr>
          <!-- 空态②：没有镜头表 → 提示先跑拆镜 -->
          <tr v-else-if="!state.shots.length">
            <td colspan="7">
              <div class="emptyrow">
                <b>还没有镜头表</b><br>
                先跑「拆镜」阶段，或把镜头 JSON 放进 shots/ 目录。<br>
                拆镜后这里会列出每一镜的台词、景别、时长与状态。
              </div>
            </td>
          </tr>
          <!-- 空态③：筛选/搜索后为空 → 提示 + 显示全部 -->
          <tr v-else-if="!rows.length">
            <td colspan="7">
              <div class="emptyrow">
                当前筛选下没有镜头。
                <button class="ghost" data-act="clear" title="清掉筛选与搜索">显示全部</button>
              </div>
            </td>
          </tr>

          <template v-else>
            <tr
              v-for="r in rows"
              :key="r.s.id"
              :data-id="r.s.id"
              :class="{ 'is-active': state.selected === r.s.id }"
            >
              <td>
                <div class="scene" :title="sceneLabel(r.s).real ? `场景实体 ${r.s.scene_id}` : '没有场景实体（老数据）'">
                  {{ sceneLabel(r.s).name }}
                  <small v-if="sceneLabel(r.s).sub">{{ sceneLabel(r.s).sub }}</small>
                  <small>#{{ r.idx }}</small>
                </div>
              </td>

              <!-- ① 画面格：缺什么就直接写在格子里，不留白 -->
              <td>
                <div v-if="r.thumb === 'img'" class="thumb">
                  <img loading="lazy" alt="" :src="r.thumbUrl">
                  <span v-if="r.badge" class="badge" :class="r.badgeTone">{{ r.badge }}</span>
                </div>
                <div v-else class="thumb" :class="r.thumb">{{ THUMB_TEXT[r.thumb] }}</div>
                <div class="thumbcap" :title="r.s.id">
                  <span v-if="r.s.locked" class="lockmark" title="已锁定：渲染不会覆盖这一版">🔒</span>
                  {{ r.s.id }} · {{ r.s.frames || '?' }}帧
                </div>
              </td>

              <!-- 台词格：整格是一个按钮，点开详情（旧文件 .cell-edit） -->
              <td>
                <button class="cell-edit" data-act="edit" :data-id="r.s.id" title="点击编辑台词/旁白">
                  <div v-if="r.dice" class="title">{{ r.dice }}</div>
                  <div v-else-if="!r.narr" class="line">（无台词）</div>
                  <div v-if="r.narr" class="line">旁白：{{ r.narr }}</div>
                </button>
              </td>

              <td>
                <div>{{ r.s.shot_size || '—' }}</div>
                <div class="line dim">{{ r.s.camera || '' }}</div>
                <div v-if="r.s.chars.length" class="small dim">{{ r.s.chars.join('、') }}</div>
              </td>

              <td class="tabular">
                {{ fmtMmSs(r.s.sec) }}
                <div class="small dim">{{ r.s.sec }}s</div>
              </td>

              <!-- ② 状态三色：tone 由 kindInfo(kindOf(s)) 给；完成态是 16px 青柠 ✓ 圆标 -->
              <td>
                <div class="shotstatus" :class="`tone-${r.info.tone}`">
                  <span v-if="r.info.ck" class="ck">✓</span>
                  <span v-else class="dot" :class="`tone-${r.info.tone}`" />
                  <span>{{ r.info.t }}</span>
                </div>
                <div v-if="r.qcText" class="small" :class="r.qcTone">{{ r.qcText }}</div>
              </td>

              <!-- ③ 操作按钮随状态改名；disabled 一定带 title 说明原因 -->
              <td>
                <div class="ops">
                  <button
                    v-for="op in r.ops"
                    :key="op.act"
                    :data-act="op.act"
                    :data-id="r.s.id"
                    :disabled="op.disabled"
                    :title="op.title"
                  >{{ op.label }}</button>
                </div>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '@/api/client'
import type { ShotRow } from '@/api/types'
import type { AnyKind } from '@/stores/app'
import {
  FILTERS, fmtMmSs, kindInfo, kindOf, opsFor, refreshShots, refreshStatus,
  sceneLabel, state, visibleShots,
} from '@/stores/app'
import { openShot } from '@/stores/ui'

type KindInfo = ReturnType<typeof kindInfo>
/** 操作按钮的动作码：表格里只有「渲染/重渲」「编辑」，空态里还有「显示全部」。 */
type Act = 'regen' | 'edit' | 'clear'

interface RowOp {
  act: Act
  label: string
  disabled: boolean
  title: string
}

/** 一行要显示的所有派生值：模板里只读，不再做判断（旧文件是在 map 里现算的）。 */
interface Row {
  s: ShotRow
  idx: number
  info: KindInfo
  /** 画面格的四态：有产物 / 生成中 / 渲染失败 / 待生成 */
  thumb: 'img' | 'rendering' | 'failed' | 'empty'
  thumbUrl: string
  /** QC 角标：'' | 'QC' | '可疑' */
  badge: string
  badgeTone: 'bad' | 'warn' | ''
  /** 状态列下面那行 QC 文案（pass/无结论时为空） */
  qcText: string
  qcTone: 'tone-bad' | 'tone-warn'
  dice: string
  narr: string
  ops: RowOp[]
}

/** ① 画面格里的字：没有产物不留白，直接把状态写在格子里。 */
const THUMB_TEXT: Record<Row['thumb'], string> = {
  img: '',
  rendering: '生成中',
  failed: '渲染失败',
  empty: '待生成',
}

/** 全表序号（旧文件 idxOf：在**完整**镜头表里的 1-based 位置，不随筛选变） */
const idxOf = computed(() => new Map(state.shots.map((s, i) => [s.id, i + 1] as const)))

/** ③ 操作按钮随状态改名。有产物「重渲」、无产物「渲染」（取 store 的 opsFor，别处改名只改一处）。 */
function opsForRow(s: ShotRow, kind: AnyKind): RowOp[] {
  const list: RowOp[] = []
  if (kind === 'rendering') {
    list.push({ act: 'regen', label: '生成中…', disabled: true, title: '该镜正在生成' })
  } else {
    const op = opsFor(s)
    const has = !!(s.clip && s.clip.exists)
    list.push({
      act: 'regen',
      label: op.primary,
      disabled: op.primaryDisabled,
      title: state.running
        ? '项目里有任务在跑（同项目同时只允许一个），可先停止或等它结束'
        : (has ? '用当前提示词强制重渲这一镜' : '按当前提示词生成这一镜'),
    })
  }
  list.push({ act: 'edit', label: '编辑', disabled: false, title: '打开详情：改台词/旁白/六段式提示词' })
  return list
}

/** 行数据来自 visibleShots（computed），派生值一次算清。 */
const rows = computed<Row[]>(() =>
  visibleShots.value.map((s) => {
    const kind = kindOf(s)
    const info = kindInfo(kind)
    const has = !!(s.clip && s.clip.exists)
    const qv = (s.qc && s.qc.verdict) || ''
    const badge = qv === 'fail' || qv === 'error' ? 'QC' : (qv === 'suspicious' ? '可疑' : '')
    return {
      s,
      idx: idxOf.value.get(s.id) || 0,
      info,
      thumb: has ? 'img' : (kind === 'rendering' ? 'rendering' : (kind === 'failed' ? 'failed' : 'empty')),
      thumbUrl: has ? api.thumbUrl(state.project, s.id, s.clip.mtime) : '',
      badge,
      badgeTone: badge ? (qv === 'suspicious' ? 'warn' : 'bad') : '',
      qcText: qv && qv !== 'pass' ? `QC ${qv}` : '',
      qcTone: qv === 'suspicious' ? 'tone-warn' : 'tone-bad',
      dice: (s.dialogue || '').trim(),
      narr: (s.narration || '').trim(),
      ops: opsForRow(s, kind),
    }
  }),
)

/** 表头副标题（旧 renderFilters 末尾那段 mainSub） */
const subText = computed(() => {
  const label = FILTERS.find((f) => f[0] === state.filter)?.[1] || ''
  return (state.filter === 'all' ? '' : `筛选：${label} · `)
    + `${visibleShots.value.length} / ${state.shots.length} 镜`
    + (state.q ? ` · 搜索「${state.q}」` : '')
})

/* ---------------- 选中联动：把选中行滚进视野（旧 selectShot 的 scrollIntoView） ---------------- */
const tbodyEl = ref<HTMLTableSectionElement | null>(null)
watch(
  () => state.selected,
  async (id) => {
    if (!id) return
    await nextTick()
    const list = tbodyEl.value ? [...tbodyEl.value.querySelectorAll<HTMLElement>('tr[data-id]')] : []
    list.find((tr) => tr.dataset.id === id)?.scrollIntoView({ block: 'nearest' })
  },
)

/* ---------------- 点击：一处状态驱动，一个 listener ---------------- */
function pick(id: string, open: boolean): void {
  if (!id) return
  state.selected = id
  if (open) openShot(id)
}

function clearFilter(): void {
  state.filter = 'all'
  state.q = ''
}

/** 旧 #shotsRefresh：重新拉镜头表 */
async function reload(): Promise<void> {
  if (!state.project) return
  try {
    await refreshShots()
  } catch (e) {
    ElMessage.error(`镜头表获取失败：${(e as Error).message}`)
  }
}

/** 单镜渲染 / 重渲。
 *  ⚠️ 旧文件打 `/api/rerender {shot_id}`；client.ts 的 `api.rerender(project, ids)` 发的是 `ids`，
 *  而后端 api_rerender 只读 `shot_id`/`shot`、`_start()` 只认 `only` —— 直接调会 400。
 *  这里用与后端 api_rerender 完全等价的调用：`_start({stage:'render'}, only=[shot], force=True)`。 */
async function rerender(s: ShotRow): Promise<void> {
  if (!state.project) return
  if (!window.confirm(`强制重渲镜头 ${s.id}？`)) return
  try {
    const r = await api.run(state.project, 'render', { force: true, only: [s.id] })
    ElMessage.success(r.message || `已开始重渲 ${s.id}`)
    window.setTimeout(() => { void refreshStatus(true) }, 300)
  } catch (e) {
    ElMessage.error(`重渲失败：${(e as Error).message}`)
  }
}

function onTableClick(e: MouseEvent): void {
  const el = e.target as HTMLElement | null
  if (!el || typeof el.closest !== 'function') return
  const btn = el.closest<HTMLElement>('button[data-act]')
  if (btn) {
    const id = btn.dataset.id || ''
    const act = btn.dataset.act as Act | undefined
    if (act === 'edit') { pick(id, true); return }
    if (act === 'clear') { clearFilter(); return }
    if (act === 'regen') {
      const row = rows.value.find((r) => r.s.id === id)
      if (row) void rerender(row.s)
      return
    }
  }
  const tr = el.closest<HTMLElement>('tr[data-id]')
  if (tr?.dataset.id) pick(tr.dataset.id, true)
}
</script>

<style scoped>
.mainhead {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap; flex: 0 0 auto;
  padding: 7px 10px; border-bottom: 1px solid var(--line); background: #fbfbfc;
}
.tablewrap { flex: 1 1 auto; min-height: 0; overflow: auto; }

button.ghost {
  font: inherit; font-size: 12px; padding: 4px 9px; cursor: pointer;
  background: transparent; border: 1px solid transparent; border-radius: var(--radius-sm); color: var(--muted);
}
button.ghost:hover { background: var(--hover); border-color: #d8e6b4; }

.line { color: #4b5563; font-size: 12px; line-height: 1.5; }

/* QC 角标（旧 .thumb .badge + .tag bad/warn；theme.css 给了位置，这里补底色） */
.thumb .badge {
  position: absolute; right: 3px; top: 3px; font-size: 9.5px; padding: 0 4px;
  border-radius: 999px; border: 1px solid var(--line); background: rgba(255, 255, 255, 0.92);
}
.thumb .badge.bad { background: #fdeceb; color: var(--bad); border-color: #eec2bf; }
.thumb .badge.warn { background: #fdf3e2; color: var(--warn); border-color: #f0d9ae; }
.thumb.failed { color: var(--bad); }
.thumb.rendering { color: var(--run); }

/* E1 锁定标记：theme.css 的 .lockmark 已失效，这里补回（旧 tr.is-locked .lockmark 的琥珀色） */
.lockmark { font-size: 10px; color: #8a5a12; }

.ops { display: flex; flex-direction: column; gap: 4px; align-items: stretch; }
.ops button {
  font: inherit; font-size: 11.5px; padding: 3px 6px; white-space: nowrap; cursor: pointer;
  background: var(--card); border: 1px solid var(--line); border-radius: var(--radius-sm); color: inherit;
}
.ops button:hover:not(:disabled) { border-color: color-mix(in srgb, var(--lime) 70%, var(--ink)); }
.ops button:disabled { opacity: 0.45; cursor: not-allowed; }

/* 状态点（完成态用 theme.css 的 .shotstatus .ck 青柠 ✓ 圆标） */
.dot { width: 8px; height: 8px; border-radius: 50%; flex: 0 0 auto; background: #c7ccd4; }
.dot.tone-ok { background: var(--ok); }
.dot.tone-warn { background: var(--warn); }
.dot.tone-bad { background: var(--bad); }
.dot.tone-run { background: var(--run); }
.dot.tone-idle { background: #c7ccd4; }
</style>
