<!--
  剧本 tab：**无损**结构化剧本视图。
  · 每句对白带 `src_span` 指回小说原文，后端 `verify()` 做确定性逐字校验。
  · ★ 这一层**不替代小说**作为对白来源 —— 调研负面清单明确写着
    「storyforge 的摘要式链路」是架构级错误（原文第 1 步就丢，对白必丢）。
    所以它是**旁路产物**：拆镜仍直接从小说来；丢了它流水线照跑。
  · 页首把校验结论摆在最显眼处：覆盖率 / 失败数 / 门禁。**失败 > 0 时用红色，不粉饰。**
  · ★ U9（2026-09-25）剧本↔镜头表联动：每行对白按正文匹配到镜头，
    点行直达该镜详情（定位 + 开弹层）；反过来选中镜头会高亮并滚到对应剧本行。
    匹配不上的对白标成「没进任何镜头」—— 审计「台词丢失」的**现场**就在这里。
-->
<template>
  <div ref="rootEl" class="sctab">
    <div class="summary">
      <span>{{ summary }}</span>
      <span class="spacer" />
      <el-button size="small" :loading="loading" @click="load(true)">刷新</el-button>
    </div>

    <el-alert
      v-if="state.scripts.some((c) => c.has_script)"
      :type="gateOk ? 'success' : 'error'"
      :closable="false"
      class="gate"
    >
      <template #title>
        <span v-if="gateOk">
          无损校验通过：所有对白都能在小说原文里<strong>逐字</strong>找到（覆盖率 {{ overallCov ?? 100 }}%）
        </span>
        <span v-else>
          <strong>无损校验未通过</strong>：有对白在原文里找不到 —— 说明被改写或凭空生成了
        </span>
      </template>
    </el-alert>

    <div v-if="hasAny" class="hintline linkhint">
      剧本行已与镜头表互相对接：点<strong>带 → 镜头号的行</strong>直达该镜详情；
      在镜头表选中某镜，这里会自动高亮并滚到对应行。标
      <span class="shotlink lost">⚠ 没进任何镜头</span> 的对白就是审计「台词丢失」的现场。
    </div>

    <div v-if="!hasAny" class="emptyrow">
      <b>还没有剧本</b><br>
      剧本层把小说章节整理成「场次 → 节拍 → 人物 / 对白 / 舞台指示」的<strong>可读视图</strong>。<br>
      命令行：<code>python3 -m vm.script 西游记</code><br>
      <span class="dim">它是旁路产物：拆镜仍然直接从小说走，剧本层丢了流水线照跑。</span>
    </div>

    <div v-for="pc in parsed" :key="pc.c.no" class="ch">
      <div class="chhead">
        <b>第 {{ pc.c.no }} 章 · {{ pc.c.title }}</b>
        <span class="spacer" />
        <span class="tag info">{{ pc.c.beats }} 节拍 / {{ pc.c.lines }} 句</span>
        <!-- 对接统计：一眼看出这章有多少对白真正进了镜头表 -->
        <span class="tag info" :title="'剧本行 ↔ 镜头表互相对接：点行直达该镜'">对接 {{ pc.stat.shots }} 镜</span>
        <span
          v-if="pc.stat.lost"
          class="tag warn losttag"
          title="这些对白没有进入任何镜头 —— 审计「台词丢失」的现场，点这里跳到第一句"
          @click="scrollToLost(pc.c.no)"
        >丢 {{ pc.stat.lost }} 句</span>
        <span
          class="tag"
          :class="pc.c.checks.gate_passed ? 'ok' : 'bad'"
          :title="'覆盖率=逐字可溯率：（逐字通过+仅差标点）÷已校验句数 —— 与门禁同一口径，两句话同真同假'"
        >
          覆盖 {{ covOf(pc.c) ?? (pc.c.checks.gate_passed ? 100 : '-') }}%
        </span>
        <span v-if="pc.c.checks.span_drift" class="tag warn" :title="'LLM 数错偏移，已就地修正（不算失败）'">
          偏移修正 {{ pc.c.checks.span_drift }}
        </span>
      </div>
      <!-- ★ 逐行渲染（不是一整块 <pre>）：每行才能挂「匹配到哪一镜」并可点。
           行内**原文逐字保留**（⟨start-end⟩ 偏移等），只是行尾追加镜头徽标。 -->
      <div class="scripttext">
        <div
          v-for="(ln, i) in pc.lines"
          :key="i"
          class="sl"
          data-testid="script-line"
          :class="[ln.kind, { linked: !!ln.shotId, lost: ln.lost, sel: ln.shotId && ln.shotId === state.selected }]"
          :data-key="`${pc.c.no}:${i}`"
          :title="lineTitle(ln)"
          @click="onLineClick(ln)"
        ><span class="raw">{{ ln.text || ' ' }}</span><span v-if="ln.shotId" class="shotlink" :class="toneOf(ln.shotId)">→ {{ ln.shotId }}</span><span v-else-if="ln.lost" class="shotlink lost">⚠ 没进任何镜头</span></div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * U9 剧本↔镜头表联动（2026-09-25）。
 *
 * 为什么值得做：审计的「台词丢失 / 说话人歧义」finding 说"某句对白没进镜头"，
 * 但剧本 tab 原来是一整块只读文本 —— 用户**找不到是哪句、也到不了那一镜**。
 * 现在：
 *   ① 逐行解析剧本正文（vm/script.py `render()` 的版式：`角色：对白 ⟨start-end⟩`），
 *      按**对白正文**匹配镜头表的 dialogue/narration（归一化后精确相等优先、
 *      互相包含兜底 —— 与 vm/script.match_merged_quotes 同款宽容，防止合并引号误报）。
 *   ② 点剧本行 = state.selected + openShot()（定位 + 开详情），与胶片条点击同一入口。
 *   ③ watch(state.selected) 反向高亮 + 滚动到对应剧本行（镜头 → 剧本）。
 *   ④ 匹配不上的对白标红警示 —— 「台词丢失」的现场直接可见，章头「丢 N 句」可跳过去。
 *
 * 匹配范围按**章**收敛（chapterOf(镜头 id) 首段 == 章号），跨章同台词不乱连。
 */
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { ElButton } from 'element-plus'
import type { ScriptChapter, ShotRow } from '@/api/types'
import { chapterOf, kindInfo, kindOf, refreshScript, state } from '@/stores/app'
import { openShot } from '@/stores/ui'

const loading = ref(false)
const rootEl = ref<HTMLElement | null>(null)

interface SLine {
  /** 原始整行（**逐字保留**，⟨start-end⟩ 等字面不动 —— E2E 也依赖它） */
  text: string
  kind: 'plain' | 'scene' | 'beat' | 'action' | 'dlg'
  /** 对白/旁白/动作正文（用于匹配镜头） */
  quote: string
  /** 匹配到的镜头 id（'' = 没匹配上） */
  shotId: string
  /** 有对白内容却没进任何镜头 —— 「台词丢失」现场 */
  lost: boolean
}

const hasAny = computed(() => state.scripts.some((c) => c.has_script))
const gateOk = computed(() =>
  state.scripts.filter((c) => c.has_script).every((c) => c.checks.gate_passed !== false),
)
/** 覆盖率口径统一成**逐字可溯率** —— 与门禁同一组计数：（逐字通过 + 仅差标点）÷ 已校验句数。
 *  ★ 为什么换口径（test-guard 终验 D②）：`checks.coverage_pct` 是「剧本区间盖住了原文
 *  多少对白**字符**」，引号风格不同的小说分母会被算成 0 —— 于是同屏出现「无损校验通过、
 *  6 句全找到」与「覆盖率 0%」的自相矛盾。门禁判据是 `failed == 0`（逐字计数同源），
 *  展示数字必须从同一批计数来 —— **两句话同真同假**。 */
function covOf(c: ScriptChapter): number | null {
  const checked = c.checks.checked ?? 0
  if (checked <= 0) return null // 没有校验计数（老数据）：不编数字
  const ok = (c.checks.exact_ok ?? 0) + (c.checks.normalized_ok ?? 0)
  return Math.round((ok / checked) * 100)
}

const overallCov = computed<number | null>(() => {
  let checked = 0
  let ok = 0
  for (const c of state.scripts.filter((x) => x.has_script)) {
    checked += c.checks.checked ?? 0
    ok += (c.checks.exact_ok ?? 0) + (c.checks.normalized_ok ?? 0)
  }
  return checked > 0 ? Math.round((ok / checked) * 100) : null
})
const summary = computed(() => {
  const cs = state.scripts.filter((c) => c.has_script)
  if (!cs.length) return '还没有剧本'
  const lines = cs.reduce((a, c) => a + c.lines, 0)
  return `${cs.length} 章 / ${lines} 句对白`
})

/** 归一化：去掉一切空白/标点/符号，只留文字数字 —— 「下一站，归墟。」≡「下一站归墟」。 */
function nz(s: string): string {
  return (s || '').replace(/[\s\p{P}\p{S}]+/gu, '')
}

/** 对白 → 镜头：先精确（归一化后相等），再互相包含（合并引号/截断兜底，取最长字段）。
 *  与 vm/script.py 的 match_merged_quotes 是同一套判据，前后端对"什么算同一句"不打架。 */
function matchQuote(quote: string, cands: ShotRow[]): string {
  const q = nz(quote)
  if (q.length < 2) return ''
  for (const s of cands) {
    if (nz(s.dialogue) === q || nz(s.narration) === q) return s.id
  }
  let best = ''
  let bestLen = 0
  for (const s of cands) {
    for (const x of [s.dialogue, s.narration]) {
      const n = nz(x)
      if (n.length >= 2 && (n.includes(q) || q.includes(n)) && n.length > bestLen) {
        best = s.id
        bestLen = n.length
      }
    }
  }
  return best
}

/** 解析 vm/script.render() 的版式。只认三种"活"行：节拍头 / 旁白动作 / 角色对白。
 *  ★ 实测版式（雨夜地铁 chapter01）：节拍头是 `  [1-1-01] (林樾)` —— **节拍 id 就是镜头 id**
 *  （build_from_shots 一拍一镜），旁白行是 `旁白：…`（char="旁白" 的普通对照行）。 */
function parseText(text: string): SLine[] {
  return (text || '').split('\n').map((raw): SLine => {
    const base: SLine = { text: raw, kind: 'plain', quote: '', shotId: '', lost: false }
    if (/^\s*──\s*\S+/.test(raw)) return { ...base, kind: 'scene' }
    const beat = /^\s*\[([^\]]+)\]/.exec(raw)
    if (beat) return { ...base, kind: 'beat', quote: beat[1].trim() }
    const act = /^\s*旁白\/动作：([\s\S]+)$/.exec(raw)
    if (act) return { ...base, kind: 'action', quote: act[1].trim() }
    // 版式：`      {角色}：{对白}  ⟨start-end⟩`（或 `⟨无定位⟩`）。对白里可能有全角冒号，
    // 所以角色只吃到**第一个**冒号，且行尾的 ⟨…⟩ 单独剥掉再参与匹配。
    const dlg = /^\s*[^：\s]+：([\s\S]+?)\s*⟨[^⟩]*⟩\s*$/.exec(raw)
    if (dlg) return { ...base, kind: 'dlg', quote: dlg[1].trim() }
    return base
  })
}

/** 一章的行 + 匹配结果 + 统计。state.shots 就绪前返回空匹配，之后自动补上。 */
function analyze(c: ScriptChapter): { lines: SLine[]; stat: { shots: number; lost: number } } {
  const lines = parseText(c.text)
  // 匹配范围先按章收敛（镜头 id 首段 = 章号），整章没镜头再退回全表（多章合并项目兜底）
  let cands = state.shots.filter((s) => chapterOf(s.id) === c.no)
  if (!cands.length) cands = state.shots
  // ① 对白 / 旁白 / 动作行：按**正文**匹配（归一化精确相等优先、互相包含兜底）
  for (const ln of lines) {
    if (ln.kind === 'dlg' || ln.kind === 'action') {
      if (ln.quote) ln.shotId = matchQuote(ln.quote, cands)
    }
  }
  // ② 节拍头：build_from_shots 的节拍 id **就是镜头 id**（`[1-1-01]`）—— 直接对上，
  //    连没有对白的空拍（纯动作镜）也能跳；老版 [A1] 式节拍 id 走 ③ 兜底。
  for (const ln of lines) {
    if (ln.kind === 'beat' && ln.quote && state.shots.some((s) => s.id === ln.quote)) ln.shotId = ln.quote
  }
  // ③ 兜底：还没匹配的节拍头/旁白动作行 → 继承所在节拍块里的镜头（一拍一镜）
  let blockShot = ''
  for (let i = 0; i < lines.length; i++) {
    const ln = lines[i]
    if (ln.kind === 'beat' || ln.kind === 'scene') {
      blockShot = ln.shotId || ''
      if (!blockShot) {
        for (let j = i + 1; j < lines.length && lines[j].kind !== 'beat' && lines[j].kind !== 'scene'; j++) {
          if (lines[j].shotId) { blockShot = lines[j].shotId; break }
        }
      }
    }
    if (!ln.shotId && (ln.kind === 'beat' || ln.kind === 'action')) ln.shotId = blockShot
  }
  // 「丢失」判定：有对白/动作内容的活行却没匹配到任何镜头
  for (const ln of lines) {
    ln.lost = (ln.kind === 'dlg' || ln.kind === 'action') && !!ln.quote && !ln.shotId
  }
  const shotSet = new Set(lines.map((l) => l.shotId).filter(Boolean))
  return { lines, stat: { shots: shotSet.size, lost: lines.filter((l) => l.lost).length } }
}

const parsed = computed(() =>
  state.scripts
    .filter((x) => x.has_script)
    .map((c) => ({ c, ...analyze(c) })),
)

/** 状态色用 theme.css 的 .tone-*（与镜头表同一套语义：绿=已生成/黄=需重渲/红=不合格）。 */
function toneOf(id: string): string {
  const s = state.shots.find((x) => x.id === id)
  return s ? `tone-${kindInfo(kindOf(s)).tone}` : ''
}

function lineTitle(ln: SLine): string {
  if (ln.shotId) return `点击定位到镜头 ${ln.shotId} 并打开详情`
  if (ln.lost) return '这句没有进入任何镜头 —— 审计「台词丢失」的现场；去镜头表补镜或改写它'
  return ''
}

/** 点行 → 与胶片条点击同一入口：state.selected 联动 + openShot 开详情。 */
function onLineClick(ln: SLine) {
  if (!ln.shotId) return
  state.selected = ln.shotId
  openShot(ln.shotId)
}

/** 镜头 → 剧本：选中变了就高亮对应行并滚到它（互相跳转的另一半）。 */
const activeKey = ref('')
function scrollToKey(key: string) {
  activeKey.value = key
  void nextTick(() => {
    const el = rootEl.value?.querySelector<HTMLElement>(`[data-key="${key}"]`)
    el?.scrollIntoView({ block: 'nearest' })
  })
}
// 只跟 state.selected 的**变化**走：parsed 每次轮询刷新都会重算，
// 若一起 watch，任务运行时每 4s 就把正在阅读的位置拽回选中行（踩过一次）。
watch(
  () => state.selected,
  (id) => {
    if (!id) return
    for (const pc of parsed.value) {
      const i = pc.lines.findIndex((l) => l.shotId === id)
      if (i >= 0) { scrollToKey(`${pc.c.no}:${i}`); return }
    }
  },
  { immediate: true },
)

/** 章头「丢 N 句」→ 直达第一句丢失的对白（审计「台词丢失」直达现场）。 */
function scrollToLost(no: number) {
  const pc = parsed.value.find((x) => x.c.no === no)
  const i = pc?.lines.findIndex((l) => l.lost) ?? -1
  if (pc && i >= 0) scrollToKey(`${no}:${i}`)
}

async function load(force = false) {
  if (loading.value) return
  loading.value = true
  try {
    if (force || !state.scripts.length) await refreshScript()
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.sctab { display: flex; flex-direction: column; gap: 8px; }
.summary { display: flex; align-items: center; gap: 8px; font-size: 12px; }
.gate { font-size: 11.5px; line-height: 1.6; }
.ch { border: 1px solid var(--line); border-radius: 10px; overflow: hidden; background: var(--card); }
.chhead {
  display: flex; align-items: center; gap: 6px; padding: 6px 8px; font-size: 11.5px;
  background: var(--hover); border-bottom: 1px solid var(--line); flex-wrap: wrap;
}
.tag { font-size: 9.5px; padding: 0 5px; border-radius: 4px; }
.tag.ok { background: color-mix(in srgb, var(--ok) 14%, var(--card)); color: var(--ok); }
.tag.warn { background: color-mix(in srgb, var(--warn) 14%, var(--card)); color: var(--warn); }
.tag.bad { background: color-mix(in srgb, var(--bad) 12%, var(--card)); color: var(--bad); }
.tag.info { background: var(--hover); color: var(--muted); }
/* 「丢 N 句」可点：直达第一句丢失的对白 */
.losttag { cursor: pointer; }
.losttag:hover { outline: 1px solid var(--warn); }
.linkhint { margin-top: 0; }
/* 剧本正文用等宽，逐行的 ⟨start-end⟩ 偏移才能对齐着看 */
.scripttext {
  margin: 0; padding: 8px 10px; font-size: 11.5px; line-height: 1.62;
  font-family: ui-monospace, Menlo, Consolas, monospace;
  max-height: 420px; overflow: auto;
}
/* 逐行：原文逐字保留（pre-wrap 保住行首缩进），行尾挂镜头徽标 */
.sl {
  white-space: pre-wrap; word-break: break-word;
  border-radius: 4px; padding: 0 2px;
}
.sl.scene { font-weight: 700; margin-top: 6px; }
.sl.beat { color: var(--run); }
.sl.plain { color: var(--muted); }
.sl.linked { cursor: pointer; }
.sl.linked:hover { background: var(--hover); }
/* 有对白却没进任何镜头 —— 「台词丢失」的现场，要显眼但不是告警弹窗级别 */
.sl.lost {
  cursor: pointer;
  background: color-mix(in srgb, var(--bad) 8%, transparent);
  box-shadow: inset 2px 0 0 var(--bad);
}
.sl.lost:hover { background: color-mix(in srgb, var(--bad) 14%, transparent); }
/* 镜头 ↔ 剧本互相跳转的"当前行"：与镜头表行选中同一套视觉语言 */
.sl.sel {
  background: var(--hover);
  box-shadow: inset 3px 0 0 var(--lime);
}
.shotlink {
  margin-left: 8px; font-size: 10px; font-weight: 600;
  font-variant-numeric: tabular-nums; white-space: nowrap;
}
.shotlink.lost { color: var(--bad); }
</style>
