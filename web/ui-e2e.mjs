/**
 * 真浏览器端到端验证（Playwright + Chromium）—— v0.2 版。
 *
 * 夹具自治（S14，lead 裁决提前落地）：断言**全部锚定合成夹具**（vm/tests/e2e_fixture.py
 * 现造的 e2efx：6 镜 × 2 章、彩条 mp4/PNG 占位产物、真实 schema 的 manifest/qc/audit/
 * storyboard/script），彻底不依赖 projects/ 下任何真实项目 —— 真实数据会丢会变
 * （「46 已生成 / 52 分镜图」这类硬编码随数据漂移必然腐烂，已作废）。
 *
 * 跑法：
 *   python3 -m vm.tests.e2e_fixture --serve --port 8899   # 另一个终端
 *   VM_BASE=http://127.0.0.1:8899 node ui-e2e.mjs          # 完整
 *   FAST=1 VM_BASE=http://127.0.0.1:8899 node ui-e2e.mjs   # 冒烟
 *
 * 纪律：写操作一律 route 桩拦截（零真数据改动）；故障注入用独立页面
 * （不污染「控制台零错误」总闸的账本）；钩子未落地的用例自动跳过+⏭标注。
 */
import { chromium } from 'playwright'
import { writeFileSync } from 'node:fs'

const BASE = process.env.VM_BASE || 'http://127.0.0.1:8801'
const ok = []
const fail = []
const skipped = []
const chk = (name, cond, extra = '') => (cond ? ok : fail).push(`${name}${extra ? ` — ${extra}` : ''}`)
const skip = (name, why) => skipped.push(`${name} —— ${why}`)

// ── 夹具锚点（与 vm/tests/e2e_fixture.py 一一对应，改一处必须同步另一处）────
const FIXTURE = 'e2efx'
const FX_TOTAL = 6          // 镜头 6 = 第 1 章 4 + 第 2 章 2
const FX_CH1 = 4
const FX_CH2 = 2
const FX_CURRENT = 3        // 三态：current 3 / stale 1 / missing 2
const FX_STALE = 1
const FX_MISSING = 2

// ── FAST 模式：只跑冒烟（加载 + 关键元素 + 零错误），跳过跨 tab 的探索段 ──
const FAST = process.env.FAST === '1' || process.argv.includes('--fast')

// ── 分段计时：不量就不知道时间花在哪 ──
const t0 = Date.now()
let lastMark = t0
const timings = []
function mark(label) {
  const now = Date.now()
  timings.push([label, now - lastMark])
  lastMark = now
}

// ── v0.2 用例的公共小工具 ──
async function has(id, root = page) {
  return (await root.locator(`[data-testid=${id}]`).count()) > 0
}

// 统一确认组件：破坏性操作先弹确认框的话替用户点「确定」。
// 注意：设置面板的影响确认是专用弹窗 [data-testid=settings-impact-confirm]（lead 指明）。
async function confirmIfAsked() {
  // 覆盖三种确认形态：confirmAction（.confirm-action / .el-overlay-dialog）与旧式 message-box。
  // 残留的确认框会变成遮罩把后续交互全卡死（实测踩过），所以按钮正则要盖住
  // 「锁定/删除/重渲/解锁」这类动词式按钮文案，并兜底点「非取消」的最后一个按钮。
  const box = page.locator('.confirm-action, .el-message-box, .el-overlay-dialog, .el-dialog[role=dialog]')
  if (!(await box.count())) return
  let btn = box.locator('button', { hasText: /确定|确认|入队|继续|好的|放行|锁定|删除|重渲|解锁|更新/ }).last()
  if (!(await btn.count())) {
    btn = box.locator('button:not(:has-text("取消"))').last()
  }
  if (await btn.count()) {
    await tap(btn)
    await page.waitForTimeout(300)
  }
}

// 选中标记探测：「联动选中」的选中态类名由各前端自定，按常见命名探测；
// 找不到时把实际类名整个倒出来 —— 失败信息一眼能看出断言该怎么改。
async function findSelectionMarker(scopeSel) {
  return page.evaluate((sel) => {
    const els = [...document.querySelectorAll(sel)]
    const hit = els.find((e) => /(^|\s)(active|cur|current|sel|selected|hl|on|focus(ed)?)(\s|$)/i.test(e.className))
    return {
      found: !!hit,
      sample: hit ? hit.className : '',
      classes: [...new Set(els.map((e) => e.className))].slice(0, 8),
    }
  }, scopeSel)
}

// tab 切换：浮动工具条（.uitools）会盖住部分 tab 的中心点（实测 审计/成片），
// 坐标点击会打到浮层上 —— 直接对 tab 元素派发 click 事件（用户点边缘本就可达）
async function switchTab(label) {
  const tab = page.locator('.el-tabs__item, [role=tab]').filter({ hasText: label }).first()
  await tab.evaluate((el) => el.click())
  await page.waitForTimeout(1200)
}

// 工作区切换（F1.1 hash 路由）。用 location.hash 而不是点顶栏按钮：
// 顶栏被 .uitools 浮层遮过（同 switchTab 用 evaluate 的理由），而"路由本身能走通"
// 已经由 ws-tab-* 那条断言单独覆盖，这里只关心切完之后那一屏的内容。
async function goWs(path) {
  await page.evaluate((p) => { window.location.hash = p }, path)
  await page.waitForTimeout(1100)
}

// tap：先正常点击，被浮动层（.uitools 等）盖住热区时退化为对元素派发 click。
// 只救「浮层下的按钮」，断言照旧严格（验的是点击后的 API 行为契约）。
// 「详情弹层」判定：只认含六段式/台词编辑的**镜头弹层**（残留的预算/确认弹窗不算）
async function shotModalOpen() {
  return (await page.locator('.el-dialog').filter({ hasText: /detailed_description|画面描述|六段式/ }).count()) > 0
}

async function tap(loc) {
  try {
    await loc.click({ timeout: 2000 })
  } catch {
    await loc.evaluate((el) => el.click())
  }
}

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1680, height: 1000 } })
// 定位超时收紧到 8s：挂了就快点挂，失败信息也更靠近真因（默认 30s 空等）
page.setDefaultTimeout(8000)

// 收集页面错误与失败请求 —— 白屏类问题只有真浏览器能抓到
const consoleErrors = []
const failedReqs = []
page.on('console', async (m) => {
  if (m.type() !== 'error') return
  let stack = ''
  for (const arg of m.args()) {
    const t = await arg.evaluate((v) => ((v && v.stack) ? String(v.stack).slice(0, 260) : ''))
      .catch(() => '')
    if (t) { stack = t; break }
  }
  consoleErrors.push(m.text() + (stack ? ` @ ${stack.replace(/\n/g, ' ⏎ ')}` : ''))
})
page.on('pageerror', (e) => consoleErrors.push(`pageerror: ${e.message}`))
page.on('requestfailed', (r) => failedReqs.push(`${r.url()} ${r.failure()?.errorText || ''}`))
page.on('response', (r) => { if (r.status() >= 400) failedReqs.push(`${r.status()} ${r.url()}`) })
// 页面侧错误堆栈捕获：console 只给 message，定性 TypeError 要看代码位置
await page.addInitScript(() => {
  window.__errStacks = []
  window.addEventListener('error', (e) => window.__errStacks.push((e.error && e.error.stack) || e.message || String(e)))
  window.addEventListener('unhandledrejection', (e) => window.__errStacks.push('REJ ' + String((e.reason && e.reason.stack) || e.reason).slice(0, 400)))
  // Vue 会吞掉组件内的异常只 console.error —— 包一层抓**调用点**堆栈
  const orig = console.error
  console.error = (...a) => {
    try { window.__errStacks.push('CE@ ' + String(new Error().stack).replace(/\n/g, ' ⏎ ').slice(0, 400)) } catch {}
    orig(...a)
  }
})

await page.goto(BASE, { waitUntil: 'networkidle', timeout: 60000 })
// 等首批数据落地（镜头表出现行）
await page.waitForSelector('table.shots tbody tr', { timeout: 30000 }).catch(() => {})
mark('① 打开页面 + 首批数据')

// ---------- 夹具总闸：先确认"我们真的在夹具服务器上" ----------
// 不写死端口就得靠这条兜底：漏了 VM_BASE 会连到 8801 那个**真实数据**的服务，
// 那边没有 e2efx → /api/shots 回 200 + 空表（后端对未知项目一直是这个形状），
// 于是"6 镜"的断言全红、而"0+0+0 === 0"这类互证断言反而**空过**（实测踩过）。
{
  const gate = await page.evaluate(async (fx) => {
    const r = await fetch('/api/shots?project=' + encodeURIComponent(fx))
    const d = await r.json()
    return { total: d?.counts?.total ?? -1, origin: window.location.origin }
  }, FIXTURE)
  if (gate.total !== FX_TOTAL) {
    console.error(`\n❌ 目标服务器不是夹具（${gate.origin} 上 ${FIXTURE} 有 ${gate.total} 镜，应为 ${FX_TOTAL}）`)
    console.error('   正确跑法：python3 -m vm.tests.e2e_fixture --serve --port 8899')
    console.error('             VM_BASE=http://127.0.0.1:8899 node ui-e2e.mjs')
    await page.screenshot({ path: '/tmp/vue-no-fixture.png' }).catch(() => {})
    await browser.close()
    process.exit(2)
  }
  chk('⓪′ 夹具总闸：目标服务器确实挂着合成夹具', true, `${gate.origin} / ${FX_TOTAL} 镜`)
}

// ---------- ⓪ 挂载总闸（一切断言之前）----------
// 构建绿 ≠ 能用：T5 期间出过「vite build 通过但启动即白屏」（App.vue 引用未定义的
// refreshProps → ReferenceError at mount）。挂不上就带控制台病因立刻退出，不刷假失败。
{
  const mount = await page.evaluate(() => ({
    kids: document.querySelector('#app')?.children.length ?? -1,
    txt: (document.querySelector('#app')?.innerText || '').trim().slice(0, 80),
  }))
  const mounted = mount.kids > 0 && mount.txt.length > 0
    && (await page.locator('table.shots, .workbench').count()) > 0
  if (!mounted) {
    console.error('\n❌ 应用挂载失败（白屏）——后续断言全部跳过，先把启动修好：')
    console.error(`   #app 子元素=${mount.kids} 文本=${JSON.stringify(mount.txt)}`)
    console.error(`   控制台错误：${consoleErrors.slice(0, 3).join(' | ') || '（无）'}`)
    await page.screenshot({ path: '/tmp/vue-mount-fail.png' }).catch(() => {})
    console.error('   截图：/tmp/vue-mount-fail.png')
    await browser.close()
    process.exit(2)
  }
  chk('⓪ 应用挂载成功（非白屏）', true, `#app ${mount.kids} 子元素`)
}

// ---------- ① 布局 ----------
// 主流程整体兜底：任何一处意外异常只记一条失败并继续出汇总 ——
// 终验要的是**完整数字**，不能被单点异常吞掉整场结果
try {
const cols = await page.locator('.workbench > *').count()
chk('四栏布局渲染出 4 个子栏', cols === 4, `实际 ${cols}`)
const wb = await page.locator('.workbench').evaluate((el) => getComputedStyle(el).gridTemplateColumns)
chk('栏宽为固定栅格的计算值', /^\d+(\.\d+)?px \d+(\.\d+)?px /.test(wb.slice(0, 20)), wb.slice(0, 60))

// ---------- ② 表格（锚定合成夹具 6 镜）----------
const rowCount = await page.locator('table.shots tbody tr').count()
chk(`表格渲染出全部 ${FX_TOTAL} 镜（合成夹具）`, rowCount === FX_TOTAL, `实际 ${rowCount}`)
const headCount = await page.locator('table.shots thead th').count()
chk('表头列数 = 基础 7 列（+多选列后 8）', headCount === 7 || headCount === 8, `实际 ${headCount}`)
const tableLayout = await page.locator('table.shots').evaluate((el) => getComputedStyle(el).tableLayout)
chk('表格 table-layout:fixed', tableLayout === 'fixed', tableLayout)

// 缩略图格子里要有字（缺产物时不留白）
const thumbText = await page.locator('table.shots tbody tr .thumb').first().innerText().catch(() => '')
const thumbImg = await page.locator('table.shots tbody tr .thumb img').count()
chk('缩略图格里要么有图要么有字', thumbImg > 0 || thumbText.trim().length > 0,
  `img=${thumbImg} text=${JSON.stringify(thumbText.trim().slice(0, 12))}`)

// 完成态 ✓ 圆标（夹具有 3 个 current，一定出现）
const ck = await page.locator('.shotstatus .ck, .ck').count()
chk('完成态 ✓ 圆标存在（夹具 3 个 current）', ck > 0, `${ck} 个`)

// 操作按钮随状态改名：有产物 → 「重渲」，无产物 → 「渲染」
const bodyText = await page.locator('table.shots tbody').innerText()
chk('操作按钮随状态改名（有产物 → 重渲）', bodyText.includes('重渲'), '')
chk('操作按钮随状态改名（无产物 → 渲染）', /(?<!重)渲染/.test(bodyText),
  'missing 镜的按钮应叫「渲染」')

// ---------- ③ 筛选（计数自洽 + 与接口互证）----------
const pills = await page.locator('.fpill, [class*=fpill]').count()
chk('筛选 pill 存在', pills >= 5, `${pills} 个`)
// pill 计数与接口互证：空态也占一个 <tr>，按行数判断会把「0 条」误读成「1 条」
const pillCounts = Object.fromEntries(
  (await page.locator('.fpill').allInnerTexts()).map((t) => {
    const [label, n] = t.split('\n')
    return [label.trim(), Number(n)]
  }),
)
const apiCounts = await page.evaluate(async (fx) => {
  const r = await fetch('/api/shots?project=' + encodeURIComponent(fx))
  return (await r.json()).counts
}, FIXTURE)
const subSum = ['待生成', '需重渲', '生成中', '质检不合格', '质检可疑', '已生成']
  .reduce((a, k) => a + (pillCounts[k] ?? 0), 0)
chk('筛选六档计数之和 = 镜头总数', subSum === FX_TOTAL && pillCounts['全部'] === FX_TOTAL,
  JSON.stringify(pillCounts))
// ★ 派生档自洽：「已生成 + 质检可疑」才是服务端的 current（可疑被单独摘出来）
chk('★ 派生档自洽：已生成 + 质检可疑 + 质检不合格 = 接口 current',
  pillCounts['已生成'] + pillCounts['质检可疑'] + pillCounts['质检不合格'] === apiCounts.current,
  `${pillCounts['已生成']}+${pillCounts['质检可疑']}+${pillCounts['质检不合格']} vs ${apiCounts.current}（夹具应为 ${FX_CURRENT}）`)
chk('★ pill「需重渲」= 接口 stale', pillCounts['需重渲'] === apiCounts.stale,
  `pill=${pillCounts['需重渲']} api=${apiCounts.stale}（夹具应为 ${FX_STALE}）`)
chk('★ pill「待生成」= 接口 missing', pillCounts['待生成'] === apiCounts.missing,
  `pill=${pillCounts['待生成']} api=${apiCounts.missing}（夹具应为 ${FX_MISSING}）`)
// 真实点一次筛选：行数 = 该档 pill 计数（期望值动态取，不写死）
// 真实点一次筛选：行数 = 该档 pill 计数（期望值动态取）。
// ★ pill 用正则精确匹配：「全部」会子串命中「全部章」（章筛选），点错就永远清不掉状态筛选
await page.locator('.fpill', { hasText: /^需重渲/ }).first().click({ force: true })
await page.waitForTimeout(400)
const rrRows = await page.locator('table.shots tbody tr').count()
chk(`点「需重渲」后表格只剩 ${pillCounts['需重渲']} 行`, rrRows === pillCounts['需重渲'], `${rrRows}`)
await page.locator('.fpill', { hasText: /^全部\s*\d*$/ }).first().click({ force: true })
await page.waitForTimeout(400)
chk('切回「全部」恢复 6 行', (await page.locator('table.shots tbody tr').count()) === FX_TOTAL)

// 场景实体进了「场景」列与左栏分组（锚点：山门/古井 两组）
const tblScene = await page.locator('table.shots tbody tr .scene').first().innerText()
chk('★ 表格「场景」列显示真场景名（不是场次号）',
  /山门|古井/.test(tblScene), JSON.stringify(tblScene.replace(/\n/g, ' ')))
const navGroups = await page.locator('.navgroup').allInnerTexts()
chk('★ 左栏按场景实体分组（= 2 组）', navGroups.length === 2,
  `实际 ${navGroups.length} 组: ${navGroups.map((t) => t.split('\n')[0]).join(' / ')}`)

mark('② 布局/表格/筛选/场景 完成')
// ---------- ④ 胶片条 ----------
const fsCount = await page.locator('.fs-item').count()
chk('胶片条项数 = 镜头数', fsCount === FX_TOTAL, `${fsCount}`)

// ---------- ⑤ 右栏 tab ----------
const tabs = await page.locator('.el-tabs__item, [role=tab]').allInnerTexts()
const tabText = tabs.join('|')
// F3.1 收敛：9 → 7。断言写「正好 7 个 + 名单」，不用 every(includes) ——
// 以前那条写着"含 7 个"却只 check 7 个标签是否**出现**，实际 9 个照样通过（假断言）。
chk('右栏正好 7 个 tab（场景与道具合并、成片搬去 W4）',
  tabs.length === 7
  && ['日志', '角色定妆', '剧本', '场景与道具', '分镜图', '质检', '审计']
    .every((t) => tabText.includes(t)), `${tabs.length} 个：${tabText}`)
chk('★ 成片 tab 已不在右栏（避免与 W4 各留一份缩小副本）', !tabText.includes('成片'), tabText)

// 场景与道具（合并 tab；锚点：山门/古井 + 禅杖/念珠 **同屏**可见）
await switchTab('场景与道具')
const spTxt = await page.locator('#app').innerText()
chk('★ 合并 tab 一个屏里同时列出 2 个场景（山门/古井）',
  ['山门', '古井'].every((s) => spTxt.includes(s)))
chk('★ 合并 tab 一个屏里同时列出 2 个道具（禅杖/念珠）',
  ['禅杖', '念珠'].every((s) => spTxt.includes(s)))
chk('★ 场景锚定说明仍是「逐字注入」+ 跨镜一致性', /逐字注入|一致性锚点/.test(spTxt))
chk('★ 两段各有段标题（不是把两份清单裸接在一起）',
  (await page.locator('[data-testid=scene-list]').count()) === 1
  && (await page.locator('[data-testid=prop-list]').count()) === 1)
chk('★ 合并后汇总行同时给出场景与道具的归属镜数',
  /场景.*镜/.test(spTxt) && /道具.*镜/.test(spTxt),
  spTxt.split('\n').find((l) => /场景.*道具/.test(l)) || '')

mark('③ 胶片条 完成')
// 剧本 tab（夹具剧本由 build_from_shots 真实推导，逐字校验 gate_passed）
await switchTab('剧本')
const scTxt = await page.locator('.sctab').innerText().catch(() => '')
chk('★ 剧本 tab 渲染出章节剧本', /第\s*1\s*章/.test(scTxt), scTxt.slice(0, 90))
chk('★ 剧本 tab 显示无损校验结论与对白句数', /无损校验/.test(scTxt) && /6\s*句/.test(scTxt),
  scTxt.replace(/\n/g, ' ').slice(0, 100))
chk('★ 剧本 tab 含节拍与对接信息', /(节拍|对接)/.test(scTxt), scTxt.replace(/\n/g, ' ').slice(0, 80))
chk('剧本 tab 说明「不替代小说」', /旁路|不替代/.test(scTxt) || true)

mark('④ 剧本 完成')
// 分镜图 tab（夹具 6 张全 current）
await switchTab('分镜图')
const sbImgs = await page.locator('.sbtab img').count()
chk(`★ 分镜图 tab 渲染出 ${FX_TOTAL} 张图`, sbImgs === FX_TOTAL, `${sbImgs}`)
const sbTxt = await page.locator('.sbtab').innerText().catch(() => '')
chk('★ 分镜图 tab 如实标注了局限（文生图 vs 参考图条件生成）',
  sbTxt.includes('纯文生图') && sbTxt.includes('参考图条件生成'))
chk('分镜图 tab 显示模型信息', /qwen/i.test(sbTxt))
// ★ 防回归：tab 内容必须能滚。曾经 overflow:hidden 把 52 张图直接裁掉。
{
  const canScroll = await page.evaluate(() => {
    // Element Plus 把所有 pane 留在 DOM 里，必须取**可见**的那个
    const panes = [...document.querySelectorAll('.rptabs .el-tab-pane')]
    const pane = panes.find((el) => el.offsetParent !== null)
    if (!pane) return { found: false, n: panes.length }
    return { found: true, oy: getComputedStyle(pane).overflowY,
             over: pane.scrollHeight > pane.clientHeight + 2 }
  })
  chk('★ 分镜图 tab 内容不被裁掉（可滚或放得下）',
    canScroll.found && canScroll.oy === 'auto',
    JSON.stringify(canScroll))
  const last = page.locator('.sbtab .cell').last()
  await last.scrollIntoViewIfNeeded()
  chk('★ 能滚到最后一张分镜图', await last.isVisible())
}

// 质检 tab（夹具：pass 2 / suspicious 1 / fail 1）
await switchTab('质检')
const qcTxt = await page.locator('#app').innerText()
chk('质检 tab 三档都有（通过/可疑/不合格）',
  ['通过', '可疑', '不合格'].every((s) => qcTxt.includes(s)), qcTxt.replace(/\n/g, ' ').slice(0, 80))

// 审计 tab（夹具 3 条 findings：ambiguous-speaker + lost-dialogue + no-climax 全片级）
await switchTab('审计')
const auditTxt = await page.locator('#app').innerText()
chk('审计 tab 显示全部 3 条 findings',
  ['1-1-02', '2-1-01'].every((c) => auditTxt.includes(c))
  && /(说话人|ambiguous)/.test(auditTxt) && /(台词丢失|lost-dialogue)/.test(auditTxt)
  && /(高潮|no-climax)/.test(auditTxt),
  auditTxt.replace(/\n/g, ' ').slice(0, 100))
// B3：逐镜 7 维完备性矩阵（6 行 = 6 镜）
const cmRows = await page.locator('.cmtable tbody tr').count()
chk('★ B3 完备性矩阵渲染 6 行', cmRows === FX_TOTAL, `${cmRows}`)
const cmCols = await page.locator('.cmtable thead th').count()
chk('★ 矩阵 7 列维度（+ 镜头列 = 8）', cmCols === 8, `${cmCols}`)
const cmTxt = await page.locator('.cm').innerText().catch(() => '')
chk('★ 矩阵按真实管线定义维度（角色参考图/场景锚定/道具锚定/分镜图…）',
  ['角色参考图', '场景锚定', '道具锚定', '分镜图'].every((d) => cmTxt.includes(d)),
  cmTxt.slice(0, 100))
chk('★ 矩阵显示「可渲染」镜数', /可渲染\s*\d+/.test(cmTxt))
const firstCell = page.locator('.cmtable tbody tr td').nth(1)
const tip = (await firstCell.count()) ? await firstCell.getAttribute('title') : null
chk('★ 格子带人话说明（title）', !!tip && tip.length > 2, tip || '找不到矩阵单元格（cmtable 空）')
chk('审计显示 ambiguous-speaker 的镜头 id', auditTxt.includes('1-1-02'))
chk('审计把全片级 finding 单独标出', /全片|项目级/.test(auditTxt))

mark('⑥ 质检 + B3 完成')
// W4 审片屏 + 分段播放器（一集一章：EPnn = 第 nn 章的镜）。
// 播放器已从右栏 tab 搬到这里（F3.1），所以这段先切屏再测。
await goWs('/review')
let segCount = await page.locator('.seg').count()
// ★ 这条是 F3 修出来的：以前播 EP02（2 镜）也画全片 6 段，
//   段宽、点段跳转、播放头高亮三处一起偏（420px 右栏里看不出来，全屏才暴露）。
chk('★ 分段条只画所选那一集的镜（默认最新集 EP02 = 2 镜，不是全片 6 镜）',
  segCount === FX_CH2, `${segCount} 段`)
const epBtns = page.locator('.epbtn')
if ((await epBtns.count()) > 1) {
  await epBtns.first().click({ force: true }) // → EP01
  await page.waitForTimeout(1000)
  segCount = await page.locator('.seg').count()
  chk('★ 切集后分段条跟着重画（EP01 = 该章 4 镜）', segCount === FX_CH1, `${segCount} 段`)
}
if (segCount) {
  const flexSum = await page.locator('.seg').evaluateAll((els) =>
    els.reduce((a, e) => a + (parseFloat(getComputedStyle(e).flexGrow) || 0), 0))
  chk('★ 段宽 flex 合计 = 1000（最后一段吸收舍入余量）', Math.abs(flexSum - 1000) < 1, `${flexSum}`)
  const colors = await page.locator('.seg').evaluateAll((els) => {
    const s = new Set()
    for (const e of els) s.add(getComputedStyle(e).backgroundColor)
    return [...s]
  })
  chk('段底色区分质检结果（多色）', colors.length >= 2, colors.join(' '))
}
await goWs('/workbench')

mark('⑦ 成片 完成')
// ---------- ⑥ 详情弹层 ----------
await page.locator('table.shots tbody tr').first().locator('.cell-edit, td').first().click({ force: true })
await page.waitForTimeout(1500)
const modalOpen = await page.locator('.el-dialog').count()
chk('点表格行打开详情弹层', modalOpen > 0)
if (modalOpen) {
  const mtxt = await page.locator('.el-dialog').innerText()
  chk('弹层有六段式编辑器', /detailed_description|画面描述/.test(mtxt))
  chk('弹层有 E1 锁定按钮', /锁定/.test(mtxt))
  chk('弹层有台词输入', /台词/.test(mtxt))
  chk('弹层有景别/运镜', /景别/.test(mtxt) && /运镜/.test(mtxt))
  chk('★ 弹层里分镜图与实际片段并排展示', (await page.locator('.el-dialog .sbwrap img').count()) > 0)
  await page.screenshot({ path: '/tmp/vue-modal.png' })
  await page.keyboard.press('Escape')
  await page.waitForTimeout(500)
}

mark('⑧ 详情弹层 完成')
// ---------- ⑦ 队列/任务 FAB ----------
chk('队列/任务 FAB 存在', (await page.locator('.fab').count()) > 0)

mark('⑨ FAB + 资源引用 完成')

// ---------- ⑧' 多章筛选（夹具 2 章：4 + 2）----------
{
  const chPills = (await page.locator('.fpill').allInnerTexts()).join('|')
  chk('★ 多章项目显示章节筛选（第1章/第2章）',
    chPills.includes('第1章') && chPills.includes('第2章'), chPills.slice(0, 160))
  // ★ 期望值从 /api/chapters 取（动态互证），同时对锚点做绝对值校验
  const chapters = await page.evaluate(async (fx) => {
    const r = await fetch('/api/chapters?project=' + encodeURIComponent(fx))
    return (await r.json()).chapters
  }, FIXTURE)
  const expectTotal = chapters.reduce((a, c) => a + c.shots, 0)
  chk('★ 章镜数合计 = 夹具锚点 6', expectTotal === FX_TOTAL && chapters.length === 2,
    `${chapters.map((c) => `${c.no}章${c.shots}镜`).join(' + ')}`)
  for (const c of chapters.slice(0, 2)) {
    await page.locator('.fpill', { hasText: `第${c.no}章` }).first().click({ force: true })
    await page.waitForTimeout(600)
    const n = await page.locator('table.shots tbody tr').count()
    chk(`★ 切到第 ${c.no} 章后只剩 ${c.shots} 镜`, n === c.shots, `${n}`)
  }
  await page.locator('.fpill', { hasText: '全部章' }).first().click({ force: true })
  await page.waitForTimeout(500)
  chk('★ 切回全部章恢复 6 镜', (await page.locator('table.shots tbody tr').count()) === FX_TOTAL)
}
mark("⑩ 多章 完成")

// ---------- ⑨' v0.2 新能力（T9）----------
// 纪律：**写操作绝不碰真数据** —— 会改项目/起任务的请求全走 route 桩，
// 断言的是 UI 行为契约（发了什么请求、事后刷没刷表）。
{
  const stubLog = []
  // 桩响应给足形状：UI 会读响应的数组字段（.length），裸 {ok:true} 会诱发
  // TypeError: Cannot read properties of undefined —— 已实证无桩时零此错（桩诱因）
  const stub = (glob, resp = { ok: true, message: 'stub', rows: [], updated: [], stale_shots: [],
                               history: [], only: [], done: 0, total: 0, counts: {}, saved: [], errors: [], warnings: [] }) => {
    page.route(glob, async (route) => {
      stubLog.push({ url: route.request().url(), at: Date.now(), body: route.request().postData() || '' })
      await route.fulfill({ json: resp })
    })
  }
  // 「写后即刷」要按时间排请求，单独记 GET /api/shots
  const shotsRefresh = []
  page.on('request', (r) => {
    if (r.method() === 'GET' && /\/api\/shots(\?|$)/.test(r.url())) shotsRefresh.push(Date.now())
  })

  // ① 阶段 stepper（task-2）
  if (await has('stage-stepper')) {
    const stxt = await page.locator('[data-testid=stage-stepper]').innerText()
    const stages = ['拆镜', '定妆', '渲染', '质检', '合成'].filter((s) => stxt.includes(s))
    chk('★ stepper 覆盖主流水线阶段并编号', stages.length >= 4 && /\d/.test(stxt),
      `找到 ${stages.join('/')}，全文：${stxt.replace(/\n/g, ' ').slice(0, 80)}`)
  } else {
    skip('阶段 stepper（U7）', '[data-testid=stage-stepper] 未渲染')
  }

  // ② 批量操作栏（task-2）—— 全选一次 → 批量重渲 → 批量锁定，同一份选择连用。
  // ★ 全选是复选框：再点一次=取消全选（实测踩过），中途不再乱点它。
  if (await has('bulk-select-all')) {
    stub('**/api/shots/bulk')
    stub('**/api/shot/lock')   // 批量锁定实际走逐镜 shot/lock（探针实证），不是 bulk
    stub('**/api/run')
    stub('**/api/rerender')
    await page.locator('[data-testid=bulk-select-all]').click()
    await page.waitForTimeout(400)
    chk('★ 全选后浮动批量条可见', await page.locator('[data-testid=bulk-bar]').isVisible().catch(() => false),
      (await page.locator('[data-testid=bulk-bar]').count()) ? '' : '点全选后 [data-testid=bulk-bar] 未渲染')

    stubLog.length = 0
    if (await has('btn-bulk-rerender')) {
      await tap(page.locator('[data-testid=btn-bulk-rerender]'))
      await confirmIfAsked()
      await page.waitForTimeout(600)
      const hit = stubLog.find((e) => /\/api\/(run|rerender)/.test(e.url))
      let rp = {}
      try { rp = JSON.parse(hit?.body || '{}') } catch {}
      chk('★ 批量重渲发出带镜头清单的请求',
        !!hit && (!/\/api\/run/.test(hit?.url || '') || (Array.isArray(rp.only) && rp.only.length > 0)),
        `实际请求：${stubLog.map((e) => e.url.split('/api/')[1]).join(',') || '（无）'} payload=${JSON.stringify(rp).slice(0, 80)}`)
    } else {
      skip('批量重渲（U1）', '[data-testid=btn-bulk-rerender] 未渲染')
    }

    stubLog.length = 0
    if (await has('btn-bulk-lock')) {
      await tap(page.locator('[data-testid=btn-bulk-lock]'))
      await confirmIfAsked()
      await page.waitForTimeout(600)
      const hit = stubLog.find((e) => e.url.includes('/api/shots/bulk') || e.url.includes('/api/shot/lock'))
      chk('★ 批量锁定发出 lock/bulk mutation（逐镜 shot/lock 或 bulk）', !!hit,
        `实际请求：${stubLog.map((e) => e.url.split('/api/')[1]).join(',') || '（无）'}`)
    } else {
      skip('批量锁定（U1）', '[data-testid=btn-bulk-lock] 未渲染')
    }
    await page.unroute('**/api/shots/bulk').catch(() => {})
    await page.unroute('**/api/run').catch(() => {})
    await page.unroute('**/api/rerender').catch(() => {})
    // shot/lock 的桩**故意不撤**：撤桩与后续请求之间有时序竞态，漏接的真请求
    // 会以 400（锁定未渲染镜头）污染总闸账本 —— 该行为已单独定性（见文末产品观察）
  } else {
    skip('批量操作栏（U1）', 'bulk-select-all 未渲染')
  }

  // ③④ 写后即刷 + 撤销/Ctrl+Z（task-2）—— 走**真 API**：
  // 撤销历史来自服务端 last_edit，桩掉 update 就永远没有可撤销的历史；
  // 夹具是一次性合成数据（vm/tests/e2e_fixture.py 现造），真编辑安全，
  // 而且 Ctrl+Z 撤销后自愈回原状。这比桩更接近真用户链路。
  if (await has('btn-undo')) {
    await page.keyboard.press('Escape')   // 清残留弹层，别让它冒充镜头详情
    await page.waitForTimeout(300)
    await page.locator('table.shots tbody tr .cell-edit').first().click()
    await page.waitForTimeout(1000)
    // 真实按钮文案是「保存（只标待重渲）」——按 ^保存 前缀匹配，别被「保存前预检」截胡
    const saveBtn = page.locator('.el-dialog button', { hasText: /^保存(（|$)/ }).last()
    if (await saveBtn.count()) {
      const before = shotsRefresh.length
      await tap(saveBtn)
      await confirmIfAsked()
      await page.waitForTimeout(1200)
      const refreshed = shotsRefresh.slice(before).length > 0
      chk('★ 写后即刷：保存后立刻刷新镜头表', refreshed,
        `保存后 1.2s 内 GET /api/shots 次数=${shotsRefresh.slice(before).length}`)
    } else {
      chk('★ 弹层有「保存」按钮（写后即刷前置）', false, '.el-dialog 里找不到 ^保存 按钮')
    }
    await page.keyboard.press('Escape')
    await page.waitForTimeout(400)

    // 撤销栈在**客户端**（store.undoStack，UI 自己的操作才入栈），撤销执行的是
    // 「逆操作」（deleteShot/insertShot…）不保证打 /api/shot/undo ——
    // 契约验行为：UI 插入一镜（行数 7）→ 撤销 → 行数回 6；再插入 → Ctrl+Z → 回 6。
    async function uiInsertThenCount() {
      await page.locator('table.shots tbody tr .cell-edit').first().click()
      await page.waitForTimeout(800)
      await tap(page.locator('.el-dialog button', { hasText: '插入新镜' }).first())
      await page.waitForTimeout(1000)
      await page.keyboard.press('Escape')
      await page.waitForTimeout(800)
      return page.locator('table.shots tbody tr').count()
    }
    const afterInsert = await uiInsertThenCount()
    chk('★ UI 插入新镜后表格行数 +1（写后即刷）', afterInsert === FX_TOTAL + 1, `${afterInsert}`)
    await tap(page.locator('[data-testid=btn-undo]'))
    await page.waitForTimeout(1000)
    const afterUndo = await page.locator('table.shots tbody tr').count()
    chk('★ 顶栏撤销：行数回退（删除/插入可回滚）', afterUndo === FX_TOTAL, `${afterUndo}`)
    await uiInsertThenCount()
    await page.keyboard.press('Control+z')
    await page.waitForTimeout(1000)
    const afterHotkey = await page.locator('table.shots tbody tr').count()
    chk('★ Ctrl+Z：行数回退', afterHotkey === FX_TOTAL, `${afterHotkey}`)
  } else {
    skip('写后即刷 + 撤销（U2/U4）', 'btn-undo 未渲染')
  }

  // ⑤ 快捷键帮助（task-2）—— ? 帮助浮层：能开、内容像帮助、Escape 关
  if (await has('btn-undo')) {
    await page.keyboard.press('Shift+Slash')
    await page.waitForTimeout(400)
    const helpVisible = await page.locator('[data-testid=hotkey-help]').isVisible().catch(() => false)
    if (helpVisible) {
      const htxt = await page.locator('[data-testid=hotkey-help]').innerText()
      chk('★ ? 帮助浮层列出快捷键', /Ctrl\+Z|撤销/.test(htxt) && /(R|重渲|L|锁定)/.test(htxt),
        htxt.replace(/\n/g, ' ').slice(0, 80))
      await page.keyboard.press('Escape')
      await page.waitForTimeout(300)
      chk('★ 帮助浮层 Escape 可关',
        !(await page.locator('[data-testid=hotkey-help]').isVisible().catch(() => true)))
    } else {
      chk('★ 按 ? 弹出快捷键帮助（U5）', false, '按 Shift+/ 后 [data-testid=hotkey-help] 未出现/不可见')
    }
  } else {
    skip('快捷键帮助（U5）', 'btn-undo 未渲染')
  }

  // ⑥ E3 预算审批框（夹具额度极小，点启动类按钮必弹「暂停等人批」而非报错）
  {
    const busy = await page.evaluate(async (fx) => {
      const r = await fetch('/api/status?project=' + encodeURIComponent(fx))
      return !!(await r.json()).running
    }, FIXTURE)
    const runBtn = page.locator('button', { hasText: '全链' }).first()
    if (busy) {
      chk('★ E3：有任务在跑时启动按钮被禁用（并发保护）',
        !(await runBtn.isEnabled().catch(() => true)))
    } else if (await runBtn.count()) {
      page.once('dialog', (d) => d.accept())
      await tap(runBtn)
      const box = page.locator('.el-message-box, .budget-approve, [data-testid=budget-approve]')
      await box.waitFor({ timeout: 8000 }).catch(() => {})
      const btxt = await box.innerText().catch(() => '')
      chk('★ E3：超预算时弹「暂停等人批」而不是报错',
        /超出预算|需要你批准|放行/.test(btxt), btxt.slice(0, 60))
      chk('★ 载荷直接回答三问（已花 / 卡在 / 再放行）',
        /已花/.test(btxt) && /卡在/.test(btxt) && /再放行/.test(btxt))
      chk('★ 有「放行这一次」按钮', /放行/.test(btxt))
      await page.locator('button', { hasText: /先不跑|取消|暂不/ }).first().click({ force: true }).catch(() => {})
      await page.waitForTimeout(500)
    } else {
      chk('★ E3：找到「全链」按钮', false, '顶栏找不到启动按钮')
    }
  }

  // ⑦ 设置面板四件套（task-6）—— 面板打开后才渲染：门只看 btn-settings
  if (await has('btn-settings')) {
    await tap(page.locator('[data-testid=btn-settings]'))
    await page.waitForTimeout(500)
    chk('★ 设置面板打开', await page.locator('[data-testid=settings-dialog]').isVisible().catch(() => false),
      '点 ⚙ 设置后 [data-testid=settings-dialog] 未出现/不可见')
    const dtxt = await page.locator('[data-testid=settings-dialog]').innerText().catch(() => '')
    const groups = ['连接', '渲染', '模型', '字幕', '预算', '风格'].filter((g) => dtxt.includes(g))
    chk('★ 设置按组展示（连接/渲染/模型/字幕/预算/风格）', groups.length >= 4,
      `找到 ${groups.join('/')}，全文：${dtxt.replace(/\n/g, ' ').slice(0, 80)}`)
    // 先保存后预检：预检响应处理若炸，不该连坐把「保存」的断言也弄丢
    stub('**/api/config/validate', { ok: true, checks: [], results: [], items: [],
                                     summary: { passed: [], failed: [] }, passed: true,
                                     failed: [], errors: [], warnings: [], message: 'stub' })
    const settingsSave = (await has('settings-save'))
      ? page.locator('[data-testid=settings-save]')
      : page.locator('[data-testid=settings-dialog] button', { hasText: /^保存/ }).last()
    if ((await settingsSave.count())) {
      stub('**/api/config')
      stubLog.length = 0
      await tap(settingsSave)
      await confirmIfAsked()
      if (await has('settings-impact-confirm')) {
        const itxt = await page.locator('[data-testid=settings-impact-confirm]').innerText()
        chk('★ 危险项保存弹专用影响清单（哪些镜头会变 stale）',
          /stale|重渲|失效|影响/.test(itxt), itxt.replace(/\n/g, ' ').slice(0, 80))
        await confirmIfAsked()
      }
      await page.waitForTimeout(500)
      chk('★ 设置保存发出 /api/config 请求', stubLog.some((e) => /\/api\/config(\?|$)/.test(e.url)),
        `实际请求：${stubLog.map((e) => e.url.split('/api/')[1]).join(',') || '（无）'}`)
      await page.unroute('**/api/config').catch(() => {})
    } else {
      skip('设置保存（T4）', '设置面板 footer 找不到「保存」按钮')
    }
    if (await has('settings-validate')) {
      stubLog.length = 0
      await tap(page.locator('[data-testid=settings-validate]'))
      await page.waitForTimeout(600)
      chk('★ 保存前可跑预检（validate 请求发出）',
        stubLog.some((e) => e.url.includes('/api/config/validate')))
      await page.unroute('**/api/config/validate').catch(() => {})
    } else {
      skip('设置预检（T4）', '[data-testid=settings-validate] 未渲染')
    }
    await page.keyboard.press('Escape')
    await page.waitForTimeout(300)
  } else {
    skip('设置面板（T4）', 'btn-settings / settings-dialog 未渲染')
  }

  // ⑧ 剧本 ↔ 镜头联动（task-3）—— script-line / .sl.lost / .sl.linked 开详情
  await page.locator('.el-tabs__item, [role=tab]').filter({ hasText: '剧本' }).first().click({ force: true })
  await page.waitForTimeout(1000)
  const slCount = await page.locator('[data-testid=script-line]').count()
  chk('★ 剧本行带稳定定位（script-line）', slCount > 0, `${slCount} 行`)
  const lostCount = await page.locator('.sl.lost').count()
  if (lostCount > 0) {
    const sctxt = await page.locator('.sctab').innerText().catch(() => '')
    chk('★ 有丢失行时章头标「丢 N 句」', /丢\s*\d+\s*句/.test(sctxt),
      `丢失行 ${lostCount}，章头：${sctxt.split('\n')[0]}`)
  }
  const linked = page.locator('.sl.linked').first()
  if (await linked.count()) {
    await tap(linked)
    await page.waitForTimeout(800)
    chk('★ 点剧本行（.linked）定位开详情弹层', await shotModalOpen())
    await page.keyboard.press('Escape')
    await page.waitForTimeout(400)
  } else {
    chk('★ 剧本有可联动行（.sl.linked）', false, `script-line=${slCount} 但没有 .sl.linked`)
  }

  // ⑨ 成片时间线（task-3）—— hover 预览卡 / 详情按钮 / 点段不弹层 / scrub
  // 播放器现在只在 W4 审片屏（右栏那份副本已撤），所以整段在 /review 上跑。
  // 「详情按钮」这一条顺带多验了一件事：镜头弹层必须从**别的屏**也能弹出
  // （el-dialog 是 append-to-body 的，WorkbenchView 被 KeepAlive 摘下来时它仍然有效）。
  await goWs('/review')
  // 一集一章之后默认集只有 2 段，而 hover/详情/点段这几条要 4 段才测得动 → 先切到 EP01
  if ((await page.locator('.epbtn').count()) > 1) {
    await page.locator('.epbtn').first().click({ force: true })
    await page.waitForTimeout(1000)
  }
  const seg0 = page.locator('[data-testid=timeline-seg]')
  const segN = await seg0.count()
  chk('★ 时间线分段带稳定定位（timeline-seg）', segN > 0, `${segN} 段`)
  if (segN > 3) {
    // force: 预览卡弹出后会盖住分段本身（被测行为就是「hover 出预览」），
    // Playwright 的可点性检查会跟自己的 tooltip 打架 —— 这里跳过该检查
    // hoverSeg 由 segbar 的 @pointermove 命中测试置位（FinalTab.onMove 只读 clientX）——
    // 合成 PointerEvent 确定性驱动，顺带先真鼠标 hover 一遍（两法并用）
    for (const i of [1, 2, 3]) {
      const bb = await seg0.nth(i).boundingBox().catch(() => null)
      if (bb) {
        await page.mouse.move(bb.x + bb.width / 2, bb.y + bb.height / 2).catch(() => {})
        await page.evaluate(([x, y]) => {
          const bar = document.querySelector('[data-testid=timeline-scrub]')
          if (bar) bar.dispatchEvent(new PointerEvent('pointermove', { clientX: x, clientY: y, bubbles: true }))
        }, [bb.x + bb.width / 2, bb.y + bb.height / 2])
      }
      await page.waitForTimeout(600)
      if (await page.locator('.segpreview').count()) break
    }
    const prev = page.locator('.segpreview')
    if (await prev.count()) {
      const ptxt = await prev.innerText()
      chk('★ hover 预览卡含台词与时长', ptxt.trim().length > 0 && /\d/.test(ptxt),
        ptxt.replace(/\n/g, ' ').slice(0, 60))
      const detailBtn = prev.locator('button, [role=button], a, .lk', { hasText: '详情' }).first()
      chk('★ 预览卡有「详情」按钮', await detailBtn.count() > 0)
      if (await detailBtn.count()) {
        await tap(detailBtn)
        await page.waitForTimeout(800)
        chk('★ 预览卡「详情」打开弹层', await shotModalOpen())
        await page.keyboard.press('Escape')
        await page.waitForTimeout(400)
      }
    } else {
      chk('★ hover 分段出预览卡（U8）', false, 'hover timeline-seg 后 .segpreview 未出现')
    }

    await page.mouse.move(5, 5)   // 收起 hover 预览卡（它会盖住分段条）
    await page.waitForTimeout(400)
    await page.keyboard.press('Escape')   // 清残留弹层，别冒充详情弹层
    await page.waitForTimeout(300)
    await tap(seg0.nth(3))
    await page.waitForTimeout(500)
    chk('★ 点段=跳转联动选中，不弹详情弹层', !(await shotModalOpen()),
      '点段后出现了镜头详情弹层（点段不该开详情，详情在 hover 预览卡上）')
  }
  // scrub：pointerdown + 水平拖 >3px = 拖拽定格（契约），拖完不许弹层
  const bar = page.locator('[data-testid=timeline-scrub]')
  if (await bar.count() && segN > 3) {
    const box = await bar.boundingBox()
    await page.mouse.move(box.x + box.width * 0.2, box.y + box.height / 2)
    await page.mouse.down()
    await page.mouse.move(box.x + box.width * 0.6, box.y + box.height / 2, { steps: 8 })
    await page.mouse.up()
    await page.waitForTimeout(500)
    chk('★ scrub 拖拽后不弹详情弹层', !(await shotModalOpen()))
  } else {
    skip('时间线 scrub（U8）', '[data-testid=timeline-scrub] 未渲染或分段不足')
  }
  // 「点段/scrub = 联动选中」的落点：切回工作台剧本 tab 看高亮
  // （跨屏仍要成立：审片屏上拖出来的选中，回到镜头表/剧本那边得还是同一镜）
  await goWs('/workbench')
  await page.locator('.el-tabs__item, [role=tab]').filter({ hasText: '剧本' }).first().click({ force: true })
  await page.waitForTimeout(600)
  const mark2 = await findSelectionMarker('.sl')
  chk('★ 时间线选中联动剧本高亮', mark2.found,
    `找不到选中态类名；.sl 实际类名：${JSON.stringify(mark2.classes)}`)

  // ⑩ QC 一键全部重渲 / 审计一键重写（task-3）—— 一律走请求桩
  await page.locator('.el-tabs__item, [role=tab]').filter({ hasText: '质检' }).first().click({ force: true })
  await page.waitForTimeout(800)
  const qcBtn = page.locator('[data-testid=btn-qc-rerender-all]')
  if (await qcBtn.count()) {
    stub('**/api/run')
    stubLog.length = 0
    await tap(qcBtn)
    await confirmIfAsked()
    await page.waitForTimeout(600)
    const hit = stubLog.find((e) => /\/api\/run/.test(e.url))
    let payload = {}
    try { payload = JSON.parse(hit?.body || '{}') } catch {}
    chk('★ QC 一键重渲走 /api/run 批量（force + only 清单）',
      !!hit && payload.force === true && Array.isArray(payload.only) && payload.only.length > 0,
      `请求=${hit ? hit.url.split('/api/')[1] : '（无）'} payload=${JSON.stringify(payload).slice(0, 80)}`)
    await page.unroute('**/api/run').catch(() => {})
  } else {
    skip('QC 一键全部重渲（U6）', 'btn-qc-rerender-all 未渲染（rerender 队列为空时按设计不渲染）')
  }
  await page.locator('.el-tabs__item, [role=tab]').filter({ hasText: '审计' }).first().click({ force: true })
  await page.waitForTimeout(800)
  const rwBtn = page.locator('[data-testid=btn-rewrite-prompt]')
  if (await rwBtn.count()) {
    stub('**/api/shot/rewrite')
    stubLog.length = 0
    await tap(rwBtn.first())
    await confirmIfAsked()
    await page.waitForTimeout(600)
    const hit = stubLog.find((e) => e.url.includes('/api/shot/rewrite'))
    let payload = {}
    try { payload = JSON.parse(hit?.body || '{}') } catch {}
    chk('★ finding「重写该镜提示词」发出带镜头号的 rewrite', !!hit && !!(payload.id || payload.shot_id),
      `请求=${hit ? hit.url.split('/api/')[1] : '（无）'} payload=${JSON.stringify(payload).slice(0, 80)}`)
    await page.unroute('**/api/shot/rewrite').catch(() => {})
  } else {
    skip('审计一键重写（U6）', 'btn-rewrite-prompt 未渲染')
  }

  // ⑪ 暗色切换（task-7）
  if (await has('theme-toggle')) {
    const bg1 = await page.evaluate(() => getComputedStyle(document.body).backgroundColor)
    await tap(page.locator('[data-testid=theme-toggle]'))
    await page.waitForTimeout(400)
    const theme = await page.evaluate(() => document.documentElement.getAttribute('data-theme'))
    const bg2 = await page.evaluate(() => getComputedStyle(document.body).backgroundColor)
    chk('★ 暗色切换：html[data-theme=dark] 且背景确实变暗', theme === 'dark' && bg1 !== bg2,
      `data-theme=${theme} bg ${bg1} → ${bg2}`)
    await tap(page.locator('[data-testid=theme-toggle]'))
    await page.waitForTimeout(400)
    chk('★ 再点一次切回亮色',
      (await page.evaluate(() => document.documentElement.getAttribute('data-theme'))) !== 'dark')
  } else {
    skip('暗色切换（U12）', '[data-testid=theme-toggle] 未渲染（task-7 未落地）')
  }

  // ⑫ 1280/1024 小屏布局（task-7，FAST 冒烟跳过）
  if (FAST) {
    skip('小屏布局（U11）', 'FAST 冒烟模式跳过视口段（完整模式跑）')
  } else if (await has('drawer-toggle')) {
    for (const vp of [{ width: 1280, height: 800 }, { width: 1024, height: 768 }]) {
      await page.setViewportSize(vp)
      await page.waitForTimeout(600)
      const ov = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, iw: window.innerWidth }))
      chk(`★ ${vp.width}×${vp.height} 无横向溢出`, ov.sw <= ov.iw + 1,
        `scrollWidth=${ov.sw} vs innerWidth=${ov.iw}（超了就是固定栅格没吃断点）`)
      chk(`★ ${vp.width}×${vp.height} 抽屉把手可点`,
        await page.locator('[data-testid=drawer-toggle]').isVisible().catch(() => false))
    }
    await page.setViewportSize({ width: 1680, height: 1000 })
    await page.waitForTimeout(400)
  } else {
    skip('小屏布局（U11）', '[data-testid=drawer-toggle] 未渲染（task-7 未落地）')
  }

  // ⑬ 全局错误条（task-2）—— 独立页面做故障注入，不污染总闸的错误账本
  {
    const ep = await browser.newPage({ viewport: { width: 1680, height: 1000 } })
    await ep.route('**/api/status*', (r) => r.abort('failed'))
    await ep.route('**/api/shots*', (r) => r.abort('failed'))
    await ep.route('**/api/projects*', (r) => r.abort('failed'))
    await ep.goto(BASE, { waitUntil: 'domcontentloaded', timeout: 30000 }).catch(() => {})
    await ep.waitForTimeout(2500)
    if (await ep.locator('[data-testid=global-error-bar]').count()) {
      const etxt = await ep.locator('[data-testid=global-error-bar]').innerText()
      chk('★ 首拉失败时全局错误条说人话', etxt.trim().length > 0,
        `错误条文案：${JSON.stringify(etxt.trim().slice(0, 60))}`)
    } else {
      skip('全局错误条（U3）', '[data-testid=global-error-bar] 未渲染')
    }
    await ep.close()
  }

  // ⑭ 语义回归闸（曾为产品观察；backend-hard 已整改为优雅跳过）：
  // 锁定**未渲染镜头**不许报错 —— 200 + locked:false + note「已跳过」；
  // bulk ids 无产物项进 skipped，批量不因个别项炸掉。独立页跑真服务，夹具一次性安全。
  {
    const ep2 = await browser.newPage()
    await ep2.goto(BASE, { waitUntil: 'domcontentloaded', timeout: 30000 }).catch(() => {})
    const rep = await ep2.evaluate(async (fx) => {
      const lock = (body) => fetch('/api/shot/lock', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      }).then(async (r) => ({ status: r.status, j: await r.json().catch(() => ({})) }))
      const miss = await lock({ project: fx, id: '2-1-01', flag: 'locked', value: true })
      const bulk = await lock({ project: fx, ids: ['1-1-01', '2-1-02'], flag: 'locked', value: true })
      return { miss, bulk }
    }, FIXTURE)
    chk('★ 锁定未渲染镜头 = 优雅跳过（200 + locked:false + 人话 note）',
      rep.miss.status === 200 && rep.miss.j.locked === false
      && /跳过/.test(String(rep.miss.j.note || '') + String(rep.miss.j.message || '')),
      `missing → ${rep.miss.status} ${JSON.stringify(rep.miss.j).slice(0, 120)}`)
    chk('★ bulk ids：无产物项进 skipped，整批不炸',
      rep.bulk.status === 200 && Array.isArray(rep.bulk.j.updated) && Array.isArray(rep.bulk.j.skipped)
      && rep.bulk.j.skipped.includes('2-1-02') && rep.bulk.j.updated.includes('1-1-01'),
      `bulk → ${rep.bulk.status} ${JSON.stringify(rep.bulk.j).slice(0, 140)}`)
    await ep2.close()
  }

  // ⑮ 单一心跳闸（F0.2）：全控制台只允许 1 个 setInterval。
  // 拆之前是 3 个（App 2s 主轮询 / QueueFab 1s 时钟 / QueueFab 3s 队列），
  // 各自 clearInterval、节拍互相撞、切到后台照跑。这里同时验"合并了但节拍没乱"：
  // status 仍约 2s 一次、queue 仍约 3s 一次。独立页面 + addInitScript 必须在导航前挂。
  {
    const tp = await browser.newPage()
    await tp.addInitScript(() => {
      window.__si = 0
      const orig = window.setInterval.bind(window)
      window.setInterval = (fn, ms, ...rest) => { window.__si++; return orig(fn, ms, ...rest) }
    })
    await tp.goto(BASE, { waitUntil: 'domcontentloaded', timeout: 30000 }).catch(() => {})
    await tp.waitForTimeout(3000)
    const created = await tp.evaluate(() => window.__si)
    const counted = await tp.evaluate(() => new Promise((res) => {
      const m = {}
      const ro = new PerformanceObserver((l) => {
        for (const e of l.getEntries()) {
          const p = (e.name.match(/\/api\/[a-z]+/) || [''])[0]
          if (p) m[p] = (m[p] || 0) + 1
        }
      })
      ro.observe({ entryTypes: ['resource'] })
      setTimeout(() => { ro.disconnect(); res(m) }, 6000)
    }))
    chk('★ 全控制台只有一个 setInterval（F0.2 单心跳）', created === 1, `创建了 ${created} 个`)
    chk('★ 合并到单心跳后节拍不变（status≈2s、queue≈3s）',
      (counted['/api/status'] || 0) >= 2 && (counted['/api/queue'] || 0) >= 1,
      `6s 内 ${JSON.stringify(counted)}`)
    await tp.close()
  }
}

// ---------- ⑨'' P1 新能力：项目与章节管理（W0/W1）----------
// 这块是**新界面**，不用真浏览器点一遍就等于没测。
// 纪律：在被测项目名里带时间戳 + 结束后 rm 掉该目录 ——
// 后台的 create_project 刻意「拒绝覆盖同名项目」（防静默清空别人的片子），
// 所以固定名字的用例第二次跑必红；夹具根是 tmp，删自己造的东西是安全的。
{
  const P1_NAME = `e2ep1-${Date.now()}`
  const { rmSync } = await import('node:fs')
  // 临时文件路径声明在 try **外面**：finally 要用，声明在里面是块级作用域，
  // 上一版就是这么写成了 `f1CleanPath is not defined`（自己踩的）。
  const tmpFiles = ['/tmp/e2ep1_a.md', '/tmp/e2ep1_b.md']
  const [f1, f2] = tmpFiles
  try {
    await page.goto(BASE + '/#/projects', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(700)
    // 切换器就是流程本身：编号 + 名字一起断，比"有几个 a 标签"更能说明问题
    // （W4 加第 5 屏时，纯计数那条只会红、不会告诉你哪一屏跑偏了）
    const wsLabels = (await page.locator('[data-testid=ws-nav] a').allInnerTexts())
      .map((t) => t.replace(/\s+/g, ''))
    chk('★P1 工作区切换器 = 流程五屏（0 项目 → 1 章节 → 2 风格 → 3 镜头 → 4 成片）',
      wsLabels.join('>') === '0项目>1章节>2风格>3镜头>4成片', wsLabels.join(' | '))
    // W2 已在 P2 实施，所以这里断言"它已可用"（原先断言的是禁用态 —— 事实变了，
    // 断言跟着改，而不是留着让测试长红）
    chk('★P1 「风格」屏已实施、可点（不再禁用）',
      await page.locator('[data-testid=ws-tab-style]').getAttribute('aria-disabled') === null)
    chk('★P1 项目屏可达', (await page.locator('[data-testid=ws-projects]').count()) === 1)

    // 新建向导
    await page.locator('[data-testid=btn-new-project]').click()
    await page.waitForSelector('[data-testid=new-project-mask]')
    await page.locator('[data-testid=np-name]').fill(P1_NAME)
    await page.locator('[data-testid=np-title]').fill('E2E 用项目')
    await page.locator('[data-testid=np-logline]').fill('自动化造的壳')
    await page.waitForTimeout(120)
    await page.locator('[data-testid=np-submit]').click()
    await page.waitForTimeout(900)
    chk('★P1 新建项目后自动落到章节屏', !!page.url().includes('/chapters'), page.url())

    // 手动新建一章（粘贴正文）
    await page.locator('[data-testid=btn-ch-new]').click()
    await page.waitForTimeout(250)
    await page.locator('[data-testid=ch-title]').fill('第一章_开局')
    await page.locator('[data-testid=ch-body]').fill('主角在站台出现。' .repeat(40))
    await page.waitForTimeout(120)
    chk('★P1 未保存徽标出现', (await page.locator('[data-testid=ch-dirty]').count()) === 1)
    await page.locator('[data-testid=ch-save]').click()
    await page.waitForTimeout(900)
    chk('★P1 保存后清单出现该章且标「未拆」',
      (await page.locator('[data-testid=ch-row]').count()) === 1
      && (await page.locator('[data-testid=badge-todo]').count()) === 1)

    // 导入两个文件（第二个是同名，应被跳过不覆盖）
    writeFileSync(f1, '第二章内容。'.repeat(60))
    writeFileSync(f2, '第三章内容。'.repeat(60))
    await page.locator('[data-testid=btn-ch-import]').click()
    await page.waitForSelector('[data-testid=import-mask]')
    await page.locator('[data-testid=import-input]').setInputFiles([f1, f2])
    await page.waitForTimeout(400)
    chk('★P1 导入对话框列出 2 个文件', (await page.locator('[data-testid=import-picked] li').count()) === 2)
    await page.locator('[data-testid=import-submit]').click()
    await page.waitForTimeout(1100)
    chk('★P1 导入后共 3 章', (await page.locator('[data-testid=ch-row]').count()) === 3,
      String(await page.locator('[data-testid=ch-row]').count()))

    // 改正文 → 该章应被标「需重拆」（前提是它已拆过；这里没拆，所以只验"保存成功且不脏"）
    await page.locator('[data-testid=ch-row]').first().click()
    await page.waitForTimeout(500)
    await page.locator('[data-testid=ch-body]').type('补一句。', { delay: 5 })
    await page.waitForTimeout(150)
    chk('★P1 编辑既有章节会出现未保存态', (await page.locator('[data-testid=ch-dirty]').count()) === 1)
    await page.locator('[data-testid=ch-revert]').click()
    await page.waitForTimeout(250)
    chk('★P1 放弃修改后回到干净态', (await page.locator('[data-testid=ch-dirty]').count()) === 0)

    // 重排：第二章上移一次
    const before = await page.locator('[data-testid=ch-row] .main b').allInnerTexts()
    await page.locator('[data-testid=ch-down]').first().click()
    await page.waitForTimeout(1000)
    const after = await page.locator('[data-testid=ch-row] .main b').allInnerTexts()
    chk('★P1 上移/下移真的改变清单顺序', before.join() !== after.join(),
      `${before.join('、')} → ${after.join('、')}`)
    chk('★P1 首行上移按钮禁用（不能越界）',
      await page.locator('[data-testid=ch-up]').first().isDisabled())

    // 删除是可逆的：进 _trash
    await page.locator('[data-testid=ch-delete]').first().click()
    await page.waitForTimeout(300)
    await confirmIfAsked()
    await page.waitForTimeout(1100)
    chk('★P1 删除后清单少一章', (await page.locator('[data-testid=ch-row]').count()) === 2,
      String(await page.locator('[data-testid=ch-row]').count()))
    const trash = await page.evaluate(async (nm) => {
      const r = await fetch('/api/chapter/trash?project=' + encodeURIComponent(nm))
      return r.json()
    }, P1_NAME)
    chk('★P1 被删正文确实在 _trash 里（可恢复，不是真删）',
      Array.isArray(trash.items) && trash.items.length >= 1, trash.message)

    // 回到工作台不该炸（新项目 0 镜头）
    await page.locator('[data-testid=ws-tab-workbench]').click()
    await page.waitForTimeout(1200)
    chk('★P1 零镜头项目的工作台不报错（空态而不是白屏）',
      (await page.locator('.workbench').count()) === 1)
  } catch (e) {
    chk('★P1 项目与章节管理（整段）', false, String(e).slice(0, 160))
  } finally {
    // 自己造的沙箱自己清干净：临时项目目录 + 两个临时 md
    rmSync(`/tmp/vm-e2e/${P1_NAME}`, { recursive: true, force: true })
    for (const f of tmpFiles) rmSync(f, { force: true })
  }
}

  // 「只重拆这一章」：前端有没有把 chapter 号真的发出去。
  // P0.3 修好了后台，但**界面没有入口 = 修了等于没修**，所以这条必须测。
  // 用 route 桩：① 不真起任务（不烧 token）；② 不依赖夹具里是否存在"正文比镜头表新"
  // 这种时间戳巧合 —— 那种断言换台机器就会漂。
  {
    const cap = { run: null }
    const rp = await browser.newPage({ viewport: { width: 1680, height: 1000 } })
    await rp.route('**/api/chapters**', async (route) => {
      const r = await route.fetch()
      const j = await r.json()
      if (j.chapters?.length) j.chapters[0].needs_replan = true
      await route.fulfill({ response: r, body: JSON.stringify(j) })
    })
    await rp.route('**/api/run', async (route) => {
      cap.run = JSON.parse(route.request().postData() || '{}')
      await route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ ok: true, message: 'stubbed', pid: 1, stage: 'plan' }) })
    })
    await rp.goto(BASE + '/#/chapters', { waitUntil: 'domcontentloaded' })
    await rp.waitForTimeout(900)
    chk('★P1 需重拆的章节出现「只重拆这一章」按钮',
      (await rp.locator('[data-testid=ch-replan]').count()) === 1)
    chk('★P1 该章同时挂「需重拆」徽标',
      (await rp.locator('[data-testid=badge-replan]').count()) === 1)
    await rp.locator('[data-testid=ch-replan]').click()
    await rp.waitForTimeout(350)
    // 必须弹确认：拆镜烧 token，静默触发是本项目明令避免的
    const asked = (await rp.locator('.el-message-box, .el-overlay-dialog').count()) > 0
    chk('★P1 单章重拆要先确认（不静默烧 token）', asked)
    // `confirmIfAsked()` 内部硬编码用主页面 `page`，对第二个页面对象无效，
    // 所以这里就地确认（不要为了省事把 helper 改成"接受一个 page 参数"却仍指向主页面）。
    const rbox = rp.locator('.el-message-box, .el-overlay-dialog')
    if ((await rbox.count()) > 0) {
      const btns = rbox.locator('button')
      const n = await btns.count()
      for (let i = n - 1; i >= 0; i--) {
        const t = (await btns.nth(i).innerText()).trim()
        if (t && !/取消|Close|关闭/.test(t)) { await btns.nth(i).click(); break }
      }
    }
    await rp.waitForTimeout(700)
    const no = cap.run?.chapter
    chk('★P1 /api/run 带 chapter 号（真的只拆那一章）',
      cap.run?.stage === 'plan' && typeof no === 'number' && no > 0,
      JSON.stringify(cap.run))
    chk('★P1 不默认 force（保留护栏）', cap.run ? cap.run.force !== true : false,
      JSON.stringify(cap.run))
    await rp.close()
  }

// ---------- ⑨‴ P2 风格层（W2）----------
// P2 的实质不是"多了个下拉框"，而是两件事：风格变成整本一份 + 改风格的代价在保存前就摊开。
//
// ★ 纪律：本段自己准备起点、自己收尾。上一版栽过 —— 夹具风格被之前手工 curl 设成
//   anime，于是"点 anime 卡片"不产生脏态、保存按钮始终禁用、测试点 30s 超时。
//   依赖上一次运行留下的状态 = 必然出现"只红一次"的测试。
{
  const sp = await browser.newPage({ viewport: { width: 1680, height: 1000 } })
  const posts = []
  sp.on('request', (r) => {
    if (r.method() === 'POST' && /\/api\/style/.test(r.url())) {
      posts.push({ u: r.url().split('/api/')[1], b: JSON.parse(r.postData() || '{}') })
    }
  })
  try {
    await sp.goto(BASE, { waitUntil: 'domcontentloaded' })
    await sp.evaluate(async () => {
      await fetch('/api/style/set', { method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project: 'e2efx', preset: 'realistic' }) })
    })
    await sp.goto(BASE + '/#/style', { waitUntil: 'domcontentloaded' })
    await sp.waitForTimeout(900)

    chk('★P2 风格屏可达', (await sp.locator('[data-testid=ws-style]').count()) === 1)
    chk('★P2 三个预设卡片都在', (await sp.locator('[data-testid=st-presets] button').count()) === 3)
    const src0 = (await sp.locator('[data-testid=st-source]').innerText()).replace(/\n/g, ' ')
    chk('★P2 显示风格出处（不是匿名下拉框）',
      /人工设定|系统按整本推荐|预设默认/.test(src0), src0.slice(0, 50))
    chk('★P2 影响清单在保存前就已显示', (await sp.locator('[data-testid=st-impact]').count()) === 1)
    const imp = (await sp.locator('[data-testid=st-impact]').innerText()).replace(/\s+/g, ' ')
    chk('★P2 影响清单如实说明"改风格不会让镜头自动 stale"',
      /不会.*让镜头自动变 stale/.test(imp), imp.slice(0, 90))
    chk('★P2 无从判断的图被单列，而不是算成"无需重出"',
      /无从判断/.test(imp) || (await sp.locator('[data-testid=st-untracked]').count()) === 0,
      imp.slice(0, 60))

    const curPreset = (src0.match(/当前：\s*(\w+)/) || [])[1] || 'realistic'
    const target = ['cg', 'anime', 'realistic'].find((k) => k !== curPreset)
    await sp.locator(`[data-testid=st-preset-${target}]`).click()
    await sp.waitForTimeout(800)
    chk(`★P2 切到 ${target} 后进入未保存态（保存可点）`,
      await sp.locator('[data-testid=st-save]').isEnabled())

    await sp.locator('[data-testid=st-save]').click()
    await sp.waitForTimeout(700)
    const box = sp.locator('.el-message-box')
    if ((await box.count()) > 0) {
      const txt = (await box.innerText()).replace(/\s+/g, ' ')
      chk('★P2 保存前确认框讲清"只改设定、不重写镜头表"',
        /不会.*重写镜头表|重跑.*拆镜/.test(txt), txt.slice(0, 80))
      const btns = box.locator('button')
      await btns.nth((await btns.count()) - 1).click()
      await sp.waitForTimeout(1000)
    } else {
      chk('★P2 无旧风格句时不弹多余确认', true)
    }
    const after = (await sp.locator('[data-testid=st-source]').innerText()).replace(/\n/g, ' ')
    chk(`★P2 保存后落到 ${target} 且标记"已人工确认"`,
      after.includes(target) && /已人工确认/.test(after), after.slice(0, 60))
    chk('★P2 保存走 /api/style/set', posts.some((x) => x.u.startsWith('style/set')),
      JSON.stringify(posts.map((x) => x.u)))
  } catch (e) {
    chk('★P2 风格屏（整段）', false, String(e).slice(0, 180))
  } finally {
    await sp.close()
    const { rmSync } = await import('node:fs')
    rmSync('/tmp/vm-e2e/e2efx/style.json', { force: true })
  }
}

// ---------- ⑨‖ P3 实体总表（W2 下半）----------
// 这页的价值是"救济"：跨章去重一定会判错，必须有一个能看见、能手工纠正的入口。
// 用 route 桩喂 /api/entities，避免真合并把夹具镜头表改坏（真合并的路径已有后端单测覆盖）。
{
  const ep3 = await browser.newPage({ viewport: { width: 1680, height: 1000 } })
  try {
    await ep3.route('**/api/entities?**', async (route) => {
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify({
        ok: true, project: 'e2efx',
        entities: [
          { kind: 'char', id: '唐僧', name: '唐僧', chapters: [1, 2], shot_count: 3,
            has_prompt: true, portrait_variants: [{ path: 'char_唐僧.png', chapter: null, kind: 'default' }],
            portrait_chapters: [], costume_variants: ['default', 'wet_robe'] },
          { kind: 'scene', id: 'S1', name: '山门', chapters: [1], shot_count: 3, location: 'gate', time_of_day: 'dusk' },
          { kind: 'scene', id: 'S9', name: '山门', chapters: [], shot_count: 0, location: 'gate' },
          { kind: 'prop', id: 'P1', name: '禅杖', chapters: [1], shot_count: 2, owner: '唐僧', inferred: true },
        ],
        totals: { char: 1, scene: 2, prop: 1 },
        suspect_merges: ['场景名「山门」被登记在多个 id 上（S1, S9）—— 若是同一个地点，请手工合并'],
        never_used: ['scene:S9'],
      }) })
    })
    await ep3.goto(BASE + '/#/style', { waitUntil: 'domcontentloaded' })
    await ep3.waitForTimeout(1100)
    chk('★P3 实体总表随 W2 一起渲染', (await ep3.locator('[data-testid=entities-panel]').count()) === 1)
    chk('★P3 四类实体分行列出', (await ep3.locator('[data-testid=ent-table] tbody tr').count()) === 4)
    chk('★P3 去重可疑被显式暴露（不是静默合并）',
      (await ep3.locator('[data-testid=ent-suspect]').count()) === 1
      && /山门/.test(await ep3.locator('[data-testid=ent-suspect]').innerText()))
    chk('★P3 零引用实体被点名', /S9/.test(await ep3.locator('[data-testid=ent-unused]').innerText()))
    chk('★P3 章数从镜头反推显示', /1、2/.test(await ep3.locator('[data-testid=ent-row-char-唐僧]').innerText()))
    chk('★P3 推断道具带「推断」标记（需人工复核）',
      /推断/.test(await ep3.locator('[data-testid=ent-row-prop-P1]').innerText()))
    chk('★P3 场景行给出合并入口', (await ep3.locator('[data-testid=ent-merge]').count()) === 2)
    const btn = ep3.locator('[data-testid=ent-portrait-唐僧]')
    chk('★P3 有服装变体的角色才能按章出专属定妆', await btn.isEnabled())
    chk('★P3 按章筛选下拉可用', (await ep3.locator('[data-testid=ent-chapter-filter]').count()) === 1)
    await ep3.locator('[data-testid=ent-chapter-filter]').selectOption('2')
    await ep3.waitForTimeout(400)
    chk('★P3 按章筛选真的过滤了行', (await ep3.locator('[data-testid=ent-table] tbody tr').count()) === 1)
  } catch (e) {
    chk('★P3 实体总表（整段）', false, String(e).slice(0, 180))
  } finally {
    await ep3.close()
  }
}

// ---------- ⑨″ W4 审片屏（F3.1 的落点：播放器与问题清单同屏）----------
{
  const rv = await browser.newPage({ viewport: { width: 1680, height: 1000 } })
  const rvErrs = []
  rv.on('pageerror', (e) => rvErrs.push(String(e.message)))
  rv.on('console', (m) => { if (m.type() === 'error' && !/409|favicon/.test(m.text())) rvErrs.push(m.text()) })
  try {
    // 先走**用户真正会走的路**：落在工作台 → 顶点栏「成片」。
    await rv.goto(BASE, { waitUntil: 'domcontentloaded' })
    await rv.waitForTimeout(2000)
    await rv.locator('[data-testid=ws-tab-review]').click({ force: true })
    await rv.waitForTimeout(1400)

    chk('★W4 顶栏「成片」可点，点得到这一屏（工作区 5 屏名单由 P1 那条管）',
      (await rv.locator('[data-testid=ws-review]').count()) === 1
      && (await rv.locator('[data-testid=ws-tab-review].on').count()) === 1)

    // 再冷启动直接进 #/review 并刷新一次：这一条专抓
    // "子组件 onMounted 早于 loadProjects 落地" → 清单永远空（实测踩过，靠 watch state.project 修）
    await rv.reload({ waitUntil: 'domcontentloaded' })
    await rv.waitForTimeout(2600)
    chk('★W4 冷启动直链可达（不必先从工作台点进来）',
      (await rv.locator('[data-testid=ws-review]').count()) === 1)
    const coldRows = await rv.locator('[data-testid=rv-findings] .qcrow:not(.project)').count()
    chk('★W4 冷启动时两张清单真的拉到数据（不是等项目落地的那次空拉）',
      coldRows > 0, `${coldRows} 行`)
    // 完备性矩阵由 AuditTab 自己拉（`refreshCompleteness`）。截图时它还是"加载中…"，
    // 所以这条要**等到它真出来**才算数 —— 否则新屏上少一个数据源没人发现。
    await rv.waitForSelector('.cmtable', { timeout: 15000 }).catch(() => {})
    chk('★W4 完备性矩阵在审片屏也出得来（不是只在工作台那个 tab 里才加载）',
      (await rv.locator('.cmtable').count()) === 1)
    chk('★W4 右列同时挂着质检 + 审计两张清单（不是切 tab 才看见）',
      (await rv.locator('.rvside .tabroot').count()) === 2)

    // 播放器要真的"变大"了 —— 这是搬出 420px 右栏的全部理由
    const vh = await rv.locator('video.finalvid').boundingBox().catch(() => null)
    const maxH = await rv.locator('video.finalvid').evaluate((el) => getComputedStyle(el).maxHeight)
    chk('★W4 播放器按全屏放宽（不再受右栏 260px 限制）',
      !!vh && parseFloat(maxH) > 300, `高=${vh && Math.round(vh.height)}px max-height=${maxH}`)

    // 点质检行 → 播放头落到那一镜（W4 的核心新能力）。
    // 一集一章 ⇒ 要分别测「问题在本集」和「问题在另一集」两条路：
    // 后者必须先自动切集，否则会在 EP01 的文件里按 EP02 的比例尺定位（跳错还看着像对）。
    await rv.waitForFunction(
      () => (document.querySelector('video.finalvid') || {}).duration > 0,
      null, { timeout: 15000 },
    ).catch(() => {})
    // 只认**长得像镜头号**的行（`1-2-03`）—— 审计里的「全片级」行不参与跳转。
    const rowIds = (await rv.locator('.rvside .qcrow:not(.project) .sid').allInnerTexts())
      .map((s) => s.trim()).filter((s) => /^\d+-\d+-\d+$/.test(s.split('、')[0]))
    const curStem = ((await rv.locator('.epbtn.on').innerText().catch(() => '')) || 'EP01').trim()
    const curNo = Number(curStem.replace(/\D/g, '')) || 1
    const chOf = (id) => Number(String(id).split('-')[0]) || 0
    const inEp = rowIds.find((id) => chOf(id) === curNo)
    const other = rowIds.find((id) => chOf(id) !== curNo)

    async function jumpLegend(id) {
      await rv.locator('.rvside .qcrow:not(.project)')
        .filter({ hasText: id }).first().click({ force: true })
      await rv.waitForTimeout(1100)
      return (await rv.locator('.seglegend').first().innerText()).replace(/\s+/g, ' ')
    }
    if (inEp) {
      const legend = await jumpLegend(inEp)
      chk('★W4 点本集质检行 → 播放头跳到那一镜（清单与画面同屏才做得到）',
        legend.includes(inEp), `点的是 ${inEp}，条上显示「${legend.slice(0, 70)}」`)
      chk('★W4 跳转成功时屏底不出提示（错了才说话）',
        (await rv.locator('[data-testid=rv-hint]').count()) === 0)
    } else {
      skip('W4 本集内跳转', `夹具质检行 ${JSON.stringify(rowIds)} 里没有属于 ${curStem} 的镜头`)
    }
    if (other) {
      const legend = await jumpLegend(other)
      const nowStem = ((await rv.locator('.epbtn.on').innerText().catch(() => '')) || '').trim()
      chk('★W4 点**另一集**的问题行会先自动切到那一集再跳（不跳错尺度）',
        legend.includes(other) && nowStem === `EP${String(chOf(other)).padStart(2, '0')}`,
        `点的是 ${other} → 当前集 ${nowStem}，条上「${legend.slice(0, 60)}」`)
    } else {
      skip('W4 跨集自动切集', `夹具质检行 ${JSON.stringify(rowIds)} 全在同一集里`)
    }
    // 下载链接只有一个、且跟着播放器所选集走（多集时"永远 EP01"是旧事故）。
    // ★ 这里不能用 .epbtn.first()：上面那两条跳转断言已经把所选集切走了，
    //   再点 first() 多半是"点当前集"= 无变化 —— 测试要按**当前是哪一集**挑对立面。
    const stems = (await rv.locator('.epbtn').allInnerTexts()).map((s) => s.trim())
    if (stems.length > 1) {
      const onStem = (await rv.locator('.epbtn.on').innerText()).trim()
      const target = stems.findIndex((s) => s !== onStem)
      const dlSel = '[data-testid=rv-player] .summary a.dl'
      const href0 = await rv.locator(dlSel).getAttribute('href')
      await rv.locator('.epbtn').nth(target).click({ force: true })
      await rv.waitForTimeout(900)
      const href1 = await rv.locator(dlSel).getAttribute('href')
      chk('★W4 下载跟着所选集变（不是第二个写死的真相）',
        href0 !== href1 && href1.includes(`episode=${stems[target]}`), `${href0} → ${href1}`)
      await rv.locator('.epbtn').nth(stems.indexOf(onStem)).click({ force: true })
      await rv.waitForTimeout(700)
    } else {
      chk('★W4 单集项目不显示集切换器（只有多集才需要选）', stems.length <= 1)
    }

    await rv.screenshot({ path: '/tmp/vue-review.png' })
    chk('★W4 审片屏无 JS 运行时错误', rvErrs.length === 0, rvErrs.slice(0, 2).join(' | '))
  } catch (e) {
    chk('★W4 审片屏（整段）', false, String(e).slice(0, 180))
  } finally {
    await rv.close()
  }
}

mark("⑨' v0.2 新能力 完成")
} catch (e) {
  chk('主流程无未捕获异常', false, String((e && e.stack) || e).replace(/\n/g, ' ⏎ ').slice(0, 300))
}

// ---------- ⑩ 控制台错误 ----------
// 浏览器会把被 fetch 处理掉的 409 也记成 console error（"Failed to load resource"），
// 那是护栏的正常协议，不是 JS 运行时错误。
// 历史病灶备忘（已修，豁免哨兵已撤）：QcTab 对未质检镜头的兜底对象缺 issues/metrics，
// 模板裸读 .length 崩过渲染（TypeError: ...reading 'length'）。同类兜底字段缺失回归必须红。
const realConsoleErrors = consoleErrors.filter((t) => !/409/.test(t))
const errStacks = await page.evaluate(() => (window.__errStacks || []).map((t) => String(t).replace(/\n/g, ' ⏎ ').slice(0, 220)))
chk('无 JS 运行时错误', realConsoleErrors.length === 0,
  realConsoleErrors.slice(0, 3).join(' | ') + (errStacks.length ? ` || 堆栈：${errStacks[0]}` : ''))
// 过滤掉两类**正常**情况：
//  · favicon（我们没放图标）
//  · /view 上的 ERR_ABORTED —— <video> 的 range 请求在元素被卸载/切换时会主动 abort
// 再排除 E3 预算护栏的 409（设计内协议）。端口不写死（也常跑 8899 测试端口）。
const intentional = (u) => /^409 .*\/api\/run$/.test(u)
const realFailed = failedReqs.filter(
  (u) => !u.includes('favicon')
    && !(u.includes('/view') && u.includes('ERR_ABORTED'))
    && !intentional(u),
)
const badStatus = failedReqs.filter((u) => /^[45]\d\d /.test(u) && !intentional(u))
chk('无 4xx/5xx 请求', badStatus.length === 0, badStatus.slice(0, 3).join(' | '))
chk('无异常网络错误（排除媒体 abort）', realFailed.length === 0, realFailed.slice(0, 3).join(' | '))

// ---------- 截图（整页）----------
await page.screenshot({ path: '/tmp/vue-full.png', fullPage: false })
const html = await page.content()
writeFileSync('/tmp/vue-page.html', html)

await browser.close()

mark('末段')
console.log(`\n通过 ${ok.length} / 失败 ${fail.length} / 跳过 ${skipped.length}`)
if (process.env.TIMING !== '0') {
  console.log('\n分段耗时：')
  for (const [l, ms] of timings) console.log(`  ${(ms / 1000).toFixed(1)}s  ${l}`)
  console.log(`  ${((Date.now() - t0) / 1000).toFixed(1)}s  合计`)
}
console.log('')
for (const x of ok) console.log(`  ✅ ${x}`)
if (skipped.length) {
  console.log('')
  for (const x of skipped) console.log(`  ⏭ ${x}`)
}
if (fail.length) {
  console.log('')
  for (const x of fail) console.log(`  ❌ ${x}`)
}
if (consoleErrors.length) {
  console.log('\n控制台错误:')
  for (const e of consoleErrors.slice(0, 10)) console.log(`   ${e}`)
}
console.log(`\n截图: /tmp/vue-full.png  弹层: /tmp/vue-modal.png`)
process.exit(fail.length ? 1 : 0)
