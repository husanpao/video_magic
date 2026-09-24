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

  本版新增（U1 / U2 / U10）：
  · **多选 + 批量操作栏**：每行复选框（藏在「场景」列左侧，不加第 8 列 —— 表头 7 列是 E2E 断言，
    也是"复选框不该抢一列宽"的常识）+「全选当前筛选」+ 浮动批量条：批量重渲 / 锁定 / 解锁 /
    改运镜 / 删除。api.bulk / api.rerenderBulk 的契约终于有人调用了。
  · **写后即刷 + 乐观更新**：渲染提交本地先标「生成中」（renderHint），不等 4s 轮询。
  · 统一确认 confirmAction（替换 window.confirm）。
-->
<template>
  <section class="col shotsec">
    <div class="mainhead">
      <b>镜头表</b>
      <span class="dim small">{{ subText }}</span>
      <span class="spacer" />
      <button
        class="ghost"
        data-testid="bulk-select-all"
        :title="allVisibleChecked ? '取消当前筛选下的全部选中' : '把当前筛选下所有镜头都选中（批量操作用）'"
        @click="toggleCheckAllVisible"
      >{{ allVisibleChecked ? '取消全选' : '全选当前筛选' }}</button>
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
              :class="{ 'is-active': state.selected === r.s.id, 'is-checked': r.checked }"
            >
              <td class="scenecell">
                <!-- 复选框（U1 多选）：**放在场景格左缘**而不是加一整列 ——
                     表头固定 7 列（E2E 有断言），而且复选框不值得抢走一列的宽度。
                     点它只翻转选中，不开详情（事件委托里按 data-act=sel 分发）。 -->
                <input
                  class="rowsel"
                  type="checkbox"
                  data-act="sel"
                  :data-id="r.s.id"
                  :checked="r.checked"
                  :title="`选中 ${r.s.id}（批量重渲/锁定/删除用）`"
                >
                <div class="scene" :title="sceneLabel(r.s).real ? `场景实体 ${r.s.scene_id}` : '没有场景实体（老数据）'">
                  {{ sceneLabel(r.s).name }}
                  <small v-if="sceneLabel(r.s).sub">{{ sceneLabel(r.s).sub }}</small>
                  <small>#{{ r.idx }}</small>
                </div>
              </td>

              <!-- ① 画面格：缺什么就直接写在格子里，不留白 -->
              <td>
                <div v-if="r.thumb === 'img'" class="thumb">
                  <!-- 缩略图异步化（S9）的前端配合：后端未命中缓存时最多等 2.5s，
                       等不到回**占位图 + X-Thumb-Pending: 1**。<img> 读不到响应头，
                       但占位图是固定 360×240、真缩略图按 16:9 片段出 360×200 上下 ——
                       用尺寸嗅探占位图（onThumbLoad），延迟换 t 重试，做完即变真图；
                       加载失败（onThumbError）也重试一次。都只试有限次数，不打循环。 -->
                  <img loading="lazy" alt="" :src="r.thumbUrl" @load="onThumbLoad" @error="onThumbError">
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

    <!-- 浮动批量操作栏（U1）：勾了镜才出现。为什么不放表头里 ——
         它是"对已选集合的动作"，跟着选择走、悬浮在表格底部最顺手。 -->
    <div v-if="state.checked.length" class="bulkbar" data-testid="bulk-bar">
      <span class="tabular">已选 {{ state.checked.length }} 镜</span>
      <!-- 兼容两份 testid 清单（btn-bulk-rerender / bulk-rerender 都给） -->
      <span data-testid="bulk-rerender" class="aliaswrap">
        <button
          data-testid="btn-bulk-rerender"
          :disabled="state.running"
          :title="state.running ? '有任务在跑，先等它结束' : `强制重渲选中的 ${state.checked.length} 镜（一个任务覆盖多镜）`"
          @click="bulkRerender"
        >批量重渲</button>
      </span>
      <button
        data-testid="btn-bulk-lock"
        :title="`锁定选中的 ${state.checked.length} 镜（渲染不会覆盖锁定版）`"
        @click="bulkLock(true)"
      >批量锁定</button>
      <button :title="`解锁选中的 ${state.checked.length} 镜`" @click="bulkLock(false)">解锁</button>
      <button
        :title="`把选中的 ${state.checked.length} 镜改成同一个运镜（同步提示词纪律行）`"
        @click="bulkCamera"
      >改运镜…</button>
      <button class="danger" :title="`删除选中的 ${state.checked.length} 镜（可撤销）`" @click="bulkDelete">删除选中</button>
      <button class="ghost" title="清掉多选（不影响筛选）" @click="state.checked = []">清除选择</button>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api/client'
import type { ShotRow } from '@/api/types'
import type { AnyKind } from '@/stores/app'
import {
  allVisibleChecked, FILTERS, fmtMmSs, isChecked, kindInfo, kindOf, markRendering, opsFor,
  pushEdit, refreshAfterEdit, refreshShots, refreshStatus, rerenderOne, sceneLabel, state,
  toggleCheckAllVisible, toggleChecked, visibleShots,
} from '@/stores/app'
import { openShot } from '@/stores/ui'
import { confirmAction } from '@/composables/confirmAction'

type KindInfo = ReturnType<typeof kindInfo>
/** 操作按钮的动作码 + 复选框（sel）。空态里还有「显示全部」。 */
type Act = 'regen' | 'edit' | 'clear' | 'sel'

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
  /** 多选（U1） */
  checked: boolean
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
      checked: isChecked(s.id),
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

/** 旧 #shotsRefresh：重新拉镜头表（失败走全局错误条，refreshShots 内部已 guard） */
async function reload(): Promise<void> {
  if (!state.project) return
  await refreshShots()
}

/** 单镜渲染 / 重渲：确认 + 提交 + 乐观标「生成中」都在 store.rerenderOne（快捷键 R 共用）。 */
function onTableClick(e: MouseEvent): void {
  const el = e.target as HTMLElement | null
  if (!el || typeof el.closest !== 'function') return
  const actEl = el.closest<HTMLElement>('[data-act]')
  if (actEl) {
    const id = actEl.dataset.id || ''
    const act = actEl.dataset.act as Act | undefined
    if (act === 'sel') {
      // 复选框：浏览器已经把 checked 翻好了，读它当前值同步进 store
      toggleChecked(id, (actEl as HTMLInputElement).checked)
      return
    }
    if (act === 'edit') { pick(id, true); return }
    if (act === 'clear') { clearFilter(); return }
    if (act === 'regen') {
      const row = rows.value.find((r) => r.s.id === id)
      if (row) void rerenderOne(row.s)
      return
    }
  }
  const tr = el.closest<HTMLElement>('tr[data-id]')
  if (tr?.dataset.id) pick(tr.dataset.id, true)
}

/** 换一个 t 重拉缩略图（破缓存）。 */
function bumpThumb(img: HTMLImageElement): void {
  try {
    const u = new URL(img.src, window.location.href)
    u.searchParams.set('t', String(Date.now()))
    img.src = u.toString()
  } catch { /* URL 构造失败就保持当前图，不重试 */ }
}

/** 嗅探「占位图」（X-Thumb-Pending 的 <img> 侧替代判据）：
 *  占位图固定 360×240，16:9 片段的真缩略图是 360×202 上下 —— 撞不上。
 *  还在生成 → 2.5s 后换 t 再要一次，最多 4 轮（后台 ffmpeg 最坏 25s，
 *  4 轮 × 2.5s 覆盖不到全部，但覆盖了"马上就好"的绝大多数；剩下的手动刷新）。 */
function onThumbLoad(e: Event): void {
  const img = e.target as HTMLImageElement | null
  if (!img || img.naturalWidth !== 360 || img.naturalHeight !== 240) return
  const n = Number(img.dataset.pendingRounds || '0')
  if (n >= 4) return
  img.dataset.pendingRounds = String(n + 1)
  window.setTimeout(() => bumpThumb(img), 2500)
}

/** 缩略图容错：失败只重试一次（带新 t 破缓存）。慢图/断链不用刷新页面才变真。 */
function onThumbError(e: Event): void {
  const img = e.target as HTMLImageElement | null
  if (!img || img.dataset.retried === '1') return
  img.dataset.retried = '1'
  window.setTimeout(() => bumpThumb(img), 2500)
}

/* ---------------- 批量操作（U1：api.bulk / rerenderBulk 的 UI 出口） ---------------- */

/** 批量重渲：走 `/api/run` 的 `only` 列表（一个任务覆盖多镜，比 N 次单镜省）。
 *  提交后本地先标「生成中」（U2 乐观更新）。渲染不可逆（会替换产物），
 *  但**不进撤销栈** —— 撤销栈管的是镜头表数据，产物重生成没有"逆操作"可言。 */
async function bulkRerender(): Promise<void> {
  const ids = [...state.checked]
  if (!ids.length) return
  const ok = await confirmAction({
    title: '批量重渲',
    message: `强制重渲选中的 ${ids.length} 镜？会用当前提示词重新生成并替换这些版本（🔒 锁定的镜不会被覆盖）。`,
    list: ids,
    confirmText: `重渲 ${ids.length} 镜`,
  })
  if (!ok) return
  try {
    const r = await api.rerenderBulk(state.project, ids)
    markRendering(ids)
    ElMessage.success(r.message || `已提交 ${ids.length} 镜重渲，看顶部进度`)
    window.setTimeout(() => { void refreshStatus() }, 300)
  } catch (e) {
    ElMessage.error(`批量重渲失败：${(e as Error).message}`)
  }
}

/** 批量锁定 / 解锁。一条撤销记录（逆操作 = 整批反向置位），多级撤销对批量同样成立。 */
async function bulkLock(value: boolean): Promise<void> {
  const ids = [...state.checked]
  if (!ids.length) return
  const ok = await confirmAction({
    title: value ? '批量锁定' : '批量解锁',
    message: value
      ? `锁定选中的 ${ids.length} 镜？锁定版渲染不会覆盖（需显式强制重跑才能改）。`
      : `解锁选中的 ${ids.length} 镜？之后的渲染会正常覆盖它们。`,
    list: ids,
    confirmText: value ? '锁定' : '解锁',
  })
  if (!ok) return
  try {
    const apply = async (v: boolean) => {
      for (const id of ids) await api.lockShot(state.project, id, 'locked', v)
    }
    await apply(value)
    pushEdit({
      label: `${value ? '锁定' : '解锁'} ${ids.length} 镜`,
      undo: () => apply(!value),
      redo: () => apply(value),
    })
    ElMessage.success(`已${value ? '锁定' : '解锁'} ${ids.length} 镜`)
    await refreshAfterEdit()
  } catch (e) {
    ElMessage.error(`批量锁定失败：${(e as Error).message}`)
  }
}

/** 批量改运镜 —— api.bulk 的正牌用途（只收"安全"字段：景别/运镜/时长/服装/动作）。
 *  后端会把运镜**同步进提示词的 CAMERA DISCIPLINE 行**（否则整批"改了不生效"）。 */
async function bulkCamera(): Promise<void> {
  const ids = [...state.checked]
  if (!ids.length) return
  const v = await ElMessageBox.prompt(
    `新的运镜（例：缓推（Push In, slow)）。会同步这 ${ids.length} 镜提示词里的 CAMERA DISCIPLINE 行，并标为待重渲。`,
    `批量改运镜（${ids.length} 镜）`,
    { inputValue: '', confirmButtonText: '改', cancelButtonText: '取消' },
  ).catch(() => null)
  if (!v) return
  const cam = String(v.value || '').trim()
  if (!cam) return
  // 先记旧值（每镜不同 —— 逆操作只能逐镜回写，不能一条 bulk 反着打）
  const olds = ids.map((id) => state.shots.find((s) => s.id === id)?.camera || '')
  try {
    await api.bulk(state.project, { selector: { ids }, patch: { camera: cam } })
    pushEdit({
      label: `批量改运镜 ${ids.length} 镜`,
      undo: async () => {
        for (let i = 0; i < ids.length; i++) await api.updateShot(state.project, ids[i], { camera: olds[i] })
      },
      redo: () => api.bulk(state.project, { selector: { ids }, patch: { camera: cam } }),
    })
    ElMessage.success(`已批量改 ${ids.length} 镜运镜（只标待重渲，不自动渲染）`)
    await refreshAfterEdit()
  } catch (e) {
    ElMessage.error(`批量改运镜失败：${(e as Error).message}`)
  }
}

/** 批量删除。逆操作走后端**单步备份**（shots/*.json.bak）：
 *  批量删除是一次 _apply_edit = 一份 .bak，正好整批恢复；
 *  重做 = 原样再删一遍（确定性）。
 *  ⚠️ 已知边界：删除之后若又做了别的编辑再撤销，.bak 会被新编辑覆盖 ——
 *  单镜删除走的是编辑接口互逆（见 ShotModal），不受这个限制。 */
async function bulkDelete(): Promise<void> {
  const ids = [...state.checked]
  if (!ids.length) return
  const ok = await confirmAction({
    title: '批量删除镜头',
    message: `删除选中的 ${ids.length} 镜？成片按镜头表组装，删除后不再包含它们；产物文件不删。可用顶栏「撤销」（Ctrl+Z）找回。`,
    list: ids,
    confirmText: `删除 ${ids.length} 镜`,
    danger: true,
  })
  if (!ok) return
  try {
    for (const id of ids) await api.deleteShot(state.project, id)
    pushEdit({
      label: `删除 ${ids.length} 镜`,
      undo: () => api.undo(state.project),
      redo: async () => { for (const id of ids) await api.deleteShot(state.project, id) },
    })
    ElMessage.success(`已删除 ${ids.length} 镜`)
    state.checked = []
    await refreshAfterEdit()
  } catch (e) {
    ElMessage.error(`批量删除失败：${(e as Error).message}`)
  }
}
</script>

<style scoped>
.shotsec { position: relative; }
.mainhead {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap; flex: 0 0 auto;
  padding: 7px 10px; border-bottom: 1px solid var(--line); background: var(--surface-2);
}
.tablewrap { flex: 1 1 auto; min-height: 0; overflow: auto; }

button.ghost {
  font: inherit; font-size: 12px; padding: 4px 9px; cursor: pointer;
  background: transparent; border: 1px solid transparent; border-radius: var(--radius-sm); color: var(--muted);
}
button.ghost:hover { background: var(--hover); border-color: var(--hover-border); }

.line { color: var(--text-2); font-size: 12px; line-height: 1.5; }

/* 复选框（U1）：贴在场景格左缘，不占一整列宽度 */
.scenecell { position: relative; padding-left: 21px; }
.rowsel {
  position: absolute; left: 5px; top: 8px; width: 13px; height: 13px;
  accent-color: var(--lime); cursor: pointer; margin: 0;
}
tr.is-checked { background: color-mix(in srgb, var(--lime) 12%, var(--card)); }

/* QC 角标（旧 .thumb .badge + .tag bad/warn；theme.css 给了位置，这里补底色） */
.thumb .badge {
  position: absolute; right: 3px; top: 3px; font-size: 9.5px; padding: 0 4px;
  border-radius: 999px; border: 1px solid var(--line); background: var(--badge-veil);
}
.thumb .badge.bad { background: var(--bad-bg); color: var(--bad); border-color: var(--bad-border); }
.thumb .badge.warn { background: var(--warn-bg); color: var(--warn); border-color: var(--warn-border); }
.thumb.failed { color: var(--bad); }
.thumb.rendering { color: var(--run); }

/* E1 锁定标记：theme.css 的 .lockmark 已失效，这里补回（旧 tr.is-locked .lockmark 的琥珀色） */
.lockmark { font-size: 10px; color: var(--warn); }

.ops { display: flex; flex-direction: column; gap: 4px; align-items: stretch; }
.ops button {
  font: inherit; font-size: 11.5px; padding: 3px 6px; white-space: nowrap; cursor: pointer;
  background: var(--card); border: 1px solid var(--line); border-radius: var(--radius-sm); color: inherit;
}
.ops button:hover:not(:disabled) { border-color: color-mix(in srgb, var(--lime) 70%, var(--ink)); }
.ops button:disabled { opacity: 0.45; cursor: not-allowed; }

/* 状态点（完成态用 theme.css 的 .shotstatus .ck 青柠 ✓ 圆标）。
   .dot.tone-* 三份重复样式已收编进 theme.css（U13），此处不再各抄一份。 */

/* 浮动批量操作栏（U1）：悬浮在表格底部，跟着"有没有选中"出现 */
.bulkbar {
  position: absolute; left: 10px; right: 10px; bottom: 10px; z-index: 5;
  display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
  padding: 7px 11px; font-size: 12px;
  background: var(--card); border: 1px solid var(--line); border-radius: var(--radius);
  box-shadow: var(--shadow);
}
.bulkbar button {
  font: inherit; font-size: 11.5px; padding: 4px 9px; cursor: pointer;
  background: var(--card); border: 1px solid var(--line); border-radius: var(--radius-sm); color: inherit;
}
.bulkbar button:hover:not(:disabled) { border-color: color-mix(in srgb, var(--lime) 70%, var(--ink)); }
.bulkbar button:disabled { opacity: 0.45; cursor: not-allowed; }
.bulkbar button.danger {
  color: var(--bad); border-color: color-mix(in srgb, var(--bad) 45%, var(--line)); font-weight: 600;
}
.bulkbar button.danger:hover { background: color-mix(in srgb, var(--bad) 6%, var(--card)); }
.bulkbar button.ghost { color: var(--muted); border-color: transparent; }
.aliaswrap { display: inline-flex; }
</style>
