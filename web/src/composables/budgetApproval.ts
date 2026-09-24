import { ElMessageBox } from 'element-plus'
import { api, ApiError } from '@/api/client'

/**
 * E3 预算审批框的**复用版**（抽卡 / 队列作业用）。
 *
 * 背景：预算护栏（T2）把抽卡/队列作业也纳入了 check()，所以「连点十几张抽卡」
 * 超预算时 `/api/queue/add` 会回 409 + 结构化载荷 —— 和 TopBar.launch 的渲染阶段
 * 是同一套协议。抽卡入口在好几处（角色定妆 / 场景 / 道具），审批框只该有一份实现：
 * **已花多少 / 卡在哪一步 / 再放行多少** 三问必须直接摆出来（一道通向不了人的告警
 * 和没有告警是一回事），批准后放行一次、自动重试原动作。
 *
 * 与 confirmAction 的分工：confirmAction 回答"这个动作做不做"，
 * 这里回答"超支了批不批"（customClass: budget-approve），两者不要合并。
 */

export type ApprovalOutcome = 'approved' | 'rejected' | 'not-budget'

/** err 是不是"超预算等人批"，是的话弹审批框并代为放行一次。
 *  返回 `approved` 后调用方应**重试一次原动作**（与 TopBar.launch 同语义）。 */
export async function requestBudgetApproval(
  e: unknown,
  project: string,
  fallbackStage = '',
): Promise<ApprovalOutcome> {
  const err = e as ApiError
  if (!err || typeof err.needsApproval !== 'boolean' || !err.needsApproval) return 'not-budget'
  const p = (err.payload || {}) as {
    message?: string; reasons?: string[]
    spent?: { runs?: number; gpu_sec?: number; llm_tokens?: number }
    est?: { gpu_min?: number; llm_tokens?: number; shots_pending?: number }
    stage?: string
  }
  const sp = p.spent || {}
  const est = p.est || {}
  const stage = p.stage || fallbackStage
  const lines = [
    `这一步要花约 ${est.gpu_min ?? '?'} 分钟 GPU / 约 ${est.llm_tokens ?? '?'} tokens`,
    `（${est.shots_pending ?? '?'} 项待处理）`,
    '',
    `已花：今天 ${((sp.gpu_sec || 0) / 60).toFixed(1)} 分钟 GPU、${sp.llm_tokens || 0} tokens（${sp.runs || 0} 次任务）`,
    `卡在：${stage || '本步'}`,
    `再放行 ${est.gpu_min ?? '?'} 分钟 GPU / ${est.llm_tokens ?? '?'} tokens 即可继续`,
    '',
    ...(p.reasons || []).map((r) => `· ${r}`),
  ].join('\n')
  try {
    await ElMessageBox.confirm(lines, '⚠ 超出预算，需要你批准', {
      confirmButtonText: '放行这一次',
      cancelButtonText: '先不跑',
      type: 'warning',
      customClass: 'budget-approve',
    })
  } catch {
    return 'rejected'
  }
  try {
    await api.approve(project, stage)
    return 'approved'
  } catch {
    return 'rejected'
  }
}
