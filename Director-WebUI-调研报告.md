# `JYE-HC/Director-WebUI` 功能清单与可借鉴点报告

> 调研对象：<https://github.com/JYE-HC/Director-WebUI>（ComfyUI Registry 发布名 **DirectorDeck**）
> 调研方式：**已成功 `git clone --depth 1`**，本地路径 `/tmp/dw`，commit 取自 `main` 分支（`release-manifest.json` 记录源 commit `6622bae10e6031368860a3dc266776d9a4dff430`，版本 `0.1.0-rc1`，`CHANGELOG` 顶部为 `0.2.0 - Unreleased`）。
> 规模：541 个文件 / 22 MB；后端 Python 约 **117,766 行**（不含内置 raylight fork）；前端 `App.tsx` 单文件 286 KB、`LongFormTimelineWorkspace.tsx` 131 KB。
> 许可证：**GPL-3.0-only**（`LICENSE`，`RELEASE.md` 明确"matching ComfyUI's project license"）。

**证据标注约定**：本文所有结论均标注 `文件:行` 或"README 原文"。凡是**推断**、**未找到**、**无法确认**的地方我都显式写出，不编造。文末有完整证据索引。

---

## 0. 一句话结论

这是一个**工程质量远超同类"ComfyUI 套壳"项目**的导演台：它的核心价值**不在于 UI，而在于它把"一条长片时间线"编译成"N 个互相隔离的原生 ComfyUI prompt"的那套编译器 + 状态机 + 审计体系**。

但对我们最重要的三个发现是：

1. **它没有角色系统。** 全仓库 0 处 `character` / `persona` / `costume` / `角色` / `服装` / `定妆` 概念（grep 已确认，见 §6）。文档自己列在"尚未覆盖"里：*"项目级人物、服装、场景、镜头语言和 speaker continuity bible"*（`docs/unified-timeline-design.md:133`）。**我们已有的"定妆照 + 服装变体"是领先它的能力，不是需要抄它的地方。**
2. **它没有字幕，也没有任何剪辑层。** 字幕/音频轨/转场/响度归一化全部缺失（§6）。**我们的"中文字幕烧录"同样是领先项。**
3. **它的"抽卡"其实很弱。** 它**不生成 N 张候选让用户挑**；它是"每次 job 每段产 1 个 take，靠反复重跑累积 take 历史"，而且文档明确承认*"不会把'最新'自动持久化为用户已接纳版本"*、没有 accepted/stale 生命周期（`docs/unified-timeline-design.md:131-136`）。**我们的抽卡 + 用户挑选机制比它成熟。**

**而它真正值得我们偷的，是下面这批"硬骨头"**：H3 帧格点数学、Autogrow 稠密槽位契约、`MiniMaxH3AddGuide` 尾帧接续的裁剪数学、静音补齐式 concat、ffmpeg 场景切分、模板 Bundle 字节冻结式版本管理、以及"进度阶段加权"模型。

---

## 1. 项目定位：先纠正几个可能的前提误解

| 维度 | 事实 | 证据 |
|---|---|---|
| 部署形态 | **不是独立 Web 服务**。它是纯 ComfyUI 插件，后端 FastAPI **嵌入 ComfyUI 进程**，前端由 ComfyUI 托管在 `/directordeck/` | `README.md`；`CHANGELOG.md` "Rebuilt Director as a pure ComfyUI plugin" |
| 数据库 | 固定 `ComfyUI/user/directordeck/database/directordeck.sqlite3`，**在 ComfyUI 目录下而非插件目录** | `README.md` "数据位置" |
| 前端栈 | **不是原生 JS**。React + TypeScript + Vite（`frontend/package.json`），而你们是 2200 行原生 JS | `frontend/src/App.tsx` 等 |
| 工作流来源 | **代码拼装，不是模板 JSON 文件**。后端从版本化 Python 模板构造 API prompt，**浏览器永远不能提交 workflow / class_type / 节点连线** | `README.md`「执行边界」；`docs/native-workflow-execution.md:5-14` |
| 模型 | MiniMax H3，`BasicGuider` 固定，**产品层面不存在 CFG 和负面提示词** | `docs/architecture.md:106-107`；`schemas.py:756-774` 直接 `pop("cfg")` |
| 帧率 | **固定 24fps**，且 `project.render.fps !== 24` 直接报错 | `frontend/src/domain/timelineProject.ts:3935` |
| 单段时长上限 | 见 §4.3，实际有效上限是 **498 帧 = 20.75 s**（不是 512） | 计算结果 + `native_templates.py:245-252` |
| 多卡 | 自带并维护 raylight fork（`custom_nodes/raylight`，222 文件），专属节点名 `DirectorDeckRay*` | `release-manifest.json`；`native_templates.py:223-233` |

> ⚠️ **一个必须提醒的陷阱**：`docs/unified-timeline-design.md:128-137` 有一节「尚未覆盖的剪辑层能力」，声称 undo/redo、缩放未实现。**但代码里已经实现了**——`App.tsx:1635-1648` 有 Ctrl+Z / Ctrl+Shift+Z / Ctrl+Y，`LongFormTimelineWorkspace.tsx:1984-1998` 有 `+`/`-`/`0` 缩放，`LongFormTimelineWorkspace.tsx:105-106` 定义 `TIMELINE_ZOOM_MIN=12 / MAX=240`。**该文档滞后于代码，引用它时要当心。**

---

## 2. 全部功能模块清单

### 2.1 后端模块总表

| 模块 | 文件 | 做什么 | 对我们有用? |
|---|---|---|---|
| API 主入口 | `backend/directordeck/app.py` (**463 KB / 约 11k 行**) | 全部 FastAPI 路由、dispatcher、reconciler、后台对账、任务生命周期 | ⚠️ 只挑机制，别学单体文件 |
| 数据模型 | `schemas.py` (96 KB) | Pydantic 严格模型 + v1→v5 全部迁移校验器 | ✅ 时间线数据模型可对标 |
| 持久化 | `database.py` (343 KB) | SQLite schema、CAS 状态迁移、审计快照 | ⚠️ 同上，但 CAS 思路值得学 |
| 原生模板编译 | `native_templates.py` (56 KB) | H3 帧对齐、Autogrow 稠密槽、接续 guide、共享 loader 核心 | ✅✅ **最高价值** |
| 模板/特征注册表 | `workflow/templates.py`, `templates_v6.py`, `registry.py`, `feature_definitions.py` | 声明式模板 + 资源读写 DAG + Bundle 版本冻结 | ✅✅ 版本管理思路 |
| 工作流片段解释器 | `workflow/interpreters/*.py` (16 个) | 每个特征一段发射函数（`emit_standard_sampling` 等） | ✅ 解耦范式 |
| V4/V5/V6 编译器 | `workflow/v4_compiler.py`, `v5_registry.py`, `v6_projection.py` 等 | 多代并存的编译管线 | ⚠️ 过度设计，见 §6 |
| 图审计 | `workflow/audit.py` (59 KB), `node_contracts.py` (55 KB) | 逐节点白名单 + 晚绑定 JSON Pointer 校验 | ✅ 节选 |
| 提交规划 | `execution/submission.py` (35 KB) | `LockedSubmissionPlanner`，端点锁下的单段提交波次 | ✅ 队列语义 |
| 进度 | `progress.py` (50 KB) | WebSocket `execution_start`/`executing`/`progress` → 阶段+step | ✅ **加权阶段模型** |
| 媒体 | `media.py` (24 KB) | ffprobe、24fps 不可变代理、场景切分、concat 组装 | ✅✅ |
| 插件宿主 | `plugin/__init__.py` (59 KB) | 宿主能力探测、object_info 契约匹配、逻辑 GPU 清单 | ✅ 能力探测思路 |
| RayLight | `custom_nodes/raylight` (222 文件) | 多卡 fork + 8 个 `DirectorDeckRay*` 节点 | ❌ 单卡无需求 |
| 迁移 | `migrations/*.py` | timeline v4→v5、settings v2→v3 等 | ⚠️ 参考 |
| 能力预检 | `capabilities/preflight.py`, `evaluator.py`, `catalog.py` | 提交前 node/model/device/topology 全量校验 | ✅ |
| 工具 | `tools/validate_native_comfy_prompts.py` (40 KB) | **CPU-only** prompt 结构验证脚本，golden 文件对比 | ✅✅ 可复刻 |

### 2.2 前端模块总表

| 模块 | 文件 | 做什么 |
|---|---|---|
| 应用外壳 | `App.tsx` (286 KB) | 侧栏/顶栏/任务抽屉/设置页/全局状态机 |
| 时间线工作区 | `LongFormTimelineWorkspace.tsx` (131 KB) | 资产库 / 主预览 / 时间线 / 分段检查器 **四区合一** |
| 领域逻辑 | `domain/timelineProject.ts` (187 KB) | 全部编辑操作 + reducer + 校验 + 迁移 |
| 撤销栈 | `state/timelineHistory.ts` (54 KB) | **patch 式历史**，合并窗口 + 字节预算 |
| 持久化 | `state/timelinePersistence.ts` (44 KB) | 服务端权威 + 本地 WAL 信封，rev 感知 |
| API 客户端 | `api/client.ts` (127 KB), `api/types.ts` (48 KB) | 类型化客户端 |
| 任务抽屉 | `components/TaskDrawer.tsx` (60 KB) | 任务列表/过滤/排序/取消/导出诊断/导入输出 |
| 设置页 | `components/SettingsPage.tsx` (69 KB) | 模型绑定、设备放置、多卡、ffmpeg 安装 |
| i18n | `i18n/locales/zh-CN.json` | **中文优先**，且是极好的功能索引（见 §2.3） |
| 样式 | `styles.css` (169 KB) | 深色 + Claude 风暖色浅色双主题 |

### 2.3 从 `zh-CN.json` 反推出的完整用户可见功能（高置信）

`frontend/src/i18n/locales/zh-CN.json` 只有 174 行，但信息密度极高，可直接当功能清单用：

- **输出规格**：画幅、分辨率、导出方式（`组装完整视频` / `输出独立片段`）— `zh-CN.json:9-17`
- **双模型族独立采样**：FL2VA / Ref2VA 各自一套 `步数 / Seed / 随机 / 采样器 / 调度器 / Video Shift / Audio Shift / LoRA / LoRA 强度` — `zh-CN.json:18-40`
- **推理加速**：`Comfy Kitchen Attention`（CK Attention）开关，带 7 种 reason code 的能力状态机（`checking/available/unavailable/unknown/legacy`）— `zh-CN.json:44-70`
- **六配方中文名**：文生视频 / 图生视频 / 首尾帧视频 / 参考生视频 / 视频重绘 / 参考视频重绘 — `zh-CN.json:106-111`
- **预检错误分类法**（约 30 条结构化错误 + 每条都有 remediation 文案），例如 `segment_frame_limit_exceeded`（超 512 帧）、`ref2va_input_required`、`historical_take_geometry_mismatch`、`node_unavailable`、`lora_strength_invalid` — `zh-CN.json:119-165`
- **预检成功面板**：显示 `{visible}f 可见 / {sample}f 采样 · seed {seed}`、接续上下文帧数、对齐尾帧数、锚点重置、派生配方、take 编号 — `zh-CN.json:78-105`

---

## 3. 重点专题深挖

### 3.1 长片 / 多镜编排：50+ 镜头怎么管

#### 核心模型：父任务 + 每段一个独立 child prompt

> README 原文：「一次时间线提交产生一个父任务，并为每个所选分段建立一个独立原生 child prompt。各 prompt 使用稳定的模型、CLIP、VAE loader 节点 ID/输入，让 ComfyUI 可以跨 prompt 复用缓存，同时把失败、取消和解码中间值限制在单段。」

**关键设计决策：Director 自己不并发。** 它把所有 child 作为**独立 prompt 提交给 ComfyUI 队列**，靠 ComfyUI 自身的队列串行化：

```python
# backend/directordeck/app.py:7721-7723
# ComfyUI's normal queue serializes these one-segment prompts. Stable
# loader ids/inputs permit endpoint-local cache reuse without putting
# 128 independent sampling/decode branches in one failure domain.
```

**为什么不合成一张大图？** 注释说得很清楚：**不让 128 个采样/解码分支落进同一个失败域**。这是单卡 5090 场景下非常正确的取舍 —— 一张大图 OOM 就全崩，N 张小图 OOM 只崩一段。

#### 提交顺序

`docs/native-workflow-execution.md:25-26`：
- **关闭接续**：`Standard → RayLight`，组内 `FL2VA → Ref2VA`，组内保持时间线顺序
- **开启接续**：**严格按时间线逐段提交**，不允许跨越前驱依赖重排

#### 上限与边界

| 项 | 值 | 证据 |
|---|---|---|
| 时间线最大分段数 | **128** | `schemas.py:1138` `max_length=128` |
| 启用段总帧数 | ≤ **100,000** | `timelineProject.ts:3957` |
| 单段帧数 | ≤ 498（见 §4.3） | `native_templates.py:248` |
| 每轮对账 queue 补查 | **最多 16 项**，轮转窗口 | `docs/architecture.md:134-137` |

#### 后台对账：反请求风暴设计（很值得学）

`docs/architecture.md:132-137`：
> 「每个后台 reconciliation pass 使用**一次** queue 快照和**一次**受限 bulk-history 读取。queue 可用时，最多 16 个缺失 prompt ID 被精确查一次，窗口在后续轮询中轮转；**queue 不可用时，bulk positives 可以推进 children，但"缺席"绝不被当作取消**。」

以及 `README.md`：
> 「HTTP job list/detail 读取**仅读 SQLite**，对 parent/child 和历史 legacy 行都是如此；因此一个断连或黑洞 ComfyUI 端点**无法冻结任务面板**。」

这条对我们非常有用：我们的 Web 控制台如果每次轮询都打 ComfyUI `/history`，ComfyUI 一卡整个控制台就卡死。

#### 重启恢复

`docs/architecture.md:146-154`：
- lifespan 启动时**只做一次本地事务**（把死进程的 submission ownership 变成 restart-recovery marker），然后立刻 yield（不阻塞服务可用性）
- 另起一个**有界、可取消**的 recovery worker，对每个 caller-assigned prompt ID 走原子 cancel API
- **未确认的 cancel 保持 recovery-owned 并重试**，不能因为 queue/history 瞬时缺席就关闭
- 同一 endpoint 的新 dispatcher 必须**等待所有 recovery-owned 的旧 child**（含旧 Standard prompt）拿到精确取消或终态证明 —— 防止迟到的 `/prompt` 越过新的 Ray/Standard 切换顺序

> **对比我们**：我们的"渲染检查点断点续跑"是**基于 manifest 指纹的重新计算**。Director 的做法更保守但更严谨：它不假设"没在队列里 = 没在跑"，而是**先定向取消再重提交**。我们如果在 ComfyUI 重启后只靠指纹判断，可能出现"以为没跑、其实旧 prompt 迟到入队"的重复写。**具体建议见 Top 10 第 6 条。**

### 3.2 镜头级编辑：能改什么、能拆什么

#### 每段可编辑字段（`backend/directordeck/schemas.py:1002-1039`）

**公共字段**（`ShotBase`, `schemas.py:784-793`）：
- `id`（1-128 字符，稳定 UUID）
- `title`（≤256）
- `prompt`（**≤7000 Unicode 字符**）
- `duration_seconds`（FL2VA: 0.1–120；Ref2VA: 0–86400，因为要先铺源视频）
- `enabled`（启用/停用）

**FL2VA 专属**（`schemas.py:1002-1010`）：
- `first_image` / `last_image`（**可空**，非空时必须是 image 类型）
- `continuity: {enabled: bool, overlap_frames: 5|22|39|56}`
- `ref_image_size: "match"|"max"`
- `audio_mode: "generate"|"source"|"mute"`

**Ref2VA 专属**（`schemas.py:1022-1039`）：
- `source_video` + `source_start_seconds` + `source_duration_seconds`（源视频裁剪区间）
- `source_audio_as_reference`（是否把源音轨当参考音）
- `reference_images[]` / `reference_videos[]` / `reference_audios[]`（**带 slot 的 SlottedAssetReference**）
- 同上 `continuity` / `ref_image_size` / `audio_mode`

#### 完整的编辑操作词表（`timelineProject.ts:1772-1836` reducer action 全集）

```
segment/insert              插入空段（before/after，可选 mode）
segment/insert-video        从视频素材插入（自动铺满源时长）
segment/insert-videos       批量插入多个视频
segment/move                拖动排序
segment/delete-selected     删除所选
segment/merge-selected      合并连续所选段
segment/split-selected      在播放头处分割
segment/apply-source-cuts   按帧列表把源视频段切成 N 段
segment/split-evenly        均分切成 N 段
segment/duplicate-selected  复制所选（Ctrl+D）
segment/set-enabled         批量启用/停用
segment/set-continuity      改接续开关与 overlap_frames
segment/set-source-range    改源视频裁剪区间
segment/set-source-audio-reference  切换源音轨参考
segment/replace             整体替换
segment/bind-asset(s)       绑定素材（可批量）
segment/reorder-reference   拖动重排参考槽位
segment/set-mode            切换 FL2VA ↔ Ref2VA（重建字段）
segment/insert-reference-token  插入 <Picture N> 等标签
segment/insert-subject-token    插入 <subject N> 标签
segment/apply-config        ★ 配置批量传播（见下）
```

**四个关键函数**：
- `mergeSelectedSegments` — `timelineProject.ts:1294`。合并 prompt 用 `\n\n` 拼接、时长求和、Ref2VA 时源区间也求和
- `splitSelectedSegment` — `timelineProject.ts:1352`。**按播放头比例分割**，Ref2VA 时同时按比例切 `source_start_seconds` / `source_duration_seconds`（**很细**）
- `splitTimelineSourceSegmentAtCuts` — `timelineProject.ts:1405`。把整段源视频按 ffmpeg 场景切点一次性切成 N 段
- `splitTimelineSourceSegmentEvenly` — `timelineProject.ts:1477`

#### ★ 最有价值的编辑功能：`apply-config` 配置批量传播（`timelineProject.ts:1629-1672`）

```typescript
// timelineProject.ts:239-260
export interface TimelineSegmentCopyOptions {
  mode, duration, continuity, audioMode, refImageSize,
  prompt, promptReferences, features
}
export const DEFAULT_TIMELINE_SEGMENT_COPY_OPTIONS = {
  mode: true, duration: true, continuity: true,
  audioMode: true, refImageSize: true,
  prompt: false, promptReferences: false, features: false,  // ← 默认不覆盖提示词
};

// timelineProject.ts:1629
export function applyTimelineSegmentConfiguration(
  state, sourceId,
  scope: "following" | "selected",   // ← 应用到"后续所有段"或"所选段"
  options: TimelineSegmentCopyOptions,
)
```

**为什么精妙**：它把"批量设置时长/接续/音频策略"和"批量覆盖提示词"**在语义上分开**了，默认**不覆盖 prompt**（因为 prompt 是每段唯一的创作内容）。而且 `prompt` + `promptReferences` 必须**同时勾选**才生效（`:1590`），因为改了 prompt 就必须同步重绑素材标签，否则会产生非法 `<Picture N>` 引用。

还有一条细节规则（`:1636-1640`）：复制后如果目标段是 FL2VA 且有 `first_image`，**自动关闭 continuity** —— 因为显式首帧锚点本身就是接续边界，两者语义冲突。

#### 素材标签自动重写（`timelineProject.ts:3648-3712`）

`segmentReferenceTag()` 从**素材对象**反推它在该段提示词里的合法标签：
- FL2VA：`[first_image, last_image]` 按顺序压密成 `<Picture 1>` / `<Picture 2>`
- Ref2VA 源视频：固定 `<Video 1>`
- Ref2VA 参考图/音/视频：`<Picture {slot+1}>` / `<Audio {slot+1+offset}>` / `<Video {slot+1+offset}>`，其中 audio 的 offset 在 `source_audio_as_reference` 开启时为 1

`setSourceAudioAsReference()`（`:3702`）在切换源音轨参考时，会**批量重写提示词里所有 `<Audio N>` 标签**（`:3688-3698` 的 `rewriteAudioReferenceLabels`），因为官方节点的 audio 编号会整体后移一位。

**这个"标签-素材双向一致性"机制非常值得偷。** 我们的参考图挂载如果是手工写 `ref_image_0`，用户在中间插一张图就会全错位。

#### 提示词自动完成

`LongFormTimelineWorkspace.tsx:1221` 的 textarea placeholder 原文：
> "描述动作、构图、运镜与声音；输入 **@** 选择主体，输入 **#** 选择当前片段已引入的素材…"

`@` → `<subject N>`（**且必须已在 `subject_definitions` 段里定义过**，`timelineProject.ts:3519-3522` 严格校验）
`#` → `<Picture N>` / `<Video N>` / `<Audio N>`（**且必须在当前段已绑定**，`:3514`）

带键盘导航（`:1240-1252`：ArrowUp/Down/Enter/Escape）、ARIA combobox 语义（`:1223-1227`）。

### 3.3 提示词工程：H3 结构真相（★ 最反直觉的一节）

#### 发现 1：**FL2VA 和 Ref2VA 用的是两套不同的段落结构**

**Ref2VA = 六段式**（`timelineProject.ts:531-543`）：
```
subject_definitions:

summary:

retention_analysis:

detailed_description:

overall_soundscape:

non_diegetic_music:
```

**FL2VA = 三段式**（`timelineProject.ts:582-588`）：
```
integrated_multimodal_description:

overall_soundscape:

non_diegetic_music:
```
**没有** `subject_definitions` / `summary` / `retention_analysis` / `detailed_description`。测试里显式断言 `expect(t2v).not.toContain("subject_definitions:")`（`frontend/src/test/timelineProject.test.ts:152`）。

> **对我们的意义**：你们现在的 H3 六段式如果是**所有模式统一**的，那对 FL2VA 是错的。FL2VA 应该用 `integrated_multimodal_description` 单段主体。

#### 发现 2：FL2VA 的空骨架**自带首行对齐指令**（原文英文，来自 H3 base 规范）

`timelineProject.ts:590-598` 三个常量**逐字**：
```typescript
// I2V（仅首图）
"For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced."

// FL2V（首尾俱全）
"How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; Picture 2 (from Shot N) aligns with the S.SS-second mark of the target video."

// L2V（仅尾图）
"How the reference pictures align with the target video — <Picture 1> (from [Shot N]) aligns with the S.SS-second mark of the target video."
```

注意 `S.SS` 是**留给用户填的占位符**，框架不替你算时间戳。且 L2V 时尾图**被重编号为 `<Picture 1>`**（因为只有一张图，压密）。`promptSkeleton()`（`:604-614`）按 recipe 分派这四种骨架。

> ✅ **这是可以直接抄的字符串**。如果你们的 H3 调用里没有这几句对齐指令，首尾帧控制力会明显弱。

#### 发现 3：★ **没有 CAMERA/POSITION DISCIPLINE**

我对整个仓库做了大小写不敏感的全量搜索：

```bash
grep -rni "discipline" /tmp/dw    # → 0 命中
```

**结论：这个项目完全不存在"纪律行"（CAMERA DISCIPLINE / POSITION DISCIPLINE）机制。** 无论是文档、代码、测试、fixture 都没有。

> 说明：你们提到的"纪律行"如果来自 H3 官方 spec 或别的社区工作流，Director **没有实现它**。所以这一块**不能从它这里偷**，得自己搞（或者说明它确实不需要 —— 无 CFG、BasicGuider 的路径下，纪律行可能是某些社区模板的补丁做法而非官方要求）。**这一点我无法从本仓库判断优劣，只能报告"它没有"。**

#### 发现 4：★ **后端根本不校验提示词结构**

我搜遍了后端：
- `backend/directordeck/schemas.py:787` 只有 `max_length=MINIMAX_H3_PROMPT_MAX_CHARACTERS`（=7000）
- 没有任何 `subject_definitions` / `six_section` / 段落结构校验（grep → 0 命中后端）
- `native_templates.py:1289` 直接 `prompt = segment.prompt.strip()` 塞进节点的 `prompt` 输入
- 后端出现的 `_validate_prompt_shape`（`workflow/audit.py:169`）校验的是 **ComfyUI API prompt 的图结构**，不是 H3 文本提示词

**所以：六段式/三段式骨架纯粹是前端"填入框架"按钮的便利设施（`LongFormTimelineWorkspace.tsx:1204-1210` 的"填入框架"按钮），没有任何强制力。** 用户可以把骨架删光写一段大白话，照样提交。

> 🎯 **这是一个明确的超越点**：我们的 DeepSeek 拆镜流水线**可以而且应该在服务端强制校验段落结构**（哪些段必须有、顺序、`subject_definitions` 与正文 `<subject N>` 的引用一致性、`overall_soundscape` 非空等），把这些做成像 §2.3 那样的**结构化错误码 + remediation 文案**。Director 没做，我们做了就是真的更稳。

#### 发现 5：★ H3 帧数格点 `17k+5`（**直接可用的硬核常量**）

`native_templates.py:245-257`：
```python
def _align_h3_frame_count(raw_frames: int) -> int:
    frames = max(5, raw_frames)
    frames += (5 - frames % 17) % 17
    if frames > 512:
        raise NativeTemplateError(
            f"segment compiles to {frames} frames; native H3 template limit is 512"
        )
    return frames

def _align_h3_frames(duration_seconds: float, fps: float) -> int:
    raw = max(5, int(round(duration_seconds * fps)))
    return _align_h3_frame_count(raw)
```

前端有**逐字对应的实现**（`frontend/src/domain/timing.ts:3-30`），并把常量**显式命名**：
```typescript
export const H3_FRAME_STRIDE = 17;
export const H3_FRAME_OFFSET = 5;
export const H3_MAX_SHOT_FRAMES = 512;
/** Align an already-counted frame request to MiniMax H3's 17k+5 lattice. */
```

**我实测算出的边界（重要）**：

| 请求帧数 raw | 对齐后 | 结果 |
|---|---|---|
| 5 | 5 | ✅ |
| 22 | 22 | ✅ |
| 39 | 39 | ✅ |
| 56 | 56 | ✅ |
| 498 | 498 | ✅ |
| **499 ~ 511** | **515** | ❌ **抛错** |
| 512 | 515 | ❌ **抛错** |

→ **单段有效最大帧数其实是 498（= 20.75 s @24fps），不是 512。** 512 这个数字在 UI 文案里出现（`H3_MAX_SHOT_FRAMES = 512`），但因为它不在 `17k+5` 格点上，请求 512 会被向上取整到 515 然后拒绝。

**你们 3–15 s 的镜头换算**：
| 目标 | raw | 对齐帧数 | 实际时长 |
|---|---|---|---|
| 3 s | 72 | 73 | 3.042 s |
| 5 s | 120 | 124 | 5.167 s |
| 8 s | 192 | 192 | 8.000 s |
| 10 s | 240 | 243 | 10.125 s |
| 15 s | 360 | 362 | 15.083 s |

> 如果你们的 pipeline 没有做这个对齐，H3 要么拒绝、要么内部静默对齐导致**实际时长和字幕时间轴对不上**。这是 Top 10 里我认为**最该立刻抄**的一条。另外注意 `roundPositiveHalfEven`（`timing.ts:11-20`）—— 用的是**银行家舍入**（Python 的 `round()` 语义），不是 JS 的 `Math.round()`，前后端必须一致否则帧数会差 1。

#### 发现 6：接续（continuity）的完整数学

`docs/native-workflow-execution.md:61-81` + `native_templates.py:1361-1421`：

```
前驱 child 唯一 SaveVideo output
  → LoadVideo(file="... [output]")
  → GetVideoComponents
  → ImageFromBatch(batch_index=-N, length=N)        # 取前驱最后 N 帧
  → MiniMaxH3AddGuide(frame_idx=0)                  # 锚在采样 latent 的第 0 帧

VAEDecode(sample = align(F+N))
  → ImageFromBatch(batch_index=N, length=F)         # 裁掉前置 N 帧 + alignment tail
  → CreateVideo → SaveVideo
```

其中：
- `F` = 该段原本的可见帧数（`17k+5`）
- `N ∈ {5, 22, 39, 56}`（`schemas.py:999` `Literal[5, 22, 39, 56]`）—— **这四个数本身就是合法的 H3 帧数**（都在 `17k+5` 格点上），所以取前驱尾帧不会引入非法帧数
- 采样帧数是 `align(F + N)`，**再产生一个 alignment tail**
- 保存时只取 `[N, N+F)`，**所以前置帧和 tail 都不进成片，全片时长与关闭接续时完全相同**

**音频同步裁剪**（`workflow/interpreters/audio_output.py:48-56`）：
```python
if continuity_prefix_frames:
    selected_audio = TrimAudioDuration(
        audio=selected_audio,
        start_index=continuity_prefix_frames / draft.render.fps,   # = N/24 秒
        duration=visible_frames / draft.render.fps,                # = F/24 秒
    )
```
前驱尾部音频作为 guide 时用**负索引**（`native_templates.py:1396-1398`）：
```python
TrimAudioDuration(audio=..., start_index=-(overlap_frames/fps), duration=overlap_frames/fps)
```

**FL2VA + 尾图 + 接续的三重锚定**（`native_templates.py:1406-1420`）—— 这段注释极具信息量：
```python
# ImageToVideo keeps last_frame in the Qwen presentation so FL2VA prompts
# can resolve its <Picture N> label. A continuity sample also contains a
# hidden aligned tail, so repeat the same image as a guide at the final
# visible output frame; the node's implicit sample-end anchor is cropped.
if isinstance(segment, UnifiedFL2VASegment) and segment.last_image is not None:
    conditioning = MiniMaxH3AddGuide(
        positive=conditioning, latent=latent, vae=..., 
        image=_load_image(graph, segment.last_image),
        frame_idx=overlap_frames + visible_frames - 1,   # = N+F-1
    )
```
即：尾图**同时**接 `MiniMaxH3ImageToVideo.last_frame`（让 Qwen 图文输入能解析 `<Picture N>` 视觉块）**和**一个 `AddGuide(frame_idx=N+F-1)`。

**接续锚点重置规则**（`docs/native-workflow-execution.md:190-191`）：
- 首个启用段 = 锚点重置
- **带 `first_image` 的 FL2VA 段也 = 锚点重置**（所以复制配置时会自动关 continuity，见 §3.2）
- 其余段依赖**紧邻的前一个启用段**（停用段被跳过，不是"前一个段"而是"前一个启用段"）
- T2V / FL2V / Ref2V / FL2VA / Ref2VA **可以混排接续**

**历史 take 复用**（`docs/architecture.md:79-85`）：如果前驱**没有被选中本次生成**，后端从 `segment_takes` 账本按 **endpoint + 稳定 segment ID + 输出几何（宽高 / FPS / H3 可见帧数）** 解析最新成功 take。**提示词、模型、LoRA、采样参数、seed 变化都不影响匹配**。

**依赖失败语义**（`docs/native-workflow-execution.md:193-194`）：前驱失败/被外部取消/输出缺失或有歧义 → 所有未提交的后继标 `status=failed, stage=dependency_failed`，**绝不把未绑定占位路径交给 ComfyUI**。占位符是个显式哨兵：
```python
# native_templates.py:145
_UNBOUND_PREDECESSOR_OUTPUT = "__DIRECTORDECK_UNBOUND_PREDECESSOR_OUTPUT__.mp4 [output]"
```

#### 发现 7：官方节点**不需要** `MiniMaxH3Director`

README 原文：「不用 `MiniMaxH3Director` 自定义节点，也不依赖其上传、探测、分镜或进度接口。」`native_templates.py:1475-1481` 主动**拒绝**任何包含 `MiniMaxH3Director` 的 prompt。六种配方全部由官方 `MiniMaxH3ImageToVideo` / `MiniMaxH3ReferenceToVideo` 的 typed 输入表达。

### 3.4 角色一致性 / 参考图：它到底有什么

#### ★ 结论：**它没有任何"角色"抽象**

我做了全量搜索：
```bash
grep -rni "character|persona|costume|outfit|wardrobe|角色|服装|定妆" \
  backend/directordeck/*.py frontend/src/domain/*.ts
# → 只命中 "character" 变量名（遍历字符串字符）和 promptLength 的 "characters"
# → 0 处角色/服装/定妆概念
```

文档自认（`docs/unified-timeline-design.md:133`）：
> 「尚未覆盖：**项目级人物、服装、场景、镜头语言和 speaker continuity bible**。」

**它只有"分段局部参考槽位"这一层抽象。** 没有跨段的角色实体、没有角色→参考图的映射、没有服装变体、没有角色 ID 传播。

#### 它的参考图机制（这部分值得学的是**契约严谨性**）

**容量上限**（`backend/directordeck/h3_capabilities.py`，与前端 `frontend/src/domain/h3Capabilities.ts` 严格镜像）：
```python
H3_REFERENCE_LIMITS = H3ReferenceLimits(
    reference_images=9,
    reference_video_channels=3,
    standalone_reference_audios=3,
    source_videos=1,
    paired_reference_video_audios=3,
)
```
- 参考图 **9 张**、视频总计 **3 路**、独立参考音 **3 条**
- **各类型之间不共享总容量**（`docs/unified-timeline-design.md:45-47`）
- 源视频占 `ref_video_0`，所以有源视频时独立视频**最多 2 路**

**★ Autogrow 平铺键格式（你们问的那个）** —— `native_templates.py:1281-1358`：

```python
common.update(audio_vae=..., ref_image_size=segment.ref_image_size)

# 源视频占第 0 路
if segment.source_video is not None:
    images, source_audio = _load_video_components(graph, segment.source_video, start=..., duration=...)
    common["ref_videos.ref_video_0"] = images
    video_offset = 1
    if segment.source_audio_as_reference:
        common["ref_video_audios.ref_video_audio_0"] = source_audio

# 参考图：按 slot 排序后压密重编号
for dense, asset in enumerate(sorted(segment.reference_images, key=lambda i: i.slot)):
    common[f"ref_images.ref_image_{dense}"] = _load_image(graph, asset)

# 独立参考视频：从 video_offset 开始
for dense, asset in enumerate(sorted(segment.reference_videos, key=lambda i: i.slot), start=video_offset):
    images, _audio = _load_video_components(graph, asset)
    common[f"ref_videos.ref_video_{dense}"] = images

# 独立参考音频
for dense, asset in enumerate(sorted(segment.reference_audios, key=lambda i: i.slot)):
    common[f"ref_audios.ref_audio_{dense}"] = _load_audio(graph, asset)

node = graph.add("MiniMaxH3ReferenceToVideo", **common)
```

**四种平铺键**：`ref_images.ref_image_{i}`、`ref_videos.ref_video_{i}`、`ref_video_audios.ref_video_audio_{i}`、`ref_audios.ref_audio_{i}`。**索引是压密重编号的（`dense`），不是原始 slot。**

**★ 稠密槽位强制校验**（`native_templates.py:1250-1278`）：
```python
def _require_dense_slots(values, *, field, segment_id):
    """Stock Autogrow assigns dense ordinals; never silently renumber prompts."""
    slots = sorted(value.slot for value in values)
    expected = list(range(len(values)))
    if slots != expected:
        raise NativeTemplateError(
            f"segment '{segment_id}' {field} slots must be dense {expected}; "
            f"got {slots}. Repair the slots and prompt tags explicitly."
        )
```
**设计意图**：Autogrow 的**展示顺序**决定提示词编号。如果内部 slot 稀疏（例如 slot = [0, 2]），而压密后变成 `ref_image_0/ref_image_1`，那么用户提示词里写的 `<Picture 3>` 就和实际编号错位了。**它选择直接报错而不是静默重编号。** 这是一条非常重要的"失败封闭"原则。

同样的规则在 schema 层（`schemas.py:1043-1074`）和前端校验层（`timelineProject.ts:3985-3995` 的 `denseSlots`）三处重复检查。

**其他参考图细节**：
- `ref_image_size: "match" | "max"` 直接透传给官方节点（`native_templates.py:1308`）
- 参考视频**至少 5 帧**（`timelineProject.ts:3994`）
- 同一素材不能同时占 `source_video` 和 `reference_video` 槽（`schemas.py:1085-1090`）
- 源视频上传后生成**不可变 24fps 代理**，工作流只读代理（`docs/native-workflow-execution.md:101-102`）
- V2V 用 `LoadVideo → Video Slice → GetVideoComponents`，`Video Slice` 带 `strict_duration=True`（`native_templates.py:1233-1241`）

> **和我们对比**：你们的"服装变体（图锁身份+文控服装）"在**语义层**比它高一层。但你们可以**借鉴它的槽位纪律**：给"角色 → 参考图槽位"建立显式映射表，并且在槽位稀疏时**报错而非静默重编号**。它踩过的坑（Autogrow 展示序 = 提示词编号）你们一定也会踩。

### 3.5 工作流管理：模板是代码，版本靠 Bundle 字节冻结

#### 不是 JSON 模板文件，是 Python 声明式结构

这是**整个项目最独特的设计**。`docs/native-workflow-execution.md:11` 原文：
> 「自定义节点只有两类例外：后端从受支持 H3 LoRA 文件名**确定性推导**的加载器，以及 GPU 池自动启用的 RayLight 多卡执行节点。」

模板长这样（`workflow/templates.py:128-175`）：
```python
V4_STANDARD_SEGMENT_TEMPLATE = SegmentTemplate(
    id="h3_standard_segment",
    revision=1,
    entries=(
        _entry("shared_models", mode="needed", phase="bootstrap", backend="standard",
               writes=(_write("clip","CLIP"), _write("video_vae","VAE"), _write("audio_vae","VAE",required=False))),
        _entry("standard_model_load", mode="needed", phase="model_load", backend="standard",
               writes=(_write("model","MODEL"),)),
        _entry("standard_model_device", mode="needed", phase="model_prepare", backend="standard",
               reads=(_read("model","MODEL"),), writes=(_write("model","MODEL","replace"),)),
        _entry("lora", mode="switch", phase="model_prepare", backend="standard",
               reads=(_read("model","MODEL"),), ...),
        # ... family_conditioning / continuity / standard_sampling / decode_video / audio_output / save_take
    ),
)
```

**每个 entry 声明 4 件事**：
1. `reads` / `writes` —— **资源读写声明**，构成一张隐式资源 DAG
2. `phase` —— 图阶段（`bootstrap` / `model_load` / `model_prepare` / …）
3. `mode` —— `needed`（必需）/ `switch`（可选开关）
4. `params_schema` / `defaults` / `cache_policy` / `backends` / `families` / `scopes` / `ui`

**编译器做的事**（`workflow/registry.py:78-198`）：
- `_validate_template_dependencies` —— 校验 entry 的 reads 都能被更早的 entry 满足（**拓扑序校验**）
- `_validate_template_resource_flow` —— 校验资源流没有断点
- 校验通过后冻结注册表（`FeatureInterpreterRegistryFrozenError`）

**每个 entry 由一个解释器函数发射**（`workflow/interpreters/`，16 个文件），例如：
```python
# workflow/interpreters/sampling_standard.py:11-40
def emit_standard_sampling(emitter, *, model, conditioning, latent, sampling, seed):
    guider   = emitter.add("BasicGuider", model=model, conditioning=conditioning)
    scheduler= emitter.add("BasicScheduler", model=model, scheduler=sampling.scheduler,
                           steps=sampling.steps, denoise=1.0)
    sampler  = emitter.add("KSamplerSelect", sampler_name=sampling.sampler)
    noise    = emitter.add("RandomNoise", noise_seed=seed)
    sampled  = emitter.add("SamplerCustomAdvanced", noise=edge(noise), guider=edge(guider),
                           sampler=edge(sampler), sigmas=edge(scheduler), latent_image=latent)
    return edge(sampled, 0)
```

> **优点**：模板是**类型检查过的 Python**，改动有 IDE 支持，不可能出现 JSON 里手写错节点 ID 的问题；资源 DAG 校验能在编译期发现"忘了接 VAE"这类错误。
> **缺点**：加一个新工作流要动 4-5 个文件（template / registry / interpreter / contracts / tests），**心智成本很高**。见 §6。

#### ★ 版本管理：多代 Bundle 并存 + 字节冻结

`workflow/templates.py:385-386` 注释原文：
```python
# ``V4_TEMPLATE_BUNDLE`` above is a historical, byte-frozen contract.  Current
# v5 projects compile against a separate bundle so adding an authorable feature
# can never mutate the compatibility fixture or the old graph ordering.
```

```python
V4_TEMPLATE_BUNDLE = TemplateBundle(version=4, ...)
V5_TEMPLATE_BUNDLE = TemplateBundle(version=5, ...)
V6_TEMPLATE_BUNDLE = ...                              # templates_v6.py:314
CURRENT_TEMPLATE_BUNDLE = V6_TEMPLATE_BUNDLE          # templates.py:797
```

**每代 Bundle 的图解顺序被永久冻结**，绝不修改（`templates.py:5-11` 开头注释）：
> 「The order below follows the nodes emitted by the legacy v4 compiler. It is **intentionally not the aspirational order in the architecture prose**: moving Standard LoRA after SigmaShift, or DirectorDeckRayLoraLoader after DirectorDeckRayUNETLoader, would change the exact prompt and **violate the Stage-2 migration gate**.」

还有 **golden 文件回归测试**（37 万个字符的 JSON！）：
- `backend/tests/fixtures/extensible_workflow_v0/native_prompt_goldens.json`（**372,946 字节**）
- `current_v4_expected.json`（34,766 字节）
- `current_v4.sqlite3.gz`（7.8 KB，**真实的 SQLite 快照**）

配套验证脚本 `tools/validate_native_comfy_prompts.py`（40 KB）**只在 CPU 上导入节点、校验 prompt 结构，不加载模型、不排队、不启动 Ray**（`RELEASE.md` "Validation boundary"）。

**每个任务持久化 settings snapshot + 模板版本 + 编译清单**（`docs/architecture.md:36-39`），历史 job 的 snapshot 是执行审计，永不随 live settings 迁移。

> **这是"模板版本化"的满分答案**：模板不可变 + 每个 job 记录当时用的是哪代 + golden 文件锁死输出字节。**对我们的意义**：你们的"manifest 指纹三态幂等"已经在做类似的事，但如果你们改了节点图而没升版本号，旧的 manifest 指纹会失效却不知道为什么。**建议引入 `template_bundle_version` 并写进 manifest。**

#### ★ 提交前的三重白名单校验

`docs/architecture.md:266-277`：
1. 固定节点 class allowlist + 期望来源（core / comfy_extras / custom）
2. 提交前已知节点来源，然后由 **ComfyUI 原生 prompt validator** 做精确输入/输出兼容性再校验
3. `_object_info_matches_contract`（`plugin/__init__.py:709-778`）—— 用 `/object_info` 反查节点的 `required` / `optional` / `output` / `output_is_list` / `output_node`，**包括 Autogrow 成员契约**（`_raw_autogrow_member_contract`, `:648`）

而且注意它的**克制**（`plugin/__init__.py:718-720` 注释）：
> 「This is advisory normalization only. Extra optional widgets and moving a required input to optional are compatible observations; **ComfyUI remains the authority** even when this bounded matcher returns False.」

以及 `native_templates.py:1459-1474`：`node_provenance` 参数**保留在契约里但故意忽略** —— 因为 ComfyUI 自己会判。**这种"只做能确定的检查，其余交给权威"的态度值得学。**

#### ★ LoRA 加载器的文件名→节点映射（`config/directordeck.json` + `workflow/lora_factory.py`）

**配置文件**（`backend/directordeck/config/directordeck.json`，**这是唯一的外部配置文件**）：
```json
{
  "schema_version": 1,
  "lora": {
    "loaders": [
      { "id": "model_only", "class_type": "LoraLoaderModelOnly",
        "input_contract": "model_only", "supported_families": ["fl2va","ref2va"], "options": [] },
      { "id": "minimax_h3_turbo", "class_type": "MiniMaxH3TurboLoRA",
        "input_contract": "dedicated_model", "supported_families": ["fl2va","ref2va"],
        "options": [ { "id": "low_vram", "type": "boolean", "label": "low_vram",
                       "description": "启用 MiniMax-H3 Turbo LoRA 节点的低显存模式。",
                       "default": false } ] }
    ],
    "fallback_policy": { "loader_ids": ["model_only"], "default_loader_id": "model_only" },
    "loader_policies": [
      { "lora_filename": "minimax_h3_turbo_.*\\.safetensors$",
        "loader_ids": ["minimax_h3_turbo"], "default_loader_id": "minimax_h3_turbo" }
    ]
  }
}
```

**发射逻辑**（`workflow/interpreters/lora.py:20-50`）按 `input_contract` 分派：
```python
if adapter.input_contract == "dedicated_model":
    inputs = {"model": model, "lora_name": binding.lora_name,
              "strength": binding.lora_strength,
              "low_vram": options.get("low_vram", binding.lora_low_vram)}
elif adapter.input_contract in {"model_only", "bypass_model_only"}:
    inputs = {"model": model, "lora_name": binding.lora_name,
              "strength_model": binding.lora_strength}
```

**★ 而且它按文件名精确路由**（`docs/native-workflow-execution.md:155-162`）：
> 「Standard 只认可当前经过审计的**精确 basename**：
> - `minimax_h3_turbo_v4_step600(_ema)` → `MiniMaxH3TurboLoRA`
> - 两套 4/8-step `*_10ErosMax_beta1_pruned_compat_v001_T8` → `LoraLoaderBypassModelOnly`
> - `*_comfyui_bf16` → `LoraLoaderModelOnly`
> - RayLight 一律 `RayLoraLoader`
> 相似后缀、未知命名、缺节点或错误 provenance 均在 `/prompt` 前失败，**不猜测或回退**。」

> 🎯 **这条对你们直接有用**：你们的 **fl2v turbo 8step LoRA** 属于 `*_T8` 这一类。如果你们的 LoRA 是量化旁路（bypass model only）类型，就必须用 `LoraLoaderBypassModelOnly` 而不是 `LoraLoaderModelOnly`，用错了会静默降质或报错。**建议你们也建一个 `lora_filename → class_type + 输入契约` 的映射表，并且在文件名不匹配时 fail-closed 而不是回退到通用 loader。**

**注意一个缺口**：我**没有在代码中找到"选 turbo LoRA 就自动把 steps 从 25 改成 8"的逻辑**。默认 `SamplingConfig.steps = 25`（`schemas.py:740`），LoRA 选择和 steps 是**完全独立的两个设置**。所以 turbo LoRA 的 8 步需要用户手动设。**这是它的一个可用性缺陷，你们不要复制。**

### 3.6 UI/UX

#### 布局：四区 + 壳层（`docs/unified-timeline-design.md:12-32`）

```
┌─ 顶栏 (topbar--timeline) ────────────────────────────────────┐
│ App.tsx:6059                                                  │
├──────────┬──────────────────────────┬────────────────────────┤
│ 左侧      │ 中央主预览                │ 右侧实时进度            │
│ 资产库    │ + 原视频对比双窗格         │ (可折叠)               │
│          │ + 实时执行监视器           │                        │
├──────────┴──────────────────────────┴────────────────────────┤
│ 操作栏 (timeline-commandbar)：插入/分割/复制/合并/删除/均分/智能分割/缩放 │
├───────────────────────────────────────────────────────────────┤
│ 主时间线（唯一主轨，无重叠）                                    │
├───────────────────────────────────────────────────────────────┤
│ 分段检查器（只编辑当前选择）                                    │
└───────────────────────────────────────────────────────────────┘
全局设置 / 任务抽屉 / 系统设置 / 主题 = 浮层，不挤占时间线工作区
```

**左侧资产库**：类型过滤、网格密度、上传、**拖放绑定**、排序、批量移出（`LongFormTimelineWorkspace.tsx:1195-1199` 三个 `SegmentReferenceGrid`）

**中央主预览**（`LongFormTimelineWorkspace.tsx:2150`）：
- 显式素材选择优先
- 播放时间线时按**稳定 segment ID** 取最新非歧义 `segment_results` 候选
- 没有候选时显示源视频或 slate（**明确不把参考素材伪装成成片** — `:2131` "该片段尚无生成候选；当前仅预览占位画面或源素材"）
- `原视频对比` 双窗格（`:2229` 按钮，`:2152-2162` 布局）
- `实时执行` 折叠面板（`:2150` 按钮）

**主时间线**（`docs/unified-timeline-design.md:24-28`）：
- 唯一主轨，**无重叠**（不是多轨编辑器）
- 首次绑定 Ref2VA 源视频时**以服务器探测的完整时长铺满主轨**，并显示**等距关键帧画面带**（`sourceTimelineThumbnailTimes()`, `timelineProject.ts:2437`）
- 超过 512 帧的源片必须先切段才能生成
- **支持：插入空 FL2VA 段、拖动排序、Ctrl/Cmd 切换选择、Shift 范围选择、键盘删除**
- Shift 跨轨从目标段重新起选，不误选夹在中间的停用段

#### ★ 快捷键全集（`LongFormTimelineWorkspace.tsx:1935-2001`）

| 键 | 动作 |
|---|---|
| `Ctrl/Cmd+A` | 全选（含启用与停用段） |
| `Ctrl/Cmd+D` | 复制所选（受 128 上限约束） |
| `Delete` / `Backspace` | 删除所选（带 `window.confirm`） |
| `Space` | 播放/暂停 |
| `S` | 在播放头处分割当前段 |
| `←` / `→` | 播放头 ±1 帧（`Shift` 时 ±1 秒） |
| `Home` / `End` | 跳到片头/片尾 |
| `+` / `=` | 放大时间线（步长 12 px/s） |
| `-` | 缩小时间线 |
| `0` | 适配视口 |
| `Escape` | 清空选择 / 关闭浮层 |
| `Ctrl+Z` | 撤销 |
| `Ctrl+Shift+Z` / `Ctrl+Y` | 重做 |

**注意**：`Ctrl+Z` 等有 `event.keyCode === 229` 的 IME 组合态守卫（`App.tsx:1641`），以及"文本框内不触发"守卫（`interactiveTimelineTarget(event.target)`）—— 中文输入法场景下必须这么做，**这个细节直接用得上**。

时间线有**缩放控件三件套**（`LongFormTimelineWorkspace.tsx:2323-2326`）：`−` 按钮 + `range` 滑杆 + `＋` 按钮，范围 `12–240 px/s`（`:105-106`）。

#### ★ 撤销/重做：patch 式历史（`state/timelineHistory.ts`）

```typescript
export const MIN_TIMELINE_HISTORY_CAPACITY = 50;              // :16
export const MAX_TIMELINE_HISTORY_CAPACITY = 100;             // :17
export const DEFAULT_TIMELINE_HISTORY_CAPACITY = 100;         // :18
export const DEFAULT_TIMELINE_HISTORY_COALESCE_WINDOW_MS = 800;  // :19 ← 合并窗口
export const MAX_TIMELINE_HISTORY_BYTE_BUDGET = 16 * 1024 * 1024; // :20 ← 16 MB 字节预算
export const TIMELINE_HISTORY_CHECKPOINT_INTERVAL = 20;       // :22 ← 每 20 步存一次全量
export const TIMELINE_HISTORY_ENVELOPE_FORMAT = "director-timeline-history"; // :24
```

关键函数：`recordTimelineHistory` / `sealTimelineHistoryCoalescing` / `undoTimelineHistory` / `redoTimelineHistory` / **`jumpTimelineHistory`（跳到任意历史点！）** / `rebaseTimelineHistoryHead` / `serializeTimelineHistory` / `deserializeTimelineHistory`。

**设计要点**：
- **patch 式**（不是全量快照），所以要有 `TIMELINE_HISTORY_CHECKPOINT_INTERVAL = 20` 定期存全量以便快速跳转
- **800 ms 合并窗口**：连续打字不会产生 800 个历史条目
- **16 MB 字节预算**：历史栈本身有内存上限
- prompt 输入框有 `data-timeline-history-field={'segment:${id}:prompt'}` 标记（`LongFormTimelineWorkspace.tsx:1218`），说明**撤销粒度可以精确到单个字段**

#### 持久化：rev 感知的双向 WAL（`state/timelinePersistence.ts` + `timelineProject.ts:4060-4090`）

多代 WAL key 并存且**明确区分"可回放"与"仅证据"**：

| key | 处置 |
|---|---|
| `directordeck:v2:timeline` | 仅历史镜像，**永不作为 WAL 回放** |
| `directordeck:v3:timeline-wal` | 未绑定，隔离 |
| `directordeck:v4:timeline-wal` | 无项目归属，**隔离而非回放** |
| `directordeck:v5:timeline-wal` | 证明了 DB/项目归属但**没记录 server revision 与 base 文档** → **仅证据，永不提升** |
| `directordeck:v6:timeline-wal` | 进程级全局 key，**当外来证据逐字节保留** |
| `directordeck:v8:timeline-wal:{...}` | **当前**（format `director-revision-aware-timeline-wal`, version 3） |

`docs/unified-timeline-design.md:126` 原文：「浏览器旧 localStorage 采用同样的非破坏迁移；**服务端时间线确认前不覆盖本地尚未完成自动同步的编辑**。」

> **对我们的意义**：你们的"极简 Web 控制台"如果是 localStorage 存草稿，**多标签页 + 多代版本会静默互相覆盖**。这套"rev 感知 + 隔离而非删除"的做法可以直接抄一个简化版。

#### 任务抽屉（`components/TaskDrawer.tsx`）

- 搜索（`:1183`）、项目过滤（`:1193`）、排序（`:1200`）、状态 tab 导航（`:1208`）
- 取消（`:1299`，且 `supportsCancel` 为 false 时禁用并给 title "当前 ComfyUI 版本不支持安全的定向取消"）
- 批量取消 `onBulkCancel`
- 输出查看器（`:609`，支持文本输出用 `<iframe sandbox="">` 渲染 `:643`）
- 生成参数详情面板（`:695`）：输出规格 / 采样（双族）/ 模型与执行（双族，含设备）/ 本次生成分段列表
- **`导出项目配置`** `onExportProjectConfig`、**`导出诊断`** `onExportDiagnostic`、**`导入输出`** `onImportOutput`、**`从任务加载项目`** `onLoadProject`

#### i18n

`frontend/src/i18n/locales/zh-CN.json` —— **只有 zh-CN 一个 locale 文件**（`i18n/index.ts` 455 字节）。所以"支持中文"其实是**中文原生**，不是国际化。

### 3.7 容错 / 显存 / ComfyUI 断连

#### 重试

| 机制 | 位置 | 说明 |
|---|---|---|
| **任务级重试** | `app.py:11057` `POST /api/jobs/{job_id}/retry` | **不从历史 job 复制任何编译结果 / child / prompt-id / ledger**。走完整创建路径：当前 schema 校验 + 模型检查 + 能力预检 + 重编译，产生**全新执行证据谱系** |
| 重试前置条件 | `app.py:11071-11100` | 若还有 child 持有未释放的 ComfyUI prompt → **409**。legacy job 无结构化 ownership 证据时用保守 gate |
| **组装重试** | `app.py:4131, 4164` `stage="assembly_retry"` | ffmpeg 组装失败可重试 |
| **Ray 池恢复** | `app.py:4722` | "operator-controlled retry path after a transient `cancel_failed`" |
| **无自动分段重试** | — | 文档明确 `docs/unified-timeline-design.md:135`：「**显式 accepted/rejected 段级重试状态**」尚未覆盖。失败段保留成功 take，用户手动重跑 |

> ⚠️ **没有指数退避、没有自动重试。** 我 grep `retry|retries|backoff` 后确认后端**没有任何 backoff 实现**（只有 `_RAYLIGHT_GENERATION_POLL_SECONDS = 1.0` 这种固定间隔轮询）。

#### 超时

| 项 | 值 | 证据 |
|---|---|---|
| ComfyUI HTTP 默认 | 30 s | `comfy.py:220, 226` |
| 轻量探测 | 10 s | `comfy.py:267, 364` |
| `/prompt` 提交 | **300 s** | `comfy.py:411` |
| 宿主能力 object_info | 10 s | `plugin/__init__.py:78` |
| ffmpeg 场景检测 | **1800 s** | `media.py:717` |
| ffmpeg 单段归一化 | **1800 s** | `media.py:598` |
| ffmpeg concat | **1800 s** | `media.py:637` |
| ffprobe 元数据 | 60 s | `media.py:51` |
| Ray 生成轮询 | 1.0 s | `app.py:281` |
| 后台对账间隔 | **10.0 s** | `app.py:9227` |
| WebSocket 首次握手 | **至多 1 s 有界等待**，超时继续提交由 queue/history 兜底 | `docs/native-workflow-execution.md:223` |
| RayKill 屏障 | 300 s（可配） | `app.py:5342` |
| 上传进度 TTL | 15 min | `app.py:325` |

#### ★ ComfyUI 断连处理（这批设计很硬）

**① WebSocket 先连后提交**（`docs/native-workflow-execution.md:221-224`）：
> 「Director 会在 POST `/prompt` **之前**保存 caller-assigned prompt ID 并**先连接 WebSocket**；即使首个 `executing` 早于 POST 响应，也允许精确匹配的 `preparing/submitting` child 直接进入 running，随后提交确认不得把阶段覆盖回 queued。」

**② 断连不等于失败**（`docs/architecture.md:130-132`）：
> 「**Losing the WebSocket only removes live stage/step detail; queue/history remain the durable lifecycle authority.**」

**③ "缺席"绝不推断为取消**（`docs/architecture.md:134-137`）—— 见 §3.1。

**④ HTTP 读路径与对账路径彻底隔离**：
> 「Whole-timeline ffmpeg assembly is owned **only** by the process reconciler. HTTP job list/detail reads are **SQLite-only** for both parent/child and historical legacy rows; a disconnected or black-hole ComfyUI endpoint therefore **cannot freeze the task panel**.」

→ **只有取消（`cancellation`）是唯一被允许抢占 assembly flight 的 HTTP 操作。**

**⑤ 取消只走原子 API**（`docs/architecture.md:143-144`）：
> 「Segmentation 依赖当前 ComfyUI 原生**原子 job-cancel API**；旧服务器缺少该能力时界面**禁用取消**，绝不退回可能误杀其他任务的全局 `/interrupt`。」

**⑥ 取消优先于依赖失败**（`docs/native-workflow-execution.md:198`）：
> 「用户取消整个父任务时 cancellation ownership 优先，dispatcher 立即停止续提，未提交后继按**取消语义**收口而**不是误标依赖失败**。」

**⑦ CAS 防复活**（`docs/architecture.md:129-132`）：
> 「Cancellation, progress, final assembly and deletion use **compare-and-set** state transitions. A late queue response, WebSocket frame or assembly completion **cannot revive a cancelled/terminal task**.」

#### 显存管理（单卡 5090 场景的关键）

**标准模式：不主动清显存**（`docs/architecture.md:183-184`）：
> 「The standard compiler **never adds an unload/free-memory node**, so stable loader inputs can reuse ComfyUI's model cache.」

**这就是为什么它要"稳定 loader 节点 ID/输入"**：让 ComfyUI 跨 prompt 复用模型缓存。

**★ 按模型分别放置逻辑设备**（`workflow/interpreters/`）：
```
UNETLoader → SelectModelDevice(device=binding.device)
CLIPLoader → SelectCLIPDevice(device=settings.models.clip.device)
VAELoader(video/audio) → SelectVAEDevice(device=...)
```
`gpu:N` 是 **ComfyUI 进程内的逻辑索引**，可能被 `CUDA_VISIBLE_DEVICES` 重映射，**从不作为物理卡号呈现**（`docs/architecture.md:170-171`）。

> 🎯 **这一条对你们最直接**：你们单任务吃满 23.6/24 GB。Director 的做法是**把 CLIP / Video VAE / Audio VAE 显式放到不同逻辑设备**（比如 CPU 或另一张卡），从而给 UNET 腾出空间。你们虽然是单卡，但**把 CLIP 和 VAE 放 CPU 是一个可试验的显存优化**（代价是 encode/decode 变慢）。它的 `SelectCLIPDevice` / `SelectVAEDevice` / `SelectModelDevice` 是三个独立开关，粒度很细。

**多卡 RayLight 的常驻策略**（单卡用不到，但概念可借鉴）：
- `keep_until_switch`（默认）：按 `family + model + LoRA + GPU pool + topology + sigma shift` 完整 key 常驻，同 key 跨段/跨任务复用 CUDA 权重
- `release_after_sampling`：每次采样后 `clear_vram_after_sampling=true` 卸载 worker 权重
- **切换 key 必须先过 `RayKill` 屏障并等 exact history 成功**，然后**递增 epoch** —— 防止 A→B→A 时第三个 A 命中已被 shutdown 的 actor handle
- `driver_cleanup_policy=ray_devices`：只释放 `GPU_SELECT` 列出的卡上的 driver 模型
- 显式承认局限：*"cross-process memory on an overlapping CLIP/VAE device cannot be coordinated by ComfyUI alone, so simultaneous auxiliary residency is not guaranteed"*

**没有 OOM 自动检测/重试**（grep 无命中）。

#### ffmpeg 组装（★ 直接可抄）

`media.py:514-644` `assemble_video_paths()`。docstring 就说出了核心 trick：

> 「Every intermediate receives H.264 video and a stereo 48 kHz AAC track. **Supplying silence for a segment without audio keeps ffmpeg's concat timeline aligned instead of dropping or shifting later soundtracks.**」

```python
for index, source in enumerate(sources):
    command = ["ffmpeg","-hide_banner","-loglevel","error","-nostdin","-y","-i",str(source_path)]
    has_audio = _has_audio_stream(source_path)
    if not has_audio:
        command.extend(["-f","lavfi","-i","anullsrc=channel_layout=stereo:sample_rate=48000"])
    command.extend([
        "-map","0:v:0",
        "-map","0:a:0" if has_audio else "1:a:0",
        "-vf", f"fps={fps:g},scale={width}:{height}:flags=lanczos,setsar=1",
        "-af", "aresample=48000:async=1:first_pts=0,apad",
        *_fps_sync_args(),
        "-shortest",
        "-c:v","libx264","-preset","medium","-crf","18","-pix_fmt","yuv420p",
        "-c:a","aac","-ar","48000","-ac","2","-b:a","192k",
        "-map_metadata","-1","-map_chapters","-1",
        str(normalized_path),
    ])
    _run_command(command, timeout=1800)
    # 用实测时长写 concat 文件
    normalized_durations.append(probe_video_path(normalized_path, allow_frame_count_estimate_on_timeout=True).duration)

# concat 文件显式写 duration
concat_file.write_text("".join(
    f"file '{path.as_posix()}'\nduration {duration:.9f}\n"
    for path, duration in zip(normalized, normalized_durations, strict=True)))

_run_command(["ffmpeg",...,"-f","concat","-safe","0","-i",str(concat_file),
              "-c","copy","-movflags","+faststart",str(destination_path)], timeout=1800)
```

**7 个关键技巧**：
1. **无音轨的段补 `anullsrc` 静音** → 否则 concat 后音画不同步（`-shortest` 会把后面的音轨拉偏）
2. **`aresample=48000:async=1:first_pts=0,apad`** → 音频重采样 + 异步补偿 + 补零到视频长度
3. **concat 文件显式写 `duration`** → 修复 concat demuxer 最经典的时长漂移 bug
4. **`scale=...:flags=lanczos,setsar=1`** → 统一 SAR 防止拼接处画面跳变
5. **`-crf 18 -preset medium`** → 中间产物近无损，避免二次压缩累积
6. **`-map_metadata -1 -map_chapters -1`** → 清除元数据，保证**字节级确定性输出**
7. **`+faststart`** → 网络播放可边下边播

**★ 24fps 不可变代理**（`media.py:298-480`）—— `_can_remux_24fps_proxy()` 的判断链很讲究：

```python
# media.py:342-388
avg_fps  = _frame_rate(video.get("avg_frame_rate"))
real_fps = _frame_rate(video.get("r_frame_rate"))
... isclose(avg_fps, 24.0) and isclose(real_fps, 24.0) ...
# Container rate fields can claim 24 fps for VFR media. Packet durations
... _packet_durations_are_24fps(packet_output)   # ← 逐 packet 校验
```

`_packet_durations_are_24fps`（`:155-163`）检查每个 packet 的 `duration ≈ 1/24`。**为什么**：容器头字段可以撒谎说是 24fps，但实际是 VFR（可变帧率）。**只有逐 packet 验证通过才 remux（不重编码），否则才转码**：

```
fps=24,scale=trunc(iw/2)*2:trunc(ih/2)*2     # media.py:445
```

`trunc(iw/2)*2` 是为了保证宽高是偶数（H.264 yuv420p 要求）。

#### ★ ffmpeg 场景切分（直接可抄）

`media.py:685-740` `detect_shots_path()`：
```python
_SCENE_THRESHOLDS = {"low": 0.55, "medium": 0.35, "high": 0.18}   # media.py:47

result = _run_command([
    "ffmpeg","-hide_banner","-nostdin","-i",str(path),
    "-vf", f"select=gt(scene\\,{threshold}),showinfo",
    "-an","-f","null","-",
], timeout=1800)

# 从 stderr 抓 pts_time:(\d+\.\d+)，换算成帧号
_PTS_TIME = re.compile(r"\bpts_time:([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)")

candidates = [frame for frame in _scene_frames(...) if 0 < frame < total_frames]
cuts = [0]
removed = 0
for frame in candidates:
    if frame - cuts[-1] < min_shot_frames or total_frames - frame < min_shot_frames:
        removed += 1          # 太短的镜头切点被丢弃
        continue
    cuts.append(frame)
cuts.append(total_frames)
warnings = [f"{removed} 个镜头切点因小于最小镜头长度而被忽略"] if removed else []
return DetectShotsResponse(cut_frames=cuts, shot_count=len(cuts)-1, warnings=warnings)
```

**API**：`POST /api/rv2v/detect-shots`（`app.py:10677`），前端参数是**灵敏度（低/中/高）+ 最短帧**（`LongFormTimelineWorkspace.tsx:2275-2276`）。

> 🎯 **这是对你们"LLM 拆镜"的强力补充**：LLM 拆镜是**语义**拆（按剧情/台词），ffmpeg 场景检测是**视觉**拆（按真实剪辑点）。如果你们的输入是**已有视频**（而不是小说文本），这个 30 行函数可以直接用。三个阈值 + 最短帧过滤 + 返回 warnings 的设计很干净。

#### 进度阶段加权模型（★ 可抄）

`workflow/execution_hints.py` —— 每个阶段有 **`weight`**：

| 阶段 | kind | weight | 证据 |
|---|---|---|---|
| `sampling` | `fractional` | **0.70** | `execution_hints.py:97` |
| `decode_video` | `milestone` | 0.15 | `:113` |
| `assemble_media` | `milestone` | 0.10 | `:121` |
| `persist_take` | `milestone` | 0.05 | `:126` |
| 采样前各节点阶段 | `stage` | **0.0** | `:143` |

`kind` 三种：`fractional`（有精确 step/total，如采样器）、`milestone`（只有里程碑）、`stage`（只表示"在做这个"）。

`COMMON_NODE_STAGE_LABELS`（`execution_hints.py:15-45`）把节点 class_type 映射成**中文用户文案**：
```python
"UNETLoader": "加载生成模型",
"MiniMaxH3TurboLoRA": "加载 H3 Turbo LoRA",
"MiniMaxH3ImageToVideo": "构建画面条件",
"MiniMaxH3ReferenceToVideo": "构建多模态条件",
"MiniMaxH3AddGuide": "构建接续条件",
"BasicGuider": "准备采样引导",
...
```
`progress.py:956` 采样阶段产生 `f"片段 {i+1}/{len(sampler_ids)} · 采样 {step}/{maximum}"`。

并且诚实地承认局限（`docs/native-workflow-execution.md:207-211`）：
> 「RayLight worker 通常不会为加载、条件构建、解码和保存上报 `step/max`，这些阶段只保证"当前节点阶段"可见…**非采样阶段没有标准总量，只显示粗粒度百分比里程碑，不能伪造连续进度。**」

#### 实时预览帧的安全边界（`docs/architecture.md:162-166`）

- 只接受带 metadata 的 **event-4** PNG/JPEG
- 严格校验 prompt / sampler / 图片类型，**上限 2 MiB**
- **最新帧只进内存 TTL/LRU 缓存，绝不写 SQLite**
- 公开 URL 仅在 child 仍活动且缓存命中时出现，响应带 `no-store` + `nosniff`
- 任务删除或 child 终态会使缓存失效；**重启不恢复旧预览帧**

#### 单实例锁

`backend/directordeck/instance_lock.py` + `directordeck.sqlite3.instance.lock`（`README.md` "数据位置"）。`backend/tests/test_instance_lock.py` 有 12 KB 测试。

---

## 4. Top 10 值得偷（按价值排序）

### 🥇 1. H3 帧数格点对齐 `17k+5`（含银行家舍入）

**它用 X 解决了 Y**：用 `frames += (5 - frames % 17) % 17` 把任意时长吸附到 H3 唯一合法的帧数格点，并且**前后端用同一套数学**（`native_templates.py:245-257` ↔ `timing.ts:3-42`），还正确实现了 Python 的 half-even 舍入（`roundPositiveHalfEven`）。

**我们的 Z 模块该怎么改**：在拆镜产出（DeepSeek 返回时长）之后、写 manifest 之前，加一道 `align_h3_frames()`。我们的 QC 步骤"段尾冻结"检测如果拿到的实际帧数和预期不符，很可能**根因就在这里**。同时把 498 这个真实上限写进拆镜 prompt 的约束里。

**为什么排第一**：成本 20 行代码，收益是消除一类静默错误（时长漂移 → 字幕错位 → 音频对不上）。而且这是**规格级事实**，不是设计偏好。

```python
# 建议直接落地的实现
H3_FRAME_STRIDE, H3_FRAME_OFFSET, H3_MAX_FRAMES = 17, 5, 512

def align_h3_frames(duration_seconds: float, fps: float = 24.0) -> int:
    raw = max(5, round(duration_seconds * fps))   # Python round = half-even，天然正确
    frames = raw + (H3_FRAME_OFFSET - raw % H3_FRAME_STRIDE) % H3_FRAME_STRIDE
    if frames > H3_MAX_FRAMES:
        raise ValueError(f"{frames} 帧超出 H3 上限；最长可用 {498} 帧 / 20.75s")
    return frames
```

### 🥈 2. 无音轨段补静音 + concat 显式 duration

**它用 X 解决了 Y**：`anullsrc` 静音注入 + `aresample=...:async=1:first_pts=0,apad` + concat 文件写 `duration`，解决"某些段没音轨导致 concat 后音画整体错位"。

**我们的 Z 模块该怎么改**：我们的 concat + 字幕烧录是**两步 ffmpeg**。如果把 concat 和字幕合成一次 ffmpeg 调用，`subtitles=` filter 的时间轴就对不上错位的音轨。**建议先按它的方式做一次确定性归一化 concat（拿 `-crf 18` 中间产物），再在第二步用 `subtitles=` 烧字幕**。这样字幕时间轴有唯一可靠基准。

### 🥉 3. 提示词段落的**服务端**强制校验 + 结构化错误码

**它用 X 解决了 Y**：它**没解决** —— 它只在**前端**提供"填入框架"按钮，后端只查 7000 字符长度，段落结构零校验（§3.3 发现 4）。

**我们的 Z 模块该怎么改**：这是**空白地带，做了就是净胜**。在渲染前加一个 `validate_h3_prompt(segment)`：
- 按 recipe 检查必需段落（FL2VA → `integrated_multimodal_description` / `overall_soundscape` / `non_diegetic_music`；Ref2VA → 六段式）
- 检查 `subject_definitions` 里定义的每个 `<subject N>` 是否在正文出现（反向也查：正文引用的 `<subject N>` 是否已定义）
- 检查 `<Picture N>` / `<Video N>` / `<Audio N>` 是否都在该段已绑定（Director 只在前端做了这个 —— `timelineProject.ts:4076-4077` 的 `invalidPromptTokens`）
- 检查 FL2VA I2V/FL2V 是否带对应的对齐指令句
- 输出 `(code, message, remediation)` 三元组，照抄 `zh-CN.json:119-165` 的形态

**关键**：Director 把 `invalidPromptTokens` 放在**前端**（`timelineProject.ts:4076`），这是可以绕过的。放服务端才是真的 fail-closed。

### 4. Autogrow 稠密槽位 fail-closed（不静默重编号）

**它用 X 解决了 Y**：`_require_dense_slots()`（`native_templates.py:1250-1259`）检测到 slot 稀疏时**直接抛错并告诉用户"Repair the slots and prompt tags explicitly"**，而不是压密后让提示词编号与画面错位。三处重复校验（schema / 模板 / 前端）。

**我们的 Z 模块该怎么改**：我们的"服装变体（图锁身份+文控服装）"必然要处理"一个角色多张参考图"。如果中间删掉一张，后面的 `ref_image_N` 全要重排，而提示词里的 `<Picture N>` 也要同步改。**建议**：
- 内部用 `slot` 存，渲染时压密成 `dense`
- 压密前后不一致 → **报错**，不要自动改用户提示词
- 提供一个显式的"重排并重写标签"操作（Director 的 `setSourceAudioAsReference` → `rewriteAudioReferenceLabels` 是正面范例：`timelineProject.ts:3688-3698`）

### 5. 模板 Bundle 字节冻结 + golden 回归 + manifest 记版本

**它用 X 解决了 Y**：`V4/V5/V6_TEMPLATE_BUNDLE` 多代并存，旧代**永不修改**；372 KB 的 `native_prompt_goldens.json` 锁死输出字节；每个 job 持久化 `template_bundle_version`。

**我们的 Z 模块该怎么改**：我们的 manifest 指纹三态幂等现在是"配置 → 指纹"。**加一个维度**：`manifest = hash(config) + template_bundle_version`。这样改节点图时**必须显式升版本号**，否则旧 manifest 会被误判为"已渲染可复用"。同时把"当前模板的预期 prompt JSON"存一份 golden，CI 里对比 —— **这能防住"我不小心动了节点连线导致所有历史幂等失效"这类事故**。

成本很低：一个版本常量 + 一个 golden JSON + 一个 pytest。

### 6. 重启后"先定向取消，再重提交"（不假设缺席=已死）

**它用 X 解决了 Y**：启动时把死进程的 submission ownership 变成 recovery marker，起一个独立的有界 worker **对每个已知 prompt ID 走原子 cancel API**，未确认就重试；新 dispatcher 必须等所有 recovery-owned 旧 child 拿到终态证明才能提交。

**我们的 Z 模块该怎么改**：我们的"断点续跑"如果只靠 manifest 指纹，在 ComfyUI 进程中重启（或 Web 控制台重启但 ComfyUI 还活着）的场景下会漏掉"旧 prompt 可能仍在队列里迟到执行"。**建议**：
1. 提交 `/prompt` 前就把 `prompt_id` 写进 manifest（像 Director 那样"缩小匿名任务窗口"，`docs/native-workflow-execution.md:32-33`）
2. 续跑启动时先对 manifest 里所有非终态 `prompt_id` 走 `/queue` + 定向 cancel
3. **只有拿到"已取消"或"已完成"的确认，才允许提交新 prompt**
4. 区分"取消"和"依赖失败"两种收口语义

### 7. 加权进度阶段模型

**它用 X 解决了 Y**：`{id, label, node_id, kind, weight}` 的四元组，`kind ∈ {fractional, milestone, stage}`，权重 `0.70/0.15/0.10/0.05`，采样器提供精确 `step/total` 就加权插值，其他阶段只报里程碑。**并且明确拒绝伪造进度。**

**我们的 Z 模块该怎么改**：我们的 2200 行原生 JS 控制台应该有进度条。**不要做一个假的匀速进度条** —— 按 Director 的方式：定义阶段表 + 权重，能从 ComfyUI WebSocket `progress` 事件拿到 step/total 的阶段就精确插值，其余阶段跳到该阶段起点。用户对"0.70 权重给了采样"的体感远好于"卡在 43% 十分钟"。

顺带抄 `COMMON_NODE_STAGE_LABELS`（`execution_hints.py:15-45`）的中文文案 —— **28 条**节点类型 → 用户可读动词，**直接可用**。

### 8. `MiniMaxH3AddGuide` 尾帧接续的裁剪数学

**它用 X 解决了 Y**：`ImageFromBatch(batch_index=-N, length=N)` 取前驱尾帧 → `AddGuide(frame_idx=0)` → 采样 `align(F+N)` → `ImageFromBatch(batch_index=N, length=F)` 裁掉前缀和 tail。**全片时长精确等于关闭接续时的时长，拼接处不重复尾帧。**

**我们的 Z 模块该怎么改**：如果你们的逐镜渲染现在**没有**镜间衔接，镜头之间会有"跳变感"。这个机制是**唯一一个用官方节点做的、不增加总时长的接续方案**。要点：
- `N ∈ {5, 22, 39, 56}` —— **必须用这四个值**，因为它们本身就在 `17k+5` 格点上
- 音频同步用 `TrimAudioDuration(start_index=N/24, duration=F/24)`
- 前驱尾部音频作 guide 用**负索引**
- FL2VA + 尾图 + 接续时，尾图要**锚两次**（`last_frame` 输入 + `AddGuide(frame_idx=N+F-1)`）
- **首段和带 `first_image` 的段是锚点重置**，不读前驱

**但要注意它的自我限定**（`docs/native-workflow-execution.md:189`）：
> 「当前连续性是官方 H3 guide 条件接续，**不是**旧 Director 的跨段 AV latent handoff，**也不是**剪辑层 crossfade。」

所以这是"条件化生成"而非"无缝拼接"。如果要真正无缝，还是得跨段 latent 传递（它明确说没做）。

### 9. `apply-config` 分字段批量传播（默认不覆盖 prompt）

**它用 X 解决了 Y**：`TimelineSegmentCopyOptions` 8 个布尔开关 + `scope: "following" | "selected"`，**默认 `prompt: false`**，且 `prompt` 与 `promptReferences` 必须同时勾选才生效。

**我们的 Z 模块该怎么改**：我们 50+ 镜的调参场景里，"把这批镜头的时长都改成 5 秒" / "把后面所有镜头都开接续" 是高频操作。手工点 50 次很痛苦。建议实现同样的粒度：
- 作用域：所选 / 后续全部
- 字段：时长 / 接续 / 音频策略 / ref_image_size / 模型族 / **提示词（默认关）** / 素材绑定（默认关）
- 提示词和素材绑定必须绑定在一起

外加它那条**语义联动规则**（`timelineProject.ts:1636-1640`）：复制后如果目标是"带显式首帧锚点的 FL2VA 段"，**自动关掉 continuity**。这类"两个字段语义冲突时自动收敛"的规则，比让用户自己去发现要好。

### 10. ffmpeg 场景检测做视觉拆镜

**它用 X 解决了 Y**：`ffmpeg -vf select=gt(scene\,THRESHOLD),showinfo` 抓 `pts_time:`，三档阈值 `{low:0.55, medium:0.35, high:0.18}` + 最短镜头帧数过滤 + 返回被丢弃切点的 warning。30 行代码，无依赖。

**我们的 Z 模块该怎么改**：我们已经有 DeepSeek 做**语义**拆镜。如果你们的素材里有**已有视频**（参考片、真人素材、旧作），加一个"视觉拆镜"通道做交叉验证：
- LLM 拆镜的切点 vs ffmpeg 场景切点，**偏差大的镜头标记出来人工 review**
- 或者用 ffmpeg 场景切点作为 LLM 拆镜的**硬约束**（"场景切点必须在这些帧附近"）

**为什么排第 10 而不是更高**：它对"小说 → 视频"的主链路是补充而非核心。但对"视频 → 视频"（V2V 重绘）链路是刚需。

---

## 5. 反向清单：不值得偷 / 不要学的

### ❌ 1. 三位数 KB 的单体文件

`app.py` **463 KB / 约 11,000 行**；`database.py` **343 KB**；`App.tsx` **286 KB**；`timelineProject.ts` **187 KB**；`styles.css` **169 KB**。

**理由**：这是**测试驱动到极致的副产品**（后端 90 个测试文件、单个 `test_per_segment_execution.py` **278 KB**、`App.test.tsx` **312 KB**）。它对一个 GPL 单人/小团队项目行得通，因为它有极其完整的回归测试兜底。**我们的 2200 行原生 JS 控制台如果长到这个规模就不可维护了。**
**我们该怎么做**：借它的**机制**，不借它的**形态**。机制用 Python 独立模块实现（`h3_frames.py` / `prompt_contract.py` / `concat.py`），不要堆进一个大文件。

### ❌ 2. V4 / V5 / V6 三代编译器并存

`workflow/` 下有 `v4_compiler.py`(39KB) / `v4_executor_adapter.py`(31KB) / `v4_resolver.py`(34KB) / `v5_compat.py`(25KB) / `v5_registry.py`(17KB) / `v6_projection.py`(17KB) / `v6_execution_adapter.py`(14KB) / `legacy.py`(46KB)，加上 `migrations/` 4 个文件和一个 30KB 的 `migration_api.py`。

**理由**：它需要多代并存是因为**它已经发布了、有真实用户的历史 job 不能失效**。这是**存量包袱**，不是设计优点。
**我们该怎么做**：我们是从零自建，**只做一代**。在 manifest 里记录 `schema_version`，遇到不认识的版本**报错要求人工迁移**即可。**永远不要为"可能的历史兼容"提前建三代**。

### ❌ 3. RayLight 多卡 + 222 文件的 bundled fork

`custom_nodes/raylight` 有 222 个文件，`release-manifest.json` 记录了 fork 的上游 commit 和 tree sha256，还有 `raylight_setup.py` 做按需依赖安装、`requirements-raylight.txt`、epoch/taint 账本、`RayKill` 屏障、`driver_cleanup_policy`。

**理由**：**你们是单卡 RTX 5090。** 这整块（估计占后端 20%+ 代码量和绝大部分复杂度来源）对你们**收益为零**。而且它的文档反复强调"手工混跑不受支持"、"不承诺辅助模型常驻"—— 说明这套机制本身很脆弱。
**我们该怎么做**：**完全跳过。"单卡"不是"未来可能多卡"的临时状态，它可以是一个明确的产品边界。** 把省下的精力投到 §4 的 1/2/3/5 条。

### ❌ 4. CK Attention（Comfy Kitchen Attention）能力探测的状态机

`capabilities/comfy_kitchen_attention*.py` 3 个文件 + `capabilities/observer.py` + 前端 `ComfyKitchenAttentionField.tsx` + `useComfyKitchenAttentionCapability.ts`，`zh-CN.json:44-70` 有 **7 种 reason code**。

**理由**："暂时无法确认" / "未确认" / "旧项目" 这种状态，是为了让用户在**不确定**时仍能操作。**但一个"暂时无法确认却能启用"的开关，实际调试成本极高** —— 出问题时你无法判断是节点不支持还是探测失败。
**我们该怎么做**：能力探测要么确定（可用/不可用），要么**不暴露这个开关**。宁缺勿滥。

### ❌ 5. 多 WAL key 的 localStorage 迁移考古

`timelineProject.ts:4060-4090` 定义了 8 个 localStorage key：`v2:timeline`、`v2:timeline-quarantine`、`v3:timeline-wal`、`v3:timeline-wal-quarantine`、`v4:timeline-wal`、`v5:timeline-wal`、`v5:timeline-wal-quarantine`、`v6:timeline-wal`、`v8:timeline-wal:{...}`，每个都有"为什么不能回放、只能当证据"的详细注释。

**理由**：这同样是**存量包袱** —— 每代前端都改过持久化语义，所以需要逐代考古。注释里那句 *"v5 proved database/project ownership but did not record the exact server revision and base document. It is evidence only and must never be promoted."* 是血泪教训。
**我们该怎么做**：**抄它的当前形态**（rev 感知 + `base_document` + `server_revision`），**但不要抄迁移考古**。新的 WAL 格式一次设计对：带 `{server_revision, base_document_digest, project_id, format, version}`。这样将来升级不需要考古。

### ❌ 6. 全局 `keep_until_switch` 显存常驻（对我们不适用）

**理由**：它是为**多卡 RayLight**设计的。单卡 Standard 路径下它做的是**相反**的事 —— **不加任何 unload 节点，靠 ComfyUI 自己的缓存**（`docs/architecture.md:183-184`）。
**我们该怎么做**：★ 注意这里有个**容易被误读的点**。对单卡的我们，正确的借鉴是**"不要主动清显存"** —— 保持 loader 节点 ID 稳定，让 ComfyUI 复用缓存。**但你们单任务吃满 23.6/24 GB 说明缓存策略没起作用或模型太大。** 更可行的方向是 **§3.7 的按模型分设备放置（CLIP/VAE 放 CPU）**。

### ⚠️ 7. 部分可疑/未验证的点

- **`MiniMaxH3TurboLoRA` 的 `low_vram` 选项**：`config/directordeck.json` 里声明了，`lora.py:41` 会传，但**注释说它是"用户权威"、Director 不鉴定其第三方实现**（`README.md`）。所以这个选项**实际效果无法从本仓库确认**。
- **"选 turbo LoRA 自动改 steps"**：**不存在**（§3.5 已说明）。默认 25 步，需手动改成 8。
- **两条 600 秒/1800 秒的超长 ffmpeg 超时**：我认为是"宁可等死也不误杀"的保守选择，但对交互式体验不友好。自行判断。

---

## 6. 我们已领先 / 它明确缺失的地方（不要白抄）

这张表很重要 —— 避免我们花时间去"补一个我们已经有的东西"：

| 能力 | Director-WebUI | 我们 | 依据 |
|---|---|---|---|
| **角色身份一致性** | ❌ **完全没有**。0 处 character/persona/角色概念 | ✅ 定妆照（H3 T2VA 生成 + 抽帧当参考图） | grep 全仓库 0 命中；`docs/unified-timeline-design.md:133` |
| **服装/多形态** | ❌ 完全没有 | ✅ 服装变体（图锁身份 + 文控服装） | 同上 |
| **抽卡（N 候选让用户挑）** | ⚠️ **不是真抽卡**。每 job 每段 1 take，靠重跑累积；**无 accepted/stale 生命周期**，不把"最新"持久化为"已接纳" | ✅ 同角色出 N 张候选让用户挑 | `docs/unified-timeline-design.md:131-132, 135-136` |
| **字幕** | ❌ **零支持**。`grep -ri subtitle\|.srt\|.ass\|subtitles=` → 0 命中（非测试代码） | ✅ 中文字幕烧录 | grep 确认 |
| **音频轨 / 剪辑层** | ❌ 无多轨、无转场、无 crossfade、无响度归一化、无段级 fade/trim | 部分有 | `docs/unified-timeline-design.md:131, 134` |
| **LLM 语义拆镜** | ❌ 无 LLM。只有 ffmpeg 视觉场景检测 | ✅ DeepSeek 拆镜 | `media.py:685` 只有 ffmpeg |
| **机械质检** | ⚠️ 只有"输出存在性/唯一性"检查（近黑/静音/冻结**未找到**） | ✅ 近黑/静音/段尾冻结 | grep 无 luminance/blackdetect/freeze 检测 |
| **多轨 / 吸附 / ripple** | ❌ 单一主轨，无重叠，无 ripple | — | `docs/unified-timeline-design.md:24, 130` |

> **一句话**：**它的强项在"执行侧"（编译、调度、审计、显存），我们的强项在"创作侧"（角色、服装、抽卡、字幕、质检）。** 抄它执行侧的硬骨头，同时把我们的创作侧优势做得更硬。

**另外它还有一个我们没有的、值得注意的缺失维度**：它的"近黑/静音/段尾冻结"质检**确实没有**。我 grep 了 `blackdetect` / `freezedetect` / `silencedetect` / `luminance`，非测试代码里 0 命中。所以**我们的机械质检是它没有的**，值得保留强化。

---

## 7. 立即行动清单（按投入产出比排序）

| # | 动作 | 预估成本 | 收益 |
|---|---|---|---|
| 1 | 落 `align_h3_frames()` + 常量 `(17, 5, 498)`，拆镜后写 manifest 前调用 | 20 行 | 消除时长漂移/字幕错位一整类 bug |
| 2 | concat 加 `anullsrc` 静音补齐 + concat 文件写 `duration` + `aresample=...apad` | 30 行 | 音画不同步根治 |
| 3 | 服务端 `validate_h3_prompt()`（按 recipe 查段落 + `<subject N>`/`<Picture N>` 交叉引用） | 150 行 | 净胜项，Director 没做 |
| 4 | manifest 增加 `template_bundle_version` 维度 + golden prompt 回归测试 | 50 行 + 1 JSON | 防"改图导致幂等失效"事故 |
| 5 | 参考图 slot 稠密性 fail-closed（不静默重编号）+ 显式"重排并重写标签"操作 | 80 行 | 防参考图错位（我们服装变体的高频路径） |
| 6 | 续跑改为"先定向 cancel 已知 prompt_id 再重提交" | 100 行 | 防重复渲染 |
| 7 | 进度条按加权阶段模型重做（+ 抄 28 条中文阶段文案） | 120 行 | 体感大幅提升 |
| 8 | CLIP / Video VAE / Audio VAE 独立设备放置（试 CPU） | 配置 + 试验 | 缓解 23.6/24 GB |
| 9 | `apply-config` 式分字段批量传播（默认不覆盖 prompt） | 150 行 | 50+ 镜调参效率 |
| 10 | 引入 `MiniMaxH3AddGuide` 尾帧接续（`N ∈ {5,22,39,56}`） | 需先做 #1 | 镜头衔接质量 |

**互斥/依赖**：#10 依赖 #1（没有帧格点对齐就无法正确计算 `align(F+N)` 和裁剪边界）。

**不要做的**：不要引入 RayLight、不要建三代编译器、不要做 CK Attention 状态机、不要写多代 WAL 考古。

---

## 附录 A：关键常量与契约速查

```python
# ── H3 帧格点（native_templates.py:245-252 / timing.ts:3-5）────────────────
H3_FRAME_STRIDE = 17
H3_FRAME_OFFSET = 5
H3_MAX_SHOT_FRAMES = 512          # 声明值
# 实际最大可用帧数 = 498 (= 20.75s @24fps)；499~512 会被向上取整到 515 并拒绝
# 合法帧数序列: 5, 22, 39, 56, 73, 90, ... , 481, 498
# 舍入：half-even（Python round 语义），不是 Math.round

# ── 接续 overlap（schemas.py:999）────────────────────────────────────────
CONTINUITY_OVERLAP_FRAMES = (5, 22, 39, 56)   # 均为合法 H3 帧数
DEFAULT_OVERLAP = 22

# ── H3 参考素材容量（h3_capabilities.py）─────────────────────────────────
REFERENCE_IMAGES = 9
REFERENCE_VIDEO_CHANNELS = 3      # 源视频占 ref_video_0 → 独立视频最多 2
STANDALONE_REFERENCE_AUDIOS = 3
SOURCE_VIDEOS = 1
PAIRED_REFERENCE_VIDEO_AUDIOS = 3 # 配对音轨不占独立音频名额

# ── Autogrow 平铺键（native_templates.py:1329-1356）──────────────────────
"ref_videos.ref_video_{dense}"            # 源视频=0，独立视频从 video_offset 起
"ref_video_audios.ref_video_audio_0"      # 源音轨作配对参考
"ref_images.ref_image_{dense}"            # 按 slot 排序后压密重编号
"ref_audios.ref_audio_{dense}"
# 索引必须是稠密的 0..N-1，稀疏 → 抛错（_require_dense_slots）

# ── 提示词（schemas.py:53, timelineProject.ts:531-614）───────────────────
MINIMAX_H3_PROMPT_MAX_CHARACTERS = 7000   # Unicode code points，非 UTF-16 长度

# Ref2VA 六段式
"subject_definitions:\n\nsummary:\n\nretention_analysis:\n\n"
"detailed_description:\n\noverall_soundscape:\n\nnon_diegetic_music:"

# FL2VA 三段式（无 subject_definitions / summary / retention_analysis / detailed_description）
"integrated_multimodal_description:\n\noverall_soundscape:\n\nnon_diegetic_music:"

# FL2VA 首行对齐指令（英文原文，来自 H3 base 规范）
I2V = "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced."
FL2V = "How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; Picture 2 (from Shot N) aligns with the S.SS-second mark of the target video."
L2V  = "How the reference pictures align with the target video — <Picture 1> (from [Shot N]) aligns with the S.SS-second mark of the target video."

# ── 采样默认值（timelineProject.ts:479-487 / schemas.py:739-754）─────────
steps = 25            # 注意：选 turbo LoRA 不会自动改这个值
seed = 0, random_seed = True      # 前端提交前重掷，服务端不二次抽签
sampler = "res_multistep"         # Literal["res_multistep","euler","dpmpp_2m"]
scheduler = "simple"
shift = 12.0, audio_shift = 3.0   # → MiniMaxH3SigmaShift(shift_video, shift_audio)
# 无 CFG、无负面提示词（BasicGuider）

# ── 渲染（schemas.py:733-736）───────────────────────────────────────────
width=864, height=480   # multiple_of=32；你们一致
fps = 24.0              # 硬编码检查 == 24

# ── 上限（schemas.py:1138 / timelineProject.ts:3957）────────────────────
MAX_SEGMENTS = 128
MAX_TOTAL_ENABLED_FRAMES = 100_000

# ── 场景检测（media.py:47）──────────────────────────────────────────────
SCENE_THRESHOLDS = {"low": 0.55, "medium": 0.35, "high": 0.18}
# ffmpeg -vf "select=gt(scene\,{t}),showinfo"，从 stderr 抓 pts_time:

# ── 组装编码参数（media.py:570-596）─────────────────────────────────────
"fps={fps},scale={w}:{h}:flags=lanczos,setsar=1"
"aresample=48000:async=1:first_pts=0,apad"
libx264 -preset medium -crf 18 -pix_fmt yuv420p
aac -ar 48000 -ac 2 -b:a 192k
-map_metadata -1 -map_chapters -1
concat: -c copy -movflags +faststart

# ── 进度阶段权重（execution_hints.py）───────────────────────────────────
sampling        fractional  0.70
decode_video    milestone   0.15
assemble_media  milestone   0.10
persist_take    milestone   0.05
(pre-sampling node stages)  0.00

# ── 超时（comfy.py / media.py / app.py）─────────────────────────────────
ComfyUI HTTP default 30s | 轻量探测 10s | /prompt 300s
ffprobe 60s | ffmpeg 归一化 1800s | concat 1800s | 场景检测 1800s
后台对账间隔 10.0s | Ray 轮询 1.0s | WS 首次握手 ≤1s | 上传进度 TTL 15min

# ── LoRA 文件名 → 节点映射（docs/native-workflow-execution.md:155-162）──
minimax_h3_turbo_v4_step600(_ema)                    → MiniMaxH3TurboLoRA
*_10ErosMax_beta1_pruned_compat_v001_T8  (4/8 step)  → LoraLoaderBypassModelOnly
*_comfyui_bf16                                       → LoraLoaderModelOnly
RayLight                                             → RayLoraLoader
# 不匹配 → fail closed，不猜测不回退
# LoRA loader 配置文件: backend/directordeck/config/directordeck.json

# ── 历史撤销（timelineHistory.ts:16-25）─────────────────────────────────
CAPACITY = 100 (min 50)
COALESCE_WINDOW_MS = 800
BYTE_BUDGET = 16 MiB
CHECKPOINT_INTERVAL = 20

# ── 上传上限（app.py:329-334）───────────────────────────────────────────
image 32 MiB | audio 128 MiB | video 512 MiB
```

---

## 附录 B：证据索引

**文档（本地 `/tmp/dw/docs/`）**
- `README.md` — 执行边界、当前能力、生成文件与任务记录、安装、多卡、ffmpeg、数据位置、资源常驻
- `docs/architecture.md` — 产品边界、canonical 域、父子生命周期、GPU 与常驻（L168-235）、媒体与删除边界、安全检查（L266-277）
- `docs/unified-timeline-design.md` — 页面结构（L12-32）、canonical 数据模型（L34-53）、执行语义（L55-104）、素材删除（L106-117）、迁移（L119-126）、**尚未覆盖的能力（L128-137）**
- `docs/native-workflow-execution.md` — 不变量（L5-14）、父子图（L16-37）、标准模板（L39-106）、Standard/RayLight 选择（L108-162）、能力预检（L164-185）、连续性与进度（L187-237）
- `docs/mode-contract.md`, `docs/project-management-design.md` — 未逐行引用（模式契约与项目管理设计）
- `RELEASE.md`, `release-manifest.json`, `CHANGELOG.md` — 版本/兼容矩阵/发布流程

**后端**
- `native_templates.py:145` 未绑定前驱哨兵 · `:151-216` 数据类 · `:218-233` 白名单/provenance/RayLight 必需节点 · `:245-257` **帧格点对齐** · `:260-304` `[output]` 路径规范化与安全校验 · `:352` `bind_native_workflow_predecessor_output` · `:608-643` `_shared_core` · `:1122` `build_raylight_shutdown_unit` · `:1211-1247` 素材加载 · `:1250-1278` **稠密槽位校验** · `:1281-1358` **`_conditioning` / Autogrow 平铺键** · `:1361-1421` **`_add_continuity_guides`** · `:1454-1481` 能力校验 + 拒绝 `MiniMaxH3Director`
- `schemas.py:53` 7000 字符 · `:125-168` LoRA 绑定与 override · `:204-256` RuntimeSettings · `:621-674` VideoMetadata/AssetReference · `:675-732` SlottedAssetReference · `:733-736` RenderConfig · `:739-774` SamplingConfig（含 `pop("cfg")`） · `:784-793` ShotBase · `:995-999` TimelineContinuity · `:1002-1091` 两族 segment 模型 · `:1097-1113` recipe 推导 · `:1116-1139` UnifiedTimelineDraft · `:1411` UnifiedTimelineDraftV5
- `h3_capabilities.py` 全文 — 参考素材容量
- `config/directordeck.json` 全文 — LoRA loader 注册表
- `execution/submission.py:424-560+` `LockedSubmissionPlanner`、`_set_exact_input`（JSON Pointer 晚绑定）
- `comfy.py:220-226` 超时 · `:235-238` `_http` · `:267,364` 10s 探测 · `:411` 300s `/prompt`
- `media.py:47` 场景阈值 · `:134-163` `_fps_sync_args` / `_packet_durations_are_24fps` · `:298-480` **24fps 不可变代理** · `:514-644` **`assemble_video_paths`** · `:676-758` **`detect_shots_path` / `detect_shots_bytes`**
- `progress.py:47` `_EXECUTION_STAGES` · `:743-907` child 进度 · `:956` 采样 `step/max` · `:1226` WS listener
- `app.py:281` Ray 轮询 · `:300-334` 取消阶段集合 / `:329-334` 上传上限 · `:3636` `directordeck/timelines` · `:5342-5354` RayKill 屏障超时 · `:6350` `_resolve_historical_continuity_takes` · `:7690-7780` **提交循环 + ComfyUI 队列串行化注释** · `:9227` 对账间隔 10s · `:10677` `/api/rv2v/detect-shots` · `:11057` **`/api/jobs/{id}/retry`** · `:11494` 分段 take 不触发 queue
- `workflow/interpreters/` — `sampling_standard.py` 采样链 · `decode_video.py` **`ImageFromBatch` 裁剪** · `audio_output.py` **`TrimAudioDuration`** · `save_take.py` SaveVideo 前缀 · `continuity.py` · `conditioning.py` · `lora.py` 三种输入契约 · `standard_model_path.py` 设备放置 + `MiniMaxH3SigmaShift` · `shared_models.py`
- `workflow/execution_hints.py:15-45` **中文阶段文案** · `:88-149` **加权阶段模型**
- `workflow/templates.py:5-11` 顺序冻结注释 · `:128-175` `V4_STANDARD_SEGMENT_TEMPLATE` · `:249` `V4_RAYLIGHT_SEGMENT_TEMPLATE` · `:373-383` V4 Bundle · `:385-386` **字节冻结注释** · `:786-797` V5/V6/CURRENT Bundle
- `workflow/templates_v6.py:314` `V6_TEMPLATE_BUNDLE`
- `workflow/registry.py:78-198` 注册表 + 拓扑/资源流校验
- `workflow/lora_factory.py:126-200` adapter 注册表 + `_LEGACY_ADAPTERS`
- `workflow/audit.py:169` `_validate_prompt_shape`（图结构，非文本提示词）
- `workflow/v4_compiler.py:337-411` `build_v4_route_context`（**visible/sample/prefix 帧三件套**） · `:452-571` `_compile_route`

**插件层**
- `plugin/__init__.py:54-112` 常量（含 `_OBJECT_INFO_LIMIT_BYTES = 32MiB`、`_HOST_CAPABILITY_HTTP_TIMEOUT_SECONDS = 10.0`） · `:419-520` **`_ComfyHostCapabilityProvider`** · `:623-778` Autogrow 成员契约 + **`_object_info_matches_contract`** · `:840-908` 逻辑 GPU 清单 · `:910-978` ffmpeg 能力探测（要求 `libx264` + `aac`）

**前端**
- `frontend/src/domain/timing.ts:3-5` **`H3_FRAME_STRIDE/OFFSET/MAX`** · `:11-30` half-even + 对齐 · `:33-42` `alignH3Frames`
- `frontend/src/domain/timelineProject.ts:239-260` `TimelineSegmentCopyOptions` · `:479-487` 采样默认值 · `:499-513` `deriveSegmentRecipe` · `:531-543` **六段式骨架** · `:551-579` `promptSubjectReferences` · `:582-598` **FL2VA 三段式 + 三条对齐指令原文** · `:604-614` `promptSkeleton` · `:726-767` 时长对齐/播放时长 · `:1290-1334` 合并 · `:1336-1476` 分割 · `:1405-1476` 按切点分割 · `:1477-1521` 均分 · `:1521-1591` 复制 · `:1591-1672` 配置复制 + `applyTimelineSegmentConfiguration` · `:1772-1836` **reducer action 全集** · `:1847-2275` reducer · `:2291-2372` 接续边界/问题 · `:2437-2464` 源缩略图时间 · `:3512-3554` 标签/主体 token 插入 · `:3648-3712` **`segmentReferenceTag` / `setSourceAudioAsReference` / `rewriteAudioReferenceLabels`** · `:3917-4081` **`validateTimelineProject` 全量校验** · `:4060-4090` WAL key 常量与考古说明
- `frontend/src/components/LongFormTimelineWorkspace.tsx:89` 候选类型 · `:105-123` 缩放常量与刻度 · `:1106-1136` 检查器基础字段 · `:1180-1260` 提示词编辑器 + `@`/`#` 自动完成 · `:1935-2001` **快捷键全集** · `:2071` 候选琴 · `:2131-2162` 主预览/对比/实时执行 · `:2229` 原视频对比按钮 · `:2273-2288` 命令栏（均分/智能分割/选择） · `:2323-2326` 缩放控件
- `frontend/src/state/timelineHistory.ts:16-25` **历史常量** · `:185-264` 创建/上下文捕获 · `:526-699` 记录/合并/撤销/重做 · `:748-822` 跳转/重基线 · `:1311-1554` 序列化/反序列化/标签
- `frontend/src/components/TaskDrawer.tsx:242-260,438,906` segment_results 使用 · `:514` 任务元数据 · `:609-656` 输出查看器 · `:695-772` 生成参数面板 · `:1134-1208` 列表头/搜索/过滤/排序/tab
- `frontend/src/App.tsx:1635-1648` **Ctrl+Z/Shift+Z/Y**（含 IME 229 守卫） · `:1569-1575` 文本导航密封 · `:5828` `app-main` · `:6059` `topbar--timeline` · `:6370` TaskDrawer 挂载
- `frontend/src/i18n/locales/zh-CN.json` 全文 174 行 — 功能清单 + ~30 条结构化错误码
- `frontend/src/domain/promptLimits.ts:2` 7000 字符（Unicode code point 计数）
- `frontend/src/domain/h3Capabilities.ts` 全文 — 与后端镜像的容量常量
- `frontend/src/test/timelineProject.test.ts:140-175` — **四种骨架的显式断言**（含 `not.toContain("subject_definitions:")`）

**Fixtures / 测试（作为设计意图的证据）**
- `backend/tests/fixtures/extensible_workflow_v0/native_prompt_goldens.json` — **372,946 字节 prompt golden**
- `backend/tests/fixtures/extensible_workflow_v0/current_v4_expected.json` — 34,766 字节
- `backend/tests/fixtures/extensible_workflow_v0/current_v4.sqlite3.gz` — 真实 SQLite 快照
- `backend/tests/test_per_segment_execution.py` — **278,696 字节**
- `frontend/src/test/App.test.tsx` — **312,420 字节**
- 后端共 **85 个测试文件**（`backend/tests/*.py` 实测计数）；`conftest.py` 43,470 字节

**工具**
- `tools/validate_native_comfy_prompts.py` — 40 KB，CPU-only prompt 结构验证
- `tools/benchmark_job_read_path.py` — 20 KB
- `.github/workflows/ci.yml` — 3 KB

**明确未找到（负面证据）**

搜索范围统一为 `backend/directordeck/` + `frontend/src/`，`*.py` / `*.ts` / `*.tsx`，并**排除** `test/`、`test_*`、`*.test.*`、`fixtures`：

| 搜索词 | 命中 | 结论 |
|---|---|---|
| `discipline`（全仓库大小写不敏感，含 docs） | **0** | ❌ 无"纪律行"机制 |
| `subtitles=` / `\.srt` / `blackdetect` / `freezedetect` / `silencedetect` | **0** | ❌ 无字幕、无机械质检 |
| `persona` / `costume` / `outfit` / `wardrobe` / `服装` / `角色` / `定妆` | **0** | ❌ 无角色/服装系统 |
| `subtitle` | 1 | ⚠️ **假阳性** — 是 i18n key 名 `preflight.success.subtitle`（`PreflightResultPanel.tsx:69`），与字幕无关 |
| `\.ass` | 87 | ⚠️ **假阳性** — 全部匹配 `.asset` / `.assets` / `asset_id` |
| `character` | 79 | ⚠️ **假阳性** — 全部是 `ord(character)` 循环变量、`_CONTROL_CHARACTERS`（`task_management.py:56`）、注释里的 "LIKE metacharacters" |
| 后端 `subject_definitions` / 段落结构校验 | **0** | ❌ 提示词结构零校验（仅 7000 字符长度） |
| 后端 `backoff` / 自动重试 | **0** | ❌ 仅 `POST /api/jobs/{id}/retry` 手动重试端点，无退避、无自动重试 |
| turbo LoRA → 自动改 steps | **0** | ❌ LoRA 选择与 steps 完全独立 |

---

*报告生成基于本地 clone `/tmp/dw`（`main` 分支）。若上游已更新，`CHANGELOG.md` 显示 `0.2.0` 仍为 `Unreleased`，插件形态的重构可能仍在进行中。*
