<template>
  <div class="tabroot">
    <div class="summary">
      <span>{{ summary }}</span>
      <span class="spacer"></span>
      <el-button size="small" text :loading="loading" @click="load(true)">刷新</el-button>
    </div>
    <div class="tabbody">
      <div v-if="err" class="emptyrow">角色定妆获取失败：{{ err }}</div>
      <div v-else-if="!state.chars.length" class="emptyrow">
        没有角色提示词。先跑「拆镜」，或在 prompts/ 放 char_&lt;角色&gt;.txt。
      </div>
      <template v-else>
        <div v-for="c in state.chars" :key="c.name" class="charcard">
          <div class="charhead">
            <img
              v-if="c.ref_url"
              class="ref"
              :src="c.ref_url"
              loading="lazy"
              title="当前定妆照（采纳后就是这张）"
              alt=""
            >
            <div v-else class="ph-ref">
              还没有定妆照<br><span class="small">点「抽卡」或顶部「定妆」</span>
            </div>
            <div class="charinfo">
              <div class="name">
                <span>{{ c.name }}</span>
                <span class="dim small">
                  参考图 {{ c.ref_mtime ? new Date(c.ref_mtime * 1000).toLocaleString() : '无' }}
                </span>
                <el-tag
                  v-if="c.ref_url && c.ref_synced === false"
                  size="small"
                  type="warning"
                  title="ComfyUI 的 LoadImage 只读它自己的 input/ 目录"
                >⚠ 未同步</el-tag>
              </div>
              <!-- ★ 提示词可编辑（2026-09-24）：它决定抽卡抽出来什么。
                   后端 /api/chars/prompt 一直存在，但前端从来没接过入口 ——
                   于是用户只能接受系统写好的提示词，这不对。 -->
              <textarea
                class="promptedit"
                :value="promptText[c.name] ?? c.prompt"
                spellcheck="false"
                rows="5"
                @input="promptText[c.name] = ($event.target as HTMLTextAreaElement).value"
                @keydown.stop
              />
              <div class="perow">
                <button
                  class="pebtn"
                  :disabled="busyName === c.name || (promptText[c.name] ?? c.prompt) === c.prompt"
                  @click="savePrompt(c.name)"
                >
                  {{ (promptText[c.name] ?? c.prompt) === c.prompt ? '已保存' : '保存提示词' }}
                </button>
                <button
                  v-if="(promptText[c.name] ?? c.prompt) !== c.prompt"
                  class="pebtn ghost"
                  @click="promptText[c.name] = c.prompt"
                >撤销</button>
                <!-- ★ 上传自有定妆照：`/api/chars/upload` 早就存在，前端一直没接入口。
                     适配直接替换 refs/char_<名>.png（并同步到 ComfyUI input）。 -->
                <ImgUpload :handler="(f, b) => uploadPortrait(c.name, f, b)" label="上传定妆照" />
                <span class="dim small">保存后「抽卡」出的图就会跟着变（已采纳的定妆照不受影响）</span>
              </div>
              <div v-if="(c.prompt_warnings || []).length" class="stylewarn">
                <b>⚠ 检测到风格锚：{{ (c.prompt_warnings || []).join('、') }}</b><br>
                实测会把画面拉成廉价 3D 卡通；建议删掉，改用朴实的写实描述。
              </div>
              <div class="charrow">
                <span class="dim small">抽卡张数</span>
                <el-input-number
                  v-model="counts[c.name]"
                  class="ninput"
                  size="small"
                  :min="1"
                  :max="24"
                  controls-position="right"
                  title="每张约 40 秒；已存在的 seed 会自动跳过（默认 2 张）"
                />
                <el-button type="primary" size="small" @click="gacha(c)">抽卡</el-button>
              </div>
            </div>
          </div>

          <div v-if="(c.candidates || []).length" class="candgrid">
            <div
              v-for="(cand, i) in c.candidates"
              :key="candKey(cand, i)"
              class="cand"
              :class="{ adopted: !!cand.adopted }"
            >
              <img
                :src="thumb(cand)"
                :title="cand.adopted ? '当前定妆照（已是这张）' : '点图即采纳这张'"
                loading="lazy"
                alt=""
                @click="adopt(c, cand)"
              >
              <div class="cap">
                <span>seed {{ seed(cand) }}</span>
                <span>{{ cand.adopted ? '当前' : fmtSize(sizeOf(cand)) }}</span>
              </div>
              <div class="candfoot">
                <el-button size="small" :disabled="!!cand.adopted" @click="adopt(c, cand)">
                  {{ cand.adopted ? '✓ 当前' : '采纳' }}
                </el-button>
                <a v-if="urlOf(cand)" class="big" :href="urlOf(cand)" target="_blank" rel="noreferrer">大图</a>
              </div>
            </div>
          </div>
        </div>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 角色定妆。
 *
 * 移植自 index.html 的 `refreshChars()` / `renderChars()`：
 *   每个角色一张卡：名字 + 参考图缩略图 +（未同步标记）+ 提示词与风格锚警告
 *   + 候选图网格（seed / 大小 / 是否当前）+ 抽卡按钮（**默认 n=2**）+ 点候选图采纳。
 * 采纳后中提示「已标 stale」—— 定妆照变了，相关已生成镜头需重渲。
 *
 * 旧文件的「保存提示词 / 上传自有图」依赖 `/api/chars/prompt`、`/api/chars/upload`，
 * 而固定的 `src/api/client.ts` 没有这两个方法（也不能改），故此处只做只读展示。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { ElButton, ElInputNumber, ElMessage, ElTag } from 'element-plus'
import ImgUpload from '@/components/ImgUpload.vue'
import { api } from '@/api/client'
import type { CharCandidate, CharRow } from '@/api/types'
import { fmtSize, refreshChars, refreshShots, refreshStatus, state } from '@/stores/app'

const loading = ref(false)
const err = ref('')
/** 每个角色的抽卡张数，默认 2（用户明确要求「抽卡抽 2 张就行，不要那么多」）。 */
const counts = reactive<Record<string, number | undefined>>({})

const candTotal = computed(() => state.chars.reduce((n, c) => n + (c.candidates || []).length, 0))
const warnTotal = computed(() => state.chars.filter((c) => (c.prompt_warnings || []).length).length)
const summary = computed(() => {
  if (err.value) return `角色定妆获取失败：${err.value}`
  if (!state.chars.length) return loading.value ? '加载中…' : '没有角色'
  return `${state.chars.length} 个角色 · ${candTotal.value} 张候选` +
    (warnTotal.value ? ` · ⚠ ${warnTotal.value} 个提示词含风格锚` : '')
})

// CharCandidate 现在是**精确类型**（字段对齐真实 /api/chars 响应），直接取即可 ——
// 之前那套 candStr/candNum 守卫是给松类型（[k:string]: unknown）写的，会把拼错的字段名吞掉。
const thumb = (c: CharCandidate): string => c.thumb_url || c.url
const urlOf = (c: CharCandidate): string => c.url
const seed = (c: CharCandidate): string | number => c.seed ?? '—'
const sizeOf = (c: CharCandidate): number => c.size ?? 0
const candKey = (c: CharCandidate, i: number): string => c.file || c.url || String(i)

function strList(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : []
}

/** 首次进入拉一次；之后靠 RightPanel 的切换刷新或「刷新」按钮，不每次切都重拉。 */
async function load(force = false) {
  if (loading.value) return
  if (!force && state.chars.length) return
  loading.value = true
  err.value = ''
  try {
    await refreshChars()
    for (const c of state.chars) if (counts[c.name] === undefined) counts[c.name] = 2
  } catch (e) {
    err.value = (e as Error).message
  } finally {
    loading.value = false
  }
}

async function gacha(c: CharRow) {
  const n = Number(counts[c.name] ?? 2) || 2
  if (!window.confirm(`给「${c.name}」抽 ${n} 张候选？\n（GPU 任务，约每张 40 秒；已存在的 seed 会自动跳过）`)) return
  try {
    const r = await api.gacha(state.project, c.name, n)
    ElMessage.success(r.message || `已启动「${c.name}」抽卡（${n} 张），进度见「日志」tab`)
    try {
      await refreshStatus()
    } catch { /* 状态刷新失败不影响抽卡已启动这一事实 */ }
    await load(true)
  } catch (e) {
    ElMessage.error(`抽卡启动失败：${(e as Error).message}`)
  }
}

/** 点候选图 = 采纳（规格要求）；按钮走同一条路径。 */
async function adopt(c: CharRow, cand: CharCandidate) {
  if (cand.adopted) return
  // ★ 文件在**候选**上，不在 CharRow 上。原先写成 c.file 会永远拿不到值。
  const file = cand.file
  if (!file) {
    ElMessage.warning('这张候选没有文件名，无法采纳')
    return
  }
  if (!window.confirm(`采纳 ${file} 作为「${c.name}」的定妆照？\n已生成的相关镜头会被标成「需重渲」。`)) return
  try {
    const r = await api.adopt(state.project, c.name, file)
    const stale = strList(r.stale_shots)
    const warn = typeof r.warning === 'string' ? r.warning : ''
    const parts = [r.message || `已采纳 ${file} 作为「${c.name}」的定妆照`]
    parts.push(
      stale.length
        ? `已标 stale：${stale.length} 个相关镜头需重渲（${stale.slice(0, 8).join('、')}${stale.length > 8 ? ' …' : ''}）`
        : '已标 stale：相关镜头（若有）需重渲',
    )
    if (warn) parts.push(`⚠ ${warn}`)
    ElMessage.success({ message: parts.join('；'), duration: 6000 })
    await load(true)
    try {
      await refreshShots()
      await refreshStatus()
    } catch { /* 采纳已成功，列表刷新失败不改变结论 */ }
  } catch (e) {
    ElMessage.error(`采纳失败：${(e as Error).message}`)
  }
}

onMounted(() => {
  void load()
})

// ── 角色提示词编辑 ──
const promptText = reactive<Record<string, string>>({})
const busyName = ref('')

async function savePrompt(name: string) {
  const text = promptText[name]
  if (text === undefined) return
  busyName.value = name
  try {
    const r = await api.charPrompt(state.project, name, text)
    ElMessage.success(String(r.message || '已保存'))
    await refreshChars()
  } catch (e) {
    ElMessage.error(`保存失败：${(e as Error).message}`)
  } finally {
    busyName.value = ''
  }
}

/** 上传自有定妆照 → 直接作为该角色的参考图（并同步到 ComfyUI input）。 */
async function uploadPortrait(name: string, filename: string, b64: string) {
  const r = await api.charUpload(state.project, name, filename, b64)
  ElMessage.success(String(r.message || '已上传定妆照'))
  await refreshChars()
  await refreshShots()
}
</script>

<style scoped>
.charcard {
  border-bottom: 1px solid var(--line);
  padding: 10px 2px;
}
.charhead {
  display: flex;
  gap: 10px;
  align-items: flex-start;
}
.charhead img.ref {
  width: 104px;
  height: 104px;
  object-fit: cover;
  border-radius: var(--radius-sm);
  border: 2px solid var(--line);
  background: #eef0f3;
  flex: 0 0 auto;
}
.charhead .ph-ref {
  width: 104px;
  height: 104px;
  border-radius: var(--radius-sm);
  border: 2px dashed var(--line);
  display: flex;
  align-items: center;
  justify-content: center;
  color: #8b93a1;
  font-size: 11px;
  text-align: center;
  flex: 0 0 auto;
}
.charinfo {
  flex: 1;
  min-width: 0;
}
.charinfo .name {
  font-weight: 600;
  font-size: 13px;
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  align-items: center;
}
.prompt {
  margin: 6px 0 0;
  max-height: 92px;
  overflow: auto;
  padding: 6px 8px;
  font: 11.5px/1.5 ui-monospace, Menlo, Consolas, monospace;
  white-space: pre-wrap;
  word-break: break-word;
  color: #4b5563;
  background: #fcfdfe;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
}
.stylewarn {
  margin-top: 6px;
  padding: 7px 10px;
  border-radius: var(--radius-sm);
  font-size: 11.5px;
  line-height: 1.6;
  background: color-mix(in srgb, var(--warn) 10%, #fff);
  border: 1px solid color-mix(in srgb, var(--warn) 28%, #fff);
  color: var(--warn);
}
.charrow {
  display: flex;
  gap: 6px;
  align-items: center;
  margin-top: 6px;
  flex-wrap: wrap;
}
.ninput {
  width: 100px;
}
.candgrid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(96px, 1fr));
  gap: 8px;
  margin-top: 8px;
}
.cand {
  border: 2px solid var(--line);
  border-radius: var(--radius-sm);
  overflow: hidden;
  background: #fff;
}
.cand.adopted {
  border-color: var(--ok);
}
.cand img {
  display: block;
  width: 100%;
  aspect-ratio: 1/1;
  object-fit: cover;
  cursor: pointer;
}
.cand .cap {
  padding: 2px 5px;
  font-size: 10.5px;
  color: var(--muted);
  display: flex;
  justify-content: space-between;
  font-variant-numeric: tabular-nums;
}
.candfoot {
  display: flex;
  align-items: center;
  gap: 4px;
  border-top: 1px solid var(--line);
  padding: 2px 4px;
}
.candfoot .big {
  font-size: 10.5px;
  color: var(--muted);
  flex: 0 0 auto;
}
.promptedit {
  width: 100%; font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 11px;
  line-height: 1.5; border: 1px solid var(--line); border-radius: 8px; padding: 6px 8px;
  resize: vertical; color: #374151; margin-top: 4px;
}
.perow { display: flex; align-items: center; gap: 7px; margin-top: 4px; flex-wrap: wrap; }
.pebtn {
  font: inherit; font-size: 11px; padding: 2px 9px; border-radius: 6px; cursor: pointer;
  border: 1px solid var(--line); background: var(--card); color: var(--ink);
}
.pebtn.ghost { background: transparent; color: var(--muted); }
.pebtn:hover:not(:disabled) { border-color: color-mix(in srgb, var(--lime) 70%, var(--ink)); }
.pebtn:disabled { opacity: .5; cursor: not-allowed; }
</style>
