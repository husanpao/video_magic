<template>
  <div class="tabroot">
    <div class="summary">
      <template v-if="!eps.length">还没有成片</template>
      <template v-else>
        <!-- ★ 集选择器（2026-09-24）：逐章出集后有两集（EP01/EP02），
             原来只读 `state.final` 单个字段，**EP02 永远看不到**。 -->
        <span v-if="eps.length > 1" class="epsel">
          <button
            v-for="e in eps"
            :key="e.stem"
            class="epbtn"
            :class="{ on: e.stem === current }"
            @click="current = e.stem"
          >{{ e.stem }}</button>
        </span>
        <span class="name">{{ currentEp?.name || state.final?.name }}</span>
        <span class="small">{{ fmtSize(currentEp?.size || state.final?.size || 0) }}</span>
        <span class="small">{{ mtimeText }}</span>
        <span class="spacer"></span>
        <a class="dl" :href="downloadUrl" download>下载</a>
      </template>
    </div>

    <div class="tabbody">
      <div v-if="!eps.length" class="emptyrow">
        <b>还没有成片</b><br>先跑「合成」阶段（clips/ 里的片段按镜头表拼接）。
      </div>
      <template v-else>
        <video
          :key="videoKey"
          ref="videoEl"
          class="finalvid"
          controls
          preload="metadata"
          :src="videoUrl"
          @loadedmetadata="syncNow"
          @timeupdate="syncNow"
          @seeked="syncNow"
        ></video>

        <!-- 分段进度条：每段 flex ∝ 该镜 sec_actual —— 既是进度条、又是时长分布、又是导航。
             段底色按质检/状态：绿(通过) / 黄(可疑·需重渲) / 红(不合格·缺产物)。
             ★ U8（2026-09-25）：整条可**拖拽 scrub**（按下拖动即连续定位），
             hover 弹出该镜缩略图+台词，点段联动 state.selected（镜头表/胶片条跟着亮）。 -->
        <div ref="wrapEl" class="segwrap">
          <div
            ref="barEl"
            class="segbar"
            role="group"
            data-testid="timeline-scrub"
            aria-label="分镜进度条：点段跳转，按住左右拖动可逐帧定位"
            @pointerdown="onDown"
            @pointermove="onMove"
            @mouseleave="onLeave"
          >
            <div
              v-for="seg in segs"
              :key="seg.id"
              class="seg"
              data-testid="timeline-seg"
              :class="[seg.tone, { now: seg.i === cur }]"
              :style="{ flex: seg.flex }"
              :title="seg.title"
              :data-i="seg.i"
              :aria-label="seg.title"
              tabindex="0"
              role="button"
              @click="onSegClick(seg)"
              @keydown.enter.prevent="onSegClick(seg)"
              @keydown.space.prevent="onSegClick(seg)"
            ></div>
          </div>
          <div class="seglegend small dim">
            <span>{{ nowText }}</span>
            <span class="spacer"></span>
            <span class="scrubhint">按住拖动可定位 · 悬停看缩略图</span>
            <span><i class="sw ok"></i>通过</span>
            <span><i class="sw warn"></i>可疑</span>
            <span><i class="sw bad"></i>不合格</span>
          </div>

          <!-- hover 预览卡：该镜缩略图 + 台词（U8 的核心 —— 不用点开弹层就能"看见这一镜说什么"）。
               放在 segwrap 内、segbar 之上：鼠标从条移向卡片时不算离开，卡片不会闪没。 -->
          <div
            v-if="hoverSeg"
            class="segpreview"
            :style="{ left: hoverX + 'px' }"
            role="tooltip"
          >
            <img v-if="hoverSeg.thumb" class="pvimg" :src="hoverSeg.thumb" alt="" loading="lazy">
            <div v-else class="pvimg ph">无画面</div>
            <div class="pvbody">
              <div class="pvhead">
                <b class="tabular">{{ hoverSeg.id }}</b>
                <span class="dim small">{{ hoverSeg.kindT }}</span>
                <span class="spacer"></span>
                <span class="dim small num">{{ hoverSeg.startText }} · {{ hoverSeg.dur.toFixed(1) }}s</span>
              </div>
              <div class="pvline">{{ hoverSeg.line }}</div>
            </div>
            <!-- 「详情」走 stores/ui 的 openShot（与胶片条点击同一入口）：
                 点段本身只联动选中不弹窗，避免打断播放；要编辑再按这里。 -->
            <button class="pvbtn" @click="openDetail">详情</button>
          </div>
        </div>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 成片 + 分段播放器。
 *
 * 移植自 index.html 的 `renderFinal()` / `renderSegbar()`：
 *   成片 video（api.finalUrl）+ 文件名 / 大小 / 时间 / 下载链接（api.finalDownloadUrl），
 *   分段条每段 flex ∝ 该镜 `sec_actual`，点某段跳到该镜起点，播放时高亮当前段。
 *
 * ★ 两个已踩过的坑都在这：
 *   1) `sec_actual` 累计（265.844s）与成片实际时长（265.875s）有微小差异，
 *      必须按比例缩放 `v.duration / total`，否则越往后点得越偏；
 *   2) 52 段各自 Math.round(dur/total*1000) 的舍入累积会让总宽只有 984/1000，
 *      进度条右端留空（看着像"还有没渲的段落"）→ **最后一段吸收余量 drift**。
 *
 * U8 成片时间线体验（2026-09-25）：
 *   · **scrub**：条上按下即进入拖拽定位（位移 >3px 才算拖拽，避免误伤点击），
 *     拖到哪播到哪；松手时把最近的段写进 state.selected —— 镜头表/胶片条跟着亮。
 *   · **hover 预览**：不用点开详情就能看到该镜缩略图 + 台词/旁白（问题镜可视化定位）。
 *   · **点段联动**：只写 state.selected + 跳播放头，**不弹详情** —— 弹层会盖住播放器
 *     打断审片；真要编辑点预览卡上的「详情」。
 *
 * `state.final` 与 `state.shots` 都由 App.vue 的轮询维护，这里不拉数据。
 */
import { computed, onUnmounted, ref, watch } from 'vue'
import { api } from '@/api/client'
import { fmtMmSs, fmtSize, kindInfo, kindOf, segTimeline, state } from '@/stores/app'
import { openShot } from '@/stores/ui'

interface Seg {
  i: number
  id: string
  tone: 'ok' | 'warn' | 'bad'
  flex: string
  title: string
  /** 预览卡用：起点（秒）/ 时长 / 台词或旁白 / 缩略图 / 状态中文 */
  start: number
  dur: number
  line: string
  thumb: string
  kindT: string
  startText: string
}

const videoEl = ref<HTMLVideoElement | null>(null)
const barEl = ref<HTMLElement | null>(null)
const wrapEl = ref<HTMLElement | null>(null)
/** 播放头落在第几段。 */
const cur = ref(0)
const duration = ref(0)
/** hover 预览：只存段号与横向位置（内容由 segs 派生，不重复存）。 */
const hover = ref<{ i: number; x: number } | null>(null)

const eps = computed(() => state.finals || [])
const current = ref('')
// 默认选中**最新一集**（刚跑完合成时你的注意力在那）
watch(eps, (v) => { if (v.length && !v.some((e) => e.stem === current.value)) current.value = v[v.length - 1].stem },
  { immediate: true })
const currentEp = computed(() => eps.value.find((e) => e.stem === current.value) || eps.value[eps.value.length - 1])
const videoUrl = computed(() => api.finalUrl(state.project, currentEp.value?.stem || 'EP01'))
const downloadUrl = computed(() => api.finalDownloadUrl(state.project, currentEp.value?.stem || 'EP01'))
/** 重新合成后 mtime/size 变 → 换 key 强制重建 <video>，否则还播着旧文件。 */
const videoKey = computed(() =>
  state.final ? `${state.final.mtime}:${state.final.size}` : 'none')
const mtimeText = computed(() =>
  state.final ? new Date(state.final.mtime * 1000).toLocaleString() : '')

const segs = computed<Seg[]>(() => {
  const shots = state.shots
  if (!shots.length) return []
  const { cum, total } = segTimeline(shots)
  const durs = shots.map((s) => Number(s.sec_actual || s.sec || 0))
  const denom = total > 0 ? total : 1
  const weights = durs.map((d) => Math.max(3, Math.round((d / denom) * 1000)))
  // ★ 让最后一段吸收舍入余量，保证总宽正好 1000
  const drift = 1000 - weights.reduce((a, b) => a + b, 0)
  if (weights.length) weights[weights.length - 1] = Math.max(3, weights[weights.length - 1] + drift)
  return shots.map((s, i) => {
    const k = kindOf(s)
    const qv = (s.qc && s.qc.verdict) || ''
    let tone: Seg['tone'] = 'ok'
    if (!(s.clip && s.clip.exists)) tone = 'bad'
    else if (qv === 'fail' || qv === 'error') tone = 'bad'
    else if (qv === 'suspicious' || k === 'stale' || k === 'qc_failed') tone = 'warn'
    return {
      i,
      id: s.id,
      tone,
      flex: `${weights[i]} 0 0`,
      title: `${s.id} · ${durs[i].toFixed(2)}s · 起点 ${fmtMmSs(cum[i])} · ${kindInfo(k).t}`,
      start: cum[i] ?? 0,
      dur: durs[i],
      line: (s.dialogue || s.narration || '').trim() || '（无台词 / 旁白）',
      thumb: s.clip && s.clip.exists ? api.thumbUrl(state.project, s.id, s.clip.mtime) : '',
      kindT: kindInfo(k).t,
      startText: fmtMmSs(cum[i] ?? 0),
    }
  })
})

const hoverSeg = computed(() => (hover.value ? segs.value[hover.value.i] : null))
const hoverX = computed(() => hover.value?.x ?? 0)

const nowText = computed(() => {
  const shots = state.shots
  const s = shots[cur.value]
  if (!duration.value || !shots.length || !s) return '点任意分段跳到该镜'
  const { cum } = segTimeline(shots)
  return `当前：第 ${cur.value + 1}/${shots.length} 镜 ${s.id}（起点 ${fmtMmSs(cum[cur.value] ?? 0)}）`
})

/** timeupdate / loadedmetadata / seeked 共用：把播放头换算成段号并高亮。 */
function syncNow() {
  const v = videoEl.value
  if (!v) return
  duration.value = Number.isFinite(v.duration) ? v.duration : 0
  const { cum, total } = segTimeline(state.shots)
  if (!cum.length) return
  // 累计时长与成片实际时长的微小差异按比例缩放，否则越往后越偏
  const scale = total > 0 && v.duration > 0 ? v.duration / total : 1
  const now = v.currentTime / (scale || 1)
  let i = 0
  for (let k = 0; k < cum.length; k++) if (now + 1e-6 >= cum[k]) i = k
  cur.value = i
}

/** 点某段 → 跳到该镜起点（同样按比例换算到成片时间轴）。 */
function seek(i: number) {
  const v = videoEl.value
  if (!v || !v.duration) return
  const { cum, total } = segTimeline(state.shots)
  const scale = total > 0 ? v.duration / total : 1
  v.currentTime = (cum[i] ?? 0) * scale
  cur.value = i
  void v.play().catch(() => { /* 浏览器不允许自动播放时忽略 */ })
}

// ── U8：scrub 拖拽 / hover 预览 / 点段联动 ─────────────────────────────────

/** 指针 x → 时间轴秒数（按条宽均分到总时长 —— 段宽本身就是 ∝ 时长的）。 */
function timeAt(clientX: number): number {
  const el = barEl.value
  if (!el) return 0
  const r = el.getBoundingClientRect()
  const frac = r.width > 0 ? Math.min(1, Math.max(0, (clientX - r.left) / r.width)) : 0
  const { total } = segTimeline(state.shots)
  return frac * total
}

function segIndexAt(t: number): number {
  const { cum } = segTimeline(state.shots)
  let i = 0
  for (let k = 0; k < cum.length; k++) if (t + 1e-6 >= cum[k]) i = k
  return i
}

/** 某段中心相对 segwrap 的横向位置（预览卡定位用，两端夹住不出界）。 */
function segCenterX(i: number): number {
  const wrap = wrapEl.value
  const bar = barEl.value
  if (!wrap || !bar) return 0
  const el = bar.querySelector<HTMLElement>(`.seg[data-i="${i}"]`)
  if (!el) return 0
  const wr = wrap.getBoundingClientRect()
  const er = el.getBoundingClientRect()
  const half = (er.width + 90) / 2 // 90 ≈ 预览卡半宽的下限，粗夹即可
  return Math.min(wr.width - half, Math.max(half, er.left - wr.left + er.width / 2))
}

function selectSeg(i: number) {
  const s = state.shots[i]
  // 联动：镜头表 / 胶片条 / ShotNav 都在 watch state.selected
  if (s && state.selected !== s.id) state.selected = s.id
}

/** scrub 到指针位置：换算时间 + 高亮段 + 联动选中。 */
function scrubTo(clientX: number) {
  const v = videoEl.value
  if (!v || !v.duration) return
  const { total } = segTimeline(state.shots)
  const scale = total > 0 ? v.duration / total : 1
  const t = timeAt(clientX)
  v.currentTime = t * scale
  const i = segIndexAt(t)
  cur.value = i
  selectSeg(i)
}

let dragging = false
/** 本次按下是否已演变成拖拽（>3px）。演变成拖拽后要吞掉随后的 click，避免"拖完又跳回段起点"。 */
let dragged = false
let startX = 0

function onDown(e: PointerEvent) {
  if (e.button !== 0 || !segs.value.length) return
  dragging = true
  dragged = false
  startX = e.clientX
  hover.value = null // 拖拽时预览卡挡视野
  // 监听挂 window（而不是 setPointerCapture）：
  // capture 会把后续 click 的 target 重定向到条上，段的 @click 就收不到了。
  window.addEventListener('pointermove', onWinMove)
  window.addEventListener('pointerup', onWinUp)
  window.addEventListener('pointercancel', onWinUp)
}

function onWinMove(e: PointerEvent) {
  if (!dragging) return
  if (!dragged && Math.abs(e.clientX - startX) < 3) return // 位移不足 → 还是"点击"
  dragged = true
  scrubTo(e.clientX)
}

function onWinUp(e: PointerEvent) {
  if (!dragging) return
  if (dragged) scrubTo(e.clientX) // 松手定格在最终位置
  dragging = false
  window.removeEventListener('pointermove', onWinMove)
  window.removeEventListener('pointerup', onWinUp)
  window.removeEventListener('pointercancel', onWinUp)
}

function onSegClick(seg: Seg) {
  if (dragged) { dragged = false; return } // 刚才是拖拽，吞掉这次合成 click
  seek(seg.i)
  selectSeg(seg.i) // 点段联动 state.selected（镜头表/胶片条跟着亮）
}

/** 条上移动（非拖拽）→ 显示该镜缩略图 + 台词。 */
function onMove(e: PointerEvent) {
  if (dragging || !segs.value.length) return
  const i = segIndexAt(timeAt(e.clientX))
  hover.value = { i, x: segCenterX(i) }
}

function onLeave() {
  hover.value = null
}

function openDetail() {
  const s = hoverSeg.value
  if (s) openShot(s.id)
}

onUnmounted(() => {
  window.removeEventListener('pointermove', onWinMove)
  window.removeEventListener('pointerup', onWinUp)
  window.removeEventListener('pointercancel', onWinUp)
})

// 换项目/重新合成后播放头归零
watch(() => [state.project, videoKey.value], () => {
  cur.value = 0
  duration.value = 0
  hover.value = null
})
</script>

<style scoped>
.finalvid {
  width: 100%;
  max-height: 260px;
  background: #000;
  border-radius: var(--radius-sm);
  border: 1px solid var(--line);
}
.segwrap {
  margin-top: 10px;
  /* 预览卡的定位锚 */
  position: relative;
}
.name {
  font-weight: 600;
}
.dl {
  font-size: 12px;
}
.epsel { display: inline-flex; gap: 3px; margin-right: 6px; }
.epbtn {
  font: inherit; font-size: 11px; padding: 2px 9px; border-radius: 6px; cursor: pointer;
  border: 1px solid var(--line); background: var(--card); color: var(--muted);
}
.epbtn.on { border-color: var(--lime); color: var(--ink); font-weight: 700; background: var(--lime-pale); }

/* ── U8 hover 预览卡：缩略图 + 台词，一眼看清"这一镜在说什么" ── */
.segpreview {
  position: absolute;
  bottom: 34px;               /* 悬在分段条上方，不挡条本身 */
  transform: translateX(-50%);
  display: flex;
  gap: 8px;
  align-items: stretch;
  width: 320px;
  max-width: 90%;
  padding: 7px;
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  box-shadow: var(--shadow);
  z-index: 5;
  pointer-events: auto;
}
.pvimg {
  width: 96px;
  height: 54px;
  object-fit: cover;
  border-radius: 5px;
  flex: 0 0 auto;
  background: var(--hover);
}
.pvimg.ph {
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 10.5px;
  color: var(--muted);
}
.pvbody { flex: 1 1 auto; min-width: 0; }
.pvhead { display: flex; align-items: center; gap: 6px; font-size: 11.5px; }
.pvline {
  margin-top: 3px;
  font-size: 11.5px;
  line-height: 1.5;
  color: var(--ink);
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.pvbtn {
  flex: 0 0 auto;
  align-self: flex-end;
  font: inherit;
  font-size: 11px;
  padding: 2px 9px;
  border-radius: 6px;
  cursor: pointer;
  border: 1px solid var(--line);
  background: var(--card);
  color: var(--ink);
}
.pvbtn:hover { border-color: var(--lime); }
.scrubhint { margin-right: 4px; }
</style>
