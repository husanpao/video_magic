# 四项目调研：Slate / MCWW / ac-comfyui-queue-manager / storyboard-master

> 调研目的：为自建 AI 漫剧流水线（Python + 本地 ComfyUI HTTP + MiniMax H3，864×480@24fps，单卡 5090 单并发，零框架单页控制台）提取可借鉴的契约设计、状态管理与队列语义。
> 调研方式：`git clone --depth 1` 到 `/tmp/research/`，**读真实代码为主**，README/文档为辅。每条结论标注依据；无法在代码中证实的一律标「推断」。
> 调研日期：2026-09-23。

## 0. 结论速览（TL;DR）

| 项目 | 真实体量 | 一句话定性 | 最值得拿的东西 |
|---|---|---|---|
| **coracoo/Slate** | 330 文件 / Python ~23k 行 + Vue 前端 | **成熟度远超预期**的本地短片工作台：契约先行、校验器与契约一一对应、产物来源指纹、版本快照、惰性迁移 | 分镜 schema 的分层字段设计 + 三类提示词分离 + `@kind:id` 资产引用 + 消费阶段化指纹 + 资产修订号 + 表演层契约 + 镜内台词轨 + H3 帧步进对齐 |
| **light-and-ray/MCWW** | 代码仅 ~15 个 py + ~30 个 js（35MB 里 99% 是文档截图） | 面向推理的 ComfyUI 外挂 UI，**队列语义最完整** | 队列持久化 + restoreKey 门控、`/queue delete` + `/interrupt` 双段取消、`UnqueuedByComfyUI` → **自动暂停而非盲目重投**、`/free` 卸载显存、优先级桶 |
| **abdullahceylan/ac-comfyui-queue-manager** | 18.7k 行 Python（但核心是桩） | **不建议借鉴实现**：`WorkflowExecutor` 是 `time.sleep(0.1)` 占位符；`priority` 参数被接收但从未入库/排序 | 仅「SQLite 作业台账 + REST 资源模型 + 归档/导入导出」这一层壳可以看一眼 |
| **c-wang-dev/storyboard-master** | ~1.5k 行 Python（零第三方依赖） | **决策端 Agent**：六维特征 → 五张决策表 → 仲裁，纯规则确定性 | 镜头语言受控词表与「决策表」形式、景别序列/机位语法/越轴提示、12 项质量自检、角色锚定卡 |

**一句话**：Slate 解决「一份分镜怎么驱动全流程且不失控」，MCWW 解决「队列在崩溃/断线/重启后怎么不重复烧卡」，storyboard-master 解决「镜头语言怎么从剧本确定性推导」，acqm 只提供「作业台账长什么样」的负面教材。

---

# 一、Slate（coracoo/Slate）— 重点深挖

## 1.1 仓库概况与可信度

- commit：`64a936c7b3b2c30ab2241b5e981ddc7024f177ee`（2026-09-21），仓库活跃。
- 文档语言为中文，**README 与代码一致性很高**（README 提到的 `validate_storyboard.py` / `previs_engine.py` / `export_docs.py` 均存在）。
- 目录分工（`README.md:147-153`）：
  - `previs_system/`：白模引擎、分镜契约、导出工具（**无 AI 的确定性渲染**）
  - `workbench/`：后端 + Vue 前端 + 厂商适配 + 任务 + Skill 测试（**制作线/拉片线**）
  - `.codex/skills/video-previs/`：自包含白模 + 拉片 Skill
- 依赖：Windows + Python 3.12 + Node 构建前端（`README.md:16-54`）。**不是零框架** —— 它是 Vue + Vite。这点我们不跟。

> ⚠️ **重要修正**：任务描述说的「一份 storyboard.json 驱动全流程」基本成立，但 Slate 实际有**三套并存的 shots 契约**，不是一份 schema：
> 1. **previs 契约**（`previs_system/docs/SPEC-字段规范.md`）：白模/3D 预演用，v1→v5.1 迭代，字段是 `mode/off/fov/path/pitch`。
> 2. **dialogue 契约**（`.codex/skills/video-previs/references/storyboard_schema.md`）：对白戏 + 白模，字段是 `cam/pos/look/lines[]/staging/pose`。
> 3. **production 契约**（`workbench/tools/production_prompts.py` + `creation_pipeline.py`）：**这才是与我们分镜表对位的那个**，字段是 `shot_size/camera_move/angle/scene_ref/actor_refs/prompt_image/prompt_video/prompt_grid/lines[]`。
>
> 三者靠 `analysis_to_storyboard.py`（拉片→白模）和 `cmd_storyboard`（剧本→制作分镜）做桥接。**这正是我们要避免的**：Slate 自己也在为「契约分裂」付桥接成本。下面的对比以 **production 契约**为主。

## 1.2 `storyboard.json` 完整 schema

### 1.2.1 顶层字段（production 契约）

依据 `workbench/tools/creation_pipeline.py:769-780`（`cmd_storyboard` 实际写盘处）：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `project` | string | ✔ | 形如 `<项目名>_剧本_<集号>` |
| `title` | string | ✔ | 人类可读标题 |
| `w` / `h` / `fps` | int | ✔ | 960×540@24（默认值，非强制） |
| `set` / `env` | object | — | 场景/环境（dialogue 契约用 `set.type` 影响地面色） |
| `prompt_version` | string | — | 生成时的提示词版本号，用于判断「旧版提示词」 |
| `script_rev` | int | — | **剧本修订号**：分集.json 每次变更 +1；`script_rev > board_rev` 即判定分镜已过期（`StudioShotsView.vue:52-54`） |
| `actors` | object | ✔(白模) | 角色字典，键为角色 ID（production 里由人物.json 派生 + 台词自愈补全） |
| `shots` | array | ✔ | 镜头数组 |
| `video_units` | array | — | **V 分镜视频**分组（三层视频，见 1.2.4） |
| `panels` / `grids` | array | — | 故事板画格与宫格（`storyboard_panels.py`） |
| `acting_context` | object | — | 表演层上下文（见 1.5） |
| `acting_status` | string | — | `pending/context_ready/ready/stale/locked/invalid` |

### 1.2.2 `shots[]` 完整字段（★ 与我们分镜表对位的核心）

证据：`workbench/tools/prompt_modules.py:355-365`（LLM 输出契约）、`creation_pipeline.py:714-768`（契约化归一）、`production_prompts.py:6`（三类提示词字段）、`StudioShotsView.vue:21-30`（前端 TS 接口）、`reference_contract.py`、`workbench/docs/2026-09-21-三层视频创作台设计.md:59-100`。

| 字段 | 类型 | 必填 | 取值/说明 | 我们是否有 |
|---|---|---|---|---|
| `id` | string | ✔ | 全片唯一，如 `S1` | ✅ `id` |
| `dur` | number | ✔ | **被 clamp 到 [1.5, 15.0]**（`creation_pipeline.py:728`） | ✅ `sec` |
| `shot_size` | enum | ✔ | 大远景/远景/全景/中景/中近景/近景/特写/大特写 | ✅ `shot_size` |
| `camera_move` | enum | ✔ | 固定/推/拉/摇/移/跟/甩/升降/环绕/手持/斯坦尼康/变焦/轨道/无人机/主观 | ✅ `camera`（自由文本？） |
| `angle` | enum | ✔ | 平视/俯视/仰视/鸟瞰/虫视/荷兰角/过肩/主观 | ❌ **缺** |
| `transition` | enum | ✔ | 硬切/叠化/淡入/淡出/闪白/划像/匹配剪辑/蒙太奇/无 | ❌ **缺** |
| `cam` | enum | ✔ | wide/two/cu/ots（白模机位，非镜头语言） | ❌ 不需要（我们不做白模） |
| `scene` | enum | ✔ | room/field（室内外，白模用） | ❌ 不需要 |
| `scene_ref` | `@scene:id` | 建议 | **场景资产引用**；一个 V 单元内所有 S 必须同 scene_ref | ❌ **缺** |
| `actor_refs` | `@character:id[]` | 建议 | 本镜可见的具名角色；**禁用「五人组/师生们」群体资产** | ~ `chars`（只是名字，没有 ID 体系） |
| `prop_refs` | `@prop:id[]` | — | 叙事道具引用 | ❌ **缺** |
| `asset_refs` | string[] | 派生 | `scene_ref + actor_refs + prop_refs` 去重，自动追加进 prompt 正文 | ❌ **缺** |
| `content` | string | ✔ | 本镜内容一句话（谁在哪做什么，**剧情视角**） | ~ `prompt` 的一部分 |
| `action` | string | ✔ | 「谁做什么」——**可执行动作**，会被白模姿态消费 | ❌ **缺（显式字段）** |
| `sound` | string | ✔ | 台词之外的环境声/音效/音乐提示（≤20 字） | ❌ **缺** |
| `lighting` / `light` | string | ✔ | 光影一句话（主光方向/明暗比/色温） | ❌ **缺** |
| `lens` | string | ✔ | 焦距 mm，基线：特写 85 / 中景 50 / 全景 35 / 大远景 24 | ❌ **缺** |
| `rig` | enum | ✔ | 固定/手持/滑轨/轨道/斯坦尼康/无人机/稳定器（**由 camera_move 映射**） | ❌ **缺** |
| `lines[]` | object[] | — | **镜内台词轨**：`{at, dur, speaker, line}`，可多条；`at+dur` 超出镜长会被夹取并告警（`creation_pipeline.py:738-742`） | ~ `dialogue`（单条，无时间轴） |
| `speaker` | string | — | 主要说话人 ID；**旁白固定写 `narrator`，不是角色**，不占 actors/不进画面 | ❌ **缺** |
| `prompt_image` | string | ✔ | **单张参考关键帧**提示词：只写一个可辨认瞬间 | ~ `prompt` |
| `prompt_video` | string | ✔ | **本镜完整连续动作**：开始状态→发展→结束状态 | ❌ **缺（关键！）** |
| `prompt_grid` | string | ✔ | **宫格故事板**布局说明：起始/发展/落点瞬间 + 格序 | ❌ **缺** |
| `prompt` | string | 兼容 | 旧字段，等价 prompt_image（`normalize_prompts`） | ✅ `prompt` |
| `negative` | string[] | ✔ | 本镜专用禁止项，≤4 条 | ❌ **缺** |
| `move` | string | 派生 | `shot_size·camera_move·angle` 拼成的显示串 | ❌ **缺** |
| `pos` / `look` | [x,y,z] | — | 显式相机世界坐标（米），优先于自动机位 | ❌ 不需要 |
| `fov` | number | — | 视角 20–90，白模用 | ❌ 不需要 |
| `staging` | {角色:[x,z]} | — | 仅本镜临时站位 | ❌ 不需要 |
| `pose` | {角色:seated/stand/ride} | — | 逐镜姿态覆盖 | ❌ 不需要 |
| `aspect_ratio` | enum | — | 16:9 / 9:16 / 1:1 / 4:3 / 3:4 / 3:2 / 2:3 / 21:9 | ❌ **缺**（我们全片固定 864×480） |
| `keyframe` | object | — | 采用的关键帧：`{sha256, item_id, ...}` | ~ 我们用 `refs/` 目录 |
| `keyframe_bindings[]` | object[] | — | `{id, position(main/…), selected_item_id, output_index, sha256, source_hash}` | ❌ **缺** |
| `video_settings` | object | — | `{duration_override_seconds, continuity{mode,source}, selected_video_item_id}` | ❌ **缺** |
| `video_duration` | number | — | 该 S 独立视频时长覆盖 | ❌ **缺** |
| `generation_options` | object | — | 逐镜生成参数覆盖 | ❌ **缺** |
| `prompt_*_source` | `llm`/人类 | — | 提示词来源标注（可审计） | ❌ **缺** |
| `asset_revisions` | {ref:int} | — | 该镜依赖的资产修订号快照 | ❌ **缺** |

### 1.2.3 受控词表与「契约化」归一（★ 很值得抄）

Slate 的做法是：**LLM 只允许输出词表内的值；词表外的值由确定性代码回退到首项，并夹取数值**（`creation_pipeline.py:714-731`）：

```python
VALID = {"shot_size": [...8项...], "camera_move": [...15项...], "angle": [...8项...],
         "cam": ["wide","two","cu","ots"], "scene": ["room","field"]}
s["dur"] = round(max(1.5, min(15.0, float(s.get("dur") or 4))), 2)
for k2, vals in VALID.items():
    if s.get(k2) not in vals:
        s[k2] = {"cam": "wide", "scene": "room"}.get(k2, vals[0])
```

还有：
- **id 去重**：重复 id 自动改成 `S{i}_2`（`:722-727`）。
- **台词归属归一**：把模型输出的角色*名字*映射回角色*ID*，`narrator` 特殊处理（`:732-746`）。
- **资产引用归一** `_normalise_refs`（`:629-653`）：接受名字/ID/`@` 三种写法，按名称长度倒序匹配（**避免「学生」先吃掉「佐藤陆」**）。
- **词表外的值只警告不阻断**（拉片的 `validate_analysis.py:19-24`，含 `VOCAB_LEGACY` 兼容旧词）。
- **校验失败拒绝写盘**：`validate_document` 有 error 就 `sys.exit(1)`，**不落半成品**（`:798-802`）。

### 1.2.4 三层视频模型 S / V / E（★ 结构上最值得偷）

依据 `workbench/docs/2026-09-21-三层视频创作台设计.md:11-19, 59-100`：

| 层 | 名称 | 数据职责 | 直接请求视频模型？ |
|---|---|---|---|
| **S** | 转场镜头 | 最小叙事/机位单位；保存动作、台词、景别、提示词；生成并采用关键帧 | 可（S 独立视频） |
| **V** | 分镜视频 | **一个连续场景内的创作单元**：有序 S + 组合提示词 + 共享约束 + 时长 + 参考图方案 | 一次请求出一个完整视频 |
| **E** | 集视频 | 按编辑顺序组合多个 V 的已选版本 + 声音轨 | 否，本地 FFmpeg 拼接 |

`video_units[]` 结构（`:73-93`）：

```json
{
  "id": "vu_稳定标识", "label": "V01 · 教室异变",
  "shot_ids": ["S1","S2","S3"], "scene_ref": "@scene:loc_classroom_2a_day",
  "extra_asset_refs": [], "shared_negative": ["字幕","水印"],
  "duration": {"planned_seconds": 9, "override_seconds": null},
  "summary": {"text":"整体叙事概述","origin":"llm","source_hash":"…","user_edited": false},
  "beats": [{"shot_id":"S1","planned_seconds":3,"text":"S1 的可执行动作与运镜衔接"}, ...],
  "reference_strategy": "ordered_keyframes",
  "continuity": {"mode":"none","source":null},
  "selected_video_item_id": null, "source_hash": "…"
}
```

**分组规则是确定性的**（`production_studio.py:46-60`）：按原 shots 顺序，**相邻且 `scene_ref` 相同**的 S 归为一个 V；非相邻同场景**不自动拼接**；`scene_ref` 缺失时一个镜头一个 V。校验：V 必须**按原顺序完整覆盖全部 S，不能漏镜、重复或调序**（`validate_units`）。

> **对我们的意义**：我们的 3–15 秒/镜正好落在 H3 的 4–15 秒窗口边缘。V 层给了「把 2–3 个短镜合并成一次模型请求」的结构化表达，正是省钱/省显存的路子（我们 H3 单次 4–15s，两三个 3s 镜合成一次 9s 请求，显存占用不变）。

### 1.2.5 时长的「只有一个实际提交值」原则（★ 概念清晰）

`:186-197`：三个时间分别记录 —— `S.dur`（原始）、`V.planned_seconds`（规划）、`override_seconds`（用户覆盖）。
- `effective_seconds = override_seconds ?? planned_seconds`；时间轴/预览/prompt/payload 全部从它生成。
- 手动改总时长**只写 override，不覆盖 `shots[].dur`**。
- 模型要求整数秒时按精度重分配，保证无重叠/无空洞/末端严格等于总时长。
- **超模型上限/duration 越界 → 提交前报错，不静默改回 5 秒、不偷偷裁剪参考图。**
- 实际文件时长由 ffprobe 回读，**不冒充请求时长**。

## 1.3 「一份文件驱动全流程」怎么落地

### 1.3.1 分层：内容真源 vs 产物快照 vs 资产登记

`workbench/tools/production_studio.py:1-2` 一句话点题：

> 「S/V 制作数据。**作者内容在分镜 JSON 内，产出快照在 creation.json 内。**」

| 文件 | 角色 | 谁写 |
|---|---|---|
| `分镜/剧本_E1.json` | **唯一内容真源**（shots + video_units + episode_edit + panels/grids + acting_*） | LLM 生成 → 人工编辑 → 确定性校验 |
| `素材/人物.json` / `场景.json` / `道具.json` | 全局资产登记（`ref` 为 `@character:id` 等） | LLM 提炼 + 人工 |
| `素材/素材图.json` | 资产图索引（母图/派生状态） | 素材生成阶段 |
| `剧本/style.json` | 画风/导演风格契约（skill 引用） | 人工 |
| `创作/creation.json` | **每次生成记录**：`unit_id/shot_ids/keyframe_id/source_hash/provider_task_id/参数/结果/错误` | 任务系统 |
| `创作/prod-<id>/request.json` | 冻结的请求快照 + 素材副本 | 任务系统 |
| `创作/<job>/*.comfy-task.json` | ComfyUI prompt_id 落盘 | ComfyUI 客户端 |
| `.versions/` | **每个文件覆写前的快照**，同目录保留最近 20 份 | `versions.snapshot()` |

### 1.3.2 状态/依赖怎么表达（★ 四个独立机制）

**(a) 内容指纹 `source_hash`** —— `production_prompts.py:85-92`，只覆盖**生成输入**字段：

```python
fields = ('id','dur','scene_ref','actor_refs','prop_refs','action','content',
          'shot_size','camera_move','angle','lighting','lines','negative') + FIELDS
```
注释明确：**内容指纹不包含更新时间、任务状态或绑定字段本身**，避免绑了关键帧后反过来把自己标成过期（设计文档 `:120`）。

**(b) 消费阶段化指纹 `media_source_hash`**（★ 最精巧的一个）—— `production_prompts.py:95-108`：

```python
def media_source_hash(shots, kind, unit=None):
    """按消费阶段计算依赖：视频/宫格文字修改不会使静帧无端失效。"""
    fields = ('id','scene_ref','actor_refs','prop_refs','action','content','shot_size','angle','lighting','negative')
    values = [{k: s.get(k) for k in fields + (('prompt_image',) if kind=='image'
               else ('dur','prompt_video','lines','camera_move'))} for s in shots]
    payload = {'shots': values, 'kind': kind}
    if kind == 'image': payload['shared_negative'] = (unit or {}).get('negative') or ''
    if kind == 'video':
        payload['keyframes'] = [(s.get('keyframe') or {}).get('sha256') for s in shots]
        payload['unit'] = {k: (unit or {}).get(k) for k in
                           ('id','duration','prompt_video','prompt_grid','negative','generation_options')}
    return fingerprint(payload)
```

**改视频提示词不会让静帧关键帧失效；改景别/光影会让两者都失效。** 我们的 `shots_fingerprint` 是单一扁平指纹，任何字段变化都会重渲全部产物。

**(c) 资产修订号 `asset_revision`** —— `production_state.py:23-36`：资产每次改动 `bump_asset_revision` +1；依赖图通过 `asset_dependency_refs` 沿 `parent_ref` / `related_refs` 递归展开（子素材继承父素材的失效）。镜头里保存 `asset_revisions` 快照，比对不上就 stale。

**(d) 产物来源摘要 `artifact_hash`** —— `artifact_provenance.py:15-38`：按产物类型（`diagram`/`prompt`/`previz`）**取不同字段投影**再哈希；`is_current(manifest, expected)` 同时要求 hash 相等且 `status != 'stale'`。注释直白：「避免仅凭文件存在性复用旧图」。且 `prompt`/`previz` 投影**刻意排除表演结果**（表演是派生产物，不该反向使它失效）。

**(e) 版本快照与乐观锁** —— `versions.py:1-20`：最新文件永远在原路径（引用零改动），历史进 `.versions/<名>.<YYYYMMDD_HHMMSS>.<ext>`，每文件保留 20 份；`snapshot()` 覆写前调用。UI 写入带 `expected_revision`，冲突返回差异而非最后写入覆盖（设计文档 `:121`）。

## 1.4 各阶段做什么：LLM vs 确定性代码

这是报告里**最实用的一张表**（依据各模块 docstring 与代码）。

| 阶段 | 做什么 | LLM | 确定性代码 | 证据 |
|---|---|---|---|---|
| ① 剧本生成 | 构想→分集/扩写/分集总览 | ✔ `episodes_prompt`/`expand_episode_prompt` | 锚点定位、修订号 `_rev_of` | `prompt_modules.py:115-197`, `creation_pipeline.py:102,320` |
| ② 分镜生成 | 剧本+人物+场景→shots + 三类提示词 + video_units 分组 | ✔ `storyboard_prompt` | **契约化归一 + 词表回退 + id 去重 + dur/台词夹取 + 资产引用归一 + 契约校验拒绝写盘** | `prompt_modules.py:299-371`, `creation_pipeline.py:675-824` |
| ②' 提示词重建 | 单独重算 image/video/grid 提示词 | ✖ | ✔ `compile_stage_prompt` | `rebuild_production.py:76-90` |
| ③ 素材生成 | 人物/场景/道具母图 + 派生状态 | ✔ 提炼文本 | 资产修订号、依赖展开、@引用解析、上限裁剪 | `asset_registry.py`, `production_state.py` |
| ③' 画格/宫格 | panel 草稿 + 生图请求编译 | ✔ `PANEL_DRAFT_SYS` | ✔ `compile_image_request`（**唯一生图编译入口**） | `storyboard_panels.py:123-160` |
| ④ 音色绑定 | 拉音色库、试听、AI 音色创作、角色绑定 | ✔ 音色创作 | 版本化目录 `素材/音色/<id>/r001/` | `voice_assets.py` |
| ⑤ 演员表现 | 单镜的可见表演节拍 | ✔ `actor_prepare_prompt` / `actor_perform_prompt` | ✔ **契约校验（越界/知识泄漏/固定字段变更=invalid，最多 1 次程序纠错）** | `prompt_modules.py:376-418`, `actor_pipeline.py:234` |
| ⑥ 平面推演 | 平面图/战略图/创作包 | ✔ 少量 | ✔ 2D 引擎渲染 | `previs_system/engine/previs_engine.py` |
| ⑦ 创作生成 | S 关键帧 / S/V 视频 / E 集视频 | ✔ 视频单元汇总、提示词优化 | ✔ 时长编译、素材序号映射、参考模式校验、幂等 nonce、ComfyUI 图构建、ffprobe 回读 | `creation_pipeline.py`, `comfyui_client.py` |
| 拉片线 | 切点检测→抽帧→vision 读帧→台词 ASR/OCR | ✔ 逐镜 vision | ✔ OpenCV 切点、ffmpeg 抽帧、**逐镜 checkpoint 复用** | `analyze_film.py:1-30, 532-652` |

**可以总结出的分工铁律**（Slate 反复强调）：
1. **LLM 决定"写什么"，确定性代码决定"能不能执行"**。LLM 的输出永远过一遍契约校验，不合法的**拒绝落盘**而不是修补后落盘。
2. **LLM 不许改固定字段**（表演层不得改 `dur/cam/pos/look/move/lines/staging`，见 `演员表演契约.md:45`）。
3. **自由文本之外的引用必须结构化**（`@character:id`），自由文本中的剧情偏离「不能完全靠程序识别」，所以必须**展示差异供人工预览**（设计文档 `:131`）。

## 1.5 acting（表演）层是什么（★ 我们完全没有的一层）

依据 `previs_system/docs/演员表演契约.md`（契约名 `actor-context-v1`）+ `workbench/tools/actor_*.py`。

**定位**：演员层是对白分镜的**可选扩展**。分镜 JSON 仍是唯一事实来源；镜号、台词、时长、机位、景别、走位、场景和道具归属由分镜锁定，**演员 agent 只能补充「镜内可见的表演」**。

**(a) 连续性上下文 `acting_context`**（`:7-16`）：
- `continuity_id` + `continuities[]`（分支，各自 `initial_state`）—— 支持**平行时间线**（如"如果当时没走"）。
- `facts[]`：剧情事实（稳定 id + text）。**事实默认对角色不可见**，只有事件把事实加入角色的 `known_facts` 后才可见 → 防止角色提前知道后面的剧情。
- `events[]`：`{id, continuity_id, order, deltas[]}`，只允许引用已声明的事实和人物。
- `actor_cards`：按角色 ID 保存 `personality/goal/relationship/expression_rules/arc_stage/source/locked_fields`。
- `state_at(context, continuity_id, event_ids)` 按事件顺序生成快照；跨分支/未知事件直接报错。

**(b) 表演节拍 `beats[]`**（`:21-45`）：每个 beat 的字段是**白名单**：

```json
{"at": 0, "duration": 1.2, "intent": "压住情绪", "posture": "肩背保持不动",
 "gaze": "短暂移向手机", "gesture": "拇指收紧", "expression": "下颌轻绷",
 "voice": "低声", "evidence_fact_ids": []}
```
- `at + duration` 必须**完整落在镜头 `dur` 内**。
- beat 的 `evidence_fact_ids` 必须是**该 beat 时点**的允许事实（承认事件前不会提前看到事件后的事实）。
- **表演输出不得包含或修改 `dur/cam/pos/look/move/lines/staging`**，也不得新增台词、角色、伤势、服装或持物变化。
- 校验失败类型：截断、解析失败、越界、**知识泄漏**、未知角色、固定字段变更 → 全判 invalid，**最多一次程序纠错**；语义自检只产生 warning，**不能替代程序校验**。

**(c) 三种编译出口**（`:53-59`）：`baseline`（原分镜提示词）/ `style`（只用已审核风格候选）/ `stateful`（当前有效表演候选 + 角色卡）。**来源变化使候选 stale 并回退 baseline**；图像只保留每角色最后一个节拍，**视频按时间顺序保留全部节拍**。

**(d) 状态机**：`pending / context_ready / ready / stale / locked / invalid`，写顶层 `acting_status`；应用用 revision 乐观锁，**重复应用同一 run 幂等**，过期候选返回冲突。

> **对我们的意义**：我们的 3–15 秒镜头里有台词和走位需求时，「每个角色一段带时间的 beat」比一整段自由文本提示词**更能被机械质检**（可校验 `at+dur` 是否落在镜内、角色是否在场、有没有新增未登记的角色）。这是把「表演」从散文变成可校验数据的唯一样本。

## 1.6 "reference pack for image-to-video" 指什么

README 说的是「参考帧、视频、宫格分别使用不同提示词」（`README.md:10`）。代码里没有叫 `reference_pack` 的东西；实际是**四层参考组织**：

**(a) 参考帧契约** `reference_contract.py`：
```python
REFERENCE_ROLES = {"start", "process", "end", "unspecified"}
```
每条参考 = `{path, reference_role, purpose, usage?, target_time_seconds?, label?}`。要点：
- **「参考帧是视频生产的画面锚点，不等同于白模预演包或平面调度图」**，且「**不推断未显式选择的参考图**」——只读镜头显式保存的 `reference_frames`/`references`/`layout_ref`，**不扫描目录**（`:52-75`）。
- 路径解析**拒绝越界**（`os.path.commonpath`，`:78-93`）。
- 给模型的可读说明格式：`剧情参考帧（start），用途：xx，目标时间：2s`。
- `strip_video_only_lines()`：从静态参考图正文里**移除声音/台词字段**（声音是视频专属，不能让生图模型画出来）。

**(b) 参考策略** `reference_strategy`：默认 `ordered_keyframes` —— 按 S 顺序引用**已采用的关键帧版本**，顺序固定，编译时统一分配「图片1/2/3」，**文本编号与真正上传顺序完全一致**（设计文档 `:201-207`）。可选 `storyboard_grid`（宫格，**从已选关键帧确定性排版，不让生图模型重画人物**）、首帧/首尾帧模式（内部 `reference_role` **不直接冒充厂商字段**，由适配器转换）。

**(c) keyframe 绑定**：`keyframe_bindings[{id, position, selected_item_id, output_index, sha256, source_hash}]` —— 存的是**本机创作记录引用 + 内容哈希**，「不是云端临时 URL」；新生成的候选**只在槽位无采用版本且 source_hash 仍匹配时自动采用**，已有选用版本不被后台结果强行替换（`:207`）。

**(d) 尾帧续接**（`:221-249`）两种模式 —— 很有想法：
| 模式 | 实际执行 | 输入模型 |
|---|---|---|
| `tail_context` 尾帧画面参考 | ffmpeg 截上段末帧 → **Vision 提取人物位置/姿态/视线、构图、道具、光线** | 经预览的**连续性描述文本**；不把末帧放进首帧参数 |
| `tail_first_frame` 尾帧强制续接 | 截同一末帧 | **真实首帧图片** → 适配器正式 first_frame 参数 |

配套硬规则：「Vision **不得从静态图编造运动事实**」（单帧看不出速度）；「**不存会漂移的『最新版本』指针**」，必须绑定 `source_item_id/output_index/source_sha256`；不能用 `duration - 1/fps` 假定 VFR 精确尾帧。

> **对我们的意义**：我们做逐镜渲染 + concat，**镜间连续性目前只靠 prompt 描述**。Slate 的 `tail_context` 模式（截上一镜末帧 → 视觉描述 → 注入下一镜 prompt）不需要任何 3D，纯 ffmpeg + 一次 vision 调用即可落地，且**它同时给了"不要编造运动"这条防幻觉纪律**。

## 1.7 previs（2D/3D 灰模）怎么做的；对没有 3D 的我们有什么用

**(a) 2D 白模引擎** `previs_system/engine/previs_engine.py`：
- **纯确定性 Python，无 AI**（文件头：「白模预演渲染引擎 v4（数据驱动，无AI）」）。依赖 numpy + PIL + cv2。
- 自建 3D 数学：`Ry/Rx/Rz` 旋转矩阵、透视投影、`ease()` 缓动、`clamp()`。
- 人物是**带关节的人形骨模**（球-棒骨架：pelvis/neck/head/sh/el/ha/hip/kn/ft），三档姿势 `pose_stand / pose_fly / pose_dive` **按 pitch 插值混合**（`blend_pose(pitch)`：0→90 站→飞，90→170 飞→俯冲）。
- 相机由数据解析：`follow/wide` 用 `off` 偏移；`cu` 自动站在 focus↔对方连线上；`ots` 自动到 host 身后越肩。**「机位坐标不用手填：cu/ots 由角色站位解析算出，保证轴线正确、不穿帮」**（SPEC `:61`）。
- 镜头内运镜参数按进度插值：`dolly/pan/whip/truck/orbit/crane/zoom`（storyboard_schema `:34-36`）。
- 产物是**带字幕条和镜号叠印的 MP4**（顶部=镜号+运镜；右上=动作+提示词；底部=【角色名】台词）。

**(b) 3D 白模** `previs_system/tools/blender_previs.py`：把 storyboard JSON **代码生成**成 Blender 构建脚本（`gen_<slug>.py`），交给 Blender MCP 执行或 CLI 渲染。坐标约定 JSON x=横 y=上 z=深 → Blender (x, 深, 上)。支持两种契约（dialogue 显式 pos/look，previs 走位 path）。

**(c) 场景资产库 v5**（SPEC `:131-145`）：背景/道具不用随机方块，而是**参数化标准资产 builder**：`tower(x,z,w,h,d,style)` / `rooftop` / `tree` / `lamp` / `car`，场景预设只**摆实例**，跨项目复用。雾按场景配置（`fogNear/fogFar/fogFloor`）。

**(d) 他们的自我批判（很诚实，值得读）**：
- 「previs 要编码的不只是『相机+走位』，还有**动作发生所依赖的道具/表面**（玻璃面、楼顶边缘、挂点）」→ v5.1 加了接触面 + `web:true`+`anchor` 蛛丝。
- 「局限：当前 2.5D 投影渲染的『面接触』为近似……精确沿面滑动/碰撞需 Three.js/Blender 等真 3D 引擎」。

> **对我们（无 3D）的判断**：
> 1. **不要做 3D 白模**，投入产出比不划算；我们已经有 H3 出片能力。
> 2. **但可以偷「灰模思路的降级版」**：用**确定性代码画一张分镜平面示意图（top-down 站位 + 机位箭头）**，不需要 3D —— 他们的 `previs_engine` 里那套「cu/ots 由站位解析算机位、保证轴线不穿帮」正是**分镜一致性检查**，可以做成我们的机械质检项（例如连续两镜机位跨轴线 → 告警）。Slate 已经把「机位→几何」写成了确定性映射（`analysis_to_storyboard.py:8-14`），这套**受控词→坐标**表可以直接抄成质检规则。
> 3. 他们的**景别→fov→相机距离**对照表（`SIZE_FOV_STAND`/`SIZE_FOV_SEEN`，`analysis_to_storyboard.py:69-70`）和**wide 距离档**（全景 6.2m / 大全景 8m / 远景 10.5m / 大远景 14.5m，`:10`）是现成的「景别量化」参考 —— 我们用不到坐标，但可以用来**校验 prompt 里的景别与描述是否自相矛盾**。

## 1.8 UI 怎么编辑 storyboard.json

**技术栈**：Vue 3 + Vite + TypeScript（`workbench/web/`），**不是零框架**。路由见 `workbench/README.md:5-18`。

**分镜编辑页** `StudioShotsView.vue`（565 行）：
- **汇总表格行内编辑**（`:138` 注释「汇总表格：行内编辑 dur/action/prompt，整组回写（版本快照保护）」）：可改 `dur`（number input，step 0.5，**min 1 max 15**，`:441`）、`prompt_image`、`prompt_video`（textarea，`:473-476`）；`shot_size/camera_move` 只在表格里**只读展示**（`:439,458`）。
- **列表 + 详情双视图**：详情面板显示 景别/角度、运镜/转场、静态参考图提示词、生视频提示词、宫格布局提示词（`:537-544`）。
- **陈旧标记**：`boardStale = boardRev < scriptRev`（`:54`）—— 剧本变了就提示分镜过期。
- **缺失字段自动补齐**：`if (!s.lens) s.lens = LENS_BY_SIZE[s.shot_size] || '50mm'`；`if (!s.rig) s.rig = RIG_BY_MOVE[s.camera_move] || ...`（`:206-207`）。
- 写回带 `expected_revision`。

**画格 UI** `components/StoryboardGrid.vue`（71 行，很薄）+ `storyboard_panels.py` 的 `panels/grids` 校验（3x3 容量 9 / 5x5 容量 25）。

**原型** `workbench/docs/prototypes/video-unit-studio.html`：三栏（左：集/V 列表；中：V 工作区 + S 卡片；右：创作台）。设计文档 `:155-184` 有完整布局 ASCII 图。

> **对我们的意义**：零框架约束下，**别抄 UI 实现，抄信息架构**：
> - 「同一份结构化字段，两个用途视图」（关键帧描述 / 视频动作描述）—— 我们现在只有一个 `prompt`，改成两个字段后 UI 就是两个 textarea。
> - 「行内编辑 + 整组回写 + 版本快照 + expected_revision」这套保存语义，用原生 JS + `PUT` 一把梭完全可行。
> - 画格三态 stale 提示（`boardRev` vs `scriptRev`）是**一行 computed**，成本极低价值极高。

## 1.9 顺带捡到的：Slate 的 ComfyUI + MiniMax H3 集成（和我们直接可比）

**H3 工作流** `workbench/tools/comfyui_h3.py`：

```python
FL_MODEL  = 'minimax_h3_fl2va_pruned_int8_convrot.safetensors'
REF_MODEL = 'minimax_h3_ref2va_pruned_int8_convrot.safetensors'
ENCODER   = 'qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors'

if seconds < 4 or seconds > 15: raise ComfyUIError('MiniMax H3 时长须为 4–15 秒')   # :20-21
frames = max(5, round(seconds * 24))
frames += (5 - frames % 17) % 17        # ★ 帧步进对齐 17k+5（:22-23）
model = REF_MODEL if refs else FL_MODEL # 有参考图→Ref2V，无→FL2V
'5': {'class_type': 'MiniMaxH3ReferenceToVideo' if refs else 'MiniMaxH3ImageToVideo',
      'inputs': {..., 'ref_image_size': 'match' if refs else ...}}   # :33-35
'8': {'class_type':'KSamplerSelect','inputs':{'sampler_name':'res_multistep'}}  # :38
'9': {'class_type':'BasicScheduler','inputs':{'scheduler':'simple','steps':20,'denoise':1.0}}  # :39
'13': {'class_type':'CreateVideo','inputs':{...,'fps':24}}          # :44
```
- **最多 3 张参考图**，超出直接报错并给替代建议（`:18-19`）。
- 参考图提示词模板：`参考图按顺序对应 <Picture 1>、<Picture 2>…，仅用于主体与画风一致性；具体动作以以下视频提示词为准。`（`:27`）
- 文件头注释：**「参考图是身份/画风锚点，并非强制首帧」**（`:2`）—— 与我们 `ref_image_size: match` 的用法对照。

**ComfyUI 客户端韧性** `workbench/tools/comfyui_client.py`：
- `run_video_workflow(..., poll_interval=5, timeout=3600)`：`POST /prompt` → **立刻 `_save_task` 落盘 prompt_id**（`:270-276`）。
- `_save_task`（`:309-315`）写 `<out>.comfy-task.json`：`{prompt_id, base_url, media_type, submitted_at, output_path}`。注释：「**接受后立即落盘远端 ID，长任务出错仍可按 ID 回收，不必重新生成。**」
- `_queue_position(prompt_id)` → `GET /queue`，返回 `running/pending/None`（`:317-329`）。
- 图片版 `run_workflow` 独有的**排队宽限**（`:374-385`）：执行超时后**先看是不是还排在 ComfyUI 队列里**，是则宽限到 `queue_deadline` 并周期打日志；只有「不在队列也无历史记录」才报「**ComfyUI 可能重启丢失了该任务**」。
- 输出回收按**扩展名**判断而非字段名（注释：「原生 SaveVideo 也通过 images 返回 MP4；不能按字段名判断媒介」，`:287-295`）。
- 超时**不取消上游任务**（`:307`：`未取消上游任务；prompt_id=`）。

> **对照我们**：`vm/state.py` 已实现「prompt_id 提交后立刻落盘 + /history 回收 + 原子写」，`vm/comfy.py` 已有 `/system_stats` 健康检查、`/queue` 位置、`wait()` 轮询、`interrupt()`。**我们缺的是 Slate 的两点**：(1) 排队宽限/区分「还在队列里跑」vs「ComfyUI 重启丢了」的显式判定；(2) 输出按扩展名兜底。另外 **17k+5 帧步进**要核对我们的实现。

---

# 二、MCWW（light-and-ray/Minimalistic-Comfy-Wrapper-WebUI）

## 2.1 仓库概况

- commit：`master`，`pushed_at: 2026-09-23`，**v2.4 刚加入 MiniMax H3 支持**（`Readme.md:3-4`, `Changelog.md:22`）。
- 140 star，AGPL-3.0，仓库 34.8MB 但**代码只有 ~15 个 Python + ~30 个 JS**，其余全是文档截图。
- 定位（`Readme.md:6`）：「a UI extension for ComfyUI adding an additional **inference focused** UI, that dynamically adapts to your workflows」—— 靠**节点标题约定**（`<Positive prompt:prompt:1>`、`<Seed:advanced:1>`、`<Result:output:1>`）自动生成推理界面，**不需要导出 API 格式**。
- 关键特性自述（`Readme.md:13-20`）：① 刷新/关闭页面不丢状态（**存在浏览器 local storage**，「It only resets on the project updates to prevent unstable behavior」）② 与 Comfy 共用同一份 workflow ③ **更好的队列**：「change the order or priority of tasks, pause/resume the queue, and don't worry closing Comfy / rebooting your PC during generations」④ 提示词 presets ⑤ batch 支持 ⑥ 图片编辑器。

## 2.2 advanced queue 到底有哪些能力（逐条代码核实）

队列实现：`mcww/queueing.py`（单例 `_Queue`）+ `mcww/processing.py`（单任务状态机）。

| 能力 | 有/无 | 实现与证据 |
|---|---|---|
| **优先级** | ✔ | `Processing.priority()` / `setPriority()`（`processing.py:87-91`）；取值 `defaultPriority=1`，上限 `queueMaxPriority=3`（`opts.py:293-294`）；UI 是**优先级单选组** `.mcww-queue-priority-radio`（`queue.js:270-327`） |
| **出队顺序** | ✔ | `iterateQueueProcessingLoop`（`queueing.py:288-311`）：取 queued 列表，`maxPriority = max(...)`，然后**从后往前扫**找到最后一个该优先级的项 → **同优先级内 FIFO** |
| **手动上移/下移** | ✔ | `moveUp/moveDown` → `_move`（`:443-471`）：**在数组里逐格交换，直到相邻两项 priority 不同才停**（即只在本优先级桶内移动） |
| **暂停/恢复** | ✔ | `togglePause/isPaused`（`:327-333`），`_paused` 标志；循环里 `if not self._paused and not self._inProgressId() and ...` 才出队（`:289`）；**恢复后当前任务继续，不重投** |
| **取消（排队中）** | ✔ | `cancel(id)`：QUEUED → ERROR，`error.text="Canceled by user", isCanceled=True`（`:380-388`） |
| **取消（进行中）** | ✔ | `interrupt()` → `unQueueComfy(prompt_id)` **然后** `interruptComfy(prompt_id)`，并置 `needUnQueueFlag=True`（`processing.py:181-186`）；下一轮 `iterateProcessing` 抛 `ComfyUIInterrupted("Unqueued")`（`:153-155`） |
| **软取消（跑完当前再停批）** | ✔ | `cancelBatchSoft(id)`（`:374-378`）置标志；出队前检查 `if processing.cancelBatchSoft:` → 直接转 ERROR「Cancelled after generation」（`queueing.py:299-301`） |
| **失败重试** | ✔（手动） | `restart(id)`：清 error，非 IN_PROGRESS 的 → QUEUED（`:365-372`）。**没有自动重试** |
| **并发控制** | ✔（硬约束为 1） | `_inProgressId()` 断言「more than 1 IN_PROGRESS」是异常（`:41-48`）；**同时只跑一个**，与我们单卡约束天然一致 |
| **队列持久化** | ✔ | `queue.bin` pickle（`initQueue`/`saveQueue`，`:493-513`），`AUTOSAVE_INTERVAL = 15` 秒（`:493`）+ 退出时保存（`mainUI.py:144-156`）；`maxQueueSize=200` 超出裁剪（`:478-487`） |
| **持久化门控（★）** | ✔ | `restoreKey`（`utils.py:189-196`）= `hash(baseStatesKey + queueing.py + processing.py + workflow.py 的内容哈希)`；**只有 key 一致才恢复队列**（`queueing.py:505`）。注释：防止代码更新后用旧队列做出不稳定行为 |
| **批处理** | ✔ | 批 = `batchCount × 文本 presets × media 输入`（`processing.py:71-85` `batchSizeTotal/batchSizeText/batchSizeMedia/batchSizeCount`），逐条推进 + `batchDone` 进度 |
| **进度上报** | ✔ | 轮询 `getResultsIfPossible(prompt_id)`（`processing.py:158`）；队列计数用 `batchSizeTotal() - batchDone`（`queueing.py:340-358`）；`_queueVersion` 自增做变更检测给前端 |

## 2.3 ComfyUI 断连 / 崩溃 / 超时怎么处理（★ 最有价值的部分）

`mcww/comfy/comfyAPI.py`：

**(a) 结果查询的三级判定**（`_getResultsInner`，`:60-96`）：
```python
def _getResultsInner(prompt_id):
    if _getQueue(prompt_id): return          # ① 还在 /queue（running 或 pending）→ 什么都不做，继续等
    history = _getHistory(prompt_id)
    if not history:
        raise UnqueuedByComfyUI("No prompt_id in history. Maybe ComfyUI has been restarted, "
                                "or the task(s) were unqueued inside ComfyUI")   # ② 不在队列也没历史 → 明确判定"丢了"
    status = history["status"]["status_str"]
    if status == "error":   # 细分 execution_error（带 exception_type/message）/ execution_interrupted
    elif status != "success": raise Exception(f"Unknown ComfyUI status: {status}")
```

**(b) 掉线识别 + 自动暂停**（`queueing.py:267-285`）：
```python
def _handleProcessingError(self, e, processing):
    processing.error = ProcessingError(errorText, isCanceled, e)
    processing.status = ProcessingStatus.ERROR
    if type(e) in [ComfyIsNotAvailable, UnqueuedByComfyUI]:
        self._paused = True          # ★ 自动暂停整个队列
    shared.api.progressAPI.voidProgressBar()
    self._queueVersion += 1
```
**这是最值得抄的一条**：遇到「ComfyUI 不可达」或「任务在 ComfyUI 里消失了」→ **不重投，直接把队列暂停并保留错误**，等人来看。理由写得很清楚：无法确认是否已被接收时重投 = 重复烧卡。这与 Slate 的设计文档 `:268` 同构：「请求超时且无法确认是否被厂商接收时进入提交结果待核对，**禁止重发 create**」。

**(c) 任务级取消的两个端点**（`:161-181`）：
```python
POST /interrupt      {"prompt_id": pid}      # interruptComfy
POST /queue          {"delete": [pid]}       # unQueueComfy
```
先 `unQueueComfy`（从队列删除）再 `interruptComfy`（中断执行）—— **两段式**，因为纯 `/interrupt` 对还没开始的任务无效。

**(d) 超时配置**（`opts.py:23-26`）：`REQUESTS_TIMEOUT_NORMAL` 默认 30s，`BIG = 4×NORMAL = 120s`，均可用环境变量覆盖。`enqueueComfy` 用 `increasedTimeout=True`（BIG）。`enqueueComfy` 后 `time.sleep(0.2)` 再返回。

**(e) 等待 ComfyUI 重启**（`waitForComfy(timeout)`，`:254-274`）：每 0.5s 轮询 `/models/loras`，200 即返回；超时打印并返回 False。

**(f) 崩溃恢复辅助**：
- `freeCacheAndMemory()` → `POST /free {"unload_models": True, "free_memory": True}`（`:241-251`）—— **★ 单卡显存紧张直接可用**。
- `getStats()` → `/system_stats`；`getConsoleLogs()` → `/internal/logs/raw`（用于把 ComfyUI 报错回显到 UI）。
- `restartComfy()`（`:196-221`）：`/manager/reboot` 三种 API 版本逐个回退（v4 → v3 → v4.2），只对 404/405 继续尝试。

**(g) 中断后如何不重复提交**：`Processing` 对象里保存 `prompt_id`，**pickle 进 queue.bin**（`processing.py:57`）。重启后 IN_PROGRESS 项仍带 prompt_id，`iterateProcessing` 直接拿它查 `/queue`+`/history` → **ComfyUI 还在跑就继续等，跑完了就把产物收回来** —— 这就是它敢说「don't worry closing Comfy / rebooting your PC during generations」的机制。

## 2.4 "Stable UI states" / presets 怎么实现

**(a) UI 状态：浏览器 localStorage + 代码哈希做 key**（★ 思路很妙）
- `getWorkflowUIStorageKey()`（`mcww_web/js/project.js:88-93`）：`workflow-ui-state-${pullData.outputs_key}` —— key 里带 workflow 的 outputs_key。
- `saveWorkflowUIState()`（`:96-134`）：扫描 `.need-save-state` 元素，**按元素序号**序列化状态数组：tabs 存 `selectedIndex`、accordion 存 `open`、checkbox 存 `checked`。
- 自动保存：`mcww_web/js/saveStates.js` —— `AUTO_SAVE_STATE_MS = 15000`；**页面失焦/切 tab 时改成每 1.5 秒存一次**（`:51-56`）；相机开着时不存；保存中则跳过并等待（上限 2s）。
- **服务端也有状态**：`mcww/ui/webUIState.py` 定义 `ProjectState`（`projectId` + 每个 element 的 save key → value + selected workflow + batchCount + priority），整体序列化成 `webUIStateJson`；其 storage key 用 `getStorageKey()`（`utils.py:199-205`）= `baseStatesKey + mode + hash(webUIState.py)`。
- **核心设计**：**状态 key 包含定义状态的源码哈希** → 代码/项目更新后 key 变了，旧状态自然失效，「It only resets on the project updates to prevent unstable behavior」。**不需要写迁移代码。**

**(b) presets：按 workflow 一个 JSON 文件**
- `mcww/presets.py`：`STORAGE_DIRECTORY/presets/<workflowName>.json`，结构 `{presetName: {elementKey: value}}`。
- 特殊键 `SAVED_FILTER_ELEMENT_KEY = "__savedFilter"` 表示「保存的筛选器」而非提示词 preset（`:33, 46-52`）。
- 支持：新增（可指定 `after` 插入位置）、删除、重命名、上下移、按新顺序整体重排（`applyNewOrder` 校验新旧 label 集合一致，`:107-113`）、`getPromptsInSamplesFormat` 导出成批处理矩阵。
- 批处理：presets 可**多选**组成 batch（`Readme.md:18`），`presetsFilterThreshold = 30` 超过就默认进入筛选模式（`opts.py:295`）。

## 2.5 对我们的适配度

**适合我们（单机、单并发、断点续跑）的**：
1. **`/queue {delete:[pid]}` + `/interrupt` 两段式取消**。
2. **「任务在 ComfyUI 消失 → 自动暂停队列 + 保留错误」而不是重投**（我们已检查 prompt_id 落盘，但缺这条"自动熔断"语义）。
3. **`POST /free {unload_models, free_memory}`** —— 我们 23.6/24GB，镜间切换模型/长跑前清显存有实际价值。
4. **队列状态与任务状态一起 pickle 持久化，恢复键 = 代码哈希**：我们已有 manifest+checkpoint，若要加"待办队列"，用「代码/契约版本哈希做 restore key」可以避免契约升级后旧队列乱跑。
5. **`restart` 手动重试 + `cancelBatchSoft` 跑完当前再停**：比我们的「中断即失败」更细腻，适合"我要出门，让它跑完这一镜就停"。
6. **UI 状态 key 带源码哈希 → 自动作废**：零迁移代码，非常适合我们这种快速迭代的零框架控制台。
7. **优先级桶 + 同桶内 FIFO + 只在本桶内移动**的组合（用一个整数 + `max` + 反向扫实现）**代码量极小**（`_move` 不到 25 行），值得直接移植成「角色定妆照优先于普通镜头」这类调度。

**不适合我们的**：
- Gradio 那套动态 UI 生成（节点标题约定）与我们的原生 JS 无关。
- 浏览器 localStorage 存 UI 状态：我们单页控制台可以考虑，但**渲染队列状态必须在服务端**（多标签页/多设备）。
- 它的持久化是 `pickle` 整个对象图 —— 我们自己用 JSON + 显式 schema 更安全（pickle 反序列化任意代码执行风险，Slate 也只用 JSON）。

---

# 三、ac-comfyui-queue-manager（abdullahceylan）

## 3.1 实情核实（结论：**不建议借鉴实现**）

- commit：`43c161a8a86179bcedaadb358785470a7cf09112`（**2025-09-27**，比另外三个项目老一年），18,684 行 Python。
- 形态：ComfyUI custom node（`__init__.py:30-39` 注册 `NODE_CLASS_MAPPINGS` + `WEB_DIRECTORY="./web"`），通过 `server.PromptServer.instance.app` 挂 REST 路由（`__init__.py:49-119`）。

**致命发现**：`workflow_executor.py:240-253` 的「执行工作流」是占位符：

```python
def _execute_workflow_directly(self, workflow_data: Dict[str, Any]) -> str:
    # This is a placeholder for actual ComfyUI integration
    # In a real implementation, this would interface with ComfyUI's execution system
    logger.info("Executing workflow directly through ComfyUI")
    time.sleep(0.1)   # Simulate processing time
    return str(uuid.uuid4())
```
并且结果里写死 `"execution_time": 0.1, "outputs": {}`（`workflow_executor.py:212-218`）。

**它不调用任何 ComfyUI 接口**：全仓 grep 无 `/prompt`、`/queue`、`/history`、`/interrupt`、WebSocket 调用（只有 `aiohttp` 用于挂自己的路由）。

**`priority` 是死参数**：
- 节点声明了 `"priority": ("INT", {"default": 0, "min": -10, "max": 10})`（`queue_manager_node.py:29`）并校验范围（`:179-181`）；
- 但 `models.py:37-56` 的 `QueueItem` **没有 priority 字段**；`database.py:35-46` 的 `CREATE TABLE queue_items` **没有 priority 列**；所有查询是 `ORDER BY created_at DESC`（`database.py:230,288,322`）。
- → **接收、校验、丢弃**。这是"README 宣称 vs 实现"落差最大的地方。

## 3.2 实际可用的能力（只有壳是真的）

| README 宣称 | 代码实情 |
|---|---|
| Persistent Queue（SQLite） | ✔ 真的：`database.py` SQLite + 索引（status/created_at/workflow_name） |
| Pause/resume queue | ⚠ 只是自己的 `queue_state` 配置字符串（`queue_service.py:359-401`），**不控制 ComfyUI 队列** |
| Priority | ✘ 死参数（见上） |
| Reorder | ✘ 无 |
| Retry on failure | ✘ 无自动重试 |
| Advanced filtering / search | ✔ 真的，`database.py:288-322` 动态 WHERE 拼接 |
| Archive / restore | ✔ 真的，`archive_service.py`（不归档 RUNNING 项） |
| Import/export | ✔ 真的，`import_export_service.py` |
| Real-time monitoring | ⚠ `execution_monitor.py` 后台线程轮询**自己数据库的状态**（`:245-265`），`workflow_executor` 是可选且为桩 → **状态只有别人写它才会变**，是个自指镜像 |
| QueueItem 状态枚举 | `pending/running/completed/failed/archived`（`models.py:14-22`）—— 这一层设计是合理的 |

## 3.3 唯一值得看一眼的东西

**一个「作业台账」的表结构与 REST 资源模型**（可作为我们 manifest 的对照，不是照抄）：

```
QueueItem = { id, workflow_name, workflow_data, status, created_at, updated_at,
              started_at, completed_at, error_message, result_data }
```
REST 设计（`api_routes.py:59-77`）：`GET/POST /items`、`GET/PUT/DELETE /items/<id>`、`DELETE /items/bulk`、`POST /items/filter`、`GET /search`、`GET /status`、`POST /pause|/resume`、`POST /archive|/restore`、`POST /export|/import`、`GET/PUT /config`。

值得注意的两个小细节：
- **时间四件套**（`created_at/updated_at/started_at/completed_at`）分列记录 —— 我们 manifest 目前只有产物记录，加上 `started_at/completed_at` 可以直接算每镜耗时（用于预估总时长/发现变慢）。
- **`workflow_data` 与 `result_data` 分开存**：请求与结果分离，重复执行不会丢原请求。

**结论**：如果我们的目标是「自己拥有一个作业台账」，Slate 的 `creation.json` + `.comfy-task.json` 是更好的模板（它同时解决了幂等、nonce、结果待核对）；acqm 只贡献「表结构长什么样」的直觉。

---

# 四、storyboard-master（c-wang-dev）

## 4.1 定位与体量

- commit：`2897c5773a20127870b9498654650f4b59406f56`（2026-08-26）。
- **它不是分镜编辑器，是"分镜决策端 Agent"**：`README.md:15` 「解析剧本片段，输出可解释的视听决策与高质量提示词包，由下游生图/生视频工具执行」。
- 组成：`engine/`（**1,484 行 Python，零第三方依赖**，含 18 个单元测试）+ `knowledge/`（32 份 Markdown 知识库）+ `agents/` + `skills/`（WorkBuddy 智能体包）。
- **不是 UI 工具，没有数据库，没有时间轴**。输出是单次 JSON「提示词包」。

## 4.2 数据模型与字段

**扁平结构**，无 project→episode→scene→shot 层级。一次运行 = 一段剧本 → 一个 JSON（示例见 `engine/examples/分镜输出示例_LLM版.json`）：

| 顶层字段 | 类型 | 说明 |
|---|---|---|
| `model` | string | 模型卡 id（`seedream-image-v5.0-pro` 等） |
| `duration_tier` | enum | **`5s`/`10s`/`15s`** |
| `keyframes` | int | 关键帧数 |
| `features` | object | 六维特征 + 派生 `info_point_count`/`duration`/`keyframes` |
| `scenes` | [{location, time}] | 场景与时间 |
| `characters` | string[] | 出场角色名 |
| `audio_visual_params` | object | `{景别, 角度, 运镜, 光影, 节奏}` |
| `grammar` | object | `{场景类型, 人物数量, 机位语法, 景别序列, 运镜, 构图, 节奏, 越轴提示, 匹配原则}` |
| `shot_sequence` | string[] | 逐帧景别序列 |
| `prompt_pack` | object | `{prompt, negative_prompt, params, consistency}` |
| `frames[]` | object[] | 逐帧：`{index, shot, angle, action, prompt}` |
| `consistency` | object | 参考图建议 |
| `param_block` | object | 模型参数块（比例/尺寸/负面词/提示词公式） |
| `character_assets[]` | object[] | **角色锚定卡 + 定妆照提示词** |
| `quality_check` | object | `{passed, total: 12, issues[]}` |
| `degraded` | string | 如 `no_api_key`（**优雅降级标记**） |

**六维特征**（`decision.py:29-38`）：`info_focus` 信息焦点 / `power` 权力关系 / `move_purpose` 运动目的 / `emotion_tone` 情绪基调 / `content_type` 内容类型 / `emotion` 情绪强度 / `pace` 节奏倾向。

## 4.3 镜头语言枚举（★ 这就是我们要的受控词表）

**(a) 五张决策表**（`knowledge/分镜决策引擎.md`，被 `decision.py` 消费）：

| 表 | 输入 | 输出取值 |
|---|---|---|
| 景别表 | 信息焦点：环境/世界观/位置 · 人物与环境/动作幅度 · 双人关系/身体语言/对白 · 情绪转折/关键反应 · 细节/眼神/手部/道具 | 远景/大远景 · 全景 · 中景 · 近景/特写 · 大特写 |
| 角度表 | 权力关系：强势/威压 · 弱势/被支配 · 对等/客观 · 代入主观 · 世界失衡 | 仰拍 · 俯拍 · 平视 · POV · 荷兰角 |
| 运镜表 | 运动目的：主体移动/追击 · 悬念揭晓/视线聚焦 · 段落结束/离开 · 建立关系/关联 · 不安/纪实 · 对峙/压抑 | 跟/移 · 推近 · 拉远 · 摇 · 手持 · 固定机位 |
| 光影表 | 情绪基调：压抑/危险/绝望 · 希望/安全 · 神秘/悬念 · 悲怆/牺牲 · 诡异(室内) · 威胁/月夜 | 低调+冷色 · 高调+暖色 · 硬侧光半明半暗 · 逆光剪影 · 顶光/底光 · 冷月光硬侧光（**四要素=光源/方向/质感/色温**） |
| 节奏表 | 情绪强度/内容：对白 · 动作 · 悬念/揭示 · 环境 · 高潮 | 3~5s 少切 · 1~2s 快切 · 缓慢推近+停帧 · 慢长镜头 · 快速剪辑+特写堆叠 |

**(b) 仲裁层（写死的优先级）**：`情绪基调 > 信息层级 > 权力关系`（`decision.py:58-68`）：
```python
if emotion in ("紧张","爆发") and "远景" in params["景别"] or "全景" in ...:
    params["景别"] = "近景 / 特写"      # 情绪优先，覆盖信息层级
if emotion == "爆发" and params["运镜"] == "拉远":
    params["运镜"] = "快速剪辑 + 特写堆叠"
```
**这是"同样输入永远同样输出"的确定性来源。**

**(c) 电影语法**（`grammar.py:11-54`）—— 按内容类型给出**景别序列**（不仅单镜，而是整段的递进）：
| 内容类型 | 景别序列 | 运镜 | 节奏 |
|---|---|---|---|
| 悬念 | 大远景 → 全景 → 特写 | 缓慢推近 + 上摇 | 慢(蓄力) |
| 揭示 | 全景 → 近景 → 大特写 | 推近 + 停帧 | 慢(蓄力) |
| 对白 | 全景 → 近景 → 特写 → 大特写 | 固定机位 + 仰拍 | 中(留气口) |
| 动作 | 全景 → 近景 → 中景 → 大特写 → 全景 → 中景 | 手持跟拍 + 快速推近 | 快(1-2s/镜) |
| 情绪 | 全景 → 近景 → 特写 | 固定 + 慢推 | 慢 |
| 环境 | 大远景 → 全景 | 摇/航拍 | 慢 |

**(d) 人物数量 → 机位语法**（`grammar.py:57-62`）：
```
1人：主观 POV 或客观交代；特写需情绪动机
2人：外反拍/内反拍/过肩；保持关系线一侧
3人：三角形布局 + 枢轴演员
4+：群像：主镜头先行 → 局部切入 → 反应镜头
```
外加 `越轴提示`（≤2 人「无越轴风险(单一纵深轴线)」；≥3 人「多人物注意轴线：用中性镜头过渡」）和 `匹配原则`：**「位置匹配 + 动作匹配 + 视线匹配（切点在动作中段）」**。
还有「收尾检测」：`move_purpose == "离开"` → 强制替换为「大远景 → 中全景 / 拉远+固定 / 逆光剪影 / 慢(留白)」（`grammar.py:84-91`）。

**(e) 内容类型判别铁律**（`分镜决策引擎.md`）—— 很实用的一条：
> 「铁律：**有动作 ≠ 动作**。看动作的**目的**，不看动作的数量。」
> 例：「打完收刀再放话」→ 对白；「挥刀但目的是威慑」→ 悬念；「黑影离去+收刀入鞘」→ 情绪收尾。

## 4.4 「镜头时长 vs 台词长度」有没有自动计算？

**没有。这一项明确不存在。** 依据：
- 全仓 grep `时长/秒/字数/语速/speech` 于 `parser.py`/`frame.py`/`quality.py`，**没有任何"台词字数 ÷ 语速"或"台词时长"计算**。
- 实际做法是**「数信息点定档位」**（`decision.py:71-79`）：
```python
def select_duration(info_point_count: int) -> str:
    if info_point_count <= 1: return "5s"
    if info_point_count <= 3: return "10s"
    return "15s"
```
其中 `info_points = len(dialogues) + len(actions)`（`parser.py:190`）—— **台词条数 + 动作条数**，不是字数。
- 再按「档位 × 节奏」交叉表定**图数**（`decision.py:82-113`）：
```python
table = {"5s": {"慢":(1,2), "中":(2,2), "快":(3,6)},
         "10s":{"慢":(2,4), "中":(3,5), "快":(5,10)},
         "15s":{"慢":(4,8), "中":(5,8), "快":(10,20)}}
# 再按运镜复杂度修正：固定(1,1) / 推近(2,2) / 拉远(2,2) / 摇(2,2) / 跟移(2,3) / 手持(2,3)
```
- 视频换算 `video_segments`：5s→1 段 2 帧；10s→2 段 4 帧；15s→3 段 6 帧。

> **对我们的意义**：它用「信息点」而不是「字数」定档，**对我们的中文短剧其实不够用**（我们有明确的台词文本，中文语速可量化）。但它给了一个**可用的档位骨架（5/10/15s）**和「图数 = f(档位, 节奏, 运镜)」的**二维交叉表**思路，这个可以移植成"该镜需要几张关键帧/参考图"的决策，避免每镜都只用 1 张定妆照。

## 4.5 ID / 编号 / 状态 / 版本

- **编号**：`S1`、`S2`… 由决策引擎生成，**扁平递增，无 scene 层级**。没有重排/插入的重新编号逻辑（因为一次运行只处理一个片段，没有编辑态）。
- **状态/版本：完全没有**。没有 draft/approved/generating/failed，没有 take 号，没有版本管理。这是工具形态决定的（它是"生成一次就交出去"的决策端，不是生产管理系统）。
- **导出**：只有 JSON；**没有字幕/EDL/CSV/XLSX/时间轴对齐**。
- **参考图/一致性**：有，且是它的一个卖点（`README.md:58`「角色表演一致性（四层）」）：
  - **L1 性格范式库**（8 范式）→ **L2 角色锚定卡**（一次绑定全剧生效）→ **L3 禁止表演清单**（闸门）→ **L4 远景体态降级**。
  - 角色锚定卡字段（示例 JSON `character_assets[].anchor_card`）：`{角色名, 性格范式, 正反派, 人格类型, 标志微表情, 情绪矩阵, 英文提示词, 禁止表演, 常驻声明}`。
  - **情绪矩阵**是分档的（轻/中/重/爆发）：如「怒：眼神变硬、抿嘴(轻) → 下颌绷紧、拳头攥紧(中) → 声音发颤的质问(重) → 极罕见爆发(爆发)」。
  - `禁止表演`：如「轻浮嬉笑、夸张咆哮、轻易落泪」—— **负面约束直接挂在角色上，而不是每镜重写**。
  - **别名归一防双脸** + 定妆照/场景设定图**双参考图** + 时间同义归一（`README.md:59`）。
  - 模型卡**死锁**：选定模型后锁定对应参数卡，**杜绝跨模型参数污染**（`README.md:57`）。

## 4.6 质量自检（★ 可以直接变成我们的机械质检项）

`quality.py` 12 项，逐项有 `item/level(error|warning|info)/advice`（`quality.py:8-113`）：
```
1 模型分流  2 风格一致性  3 景别  4 角度  5 运镜  6 光影
7 一致性建议  8 负面词  9 语法体现(逐帧)  10 负面词  11 档位秒数  12 表演一致性
```
关键校验逻辑举例：
- 「逐帧数 ≠ 图数」→ error + advice（`:80-82`）。
- 「`duration_tier` 不在 (5s,10s,15s)」→ error「非法档位」（`:94-97`）。
- 表演一致性未接入时 → **info 而非 error**（优雅跳过，`:113`）。

`frame.py:41-47` 还有「动作顶点帧」选取：`content_type=="动作" and len(actions)>=3` → 取 `actions[len(actions)//2]` 作为该帧动作（**不是简单按 index 取模**）。

## 4.7 诚实评价

- **优点**：`decision.py` + `grammar.py` 是**真代码、有 18 个单元测试、纯确定性**，可读性和可移植性都很好（总共不到 300 行）。知识库把「决策表」以 Markdown 表格形式维护、代码里读同一个 Markdown（`decision.py:41` 读 `knowledge["tables"]`）—— **文档即规则表**，这个模式很聪明。
- **缺点/局限**：① 完全没有状态/版本/时间轴/导出，**不是生产工具**；② 输出 JSON 里 `prompt` 是**模板拼接**（`"林风、黑衣人位于深夜的镖局大院，顶光/底光光影，近景/特写平视景别视角，固定机位运镜，写实电影质感，电影级摄影。"`），**信息量低于我们现在的 H3 长提示词**；③ `frames[].action` 在示例里是空字符串，说明规则解析路径下动作/台词抽取偏弱；④ 「9.8/10 分」这类自评数据是作者自测，**不应作为质量证据**。

---

# 五、横向对比：四个项目 vs 我们

| 维度 | Slate | MCWW | acqm | storyboard-master | **我们现状** |
|---|---|---|---|---|---|
| 分镜契约 | ★★★★★ 三层契约 + 校验器 + 词表回退 | ✘（依赖 ComfyUI workflow 节点标题） | ✘ | ★★★ 扁平决策输出 | ★★ `id/sec/chars/seed/prompt/shot_size/camera/dialogue/narration/costume` |
| 提示词分层 | ★★★★★ image/video/grid 三类 + 来源标注 | ★★ presets 文本复用 | ✘ | ★★ 单 prompt + negative | ★ single `prompt`（H3 长模板内联） |
| 资产引用 | ★★★★★ `@kind:id` + 修订号 + 依赖图 | ✘ | ✘ | ★★ 角色锚定卡（文本） | ★★ `chars` 名字 + `costumes.json` |
| 幂等/指纹 | ★★★★★ 消费阶段化指纹 + asset_revision + artifact_hash | ★★★ queue restoreKey（代码哈希） | ★★ DB 状态 | ✘ | ★★★★ 单指纹 manifest 三态 |
| 断点续跑 | ★★★★★ prompt_id 落盘 + nonce + 待核对 | ★★★★★ prompt_id pickle + /queue+/history 三级判定 | ✘ | ✘ | ★★★★ prompt_id 落盘 + /history |
| 队列语义 | ★★★ 异步 job + 幂等 nonce | ★★★★★ 优先级/暂停/软取消/重排/手动重试 | ★ 壳 | ✘ | ★★ 单并发顺序 + taskctl |
| 失败处理纪律 | ★★★★★ 不重发 create、区分技术/创作状态 | ★★★★★ 不可达→自动暂停不重投 | ✘ | ★★ degraded 标记 | ★★★ |
| 版本/回滚 | ★★★★★ `.versions/` 20 份 + expected_revision | ★★ presets 重排 | ★★ 归档 | ✘ | ★★ 抽卡候选 |
| 运行时状态机 | ★★★★ acting 六态 + stale | ★★★★ QUEUED/IN_PROGRESS/COMPLETE/ERROR + batch | ★★ 五态（伪） | ✘ | ★★ |
| 质检 | ★★★ 契约校验 + 引用/时长越界告警 | ★★ 日志回显 | ★ 健康检查 | ★★★★ 12 项自检 + 九要素 | ★★★ qc.py 机械质检 |
| UI | Vue（非零框架） | Gradio | web/ | 无 | ★★★ 原生 JS 单页 |

---

# 六、值得偷的 Top 10

排序依据：**对我们现有痛点的杠杆率 × 落地成本**。每条给「做法 / 依据 / 我们具体怎么改」。

### 🥇 1. `prompt` 拆成 `prompt_image` / `prompt_video`（+ 可选 `prompt_grid`）

- **做法**：同一份结构化事实，派生**用途不同**的两段文本。`prompt_image` 只写**一个可辨认瞬间**（姿态/位置/视线/景别/光线），**不写连续阶段、不写运镜过程**；`prompt_video` 写**开始状态→动作发展→结束状态**+机位运镜+节奏+声音+台词。契约要求「三个字段内容不同、不得互相补齐」，且 `require_prompts()` **缺失或重复就拒绝生成**。
- **依据**：`production_prompts.py:6`（`FIELDS`）、`:23-35`（CONTRACT）、`:62-68`（`require_prompts` 拒绝以一类内容补齐另外两类）；`StudioShotsView.vue:543-544` 两个独立编辑框；设计文档 `:178`「避免一个文本覆盖两种用途」。
- **我们怎么改**：`shots[].prompt` → 保留为兼容别名（= `prompt_image`），新增 `prompt_image`（定妆/关键帧/参考图用）与 `prompt_video`（H3 用）。**收益**：我们现在的角色定妆照和逐镜视频共用一个 prompt 字段，改一个必然污染另一个；分开后**改视频描述不会触发定妆照重生成**（配合第 2 条）。

### 🥈 2. 指纹按「消费阶段」拆开，而不是一个扁平指纹

- **做法**：`media_source_hash(shots, kind)` —— `kind='image'` 取 `{id, scene_ref, actor_refs, prop_refs, action, content, shot_size, angle, lighting, negative, prompt_image, shared_negative}`；`kind='video'` 取上述共有字段 + `{dur, prompt_video, lines, camera_move}` + **引用的关键帧 sha256** + unit 级 `{id, duration, prompt_grid, negative, generation_options}`。注释直白：「**按消费阶段计算依赖：视频/宫格文字修改不会使静帧无端失效。**」
- **依据**：`production_prompts.py:95-108`。
- **我们怎么改**：`vm/state.py:shot_fingerprint()` 目前是单一 `v1` 指纹（`state.py:84-96`）。改成 `shot_fingerprint(kind, ...)`，`kind ∈ {ref_image, clip, qc}`，每个 kind 取自己的字段子集并各自入库。**收益**：现在改一句台词（`dialogue`）会导致该镜重渲并重跑质检；拆开后只有 `clip` 失效，`ref_image` 与静帧相关产物保留。这是**直接省 GPU 时间**的一条。

### 🥉 3. 资产修订号 `asset_revision` + 依赖图，替代「参考图 mtime 指纹」

- **做法**：每个资产有单调递增 `asset_revision`；`bump_asset_revision()` 在资产改动时 +1；`asset_dependency_refs()` 沿 `parent_ref`/`related_refs` **递归展开**依赖闭包（子素材/服装变体继承父素材失效）；镜头记录生成时用到的 `asset_revisions` 快照，比对不上即 stale；依赖闭包内任一资产变更 → **精确失效受影响的镜头**，而不是全片重渲。
- **依据**：`production_state.py:23-36`（revision）、`:38-63`（依赖闭包展开）、`production_prompts.py`/镜头 `asset_revisions`；设计文档 `:120` 为什么内容指纹不含绑定字段。
- **我们怎么改**：`costumes.json` / 角色定妆照加 `asset_revision`；`shots[].chars` 与 `costume` 改为**引用稳定 ID**（`@character:tangseng`、`@costume:tangseng_robe_b`）；manifest 记录 `asset_revisions` 快照。**收益**：改一个角色的服装变体时，目前要么全片重渲要么靠人工判断；有了依赖图可以精确到"只重渲该角色出场的 8 个镜头"。

### 4. `lines[]` 镜内台词轨（带 `at`/`dur`/`speaker`）+ `narrator` 不是角色

- **做法**：一个镜头内可有多条台词，每条带**镜内相对时间** `at`(秒) + `dur`(显示时长) + `speaker` + `line`；超出镜长时**夹取并告警**而非丢弃。旁白/画外音固定用 `speaker="narrator"`，且**明确定义 narrator 不是角色**：不占 actors、不进 `actor_refs`/`asset_refs`、不给站位、**绝不出现在画面与 prompt 里**（只进台词轨供后期配音）。
- **依据**：`storyboard_schema.md:37-39`（lines 轨）、`creation_pipeline.py:732-746`（speaker 归一 + 越界夹取告警）、`prompt_modules.py:340-341`（narrator 例外条款）、`workbench/README.md:103`（旁白为独立台词轨，不生成"旁白人物"资产）。
- **我们怎么改**：`shots[].dialogue`（单条）→ `shots[].lines[]`。**收益**：(a) 一个长镜头内多句对白现在无处表达；(b) 我们的 LLM 拆镜如果生成了一个"旁白"角色会污染角色表和定妆照流程 —— 现在只需一个保留字 `narrator`；(c) 有 `at/dur` 后可以机械校验「这条台词念得完吗」（配合第 5 条）。

### 5. 受控词表 + 契约归一 + 校验失败拒绝落盘

- **做法**：① 三个镜头语言维度定成**闭集枚举**（`shot_size` 8 值 / `camera_move` 15 值 / `angle` 8 值 / `transition` 9 值）；② LLM 输出后由确定性代码**归一**：词表外的值回退到首项、`dur` clamp 到 `[1.5,15]`、id 重复改名、数值字段兜底；③ 全部通过才落盘，**有 error 就 `sys.exit(1)` 不写半成品**；④ 词表外的值在拉片侧只**告警不阻断**（保守）。
- **依据**：`prompt_modules.py:329-334`（词表）、`creation_pipeline.py:714-731`（归一）、`:798-802`（校验失败拒绝写盘）、`validate_analysis.py:19-24`（VOCAB + LEGACY 只警告）、`creation_pipeline.py:697-699`（LLM JSON 不完整拒绝写入）。
- **我们怎么改**：`vm/shots.py` 里给 `shot_size`/`camera` 加枚举 + 归一函数（我们 `camera` 现在是自由文本 `"缓推（Push In, small amplitude, slow speed）"`，这是**英文渲染细节泄漏进中文枚举位**）。改成 `camera_zh`（枚举）+ `camera_params`（`{type: push, amplitude: small, speed: slow}`）分离。**收益**：可以对「同场景连续两镜的景别是否递进」「有没有越轴」这类规则做机械质检。

### 6. 立即可校验的时长/台词纪律：`at+dur ≤ dur` + 时长单一提交值

- **做法**：① 每条台词的 `at+dur` 必须落在镜长内，超出就**夹取并打印告警**（不是静默截断）；② 时长分三层记录：原始 `shot.dur` / 规划 `planned_seconds` / 用户覆盖 `override_seconds`，**实际提交值 = `override ?? planned`**，手动改总时长**绝不回写 `shot.dur`**；③ 超模型上限 → **提交前报错，不静默改回默认值、不偷裁参考图**；④ 实际文件时长由 **ffprobe 回读**，不冒充请求时长；⑤ 模型要求整数秒/帧步进时按精度重分配，保证**无重叠无空洞、末端严格等于总时长**。
- **依据**：`creation_pipeline.py:738-742`（夹取告警）、三层视频设计 `:186-197`；预处理 `comfyui_h3.py:20-23`（帧步进 `frames += (5 - frames%17)%17`）；`comfyui_client.py` 注释（ComfyUI H3 帧步进会产生实际时长差异，UI 展示预计可执行时长并冻结进任务）。
- **我们怎么改**：`shots[].sec` → 加 `planned_seconds` / `override_seconds` / `actual_seconds`（ffprobe 回填）。**这是我们目前最容易出错的地方**：H3 的 `frames%17` 步进会让"请求 6 秒"实际出 5.96 或 6.04 秒，concat 时若按请求时长算时间轴就会累积漂移。

### 7. 「队列熔断而非重投」+ 双段式取消 + 显存释放

- **做法**：① 区分「结果查询」的三种情况：**还在 `/queue`** → 继续等；**不在队列但在 `/history`** → 收产物；**两边都没有** → 抛 `UnqueuedByComfyUI("Maybe ComfyUI has been restarted")`，并**把队列自动暂停**（`_paused = True`），**绝不重投**；② 取消要两段：先 `POST /queue {"delete":[pid]}`（对未开始的）再 `POST /interrupt {"prompt_id":pid}`（对进行中的）；③ 轮询超时**不取消上游任务**；④ 需要时 `POST /free {"unload_models":true,"free_memory":true}` 清显存。
- **依据**：MCWW `mcww/comfy/comfyAPI.py:60-96`（三级判定）、`:161-181`（unQueue + interrupt）、`queueing.py:267-285`（`_handleProcessingError` 里对 `ComfyIsNotAvailable/UnqueuedByComfyUI` 自动 `_paused=True`）、`:241-251`（`/free`）；Slate `comfyui_client.py:374-385`（排队宽限）+ `:307`（超时不取消上游）+ 设计文档 `:268`（结果不明禁止重发 create）。
- **我们怎么改**：`vm/comfy.py:wait()` 现在超时直接抛错。改成：(a) 超时先查 `/queue` 位置，仍在队列则宽限并打日志；(b) 「不在队列也无 history」→ 抛出**专用异常**，由 `pipeline.py` 捕获后**把整个 run 标记为 `awaiting_review` 并停止**，而不是继续下一镜或重投；(c) 在每镜开始前若检测到显存压力，调用 `/free`。

### 8. 「V 分镜视频」聚合层：把 2–3 个短镜合并成一次模型请求

- **做法**：在 shots 之上加 `video_units[]`：`{id(稳定), label, shot_ids[], scene_ref, shared_negative[], duration{planned,override}, summary, beats[], reference_strategy, continuity, selected_video_item_id, source_hash}`。分组规则**确定性**：按原顺序，**相邻且 `scene_ref` 相同**的 S 归为一个 V；非相邻同场景**不自动拼**；缺失场景就一镜一 V。校验：**必须按原顺序完整覆盖全部 S，不能漏镜/重复/调序**。组合提示词按**时间段**组织：`0–3秒 / S1：景别；角度；焦距；运镜；动作与视线；台词与环境声`，且**「时间和图片序号由编译器填入」**，不靠 LLM 数编号。
- **依据**：三层视频设计 `:73-93`（结构）、`:125-135`（分组规则 + LLM 边界）、`:144-153`（组合提示词形态 + 「LLM 输出引用稳定业务 ID，最后才转换成模型接受的素材序号」）、`production_studio.py:46-60`（`default_units` + `validate_units`）。
- **我们怎么改**：H3 时长窗口是 4–15 秒/次，我们现在**逐镜一次请求**，3 秒的镜头也在跑一次完整 H3 推理（显存占用与 15 秒相同！）。在 `shots` 之上加 `units[]`，同场景相邻短镜合并成一次 9–12 秒请求，**切镜由模型在段内完成**。这是**单卡吞吐最直接的提升**。注意设计文档 `:131` 的纪律：本地硬校验「覆盖完整、顺序不变、无重复、时间有效、引用有效」，且「**不能把 JSON 合法等同于内容正确**，硬校验失败不得进入视频请求」。

### 9. 表演层：把「表演」从散文变成可校验的 beat 数组（含"角色不该知道的事"）

- **做法**：每个角色在每个镜头产出 `beats[] = {at, duration, intent, posture, gaze, gesture, expression, voice, evidence_fact_ids}`，白名单字段。硬规则：`at+duration` 必须落在镜内；**演员层不得改 `dur/cam/pos/look/move/lines`**；不得新增台词/角色/伤势/服装/持物变化；校验失败类型明确（越界、**知识泄漏**、未知角色、固定字段变更）→ invalid，**最多一次程序纠错**，语义自检只 warning。另加 `acting_context`：`facts[]`（默认对角色不可见，需经 `events[].deltas` 加入 `known_facts`）、`actor_cards`（`personality/goal/relationship/expression_rules/arc_stage/locked_fields`）、`continuities[]`（分支）、`acting_status ∈ {pending, context_ready, ready, stale, locked, invalid}`。
- **依据**：`演员表演契约.md:7-16`（上下文）、`:21-47`（beats 白名单 + invalid 类型）、`:49-59`（应用/编译/stale 回退 baseline）；`prompt_modules.py:376-418`。
- **我们怎么改**：不一定要上完整连续性图，但**两个子集性价比极高**：① 在有台词/走位的镜头上，让 LLM 输出 `beats[]`（哪怕只有 `at/duration/posture/gaze/gesture/expression`），机械校验 `at+dur ≤ sec`、角色必须在场、不得新增未登记角色 → **把现在只能靠肉眼看片的表演一致性变成可回归的检查**；② `facts[]` + `known_facts` 的「角色不应知道后面的事」概念，可以简化成一条 LLM 提示词硬规则 + 一个 `known_facts` 列表，避免"第 3 镜的角色说出了第 10 镜才发生的事"。

### 10. UI 状态 key 带源码/契约哈希 → 更新即自动作废，零迁移代码

- **做法**：持久化 key = `hash(baseStatesKey + mode + 定义该状态的源码文件哈希)`（MCWW `utils.py:189-205`，用于 UI 状态与队列 restoreKey）；队列只在 key 匹配时恢复（`queueing.py:505`）。README 一句话总结：「**It only resets on the project updates to prevent unstable behavior.**」Slate 的对应物是**契约版本字段**（`prompt_version`、`production_schema_version: 1`）+ `script_rev` vs `board_rev` 的陈旧判定。
- **依据**：MCWW `utils.py:189-205`、`queueing.py:493-513`、`Readme.md:14`；Slate 设计文档 `:61`（`production_schema_version`）、`StudioShotsView.vue:52-54`（`boardStale`）、`creation_pipeline.py:772`（写 `prompt_version`）。
- **我们怎么改**：① 在 `project.json` 或分镜表顶层加 `schema_version` 与 `prompt_version`；② 控制台前端的状态 key 用 `vm/shots.py` 的内容哈希（或 `schema_version`），**契约一变旧 UI 状态自动失效**；③ 镜头级加 `script_rev`（小说章节指纹）并在 UI 标 `stale`。**这条成本几乎为零，收益是免除一整类"改了契约后旧数据静默跑错"的 bug。**

**（备选第 11 条，若还想要一条）**：`storyboard-master` 的**五张决策表 + 仲裁优先级**（`情绪基调 > 信息层级 > 权力关系`）与**景别序列/机位语法/越轴提示**，可以直接做成 `vm/plan.py` 拆镜后的**确定性后处理**：LLM 出镜 → 规则表校验/修正 →「同场景对话戏优先正反打、情绪升级才切特写、≥3 人给越轴提示」。Slate 自己也在 prompt 里写死了同样的规则（`prompt_modules.py:338`：「对话戏优先正反打：A 说→ots(A,B)，B 答→ots(B,A)，情绪升级才切 cu；轴线不许跳」）。

---

# 七、我们的分镜表 schema 应该怎么演进

## 7.1 现状

```json
{"id":"1-1-01","sec":6,"chars":["唐僧"],"seed":3100,
 "prompt":"subject_definitions: <Subject 1> = ...",
 "shot_size":"远景","camera":"缓推（Push In, small amplitude, slow speed）",
 "narration":"师徒四人一路向西。","dialogue":null}
```
52 镜实测：`id/sec/chars/seed/prompt/shot_size/camera` 100% 出现；`dialogue` 40/52；`narration` 4/52。角色/服装另有 `costumes.json` 等旁挂登记。

## 7.2 演进原则

1. **向后兼容增补，不重写旧分镜**（Slate 设计文档 `:57`：「继续以分镜 JSON 为内容真源，**向后兼容增补字段**，不重写旧 shots」）。
2. **每个新字段要么能被确定性校验，要么能被消费**；纯装饰字段不加。
3. **一处事实一个字段**，派生值（如 `move = 景别·运镜·角度`）不落盘。
4. **枚举 vs 参数分离**：中文枚举给规则和 UI，英文渲染参数给提示词。

## 7.3 建议的 v2 字段表

### 顶层（新增/变更）

| 字段 | 类型 | 说明 | 来源 |
|---|---|---|---|
| `schema_version` | int | 契约版本，用于 UI 状态失效与迁移判定 | Slate `production_schema_version` |
| `prompt_version` | string | 生成该分镜的提示词版本 | Slate `creation_pipeline.py:772` |
| `script_rev` | string | 小说章节内容指纹（如 md5 前 12 位）；`> board_rev` 则分镜过期 | Slate `script_rev` |
| `width`/`height`/`fps` | int | 从 `project.json` 提升到分镜表（**冻结**，避免后续改配置导致历史成片不可复现） | Slate `w/h/fps` |
| `style_ref` | string | 画风契约引用（如 `style.json#image`） | Slate `剧本/style.json` + skill |
| `characters[]` | object[] | **角色登记移到分镜表或显式引用**：`{id, name, aliases[], ref_image, asset_revision, anchor_card{…}, forbidden[]}` | Slate actors + storyboard-master 锚定卡 |
| `scenes[]` | object[] | `{id, name, interior, ref_image, asset_revision}` | Slate `素材/场景.json` |
| `units[]` | object[] | V 层聚合（见下） | Slate `video_units` |
| `shots[]` | object[] | 见下 | — |

### `shots[]`（v2 建议）

| 字段 | 类型 | 必填 | 说明 | 来源 |
|---|---|---|---|---|
| `id` | string | ✔ | 不变；建议补 `scene_id` 前缀语义 | — |
| `sec` | number | ✔ | 保留原名（兼容），语义=**规划时长** | — |
| `sec_override` | number\|null | — | 用户覆盖时长；**实际提交 = `sec_override ?? sec`** | Slate `override_seconds` |
| `sec_actual` | number\|null | — | **ffprobe 回读的真实时长**，不冒充请求值 | Slate 设计文档 `:188` |
| `scene_id` | string | ✔ | 场景引用（决定能否与相邻镜合并进一个 unit） | Slate `scene_ref` |
| `chars` | string[] | ✔ | 改为**角色 ID 数组**（不再是人名） | Slate `actor_refs` |
| `props` | string[] | — | 道具 ID 数组 | Slate `prop_refs` |
| `costume` | object | — | `{char_id: costume_id}` 映射（现在是单个字符串，多人同镜会歧义） | Slate 资产派生关系 |
| `shot_size` | enum | ✔ | 大远景/远景/全景/中景/中近景/近景/特写/大特写（**保持中文枚举**） | Slate + storyboard-master 同值 |
| `angle` | enum | ✔**新增** | 平视/俯视/仰视/鸟瞰/虫视/荷兰角/过肩/主观 | Slate `angle`（8 值） |
| `camera` | enum | ✔ | 固定/推/拉/摇/移/跟/甩/升降/环绕/手持/斯坦尼康/变焦/轨道/无人机/主观（**把现在的自由文本收成枚举**） | Slate `camera_move`（15 值） |
| `camera_detail` | string | — | 现在的 `"Push In, small amplitude, slow speed"` 移到这里（英文渲染参数与中文枚举分离） | 我们的现状 |
| `lens` | string | — | 焦距，如 `85mm`；可由 `shot_size` 自动补齐 | Slate `lens` |
| `rig` | enum | — | 固定/手持/滑轨/轨道/斯坦尼康/无人机/稳定器；由 `camera` 映射 | Slate `rig` |
| `transition` | enum | — | 硬切/叠化/淡入/淡出/闪白/划像/匹配剪辑/蒙太奇/无；**concat 阶段要消费它** | Slate `transition` |
| `lighting` | string | ✔**新增** | 光影一句话（主光方向/明暗比/色温） | Slate `lighting` |
| `action` | string | ✔**新增** | 「谁做什么」——可执行动作，**与画面描述分开** | Slate `action` |
| `sound` | string | — | 台词之外的环境声/音效/音乐（≤20 字）；H3 出的音频要用 | Slate `sound` |
| `negative` | string[] | ✔**新增** | 本镜禁止项（≤4 条）；H3 的负面约束现在只在模板里 | Slate `negative` |
| `prompt_image` | string | ✔**新增** | 关键帧/定妆/参考图用的提示词（= 旧 `prompt` 的兼容别名） | Slate `prompt_image` |
| `prompt_video` | string | ✔**新增** | H3 逐镜视频提示词（开始→发展→结束） | Slate `prompt_video` |
| `prompt_grid` | string | — | 宫格故事板说明（若未来用多格参考图） | Slate `prompt_grid` |
| `lines[]` | object[] | — | `{at, dur, speaker, text, kind: dialogue\|narration}`；**取代 `dialogue` + `narration`**；`narrator` 为保留 speaker | Slate `lines[]` + narrator 契约 |
| `beats[]` | object[] | — | `{char_id, at, duration, intent, posture, gaze, gesture, expression}`；机械校验落在镜内 | Slate 表演 beat |
| `keyframe` | object | — | `{path, sha256, source_hash}` — **采用的关键帧绑定**（替代隐式 `refs/` 目录约定） | Slate `keyframe_bindings` |
| `asset_revisions` | object | — | 生成时依赖的 `{asset_ref: revision}` 快照 | Slate `asset_revisions` |
| `status` | enum | — | `draft / ready / generating / rendered / qc_failed / stale` | Slate `acting_status` + our manifest 三态 |
| `qc` | object | — | 质检结果摘要 `{verdict, reasons[], at}` | 我们的 `qc.json` |
| `seed` | int | ✔ | 保留 | — |

### `units[]`（V 层，全新）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | **稳定 ID**（一旦生成不再随排序变化；Slate 用 `vu_` + 随机，显示序号与内部 ID 分离） |
| `label` | string | 显示名，如 `V01 · 三打白骨精` |
| `shot_ids` | string[] | **必须按原顺序完整覆盖**其成员，校验漏镜/重复/调序 |
| `scene_id` | string | 所有成员必须相同 |
| `duration` | object | `{planned_seconds, override_seconds}`；`effective = override ?? planned` |
| `shared_negative` | string[] | 整段共享负面（**注意：不能把各镜负面求并集**，镜特有阶段约束只作用于该时间段） |
| `beats[]` | object[] | `{shot_id, planned_seconds, text}` — 逐镜时间轴 |
| `prompt_video` | string | 组合提示词，**时间段 + 图片序号由编译器填入** |
| `reference_strategy` | enum | `ordered_keyframes` / `first_last_frame` / `none` |
| `continuity` | object | `{mode: none\|tail_context\|tail_first_frame, source: {shot_id, item_id, sha256}}` |
| `selected_clip` | string | 采用的产物 ID（**不存"最新版本"指针**） |
| `source_hash` | string | 内容指纹（不含绑定字段） |

### 建议同时新增的两个旁挂文件

- `state/asset_revisions.json` —— 资产 → 修订号（支持依赖图展开）。
- `state/board_rev.json` —— `{script_rev, board_rev, prompt_version, schema_version}`，用于 UI 陈旧标记。

## 7.4 落地顺序（按"省 GPU 时间"排序）

1. **P0（半天，零风险）**：加 `schema_version` / `prompt_version` / `script_rev`；`shots[].prompt` 拆 `prompt_image` + `prompt_video`（旧 `prompt` 保留为别名，读取时回落到 `prompt_image`）；`camera` 收成枚举 + `camera_detail` 承接原自由文本；加 `angle` / `lighting` / `negative`。**改完立刻省下「改视频描述→定妆照重生成」的浪费。**
2. **P1（1 天）**：`vm/state.py` 指纹按消费阶段拆开（`ref_image`/`clip`/`qc` 三套）；`sec` 拆 `sec/sec_override/sec_actual`，concat 用 `sec_actual`。
3. **P2（1–2 天）**：`lines[]` 取代 `dialogue`/`narration` + `narrator` 保留字；`chars` 改 ID 数组 + `costume` 改映射；`asset_revision` + 依赖图，manifest 记录快照。
4. **P3（2–3 天，收益最大）**：`units[]` 聚合层 + 编译器（确定性分组、时间段提示词、图片序号分配、硬校验），让 H3 一次请求出 2–3 个短镜。
5. **P4（可选）**：`beats[]` 表演层 + 机械校验；`tail_context` 尾帧视觉描述注入下一镜；storyboard-master 的五张决策表做成 `plan.py` 的确定性后处理。

## 7.5 三条要避开的坑（从这几份文档里学到的负面教训）

1. **契约不要分裂**（Slate 的代价）：Slate 现在有 3 套 shots 契约 + 2 个桥接器。我们只允许**一套 shots 契约 + 版本号**，白模/宫格这类新用途用**可选的旁挂数组**（`units[]`/`grids[]`），不要新建第二套 shot 结构。
2. **不要抄 acqm 的"参数存在但不用"**：`priority` 接收、校验、丢弃是最坏的失败模式（README 宣称有、用户以为有）。我们加字段时**要么有消费方，要么别加**，并用测试断言"字段被读"。
3. **不要让请求时长冒充实际时长**：H3 有 `frames%17` 步进，`concat` 必须用 ffprobe 回读值。Slate 反复强调这一点（设计文档 `:188, 197`），是最容易踩的工程坑。

---

## 附录 A：证据索引（关键文件路径）

**Slate**
- `/tmp/research/slate/workbench/tools/production_prompts.py`（三类提示词、source_hash、media_source_hash）
- `/tmp/research/slate/workbench/tools/creation_pipeline.py`（`cmd_storyboard` 675-824、契约化归一 714-768）
- `/tmp/research/slate/workbench/tools/production_studio.py`（S/V 数据、分组与校验）
- `/tmp/research/slate/workbench/tools/production_state.py`（资产修订号、依赖闭包）
- `/tmp/research/slate/workbench/tools/artifact_provenance.py`（按产物类型的指纹投影）
- `/tmp/research/slate/workbench/tools/reference_contract.py`（参考帧角色契约）
- `/tmp/research/slate/workbench/tools/storyboard_panels.py`（画格/宫格契约 + 唯一生图编译入口）
- `/tmp/research/slate/workbench/tools/prompt_modules.py`（storyboard_prompt 299-371、actor 376-418、词表 329-334）
- `/tmp/research/slate/workbench/tools/comfyui_h3.py` / `comfyui_client.py`（H3 图、帧步进、prompt_id 落盘、排队宽限）
- `/tmp/research/slate/workbench/tools/versions.py`（版本快照 20 份）
- `/tmp/research/slate/workbench/tools/project_layout.py`（惰性幂等迁移 + 字符串引用改写）
- `/tmp/research/slate/workbench/docs/2026-09-21-三层视频创作台设计.md`（S/V/E 权威设计）
- `/tmp/research/slate/previs_system/docs/SPEC-字段规范.md`（v1→v5.1 契约演进）
- `/tmp/research/slate/previs_system/docs/演员表演契约.md`（acting 契约）
- `/tmp/research/slate/.codex/skills/video-previs/references/storyboard_schema.md`（dialogue 契约字典）
- `/tmp/research/slate/workbench/web/src/views/StudioShotsView.vue`（分镜表格编辑）

**MCWW**（代码经 raw.githubusercontent 抓取，存于 `/tmp/research/mcww_src/`）
- `mcww/queueing.py`（队列 288-311 出队、327-401 暂停/中断/重启/取消、443-487 移动与清理、493-513 持久化）
- `mcww/processing.py`（状态机 31-43、批次 71-113、轮询 151-178、取消 181-190）
- `mcww/comfy/comfyAPI.py`（三级结果判定 60-96、interrupt/unQueue 161-181、/free 241-251、waitForComfy 254-274）
- `mcww/presets.py`（按 workflow 的 preset JSON）
- `mcww/utils.py:189-205`（restoreKey / storageKey = 源码哈希）
- `mcww/opts.py:23-26, 273, 291-295`（超时、maxQueueSize、优先级上限）
- `mcww_web/js/saveStates.js`、`project.js:88-140`、`queue.js`、`historyStates.js`

**acqm**
- `workflow_executor.py:240-253`（占位符铁证）、`models.py:37-56`、`database.py:35-46, 230-322`、`queue_manager_node.py:29, 173-181`、`execution_monitor.py:245-265`

**storyboard-master**
- `engine/storyboard/decision.py`（五表 + 仲裁 + 档位 + 图数）
- `engine/storyboard/grammar.py`（景别序列 / 机位语法 / 越轴 / 匹配原则）
- `engine/storyboard/quality.py`（12 项自检）、`frame.py`（动作顶点帧）
- `knowledge/分镜决策引擎.md`（决策表原文 + 内容类型判别铁律）
- `engine/examples/分镜输出示例_LLM版.json`（完整输出结构）

## 附录 B：本次调研的局限

1. **MCWW 未做完整 clone**（仓库 34.8MB 中 99% 是文档截图，网络被并发克隆占满），改为按 GitHub Tree API 精准抓取 **11 个关键源文件 + 4 个 JS**（全部列在附录 A）。未读文件：`mcww/ui/*.py` 的其余部分、`mcww/comfy/workflow.py` / `workflowConverting.py` / `nodeUtils.py`（节点标题解析与 workflow 注入）、`mcww_web/js/` 其余文件。因此 **「节点标题约定如何映射到 UI 元素」这条我没有深挖**，如需该能力请补读 `workflowConverting.py` 与 `docs/titles.md`。
2. **Slate 的 Vue 前端只读了 `StudioShotsView.vue` 与 `CreateView.vue` 的片段**，未逐行读 `CreateView`（三层视频三栏 UI 的实际实现）—— 设计文档 `:291` 明确说 P2/P3/P4 是**分阶段实施**，`CreateView.vue` 只有 167 行，说明**三层视频 UI 大概率尚未完整落地**（设计稿而非已上线功能，文档标题也自述「设计评估稿；本文件与原型不是已上线功能」）。**结论：S/V/E 可以借结构，但不要以为有现成实现可抄。**
3. **acqm 的 `workflow_executor` 是否在别处被替换**：我只在全仓 grep 了 ComfyUI 端点调用（无命中）并读了 executor 主体（占位符）。不排除 `run_integration_tests.py`/测试里有 mock 之外的路径，但生产路径就是占位符。
4. **storyboard-master 的「9.8/10 分」等评测数据是作者自评**，我没有复现，不作为质量证据引用。
5. 三个项目我**没有实际运行**，所有行为结论来自代码阅读；标 `推断` 的地方即为无法静态确认的部分。
