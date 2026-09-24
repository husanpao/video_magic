<!--
  场景 / 道具的候选概念图 —— 抽卡 + 采纳。
  和角色定妆同一套交互，但**用途不同**：
    · 角色的图会作为 <Picture N> 注入渲染（锁身份）
    · 场景/道具的图**不注入**（H3 的槽位语义是"主体"，塞进去会抢角色槽位）
      它是**审片用的**：一个场景覆盖十几到二十几镜，景不对就是几十镜白渲。
-->
<template>
  <div class="ac" :class="{ inline }">
    <div class="achead">
      <span v-if="!inline" class="dim small">{{ n }} 张候选</span>
      <span v-if="row?.adopted" class="dim small">定稿 {{ row.adopted }}</span>
      <span class="spacer" />
      <ImgUpload :handler="upload" label="上传图片" />
      <button class="acbtn" :disabled="busy" :title="`入队出 2 张候选（约 20 秒，可连点多个）`" @click="gen">
        {{ n ? '再抽 2 张' : '抽卡（2 张）' }}
      </button>
    </div>
    <div v-if="!n" class="dim small acnone">
      还没有概念图。抽 2 张看一眼——景不对的话，渲染前就能发现。
    </div>
    <div v-else class="acgrid">
      <div
        v-for="c in row!.candidates"
        :key="c.file"
        class="accand"
        :class="{ adopted: row!.adopted === c.file }"
        :title="row!.adopted === c.file ? '已采纳' : '点击采纳这张'"
        @click="adopt(c.file)"
      >
        <img :src="c.url" loading="lazy" alt="">
        <div class="accap">
          <span>{{ c.seed }}</span>
          <span class="spacer" />
          <span v-if="row!.adopted === c.file" class="okmark">✓</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '@/api/client'
import ImgUpload from '@/components/ImgUpload.vue'
import { assetOf, refreshAssets, refreshQueue, state } from '@/stores/app'
import { requestBudgetApproval } from '@/composables/budgetApproval'

const props = defineProps<{ kind: 'scene' | 'prop'; id: string; inline?: boolean }>()
const busy = ref(false)

const row = computed(() => assetOf(props.kind, props.id))
const n = computed(() => row.value?.n ?? 0)

/**
 * ★ 点击 = **入队**，不再同步等出图（2026-09-24 用户要求）。
 *
 * 原来这里是 `await api.assetGen(...)` —— 同步 HTTP，浏览器等 20 秒、按钮禁用，
 * 用户点完一个就没法点下一个，还得一直盯着。
 * 现在入队立即返回，drainer 顺序处理；可以连点十几个然后走开。
 *
 * 超预算时（T2 把抽卡纳入预算护栏）回 409 + 载荷 —— 弹 E3 审批框
 * 「已花 / 卡在哪 / 再放行多少」，批准后自动重试一次（requestBudgetApproval）。
 */
async function gen() {
  await enqueue()
}

async function enqueue(retried = false): Promise<void> {
  busy.value = true
  try {
    const r = await api.queueAdd(state.project, 'asset_gen', {
      kind: props.kind, id: props.id, n: 2,
    })
    ElMessage.success((r.message as string) || '已入队')
    await refreshQueue()
  } catch (e) {
    // 预算护栏：弹审批框而不是干巴巴报错；批准 = 放行一次并重试
    if (!retried && (await requestBudgetApproval(e, state.project, 'gacha')) === 'approved') {
      busy.value = false
      await enqueue(true)
      return
    }
    ElMessage.error(`入队失败：${(e as Error).message}`)
  } finally {
    busy.value = false
  }
}

async function adopt(file: string) {
  if (busy.value || row.value?.adopted === file) return
  busy.value = true
  try {
    const r = await api.assetAdopt(state.project, props.kind, props.id, file)
    ElMessage.success((r.message as string) || '已采纳')
    await refreshAssets()
  } catch (e) {
    ElMessage.error(`采纳失败：${(e as Error).message}`)
  } finally {
    busy.value = false
  }
}

/** 上传自有图 → 作为候选（和抽卡候选混在一起挑）。 */
async function upload(filename: string, b64: string) {
  const r = await api.assetUpload(state.project, props.kind, props.id, filename, b64)
  ElMessage.success((r.message as string) || '已上传')
  await refreshAssets()
}
</script>

<style scoped>
.ac { margin-top: 7px; }
.achead { display: flex; align-items: center; gap: 7px; margin-bottom: 5px; }
.acbtn {
  font: inherit; font-size: 11px; padding: 2px 9px; border-radius: 6px; cursor: pointer;
  border: 1px solid var(--line); background: var(--card); color: var(--ink);
}
.acbtn:hover:not(:disabled) { border-color: color-mix(in srgb, var(--lime) 70%, var(--ink)); }
.acbtn:disabled { opacity: .5; cursor: not-allowed; }
.acnone { line-height: 1.5; }
.acgrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(104px, 1fr)); gap: 6px; }
.accand { border: 1px solid var(--line); border-radius: 8px; overflow: hidden; cursor: pointer; background: var(--surface-2); }
.accand:hover { border-color: var(--hover-border); }
.accand.adopted { border-color: var(--lime); box-shadow: 0 0 0 2px color-mix(in srgb, var(--lime) 35%, transparent); }
.accand img { width: 100%; aspect-ratio: 16/9; object-fit: cover; display: block; }
.accap { display: flex; align-items: center; gap: 4px; padding: 2px 5px; font-size: 10px; color: var(--muted); }
.okmark { color: var(--ok); font-weight: 700; }
</style>
