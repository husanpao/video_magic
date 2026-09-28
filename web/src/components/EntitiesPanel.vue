<template>
  <!-- 实体总表（P3.1）：角色 / 场景 / 道具的全项目登记全景 + 出现在哪些章。
       挂在 W2 里，因为这一页与"风格"是同一件事的两半：都回答"整本项目长什么样"。 -->
  <section class="ents" data-testid="entities-panel">
    <header class="ehead">
      <h3>实体总表</h3>
      <span class="dim small" data-testid="ent-totals">
        角色 {{ totals.char }} · 场景 {{ totals.scene }} · 道具 {{ totals.prop }}
      </span>
      <span class="spacer" />
      <select v-model.number="chapterFilter" class="cf" data-testid="ent-chapter-filter">
        <option :value="0">全部章</option>
        <option v-for="c in allChapters" :key="c" :value="c">仅第 {{ c }} 章</option>
      </select>
      <button class="small" data-testid="ent-reload" :disabled="loading" @click="load">↻</button>
    </header>

    <p v-if="suspect.length" class="warnbox" data-testid="ent-suspect">
      <b>去重可疑（去重靠名字归一，一定会判错，这里就是给你手工纠偏的入口）：</b>
      <span v-for="w in suspect" :key="w"> {{ w }}；</span>
    </p>
    <p v-if="neverUsed.length" class="dim small" data-testid="ent-unused">
      {{ neverUsed.length }} 个实体没被任何镜头引用：{{ neverUsed.join('、') }}
      （多半是抽卡/合并留下的孤儿，或本章没拍到）
    </p>

    <table class="et" data-testid="ent-table">
      <thead>
        <tr><th>类型</th><th>实体</th><th>出现在章</th><th>镜数</th><th>造型 / 锚定</th><th /></tr>
      </thead>
      <tbody>
        <tr v-for="e in shown" :key="e.kind + e.id" :data-testid="`ent-row-${e.kind}-${e.id}`">
          <td><span class="tag" :class="`t-${e.kind}`">{{ KIND_CN[e.kind] }}</span></td>
          <td class="nm"><b>{{ e.name }}</b><span v-if="e.kind !== 'char'" class="dim small"> {{ e.id }}</span></td>
          <td class="tabular">
            <span v-if="!e.chapters?.length" class="dim">未知</span>
            <span v-else>{{ e.chapters.join('、') }}</span>
          </td>
          <td class="tabular">{{ e.shot_count }}</td>
          <td class="small">
            <template v-if="e.kind === 'char'">
              <span v-if="e.portrait_chapters?.length" class="badge ok" data-testid="ent-has-variant">
                {{ e.portrait_chapters.length }} 章专属造型
              </span>
              <span v-else class="dim">仅默认造型</span>
              <span v-if="(e.costume_variants?.length || 0) > 1" class="dim small">
                （服装变体 {{ e.costume_variants?.length }} 套）
              </span>
            </template>
            <template v-else-if="e.kind === 'scene'">
              <span class="dim small">{{ e.location || '—' }}{{ e.time_of_day ? ' · ' + e.time_of_day : '' }}</span>
            </template>
            <template v-else>
              <span v-if="e.inferred" class="badge warn" title="正文没写外观，是推断的通用形象，需人工复核">推断</span>
              <span v-if="e.owner" class="dim small">归属 {{ e.owner }}</span>
            </template>
          </td>
          <td class="acts">
            <button
              v-if="e.kind === 'char'" class="tiny"
              :disabled="!canPortrait(e)" :title="portraitHint(e)"
              :data-testid="`ent-portrait-${e.id}`" @click="portrait(e)"
            >出某章专属定妆</button>
            <button
              v-if="e.kind === 'scene'" class="tiny"
              data-testid="ent-merge" @click="merge(e)"
            >合并…</button>
          </td>
        </tr>
      </tbody>
    </table>
    <p v-if="!loading && !shown.length" class="dim pad" data-testid="ent-empty">
      还没有登记任何实体 —— 先跑一次「拆镜」（实体是拆镜时抽取并跨章累积登记的）。
    </p>
  </section>
</template>

<script setup lang="ts">
/**
 * 实体总表面板（P3.1）。
 *
 * ★ 这页存在的理由是**救济**，不是展示。
 * 跨章去重靠名字归一，LLM 一定会判错，而两种错的后果不对称：
 *   漏合并 = 多一条实体 → 看得见，可以在这里手工合掉；
 *   错合并 = 两个地点共用一段描述 → 静默污染所有相关镜头，**救不回来**。
 * 所以这里既暴露可疑（`suspect_merges`），又给出唯一的纠正入口（合并 / 出专属造型）。
 *
 * 合并会**同时改镜头表里的 scene_id**（那是外键）—— 只删登记项会让所有引用它的镜头
 * 指向一个不存在的场景。受影响的镜会变 stale，后端在返回体里给了数量，这里必须显示。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api/client'
import type { EntityRow } from '@/api/types'
import { state } from '@/stores/app'

const KIND_CN: Record<string, string> = { char: '角色', scene: '场景', prop: '道具' }
const rows = ref<EntityRow[]>([])
const totals = ref({ char: 0, scene: 0, prop: 0 })
const suspect = ref<string[]>([])
const neverUsed = ref<string[]>([])
const loading = ref(false)
const chapterFilter = ref(0)

// 章号下拉的数据源从实体行自己推 —— 这一页只需要"出现过哪些章"，
// 为此再拉一次 /api/chapters 是多余的一次往返与一套失败态。
const allChapters = computed<number[]>(() =>
  [...new Set(rows.value.flatMap((e) => e.chapters || []))].sort((a, b) => a - b))

const shown = computed(() =>
  chapterFilter.value
    ? rows.value.filter((e) => (e.chapters || []).includes(chapterFilter.value))
    : rows.value)

/** 只有登记了该章专属造型的角色才能出图 —— 否则出一张和默认图一样的东西，纯烧 GPU。 */
function canPortrait(e: EntityRow): boolean {
  return !!e.costume_variants?.length && (e.chapters?.length || 0) > 0
}
function portraitHint(e: EntityRow): string {
  if (canPortrait(e)) return '为某一章出这张角色的专属定妆照（换装 / 受伤 / 回忆段落）'
  return '该角色没有服装变体。先在拆镜时让系统检测造型变化，或在服装面板手工加一个带章号的变体。'
}

async function load(): Promise<void> {
  if (!state.project) return
  loading.value = true
  try {
    const r = await api.entities(state.project)
    rows.value = r.entities
    totals.value = r.totals
    suspect.value = r.suspect_merges || []
    neverUsed.value = r.never_used || []
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    loading.value = false
  }
}

async function portrait(e: EntityRow): Promise<void> {
  const chs = e.chapters?.length ? e.chapters : [0]
  let choice: number
  try {
    const r = await ElMessageBox.prompt(
      `为「${e.name}」出某章的专属定妆照。章号（该章须已登记造型变体）：`,
      '按章出专属定妆',
      { inputValue: String(chs[0] || ''), inputPattern: /^\d+$/, inputErrorMessage: '要填章号（整数）',
        confirmButtonText: '入队', cancelButtonText: '取消' },
    )
    choice = Number(r.value)
  } catch {
    return
  }
  try {
    const r = await api.entityPortrait(state.project, { name: e.name, chapter: choice })
    ElMessage.success(r.message)
    await load()
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : String(err))
  }
}

async function merge(e: EntityRow): Promise<void> {
  const scenes = rows.value.filter((x) => x.kind === 'scene' && x.id !== e.id)
  if (!scenes.length) {
    ElMessage.info('没有其它场景可以合并')
    return
  }
  let drop: string
  try {
    const r = await ElMessageBox.prompt(
      `把哪个场景并进「${e.id} ${e.name}」？该场景的 scene_id 引用会全部改指，`
      + `受影响镜头会标「需重渲」。`,
      '合并场景',
      { inputValue: scenes[0].id,
        inputValidator: (v: string) => (scenes.some((s) => s.id === v.trim()) ? true : '只能填现有场景 id'),
        confirmButtonText: '合并', cancelButtonText: '取消' },
    )
    drop = String(r.value).trim()
  } catch {
    return
  }
  try {
    const r = await api.entityMerge(state.project, { keep: e.id, drop })
    ElMessage.success(`${r.message} — ${r.hint}`)
    await load()
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : String(err))
  }
}

watch(() => state.project, () => { void load() })
onMounted(() => { void load() })
</script>

<style scoped>
.ents { border: 1px solid var(--line); border-radius: var(--radius); background: var(--card); padding: 10px 12px; display: flex; flex-direction: column; gap: 8px; }
.ehead { display: flex; align-items: center; gap: 8px; }
.ehead h3 { margin: 0; font-size: 13px; color: var(--ink); }
.spacer { flex: 1 1 auto; }
.et { width: 100%; border-collapse: collapse; font-size: 12px; color: var(--ink); }
.et th { text-align: left; font-weight: 500; font-size: 11px; color: var(--muted); padding: 4px 6px; border-bottom: 1px solid var(--line); }
.et td { padding: 5px 6px; border-bottom: 1px solid var(--line); vertical-align: middle; }
.et tr:last-child td { border-bottom: none; }
.nm b { font-weight: 600; }
.tag { font-size: 10.5px; padding: 1px 6px; border-radius: 4px; border: 1px solid var(--line); color: var(--muted); }
.t-char { color: var(--ok); background: var(--ok-bg); border-color: var(--ok-border); }
.t-scene { color: var(--run); background: var(--run-bg); border-color: var(--run-border); }
.badge { font-size: 10.5px; padding: 1px 6px; border-radius: 999px; border: 1px solid var(--line); }
.badge.ok { color: var(--ok); background: var(--ok-bg); border-color: var(--ok-border); }
.badge.warn { color: var(--warn); background: var(--warn-bg); border-color: var(--warn-border); }
.warnbox { margin: 0; font-size: 11.5px; line-height: 1.6; color: var(--warn); background: var(--warn-bg); border: 1px solid var(--warn-border); border-radius: 8px; padding: 7px 9px; }
.acts { display: flex; gap: 4px; white-space: nowrap; }
.pad { padding: 14px 0; }
select, input { font: inherit; font-size: 12px; color: var(--ink); background: var(--surface-2); border: 1px solid var(--line); border-radius: 7px; padding: 3px 6px; }
button { font: inherit; cursor: pointer; border-radius: 7px; border: 1px solid var(--line); background: var(--surface-2); color: var(--ink); }
button:hover:not(:disabled) { background: var(--hover); border-color: var(--hover-border); }
button:disabled { opacity: .45; cursor: not-allowed; }
button.small { font-size: 11.5px; padding: 3px 8px; }
button.tiny { font-size: 10.5px; padding: 2px 6px; }
.dim { color: var(--muted); } .small { font-size: 11px; } .tabular { font-variant-numeric: tabular-nums; }
</style>
