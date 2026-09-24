<!--
  清空 / 重置项目。

  ★ 三条安全设计（不可逆操作就该这么做）：
  1. **先摆清单再问确定** —— 直接弹"确定清空吗"等于没问：用户不知道删多少、留什么、能否恢复。
  2. **手输项目名才算确认** —— 点一下"确定"太容易误触；打字这个动作本身就是确认。
  3. **说清保底**：元数据（拆镜结果/角色卡/提示词）会自动备份，novel/ 与 project.json 永不删。
-->
<template>
  <el-dialog v-model="open" title="清空 / 重置项目" width="560px" :close-on-click-modal="false">
    <div class="rd">
      <div class="rdrow">
        <span class="dim">重置到哪一步：</span>
        <el-select v-model="scope" size="small" style="width: 300px" @change="load">
          <el-option value="clips" label="只清视频片段（保留镜头表，重渲即可）" />
          <el-option value="shots" label="清镜头表之后的一切（角色/场景/道具/分镜图保留）" />
          <el-option value="all" label="全部清空（只保留 novel/ 与 project.json）" />
        </el-select>
      </div>

      <div v-if="loading" class="dim">正在统计…</div>
      <template v-else-if="preview">
        <div class="rdsec">
          <b>将删除</b>
          <div v-for="t in preview.targets" :key="t.path" class="rdline del">
            <code>{{ t.path }}</code>
            <span class="spacer" />
            <span class="dim num">{{ t.files }} 个文件 · {{ mb(t.bytes) }}</span>
          </div>
          <div v-if="!preview.targets.length" class="dim">（已经没有什么可删）</div>
          <div class="rdtotal">合计释放 <b>{{ mb(preview.total_bytes) }}</b></div>
        </div>

        <div class="rdsec">
          <b>不会动</b>
          <div v-for="k in preview.keep" :key="k.path" class="rdline keep">
            <code>{{ k.path }}</code>
            <span class="spacer" />
            <span class="dim num">{{ mb(k.bytes) }}</span>
          </div>
          <div class="dim small">
            另外：拆镜结果 / 角色卡 / 提示词 会先自动备份到
            <code>_backup/reset-…</code>，视频文件不备份（太大）。
          </div>
        </div>

        <div class="rdsec">
          <b>手输项目名确认：<code>{{ preview.requires_confirm }}</code></b>
          <el-input v-model="typed" size="small" :placeholder="preview.requires_confirm" />
          <div v-if="typed && typed !== preview.requires_confirm" class="rdwarn">
            名字不匹配
          </div>
        </div>
      </template>
    </div>

    <template #footer>
      <el-button @click="open = false">取消</el-button>
      <el-button type="danger" :disabled="!canDo || busy" :loading="busy" @click="doReset">
        确认清空
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { ElButton, ElDialog, ElInput, ElMessage, ElOption, ElSelect } from 'element-plus'
import { api } from '@/api/client'
import type { ResetPreview } from '@/api/types'
import { loadProjects, refreshStatus, state } from '@/stores/app'

const open = defineModel<boolean>({ required: true })
const scope = ref<'clips' | 'shots' | 'all'>('shots')
const preview = ref<ResetPreview | null>(null)
const typed = ref('')
const loading = ref(false)
const busy = ref(false)

const canDo = computed(() =>
  !!preview.value && typed.value === preview.value.requires_confirm)

function mb(n: number) { return n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB` }

async function load() {
  loading.value = true
  typed.value = ''
  try {
    preview.value = await api.resetPreview(state.project, scope.value)
  } catch (e) {
    ElMessage.error((e as Error).message)
    preview.value = null
  } finally {
    loading.value = false
  }
}

async function doReset() {
  if (!canDo.value) return
  busy.value = true
  try {
    const r = await api.reset(state.project, scope.value, typed.value)
    ElMessage.success(String(r.message || '已清空'))
    open.value = false
    // 全项目级状态都要重拉 —— 清空后旧数据一条都不能留（否则界面显示已删的镜头）
    await loadProjects()
    await refreshStatus(true)
    window.location.reload()
  } catch (e) {
    ElMessage.error(`清空失败：${(e as Error).message}`)
  } finally {
    busy.value = false
  }
}

defineExpose({ load })
</script>

<style scoped>
.rd { display: flex; flex-direction: column; gap: 13px; }
.rdrow { display: flex; align-items: center; gap: 8px; }
.rdsec { display: flex; flex-direction: column; gap: 3px; }
.rdsec b { font-size: 12.5px; }
.rdline { display: flex; align-items: center; gap: 6px; font-size: 11.5px; padding: 2px 5px; border-radius: 5px; }
.rdline.del { background: #fdeceb; }
.rdline.keep { background: #eef7ea; }
.rdtotal { font-size: 11.5px; color: var(--muted); margin-top: 2px; }
.rdwarn { font-size: 11px; color: var(--bad); margin-top: 3px; }
</style>
