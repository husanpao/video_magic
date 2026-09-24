import { h } from 'vue'
import { ElMessageBox } from 'element-plus'

/**
 * 统一确认交互（对应改进计划 U10）。
 *
 * 为什么要有它：之前全库三套并存 —— window.confirm（原生、不可样式化、文案常丢）、
 * ElMessageBox.confirm（样式对但各处按钮文案不一）、ResetDialog 的「先摆清单再问」。
 * 破坏性操作的确认必须**说清后果**，所以这里把它收敛成一个出口：
 *
 *   const ok = await confirmAction({ title: '删除镜头', message: `删除 ${id}？`, list: ids, danger: true })
 *   if (!ok) return
 *
 * 约定：
 * - **永不 reject** —— 取消/按 Esc 一律 resolve(false)。这是 window.confirm 的语义，
 *   调用方不用 try/catch 包一层（ElMessageBox.confirm 取消会抛，直接换掉会踩坑）。
 * - 「先摆清单再确认」（ResetDialog 的安全设计）推广给删除/合并：传 `list` 会把
 *   涉及的对象逐条摆出来 —— 不知道会删什么的确认等于没问。
 * - 危险操作用 `danger`，确认按钮变红（destructive 的视觉签名）。
 *
 * ⚠️ 与 E3 预算审批框（customClass: budget-approve）是**两个东西**：
 * 预算框回答"超支了批不批"，这里回答"这个动作做不做"，不要合并。
 */

export interface ConfirmActionOptions {
  /** 标题，例如「删除镜头」 */
  title: string
  /** 一句话说清后果（支持 \n） */
  message: string
  /** 先摆清单再确认：涉及的镜头号 / 文件名等，会逐条列出（超过 12 条折叠） */
  list?: string[]
  /** 确认按钮文案，默认「确定」 */
  confirmText?: string
  /** 取消按钮文案，默认「取消」 */
  cancelText?: string
  /** 危险操作：确认按钮变红 */
  danger?: boolean
}

/** 清单渲染：≤12 条逐条摆，多了折叠成「… 等共 N 个」—— 列 52 行的确认框没人看。 */
function listLines(items: string[], cap = 12): string[] {
  const head = items.slice(0, cap).map((x) => `· ${x}`)
  if (items.length > cap) head.push(`· … 等共 ${items.length} 个`)
  return head
}

export async function confirmAction(opts: ConfirmActionOptions): Promise<boolean> {
  const { title, message, list, confirmText = '确定', cancelText = '取消', danger = false } = opts
  // 消息体用 vnode 拼：message 与清单分行展示，清单保持等宽对齐（id 列表才对得齐）
  const body = h('div', { class: 'confirm-action-body' }, [
    h('div', { style: 'white-space:pre-wrap;line-height:1.6' }, message),
    ...(list && list.length
      ? [h('div', { class: 'confirm-action-list', style: 'margin-top:8px;font-size:11.5px;line-height:1.7' }, listLines(list).join('\n'))]
      : []),
  ])
  try {
    await ElMessageBox.confirm(body, title, {
      confirmButtonText: confirmText,
      cancelButtonText: cancelText,
      type: danger ? 'warning' : 'info',
      // 自定义类名：方便测试定位，也和 budget-approve（E3 审批）区分开
      customClass: danger ? 'confirm-action confirm-action-danger' : 'confirm-action',
      // 确认按钮跟随 danger 变红；info 场景保持中性（Element Plus 默认即可）
      ...(danger ? { confirmButtonClass: 'el-button--danger' } : {}),
    })
    return true
  } catch {
    return false
  }
}
