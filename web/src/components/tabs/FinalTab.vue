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
             段底色按质检/状态：绿(通过) / 黄(可疑·需重渲) / 红(不合格·缺产物)。 -->
        <div class="segwrap">
          <div class="segbar" role="navigation" aria-label="分镜进度条">
            <div
              v-for="seg in segs"
              :key="seg.id"
              class="seg"
              :class="[seg.tone, { now: seg.i === cur }]"
              :style="{ flex: seg.flex }"
              :title="seg.title"
              @click="seek(seg.i)"
            ></div>
          </div>
          <div class="seglegend small dim">
            <span>{{ nowText }}</span>
            <span class="spacer"></span>
            <span><i class="sw ok"></i>通过</span>
            <span><i class="sw warn"></i>可疑</span>
            <span><i class="sw bad"></i>不合格</span>
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
 * `state.final` 与 `state.shots` 都由 App.vue 的轮询维护，这里不拉数据。
 */
import { computed, ref, watch } from 'vue'
import { api } from '@/api/client'
import { fmtMmSs, fmtSize, kindInfo, kindOf, segTimeline, state } from '@/stores/app'

interface Seg {
  i: number
  id: string
  tone: 'ok' | 'warn' | 'bad'
  flex: string
  title: string
}

const videoEl = ref<HTMLVideoElement | null>(null)
/** 播放头落在第几段。 */
const cur = ref(0)
const duration = ref(0)

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
    }
  })
})

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

// 换项目/重新合成后播放头归零
watch(() => [state.project, videoKey.value], () => {
  cur.value = 0
  duration.value = 0
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
.epbtn.on { border-color: var(--lime); color: var(--ink); font-weight: 700; background: #f2f8e8; }
</style>
