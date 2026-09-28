<template>
  <!-- W0 项目屏：卡片墙 + 新建向导。 -->
  <section class="ws" data-testid="ws-projects">
    <header class="wshead">
      <h2>项目</h2>
      <span class="dim small">{{ projects.length }} 个 · projects/ 目录</span>
      <span class="spacer" />
      <button class="small" data-testid="btn-project-trash" :disabled="trashLoading" @click="toggleTrash">
        {{ showTrash ? '收起回收站' : '回收站' }}{{ trash.length ? `（${trash.length}）` : '' }}
      </button>
      <button class="primary" data-testid="btn-new-project" @click="openCreate">➕ 新建项目</button>
    </header>

    <section v-if="showTrash" class="trash" data-testid="project-trash">
      <div class="trashhead">
        <b>项目回收站</b>
        <span class="dim small">删除项目只会移到这里，不会 rm；同名项目存在时不会覆盖恢复。</span>
        <span class="spacer" />
        <button class="small ghost" :disabled="trashLoading" @click="loadTrash">↻ 刷新</button>
      </div>
      <p v-if="!trash.length" class="dim small">回收站是空的。</p>
      <div v-for="item in trash" :key="item.entry" class="trashrow">
        <b>{{ item.project }}</b>
        <span class="dim small tabular">{{ item.files }} 个文件 · {{ fmtBytes(item.size) }} · {{ fmtTime(item.mtime) }}</span>
        <span class="spacer" />
        <button class="small" :disabled="busy" @click="restoreProject(item.entry, item.project)">恢复</button>
      </div>
    </section>

    <div v-if="!projects.length" class="empty" data-testid="projects-empty">
      <p>还没有任何项目。</p>
      <p class="dim small">一个项目 = 一部片子：正文（章节）、镜头表、参考图、成片都挂在它下面。</p>
      <button class="primary" data-testid="empty-new-project" @click="openCreate">新建第一个项目</button>
    </div>

    <div v-else class="cards">
      <article
        v-for="pj in projects" :key="pj.name"
        class="card" :class="{ current: pj.name === state.project }"
        data-testid="project-card"
      >
        <div class="cardhead">
          <b :title="pj.name">{{ pj.name }}</b>
          <span v-if="pj.running" class="pill run" :title="`阶段 ${pj.task_stage}`">● 运行中</span>
          <span v-else class="pill idle">空闲</span>
        </div>
        <p v-if="metaOf(pj).logline" class="logline dim">{{ metaOf(pj).logline }}</p>
        <dl class="stats">
          <div><dt>章节</dt><dd class="tabular">{{ pj.novel_chapters }}</dd></div>
          <div><dt>镜头表</dt><dd class="tabular">{{ pj.shot_files }}</dd></div>
          <div><dt>角色</dt><dd class="tabular">{{ pj.char_prompts }}</dd></div>
          <div><dt>片段</dt><dd class="tabular">{{ pj.clips }}</dd></div>
          <div><dt>成片</dt><dd class="tabular">{{ pj.finals?.length || (pj.final ? 1 : 0) }}</dd></div>
        </dl>
        <div v-if="pj.progress && pj.progress.total" class="prog">
          <i :style="{ width: Math.round((pj.progress.done / pj.progress.total) * 100) + '%' }" />
          <span class="dim small tabular">{{ pj.progress.label }} {{ pj.progress.done }}/{{ pj.progress.total }}</span>
        </div>
        <footer class="cardfoot">
          <button class="small" data-testid="proj-open-workbench" @click="go(pj.name, '/workbench')">镜头工作台</button>
          <button class="small" data-testid="proj-open-chapters" @click="go(pj.name, '/chapters')">章节</button>
          <span class="spacer" />
          <button
            class="small danger"
            data-testid="proj-delete"
            :disabled="pj.running || busy"
            :title="pj.running ? '有任务在跑，先停止才能删除' : '移进项目回收站（可恢复）'"
            @click="deleteProject(pj)"
          >删除</button>
          <button class="small ghost" :title="pj.path" @click="copyPath(pj.path)">路径</button>
        </footer>
      </article>
    </div>

    <!-- 新建向导：一次问清"叫什么 + 小说还是剧本 + 一句话题材"，
         建完直接把光标送到章节屏 —— 建项目本身不是目的，导正文才是。 -->
    <div v-if="creating" class="mask" data-testid="new-project-mask" @click.self="creating = false">
      <form class="dialog" role="dialog" aria-label="新建项目" @submit.prevent="submitCreate">
        <h3>新建项目</h3>
        <label>项目名（当目录名用）
          <input v-model.trim="form.name" data-testid="np-name" placeholder="雨夜地铁" maxlength="60" required />
        </label>
        <p v-if="nameHint" class="warn small">{{ nameHint }}</p>
        <label>显示标题（可选，默认同项目名）
          <input v-model.trim="form.title" data-testid="np-title" placeholder="《雨夜地铁》第一集" maxlength="80" />
        </label>
        <label>来源类型
          <select v-model="form.source_type" data-testid="np-source">
            <option value="novel">小说（散文正文 → 拆镜）</option>
            <option value="script">剧本（先占位，拆镜规范待实装）</option>
          </select>
        </label>
        <p v-if="form.source_type === 'script'" class="warn small">
          剧本源目前只记录来源类型，<b>拆镜仍按小说散文的规范跑</b>
          （台词逐句回原文、约每 90 字一镜）。真要按"场次→镜头"拆剧本是另一期工作。
        </p>
        <label>一句话题材（可选）
          <input v-model.trim="form.logline" data-testid="np-logline" placeholder="末班地铁停在不该停的站台" maxlength="500" />
        </label>
        <label>画面风格
          <select v-model="form.style_preset" data-testid="np-style">
            <option value="auto">auto —— 拆镜前由系统按内容推荐，我再确认</option>
            <option value="realistic">realistic —— 写实电影感</option>
            <option value="cg">cg —— 半写实 3D 建模</option>
            <option value="anime">anime —— 日式二维动画</option>
          </select>
        </label>
        <p class="dim small">
          画风实测由<b>定妆参考图</b>决定、文字控不住；这里先定"写法基准"，
          P2 的「风格」屏会用它一次性出定妆/场景/道具图。
        </p>
        <footer>
          <button type="button" class="ghost" data-testid="np-cancel" @click="creating = false">取消</button>
          <span class="spacer" />
          <button type="submit" class="primary" :disabled="busy || !form.name" data-testid="np-submit">
            {{ busy ? '创建中…' : '创建并去导章节' }}
          </button>
        </footer>
      </form>
    </div>
  </section>
</template>

<script setup lang="ts">
/**
 * W0 项目屏（P1.4）。
 *
 * 这块界面以前**根本不存在**：README 的「开始用」是两行 shell
 * （`mkdir -p projects/我的剧/novel` + `cp 第一章.md`），后台也没有任何
 * `project/create` 端点。唯一能"造出项目"的路径是对不存在的名字保存配置，
 * 那会造出一个没有 novel/ 的畸形项目。
 *
 * ★ 建完不留在本页：直接跳章节屏。"新建项目"本身不是目的，
 *   把正文弄进来才是 —— 少一次"用户还得知道下一步点哪"。
 */
import { computed, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api } from '@/api/client'
import { confirmAction } from '@/composables/confirmAction'
import type { ProjectSummary, ProjectTrashItem } from '@/api/types'
import { loadProjects, state } from '@/stores/app'

const router = useRouter()
const projects = computed(() => state.projects)
const creating = ref(false)
const busy = ref(false)
const showTrash = ref(false)
const trashLoading = ref(false)
const trash = ref<ProjectTrashItem[]>([])
const form = reactive({
  name: '', title: '', source_type: 'novel', logline: '', style_preset: 'auto',
})

const NAME_BAD = /[/\\:*?"<>|'`$;&|()\x00]/

const nameHint = computed(() => {
  const n = form.name
  if (!n) return ''
  if (n.startsWith('.') || n.startsWith('_')) return '不能以 . 或 _ 开头（那是内部/备份目录的约定）'
  if (NAME_BAD.test(n)) return '含非法字符：斜杠、冒号、引号、$、括号等都不行'
  if (projects.value.some((p) => p.name === n)) return `已有同名项目「${n}」—— 后台会拒绝覆盖，不会清空重建`
  return ''
})

function metaOf(pj: ProjectSummary) {
  // meta 随 /api/projects 一起回来（见 vm/web.py 的 api_projects）；
  // 老项目没这个文件时后端反推一份，所以这里不需要兜底
  return pj.meta || { logline: '', title: pj.name, name: pj.name, source_type: 'novel', style_preset: 'auto' }
}

async function loadTrash(): Promise<void> {
  trashLoading.value = true
  try {
    trash.value = (await api.projectTrash()).items
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    trashLoading.value = false
  }
}

async function toggleTrash(): Promise<void> {
  showTrash.value = !showTrash.value
  if (showTrash.value) await loadTrash()
}

async function restoreProject(entry: string, name: string): Promise<void> {
  if (busy.value) return
  const ok = await confirmAction({
    title: `恢复项目「${name}」`,
    message: '会把它从 projects/_trash/ 放回 projects/。\n若已有同名项目，系统会拒绝覆盖。',
    list: [name],
    confirmText: '恢复',
  })
  if (!ok) return
  busy.value = true
  try {
    const r = await api.restoreProject(entry)
    await Promise.all([loadProjects(), loadTrash()])
    ElMessage.success(r.message)
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    busy.value = false
  }
}

function fmtBytes(n: number): string {
  return n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.ceil(n / 1024)} KB`
}
function fmtTime(t: number): string { return new Date(t * 1000).toLocaleString() }

function openCreate(): void {
  form.name = ''; form.title = ''; form.logline = ''
  form.source_type = 'novel'; form.style_preset = 'auto'
  creating.value = true
}

async function submitCreate(): Promise<void> {
  if (!form.name || nameHint.value) {
    ElMessage.warning(nameHint.value || '先给项目起个名字')
    return
  }
  busy.value = true
  try {
    const r = await api.createProject({ ...form })
    await loadProjects()
    state.project = r.project
    ElMessage.success(r.message)
    creating.value = false
    await router.push('/chapters')
  } catch (e) {
    // 错误消息后台已经写成给人看的中文，这里不再包一层
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    busy.value = false
  }
}

async function deleteProject(pj: ProjectSummary): Promise<void> {
  if (pj.running || busy.value) return
  const ok = await confirmAction({
    title: `删除项目「${pj.name}」`,
    message: `会把整个项目移进 projects/_trash/（正文、镜头表、参考图、成片都会一起移走）。\n`
      + '不会真的 rm；在回收站里可以恢复。\n\n请手输项目名确认。',
    list: [
      `${pj.novel_chapters} 章正文`, `${pj.shot_files} 份镜头表`, `${pj.char_prompts} 个角色提示词`,
      `${pj.clips} 个片段`, `${pj.finals?.length || (pj.final ? 1 : 0)} 个成片`,
    ],
    confirmText: '移进回收站',
    danger: true,
    requireTyped: pj.name,
  })
  if (!ok) return
  busy.value = true
  try {
    const r = await api.deleteProject(pj.name, pj.name)
    // 删的是当前项目时不能继续让全局 state 指向不存在的目录：先选一个剩余项目；
    // 没剩项目就清空。App.vue 的 project watch 会负责清掉旧项目级数据。
    if (state.project === pj.name) state.project = projects.value.find((x) => x.name !== pj.name)?.name || ''
    await loadProjects()
    if (showTrash.value) await loadTrash()
    ElMessage.success(r.message)
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    busy.value = false
  }
}

async function go(name: string, path: string): Promise<void> {
  state.project = name
  await router.push(path)
}

async function copyPath(path: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(path)
    ElMessage.success('路径已复制')
  } catch {
    ElMessage.info(path)
  }
}
</script>

<style scoped>
.ws { flex: 1 1 auto; min-height: 0; overflow: auto; padding: 14px 16px; }
.wshead { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.wshead h2 { margin: 0; font-size: 15px; color: var(--ink); }
.spacer { flex: 1 1 auto; }
.empty { padding: 40px 12px; text-align: center; color: var(--ink); }
.trash {
  margin-bottom: 12px; padding: 10px 12px; border: 1px dashed var(--line);
  border-radius: var(--radius); background: var(--surface-2);
}
.trashhead, .trashrow { display: flex; align-items: center; gap: 8px; }
.trashhead { margin-bottom: 7px; font-size: 12px; }
.trash > p { margin: 4px 0; }
.trashrow { padding: 7px 0; border-top: 1px solid var(--line); font-size: 12px; }
.trashrow b { min-width: 80px; }
.empty p { margin: 0 0 8px; font-size: 13px; }
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 12px; }
.card {
  background: var(--card); border: 1px solid var(--line); border-radius: var(--radius);
  padding: 12px 14px; display: flex; flex-direction: column; gap: 8px;
}
.card.current { border-color: var(--lime-deep); box-shadow: 0 0 0 1px var(--lime-deep) inset; }
.cardhead { display: flex; align-items: center; gap: 8px; font-size: 13.5px; }
.cardhead b { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pill { font-size: 11px; padding: 1px 7px; border-radius: 999px; border: 1px solid var(--line); }
.pill.run { color: var(--run); background: var(--run-bg); border-color: var(--run-border); }
.pill.idle { color: var(--muted); }
.logline { margin: 0; font-size: 12px; line-height: 1.6; }
.stats { display: flex; flex-wrap: wrap; gap: 10px 16px; margin: 0; }
.stats > div { display: flex; flex-direction: column; gap: 1px; }
.stats dt { font-size: 10.5px; color: var(--muted); }
.stats dd { margin: 0; font-size: 14px; color: var(--ink); }
.prog { display: flex; align-items: center; gap: 8px; }
.prog i { display: block; height: 4px; min-width: 2px; background: var(--lime-deep); border-radius: 2px; flex: 0 0 auto; width: 0; flex-basis: 40%; }
.cardfoot { display: flex; align-items: center; gap: 6px; margin-top: 2px; }
button {
  font: inherit; font-size: 12px; cursor: pointer; border-radius: 8px;
  border: 1px solid var(--line); background: var(--surface-2); color: var(--ink); padding: 4px 9px;
}
button:hover { background: var(--hover); border-color: var(--hover-border); }
button.small { font-size: 11.5px; padding: 3px 8px; }
button.ghost { background: transparent; }
button.danger { color: var(--bad); border-color: color-mix(in srgb, var(--bad) 45%, var(--line)); }
button.danger:hover:not(:disabled) { background: color-mix(in srgb, var(--bad) 10%, var(--card)); }
button.primary { background: var(--lime-deep); color: var(--bg); border-color: var(--lime-deep); font-weight: 600; }
button:disabled { opacity: .5; cursor: not-allowed; }
.dim { color: var(--muted); }
.small { font-size: 11.5px; }
.warn { color: var(--warn); margin: 0; }
.tabular { font-variant-numeric: tabular-nums; }
.mask {
  position: fixed; inset: 0; z-index: 2600; background: color-mix(in srgb, var(--ink) 32%, transparent);
  display: flex; align-items: center; justify-content: center; padding: 18px;
}
.dialog {
  width: min(520px, 96vw); max-height: 90vh; overflow: auto;
  background: var(--card); border: 1px solid var(--line); border-radius: var(--radius);
  box-shadow: var(--shadow); padding: 16px 18px; display: flex; flex-direction: column; gap: 10px;
}
.dialog h3 { margin: 0 0 2px; font-size: 14.5px; color: var(--ink); }
.dialog label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.dialog input, .dialog select {
  font: inherit; font-size: 12.5px; color: var(--ink); background: var(--surface-2);
  border: 1px solid var(--line); border-radius: 8px; padding: 6px 8px;
}
.dialog input:focus, .dialog select:focus { outline: none; border-color: var(--lime-deep); }
.dialog p { margin: 0; line-height: 1.6; }
.dialog footer { display: flex; align-items: center; gap: 8px; margin-top: 4px; }
</style>
