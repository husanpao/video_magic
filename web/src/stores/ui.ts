import { reactive } from 'vue'

/** 详情弹层的**极简**共享状态。
 *
 *  为什么单独开一个 store：旧单文件里 `openShot()/closeShot()/detailId` 是模块级全局，
 *  ShotTable / ShotNav / Filmstrip 三处都要能"打开这一镜的详情"，而 ShotModal（详情弹层）
 *  要能读到"该显示哪一镜、开没开"。这里只放这三个字段 + 两个动作，不放别的 ——
 *  镜头数据、筛选、选中都还在 `stores/app.ts`。
 */
export const ui = reactive({
  modalOpen: false,
  detailId: '',
})

/** 打开某一镜的详情。旧文件 openShot() 要求 id 非空，这里保持一致。 */
export function openShot(id: string): void {
  if (!id) return
  ui.detailId = id
  ui.modalOpen = true
}

/** 关闭详情弹层。旧文件 closeShot() 同时清掉 detailId。 */
export function closeShot(): void {
  ui.modalOpen = false
  ui.detailId = ''
}
