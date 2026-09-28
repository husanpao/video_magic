<template>
  <!-- W2 实体与风格屏（P2.5）。 -->
  <section class="ws" data-testid="ws-style">
    <header class="wshead">
      <h2>画面风格</h2>
      <span class="dim small">{{ state.project }}</span>
      <span class="spacer" />
      <button class="small" data-testid="st-reload" :disabled="loading" @click="load">↻ 重新加载</button>
      <button class="small" data-testid="st-recommend" :disabled="busy" @click="recommend">
        🎲 按整本内容重新推荐
      </button>
    </header>

    <div v-if="loading && !cur" class="dim pad">加载中…</div>

    <template v-else-if="cur">
      <p class="srcline" data-testid="st-source">
        当前：<b>{{ cur.preset }}</b>
        <span class="dim">· 出处 {{ sourceLabel(cur.source) }}</span>
        <span v-if="cur.confirmed" class="badge ok" data-testid="st-confirmed">已人工确认</span>
        <span v-else class="badge idle">未确认</span>
        <span v-if="cur.reason" class="dim small">· {{ cur.reason }}</span>
      </p>

      <div class="cards" data-testid="st-presets">
        <button
          v-for="p in cur.presets" :key="p.key"
          class="pcard" :class="{ on: p.on }"
          :data-testid="`st-preset-${p.key}`"
          @click="pick(p.key)"
        >
          <b>{{ p.key }}</b>
          <span class="dim small sent">{{ p.sentence }}</span>
        </button>
      </div>

      <label class="sent">
        整本级风格句（会被逐字拼进**每一镜**的提示词）
        <textarea v-model="sentence" rows="3" spellcheck="false" data-testid="st-sentence" />
      </label>

      <!-- ★ 影响清单：改风格的真实代价，在点保存**之前**就摊开 -->
      <div v-if="impact" class="impact" data-testid="st-impact">
        <h4>改成这样会影响什么</h4>
        <div class="igrid">
          <div class="icell">
            <span class="dim small">角色定妆照</span>
            <b class="tabular">{{ impact.images.portraits.stale }}<span class="dim">/{{ impact.images.portraits.total }}</span></b>
            <span class="dim small">需按新风格重出</span>
            <span v-if="impact.images.portraits.untracked" class="warn small" data-testid="st-untracked">
              另有 {{ impact.images.portraits.untracked }} 张无从判断（出图时还没有指纹机制，或为你上传的图）
            </span>
            <span v-if="impact.images.portraits.never" class="dim small">
              {{ impact.images.portraits.never }} 个角色还没出过定妆照
            </span>
          </div>
          <div class="icell">
            <span class="dim small">场景 / 道具概念图</span>
            <b class="tabular">{{ impact.images.assets.stale }}<span class="dim">/{{ impact.images.assets.total }}</span></b>
            <span class="dim small">需按新风格重出</span>
            <span v-if="impact.images.assets.untracked" class="warn small">
              另有 {{ impact.images.assets.untracked }} 张无从判断
            </span>
          </div>
          <div class="icell" data-testid="st-impact-shots">
            <span class="dim small">镜头</span>
            <b class="tabular">{{ impact.shots.with_old_style }}<span class="dim">/{{ impact.shots.total }}</span></b>
            <span class="warn small">仍带旧风格句</span>
            <span class="dim small">改风格<b>不会</b>让镜头自动变 stale —— 见下</span>
          </div>
        </div>
        <p class="warnbox">
          ⚠ 风格句是在<b>拆镜那一刻</b>被烘进每一镜提示词的。所以要让新风格真的进到画面，
          必须<b>重跑「拆镜」</b>重写镜头表 —— 那会让<b>全部镜头重渲</b>（实测 52 镜约 36 分钟 GPU）。
          这正是「风格应当在拆镜之前就定好」的原因。
        </p>
        <p class="dim small">{{ impact.images.note }}</p>
      </div>

      <!-- 实体总表：与"整本风格"同属"这本书长什么样"，所以放同一屏（P3.1） -->
      <EntitiesPanel />

      <footer class="acts">
        <button
          class="small" data-testid="st-regen" :disabled="busy || !needsRegen"
          :title="needsRegen ? '把受影响的定妆照与概念图按新风格重出（入队，不重跑拆镜）' : '当前没有需要重出的图'"
          @click="regen"
        >⟳ 按新风格重出受影响的图</button>
        <span class="spacer" />
        <button class="primary small" data-testid="st-save" :disabled="busy || !dirty" @click="save">
          {{ busy ? '处理中…' : '保存风格' }}
        </button>
      </footer>
      <p class="dim small note">{{ cur.note }}</p>
    </template>
  </section>
</template>

<script setup lang="ts">
/**
 * W2 风格屏（P2.5）。
 *
 * 这个界面存在的意义不是"给个下拉框"，而是把两件事摆到用户面前：
 *  ① 风格是**整本小说级**的（不是每章一个），它会进每一镜的提示词；
 *  ② 改风格的**真实代价** —— 图要重出、镜头要重跑拆镜 + 全部重渲。
 *
 * ★ 影响清单在**保存之前**就显示，不是保存之后才告诉你坏了什么。
 *   （复用配置中心「危险项影响清单」那套交互口径。）
 */
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api/client'
import EntitiesPanel from '@/components/EntitiesPanel.vue'
import type { StyleInfo, StyleImpact } from '@/api/types'
import { state } from '@/stores/app'

const cur = ref<StyleInfo | null>(null)
const impact = ref<StyleImpact | null>(null)
const sentence = ref('')
const preset = ref('')
const loading = ref(false)
const busy = ref(false)

const dirty = computed(() =>
  !!cur.value && (preset.value !== cur.value.preset || sentence.value.trim() !== cur.value.sentence.trim()))

const needsRegen = computed(() => {
  const i = impact.value
  if (!i) return false
  return i.images.portraits.stale + i.images.portraits.untracked
       + i.images.assets.stale + i.images.assets.untracked > 0
})

function sourceLabel(s?: string): string {
  return ({ user: '人工设定', llm: '系统按整本推荐', preset: '预设默认', derived: '继承自已建角色卡' } as Record<string, string>)[s || ''] || s || '—'
}

async function load(): Promise<void> {
  if (!state.project) return
  loading.value = true
  try {
    cur.value = await api.style(state.project)
    preset.value = cur.value.preset
    sentence.value = cur.value.sentence
    await refreshImpact()
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    loading.value = false
  }
}

/** 影响清单用"将要改成的值"去问后台，所以用户还没保存就能看到代价。 */
async function refreshImpact(): Promise<void> {
  if (!state.project) return
  try {
    impact.value = await api.styleImpact(state.project, { preset: preset.value, sentence: sentence.value })
  } catch {
    impact.value = null            // 影响清单拉不到不该挡住主界面，静默降级
  }
}

async function pick(key: string): Promise<void> {
  preset.value = key
  // 选了新预设时，若用户还没手工改过句子，就把该预设的默认句填进去当起点
  const p = cur.value?.presets.find((x) => x.key === key)
  if (p && (!sentence.value.trim() || cur.value?.source !== 'user')) sentence.value = p.sentence
  await refreshImpact()
}

async function save(): Promise<void> {
  if (!cur.value) return
  const i = impact.value
  // 已有镜头带旧风格句 → 保存不会让它们变新，必须说清楚再落笔
  if (i && i.shots.with_old_style > 0) {
    try {
      await ElMessageBox.confirm(
        `有 ${i.shots.with_old_style}/${i.shots.total} 镜的提示词里仍带着旧风格句。`
        + '<br><br>保存<b>只改风格设定</b>，不会重写镜头表。要让新风格进到画面，'
        + '需要之后<b>重跑「拆镜」</b>（全部镜头会重渲）。',
        '确认保存风格', { type: 'warning', confirmButtonText: '仍然保存', cancelButtonText: '先不改' },
      )
    } catch {
      return
    }
  }
  busy.value = true
  try {
    const r = await api.styleSet(state.project, { preset: preset.value, sentence: sentence.value.trim() })
    ElMessage.success(r.message)
    await load()
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    busy.value = false
  }
}

async function recommend(): Promise<void> {
  busy.value = true
  try {
    const force = !!(cur.value?.confirmed)
    const r = await api.styleRecommend(state.project, force)
    if (r.skipped) {
      ElMessage.info(r.message)
    } else {
      ElMessage.success(r.message)
      await load()
    }
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    busy.value = false
  }
}

async function regen(): Promise<void> {
  const i = impact.value
  if (!i) return
  const n = i.images.portraits.stale + i.images.portraits.untracked + i.images.assets.stale + i.images.assets.untracked
  try {
    await ElMessageBox.confirm(
      `把 ${n} 张受影响的定妆照 / 概念图按当前风格重出（走队列，入队即可离开）。`
      + '<br><br><b>不会</b>重跑拆镜、<b>不会</b>触发镜头重渲。',
      '重出受影响的图', { type: 'warning', confirmButtonText: '入队', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  busy.value = true
  try {
    const r = await api.styleRegen(state.project, {})
    ElMessage.success(r.message)
    await load()
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    busy.value = false
  }
}

watch(() => state.project, () => { void load() })
onMounted(() => { void load() })
</script>

<style scoped>
.ws { flex: 1 1 auto; min-height: 0; overflow: auto; padding: 12px 14px; display: flex; flex-direction: column; gap: 10px; }
.wshead { display: flex; align-items: center; gap: 8px; }
.wshead h2 { margin: 0; font-size: 15px; color: var(--ink); }
.spacer { flex: 1 1 auto; }
.pad { padding: 20px; }
.srcline { margin: 0; font-size: 12.5px; color: var(--ink); display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.badge { font-size: 10.5px; padding: 1px 6px; border-radius: 999px; border: 1px solid var(--line); }
.badge.ok { color: var(--ok); background: var(--ok-bg); border-color: var(--ok-border); }
.badge.idle { color: var(--muted); }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 8px; }
.pcard { display: flex; flex-direction: column; align-items: flex-start; gap: 4px; text-align: left; padding: 9px 11px; }
.pcard b { font-size: 13px; }
.sent { display: flex; flex-direction: column; gap: 5px; font-size: 12px; color: var(--muted); }
.pcard .sent { color: var(--text-3); line-height: 1.55; }
textarea { font: inherit; font-size: 12.5px; line-height: 1.6; color: var(--ink); background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 7px 9px; resize: vertical; }
textarea:focus { outline: none; border-color: var(--lime-deep); }
.impact { border: 1px solid var(--line); border-radius: var(--radius); background: var(--surface-2); padding: 10px 12px; }
.impact h4 { margin: 0 0 8px; font-size: 12.5px; color: var(--ink); }
.igrid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 10px; }
.icell { display: flex; flex-direction: column; gap: 2px; }
.icell b { font-size: 17px; color: var(--ink); }
.warnbox { margin: 10px 0 0; font-size: 11.5px; line-height: 1.7; color: var(--warn); background: var(--warn-bg); border: 1px solid var(--warn-border); border-radius: 8px; padding: 8px 10px; }
.acts { display: flex; align-items: center; gap: 8px; }
.note { margin: 0; line-height: 1.6; }
button { font: inherit; font-size: 12px; cursor: pointer; border-radius: 8px; border: 1px solid var(--line); background: var(--surface-2); color: var(--ink); padding: 5px 10px; }
button:hover:not(:disabled) { background: var(--hover); border-color: var(--hover-border); }
button:disabled { opacity: .45; cursor: not-allowed; }
button.primary { background: var(--lime-deep); color: var(--bg); border-color: var(--lime-deep); font-weight: 600; }
button.on { border-color: var(--lime-deep); box-shadow: 0 0 0 1px var(--lime-deep) inset; background: var(--hover); }
.dim { color: var(--muted); } .small { font-size: 11.5px; } .warn { color: var(--warn); } .tabular { font-variant-numeric: tabular-nums; }
</style>
