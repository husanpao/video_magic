<template>
  <!-- W1 章节屏（P1.3）：左清单 + 右正文编辑器。 -->
  <section class="ws" data-testid="ws-chapters">
    <header class="wshead">
      <h2>章节</h2>
      <span class="dim small" data-testid="ch-project-name">{{ state.project || '（未选项目）' }}</span>
      <span v-if="!hasManifest" class="pill warn" title="清单还没建立">未建立清单</span>
      <span class="spacer" />
      <button class="small" data-testid="btn-ch-reload" :disabled="loading" @click="reload">↻ 重新加载</button>
      <button class="small" data-testid="btn-ch-import" @click="openImport">⬆ 导入 .md / .txt</button>
      <button class="primary" data-testid="btn-ch-new" @click="startNew">✚ 手动新建一章</button>
    </header>

    <p v-if="nextStep" class="guide" data-testid="ch-next-step">下一步：{{ nextStep }}</p>
    <ul v-if="conflicts.length" class="conflicts" data-testid="ch-conflicts">
      <li v-for="c in conflicts" :key="c">{{ c }}</li>
    </ul>

    <div class="split">
      <!-- 左：清单。状态徽标让"该重拆哪一章"一眼看到 -->
      <ol class="list" data-testid="ch-list">
        <li
          v-for="c in chapters" :key="c.no"
          class="row" :class="{ active: editing?.no === c.no }"
          data-testid="ch-row" @click="open(c.no)"
        >
          <span class="order">
            <button class="tiny ghost" title="上移" :disabled="!canUp(c)" data-testid="ch-up" @click.stop="move(c, -1)">↑</button>
            <button class="tiny ghost" title="下移" :disabled="!canDown(c)" data-testid="ch-down" @click.stop="move(c, 1)">↓</button>
          </span>
          <span class="col main">
            <b :title="c.title">{{ c.title }}</b>
            <span class="dim small">第 {{ c.no }} 章 · {{ fmtChars(c.novel_chars) }} 字</span>
          </span>
          <span class="col badges">
            <span v-if="c.needs_replan" class="badge warn" data-testid="badge-replan">需重拆</span>
            <span v-else-if="c.has_shots" class="badge ok" data-testid="badge-done">已拆 {{ c.shots }} 镜</span>
            <span v-else class="badge idle" data-testid="badge-todo">未拆</span>
          </span>
          <span class="col acts">
            <button class="tiny" title="改标题" data-testid="ch-rename" @click.stop="rename(c)">标题</button>
            <button
              v-if="c.needs_replan"
              class="tiny warnbtn" title="只重拆这一章（其余章节不动，省 LLM token 与渲染时间）"
              data-testid="ch-replan" @click.stop="replan(c)"
            >只重拆这一章</button>
            <button
              class="tiny danger" title="删除（正文与镜头表移到 novel/_trash/，可手工恢复）"
              data-testid="ch-delete" @click.stop="del(c)"
            >删</button>
          </span>
        </li>
        <li v-if="!chapters.length && !loading" class="emptyrow" data-testid="ch-empty">
          还没有章节。点右上「导入」上传 .md/.txt，或「手动新建一章」直接粘贴正文。
        </li>
      </ol>

      <!-- 右：编辑器 -->
      <div class="editor" data-testid="ch-editor">
        <template v-if="editing">
          <div class="ehead">
            <input v-model="editing.title" class="etitle" data-testid="ch-title" placeholder="章节标题（会当文件名用）" />
            <span class="dim small tabular">{{ editing.text.trim().length }} 字</span>
            <span v-if="dirty" class="badge warn" data-testid="ch-dirty">未保存</span>
            <span class="spacer" />
            <button class="small ghost" data-testid="ch-revert" :disabled="!dirty" @click="revert">放弃修改</button>
            <button class="primary small" data-testid="ch-save" :disabled="busy || !dirty" @click="save">
              {{ busy ? '保存中…' : '保存' }}
            </button>
          </div>
          <p class="dim small tip">
            标题只影响文件名，<b>章号不变</b>（章号是镜头 id 前缀与成片集数 EPnn 的主键）。
            改正文后这一章会标「需重拆」—— 保存不会自动重拆，因为拆镜要烧 token。
          </p>
          <textarea
            v-model="editing.text" class="body" data-testid="ch-body"
            placeholder="把这一章的正文粘在这里…" spellcheck="false"
          />
        </template>
        <div v-else class="nopick" data-testid="ch-nopick">
          <p class="dim">左边选一章来读 / 改，或点「✚ 手动新建一章」。</p>
        </div>
      </div>
    </div>

    <!-- 导入对话框：多文件一次导；同名默认不覆盖 -->
    <div v-if="importing" class="mask" data-testid="import-mask" @click.self="importing = false">
      <form class="dialog" role="dialog" aria-label="导入章节" @submit.prevent="doImport">
        <h3>导入章节文件</h3>
        <input type="file" multiple accept=".md,.txt,text/markdown,text/plain" data-testid="import-input" @change="pickFiles" />
        <p v-if="!picked.length" class="dim small">支持 .md / .txt，可多选。文件名会当章节标题用。</p>
        <ul v-else class="picked" data-testid="import-picked">
          <li v-for="f in picked" :key="f.filename">
            <label class="pickone">
              <input type="checkbox" v-model="f.overwrite" data-testid="import-overwrite" />
              覆盖同名
            </label>
            <b>{{ f.filename }}</b>
            <span class="dim small">{{ f.text.length }} 字</span>
          </li>
        </ul>
        <p v-if="picked.some(p => p.overwrite)" class="warn small">
          覆盖会丢掉那一章现有正文（含你手工改过的部分）。镜头表不会自动重拆。
        </p>
        <footer>
          <button type="button" class="ghost" data-testid="import-cancel" @click="importing = false">取消</button>
          <span class="spacer" />
          <button type="submit" class="primary" :disabled="busy || !picked.length" data-testid="import-submit">
            {{ busy ? '导入中…' : `导入 ${picked.length} 章` }}
          </button>
        </footer>
      </form>
    </div>
  </section>
</template>

<script setup lang="ts">
/**
 * W1 章节屏（P1.3）。
 *
 * 这块界面前是不存在的：章节只有 `/api/chapters` 一个**只读**清单，
 * 唯一的"管理"方式是手工往 `projects/<名>/novel/` 丢 .md 文件（README:111），
 * 前端只在左栏 FilterRail 里拿它做筛选。
 *
 * 三个刻意的设计决定：
 *  1. **排序用上移/下移按钮，不做拖拽**。拖拽要引 sortablejs 并自己处理无障碍，
 *     而"调章节顺序"是低频操作 —— 为它加依赖不值（计划 §6.6 的"可选不加"）。
 *  2. **保存不自动重拆**。改正文会让这一章标「需重拆」，但拆镜要烧 LLM token；
 *     静默触发花钱的操作是本项目明令避免的。
 *  3. **删除走 _trash 而不是真删**。正文是用户手写/导进来的东西，删了拿不回来。
 */
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api/client'
import type { ChapterRow } from '@/api/types'
import { refreshShots, state } from '@/stores/app'

interface Draft { no: number | null; title: string; text: string; origin: string }

const chapters = computed<ChapterRow[]>(() => state.chapters)
const loading = ref(false)
const busy = ref(false)
const editing = ref<Draft | null>(null)
const importing = ref(false)
const picked = ref<Array<{ filename: string; text: string; overwrite: boolean }>>([])
const nextStep = ref('')
const conflicts = ref<string[]>([])
const hasManifest = ref(true)

const dirty = computed(() => !!editing.value && editing.value.text !== editing.value.origin)

function fmtChars(n: number): string {
  return n >= 10000 ? (n / 10000).toFixed(1) + '万' : String(n)
}

/** 重排后端的接口是"给出全部章号的目标顺序"，所以相邻交换即可。 */
function canUp(c: ChapterRow): boolean { return chapters.value.indexOf(c) > 0 }
function canDown(c: ChapterRow): boolean { return chapters.value.indexOf(c) < chapters.value.length - 1 }

async function applyOrder(list: ChapterRow[]): Promise<boolean> {
  try {
    const r = await api.chapterReorder(state.project, list.map((c) => c.no))
    if (r.chapters) state.chapters = r.chapters
    ElMessage.success(r.message)
    return true
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
    await reload()
    return false
  }
}

async function move(c: ChapterRow, delta: number): Promise<void> {
  const list = chapters.value.slice()
  const i = list.indexOf(c)
  const j = i + delta
  if (i < 0 || j < 0 || j >= list.length) return
  ;[list[i], list[j]] = [list[j], list[i]]
  await applyOrder(list)
}

async function reload(): Promise<void> {
  if (!state.project) return
  loading.value = true
  try {
    const r = await api.chapters(state.project)
    state.chapters = r.chapters
    hasManifest.value = r.has_manifest !== false
    conflicts.value = r.conflicts || []
    nextStep.value = r.next_step || ''
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    loading.value = false
  }
}

async function open(no: number): Promise<void> {
  if (dirty.value && !(await confirmDiscard())) return
  try {
    const r = await api.chapter(state.project, no)
    editing.value = { no: r.no, title: r.title, text: r.text, origin: r.text }
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  }
}

function startNew(): void {
  if (dirty.value) {
    ElMessage.warning('当前章节有未保存的修改，先保存或放弃')
    return
  }
  const nextNo = Math.max(0, ...chapters.value.map((c) => c.no)) + 1
  editing.value = {
    no: null, title: `第${cnNum(nextNo)}章`, text: '', origin: '',
  }
}

function cnNum(n: number): string {
  const d = ['零', '一', '二', '三', '四', '五', '六', '七', '八', '九']
  if (n < 10) return d[n]
  if (n < 20) return '十' + (n % 10 ? d[n % 10] : '')
  return d[Math.floor(n / 10)] + '十' + (n % 10 ? d[n % 10] : '')
}

async function save(): Promise<void> {
  const e = editing.value
  if (!e) return
  if (!e.text.trim()) {
    ElMessage.warning('正文不能为空')
    return
  }
  busy.value = true
  try {
    const r = await api.chapterSave(state.project, {
      no: e.no ?? undefined, title: e.title, text: e.text,
    })
    if (r.chapters) state.chapters = r.chapters
    e.no = r.no ?? e.no
    e.title = r.title || e.title
    e.origin = e.text
    ElMessage.success(r.message + (r.hint ? ` · ${r.hint}` : ''))
    await Promise.all([reload(), refreshShots()])
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : String(err))
  } finally {
    busy.value = false
  }
}

function revert(): void {
  if (editing.value) editing.value.text = editing.value.origin
}

async function rename(c: ChapterRow): Promise<void> {
  let val: string
  try {
    const r = await ElMessageBox.prompt('改的是标题（= 正文文件名）。章号不变。', '改标题', {
      inputValue: c.title, confirmButtonText: '确定', cancelButtonText: '取消',
      inputValidator: (v: string) => (v && v.trim() ? true : '标题不能为空'),
    })
    val = String(r.value || '').trim()
  } catch {
    return                                  // 用户取消
  }
  try {
    const r = await api.chapterRename(state.project, c.no, val)
    await reload()
    if (editing.value?.no === c.no) editing.value.title = r.title || val
    ElMessage.success(r.message)
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  }
}

/**
 * 只重拆这一章（P1.5 的收尾）。
 *
 * ★ 必须二次确认，而且绝不默认 `force`：拆镜要烧 LLM token（实测一章约 1700），
 *   静默触发花钱的操作是本项目明令避免的（README 的「成本护栏」同一个立场）。
 *   超预算时后台会回 409 + 结构化载荷，`launch` 那套审批框已经在 App 级别接好了 ——
 *   这里只是不发 force。
 */
async function replan(c: ChapterRow): Promise<void> {
  if (state.running) {
    ElMessage.warning('已有任务在跑，等同一个跑完或先停止')
    return
  }
  const name = state.project
  let est = ''
  try {
    const b = await api.budget(name, 'plan')
    const g = b?.est as Record<string, number> | undefined
    if (g?.llm_tokens) est = `预计约 ${g.llm_tokens.toLocaleString()} tokens`
  } catch {
    /* 估算失败不该拦住确认框，只是少一行信息 */
  }
  try {
    await ElMessageBox.confirm(
      `只重拆第 ${c.no} 章《${c.title}》，其余 ${Math.max(0, chapters.value.length - 1)} 章的镜头表与产物**不动**。${est ? `<br>${est}。` : ''}`
      + '<br><br>该章原有的镜头表会被新结果替换。',
      '单章重拆',
      { type: 'warning', confirmButtonText: '开拆', cancelButtonText: '取消', dangerouslyUseHTMLString: true },
    )
  } catch {
    return
  }
  try {
    const r = await api.run(name, 'plan', { chapter: c.no })
    ElMessage.success(r.message || `已启动第 ${c.no} 章拆镜`)
    void reload()
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  }
}

async function del(c: ChapterRow): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `删除第 ${c.no} 章《${c.title}》？正文${c.has_shots ? `与它的镜头表（${c.shots} 镜）` : ''}`
      + '会移到 novel/_trash/，<b>不真删</b>，可手工恢复。'
      + '<br><br>这一章的镜头不会自动从成片里消失 —— 要生效需重新合成。',
      '删除章节',
      { type: 'warning', dangerouslyUseHTMLString: true, confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    const r = await api.chapterDelete(state.project, c.no)
    if (editing.value?.no === c.no) editing.value = null
    await Promise.all([reload(), refreshShots()])
    ElMessage.success(r.message)
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  }
}

function openImport(): void {
  picked.value = []
  importing.value = true
}

/** 浏览器本地读文件 → base64 以外的成本为零；正文是文本，直接 readAsText。 */
async function pickFiles(ev: Event): Promise<void> {
  const files = (ev.target as HTMLInputElement).files
  if (!files?.length) return
  const out: typeof picked.value = []
  for (const f of Array.from(files)) {
    if (!/\.(md|txt)$/i.test(f.name)) {
      ElMessage.warning(`跳过 ${f.name}：只支持 .md / .txt`)
      continue
    }
    out.push({ filename: f.name, text: await f.text(), overwrite: false })
  }
  picked.value = out
}

async function doImport(): Promise<void> {
  busy.value = true
  try {
    const r = await api.chapterImport(state.project, picked.value)
    if (r.chapters) state.chapters = r.chapters
    importing.value = false
    ElMessage.success(r.message)
    for (const sk of r.skipped || []) ElMessage.warning(`${sk.filename}：${sk.reason}`)
    await reload()
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    busy.value = false
  }
}

async function confirmDiscard(): Promise<boolean> {
  try {
    await ElMessageBox.confirm('当前章节有未保存的修改。', '放弃修改？',
      { type: 'warning', confirmButtonText: '放弃', cancelButtonText: '继续编辑' })
    return true
  } catch {
    return false
  }
}

// 换项目必须重新拉清单，并清掉上一个项目的草稿（否则会带着旧正文往新项目里写）
watch(() => state.project, () => {
  editing.value = null
  void reload()
}, { immediate: true })
</script>

<style scoped>
.ws { flex: 1 1 auto; min-height: 0; overflow: hidden; display: flex; flex-direction: column; padding: 12px 14px; }
.wshead { display: flex; align-items: center; gap: 8px; flex: 0 0 auto; }
.wshead h2 { margin: 0; font-size: 15px; color: var(--ink); }
.spacer { flex: 1 1 auto; }
.pill { font-size: 11px; padding: 1px 7px; border-radius: 999px; border: 1px solid var(--line); }
.pill.warn { color: var(--warn); background: var(--warn-bg); border-color: var(--warn-border); }
.guide { margin: 8px 0 0; font-size: 12px; color: var(--lime-deep); flex: 0 0 auto; }
.conflicts { margin: 6px 0 0; padding: 6px 10px 6px 26px; font-size: 11.5px; color: var(--warn); background: var(--warn-bg); border: 1px solid var(--warn-border); border-radius: 8px; flex: 0 0 auto; max-height: 84px; overflow: auto; }
.split { flex: 1 1 auto; min-height: 0; display: grid; grid-template-columns: minmax(300px, 40%) minmax(0, 1fr); gap: 12px; margin-top: 10px; }
.list { list-style: none; margin: 0; padding: 0; overflow: auto; min-height: 0; border: 1px solid var(--line); border-radius: var(--radius); background: var(--card); }
.row { display: flex; align-items: center; gap: 8px; padding: 7px 8px; border-bottom: 1px solid var(--line); cursor: pointer; }
.row:last-child { border-bottom: none; }
.row:hover { background: var(--hover); }
.row.active { background: var(--hover); box-shadow: 2px 0 0 var(--lime-deep) inset; }
.order { display: flex; flex-direction: column; gap: 1px; flex: 0 0 auto; }
.col.main { display: flex; flex-direction: column; gap: 1px; min-width: 0; flex: 1 1 auto; }
.col.main b { font-size: 12.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--ink); }
.col.badges, .col.acts { flex: 0 0 auto; }
.col.acts { display: flex; gap: 4px; }
.badge { font-size: 10.5px; padding: 1px 6px; border-radius: 999px; border: 1px solid var(--line); white-space: nowrap; }
.badge.ok { color: var(--ok); background: var(--ok-bg); border-color: var(--ok-border); }
.badge.warn { color: var(--warn); background: var(--warn-bg); border-color: var(--warn-border); }
.warnbtn { color: var(--warn); border-color: var(--warn-border); background: var(--warn-bg); }
.warnbtn:hover:not(:disabled) { border-color: var(--warn); }
.badge.idle { color: var(--muted); }
.emptyrow { padding: 20px 12px; font-size: 12px; color: var(--muted); text-align: center; line-height: 1.7; }
.editor { display: flex; flex-direction: column; min-width: 0; min-height: 0; gap: 6px; }
.ehead { display: flex; align-items: center; gap: 8px; flex: 0 0 auto; }
.etitle { flex: 1 1 auto; min-width: 0; font: inherit; font-size: 13px; color: var(--ink); background: var(--surface-2); border: 1px solid var(--line); border-radius: 8px; padding: 5px 8px; }
.etitle:focus { outline: none; border-color: var(--lime-deep); }
.tip { margin: 0; line-height: 1.6; flex: 0 0 auto; }
.body { flex: 1 1 auto; min-height: 0; resize: none; font: inherit; font-size: 13px; line-height: 1.75; color: var(--ink); background: var(--card); border: 1px solid var(--line); border-radius: var(--radius); padding: 10px 12px; }
.body:focus { outline: none; border-color: var(--lime-deep); }
.nopick { flex: 1 1 auto; display: flex; align-items: center; justify-content: center; border: 1px dashed var(--line); border-radius: var(--radius); }
button { font: inherit; font-size: 12px; cursor: pointer; border-radius: 8px; border: 1px solid var(--line); background: var(--surface-2); color: var(--ink); padding: 4px 9px; }
button:hover:not(:disabled) { background: var(--hover); border-color: var(--hover-border); }
button:disabled { opacity: .45; cursor: not-allowed; }
button.small { font-size: 11.5px; }
button.tiny { font-size: 10.5px; padding: 1px 5px; border-radius: 5px; }
button.ghost { background: transparent; }
button.primary { background: var(--lime-deep); color: var(--bg); border-color: var(--lime-deep); font-weight: 600; }
button.danger { color: var(--bad); border-color: var(--bad-border); }
.mask { position: fixed; inset: 0; z-index: 2600; background: color-mix(in srgb, var(--ink) 32%, transparent); display: flex; align-items: center; justify-content: center; padding: 18px; }
.dialog { width: min(540px, 96vw); max-height: 88vh; overflow: auto; background: var(--card); border: 1px solid var(--line); border-radius: var(--radius); box-shadow: var(--shadow); padding: 16px 18px; display: flex; flex-direction: column; gap: 10px; }
.dialog h3 { margin: 0; font-size: 14.5px; color: var(--ink); }
.dialog footer { display: flex; align-items: center; gap: 8px; }
.picked { margin: 0; padding-left: 0; list-style: none; display: flex; flex-direction: column; gap: 5px; font-size: 12px; color: var(--ink); }
.pickone { display: inline-flex; align-items: center; gap: 4px; font-size: 11px; color: var(--muted); }
.dim { color: var(--muted); } .small { font-size: 11.5px; } .warn { color: var(--warn); margin: 0; }
.tabular { font-variant-numeric: tabular-nums; }
@media (max-width: 1000px) { .split { grid-template-columns: 1fr; } }
</style>
