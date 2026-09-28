/**
 * useTicker —— 全控制台**唯一**的定时器（F0.2）。
 *
 * 拆之前有 3 个各自为政的 `setInterval`：
 *   · `App.vue`        2s  主轮询（status + 日志 + 运行中顺带刷镜头表/当前 tab）
 *   · `QueueFab.vue`   1s  只为了把「已排队 xx 秒」这个数字往下走
 *   · `QueueFab.vue`   3s  队列状态
 *
 * 三个定时器各自 `clearInterval`，代价不只是"多两个 timer"：
 *   ① 节拍互相撞 —— 3 个请求可能在同一个 16ms 帧里发出，主轮询卡住时队列轮询也跟着抖；
 *   ② 页面切到后台它们**照跑**（本工具经常开着不动），白白打 API；
 *   ③ 新增一个轮询点就多一个 timer，没人数的清总共在打多少请求。
 *
 * 现在：一个 interval 驱动，需要周期任务的模块**注册**进来，声明自己的间隔。
 * 到点判断放在回调侧（不是每个间隔都真发请求），所以"1s 刷数字"这种
 * 与"2s 打 API"能共存于同一个心跳而不互相拖累。
 *
 * ★ 这个模块只负责"什么时候该跑一次"，**不知道**跑的是什么 ——
 *   刷新内容仍然在 `stores/*` 里，所以本模块可以独立于业务被读懂、被测到。
 */

import { onUnmounted, ref, type Ref } from 'vue'
import { errText, setError } from '@/stores/state'

interface Subscription {
  everyMs: number
  fn: () => Promise<void> | void
  lastAt: number
  /** 可选：返回 true 表示"这一拍先跳过"（如组件已折叠、不必拉列表） */
  skip?: () => boolean
}

/** 心跳粒度：所有订阅者的间隔都会**向下取整到它的倍数**，
 *  所以 1s / 2s / 3s / 4s 的订阅不会互相错开成 5 个独立节拍。 */
const HEARTBEAT_MS = 1000

let timer = 0
const subs = new Set<Subscription>()

/** 页面在后台时暂停轮询（切回来的那一拍立刻补一次）。 */
function hidden(): boolean {
  return typeof document !== 'undefined' && document.visibilityState === 'hidden'
}

/** 切回前台时置真：让下一拍无视间隔先把所有订阅跑一遍，避免"切回来看到 3 分钟前的数据"。 */
let catchUp = true

function beat(): void {
  if (hidden()) return
  const now = Date.now()
  const force = catchUp
  catchUp = false
  for (const s of subs) {
    if (!force && now - s.lastAt < Math.max(HEARTBEAT_MS, s.everyMs)) continue
    if (s.skip?.()) { s.lastAt = now; continue }
    s.lastAt = now
    try {
      void Promise.resolve(s.fn()).catch((e: unknown) => {
        // 订阅方本该自己 guard；走到这里说明是框架级意外 ——
        // 必须冒到界面错误条上。定时器静默死掉 = 控制台"冻结"在旧数据上（旧版真实事故）。
        setError(`轮询：${errText(e)}`)
      })
    } catch (e) {
      setError(`轮询：${errText(e)}`)
    }
  }
}

function start(): void {
  if (timer) return
  timer = window.setInterval(beat, HEARTBEAT_MS)
}

function stopIfIdle(): void {
  if (!subs.size && timer) {
    window.clearInterval(timer)
    timer = 0
  }
}

/**
 * 注册一个周期回调，返回**注销函数**（组件卸载时调用）。
 * 注册当拍不发请求 —— 首拉由 `onMounted` 里显式调用，和拆分前的行为一致。
 */
export function every(
  everyMs: number,
  fn: () => Promise<void> | void,
  opts: { skip?: () => boolean } = {},
): () => void {
  const s: Subscription = { everyMs, fn, lastAt: Date.now(), skip: opts.skip }
  subs.add(s)
  start()
  return () => {
    subs.delete(s)
    stopIfIdle()
  }
}

/** 当前心跳是否活着（只给测试与调试用）。 */
export function tickerRunning(): boolean {
  return timer !== 0
}

/**
 * 一个响应式的"当前时间"，每 `everyMs` 前进一次。
 * `QueueFab` 那个「已排队 xx 秒」以前是独立 timer + `ref(Date.now())`，
 * 现在走同一个心跳。
 */
export function useClock(everyMs = 1000): Ref<number> {
  const now = ref(Date.now())
  const off = every(everyMs, () => { now.value = Date.now() })
  onUnmounted(off)
  return now
}

if (typeof document !== 'undefined') {
  document.addEventListener('visibilitychange', () => {
    if (!hidden()) catchUp = true
  })
}
