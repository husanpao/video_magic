<!--
  设置面板（T4）—— 终结「配置都是后台固定的，用户抓瞎」。

  ★ 接线方式（给 Lead / 顶栏入口按钮用，本组件不依赖 TopBar）：
      import SettingsDialog from '@/components/SettingsDialog.vue'
      <SettingsDialog v-model="settingsOpen" :project="state.project" />
    v-model 控显隐；project 不传则回落到 stores/app 的当前项目。
  ★ 每项三段说明文案（是什么 / 改了影响什么 / 会不会让镜头变 stale）直接摊开写，
    不藏 tooltip —— 用户就是来搞懂这些东西的。
  ★ 危险项（进镜头指纹）保存时先摆「影响清单」再确认（同 ResetDialog 的安全纪律）。
  ★ 颜色一律 theme.css 的 CSS 变量（T7 要做暗色模式，禁止硬编码色值）。
-->
<template>
  <el-dialog
    v-model="open"
    title="设置"
    width="820px"
    top="6vh"
    :close-on-click-modal="false"
    class="settings-dialog"
    data-testid="settings-dialog"
  >
    <div class="sg" data-testid="settings-dialog-body">
      <!-- 顶栏：作用范围 + Key 状态 -->
      <div class="head">
        <el-radio-group :model-value="cfg.mode" size="small" @change="onMode">
          <el-radio-button value="project">本项目{{ cfg.project ? `：${cfg.project}` : '' }}</el-radio-button>
          <el-radio-button value="global">全局默认（所有项目）</el-radio-button>
        </el-radio-group>
        <span class="spacer" />
        <span class="dim small" :title="API_KEY_HINT">
          API Key：{{ cfg.apiKeyPresent ? '已配置（内容不展示）' : '未配置' }}
        </span>
      </div>

      <div v-for="n in cfg.notes" :key="n" class="note">{{ n }}</div>

      <div v-if="cfg.errors.length" class="errbox" data-testid="settings-errors">
        <div v-for="(e, i) in cfg.errors" :key="i" class="errline">{{ e.message }}</div>
      </div>

      <div v-if="cfg.loading" class="dim">正在加载配置…</div>

      <!-- 分组设置 -->
      <div v-for="[group, items] in itemsByGroup" :key="group" class="grp">
        <h3 class="grptitle">{{ group }}</h3>
        <div v-for="it in items" :key="it.key" class="row" :class="{ danger: it.stale_impact }">
          <div class="lab">
            <div class="labname">
              {{ it.label }}
              <span v-if="it.stale_impact" class="tag bad">改了会重渲</span>
              <span v-else-if="it.render_only" class="tag warn">不入指纹·需强制重渲</span>
            </div>
            <div class="badges">
              <span class="tag" :class="sourceTone(it)">{{ sourceText(it) }}</span>
              <el-button
                v-if="canInherit(it)"
                size="small"
                text
                type="primary"
                data-testid="settings-inherit"
                @click="inherit(it.key)"
              >恢复继承</el-button>
              <el-button
                v-if="cfg.draft[it.key]"
                size="small"
                text
                @click="clearDraft(it.key)"
              >撤销修改</el-button>
            </div>
          </div>

          <div class="ctl">
            <template v-if="editable(it)">
              <el-switch
                v-if="it.kind === 'bool'"
                :model-value="shownValue(it) === true"
                @update:model-value="(v) => onBool(it, v)"
              />
              <el-select
                v-else-if="it.kind === 'enum'"
                :model-value="String(shownValue(it) ?? '')"
                size="default"
                style="width: 240px"
                @update:model-value="(v) => onEnum(it, v)"
              >
                <el-option v-for="opt in it.enum" :key="opt" :value="opt" :label="enumLabel(it, opt)" />
              </el-select>
              <el-input
                v-else-if="it.kind === 'str_list'"
                type="textarea"
                :rows="3"
                :model-value="listText(shownValue(it))"
                :placeholder="it.placeholder"
                @update:model-value="(v) => onList(it, v)"
              />
              <template v-else-if="it.kind === 'int' || it.kind === 'float'">
                <div class="numrow">
                  <el-input-number
                    :model-value="numOf(shownValue(it))"
                    :min="it.minimum ?? undefined"
                    :max="it.maximum ?? undefined"
                    :step="it.kind === 'float' ? 0.1 : 1"
                    :placeholder="it.nullable ? '不设限' : ''"
                    controls-position="right"
                    style="width: 180px"
                    @update:model-value="(v) => onNum(it, v)"
                  />
                  <el-button
                    v-if="it.nullable"
                    size="small"
                    text
                    @click="setDraft(it.key, null)"
                  >不设限</el-button>
                </div>
              </template>
              <el-input
                v-else
                :model-value="String(shownValue(it) ?? '')"
                :placeholder="it.placeholder"
                style="width: 340px"
                @update:model-value="(v) => onText(it, v)"
              />
            </template>
            <div v-else class="dim small">
              {{ it.scope === 'global' ? '机器级设置 —— 切到「全局默认」里改（否则 Web 端和渲染端的指纹会对不上）'
                                        : '只在「本项目」层生效 —— 切到本项目里改' }}
            </div>
            <div class="cap">{{ it.what }}</div>
            <div class="cap">{{ it.effect }}</div>
            <div class="cap" :class="it.stale_impact || it.render_only ? 'warn' : 'dim'">{{ it.stale }}</div>
          </div>
        </div>
      </div>

      <!-- 预检结果 -->
      <div v-if="cfg.checks.length" class="checks" data-testid="settings-checks">
        <h3 class="grptitle">预检结果</h3>
        <div v-for="c in cfg.checks" :key="c.id" class="checkline">
          <span class="tag" :class="c.ok ? 'ok' : 'bad'">{{ c.ok ? '✓' : '✗' }}</span>
          <b>{{ c.label }}</b>
          <span class="dim">{{ c.detail }}</span>
        </div>
      </div>
    </div>

    <template #footer>
      <el-button @click="open = false">取消</el-button>
      <el-button
        :loading="cfg.validating"
        :disabled="cfg.loading"
        data-testid="settings-validate"
        @click="onValidate"
      >保存前预检</el-button>
      <el-button
        type="primary"
        :loading="cfg.saving"
        :disabled="cfg.loading || !dirtyCount"
        data-testid="settings-save"
        @click="onSave"
      >保存{{ dirtyCount ? `（${dirtyCount} 项）` : '' }}</el-button>
    </template>
  </el-dialog>

  <!-- 危险项影响确认：先摆清单再动手（进镜头指纹的改动会让已生成镜头变需重渲） -->
  <el-dialog v-model="impactOpen" title="保存前确认：这些改动有代价" width="560px" :close-on-click-modal="false">
    <div v-if="cfg.impact" class="sg">
      <div class="impactmsg">{{ cfg.impact.message }}</div>
      <div v-for="d in cfg.impact.dangerous" :key="d.key" class="impactrow danger">
        <b>{{ d.label }}</b>：{{ fmtVal(d.old) }} → <b>{{ fmtVal(d.new) }}</b>
        <div class="cap warn">{{ d.stale }}</div>
      </div>
      <div v-for="s in cfg.impact.silent" :key="s.key" class="impactrow">
        <b>{{ s.label }}</b>：{{ fmtVal(s.old) }} → {{ fmtVal(s.new) }}
        <div class="cap warn">{{ s.note }}</div>
      </div>
      <div v-if="cfg.impact.stale_shots.length" class="shots">
        将变「需重渲」的镜头：
        <span v-for="id in cfg.impact.stale_shots" :key="id" class="tag warn">{{ id }}</span>
      </div>
    </div>
    <template #footer>
      <el-button @click="impactOpen = false">再想想</el-button>
      <el-button
        type="primary"
        :loading="cfg.saving"
        data-testid="settings-impact-confirm"
        @click="onConfirmSave"
      >确认保存</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import {
  ElButton, ElDialog, ElInput, ElInputNumber, ElMessage, ElOption,
  ElRadioButton, ElRadioGroup, ElSelect, ElSwitch,
} from 'element-plus'
import type { ConfigItem, ConfigValue } from '@/api/config'
import {
  cfg, clearDraft, dirtyCount, editable, inherit, itemsByGroup, loadConfig,
  runValidate, saveDraft, setDraft, shownValue, resetAllDrafts,
} from '@/stores/config'
import { refreshShots, refreshStatus, state as appState } from '@/stores/app'

/** v-model 控显隐；project 不传回落到当前项目（接线方零负担）。 */
const open = defineModel<boolean>({ required: true })
const props = defineProps<{ project?: string }>()

const API_KEY_HINT = 'DeepSeek API Key 不走配置（纪律）：只认环境变量 DEEPSEEK_API_KEY 或 ~/.config/video_magic/deepseek_key 文件；配置接口永远不会接收或返回 Key 本身。'

const project = computed(() => props.project || appState.project || '')
const showImpact = ref(false)
const impactOpen = computed({
  get: () => showImpact.value,
  set: (v: boolean) => { showImpact.value = v },
})

watch(open, (v) => {
  if (v) {
    resetAllDrafts()
    loadConfig(project.value)
  }
})

function onMode(v: string | number | boolean | undefined) {
  cfg.mode = v === 'global' ? 'global' : 'project'
}

// ── 表单控件适配（草稿值是联合类型，控件各要各的形状）────────────────────

function numOf(v: ConfigValue): number | undefined {
  return typeof v === 'number' ? v : undefined
}

function onNum(it: ConfigItem, v: number | undefined): void {
  setDraft(it.key, v ?? null)
}

function onBool(it: ConfigItem, v: boolean | string | number): void {
  setDraft(it.key, v === true)
}

function onEnum(it: ConfigItem, v: string | number): void {
  setDraft(it.key, String(v))
}

function onText(it: ConfigItem, v: string | number): void {
  setDraft(it.key, String(v))
}

function listText(v: ConfigValue): string {
  return Array.isArray(v) ? v.join('\n') : ''
}

function onList(it: ConfigItem, v: string | number): void {
  setDraft(it.key, String(v).split('\n').map((s) => s.trim()).filter(Boolean))
}

function fmtVal(v: ConfigValue): string {
  if (v === null || v === undefined || v === '') return '（不设置）'
  return Array.isArray(v) ? v.join('、') : String(v)
}

const STYLE_LABEL: Record<string, string> = {
  auto: 'auto（不指定，按题材推导）',
  realistic: 'realistic 写实电影感',
  cg: 'cg 半写实 3D 建模',
  anime: 'anime 日式二维动画',
}

function enumLabel(it: ConfigItem, v: string): string {
  return it.key === 'style_preset' ? (STYLE_LABEL[v] || v) : v
}

function sourceText(it: ConfigItem): string {
  switch (it.source) {
    case 'project': return '项目覆盖'
    case 'workspace': return '继承·全局'
    case 'user': return '继承·用户'
    case 'env': return '继承·环境变量'
    default: return '默认值'
  }
}

function sourceTone(it: ConfigItem): string {
  return it.source === 'project' ? 'ok' : ''
}

function canInherit(it: ConfigItem): boolean {
  if (cfg.mode === 'project') return it.overridden
  return it.layers.workspace !== null && it.layers.workspace !== undefined
}

// ── 动作 ──────────────────────────────────────────────────────────────────

async function onValidate() {
  await runValidate()
  const bad = cfg.checks.filter((c) => !c.ok).length
  if (cfg.errors.length) ElMessage.error(`配置有 ${cfg.errors.length} 处不合法`)
  else if (bad) ElMessage.warning(`预检发现 ${bad} 个环境问题（见下方预检结果）`)
  else ElMessage.success('预检通过：配置合法，环境就绪')
}

async function onSave() {
  const r = await saveDraft(false)
  if (r.needsConfirm) {
    showImpact.value = true   // 先摆影响清单，确认后才真保存
    return
  }
  finishSave(r.ok, r.message)
}

async function onConfirmSave() {
  const r = await saveDraft(true)
  if (r.needsConfirm) return   // 理论上不会；兜底别关窗
  showImpact.value = false
  finishSave(r.ok, r.message)
}

async function finishSave(ok: boolean, message: string) {
  if (!ok) {
    ElMessage.error(message)
    return
  }
  ElMessage.success(message + '；改动过的渲染参数会让相关镜头变「需重渲」')
  // 保存成功要让镜头表马上看到新指纹（写后即刷）；这两个函数归 T5，只调用不改
  try {
    await refreshShots()
    await refreshStatus()
  } catch {
    /* 刷新失败不吞掉"保存成功"这件事，错误条由 T5 的全局通道显示 */
  }
}
</script>

<style scoped>
.sg { display: flex; flex-direction: column; gap: 10px; max-height: 72vh; overflow-y: auto; padding-right: 4px; }
.head { display: flex; align-items: center; gap: 10px; }
.spacer { flex: 1; }
.note { font-size: 11.5px; color: var(--muted); background: var(--bg); border: 1px solid var(--line); border-radius: var(--radius-sm); padding: 6px 9px; }
.errbox { border: 1px solid var(--bad); border-radius: var(--radius-sm); padding: 8px 10px; display: flex; flex-direction: column; gap: 4px; }
.errline { font-size: 12px; color: var(--bad); }
.grp { display: flex; flex-direction: column; gap: 6px; }
.grptitle { margin: 8px 0 2px; font-size: 13px; color: var(--ink); border-bottom: 1px solid var(--line); padding-bottom: 4px; }
.row { display: grid; grid-template-columns: 210px 1fr; gap: 12px; padding: 8px; border-radius: var(--radius-sm); }
.row:hover { background: var(--hover); }
.row.danger { border-left: 3px solid var(--bad); }
.lab { display: flex; flex-direction: column; gap: 4px; }
.labname { font-size: 12.5px; font-weight: 600; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.badges { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; }
.ctl { display: flex; flex-direction: column; gap: 3px; }
.ctl > *:first-child { align-self: flex-start; }
.numrow { display: flex; align-items: center; gap: 6px; }
.cap { font-size: 11px; color: var(--muted); line-height: 1.5; }
.cap.warn { color: var(--warn); }
.tag { font-size: 10.5px; padding: 1px 6px; border-radius: 8px; border: 1px solid var(--line); color: var(--muted); white-space: nowrap; }
.tag.ok { color: var(--ok); border-color: var(--ok); }
.tag.warn { color: var(--warn); border-color: var(--warn); }
.tag.bad { color: var(--bad); border-color: var(--bad); }
.checks { display: flex; flex-direction: column; gap: 4px; }
.checkline { display: flex; align-items: center; gap: 8px; font-size: 12px; }
.small { font-size: 11px; }
.dim { color: var(--muted); }
.impactmsg { font-size: 12.5px; line-height: 1.6; }
.impactrow { padding: 6px 8px; border-radius: var(--radius-sm); font-size: 12px; }
.impactrow.danger { background: var(--hover); }
.shots { display: flex; align-items: center; gap: 5px; flex-wrap: wrap; font-size: 12px; color: var(--warn); margin-top: 4px; }
</style>
