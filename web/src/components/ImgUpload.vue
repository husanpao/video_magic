<!--
  图片上传按钮（角色定妆 / 场景 / 道具共用）。

  为什么单独抽出来：三处的逻辑完全一样 —— 选文件 → 读成 base64 → POST → 刷新。
  复制三份必然漂移（本项目已经吃过"同一件事在多处各写一遍"的亏）。
-->
<template>
  <button class="upbtn" :disabled="busy" :title="title" @click="pick">
    <span v-if="busy">上传中…</span>
    <span v-else>{{ label }}</span>
  </button>
  <input
    ref="fileEl"
    type="file"
    accept="image/png,image/jpeg,image/webp"
    class="upfile"
    @change="onFile"
  >
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage } from 'element-plus'

const props = withDefaults(defineProps<{
  label?: string
  title?: string
  /** 收到 (filename, base64) 后由父组件决定往哪发 */
  handler: (filename: string, b64: string) => Promise<unknown>
}>(), {
  label: '上传图片',
  title: '用你自己的图（上传后作为候选，点图即可采纳）',
})

const fileEl = ref<HTMLInputElement | null>(null)
const busy = ref(false)

function pick() { fileEl.value?.click() }

async function onFile(e: Event) {
  const f = (e.target as HTMLInputElement).files?.[0]
  if (!f) return
  // 前端拦一道：大图 base64 会很大，先给个明确上限而不是让请求超时
  if (f.size > 12 * 1024 * 1024) {
    ElMessage.error(`图片太大（${(f.size / 1048576).toFixed(1)} MB），请压到 12 MB 以内`)
    return
  }
  busy.value = true
  try {
    const b64 = await new Promise<string>((res, rej) => {
      const r = new FileReader()
      r.onload = () => res(String(r.result).split(',')[1] || '')
      r.onerror = () => rej(new Error('读取文件失败'))
      r.readAsDataURL(f)
    })
    await props.handler(f.name, b64)
  } catch (err) {
    ElMessage.error(`上传失败：${(err as Error).message}`)
  } finally {
    busy.value = false
    if (fileEl.value) fileEl.value.value = ''   // 允许重复选同一个文件
  }
}
</script>

<style scoped>
.upbtn {
  font: inherit; font-size: 11px; padding: 2px 9px; border-radius: 6px; cursor: pointer;
  border: 1px dashed color-mix(in srgb, var(--lime) 60%, var(--line));
  background: transparent; color: var(--ink);
}
.upbtn:hover:not(:disabled) { border-style: solid; background: var(--lime-pale); }
.upbtn:disabled { opacity: .5; cursor: not-allowed; }
.upfile {
  /* 必须**视觉隐藏但不能 display:none** —— display:none 的 input 在某些浏览器
     /自动化环境下点不出文件选择框（实测 Playwright 等不到 filechooser）。 */
  position: absolute; width: 1px; height: 1px; opacity: 0; pointer-events: none;
}
</style>
