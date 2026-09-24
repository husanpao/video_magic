<!--
  ② 300px 第二栏：场次分组 + 紧凑镜头导航。职责是"表格做不到的分组导航"。
  逐条对应旧 vm/static/index.html：
    · 标记（313-318）：colhead「镜头」+ #navCount + #navList
    · renderNav()（754-783）：按 sceneOf(id) 分组（Map 插入序）；组头「第 x 场 / n 镜 · ⚠坏镜数」；
      行 = 状态点 + 镜头号 + 秒数；title = 状态 + 台词前 30 字；点击 selectShot(id, false) → 只选中，不开详情
    · selectShot() 里的高亮（866-874）：.navrow.is-active 由 state.selected 驱动
  新增（规格要求）：每组可折叠（旧文件组头不能折叠）。
  注意：旧文件 selectShot(id, **false**) —— 导航栏点击**只改选中**，不打开详情弹层，
  详情由表格行 / 胶片条点击打开（见 ShotTable.vue / Filmstrip.vue）。
-->
<template>
  <section class="col">
    <div class="colhead">
      镜头
      <span class="spacer" />
      <small class="dim tabular">{{ visibleShots.length }} / {{ state.shots.length }} 镜 · {{ groups.length }} 场</small>
    </div>

    <div class="colbody">
      <div v-for="g in groups" :key="g.key || g.scene" class="navgroup-wrap">
        <button
          class="navgroup"
          :title="(collapsed.has(g.key || g.scene) ? '展开' : '折叠') + g.scene"
          @click="toggle(g.key || g.scene)"
        >
          <span class="caret">{{ collapsed.has(g.key || g.scene) ? '▸' : '▾' }}</span>
          <!-- 真场景实体显示场景名；没有实体（老数据）退回「第 x 场」并弱化 -->
          <span :class="{ 'dim': !g.real }">{{ g.real ? g.scene : `第 ${g.scene} 场` }}</span>
          <span class="spacer" />
          <span class="tabular">{{ g.list.length }} 镜<span v-if="g.bad" class="bad"> · ⚠{{ g.bad }}</span></span>
        </button>

        <template v-for="s in g.list" :key="s.id">
          <button
            v-show="!collapsed.has(g.key || g.scene)"
            class="navrow"
            :class="{ 'is-active': state.selected === s.id }"
            :data-id="s.id"
            :title="rowTitle(s)"
            @click="state.selected = s.id"
          >
            <span class="dot" :class="`tone-${kindInfo(kindOf(s)).tone}`" />
            <span class="sid">{{ s.id }}</span>
            <span class="sec">{{ s.sec }}s</span>
          </button>
        </template>
      </div>

      <div v-if="!state.shots.length" class="emptyrow">还没有镜头。</div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import type { ShotRow } from '@/api/types'
import { groupShots, kindInfo, kindOf, state, visibleShots } from '@/stores/app'

interface NavGroup {
  scene: string
  /** 是否真是场景实体（false = 退回场次号） */
  real?: boolean
  key?: string
  list: ShotRow[]
  /** 这一场里坏掉的镜数（旧 renderNav 的 bad = qc_failed | failed） */
  bad: number
}

/** 分组：**按场景实体分**（S1 山路枯林 / S2 云隐寺山门 / S3 殿后古井），
 *  没有场景实体时退回 id 里的场次号并标注「第 x 场」。
 *
 *  为什么改：旧版按 id 第二段分组，而本片 52 镜被分成了 **47 个场次号**（几乎每镜一个），
 *  分组等于没分（左栏顶着「47 场」）。场景实体才是真正的"同一个地点"。
 *  顺序仍按镜头表首次出现的顺序（旧文件的 Map 插入序语义）。 */
const groups = computed<NavGroup[]>(() =>
  groupShots(visibleShots.value).map((g) => ({
    scene: g.real ? g.name : g.name,
    real: g.real,
    key: g.key,
    list: g.items,
    bad: g.items.filter((s) => {
      const k = kindOf(s)
      return k === 'qc_failed' || k === 'failed'
    }).length,
  })),
)

/** 折叠状态。用不可变替换（整只 Set 换新）以免依赖 Set 的深响应细节。 */
const collapsed = ref<Set<string>>(new Set())
function toggle(scene: string): void {
  const next = new Set(collapsed.value)
  if (next.has(scene)) next.delete(scene)
  else next.add(scene)
  collapsed.value = next
}

function rowTitle(s: ShotRow): string {
  const t = kindInfo(kindOf(s)).t
  const line = (s.dialogue || '').slice(0, 30) || '无台词'
  return `${t} · ${line}`
}
</script>

<style scoped>
.navgroup-wrap { margin-bottom: 2px; }
.navgroup {
  display: flex; align-items: center; gap: 6px; width: 100%;
  padding: 6px 4px 2px; cursor: pointer; text-align: left;
  font: inherit; font-size: 11px; color: var(--muted);
  background: transparent; border: none;
}
.navgroup:hover { color: var(--ink); }
.navgroup .caret { width: 10px; flex: 0 0 auto; }
.navgroup .bad { color: var(--bad); }

.navrow {
  display: flex; align-items: center; gap: 6px; width: 100%;
  padding: 3px 8px; cursor: pointer; text-align: left;
  font: inherit; font-size: 12px; color: inherit;
  border: 1px solid transparent; background: transparent; border-radius: 8px;
}
.navrow:hover { background: var(--hover); }
.navrow.is-active {
  background: var(--lime); border-color: color-mix(in srgb, var(--lime) 70%, var(--ink)); font-weight: 600;
}
.navrow .sid { font-variant-numeric: tabular-nums; font-weight: 600; }
.navrow .sec { margin-left: auto; color: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }

/* .dot.tone-* 状态点样式已收编进 theme.css（U13 去重），此处不再各抄一份。 */
</style>
