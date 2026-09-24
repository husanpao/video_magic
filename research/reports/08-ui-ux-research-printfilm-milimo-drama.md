# 三个开源 AI 短剧/漫剧项目的 UI/UX 与工作流交互调研

> 调研对象：`yi1108/printfilm`（2225★，35MB，React19+Vite）、`honolulu0/ai-drama-generator`（4★，React+shadcn+tRPC）、`mainza-ai/milimovideo`（88★，32MB，React+Zustand 自研 NLE）
> 调研日期：2026-09-23　调研员：产品/交互设计调研
> 目的：为「自建 AI 漫剧流水线」的**三栏工作台**（零框架、原生 JS+CSS、单人本地）提取可借鉴的交互设计

---

## 0. 调研方法、证据与可信度分级

| 项目 | 获取方式 | 实际读到的代码 | 可信度 |
|---|---|---|---|
| printfilm | `codeload`/`git clone` 在本机被限速/断连（`fetch-pack: unexpected disconnect`），改用 **jsDelivr CDN 逐文件镜像** 到 `/tmp/research/dl/printfilm/`（307 文件：`frontend/src/**`、`admin/src/**`、`docs/**` 含 10 张截图） | 状态机 `lib/status.ts`、队列面板、任务详情、错误分类、分集编辑器三栏布局与 CSS、分镜表 `pages/studio/StoryboardPage.tsx`、分集列表、资产版本、@ 提及编辑器、字幕板、侧栏预览、设计令牌 `index.css` / `printfilm.css` / `drama.css`、文档 6 篇 | **高**（代码 + 官方设计文档 + 截图三方互证） |
| milimovideo | 同法镜像到 `/tmp/research/dl/milimovideo/`（78 文件：`web-app/src/**` + `docs/**`+ 时间轴截图） | 时间轴全套（VisualTimeline/Track/Clip/Playhead/TimeDisplay/snapEngine/timelineUtils）、分镜卡与场次组、Inspector、SSE/轮询、Store 类型与文档、快捷键实现、ErrorBoundary、ImagesView、ExportModal | **高**（时间轴与快捷键为逐行阅读） |
| ai-drama-generator | `git clone --depth 1` 成功（1.9MB） | 全部 10 个页面 + 布局 + 主题 CSS | **高**（但项目为 Manus 生成的 MVP，功能面窄） |

标注约定：**`file:line`** = 直接证据；**「推断」** = 我根据代码/截图推断而非明文；**「未证实」** = 未找到实现。

**一句话结论**：
- **printfilm** 是三个里**最接近我们要做的东西**——它把「分集 → 分镜 → 生成 → 合成」做成了**四栏工作台 + 底部分镜胶片条**，并且在 `lib/status.ts` 里给出了「**不信任数据库状态字段，按素材完备度推导镜头展示态**」的完整函数（`shotDisplayKind`），这直接解决用户原话「拆镜后每个镜头的内容我也不知道 / 哪一镜没渲看不见」。
- **milimovideo** 提供**真正可移植的 NLE 微观交互**（像素/秒缩放、15px 磁吸、选中才出现的裁剪手柄、拖拽换序的 splice 算法、J/K/L 快捷键、20 步撤销），但它的**重活（SAM3 遮罩追踪、关键帧、多轨波形、漂移校正多视频同步）对我们无用**。
- **ai-drama-generator** 唯一值得偷的是**镜头卡片的字段清单与"状态相关动作按钮"**（有图→重绘、有视频→重生视频、无视频→生成），以及**两段式内联删除确认**；其余（React/shadcn/tRPC/鉴权/S3）与我们约束冲突。它的批量进度条是**假的**（`batchProgress` 只被设为 `{current:0,total:n}` 之后直接置 null，见 `Storyboards.tsx:105-115,187-190`），是个反面教材。

---

## 1. 信息架构（IA）对比

### 1.1 printfilm：顶层三栏导航 + 工作台自有全屏壳

设计文档明文规定（`docs/PRINTFILM_UI_DESIGN_PROMPTS.md:39-51`）：

| 区 | 内容 |
|---|---|
| 左 | Logo + 字标 |
| 中 | 工作台 · 漫剧 · 科普 · 工具 · 资产/历史 · 定价 |
| 右 | 帮助 · 头像 · 主 CTA「开始创作」 |

- **「无左侧边栏」是硬约束**：`NO left sidebar dashboard chrome`（同文件 `:75`），导航居中、激活项 **lime 下划线**（`:106`）。
- **深度工作台允许全屏自有顶栏**，但仍用同一套浅色 token（`:51`）。
- 路由表（`docs/PRINTFILM_UI_ROADMAP.md:9-20`）：`/studio/new`（新建）→ `/studio/:id/style`（风格配置）→ `/studio/:id`（**分镜工作台**）→ `/studio/:id/editor`（成片编辑器）；列表在 `/history`。
- 步骤模型（`lib/dramaProjectSteps.ts:19-35`）：**有剧本走「剧情大纲 → 分镜 → 生成视频」三步；无剧本直接「分镜 → 生成视频」**（跳步由数据推导，不是死板向导）。科普管线是 6 步（`lib/status.ts:211-229`）：选题 → 风格 → 确认分镜 → 画面与配音 → 镜头视频 → 合成预览，且 `image_text`（图文成片）模式**过滤掉「镜头视频」步**（`:224-229`）。
- **深链跳过中间页**（`lib/dramaStoryboardNav.ts:19-32`）：`resolveStoryboardPath()` 直接解析到「首集编辑页」，没有分集就回大纲。这是"少一次点击"的具体做法。

**真实的分集工作台是四栏**（截图 `docs/images/image-20260917-drama-storyboard.png` + `drama.css`）：

```
┌─ 顶栏 52px：‹ 第1集   |  9:16·480p | 3D动画 | 后期字幕 | 无介绍叠字 | seedance-2.0-mini | 尾帧衔接 | ? | AI 重新分镜 ─┐
├─ 状态行 24px：完成 19/19 · 进行中 0 · 失败 0 ───────────────────────────────────────────────────────────────┤
├──────────┬────────────────┬──────────────────────────────────────────────┬───────────────────────────────┤
│ 分集目录  │ 资产面板        │ 中栏：单镜编辑                                │ 右栏：预览 / 画布              │
│ 168px     │ 300px          │ 1fr                                          │ 420px                         │
│ 第1集     │ [本集|全集] [+] │ 片段 01 · 7s              时长 [7] s           │ [预览|画布] [全片合成下载]      │
│ 第1集·19镜│ 角色/场景/道具  │ 顶部关联 chips（图+名）                        │ 大预览图（全屏/下载角标）        │
│           │ 新建角色 / 导入 │ ┌ 编辑框（结构化脚本，内联资产 chip）────────┐  │ 字幕板 ▾ 后期拼接·41条 导出SRT  │
│           │ [资产卡网格]    │ │ [7s] 【配乐】… 【场景】… 【画面·全景】…   │  │                               │
│           │ 已关联 角标     │ └ [编辑] [重新生成] ───────────────────────┘  │                               │
├──────────┴────────────────┴──────────────────────────────────────────────┴───────────────────────────────┤
│ 底部胶片条：[+] [片段01·7s] [片段02·8s] [片段03·8s] …  ← 横向滚动，选中项描边，缩略图下方两行小字 ────────────┤
└───────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```
CSS 证据：`drama.css:4173-4181` `.drama-ep-body{display:grid;grid-template-columns:168px minmax(0,1fr)}`；`:4180-4185` `.drama-ep-workspace{grid-template-columns:300px minmax(0,1fr) 420px}`；右栏是 `.drama-ep-preview`（`EpisodeEditSidePane.tsx:104`）。

> 上图除四栏宽度（CSS 实测）外，顶栏/胶片条的像素值为**截图估算**（原图 2560×1347，按比例折算）：顶栏 ≈52px、状态行 ≈24px、胶片条 ≈96px 高、单项 ≈104px 宽。

> **对我们的直接价值**：我们的"三栏"（左镜头列表 / 中单镜详情 / 右日志+角色+成片）与 printfilm 的 **300 / 1fr / 420** 几乎同构；建议左栏 304px、右栏 400–420px，与它们保持同一量级（这种宽度的好处是：缩略图 96×54 + 文字列还能塞下 22%/30% 的内容列）。

### 1.2 milimovideo：单页工作台 + 顶栏视图切换 + 固定右检查器 + 底部时间轴

`components/Layout.tsx:173-330`：

- 全屏 `bg-[#050505]`，`header` **h-14（56px）**：logo、可编辑项目名、**视图分段控件**（Timeline / Elements / Storyboard / Images / Models）、分辨率角标、撤销/重做按钮、`⌘S to Save` 提示、Export（ghost）+ Save（主色）。
- 主体：`flex-1` 主区 + 右侧 **Inspector 320px**（`InspectorPanel.tsx:169` `w-80`）。
- **视图切换用覆盖层**（`Layout.tsx:285-315`）：storyboard/elements/images/models 用 `absolute inset-0 z-10` 覆盖在 timeline 之上；只有 timeline/elements/images 模式下才显示**底部 h-80（320px）时间轴**（`:317-321`）。
- **每个面板独立 ErrorBoundary**（`:317-328`）：`PanelErrorBoundary fallbackTitle="Inspector"`，一块坏了只坏一块。
- 启动自动恢复上次项目（`:40-52`），失败则 `localStorage.removeItem('milimo_last_project_id')` 并展示空态而不是崩。

**层级模型**：`Project → scenes[] → shots[]`，前端用 `shot.sceneId` 分组，未分配的自成一类 `__unassigned__`（`StoryboardView.tsx:23-58`）。

### 1.3 ai-drama-generator：7 条路由 + 面包屑头，无持久侧栏

`client/src/App.tsx:16-31`：`/`、`/projects`、`/projects/:id`、`/projects/:id/characters`、`/projects/:id/episodes/:episodeId`、`.../storyboards`、`.../video`、`/settings/ai`。
每个页面自己渲染 `header`，头部是 **面包屑 + 返回**：`‹ 返回 | 🎬项目名 / 第N集 / 分镜`（`Storyboards.tsx:227-244`），右上角是**上下文动作**（AI生成分镜 / 合成视频）。
注意：仓库里有一个 `DashboardLayout.tsx`（可拖拽宽度的侧栏，`DEFAULT_WIDTH=280, MIN=200, MAX=480`，localStorage 记忆），**但页面并未使用它**——即"侧栏布局写好了却没接线"。这提醒我们：**别先做侧栏骨架，先做工作台主体**。

---

## 2. 分镜/镜头表怎么呈现（最重要的一节）

### 2.1 三种呈现形态，各解决不同问题

| 形态 | 出现处 | 适合场景 |
|---|---|---|
| **表格**（7 列，`<table>` + `table-layout:fixed`） | printfilm `StoryboardPage.tsx:1009-1200`（科普/短视频管线） | 一眼扫描全部镜头、对比台词/节奏/时长/状态；列宽固定不跳动 |
| **卡片列表**（横向长条 192×144 缩略图 + 右侧信息） | printfilm `a-id-drama` 旧版 / ai-drama-generator `Storyboards.tsx:346-476` | 缩略图空间大、单镜信息多、手机/窄屏友好 |
| **缩略图胶片条 + 分镜卡片网格（按场次分组）** | printfilm 新建分集台底部（截图）/ milimovideo `StoryboardView`+`SceneGroup` | 时序感 + 场景分组 + 快速跳镜 |

### 2.2 printfilm 的镜头表格（最值得抄的结构）

列（`StoryboardPage.tsx:1009-1024`）：**场景 | 画面 | 旁白/台词 | 逐段分镜 | 时长 | 状态 | 操作**

列宽与视觉（`printfilm.css:3991-4062`，全部实测值）：

| 列 | 宽度 | 关键样式 |
|---|---|---|
| 表头 th | — | 12.5px（0.78rem）600 灰字、padding `.55rem .45rem`、`background:#fafbfc`、1px 底边 |
| `.col-no` 场景 | 3rem (48px) | 700 字重、**tabular-nums**、Space Grotesk |
| `.col-thumb` 画面 | 6.6rem (105.6px) | 缩略图 **96×54**（16:9）、`border-radius:10px`；hover `outline:2px solid rgba(182,255,0,.65); outline-offset:1px` |
| `.col-narr` 旁白 | **22%** | `.title` 0.9rem 700 + `.line` 0.84rem #4b5563 **line-clamp:3**，`line-height:1.55` |
| `.col-seg` 逐段 | **30%** | 见下"节奏摘要" |
| `.col-dur` 时长 | 4rem | `mm:ss`（`formatMmSs`，`lib/status.ts:203`） |
| `.col-status` 状态 | 5.6rem (89.6px) | 纯文字 + 图标，三色 |
| `.col-ops` 操作 | 8.4rem (134px) | 文字按钮组，`flex-wrap` |

行样式：表格 14px；**偶数行 `#fafbfc` 斑马纹**；**hover 整行 `#f4fbe0`（极淡青柠）**；`td` padding `.75rem .45rem`、`vertical-align:top`（`printfilm.css:3999-4024`）。

**"哪一镜没渲"在表格里的三重表达**（这是核心答案）：

1. **缩略图格子直接写字**：无图时渲染 `<div class="pf-shot-thumb empty">生成中 / 待出图</div>`（`StoryboardPage.tsx:1060-1068`），灰底渐变 + 居中 11.5px 文字。**位置就是画面位置**，扫一眼就知道缺图。
2. **状态列**：`.pf-shot-status` 三色体系（`printfilm.css:4302-4330`）
   - 完成 → 色 `#3d6500`（深青柠绿）**+ 16px lime 圆底 ✓ 徽标**
   - 未完成 → `.warn` 色 `#8a5a12`（棕琥珀）
   - 失败 → `.bad` 色 `#a1261a`（深红）
   文案来自 `lib/status.ts:157-167` + `i18n/locales/zh/shell.ts:211-222`：`待出图 / 生成中 / 图已生成 / 待出视频 / 视频已生成 / 配音已生成 / 已完成 / 失败`。
3. **动作按钮随状态改名**（`StoryboardPage.tsx:1168-1200`）：`生成画面 ↔ 重绘画面`、`出视频 ↔ 重生视频`；按钮 `disabled` 时用 `title` 说明原因（例：`:1181-1183` `title={!shot.image_url ? '请先生成该镜画面' : undefined}`）。**禁用 + 原因**比灰掉不说话好得多。

### 2.3 「不信任状态字段」的派生状态函数（printfilm 最值钱的一段）

`lib/status.ts:104-167`：

```ts
export type ShotDisplayKind = 'failed'|'generating'|'wait_image'|'wait_video'|'image_ready'|'video_ready'
export function shotDisplayKind(shot, opts?: {pipelineMode?, generating?}): ShotDisplayKind {
  if (opts?.generating) return 'generating'
  if (shot.status === 'FAILED') return 'failed'
  if (!shot.image_url) return 'wait_image'
  const full = opts?.pipelineMode !== 'image_text'
  if (full && !shot.video_url) return 'wait_video'
  if (full) return 'video_ready'
  return 'image_ready'
}
export function shotDisplayDone(kind) { return kind === 'image_ready' || kind === 'video_ready' }
```
文件顶部注释写明了动机：**「分镜表按素材完备度展示，不盲信 shot.status（AUDIO_READY 只代表配音）」**（`:103`）。

配套：
- `isShotGenerating(project, shotId)`（`:192-201`）：从 `project.active_tasks` 里找 `shot_id === shotId && status ∈ {pending,leased,running,awaiting_poll,awaiting_review}` —— **"这一镜是否有在跑的任务"来自任务表，不来自镜头字段**。
- `isProjectWideBusy(project)`（`:177-189`）：区分**整片级任务**（`project_pipeline / project_compose_only / project_regen_audio / shot_regen_audio`）与单镜任务；整片忙时单镜按钮要额外禁用。
- `effectiveStatus(project)`（`:44-68`）：当 `status` 还停在 `SCRIPTING` 但素材已经齐了（worker 延迟/续跑状态滞后），**用素材计数推断真实阶段**（`imgs===n → VIDEOING → COMPOSING`）。文档 `PLACEHOLDER_BACKLOG.md:B-06` 明确说这是 P0「继续生成状态文案纠正」的修复。

> **对用户痛点「哪一镜没渲/不合格/要重渲」**：照抄这个思路即可——**状态 = f(已有资产, 正在跑的任务)**，每次渲染重算，不落库、不缓存。我们要比 printfilm 多一个维度（质检结果），建议扩展为：
> `failed | generating | wait_keyframe | wait_video | wait_audio | video_ready | qc_failed | stale(改了参数需重渲)`。

### 2.4 「逐段分镜」列的节奏摘要（一眼看出这镜内部节奏）

`StoryboardPage.tsx:1090-1150`：从 `segment_script` 解析出 `cues`（字幕/BGM cue 行）与 `beats`，然后：

- cues：`【…】` 去掉括号、截断 28 字，显示第一、二条（如 `字幕：底部居中… · BGM：史诗弦乐…`），0.75rem 灰字；
- beats：**最多显示 4 段**，每段一行：**深色圆角小徽标 `{duration}s`**（`background:#111;color:#fff;border-radius:4px;padding:1px 5px`）+ 文本截断 42 字；超过 4 段显示 **`另有 N 段…`**（`:1130-1134`）。

> 这套"**时长徽标 + 文本 + 溢出计数**"的密度控制，正好可以用在我们的"六段式提示词"折叠头上——折叠状态显示"6 段 · 合计 12s · 前 3 段摘要"。

### 2.5 排序 / 筛选 / 搜索 / 多选

| 能力 | printfilm | milimovideo | ai-drama-generator |
|---|---|---|---|
| 排序 | 镜头按 `shot_no` 排（`shotsByNo`，`status.ts:250`）；**分集列表无排序** | 分镜页按 `index`，可**拖拽换序**（`reorderShotsInScene`） | 无 |
| 筛选 | 项目级：胶囊筛选 全部/进行中/已完成/草稿（`DramaListPage.tsx:72-105`）；`PillFilter` 组件 `role=tablist`;镜头级**无筛选** | 无（用场次折叠代偿） | 无 |
| 搜索 | 项目名搜索（`DramaListPage.tsx:331-337`），胶囊输入框 `min-width:220px; min-height:38px; border-radius:999px`，`:focus-within` → `border-color: color-mix(lime 70%, ink)` + `box-shadow:0 0 0 3px color-mix(lime 28%, transparent)`（`printfilm.css:2871-2890`） | 无 | 无 |
| 镜头多选 | **无**（只有项目级多选） | 无（但有"按场次批量"） | 无 |
| 批量范围 | 「批量调整」弹层里 `全选` + 逐镜 checkbox（`StoryboardPage.tsx:1288-1320`） | 场次组头「Generate All (N)」作用于该组未完成项 | 无 |

> **结论**：三个项目**都没有**"镜头级筛选/搜索/多选"。「按状态筛选镜头」「全选未渲镜头」是**我们要自己设计的部分**（我们在这一点上可以超过它们）。printfilm 的**项目级**多选交互可以直接搬：hover 时浮现 checkbox（`.drama-project-row-check` → `is-visible`，`DramaListPage.tsx:499-506`），选中后**底部浮出选择条**「已选择 N 个项目 | 取消选择 | 删除」（`:519-536`）。

---

## 3. 单镜编辑体验

### 3.1 printfilm：**视图 ↔ 编辑两态** + "聚焦模式"弹层 + 单一真相源脚本

**两级编辑入口**（`EpisodeEditPage.tsx:1521-1600`）：

1. **中栏常驻编辑框**（`.drama-ep-editor-box`，`border-radius:16px; background:rgba(255,255,255,.72); padding:16px; box-shadow:0 1px 2px rgba(15,23,42,.04)`，`drama.css:4460-4475`）内是 **contentEditable 脚本编辑器**；非编辑态是只读渲染，`编辑` / `重新生成` 两个按钮在框右下（截图）。
2. **表格里点单元格 → 弹层，并且带着"你要改什么"的聚焦意图**：`openShotEdit(shot, focus)`（`StoryboardPage.tsx:533-541`）把 `focus` 映射成 **三种编辑模式**：
   - `editMode='narration'` → 标题/副标题/旁白（标题自动聚焦）
   - `editMode='segment'` → 逐段脚本 + @duration chips + 运镜备注 + 时长（`rows=8` 大文本域）
   - `editMode='full'` → 全部字段（`rows=5`）

   同一个弹层，标题随模式变（`编辑镜头 07 · 旁白与标题 / 逐段分镜 / 全部提示词`，`:1373-1381`），**底部横排放着"切到另两个模式"的 ghost 按钮**（`逐段分镜… / 旁白与标题… / 全部字段`，`:1494-1525`），加一个撑开的 spacer 把 `保存/取消` 顶到右侧。**这是"局部编辑但可随时扩展"的最省事做法**，比"一个大弹层塞 12 个字段"好。

**单一真相源 + 双向回填**（防止字段间不一致，`StoryboardPage.tsx:549-587`）：

| 用户改 | 系统同步 |
|---|---|
| 旁白 `patchEditingNarration` | `segment_script` 里的旁白段、`video_prompt` |
| 首帧画面 `patchEditingVisual` | `segment_script` 首段 visual、`img_prompt` |
| 逐段脚本 `patchEditingScript` | 回填 `narration`（`narrationFromScript`）、`img_prompt`（`firstVisualFromScript`）、**`duration` = @duration 合计**（`sumDuration`） |

**每次编辑都有"影响说明"灰字**（`pf-prompt-hint`，`:1386-1416`）：
- 副标题 →「图文成片叠字；**AI 视频成片不烧这行**，只作分镜说明」
- 旁白 →「会同步到脚本旁白段，并影响整片配音（**改完需重新生成配音/成片**）」
- 画面提示词 →「会同步到脚本首段画面，出图与视频都用这一段」

> 这就是「**改完怎么提示需要重渲**」的最低成本答案：**在该字段下面用一行 12px 灰字写清"这一改会影响哪些下游产物"**。我们可以在同一位置放一个 `⚠ 需重渲：视频/配音` 的 chip（推荐，比纯文字更可扫）。

**时长门禁**：编辑时实时显示 chips 行 = 每段时长徽标 + 右侧 `合计 12s / 30s`（超限转 `--pf-danger`）+ 下方红字错误（`:1455-1478`）；`保存` 按钮在 `!editDurationCheck.valid` 时 **disabled**（`:1531`）；后端保存前再校验一次（`saveShot`，`:588-600`，`validateSegmentScriptDuration`）。

**@ 提及编辑器（contentEditable + chip）** —— `lib/dramaEpisodePromptEditor.ts` 给出了一整套**原生 DOM 级**实现，几乎可以照搬：

| 机制 | 证据 |
|---|---|
| token 正则：`/@(asset:\d+|duration:\d+)/g` | `:10` |
| chip 选择器：`[data-mention='true']` / `[data-duration-sec]` | `:8-9` |
| 时长预设常量：`DURATION_PRESET_OPTIONS = [3,4,5,8,10,12,15]`；单镜内容上限 15s、硬上限 30s；单段 3–15s | `:11-21` |
| 创建/插入 chip：`createMentionChipElement()`、`createDurationChipElement()`、`insertMentionChipAtRange(range, chip)`、`insertPlainTextAtRange()` | `:201-311` |
| **点击时长 chip 循环切档**：`nextDurationPresetSeconds(current)` | `:256` |
| 序列化：`serializePromptEditorContent(root)`（`BLOCK_ELEMENT_TAGS = DIV|P` 处理换行） | `:23, 313-347` |
| 反向渲染：`renderPromptEditorContent()`（文本 token → chip DOM） | `:349-386` |
| **光标前文本序列化**（判断是否刚打出 `@`）：`serializePromptEditorContentBeforeCaret` | `:392-414` |
| @ 触发检测：`detectActiveMentionTrigger` / `detectMentionTriggerFromSelection`（从当前 selection 反查） | `:415-454` |
| **光标屏幕坐标**（弹层定位）：`getCaretClientRect()` | `:455-475` |
| chip 内退格删除整块：`deleteAdjacentEditorChip()` | `:83-176` |

配套 UI（`EpisodeEditMentionPopover.tsx`）：`@` 弹层 = 资产列表 + 「插入时长」（显示 **`剩余可用 Ns`**）+ 「景别/运镜」两组词库（`:300-340`，词库见 `dramaCameraLexicon.ts`：`空镜：/远景：/全景：/中景：/近景：/特写：/大特写：/建立镜头：/气氛镜头：` 与 `推镜：/拉镜：/摇镜：/移镜：/跟拍：/俯拍：/仰拍：/航拍：`，文档 `EPISODE_RULES.md:212-221`）；键盘 `↑↓` 移动、`Enter` 选中、`Esc` 关闭（`:136-144, 251-263`）；定位 `top = min(anchor.bottom+8, innerHeight-420)`、`left = clamp(12, anchor.left, innerWidth-360)`（`:161-162`，**portal + 边界夹取**）。

### 3.2 milimovideo：点击就地变输入框 + 300ms 防抖 + 选中才出裁剪手柄

分镜卡（`StoryboardShotCard.tsx`）是三层结构：`header（状态）→ 缩略图（生成遮罩+进度条）→ 动作条 → 可编辑字段 → 底部统计`。

**点击就地编辑**（`:48-68, 307-348`）：
```tsx
const [editingField, setEditingField] = useState<'action'|'dialogue'|'character'|null>(null)
commitEdit(): 若值变了 → setTimeout(()=>patchShot(...), 300) 防抖；setEditingField(null)
<textarea onBlur={commitEdit} onKeyDown={e => e.key==='Enter' && !e.shiftKey && commitEdit()} />
```
非编辑态是 `<p className="cursor-text hover:text-white/80 hover:bg-white/5 rounded px-1 py-0.5">`，空值显示 **`Click to add action...`** 斜体占位（`:324`）。**Enter 提交 / Shift+Enter 换行 / blur 提交**——这套键位值得照抄。

**两段式内联删除确认**（`:75-84, 292-301`）：第一次点 → `confirmDelete=true` 且按钮变红 + `title="Click again to confirm"`，**3 秒后自动复位**；第二次点真删。比 `confirm()` 弹窗轻，比直接删安全。（同文件 `:281` 对"移除视频"仍用了原生 `confirm()`，属不一致。）

**生成中状态画在卡片上**（`:209-231`）：缩略图上覆盖 `bg-black/40 + backdrop-blur-[2px]` 居中一个 **`CANCEL` 按钮**（红 `bg-red-500/20`），底部 **`h-1`（4px）进度条** `width: progress%` + `transition-all duration-300`；`statusMessage` 以 `animate-pulse` 显示在卡头（`:168-172`）。

**动作条随资产状态变形**（`:237-291`）：

| 当前资产 | 按钮 |
|---|---|
| 无视频 | `[🎬 Generate Video]` + `[🖼 Concept Art]` |
| 有概念图无视频 | `[🎬 Generate Video]` + `[↻ 重生成概念图(图标)]` |
| 有视频 | `[↻ Regen Video]` + `[🖼 重生成概念图]` + `[🗑 移除视频]` |

**景别色板**（`:11-23`）：`CU 特写 rose / MS 中景 blue / WS 全景 emerald / EST 建立 amber / INS 插入 purple / TRK 跟拍 cyan`，统一 `bg-{c}-500/20 + text-{c}-400 + 圆角 + 9px 粗体`。

**参数侧栏 Inspector**（`InspectorPanel.tsx:169-260`）：320px 右栏、`bg-[#0a0a0a]`、`border-l border-white/5`；label 全部 **10px uppercase tracking-widest white/40**；Prompt `textarea`（`min-h-[100px]`）、Negative Prompt（`min-h-[60px]`）、**实时演化的提示词 / 最终增强提示词**（可折叠块，`bg-milimo-500/10` + 斜体 + `max-h-[120px]` 滚动，`:213-224`）、`ADVANCED SETTINGS` 可折叠；底部**固定** 生成按钮 `w-full py-4 rounded-xl bg-milimo-500 text-black shadow-lg shadow-milimo-500/25` + `进度百分比` + 红色取消按钮（`:235-260`）。
折叠分节实现（`AdvancedSettings.tsx:28-42`）：`ChevronRight/ChevronDown 12px` + 10px 大写标签 + 展开动画 `animate-in fade-in slide-in-from-top-2 duration-300`。
无选中空态（`:35-44`）：居中大 `Settings` 图标 + `Select a shot to edit parameters` + 下方分隔线后的**Project Settings 摘要**（分辨率/帧率）。

**智能默认**（`ShotParameters.tsx:56-63`）：时长滑块 `numFrames>121`（约 >5s）时**自动勾上 `autoContinue`**（长镜头自动续生成）。滑块样式：轨道 `h-1 bg-white/10 rounded`，**滑块点仅 8px**（`w-2 h-2 rounded-full bg-milimo-500`），数值显示在标题行右侧。

### 3.3 ai-drama-generator：只有一个大弹层

`Storyboards.tsx:499-645`：`Dialog`，`max-w-2xl max-h-[80vh] overflow-y-auto`，字段顺序 = 标题 → （景别/角度/运镜/时长）四列 grid → 动作描述 → 对话 → （配乐/音效）两列 → 图像提示词。
**优点**：景别/角度/运镜是**固定枚举下拉**（`:15-17` `远景/全景/中景/近景/特写`、`平视/仰视/俯视/斜角`、`固定/推/拉/摇/跟/升/降`）—— 少打字、可统计；时长是 `min=1 max=60` 数字框。
**缺点**：没有 diff、没有撤销、没有"改了要不要重渲"的提示、没有脏标记；保存后只 toast `分镜已更新` 并 refetch（`:73-83`）。

---

## 4. 批量操作

| 交互 | 出处 | 机制 |
|---|---|---|
| 全选 + 逐项勾选 + 统一时长（留空不改）+ 可选重配音 | printfilm「批量调整」（`StoryboardPage.tsx:1273-1360`、`425-467`） | 弹层内 checkbox 列表（`max-height:160px` 滚动），**默认全选**（`openBatchAdjust` 里 `setBatchSelected(全部 id)`）；应用时 for 循环逐镜 `PATCH`，最后整体 refetch；空输入校验：`'请设置时长，或勾选重配音'` |
| 分镜 CSV 导出 | 同上 `:57-95` + 按钮 `草稿导出` | `csvEscape` + Blob 下载；**把状态文案一并导出**（用 `shotDisplayLabel`，`:68`）——导出表格里能看出哪镜没渲 |
| **按组批量生成（隐式范围）** | milimovideo 场次组头（`StoryboardSceneGroup.tsx:29-40, 174-186`） | `pendingShots = shots.filter(s => !s.videoUrl && s.status!=='generating')` → 按钮 **`Generate All (N)`**，**仅当 N>0 才出现**，点击 `batchGenerateShots(pendingShots.map(s=>s.id))` |
| 按项目多选删除 | printfilm 项目列表（`DramaListPage.tsx:499-536`） | hover 浮现 checkbox + 底部浮出操作条 |
| 前端集合更新 | printfilm 分集编辑器 | `setEditing({...editing, asset_ids: Array.from(new Set([...keptExtra, ...fromContent]))})`（`:1545-1556`）——**正文里的 @ 引用与 asset_ids 自动同步，删掉 @ 就解除关联**，并给一行说明「已取消本镜关联，资产仍保留在项目中」 |

> **给我们的建议**：批量操作的最小可用集 = ①行首/hover 复选框 ②`全选 | 全选未渲 | 反选 | 全选本章(按章节过滤后全选)` ③底部浮出批量条（`已选 12 镜 → [生成] [重渲] [改时长] [删除] | 取消`）④"按条件一键生成"按钮（文案带数量，如 `生成未渲的 7 镜`）。printfilm 的"全选 + 留空不改"模式适合"改时长"，milimovideo 的"隐式范围 + 计数文案"适合"生成"。

---

## 5. 时间轴 / NLE 交互（milimovideo 深挖 + 可移植性分级）

### 5.1 结构与数值（全部实测）

| 项 | 值 | 证据 |
|---|---|---|
| 轨道数 | 硬编码 3 条：`V1 (Main)` / `V2 (Overlay)` / `A1 (Audio)` | `VisualTimeline.tsx:35-39` |
| 轨道行高 | **h-24 = 96px** | `TimelineTrack.tsx:136` |
| 轨道头部（sticky） | **128px**（`w-32 min-w-[128px]`），含名称 + 静音/隐藏 + 锁定按钮 | `TimelineTrack.tsx:107-132` |
| 缩放 | `zoom = 像素/秒`，默认 **20**，范围 **5–300**；工具栏 range 滑块 + **Ctrl/⌘+滚轮**（×0.9 / ×1.1 每格） | `VisualTimeline.tsx:53, 285-300, 331` |
| 背景网格 | **CSS 渐变**竖线：`backgroundImage: linear-gradient(90deg, rgba(255,255,255,.08) 1px, transparent 1px)` + `backgroundSize: ${zoom}px 100%`，`pointer-events:none; z-20; mix-blend-overlay` | `:372-380` |
| 播放头 | `<Playhead>`：`w-px bg-red-500 z-50 pointer-events-none`，`left: 128 + currentTime*zoom`，`boxShadow:'0 0 4px rgba(255,0,0,.5)'`，顶部 11×12 的五边形 SVG 标记 | `Playhead.tsx:11-27` |
| 时间码 | `m:ss:ff`（**带帧**），`font-mono`，主色 | `TimeDisplay.tsx:12-22` |
| 吸附 | `SNAP_THRESHOLD_PX = 15` → `thresholdSec = 15/zoom`；候选点 = **0、播放头、其它 clip 的 start 与 end**；命中时返回吸附时间并画 **黄色 1px 全高参考线** | `snapEngine.ts` / `VisualTimeline.tsx:383-389` |
| 磁吸 V1 | 轨道 0 的 start **严格顺序拼接**（`v1Time += duration`）；其它轨用绝对 `startFrame` | `timelineUtils.ts:22-45` |
| 时长计算 | `duration = (numFrames - trimIn - trimOut) / fps`，最小 1 帧 | 同上 |
| 拖拽阈值 | 位移 `< 5px` 视为点击，不提交 | `TimelineClip.tsx:43-44` |
| 拖拽反馈 | `whileDrag={{ scale:1.02, zIndex:100, boxShadow:'0 10px 20px rgba(0,0,0,.5)' }}` | `:93` |
| 选中样式 | `border-milimo-500 + shadow-lg + z-20`；未选 `border-white/10 opacity-90 hover:border-white/30` | `:83-88` |
| 裁剪手柄 | **仅选中时出现**，左右各 `w-3`（12px），`-ml-1.5/-mr-1.5` 骑在边缘上，`cursor:ew-resize`，`hover:bg-white/50` | `:130-141` |
| 轨道锁定 | 锁定后轨道 `bg-red-900/5 pattern-diagonal-lines`，drop 直接 return；隐藏 → `opacity:.3` | `TimelineTrack.tsx:37,135-141` |
| 片段内标签 | 左上角 black/50 毛玻璃 pill，`9px font-mono`：`{name} ({numFrames}f)` | `TimelineClip.tsx:126-128` |
| 音频片段 | 波形渲染组件 `AudioClip` + 底部中央 black/40 毛玻璃标签 | `:96-105` |
| 自动保存 | 任何 project 变化 → **2s 防抖** PUT | `VisualTimeline.tsx:11-33` |
| 拖拽中临时时长 | 拖出末端时 `setTransientDuration(endTime+5)` 让画布临时变长，松手清空 | `:33, 62-65` |

### 5.2 三个关键算法（可直接翻译成原生 JS）

**① 拖拽换序（V1 磁吸 + 关联片段跟随）** `VisualTimeline.tsx:169-245`：
```js
// 1) 用"被拖片段的中心"与其它片段中心比较，算出新下标
const myNewCenter = newStartTime + myDuration/2
let newIndex = 0
for (const c of otherClips) if (myNewCenter > c.start + c.duration/2) newIndex++
// 2) 若下标变化：先算新顺序，再算 movedClip 的新 start
const newOrder = [...v1Clips]; newOrder.splice(myIndex,1); newOrder.splice(newIndex,0,movedClip)
let newStart = 0; for (let i=0;i<newIndex;i++) newStart += newOrder[i].duration
const delta = newStart - movedClip.start
// 3) V2/A1 上"中心落在该片段范围内"的关联片段整体平移 delta，最后 reorderShots(myIndex,newIndex)
```
**② 裁剪（trim）** `:113-167`：拖左手柄 → `trimIn += deltaFrames`，上限 `numFrames - trimOut - 25`（保底 1s）；非主轨还要同时把 `startFrame` 右移；拖右手柄 → `trimOut = max(0, trimOut - deltaFrames)`（**只能变短，不能变长**，除非重生成/循环）。`fps` 来自 `project.fps || 25`，所有位置都换算成**帧**存储。
**③ 播放头处分割** `:302-311`：`relativeTime = currentTime - clip.start`，必须 `0 < relativeTime < clip.duration`，`splitFrame = round(relativeTime*fps)` → `splitShot(id, splitFrame)`。
**④ 点击定位** `:248-282`：容器 click → `x = clientX - rect.left - 128 + scrollLeft`，`time = max(0, x/zoom)`；点中 `.clip-content` 则忽略（`closest('.clip-content')` 早退）。
**⑤ 从素材库拖入轨道** `TimelineTrack.tsx:33-99`：`dataTransfer.setData('application/json'|'application/milimo-element', ...)` → drop 时 `time = (clientX - rect.left)/zoom`，`startFrame = round(time*fps)`；并按类型给默认帧数：**音频 250f、视频 121f、图片 49f**（≈2s@25fps）；随后 `addShot` + 50ms 后回填 prompt/thumbnail/conditioning（他们的 `setTimeout(...,50)` 属于 workaround，我们应改成显式 `await`）。

### 5.3 快捷键（`Layout.tsx:62-166`）

| 键 | 行为 |
|---|---|
| `Space` | 播放/暂停；**从暂停开始播放时自动切到 timeline 视图** |
| `⌘/Ctrl+Z` / `+Shift+Z` | 撤销 / 重做（zundo，**20 步**；见 `docs/04_frontend_state.md`） |
| `⌘/Ctrl+S` | 保存 |
| `Delete` / `Backspace` | 删除选中镜头 |
| `J` / `K` / `L` | 标准 NLE 穿梭：J = 暂停并回退 5 帧；K = 暂停；L = 播放（已在播放则前进 5 帧） |
| `←` / `→` | ±1 帧；`Shift+←/→` ±10 帧 |
| `Home` / `End` | 跳到开头 / 末镜结尾 |
| `I` / `O` | 用播放头标记 in/out → 写 `trimIn` / `trimOut` |
| **打字保护** | `if (target.tagName === 'INPUT' \|\| 'TEXTAREA') return`（`:61-62`）——**注意：未排除 contentEditable**，我们内联编辑台词时必须补上（`isContentEditable`） |

### 5.4 可移植性分级（对 AI 生成场景）

| 交互 | 建议 | 理由 |
|---|---|---|
| 像素/秒 zoom + Ctrl 滚轮 + range 滑块 | ✅ **直接移植** | 分镜数量会到几十上百，缩放是刚需；实现只有 ~15 行 |
| CSS 渐变网格 + 1px 红线播放头 + `m:ss:ff` 时间码 | ✅ **直接移植** | 零成本、视觉专业 |
| 15px 磁吸 + 黄色吸附线 | ✅ **移植**（吸附目标改为镜头边界） | 我们拖拽是为了换序/看总长，磁吸让"接缝对齐"可感 |
| 拖拽换序（中心比较 + splice） | ✅ **移植**，但**不需要 V2/A1 关联平移** | 我们只有一条主轨；换序后 `sort_order` 重排即可 |
| 点击轨道定位 + 点击缩略图跳镜 | ✅ **移植** | 与"底部分镜胶片条"是同一件事 |
| 「选中才显示裁剪手柄」 | ⚠️ **改造成"改时长手柄/边界拖拽"** | 我们不裁素材，但**拖右边缘改时长**是自然隐喻；参考 `I/O` 标记键改成"以播放头为界拆分" |
| J/K/L 穿梭 | 🟡 **可移植但降级**：保留 `Space`、`←/→`、`Home/End`；`J/L` 可映射为"上一镜/下一镜" | 生成场景不需要 5 帧步进的双速穿梭 |
| 多轨 V2/A1、音频波形、静音/锁定/隐藏 | ❌ **不做** | 我们只有"全片一条时间轴 + 可选 BGM 一条" |
| 帧级裁剪（trimIn/trimOut）、`numFrames` 帧数模型 | ❌ **不做**（我们改"时长秒数+重生成"） | AI 生成片段不可无损裁剪延长；printfilm 也用秒而非帧（`duration_sec`） |
| 转场、关键帧、波纹删除、多机位 | ❌ **不做**（**milimovideo 自己也没实现转场** —— 全仓库 grep 无 transition 数据模型；printfilm 把「转场效果」列为 P3 待开发 `PLACEHOLDER_BACKLOG.md:E-09`） | 三个项目都没做，说明不是刚需 |
| 多 `<video>` 漂移校正、Safari 静音自动播放 | ❌ **不做** | 我们播成片用单个 `<video>` + 分镜切换定位即可 |

---

## 6. 生成过程的进度反馈

### 6.1 printfilm：右下角全局队列 FAB（**最值得抄**）

`components/drama/DramaGenQueuePanel.tsx`：

- **入口**：右下角圆形按钮 + **badge = 进行中数量**（`:270-280`），busy 时额外 class `is-busy`，只有失败无进行中时 `is-failed`。
- **展开状态存 localStorage**（`drama-gen-queue-fab-open`，`:18, 84-90`），并且**任何新任务入队会自动展开面板**（`subscribeDramaGenQueueOpen`，`:92-97`）。
- **面板头**：`生成队列` + 一行摘要：`` `${active.length} 项进行中` / `${failed.length} 项失败` / `全部完成` ``（`:152-160`）；动作：**取消全部视频任务**（`Octagon` 图标，仅当有视频任务）、**清空已结束**（`Trash2`，仅当有结束项）、关闭（`:163-195`）。
- **列表项**（`:199-257`）：类型图标 + 名称 + 类型标签（角色图/场景图/道具图/素材图/分镜视频）+ **实时 message**；状态文案：`排队中` / **`排队 #N`（FIFO 序号）** / `生成中` / `已完成` / `失败`。失败项**就地展开错误块**：`title` + `message` + `suggestion`（三级文案）+ 可选充值链接；底部提示 `查看原因 / 查看详情`。
- **进行中的进度条是"不确定态"**：`<div class="drama-gen-fab-bar" aria-hidden />`（`:251-253`）→ 纯 CSS 动画，**不假装有百分比**（对比：ai-drama-generator 的 `Progress` 永不前进，是谎报）。

**任务详情**（`DramaGenTaskDetail.tsx`）：
- 标题随状态变：`失败原因 / 任务进度 / 任务详情`（`:137`）。
- 进行中文案兜底（`:56-61`）：`等待调度器领取` / `正在生成分镜视频，完成后会自动更新封面与成片` / `正在生成图片，完成后会自动写回资产`——**告诉用户"完成之后会发生什么"**。
- **根因挖掘**（`:22-134`）：失败时拉取该 `target_id` 的最近 20 条任务，把 `error_message` 与事件消息汇总，用 `pickRootDramaGenError` 选根因；**明确跳过两类噪声**：`cancelled` 且文本含 `跳过重复任务|分镜已生成完成` 的历史任务（`:88-98`），以及被 `重试超过上限` 覆盖的外层错误（`:102-123`）。
- **原始错误可展开**：`查看原始错误 / 收起原始错误` → `<pre class="drama-gen-fab-raw">`（`:184-193`）。**默认人话，想看细节再给原文**，这是错误呈现的最优解。
- 进行中时可轮询，且文档规定 **「轮询刷新 fragments 不得打断正在播放的视频」**（`EPISODE_RULES.md:289`）。

**错误文案体系**（`lib/dramaGenError.ts`，约 20 条模式 → `{title, message, suggestion, billingBlocked?, upstreamAccountBlocked?}`）：

| title | suggestion 摘录 |
|---|---|
| 上游响应超时 | 请稍后重试；若反复失败，检查网络/代理是否能访问上游与后台模型渠道密钥 |
| 参考图疑似真人 | 请在左侧资产中打开「XXX」，重新生成或上传偏动漫/插画的形象后再生成该分镜 |
| 多次生成仍失败 | （给出降级路径） |
| 无法衔接上一镜 | 先修复并重新生成失败的上一镜，再按镜序生成后续片段 |
| 分镜已更新 | 请回到分集页，用当前分镜列表重新点生成；**不要重试旧任务** |
| 文案未通过审核 | 请修改分镜中的敏感表述后重试 |
| 画幅参数不兼容 | 请重新生成该分镜；服务端会按参考图自适应画幅 |
| 参考音频过短 / 无法下载 / 格式不支持 | 指向具体资产与操作 |

> **模式**：`标题（发生了什么）+ 建议（下一步点哪里）`，尽量点名到"哪个资产的哪个按钮"。我们的流水线日志/质检失败完全可以套用这个三元组。

### 6.2 milimovideo：一次性同步 + SSE 实时事件 + 指数退避重连

- **提交**（`stores/slices/shotSlice.ts:243-263`）：先乐观置 `isGenerating:true, statusMessage:'Queued...'` → `POST /shots/{id}/generate` → 成功后 `statusMessage:'Generating...', lastJobId`，**然后 fire-and-forget，进度全靠 SSE**；HTTP 层失败才置 `statusMessage:'Failed'`。
- **批量**（`:363-398`）：`POST /storyboard/batch-generate`，响应是 `[{shot_id, job_id}]`，逐个回填。
- **取消**（`:336-361`）：**先乐观显示 `Cancelling...` 但保持 `isGenerating=true`**，再 `POST /jobs/{id}/cancel`，成功后才落 `isGenerating:false`。
- **刷新页面后的恢复**（`utils/jobPoller.ts` + `SSEProvider.tsx:18-33`）：加载时遍历 `shots.filter(s => s.isGenerating && s.lastJobId)`，**各做一次** `GET /status/{jobId}`（**不是轮询循环**），把 `status/progress/status_message/eta_seconds/video_url/thumbnail_url/actual_frames/enhanced_prompt` 写回；如果还在跑就等 SSE 的下一条事件。
- **SSE 协议**（`SSEProvider.tsx:36-140`）：单一 `EventSource('/events')`，具名事件 **`progress` / `complete` / `error`**（外加 `onmessage` 心跳）；断线**指数退避 1s→2s→…→10s 封顶**，`onopen` 重置计数。
- **片段级显示**：`Shot` 模型里的 `progress / statusMessage / currentPrompt（生成中实时演化的提示词）/ etaSeconds / status`（`stores/types.ts:44-70`）；卡片上是 4px 进度条 + 脉冲文字（见 §3.2），时间轴片段上用 `statusMessage` 覆盖层（`TimelineClip.tsx:117-121`）。

### 6.3 ai-drama-generator：反面教材

- `Storyboards.tsx:105-115` 与 `181-192`：`setBatchProgress({current:0, total:pendingCount})` → 发请求 → 成功后 `setBatchProgress(null)`，**中途没有任何更新点**，进度条恒为 0% 后消失。
- 单镜生成：按钮转圈（`isPending`），**所有镜头共用同一个 mutation**，因此**点一个镜头的"生成图片"，全表格所有按钮都进入 disabled/转圈**（`generateImageMutation.isPending` 被所有卡片共享，`:365, 383`）。这是"每镜独立任务"的经典反例——printfilm 明确要求 **「单镜生成仅锁当前镜按钮；其它镜可编辑、可预览」**（`EPISODE_RULES.md:286`），并且代码里用 **`busyShotIds: Set<number>`** 精确到镜（`StoryboardPage.tsx:133, 1028`）。

> **我们的实现清单**：①每镜一个 `busy` 状态，放在 `Set<shotId>` 或 `shot.state` 上；②`python pipeline.py --serve` 暴露 `GET /api/state`（全量快照，含每镜 `state/kind/quality/paths`）与 `GET /api/events`（SSE）；③前端 **SSE 优先 + 3s 轮询兜底**（本地单进程，退避不需要超过 10s）；④页面刷新后用一次快照恢复（不要重新起任务）；⑤进度条：**能给真实百分比就给**（我们按"第 k/N 镜 + 当前镜内阶段"完全可算），算不出就用不确定态动画，**绝不假装**。

---

## 7. 角色 / 资产 / 抽卡（多候选）UI

### 7.1 资产面板（printfilm 中栏左侧 300px，截图 + `EpisodeEditAssetPanel.tsx`）

- 顶部：**范围分段控件** `[本集 | 全集]`（`.drama-ep-scope`，pill 容器 `padding:2px; border-radius:999px; background:#fff`，按钮 `padding:4px 12px; font-size:12px`，`drama.css:4216-4240`）+ `+` 新建；下面是 `角色 / 场景 / 道具` 三个 tab；两个主按钮 **`新建角色` / `导入`**。
- 网格：2 列；卡片 = 图片 + 名称 + `设置` 小按钮 + **`插入` 按钮**（插入到当前镜脚本，即"这个角色出现在这一镜"）。
- **已关联表达**：被当前镜引用的资产卡片上盖一个 **`已关联` 深色角标**（截图左下角可见）。**这是"定妆照 ↔ 镜头关联"最省事的可视化：不要画连线，直接在资产卡上打标 + 在中栏顶部排 chips。**
- 中栏顶部 **引用条**（`EpisodeEditReferenceStrip.tsx`）：横向 chips（缩略图 + 名称），点 chip → `focusLinkedAsset`（滚动/打开该资产）；正文里删掉 @ 引用会自动 `asset_ids` 同步（`EpisodeEditPage.tsx:1545-1556`）。

### 7.2 抽卡/多候选：**版本历史 + 「还原」**（不是候选面板）

`DramaAssetDetailModal.tsx:259-302` + `lib/dramaAssetImageVersions.ts` + `drama.css:1506-1554`：

- 数据来源：`asset.params.image_versions`（`readAssetImageVersions`）。
- 列表项布局 `grid-template-columns: 56px 1fr auto`；缩略图 **56×56、圆角 10px、`cursor:zoom-in`**（点开 lightbox，`z-index:1100`，高于 dialog 的 1000）；中间是 **版本标签 + 时间**：标签由 `source` 映射 `生成 / 上传 / 被替换 / 还原 / 历史`（`dramaAssetImageVersions.ts:40-48`）；右侧按钮文案 **`还原`**，进行中 `还原中…`。
- 整段列表 `max-height:220px; overflow:auto`；section 头 `历史版本` + `N 个`。
- 视频同理：`activateFragmentVideoVersion(fragmentId, versionId)`（`api/drama.ts:478-489`）返回新的 `video/cover/lastFrameUrl/video_versions`。
- **历史版本预览不落地**（`EpisodeEditSidePane.tsx:64-76, 132-150`）：右栏顶部出现横幅「**预览历史版本 · <标签>**」+ `设为当前` / `退出预览`；预览时用 `overrideVideoUrl` 传给播放器，点"设为当前"才 `activate`。

> **给我们的抽卡设计**：一张资产卡/一镜，下面一排 **56×56 候选缩略图**（当前选中的有 lime 描边 + 1px 环 `box-shadow:0 0 0 1px color-mix(lime 40%)`），hover 显示 `设为当前` / `放大`，列表默认**只展开最近 5 个 + `查看全部 N 个`**。不要做"候选对比弹窗 A/B 并排"——三个项目都没做，我们的候选通常是同一提示词的不同随机种子，**并排不如叠放切换**。

### 7.3 milimovideo 的元素绑定与候选复用

- 分镜卡上挂 **`ElementBadgeRow`**（最多显示 3 个，`maxDisplay={3}`，`StoryboardShotCard.tsx:352-356`），元素带 `confidence` 与 `match_source`（`stores/types.ts:73-80`）——**"自动匹配到的角色"带置信度展示**。
- 场次组头做 **cast 汇总**：去重后 `👤 阿词, 小胖 · 📍 校门口`（`StoryboardSceneGroup.tsx:147-166`）——**一眼看出这场戏有谁**。
- 图片库（`ImagesView.tsx:344-400`）：`grid-cols-2 md:3 lg:4 gap-4`，卡 `aspect-square rounded-xl border-2`；选中 `border-milimo-500 + shadow-lg shadow-milimo-500/20`，hover `border-white/20`；hover 浮现动作，其中一个是 **"把这张图作为下一轮的参考图"**（把 `meta.reference_elements` 回填到 `selectedReferences`，`:368-380`）——**生成→挑一张→当参考再生成**，这是抽卡闭环里最重要的一环。

---

## 8. 加载 / 空 / 错误状态

| 状态 | 做法 | 证据 |
|---|---|---|
| 页面加载 | 全屏居中 spinner（`Loader2 w-8 h-8 animate-spin`） | ai-drama-generator `Storyboards.tsx:216-222` |
| 列表加载 | **骨架屏**：`linear-gradient(90deg,#f3f4f6,#eceff3,#f3f4f6)` + `background-size:200% 100%` + `animation 1.2s ease-in-out infinite`，`min-height:108px` | printfilm `drama.css:3613-3626` |
| 空态 | 大图标 + **标题 + 一句解释 + CTA**；且**文案随前置条件分叉**：有剧本 → 「点击上方按钮使用AI自动生成分镜脚本」；无剧本 → 「请先在剧本编辑页面生成剧本内容」+ 按钮 `前往编辑剧本` | ai-drama-generator `Storyboards.tsx:478-495` |
| 空态（另一形态） | 居中大图标 + `No storyboard yet` + 「Write a script above and click "Analyze Script"」 | milimovideo `StoryboardView.tsx:90-96` |
| 空态（极简） | 右栏没选镜时只一行 `请选择底部分镜` | printfilm `EpisodeEditSidePane.tsx:159` |
| 设计规定的空态 | 「简单线稿插画（单色/lime）+ 中文标题「还没有项目」+ 一行副标题 + lime 按钮「新建」；白色 14px 卡片；**无 emoji、无 3D**」 | `PRINTFILM_UI_DESIGN_PROMPTS.md:252-256` |
| 设计规定的加载态 | 「**细 lime 进度条** + 灰色状态「队列中 · 预计 1 分钟」+ 取消 ghost 链接；**不要紫色 spinner、不要骨架屏混乱**」 | 同上 `:258-262` |
| 面板级错误 | `PanelErrorBoundary`：面板内红框 + 折叠的 `error.message`（10px mono，`max-h-20`）+ **`Retry` 按钮只重置该面板** | milimovideo `components/ErrorBoundary.tsx:47-83` |
| 全局错误 | 全屏红框 + 原始 message + `Reload Application` | 同上 `:22-41` |
| 业务错误 | 顶部 banner（`drama-ep-banner`，12px，错误 `#dc2626`）或 toast | printfilm `EpisodeEditPage.tsx:1447-1452` / `drama.css:4162-4172` |
| 校验错误 | 编辑器下方 `<ul aria-live="polite">` 列出 `error`/`warn` 两级的脚本问题 | printfilm `EpisodeEditPage.tsx:1562-1578` |
| 连续性提示（状态化提示条） | `尾帧衔接` 提示条三态：`is-ready`「上一镜已成片：生成时将自动抽取尾帧作衔接参考」/ `is-wait`「已开启尾帧衔接：<被阻塞原因>」/ 未开启时「分镜会独立并发生成，适合快速批量出片」 | `EpisodeEditPage.tsx:1580-1600` |

---

## 9. 视觉设计：可直接抄的数值

### 9.1 printfilm（浅色 + 青柠，最适合抄成我们的默认皮肤）

`frontend/src/index.css:1-12`（**权威 token**）：

```css
:root{
  --pf-lime:#b6ff00;      --pf-lime-soft:#eefcc8;
  --pf-ink:#111318;       --pf-muted:#6b7280;
  --pf-bg:#f7f8fa;        --pf-card:#ffffff;
  --pf-line:rgba(17,19,24,.1);
  --pf-radius:14px;       --pf-radius-sm:10px;
  --pf-shadow:0 8px 28px rgba(17,19,24,.06);
  --pf-nav-h:64px;
  font-family:'Noto Sans SC','PingFang SC',sans-serif;
  line-height:1.5; font-weight:400;
}
```
设计文档补充（`PRINTFILM_UI_DESIGN_PROMPTS.md:60-83`）：字标用 **Space Grotesk**，中文正文 **Noto Sans SC**；**白卡片 14px 圆角 + `0 8px 28px rgba(17,19,24,.06)` 柔阴影 + 1px `rgba(17,19,24,.1)` 细边**；主 CTA = **实心 lime + ink 文字、无发光**；副按钮 = 深墨实心或 ghost 描边；内容宽 **1200–1440px**；**只要浅色主题**。

| 元素 | 数值 | 出处 |
|---|---|---|
| 焦点环（统一） | `border-color: color-mix(in srgb, lime 70%, ink)` + `box-shadow: 0 0 0 3px color-mix(in srgb, lime 28%, transparent)` | `printfilm.css:2887-2890`、`drama.css:4453-4458` |
| 时长输入 | **56×32**、`border-radius:999px`、居中数字、隐藏数字微调箭头 | `drama.css:4426-4458` |
| 表格 | 14px（0.88rem）；表头 12.5px/600/`#fafbfc`；行 hover `#f4fbe0`；td padding `12px 7px` | `printfilm.css:3991-4024` |
| 缩略图 | 表格 **96×54**（16:9，10px 圆角）；胶片条约 104×? 圆角 12px（截图） | `printfilm.css:4078-4090` |
| 状态色 | 完成 `#3d6500`／警告 `#8a5a12`／失败 `#a1261a`；完成另加 16px lime 圆底 ✓ | `printfilm.css:4302-4322` |
| 工作台网格 | `168px | 300px | 1fr | 420px`；面板底色 `#f8fafc`（目录）/`#f7f7f8`（资产）；编辑框 `rgba(255,255,255,.72)` + 16px 圆角 | `drama.css:4173-4212, 4460-4475` |
| 卡片 | 分镜卡 `padding:16px; radius:14px; gap:14px`；选中 `border-color:lime + box-shadow:0 0 0 1px color-mix(lime 40%)` | `drama.css:1966-1990` |
| 顶栏 | 工作台顶栏 `height:64px; position:sticky; background:rgba(245,245,245,.95); backdrop-filter:blur(8px)` | `drama.css:2013-2030` |
| usage chip | pill、`font-variant-numeric:tabular-nums`、`max-width:280px`、超长省略 | `drama.css:2040-2056` |
| 间距 | 面板 padding 12–24px；卡片间距 14–24px；容器上下 `py-8`（32px） | 各处 |

> 还有一条**极其实用的格式约定**：状态"关于数字"的地方一律 **`tabular-nums`**（场景号、费用、时长），避免跳字（`drama.css:2052`、`printfilm.css:4030`）。

### 9.2 milimovideo（暗色 + 青蓝，适合夜间长时间盯盘）

`web-app/src/index.css` `@theme`：主色阶
`--color-milimo-50 #f0fdff / 100 #e0faff / 200 #baf3ff / 300 #7de8ff / 400 #38d6ff / **500 #06b2eb** / 600 #0090c9 / 700 #0073a3 / 800 #026086 / 900 #064f6e / 950 #04334a`

| 用途 | 值 |
|---|---|
| 根背景 / 顶栏 / 主区 / 轨道头 | `#050505` / `#0a0a0a` / `#0f0f0f` / `#111` |
| 卡片（分镜卡/场次卡） | `#1a1a1a` / `#161616`（头部 `#131313`） |
| 细分隔线 | `border-white/5`（tips: `rgba(255,255,255,.05)`），强一点用 `/10` |
| 主色文本 | `text-milimo-400`（按钮/标题）、`text-milimo-300`（时间码） |
| 玻璃拟态 | `backdrop-blur-xl bg-white/5 border-white/10 rounded-2xl`（`.glass-card` + hover `bg-white/10 border-white/20`） |
| 输入框 | `bg-black/20 border-white/10 rounded-xl p-4 text-sm`，focus → `border-milimo-500/50 + ring-1 ring-milimo-500/50` |
| 滑块 | 轨道 `h-1 bg-white/10 rounded`，**滑块点 8px** |
| 小标签 | **10px + uppercase + `tracking-widest` + `white/40`** |
| 主按钮（生成） | `py-4 rounded-xl bg-milimo-500 text-black shadow-lg shadow-milimo-500/25` |
| 滚动条 | 宽 **6px**、透明轨道、thumb `white/10` → hover `white/20`、圆角 10px |
| 动画关键词 | `animate-pulse`（生成中）、`fade-in slide-in-from-top-2 duration-300`（折叠展开） |

### 9.3 ai-drama-generator（"粗野主义"黑色 + 亮红，**不建议抄**）

`client/src/index.css:62-95`：`--background: oklch(0 0 0)`（纯黑）、`--primary: oklch(.628 .258 29.234)`（亮红，约 Tailwind red-500 量级）、`--radius: 0rem`（**零圆角**）、卡片 `oklch(.08 0 0)`、边框 `oklch(.25 0 0)`。UI 里到处 `border-2 border-white`、`uppercase tracking-wider`、`font-black`。
**问题**：黑底 + 纯白 2px 边框 + 全大写，长时间看密集表格极易疲劳；且与"画面缩略图"（彩色）抢视觉。**唯一可借的**：状态徽标配色 `黄-500/20 + text-yellow-500`（进行中）、`green-500/20 + green-500`（完成）、`blue-500`（剧本完成）、`purple-500`（分镜完成）、`orange-500`（视频完成）（`ProjectDetail.tsx:92-110`）—— **用"阶段色"区分流水线阶段**的想法可用，但要改成我们浅色主题下的低饱和版本。

---

## 10. 成片预览与质检对照

### 10.1 成片播放器：**分段播放器把"全片"与"当前镜"缝合在一起**

printfilm `DramaFragmentSegmentedVideoPlayer.tsx`（用 `playback.segment` 定位，`:145-160`）+ 右栏（截图）：

- 单个 `<video>` 顺序播放所有已生成的镜头；**播放到镜尾自动跳下一镜**（`resolveNextPlayableFragmentId`，`:261-272`），并回调 `onPlayingFragmentChange(nextFragmentId)` → **底部胶片条与中栏自动跟着切**（这就是"预览 ↔ 分镜对照"的实现）。
- 播放器下方是 **全片进度条 = 每镜按 `flex: segment.durationSec` 等比例分段**（`:478-490`），段内有按全局时间算的填充比例 `resolveEpisodeTimelineSegmentFillRatio`。**"分段宽度=时长、可点击任意段跳镜"是一箭三雕**：既是进度条、又是时长分布可视化、又是镜头导航。
- 中栏顶部关联 chips 会随当前镜更新；`字幕板` 折叠在播放器下方，头部显示 `后期拼接 · 41 条` + `导出SRT`（`DramaSubtitleBoard.tsx:44, 60-64`，导出带 **UTF-8 BOM** 便于剪映识别）。
- 合成按钮自身就是进度显示（`EpisodeEditSidePane.tsx:37-42`）：`全片合成下载 → 拉取分镜 3/19 → 服务端统一重编码拼接… → 正在拼接…`；若部分镜未生成，**先弹确认**：`有 3 镜还没有视频，将只拼接已生成的 16 镜。是否继续？`（`:72-78`）。

### 10.2 质检（QC）

- **printfilm 没有逐镜质检/评分的 UI**（全前端 grep 只有 `AUDITING: '审核中'` 这个流水线阶段名，`i18n/locales/zh/shell.ts:202`，以及方法论文案里的"人工质检"）。`lib/status.ts:279` 把 `COMPOSING/AUDITING` 都映射到「合成预览」这一步。
- **milimovideo 也没有质检面板**（无 score/quality 字段；只有 `status: completed/failed`）。
- 唯一接近的是 **`REJECTED: '未通过'`** 这个状态词（`shell.ts:206`）与 `statusTone()` 把 `FAILED/REJECTED/CANCELLED` 统一归为 `bad` 红（`status.ts:75-80`）。

> **结论**：**逐镜质检可视化是三个项目的空白，必须我们自己设计**。建议按 printfilm 已有的三种可视化词汇拼装：
> ① 表格状态列多一档 `qc_failed`（色用 `#a1261a` 红 + `⚠`）；
> ② 缩略图角标（左上角小三角/角标，类似它们的 `已关联`/`封面生成中` 角标，`DramaListPage.tsx:485-495`）；
> ③ 右栏"质检"tab：列出该镜失败项（`aria-live` 列表复用 `drama-ep-script-issues` 的 `is-error / is-warn` 两级样式，`drama.css:4477-4506`）；
> ④ 成片预览时用**分段进度条的段底色**表达质检结果（绿=过、黄=警告、红=不合格）——这是三个项目都没有、但成本极低的做法。

---

## 11. 🏆 值得偷的 Top 10 交互设计 + 原生 JS 实现建议

> 每条给出：**(a) 是什么 (b) 解决我们哪个痛点 (c) 原生 JS 怎么写**。
> 全部只用 DOM API + CSS 变量 + fetch/EventSource，无需构建。

### ① 派生式镜头状态（不落库、每次重算）
- **(a)** `kind = f(有无关键帧图, 有无视频, 有无配音, 是否有在跑任务, 是否失败, 质检结果, 参数是否比产物新)`，映射到 6–8 个字的中文状态词 + 三色 + ✓/⚠。
- **(b)** 直接解决「哪一镜没渲 / 不合格 / 要重渲看不见」；也避免"状态字段与产物不一致"（printfilm 为此专门写了 `effectiveStatus` 兜底）。
- **(c)**
```js
// state.js —— 纯函数，渲染时算
const KIND = {
  failed:{t:'失败',tone:'bad'}, generating:{t:'生成中',tone:'run'},
  wait_keyframe:{t:'待出图',tone:'warn'}, wait_video:{t:'待出视频',tone:'warn'},
  wait_audio:{t:'待配音',tone:'warn'}, video_ready:{t:'视频已生成',tone:'ok'},
  qc_failed:{t:'质检不合格',tone:'bad'}, stale:{t:'需重渲',tone:'warn'},
};
function shotKind(shot, taskSet){                 // taskSet: Set<shotId> 正在跑的任务
  if (taskSet.has(shot.id)) return 'generating';
  const v = latestVersion(shot);                  // shot.versions[shot.active]
  if (shot.last_error) return 'failed';
  if (v && v.qc === 'fail') return 'qc_failed';
  if (v && shot.spec_hash !== v.spec_hash) return 'stale';   // 参数改过 → 需重渲
  if (!v) return 'wait_keyframe';
  if (!v.video && shot.need_video) return 'wait_video';
  if (shot.need_voice && !v.audio) return 'wait_audio';
  return 'video_ready';
}
// CSS：状态只用 tone 决定颜色，文案由 JS 填
// .dot.tone-ok{background:var(--ok)} .tone-warn{background:var(--warn)} .tone-bad{background:var(--bad)} .tone-run{background:var(--run)}
```
- **别忘 stale 这一档**（printfilm/milimovideo 都缺：改了提示词后旧图不会自动标"需重渲"，printfilm 只用一行灰字提示）。实现成本：保存时把 `spec_hash = hash(台词+提示词+时长+景别+运镜+模型)` 记到 shot 和产物上，比对即可。

### ② 表格化镜头清单 + 「缩略图格子里写字」+ hover 才显形的可编辑单元格
- **(a)** `<table>` 固定列宽；无图时缩略图位置直接渲染灰底文字「待出图 / 生成中」；旁白/分镜列是**无边框按钮**，hover 才出现浅色边框与底色，点击进编辑。
- **(b)** 「拆镜后每个镜头的内容我也不知道」→ 表格把 **台词 / 分镜节奏 / 时长 / 状态 / 操作** 全摊平在一屏；占位文字让"缺什么"就在"哪个位置"。
- **(c)**
```html
<table class="shots"><colgroup>
  <col style="width:3rem"><col style="width:6.6rem"><col style="width:22%">
  <col style="width:30%"><col style="width:4rem"><col style="width:5.6rem"><col style="width:8.4rem">
</colgroup><tbody id="shotRows"></tbody></table>
```
```css
.shots{table-layout:fixed;width:100%;border-collapse:separate;border-spacing:0;font-size:.88rem}
.shots th{font-size:.78rem;font-weight:600;color:var(--muted);background:#fafbfc;padding:.55rem .45rem;border-bottom:1px solid var(--line)}
.shots td{padding:.75rem .45rem;border-bottom:1px solid var(--line);vertical-align:top}
.shots tbody tr:nth-child(even){background:#fafbfc}
.shots tbody tr:hover{background:#f4fbe0}            /* 极淡青柠，整行可点感 */
.shots .thumb{width:96px;height:54px;border-radius:10px;object-fit:cover}
.shots .thumb.empty{display:grid;place-items:center;color:#9ca3af;font-size:.72rem;
  background:linear-gradient(135deg,#e5e7eb,#d1d5db)}
.cell-edit{appearance:none;border:1px solid transparent;background:transparent;padding:.25rem .35rem;
  margin:-.25rem -.35rem;border-radius:8px;text-align:left;width:100%;font:inherit;cursor:pointer}
.cell-edit:hover:not(:disabled){border-color:#c5d4a4;background:#f4f7ec}
.shots .no{font-variant-numeric:tabular-nums;font-weight:700}
```
```js
// 事件委托：整个 tbody 一个 listener
rows.addEventListener('click', e => {
  const btn = e.target.closest('button[data-act]'); if(!btn) return;
  const {act, id} = btn.dataset;
  if(act==='edit-narr') openEditor(+id,'narration');
  if(act==='regen')     regen(+id, btn.dataset.kind);
});
```
- 关键比例参考：信息列给 **22% + 30%**，缩略图列固定 ≈106px，操作列 ≈134px，状态列 ≈90px（`printfilm.css:4026-4062`）。

### ③ 底部分镜胶片条（filmstrip）= 我们的"全片时间轴"最小可用版
- **(a)** 横向滚动条，每项 = 缩略图 + 两行小字（`片段 01 · 7s` / `9:16 · 480p`），选中项描边；左端一个圆形 `+` 增镜；点击即切换中栏与右栏预览。
- **(b)** 让"全片时长/顺序/进度"永远在视野内，不再需要滚回顶部找镜头。
- **(c)**
```html
<div class="filmstrip" id="strip"><button class="fs-add" title="新增镜头">+</button></div>
```
```css
.filmstrip{display:flex;gap:10px;overflow-x:auto;padding:10px 12px;border-top:1px solid var(--line);
  background:#fff;scroll-snap-type:x proximity}
.fs-item{flex:0 0 auto;width:104px;border:2px solid transparent;border-radius:12px;padding:4px;cursor:pointer;
  scroll-snap-align:start;background:#fff}
.fs-item img{width:100%;aspect-ratio:16/9;object-fit:cover;border-radius:8px;display:block}
.fs-item.is-active{border-color:var(--lime);box-shadow:0 0 0 2px color-mix(in srgb,var(--lime) 35%,transparent)}
.fs-item .cap{font-size:.72rem;font-weight:600}
.fs-item .sub{font-size:.68rem;color:var(--muted);font-variant-numeric:tabular-nums}
```
```js
// 选中同步：一处状态驱动三栏
function selectShot(id){ state.selected=id; renderAll(); }
strip.addEventListener('click', e=>{
  const it=e.target.closest('.fs-item'); if(it) selectShot(+it.dataset.id);
});
// 键盘：←/→ 切镜（沿用 milimovideo 的 Home/End 语义）
document.addEventListener('keydown', e=>{
  if(isTyping(e)) return;
  if(e.key==='ArrowLeft')  selectShot(prevId());
  if(e.key==='ArrowRight') selectShot(nextId());
});
```

### ④ 「逐段脚本」单一真相源 + 双向回填 + 时长闸门
- **(a)** 一镜只维护**一份带 `@duration:N` 的脚本文本**；改台词/旁白/时长输入框 → 写回脚本；改脚本 → 反算台词、首帧描述、时长合计。时长不合法 → **保存按钮 disabled** + 红字原因。
- **(b)** 避免"台词/提示词/时长三个字段各说各话"，这正是我们"六段式提示词"必须解决的一致性问题。
- **(c)**
```js
const RE_DUR = /@duration:(\d+)/g;
const sumDuration = s => [...s.matchAll(RE_DUR)].reduce((a,m)=>a+ +m[1], 0);
function setScript(text){ shot.script=text; shot.duration=sumDuration(text);
  shot.narration = narrationFromScript(text); shot.keyframe_prompt = firstVisualFromScript(text); renderShot(); }
function setNarration(v){ shot.narration=v; setScript(replaceNarration(shot.script,v)); }
function validate(){ const n=(shot.script.match(RE_DUR)||[]).length, total=sumDuration(shot.script);
  if(!n) return '缺少 @duration 标签';
  const bad=[...shot.script.matchAll(RE_DUR)].map(m=>+m[1]).find(s=>s<3||s>15);
  if(bad) return `单段时长必须在 3–15s（发现 ${bad}s）`;
  if(total>30) return `合计 ${total}s 超过单镜上限 30s`;
  return ''; }
```
- 把 printfilm 的常量抄过来即可：**单段 3–15s（预设 3/4/5/8/10/12/15）、单镜合计上限 15s（可放宽到 30s）、硬上限 30s**（`dramaEpisodePromptEditor.ts:11-21`）。

### ⑤ contentEditable + 内联 chip 的提示词编辑器（含 `@` 弹层）
- **(a)** 正文里把"角色引用""时长"渲染成**小图片 chip**，可点击（点时长 chip 循环切档）、可整块退格删除；打 `@` 弹层选资产/时长/景别运镜。
- **(b)** 「拆镜后每个镜头的内容我也不知道，也不能修改」的最强解：**内容即编辑器**，还能一眼看到这一镜引用了谁。
- **(c)** 骨架（照抄 printfilm 的思路，`:8-23,201-475`）：
```js
const TOKEN = /@(asset:\d+|duration:\d+)/g;
const CHIP = "[data-mention='true']", DURCHIP = '[data-duration-sec]';
const PRESETS = [3,4,5,8,10,12,15];

// 文本 ⇄ DOM（保存/加载各一次，其余靠在 DOM 上改）
function render(host, text){
  host.innerHTML='';
  for(const part of text.split(/(@asset:\d+|@duration:\d+)/)){
    let m;
    if(m = /^@asset:(\d+)$/.exec(part))      host.append(assetChip(+m[1]));
    else if(m = /^@duration:(\d+)$/.exec(part)) host.append(durChip(+m[1]));
    else host.append(document.createTextNode(part));
  }
}
function serialize(host){                       // DOM → 文本
  let out='';
  for(const n of host.childNodes){
    if(n.nodeType===3) out += n.textContent;
    else if(n.matches?.(CHIP))    out += `@asset:${n.dataset.assetId}`;
    else if(n.matches?.(DURCHIP)) out += `@duration:${n.dataset.durationSec}`;
    else if(n.nodeType===1)       out += (n.tagName==='DIV'||n.tagName==='P' ? '\n' : '') + serialize(n);
  }
  return out;
}
function durChip(sec){
  const el=document.createElement('span');
  el.dataset.durationSec=sec; el.contentEditable='false'; el.className='chip chip-dur';
  el.textContent=sec+'s';
  el.onclick=()=>{ const next=PRESETS[(PRESETS.indexOf(sec)+1)%PRESETS.length];
                   el.dataset.durationSec=next; el.textContent=next+'s'; emit(); };  // 点一下切档
  return el;
}
// @ 触发：只序列化光标之前的文本，取最后一个 @ 到光标之间的 query
function mentionQuery(){
  const sel=getSelection(); if(!sel.rangeCount) return null;
  const r=sel.getRangeAt(0).cloneRange(); r.setStart(editor,0);
  const before=r.toString().replace(/\s+$/,'');
  const m=/@([^\s@]*)$/.exec(before); return m? {query:m[1]} : null;
}
function caretRect(){ const r=getSelection().getRangeAt(0).cloneRange();
  const rects=r.getClientRects(); return rects[rects.length-1] || r.getBoundingClientRect(); }
// 插入：保存 Range → 弹层里选 → range.deleteContents(); range.insertNode(chip); 光标移到 chip 之后
```
- 三个必须处理的坑（printfilm 都处理了）：① `contentEditable=false` 的 chip 要能被整块删除（监听 `keydown` Backspace，判断光标前是否紧邻 chip）；② `beforeinput`/`paste` 要 `preventDefault` 并只插入纯文本（否则会粘进外部 HTML）；③ 保存到后端时**存文本 token**，不要存 HTML。

### ⑥ 聚焦式编辑弹层（三个模式 + 字段下方"影响说明"）
- **(a)** 点表格里的旁白 → 打开弹层并**只显示旁白相关字段**；弹层底部有切到"逐段分镜/全部字段"的按钮；每个字段下面一行 12px 灰字写清"改了会影响什么"。
- **(b)** 避免"大表单恐惧"；也让"改完要不要重渲"有地方说。
- **(c)**
```html
<div class="modal" role="dialog" aria-modal="true">
  <h3 id="mTitle">编辑镜头 07 · 旁白与标题</h3>
  <div class="modal-body">…仅渲染该模式的字段…</div>
  <footer>
    <button data-mode="segment">逐段分镜…</button>
    <button data-mode="narration">旁白与标题…</button>
    <button data-mode="full">全部字段</button>
    <span class="spacer"></span>
    <button class="primary" id="save" disabled>保存</button>
    <button id="cancel">取消</button>
  </footer>
</div>
```
```js
// 影响说明直接挂在字段下，并驱动脏标记/重渲提示
const IMPACT = {
  narration:'会同步到脚本旁白段，并影响整片配音（改完需重新生成配音/成片）',
  keyframe_prompt:'出图与视频都用这一段（改完需重渲画面与视频）',
  duration:'按 @duration 合计重算；改完需重渲视频',
};
function markDirty(field){
  shot.spec_hash=null;                                  // → 状态自动变 "需重渲"
  toast(`已修改「${LABEL[field]}」，标记为需重渲`);
}
```
- 弹层尺寸与动效照抄（printfilm `printfilm.css:6825-6854, 4196-4203, 6293-6301`）：
```css
.modal-backdrop{position:fixed;inset:0;z-index:1000;
  background:radial-gradient(120% 80% at 50% 0%, rgba(182,255,0,.08), transparent 55%), rgba(17,19,24,.42);
  backdrop-filter:blur(8px);display:flex;align-items:center;justify-content:center;padding:1rem;overflow-y:auto}
.modal{width:min(560px,100%);padding:1.25rem;border-radius:18px;border:1px solid rgba(17,19,24,.08);
  background:linear-gradient(180deg,#fff 0%,#fbfcfa 100%);
  box-shadow:0 1px 0 rgba(255,255,255,.8) inset, 0 24px 64px rgba(17,19,24,.18);
  animation:panel-in .28s cubic-bezier(.22,1,.36,1)}
@keyframes panel-in{from{opacity:0;transform:translateY(12px) scale(.97)}to{opacity:1;transform:none}}
.prompt-modal{max-width:560px;width:min(560px,92vw);max-height:min(88vh,680px);overflow:hidden;padding:0}
.prompt-modal>.scroll{flex:1 1 auto;min-height:0;overflow-y:auto}   /* 标题固定、内容独立滚动、底部按钮常驻 */
```
  ai-drama-generator 的等价值是 `max-w-2xl (672px) + max-h-[80vh]`。

### ⑦ 右下角全局生成队列 FAB
- **(a)** 圆形按钮 + 数字 badge；展开后是任务列表：`排队 #3 / 生成中 / 失败`，失败项就地展开"原因 + 建议 + 原始错误"；顶部能"取消全部 / 清空已结束"；展开状态记 localStorage；新任务自动展开。
- **(b)** 长任务不再需要盯着日志；失败原因不用翻 stderr。
- **(c)**
```js
// jobs.js：一个 Map + 一次重渲
const jobs=new Map();                            // id -> {title,kind,status,msg,err,createdAt}
function enqueue(job){ jobs.set(job.id,job); if(!open) togglePanel(true); render(); }
const active = ()=>[...jobs.values()].filter(j=>j.status==='queued'||j.status==='running');
const queuedOrder = ()=>[...jobs.values()].filter(j=>j.status==='queued')
                       .sort((a,b)=>a.createdAt-b.createdAt);   // FIFO 序号
function render(){
  fab.classList.toggle('is-busy', active().length>0);
  badge.textContent = active().length || '';
  list.innerHTML='';
  [...jobs.values()].sort((a,b)=>b.createdAt-a.createdAt).forEach(j=>{
    const qi = queuedOrder().findIndex(x=>x.id===j.id);
    list.append(row(j, qi));                     // 行内：状态 = qi? `排队 #${qi+1}` : LABEL[j.status]
  });
}
// 失败行：三级文案 + 可折叠原始错误
function row(j,qi){ const li=document.createElement('li');
  li.innerHTML = `<button class="job is-${j.status}">
     <span class="job-name">${esc(j.title)}</span>
     <span class="job-status">${j.status==='queued'&&qi>=0?`排队 #${qi+1}`:LABEL[j.status]}</span>
     ${j.err?`<span class="job-err">${esc(j.err.title)}</span>
             <span class="job-tip">${esc(j.err.suggestion)}</span>
             <span class="hint">查看原因</span>`:
            (j.status==='running'?'<span class="bar" aria-hidden></span>':'')}
   </button>`;
  return li; }
```
```css
.bar{display:block;height:3px;border-radius:2px;overflow:hidden;background:#e8eaee}
.bar::after{content:'';display:block;width:40%;height:100%;background:var(--lime);
  animation:indet 1.2s ease-in-out infinite}
@keyframes indet{0%{margin-left:-40%}100%{margin-left:100%}}
```
- **能算真进度就算**（`已完成 k/N`），算不出才用上面的不确定态；**绝不像 ai-drama-generator 那样把进度条写死在 0%**。

### ⑧ 错误三级文案 + 从历史挖根因
- **(a)** 每个失败给出 `标题（发生了什么）/ message（具体）/ suggestion（下一步点哪里）`，默认人话、可展开原始错误；并把同一镜的历史任务错误一起看，跳过"跳过重复任务""重试超过上限"这类噪声。
- **(b)** 我们流水线里 90% 的挫败来自"失败了但不知道为什么、也不知道点哪"。
- **(c)**
```js
const RULES = [
  {re:/timeout|超时/i,            title:'上游响应超时',       hint:'稍后重试；检查网络与模型渠道密钥'},
  {re:/真人|real person/i,        title:'参考图疑似真人',      hint:'换一版偏动漫的定妆照后重试该镜'},
  {re:/衔接|last ?frame/i,        title:'无法衔接上一镜',      hint:'先修复上一镜，再按镜序生成'},
  {re:/spec.*changed|已更新/,      title:'分镜已更新',         hint:'用当前分镜重新点生成，不要重试旧任务'},
  {re:/moderation|审核|敏感/,      title:'文案未通过审核',      hint:'修改敏感表述后重试'},
  {re:/OOM|memory/i,             title:'显存/内存不足',       hint:'降低分辨率或减少并发后重试'},
];
function explain(raw){                     // 后匹配优先，取最具体的一条
  for(const r of [...RULES].reverse()) if(r.re.test(raw)) return {title:r.title, message:raw, suggestion:r.hint};
  return {title:'生成失败', message:raw, suggestion:'可稍后重试该镜头；连续失败时更换参考图或简化脚本'};
}
const NOISE = /跳过重复任务|分镜已生成完成|重试超过|retry.*exceed/i;
function rootCause(history){ return history.map(t=>t.error).filter(e=>e && !NOISE.test(e)).pop(); }
```

### ⑨ 筛选 pill + 搜索 pill + hover 复选框 + 底部批量条（+ 带计数的条件批量按钮）
- **(a)** 状态筛选胶囊（`全部 / 待出图 / 生成中 / 需重渲 / 质检不合格 / 已完成`）+ 搜索框；hover 行/卡浮现复选框；选中后底部浮出 `已选 12 镜 [生成][重渲][改时长][删除] 取消`；列表头一个"生成未渲的 7 镜"按钮，**只在有未渲时出现且带数量**。
- **(b)** 满足"一眼看出 + 批量处理"，且不需要后端支持复杂查询（本地全量在内存里 filter）。
- **(c)**
```js
let filter='all', q='', selected=new Set();
const match = s => (filter==='all'||shotKind(s)===filter) &&
                   (!q || (s.narration+s.script).toLowerCase().includes(q.toLowerCase()));
function renderList(){ rows.innerHTML=''; state.shots.filter(match).forEach(s=>rows.append(rowEl(s,selected.has(s.id)))); }
filterBar.addEventListener('click', e=>{ const b=e.target.closest('[data-f]'); if(!b) return;
  filter=b.dataset.f; [...filterBar.children].forEach(x=>x.classList.toggle('active',x===b)); renderList(); });
search.addEventListener('input', debounce(e=>{ q=e.target.value; renderList(); },120));
bulkBtn.addEventListener('click', ()=>{                    // 带计数的条件批量
  const ids = state.shots.filter(s=>shotKind(s)==='wait_keyframe').map(s=>s.id);
  generate(ids);
});
// 底部批量条：selected.size>0 才显示
function syncBulkBar(){ bar.hidden = selected.size===0;
  bar.querySelector('.n').textContent = `已选 ${selected.size} 镜`; }
// 快捷键：ctrl+a 全选当前过滤结果、shift+click 范围选择
```
```css
.pill{border:1px solid var(--line);background:#fff;border-radius:999px;padding:.3rem .8rem;font-size:.8rem;cursor:pointer}
.pill.active{background:var(--lime);border-color:var(--lime);color:var(--ink);font-weight:600}
.search:focus-within{border-color:color-mix(in srgb,var(--lime) 70%,var(--ink));
  box-shadow:0 0 0 3px color-mix(in srgb,var(--lime) 28%,transparent)}
.bulkbar{position:sticky;bottom:0;display:flex;gap:.75rem;align-items:center;
  padding:.6rem 1rem;background:#fff;border-top:1px solid var(--line);box-shadow:0 -6px 20px rgba(17,19,24,.06)}
```

### ⑩ 迷你时间轴（只读浏览 + 点击跳镜 + 拖拽换序，不做剪辑）
- **(a)** 单条主轨的缩略图时间轴：像素/秒缩放、Ctrl+滚轮、CSS 渐变刻度、1px 红播放头、15px 磁吸、拖拽换序、点击定位、`Space/←/→/Home/End` 快捷键。
- **(b)** 底部分镜条升级版：镜头多到 60+ 时，胶片条看不出全长与节奏，时间轴能（而且它同时是"生成进度总览"：未生成的段画灰、生成中的段画脉冲、失败的段画红）。
- **(c)**
```js
let zoom = 20;                                  // px per second，范围 5–300
function layout(){                              // 主轨顺序拼接（磁吸）
  let t=0; return state.shots.map(s=>{ const c={id:s.id,start:t,dur:s.duration}; t+=s.duration; return c; });
}
// 刻度：纯 CSS
// .grid{position:absolute;inset:0;pointer-events:none;
//   background-image:linear-gradient(90deg,rgba(17,19,24,.08) 1px,transparent 1px);
//   background-size:var(--zoom) 100%}
timeline.style.setProperty('--zoom', zoom+'px');
addEventListener('wheel', e=>{ if(!(e.ctrlKey||e.metaKey)) return; e.preventDefault();
  zoom = Math.min(300, Math.max(5, zoom * (e.deltaY>0?0.9:1.1))); render(); }, {passive:false});
// 点击定位（记得减掉左侧标签列宽度 + 加滚动偏移）
track.addEventListener('click', e=>{
  if(e.target.closest('.clip')) return;
  const r=track.getBoundingClientRect();
  if(e.clientX < r.left + LABEL_W) return;               // 128px 标签列
  const t = (e.clientX - r.left - LABEL_W + scroller.scrollLeft) / zoom;
  setPlayhead(t);
});
// 15px 磁吸（候选：0、播放头、其它镜头首尾）
function snap(time){ const th = 15/zoom; let best=time, d=Infinity;
  for(const c of [0, playhead, ...clips.flatMap(c=>[c.start, c.start+c.dur])]){
    const dd=Math.abs(c-time); if(dd<d){d=dd;best=c;} }
  return d<=th ? {time:best, snapped:true} : {time, snapped:false}; }
// 拖拽换序：中心比较 + splice（照抄 milimovideo 的算法，但只用一条轨）
clip.addEventListener('pointerdown', e=>{
  clip.setPointerCapture(e.pointerId); const x0=e.clientX, t0=clip.start;
  clip.onpointermove = ev=>{ const dt=(ev.clientX-x0)/zoom; const s=snap(Math.max(0,t0+dt));
    ghost.style.transform=`translateX(${s.time*zoom}px)`;
    snapLine.hidden=!s.snapped; snapLine.style.left=(s.time*zoom)+'px'; };
  clip.onpointerup = ev=>{
    const s = snap(Math.max(0, t0 + (ev.clientX-x0)/zoom));
    const center = s.time + clip.dur/2;                 // 用中心判断插入点
    let idx=0; for(const c of clips) if(c.id!==clip.id && center > c.start+c.dur/2) idx++;
    reorder(clip.id, idx);                              // → 重排 sort_order，一次性提交全量顺序
  };
});
// 快捷键（打字时不响应）
addEventListener('keydown', e=>{
  if(isTyping(e)) return;                               // INPUT/TEXTAREA/[contenteditable]
  const f = 1/ (state.fps||25);
  if(e.code==='Space'){ e.preventDefault(); togglePlay(); }
  if(e.key==='ArrowLeft')  setPlayhead(playhead - (e.shiftKey?10*f:f));
  if(e.key==='ArrowRight') setPlayhead(playhead + (e.shiftKey?10*f:f));
  if(e.key==='Home') setPlayhead(0);
  if(e.key==='End')  setPlayhead(totalDuration);
  if(e.key==='i'||e.key==='I') splitAtPlayhead();       // 我们的"拆分"语义
});
```
- 关键 CSS 数值：轨道高 **96px**、标签列 **128px**、时间码 **`m:ss:ff`**、播放头 `#e5484d` 1px + `box-shadow:0 0 4px rgba(229,72,77,.5)`、吸附线黄色 1px、片段圆角 8–12px、选中描边 2px。
- **撤销**：不需要 zundo，用**命令栈**即可 —— 每次结构性操作（拆分/合并/删除/换序/改时长）push `{undo, redo}` 闭包，`Ctrl+Z` 弹栈执行 + 重渲；上限 50 步（milimovideo 是 20 步，`docs/04_frontend_state.md`）。文本编辑的撤销交给浏览器原生 `contenteditable` 栈，不要自己抢。

---

## 12. 看起来花哨但不适合我们的

| 功能 | 出处 | 为什么不做 |
|---|---|---|
| **自由画布 / 无限节点画布**（节点、连线、平移缩放、自动保存、历史） | printfilm `pages/drama/canvas/*`（CanvasStore 31KB、canvas.css 23KB）；还专门文档化 | 与"线性分镜流水线"心智冲突；实现成本极高（他们做到 12 万字符 CSS）；我们的镜头顺序天然是一维数组，时间轴/胶片条已足够 |
| **SAM3 遮罩 / 局部重绘 Inpaint / 物体追踪 / 关键帧插值** | milimovideo `Editor/MaskingCanvas.tsx`(19KB)、`TrackingPanel.tsx`(37KB)、`inpaintShot` | 属于"精修"环节；漫剧的核心瓶颈在"分镜可控 + 一致性 + 批量出片"，不在逐帧修图；接入成本远高于收益 |
| **多轨 NLE（V2 叠轨 + A1 音轨 + 波形 + 静音/锁定/隐藏 + 漂移校正的多 `<video>` 同步）** | milimovideo `VisualTimeline`/`TimelineTrack`/`CinematicPlayer` | 我们只有"一条主轨 + 可选 BGM"；漂移校正、Safari 自动播放策略是纯前端播放器难题，本地单机用单个 `<video>` 顺序播更稳 |
| **逐帧裁剪（trimIn/trimOut）与 numFrames 帧模型** | milimovideo 全套 | AI 生成片段**不能无损裁剪/延长**（他们自己也注明"要延长只能重生成或循环"）；我们用"秒 + 重生成"更贴合 |
| **转场、字幕字体工具条（字号/颜色/描边）、关键帧曲线** | printfilm `PLACEHOLDER_BACKLOG.md` S-05/E-08/E-09 全是"待开发"；milimovideo 无转场实现 | **三个项目都没做**。字幕我们用固定模板 + SRT 导出即可（printfilm 的 SRT 导出带 BOM，值得抄） |
| **模板商店 / 风格包 / 付费风格 / 作品社区 / 平台一键分发 / 分享链接+二维码+嵌入** | printfilm `TemplatesPage`、`WorksPage`、`PLACEHOLDER_BACKLOG.md` Y-04/05/06/07 | 单人自用，没有发布渠道需求；真要发布，导出文件手动传就行 |
| **管理后台（85 个文件的 admin 应用：用户/订单/财务/队列/仪表盘/6 个设置面板/图表）** | printfilm `admin/src/**` | 单人本地无多用户，等于零收益 |
| **计费 / 钱包 / 充值 / 定价页 / 用量看板 / 余额不足拦截** | printfilm `components/billing/*`、`PricingPage`、`dramaGenError.billingBlocked` | 我们只有"自己的 API key 花多少钱"，一行文本 + 可选预算上限足矣（成本显示可借"卡片 meta 行"：`¥325.03 · 生图 5 · 生视频 20`） |
| **中英双语 i18n（两套 locale + Proxy 取词 + 语言切换）** | printfilm `i18n/*`、`LanguageSwitch` | 单人中文自用；这套 Proxy + messages 结构还会让"查文案"变绕 |
| **邀请/客服浮标（微信二维码）/帮助中心抽屉/法务文档页** | printfilm `WeChatGroupFab`、`HelpCenter`、`legalContent`(16KB)、`methodLanding`(22KB) | 与工具本身无关 |
| **shadcn/ui 全量组件库 + 组件展示页** | ai-drama-generator `components/ui/*`（60+ 文件）+ `ComponentShowcase.tsx`(1437 行) | 与"零框架"约束冲突；我们只需 8–10 个手写控件（按钮/pill/表格/弹层/进度条/开关/下拉/tooltip/toast/滑块） |
| **可拖拽宽度侧栏 + 移动端适配（useMobile/Drawer/Sheet/响应式断点）** | ai-drama-generator `DashboardLayout`(280px 可拖拽 + localStorage)、`useMobile`、各种 `md:lg:` 断点 | 单人本地桌面固定宽度更稳（printfilm 也是写死 300/1fr/420）；省掉一整类布局 bug |
| **Map 组件 / AIChatBox / S3 预签名上传 / tRPC+Drizzle 全栈** | ai-drama-generator | 与我们 Python 后端重复；本地文件系统直接读写更简单 |
| **`AUDITING` 之类的"阶段状态"直接当 UI 状态** | printfilm `status.ts:10` | 「审核中」在我们场景没有对应动作；状态机应围绕**产物完备度**而非后端步骤名 |
| **假进度条** | ai-drama-generator `Storyboards.tsx:187-190`、`105-115` | 明确的反面教材：设一次 `{0,N}` 就再没更新过 |

---

## 13. 单人本地使用**没必要做**的（明确清单）

1. **登录 / 注册 / 鉴权 / 权限 / 多租户 / 角色**：printfilm 每个页面都要 `RequireAuth`，ai-drama-generator 有 `useAuth` 门禁 + `ManusDialog`；本地 `--serve` 无鉴权。
2. **云端同步 / 多设备 / 团队协作 / 评论 / 审批流 / 项目共享**：printfilm UI 蓝图里"团队空间"是 P3（`PRINTFILM_UI_ROADMAP.md:47`），我们直接砍。
3. **服务端分页**（printfilm 历史页做了服务端分页 `Y-10`）：本地几十个项目，前端一次拉全量 + 前端筛选/排序更简单也更快。
4. **队列去重 / 项目互斥锁 / 多用户任务限额**（printfilm P0「队列去重 / 项目互斥」`PRINTFILM_UI_ROADMAP.md:44`；milimovideo 的 `job_id` 去重与用户级并发上限）：单人单进程下，前端"按钮 disabled + 后端一把锁"就够，不需要调度器级别的语义。
5. **对象存储 / 上传服务 / 断点续传**：直接 `./projects/<name>/` 本地目录 + 静态文件服务。
6. **复杂的进度轮询退避 / 心跳 / 连接恢复**：本地进程间通信，SSE 挂了就 3s 无条件重连即可（milimovideo 的 1→10s 指数退避是给公网的）。
7. **通知中心 / 邮件 / Webhook / 站内信**：任务完成用浏览器标题闪烁或 `Notification` 一次即可。
8. **多分辨率导出、平台封装（抖音/B站）、分享链接、二维码、嵌入代码**：printfilm 一整节都是"待开发 P1–P3"，我们**导出 MP4 + SRT 到本地目录**就完成任务。
9. **模板市场 / 风格商店 / 社区作品流 / 点赞播放量**：需要后端与社交关系，零收益。
10. **i18n、无障碍 aria 全量标注、移动端断点**：可以只保留**低成本高收益**的部分——`aria-live` 用于校验提示与任务列表（printfilm 用了，见 `EpisodeEditPage.tsx:1563`）、`role="dialog"/"tablist"/"listbox"` 用在弹层与筛选上（他们的写法可以直接抄），其余不做。
11. **成本/额度仪表盘（KPI 卡、环比、图表）**：printfilm 设计文档自己把"首屏 KPI 条"列进 **Negative 清单**（`PRINTFILM_UI_DESIGN_PROMPTS.md:90`）；我们只留一行文本用量。
12. **"显示原始 JSON / 事件流"之外的开发者调试面板**：printfilm 的 admin `TaskDetailDialog`（22KB，事件时间线）对我们只有"看原始错误"那一小部分有用，已在 Top 10 第 8 条覆盖。

---

## 14. 附：证据索引（本地镜像路径）

镜像根目录：`/tmp/research/dl/`（`printfilm/`、`milimovideo/`），克隆体：`/tmp/research/honolulu0_ai-drama-generator/`。

**printfilm**
- 设计令牌：`frontend/src/index.css`（`:root` 全部 token）
- 设计规范文档：`docs/PRINTFILM_UI_DESIGN_PROMPTS.md`（IA/Negative/组件 kit/空态与加载文案）、`docs/PRINTFILM_UI_ROADMAP.md`（路由对照 + 视觉约定）、`docs/EPISODE_RULES.md`（层级模型、顶栏参数、@asset/@duration、生成与预览行为、质量门禁）、`docs/SHOT_SPLITTING.md`（两层结构、时长经验值）、`docs/PLACEHOLDER_BACKLOG.md`（**未做清单**：加镜/调序/撤销/重做/字幕样式/转场/应用到全部同类镜头）
- 截图（已用 read_image 逐张查看）：`docs/images/image-20260917-drama-storyboard.png`（四栏工作台 + 胶片条 + 内联 chip 脚本）、`image-20260917-drama-list.png`（项目卡 + 胶囊筛选 + 搜索）、`image-20260917-drama-episode.png`、`image-20260917-drama-assets.png`、`image-20260917-drama-script.png`、`image-20260910-studio.png`
- 状态机：`frontend/src/lib/status.ts`（`shotDisplayKind / shotDisplayLabel / shotDisplayDone / isShotGenerating / isProjectWideBusy / effectiveStatus / statusTone / KEPU_STEPS / formatMmSs`）、`frontend/src/i18n/locales/zh/shell.ts:193-232`（状态词表）
- 队列与错误：`frontend/src/components/drama/DramaGenQueuePanel.tsx`、`DramaGenTaskDetail.tsx`、`frontend/src/lib/dramaGenError.ts`、`lib/dramaGenQueue.ts`、`lib/dramaImageGenQueue.ts`、`lib/dramaVideoGenQueue.ts`
- 分镜表与编辑弹层：`frontend/src/pages/studio/StoryboardPage.tsx`（表格 1009-1200、批量 425-467 & 1273-1360、编辑弹层 1362-1544、CSV 57-95）；样式 `frontend/src/styles/printfilm.css`（表格 3985-4150、状态与操作 4302-4377、搜索 2871-2900）
- 分集编辑器（四栏）：`frontend/src/pages/drama/EpisodeEditPage.tsx`、`EpisodeEditAssetPanel.tsx`、`EpisodeEditSidePane.tsx`、`EpisodeEditHeaderControls.tsx`、`EpisodeEditReferenceStrip.tsx`；样式 `pages/drama/drama.css`（网格 4173-4212、时长输入 4426-4458、分镜卡 1966-1990、版本列表 1506-1554、骨架 3613-3626、校验列表 4477-4506）
- 提示词编辑器与 @ 弹层：`frontend/src/lib/dramaEpisodePromptEditor.ts`、`components/drama/DramaFragmentClipSpec.tsx`、`pages/drama/EpisodeEditPromptEditor.tsx`、`pages/drama/EpisodeEditMentionPopover.tsx`、`lib/dramaCameraLexicon.ts`
- 资产版本 / 抽卡：`pages/drama/DramaAssetDetailModal.tsx`、`lib/dramaAssetImageVersions.ts`、`api/drama.ts`（`activateAssetImageVersion` / `activateFragmentVideoVersion` / `generateEpisode(fragment_ids)` / `saveFragments(全量数组)` / `generateStatus`）
- 播放与字幕：`components/drama/DramaFragmentSegmentedVideoPlayer.tsx`、`DramaSubtitleBoard.tsx`、`lib/composeEpisodeVideoClient.ts`、`lib/dramaEpisodeVideoTimeline.ts`
- 项目列表：`pages/drama/DramaListPage.tsx`、`components/ui/PillFilter.tsx`
- 数据模型：`frontend/src/api/drama.ts`（`DramaFragment{ sort_order, content, cover, video, duration_sec, params, asset_ids }`、`DramaProject.active_tasks`、`DramaEpisode.active_tasks`）

**milimovideo**
- 时间轴：`web-app/src/components/Timeline/{VisualTimeline,TimelineTrack,TimelineClip,Playhead,TimeDisplay,AudioClip}.tsx`、`web-app/src/utils/{snapEngine,timelineUtils,jobPoller}.ts`、`web-app/src/stores/timelineStore.ts`
- 分镜：`web-app/src/components/Storyboard/{StoryboardView,StoryboardSceneGroup,StoryboardShotCard,ScriptInput,ElementBadge}.tsx`
- 检查器：`web-app/src/components/Inspector/{InspectorPanel,ShotParameters,AdvancedSettings,ConditioningEditor}.tsx`
- 布局/快捷键：`web-app/src/components/Layout.tsx:62-330`；`components/ErrorBoundary.tsx`
- 状态与事件：`web-app/src/stores/types.ts`（`Shot` 全字段）、`stores/slices/{shotSlice,projectSlice,playbackSlice,trackSlice,uiSlice}.ts`、`providers/SSEProvider.tsx`、`hooks/{useEventSource,useSSE}.ts`
- 主题：`web-app/src/index.css`（`@theme` milimo 色阶 + glass 工具类 + 6px 滚动条）
- 截图：`assets/milimo_video_timeline.png`（暗色时间轴三轨 + 左素材库 + 右检查器 + 底部 GENERATE SHOT）
- 文档：`docs/04_frontend_state.md`（7 slices / persist localStorage / zundo 撤销 20 步）、`docs/01_system_architecture.md`、`docs/05_execution_flow.md`

**ai-drama-generator**（`/tmp/research/honolulu0_ai-drama-generator/`）
- 路由与页面：`client/src/App.tsx`、`client/src/pages/{Projects,ProjectDetail,EpisodeEditor,Storyboards,Characters,VideoPreview,AIConfig}.tsx`
- 主题（粗野主义）：`client/src/index.css:62-95`
- 布局：`client/src/components/DashboardLayout.tsx`（**未被页面使用**）
- 反面教材：`client/src/pages/Storyboards.tsx:105-115, 181-192, 365, 383`（假进度、共享 pending 锁全表）

---

## 15. 一页纸结论（给实现者的最小行动清单）

1. **状态先做对**：写一个 `shotKind(shot, taskSet)` 纯函数（`failed/generating/wait_keyframe/wait_video/wait_audio/qc_failed/stale/video_ready`）+ 三色 tone + ✓/⚠；**"需重渲"用 `spec_hash` 比对**得到，不靠人记。
2. **镜头清单用表格**（7 列，`table-layout:fixed`，列宽 3rem/6.6rem/22%/30%/4rem/5.6rem/8.4rem），缩略图空位写「待出图/生成中」，行 hover `#f4fbe0`，操作按钮随状态改名 + disabled 时给 `title` 原因。
3. **底部胶片条**（104px 项、两行小字、选中 lime 描边、`+` 增镜），与中栏/右栏共用 `selected` 状态；键盘 `←/→` 切镜。
4. **中栏**：字段下方一行灰字写"改了会影响什么"；`@duration` 合计实时校验，非法就 disable 保存；脚本是唯一真相源，台词/时长双向回填。
5. **提示词分节折叠**：每节头显示「段名 · 时长 · 摘要」，展开后是 textarea + 该节的 `@` 词库按钮（景别/运镜）；`contentEditable + chip` 放到第二阶段做。
6. **右下角队列 FAB** + SSE/轮询 + 三级错误文案 + 原始错误折叠 + 排队序号 + 取消全部。
7. **批量**：hover 复选框 + 底部批量条 + 带计数的条件批量按钮（`生成未渲的 7 镜`）。
8. **时间轴**先只做"只读 + 点击跳镜 + 拖拽换序 + 缩放"，转场/裁剪/多轨一律不做。
9. **视觉**直接抄 token：`--lime:#b6ff00; --ink:#111318; --muted:#6b7280; --bg:#f7f8fa; --card:#fff; --line:rgba(17,19,24,.1); --radius:14px/10px; --shadow:0 8px 28px rgba(17,19,24,.06)`，焦点环 `0 0 0 3px color-mix(in srgb, var(--lime) 28%, transparent)`，数字一律 `tabular-nums`。
10. **不做**：登录/权限/多租户/云同步/计费/管理后台/i18n/移动端/模板商店/自由画布/遮罩重绘/多轨波形/转场。
