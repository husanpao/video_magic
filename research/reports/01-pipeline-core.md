# NiliX 流水线内核与状态管理 · 源码调研报告

> 调研对象：`/home/max/Projects/PythonProjects/video_magic/research/NiliX-main`（只读，未修改任何文件）
> 调研范围：`internal/manju` 流水线编排、条件缓存、产物时效、渲染检查点、断点续跑、并发、磁盘布局
> 代码规模：`internal/manju/manju_pipeline.go` 8893 行 / 418 KB，`manju.go` 3715 行，`manju_agent.go` 2644 行
> 所有结论均带 `文件:行号` 证据，未在代码中找到依据的推测都会显式标注。

---

## 0. 结论速览（先看这 12 条）

| # | 结论 | 证据 |
|---|---|---|
| 1 | 运行态 `stageTotal` 恒为 **8**，来源 `manjuStageOrder = {env, plan, assets, encode, render, qc, assemble, upscale}` | `internal/manju/manju.go:73`，`manju.go:2215 / 2256 / 2315` |
| 2 | 但**真正被执行的管线只有 6 阶段**：`plan → assets → encode → render → qc → assemble`，硬编码在 `manjuPipelineRun` | `manju_pipeline.go:2010` |
| 3 | `env` 不是管线阶段，是前端健康卡片独立调用的**只读自检**（POST `/api/manju/env`），且不在 `manjuPhases` 白名单里，`phase=env` 会被拒绝 | `manju.go:1793`、`manju.go:75-77`、`manju.js:3583` |
| 4 | `upscale` 也不是管线阶段，是**独立后台任务**（云端 2K，MiniMax `/v2/video_regeneration`），自己 `manjuFinish` | `manju_upscale.go:325-345`、`manju_upscale.go:280-283` |
| 5 | 条件缓存文件名 = `<项目>_v2_c<10位指纹>`，指纹 = md5(宽/高/帧数/角色/场景/参考图 mtime+size/FL2VA 尾帧/VAE/LoRA/步数/ref_image_size/最终化提示词) | `manju_pipeline.go:5676-5754` |
| 6 | **草稿/定稿缓存隔离靠指纹里的 `w=`/`h=` 维度**，草稿分辨率 = 定稿 × `draft_scale`（默认 0.5，对齐 32） | `manju_pipeline.go:5694`、`manju_pipeline.go:1896-1904` |
| 7 | 产物时效清单 `analysis/<EP>_manifest.json`，**实际只有 3 态**（missing/stale/current）；README 说的第 4 态 `unknown` 已在 2026-08-26 被**合并进 stale** | `manju_manifest.go:99-119`（代码只 return 3 个值） |
| 8 | 渲染检查点 `analysis/<EP>_render_ck.json` 在 **`comfy.Submit` 返回 prompt_id 后立即落盘**，草稿键加 `@d` 后缀 | `manju_pipeline.go:7426`、`manju_render_ck.go:19-23` |
| 9 | 崩溃收回查 **`GET /history/<prompt_id>`**：completed→直接复制产物；error→清 ck 重提；90 s 内无记录且 `GET /queue` 确认不在队列→判任务丢失重提 | `manju_render_ck.go:80-122`、`comfy/client.go:87-99` |
| 10 | **开机自动续跑的阶段白名单 = `{"all","encode","render","qc"}`**（README 的「白名单含 all」指的是这个）——`plan/assets` 不自动恢复 | `manju.go:2073-2077`（含注释「白名单必须含 all」） |
| 11 | GPU 串行化**没有 Go 侧全局锁**，靠 ComfyUI 自身单队列串行；Go 侧只做同条件编码去重（`encGate` singleflight） | `manju_pipeline.go:5765-5800`，`grep sync.Mutex` 无 GPU 锁 |
| 12 | 并发点共 5 处：逐镜提示词 4 路、下一镜预编码 1 路（渲染时重叠）、审片 `judge_concurrency`(默认 2，1-4)、ASR 与审片并行、IR 空镜扩写 2 路 | `manju_pipeline.go:3589`、`manju_pipeline.go:7271`、`manju_agent.go:1404`、`manju_agent.go:1483`、`manju_ir.go:28` |

---

## 1. 流水线阶段列表、顺序与"8 vs 6"的真相

### 1.1 后端权威定义

```go
// internal/manju/manju.go:72-77
// manjuStageOrder 管线阶段顺序(与前端 FLOW 一致,用于计算总体进度)
var manjuStageOrder = []string{"env", "plan", "assets", "encode", "render", "qc", "assemble", "upscale"}

var manjuPhases = map[string]bool{
    "all": true, "plan": true, "assets": true, "encode": true, "render": true, "qc": true, "assemble": true,
}
```

* 中文名映射：`manju.go:243-244` —— `env`＝环境自检、`plan`＝方案、`assets`＝资产、`encode`＝编码、`render`＝渲染、`qc`＝质检、`assemble`＝合成、`upscale`＝云端2K。
* **`stageTotal` 全部由 `len(manjuStageOrder)` 计算**，共 3 处：
  * 无状态空闲响应 `manjuDiskStatus`：`manju.go:2215`
  * 磁盘态响应：`manju.go:2256`
  * 内存实时响应：`manju.go:2315`
  * 所以前端拿到 `stageTotal=8` 是**常量 8**，与本次实际跑了几个阶段无关。

### 1.2 实际执行循环只有 6 个

```go
// internal/manju/manju_pipeline.go:2009-2024
func manjuPipelineRun(ctx *manjuCtx, phase string, lg *manjuLogger) int {
    stages := []string{"plan", "assets", "encode", "render", "qc", "assemble"}
    if phase != "all" {
        stages = []string{phase}
    }
    ...
    for _, st := range stages {
        ...
        manjuSetStage(st) // 实时阶段推进
        lg.logStage(st)   // 写 "━━━ 阶段 X · EP01 ━━━"
        switch st { case "plan": ... case "assemble": ... }
```

* 智能体模式（AI 一条龙）复用同一份顺序：`manju_agent.go:387` 同样是 `{"plan","assets","encode","render","qc","assemble"}`，只是 `plan` 后追加剧本师复核、`qc` 扩展为审片返工闭环（`manju_agent.go:381-386`）。

### 1.3 `env` 为什么在列表里却不是阶段

* `env` 由独立 HTTP 端点触发：`POST /api/manju/env` → `manjuEnv`（`manju.go:1793-1816`）→ `manjuEnvCheck`（`manju_pipeline.go:8083-8160`）。
* 前端只在**项目弹窗的健康卡片**里调它，与"一条龙/续跑"完全解耦：`web/kb/js/manju.js:3583 post("/api/manju/env", {config: this.project})`。
* `manjuPhases` 不含 `env` → `startManjuRun` 会返回 `未知阶段: env(可用 all/plan/assets/encode/render/qc/assemble)`（`manju.go:1881-1883`）。
* `manjuEnvCheck` **只读不写**，检查项（`manju_pipeline.go:8083-8160`）：
  config.json 存在、小说文件存在并统计章数/字数、ComfyUI `/system_stats` 在线与版本、H3 七个模型文件（`unet_ref2va`/`unet_fl2va`/`clip`/`vae_video`/`vae_audio`/`turbo_lora`/`turbo_lora_r2v`，支持 `diffusion_models|unet`、`text_encoders|clip`、`loras|pdd_acc` 备选目录）、SageAttention 节点双名探测（`PathchSageAttentionKJ`/`PatchSageAttentionKJ`）、Z-Image 三件套、ComfyUI venv python。结论以 `\n[exit 0|1]` 结尾。

### 1.4 `upscale` 为什么在列表里却不是阶段

* 入口：`POST /api/manju/upscale2k`（路由 `manju_upscale.go:447`），走 `manjuUpscaleRun`（`manju_upscale.go:325`）。
* 它自己接管全局状态：`manjuState.stage = "upscale"`（`manju_upscale.go:329`）→ `writeManjuDiskState(..., Stage:"upscale")`（`manju_upscale.go:339`）→ 结束时 `manjuFinish(rc)`（`manju_upscale.go:348`）。
* 串行逐镜循环：`manju_upscale.go:402-413`，每镜 submit→poll（最长 30 min）→下载到 `clips/<EP>/2k/NN.mp4`（`manju_upscale.go:285-320`）。
* **副作用（值得注意的坑）**：`upscale` 任务从不调用 `lg.logStage`（全仓 `logStage(` 调用点只有 `manju_pipeline.go:2023`、`manju_agent.go:404/2083/2088`）。而 `parseManjuProgress` 是从日志正则 `━━━ 阶段 (\w+)` 解析 `currentStage` 的（`manju.go:67`、`manju.go:2174-2188`）→ 云端 2K 运行期间 `currentStage=""`、`stageIdx=-1`、`progress=0`。

### 1.5 前端的阶段条

* `web/kb/js/manju.js:60-68` 的 `FLOW` 只有 **7 项**（`env, plan, assets, encode, render, qc, assemble`），**没有 upscale** → 阶段条永远不显示云端 2K。
* `web/kb/js/manju.js:4127` 的 `STAGE_ORDER` 同样是这 7 项（用于阶段按钮高亮）。
* **可观察后果**：普通模式进度分母固定为 8（`manju.go:2190`），而实际 currentStage 最大只会是 `assemble`（索引 6）→ `Progress=(6+0.5)/8*100 = 81.25%`，**进度条永远到不了 100%**。只有 `running=false` 后前端才按 `done && rc==0` 显示完成（`manju.js:4162` 附近）。

### 1.6 判定表

| 层 | 列表 | 数量 | 证据 |
|---|---|---|---|
| 后端进度常量 `manjuStageOrder` | env, plan, assets, encode, render, qc, assemble, upscale | 8 | `manju.go:73` |
| 后端执行循环 `manjuPipelineRun` | plan, assets, encode, render, qc, assemble | 6 | `manju_pipeline.go:2010` |
| 后端智能体循环 `manjuAgentPipelineRun` | plan, assets, encode, render, qc, assemble | 6 | `manju_agent.go:387` |
| 可寻址 phase 白名单 `manjuPhases` | all + 上述 6 | 7 | `manju.go:75-77` |
| 前端 FLOW / STAGE_ORDER | env, plan, assets, encode, render, qc, assemble | 7 | `manju.js:60-68`,`manju.js:4127` |
| README | 方案→资产→编码→渲染→质检→合成 | 6 | `README.md`「六阶段流水线」 |

> 结论：**README 的"六阶段"描述的是真实执行链，`stageTotal=8` 是含 `env`+`upscale` 两个旁路任务的展示口径。**

---

## 2. 每阶段的输入 / 输出 / 落盘位置

公共上下文 `manjuCtx` 定义在 `manju_pipeline.go:46-88`，三个目录在 `newManjuCtx` 里固定：

```go
// manju_pipeline.go:337-339
ctx.assetsDir = filepath.Join(ctx.workdir, "assets")
ctx.analysisDir = filepath.Join(ctx.workdir, "analysis")
ctx.clipsDir = filepath.Join(ctx.workdir, "clips")
```

`ctx.workdir` 取自 `config.paths.workdir`（`manju_pipeline.go:302`），建项目时写死为 `<manju_root>/<项目名>`（`manju_pipeline.go:8467`）。

### 2.1 env（环境自检，旁路）

| 项 | 内容 | 证据 |
|---|---|---|
| 输入 | `config.json` → `newManjuCtx` | `manju_pipeline.go:8084` |
| 处理 | 只读探测（文件/模型/节点/在线状态） | `manju_pipeline.go:8087-8157` |
| 输出 | 返回给前端的纯文本报告（`[exit 0/1]`） | `manju_pipeline.go:8152-8159` |
| 落盘 | **无** | — |

### 2.2 plan（方案）

| 项 | 内容 | 证据 |
|---|---|---|
| 入口 | `stagePlan` → `ensurePlanAndPrompts` → `ensurePlan` | `manju_pipeline.go:6166-6169`、`manju_pipeline.go:5929`、`manju_pipeline.go:2927` |
| 输入 | ① 小说正文：`chapterText()` 按 `chapters` 范围抽取（`manju_pipeline.go:2197`）；② 或脚本直出文件 `paths.script`/`script/<EP>.md`（`manju_pipeline.go:313-322`）；③ 知识库素材（`novelRootDir()` 下的 人物/角色/场景/封面/总集/设定集 `.md`+`.json`，进 `novelFingerprint`，`manju_pipeline.go:3333-3355`）；④ LLM（DeepSeek，`ctx.llm.chatJSON`，温度 0.4） | 同左 |
| 输出（落盘） | `analysis/<EP>_direct_plan.json`（`writePlan`，`manju_pipeline.go:3482/3500`）<br>`analysis/<EP>_characters.json`（`writeCharactersJSON`，`manju_pipeline.go:3504/3510`）<br>`analysis/<EP>_shots_prompts.json`（`genShotPrompts`，`manju_pipeline.go:3514/3668`）<br>`analysis/fingerprints.jsonl`（反同质化指纹，`manju_fingerprint.go:43-47`） | 同左 |
| plan 内嵌元数据 | `chapters`（复用校验）、`episode`、`novel_fp`（正文/素材指纹）、`script_parse_ver`（脚本解析器代数） | `manju_pipeline.go:3487-3499` |
| 复用条件 | `plan.chapters == ctx.chapters`（或全本请求）**且** `novel_fp` 一致 **且** 脚本模式 `script_parse_ver >= manjuScriptParseVer(=25)` | `manju_pipeline.go:2943-2996`、`manju_script_parse.go:338` |
| 复用失败动作 | `clearEpisodeArtifacts`（清该集全部 clip + 该项目全部条件缓存 + 该集 latent 命名空间） | `manju_pipeline.go:2959`、`manju_pipeline.go:3438-3480` |
| 校验 | `validatePlan`（角色卡完整性/时长-台词量/说话人纪律）不达标带意见重试一次；`validateShotPrompt` 逐镜六段式结构校验 | `manju_pipeline.go:3198/3160`、`manju_pipeline.go:3674` |

### 2.3 assets（资产）

| 项 | 内容 | 证据 |
|---|---|---|
| 入口 | `stageAssets` | `manju_pipeline.go:5348` |
| 输入 | `plan.characters[].image_prompt/second_form/views`、`plan.scenes[].image_prompt`、`assets/asset_map.json`（增量）、角色资产库 `asset_lib/characters/`（跨项目复用） | `manju_pipeline.go:5350-5360`、`manju_pipeline.go:5440` |
| 引擎 | 定妆/视图 = **Krea-2 img2img**（`krea2_unet/clip/vae`，`wfKrea2`）；Q 版 = 主图 img2img denoise 0.93（`manju_pipeline.go:5574`）；场景 = **Z-Image 1024×1024**（`wfZImage`，`manju_pipeline.go:5626`） | `manju_pipeline.go:5548-5640` |
| 输出（落盘） | `assets/characters/<cid>.png` 主定妆<br>`assets/characters/<cid>_face.png` 正脸（`ensureFaceCrop` + YuNet，兽类自动跳过）<br>`assets/characters/<cid>_form2.png` 真身（有 `second_form` 时）<br>`assets/characters/<cid>_full.png` / `_side.png` / `_detail.png` / `_q.png` 视图<br>`assets/scenes/<sid>.png` 场景图<br>`assets/scenes/<sid>_end.png` FL2VA 尾帧（`fl2va_end_frame=true` 时）<br>`assets/asset_map.json`（`characters`/`scenes` 路由表 + `views_gen`/`q_gen`/`portrait_gen`）<br>`assets/<封面>`（建项目时 `copyProjectCover`，`manju_pipeline.go:8296`） | `manju_pipeline.go:5448-5643` |
| 生成代数键 | `manjuViewGen=10`（`manju_pipeline.go:3928`）、`manjuQGen=23`（`manju_pipeline.go:3969`）、`manjuPortraitGen=6`（`manju_pipeline.go:3982`）；版本不匹配→删旧重出（`manju_pipeline.go:5365-5425`） | 同左 |
| 视图时效 | 视图 mtime 早于主图 → 重生成（`manju_pipeline.go:5560-5572`） | 同左 |
| 跨项目入库 | `manjuCharLibStore` 写 `asset_lib/characters/<角色名>/{card.json,*.png}` + `index.json` | `manju_char_lib.go:216`、`manju_asset_index.go:85` |
| 尺寸常量 | 定妆 `manjuPortraitW/H = 1024,1024`；全身/侧面 `manjuViewW/H = 832,1248` | `manju_pipeline.go:3897/3902` |

### 2.4 encode（条件预编码）

| 项 | 内容 | 证据 |
|---|---|---|
| 入口 | `stageEncode` | `manju_pipeline.go:5870` |
| 前置 | `ensureComfyReady` + `freeComfyModels`（先释放 assets 阶段常驻的 ZImage，防 Qwen3-VL 32B 爆显存挂起） | `manju_pipeline.go:5872-5878`、`manju_pipeline.go:193` |
| 输入 | 选定镜头（`selectedShots` 按 `only` 过滤）+ **最终化提示词** + 参考图文件名 + 音色参考 | `manju_pipeline.go:5880-5892` |
| 参考图复制 | 角色 → `<comfy_input>/dir_char_<shotID>_<i>_<j>.png`（`charRefNames`，`manju_pipeline.go:6222-6237`）；场景 → `dir_scene_<shotID>.png`（`sceneRefName`，`manju_pipeline.go:7161-7174`）；尾帧 → `dir_scene_<shotID>_end.png`（`manju_pipeline.go:5841`） | 同左 |
| 输出（落盘） | `<comfy_shared>/models/conditioning/<项目>_v2_c<指纹>.pt` | `manju_comfy.go:545-547`、`manju_pipeline.go:5683-5686` |
| 幂等 | `.pt` 已存在直接跳过 `跳过（缓存已存在）` | `manju_pipeline.go:5909-5913`、`manju_pipeline.go:5781` |
| 落盘校验 | wait 成功后仍须文件存在，否则报「疑似 ComfyUI 节点缓存短路」 | `manju_pipeline.go:5857-5862` |
| 耗时提示 | `manjuEncEstMin` 恒返回 4 分钟/镜（仅提示） | `manju_pipeline.go:5924-5926` |

### 2.5 render（逐镜渲染）

| 项 | 内容 | 证据 |
|---|---|---|
| 入口 | `stageRender` → `renderShotTo` | `manju_pipeline.go:7176`、`manju_pipeline.go:7319` |
| 输入 | plan+最终化提示词、条件缓存 `.pt`、`h3_context` 上一镜 latent、按镜覆盖参数（`shotOverrideFor`） | `manju_pipeline.go:7388-7403` |
| 输出（落盘） | 定稿 `clips/<EP>/NN.mp4`（`fmt.Sprintf("%02d.mp4", s.ID)`）<br>草稿（智能体草稿预审）`clips/<EP>/_draft/NN.mp4`（`draftDir`，`manju_pipeline.go:1906-1908`）<br>接缝 latent `<comfy_output>/h3_context/<项目>_<EP>/clip_%05d.safetensors`（`manju_comfy.go:541-543`） | `manju_pipeline.go:7322`、`manju_pipeline.go:7447` |
| 时效清单 | 定稿成功后 `manifestMark` → `analysis/<EP>_manifest.json`（**草稿不入清单**） | `manju_pipeline.go:7446-7448`、`manju_manifest.go:72-86` |
| 跳过规则 | 0 字节残留→删重渲；`stale`→`clearShotArtifacts` 删重渲；`forceRR`(only 非空)→覆盖重渲；QC 失败→换 seed 重渲（上限 2 次）；否则跳过 | `manju_pipeline.go:7233-7270` |
| 接缝链 | `chained = idx>1 && 上一镜 latent 存在`；**非固定运镜镜强制断链**（`manjuCameraIsStatic`，`manju_pipeline.go:7311-7317`） | `manju_pipeline.go:7359-7366` |
| 任务失败 | wait 出错即返回，**不自动重试**（升级后"异常即停"，`manju_pipeline.go:7428-7436`） | 同左 |

### 2.6 qc（机械质检 + 视觉/幽灵人声加检）

| 项 | 内容 | 证据 |
|---|---|---|
| 入口 | `stageQC` | `manju_pipeline.go:7559` |
| 输入 | `clips/<EP>/*.mp4`、`analysis/<EP>_direct_plan.json`、`defreeze` 开关、`only`（定点） | `manju_pipeline.go:7571-7589` |
| 调用 | `<comfy venv python> <manju/logs/media/manju_media.py> qc --dir ... --plan ... [--no-defreeze] [--shots ...] --json <report>` | `manju_pipeline.go:7592-7598`、`manju_pipeline.go:7998-8008` |
| 输出（落盘） | `qc/<EP>_qc.json` 质检报告（`qcReportPath`，`manju_pipeline.go:7750-7752`）<br>`qc/<EP>_rerun.json` 质检自愈重试轮数侧车（`qcRerunPath`，`manju_pipeline.go:7758-7760`）<br>`analysis/_frames/<EP>/<NN>/` 审片抽帧（智能体模式，`manju_agent.go:953`） | 同左 |
| 追加失败源 | ① `qcVisualCheck` 视觉抽检（本地 Qwen3-VL，`manju_qcvision.go`）② `qcGhostVoiceCheck` 幽灵人声（`manju_qcghost.go`） | `manju_pipeline.go:7634-7638` |
| 失败语义 | 失败集非空 → 返回 error，管线终止；`qc_skip_shots` 跳过镜从失败集剔除；`qc_accept` 逃生门放行 | `manju_pipeline.go:7638-7654` |
| 报告消费 | `stageRender` 结束后 `qcReportClear`（删报告），使失败镜只重渲一轮 | `manju_pipeline.go:7294`、`manju_pipeline.go:7814-7816` |
| 执行失败防线 | `err!=nil && len(failed)==0` → 显式报「质检执行失败」，绝不静默通过 | `manju_pipeline.go:7620-7623` |

### 2.7 assemble（FFmpeg 合成）

| 项 | 内容 | 证据 |
|---|---|---|
| 入口 | `stageAssemble` | `manju_pipeline.go:7820` |
| 前置 | 若 `render.voiceover=true` → 先 `runVoiceover`（edge-tts 补旁白/画外音） | `manju_pipeline.go:2039-2044`、`manju_pipeline.go:7531` |
| 输入 | `clips/<EP>/*.mp4`、plan、`transition`（默认 `dissolve`，接缝镜硬切）、`bgm`+`bgm_gain`+`bgm_duck`、`skip-shots` | `manju_pipeline.go:7874-7908` |
| 缺镜检查 | 方案镜数 vs 产物数比对，缺镜仅告警不阻断 | `manju_pipeline.go:7828-7852` |
| 输出（落盘） | `<workdir>/<EP>_成片.mp4`（`manju_pipeline.go:7855`）<br>`<workdir>/<EP>_成片.srt`（`srt_out` 默认导出，`manju_pipeline.go:7884-7887`）<br>`<workdir>/<EP>_成片_微创版.mp4`（`micro_voiceover` 默认开，`manju_pipeline.go:7912-7916`）<br>`<workdir>/<EP>_预告片.mp4`（`manju_agent.go:2455/2540`） | 同左 |
| 成片终检 | `agentAssembleCheck`（时长/黑屏/静音/音轨，报告性质不阻断） | `manju_pipeline.go:2043-2046`、`manju_agent.go:1666` |
| 字幕开关 | `subtitle` 默认 false（H3 原生对白轨道，烧字幕会触发终检 OCR 误报） | `manju_pipeline.go:7877-7881` |

### 2.8 upscale（云端 2K，独立任务）

| 项 | 内容 | 证据 |
|---|---|---|
| 入口 | `POST /api/manju/upscale2k` → `manjuUpscaleRun` | `manju_upscale.go:447/325` |
| 输入 | 本集 `clips/<EP>/NN.mp4`（本地 768×1344/24fps），MiniMax key（`config.render.minimax_api_key` 或环境变量） | `manju_upscale.go:285-300`、`manju_upscale.go:353-360` |
| 提交 | `manjuUpscaleSubmit(client, base, key, prompt, src, "2K")` → `/v2/video_regeneration` | `manju_upscale.go:305`、`manju_upscale.go:76` |
| 轮询 | 每 10 s 查一次，上限 30 min | `manju_upscale.go:310-312` |
| 输出（落盘） | `clips/<EP>/2k/NN.mp4` | `manju_upscale.go:281-288` |
| 并发 | 串行逐镜，失败累计但不中断（已成功保留） | `manju_upscale.go:402-413` |

---

## 3. 幂等与条件缓存

### 3.1 条件缓存名（两阶段共享编码的核心）

```go
// manju_pipeline.go:5676-5686
const manjuCacheVer = "v2"
func (ctx *manjuCtx) shotCacheNameAt(s manjuShot, w, h int) string {
    proj := reNonWord.ReplaceAllString(ctx.project, "_")
    return fmt.Sprintf("%s_%s_c%s", proj, manjuCacheVer, ctx.shotCondFingerprintAt(s, w, h))
}
```

* 物理路径：`<comfy_shared>/models/conditioning/<cacheName>.pt`（`manju_comfy.go:545-547`）。
* 缓存名**不含集号**，只含项目名 + 代数 `v2` + 指纹 → 同项目同条件跨集共享（`manju_pipeline.go:3452-3456` 的注释明确说明旧版含集号的前缀"永不匹配、旧 .pt 只增不减"，已改为按项目前缀清理）。

### 3.2 条件指纹的准确字段清单

`shotCondFingerprintAt(s, w, h)`（`manju_pipeline.go:5694-5754`）构造 md5，截取 **前 10 位十六进制**：

```
w=%d | h=%d | len=%d | chars=%s | scene=%s | refs=%s | fl2va_end=%t
| samp=<vae_video>|<turbo_lora>|<turbo_lora_r2v> | %d=<steps> | %s=<h3RefImageSize>
| prompt=<finalizeAlignedPrompt(...)>
```

逐项来源：

| 维度 | 取值 | 证据行 |
|---|---|---|
| `w`,`h` | 本次渲染宽高（草稿传半分辨率） | `manju_pipeline.go:5745` |
| `len` | `h3Length(s.Duration, ctx.fps)` 帧数 | `manju_pipeline.go:5745` |
| `chars` | `strings.Join(s.Characters, ",")`（*未截断*，前 3 个仅用于收集参考图） | `manju_pipeline.go:5746` |
| `scene` | `s.Scene` | `manju_pipeline.go:5746` |
| `refs` | 每个登场角色（≤3）的 `charViewRels` 逐条 `refStamp`；场景图 `scenes/<sid>.png` 的 `refStamp`；`fl2va_end_frame` 时追加 `end:` 前缀的场景尾帧 `refStamp`；**参考音频**：`charVoiceNames(s)` 对应文件 `<mtime>:<size>` 记 `voice:` 前缀 | `manju_pipeline.go:5698-5715`（角色+场景）、`manju_pipeline.go:5716-5727`（尾帧）、`manju_pipeline.go:5728-5735`（音色）、`manju_pipeline.go:7153-7159`（refStamp 定义） |
| `refStamp` 格式 | `rel@<mtime纳秒>:<size>`（采纳新定妆照/重生成视图后自动失效） | `manju_pipeline.go:7152-7159` |
| `fl2va_end` | `fl2va_end_frame==true && len(Characters)==0 && 尾帧文件存在` | `manju_pipeline.go:5719-5727` |
| `samp` | `vae_video`、`turbo_lora`、`turbo_lora_r2v` 三个模型名（2026-09-04 画质升级新增维度） | `manju_pipeline.go:5747` |
| 步数 | `ctx.steps`（turbo LoRA 存在时取 `turbo_steps`） | `manju_pipeline.go:5747` |
| `ref_image_size` | `h3RefImageSize(R)`，合法 `match|max`，默认 `max` | `manju_pipeline.go:5747`、`manju_comfy.go:278-283` |
| `prompt` | `ctx.finalizeAlignedPrompt(s.H3Prompt, s, ctx.shotPicSlots(s))` —— **必须与渲染时同一份最终化文本**（迁移注释见 `manju_pipeline.go:5741-5743`） | `manju_pipeline.go:5740/5744-5748` |

> 注意：`refs` 里存的是**文件 mtime+size**，不是内容哈希；`refStamp` 的注释明确写"路径@mtime纳秒:大小"（`manju_pipeline.go:7152`）。跨机拷贝文件可能因 mtime 变化导致全量重编码（这是有意的保守策略）。

### 3.3 草稿 / 定稿分辨率缓存的隔离

* 隔离手段**只有指纹里的 `w=|h=` 两项**，没有单独的目录或命名空间区分。
* 草稿分辨率：`draftDims()` 按 `draft_scale` 缩放并 32 对齐（`manju_pipeline.go:1896-1904`）：

```go
// manju_pipeline.go:1896-1904
func (ctx *manjuCtx) draftDims() (int, int) {
    sc := ctx.draftScale                      // 0.2–0.95，默认 0.5
    if sc <= 0 || sc >= 1 { sc = 0.5 }
    return manjuAlign32(int(float64(ctx.w)*sc + 0.5)), manjuAlign32(int(float64(ctx.h)*sc + 0.5))
}
```

* 智能体草稿预审：`draftMode := acfg.VisionReady() && ctx.draftJudge`（`manju_agent.go:1363`），草稿渲到 `clips/<EP>/_draft/`，审片通过后按全集顺序用审定提示词全分辨率重渲（`manju_agent.go:1593-1668`）；定稿完成后**逐镜删除草稿 .pt** 并 `RemoveAll(_draft)`（`manju_agent.go:1662-1667` 区域）。
* `res_tier` 是另一个维度：`manjuResTiers = {"draft":416, "standard":768, "fhd":1088}`（`manju_comfy.go:337`），按短边缩放（`manju_comfy.go:349-358`），与"草稿预审"的 `draft_scale` **不是同一个东西**，容易混淆。
* 默认 `res_tier=standard`、`draft_judge=true`、`draft_scale=0.5`（`manju_pipeline.go:8411-8413`）。

### 3.4 同条件并发去重（singleflight / encGate）

```go
// manju_pipeline.go:5765-5785
type encGate struct { mu sync.Mutex; gate chan struct{}; refs int }
var manjuEncSingleflight sync.Map // cacheName → *encGate
```

* 语义：同 `cacheName` 并发提交只跑一次；等待者轮询文件出现，执行者失败释放执行权后接力（`manju_pipeline.go:5799-5815`）。
* `refs` 引用计数保证"最后一个退出者才 Delete 表项"，消除旧实现"释放后删除前"窗口的双任务烧编码（注释 `manju_pipeline.go:5766-5771`）。
* 等待超时 **1900 s**（`manju_pipeline.go:5804`）。
* 必须用缓冲 1 的 chan，否则 select 恒走 default 全员死等（注释 `manju_pipeline.go:5782-5784`）。
* 专项测试：`internal/manju/manju_singleflight_test.go`。

### 3.5 其他幂等锚点

| 幂等点 | 机制 | 证据 |
|---|---|---|
| 方案复用 | `chapters` + `novel_fp` + `script_parse_ver` | `manju_pipeline.go:2943-2996` |
| 资产复用 | `fileExists(dst)` 即跳过；代数键 `views_gen/q_gen/portrait_gen` 强制换代 | `manju_pipeline.go:5448/5365-5425` |
| 角色跨项目复用 | 形象指纹（image_prompt + q_form + second_form 归一化 sha256）比对 `asset_lib/characters/<名>/card.json` | `manju_char_lib.go:24-40`、`manju_char_lib.go:216` |
| 编码复用 | `.pt` 存在即跳过 | `manju_pipeline.go:5781` |
| 渲染复用 | `clips/<EP>/NN.mp4` 存在且 manifest=current 即跳过 | `manju_pipeline.go:7268-7270` |
| 音色库复用 | `voice_lib/audio/lib_<key>.mp3` + 旁挂 `.txt` 念白 meta（文本升级自动重生成），`.src` 标记外部音源则永不覆盖 | `manju_pipeline.go:6595-6614` |
| 提示词复用 | `analysis/<EP>_shots_prompts.json` 里已有即不重生成 | `manju_pipeline.go:3517-3532` |

---

## 4. 产物时效清单 `analysis/<EP>_manifest.json`

### 4.1 文件结构与记录内容

```go
// manju_manifest.go:20-30
type manjuShotManifest struct {
    Fingerprint string `json:"fp"`             // 渲染时全部输入指纹
    OutSize     int64  `json:"size,omitempty"` // 产物大小(展示用)
    Seam        bool   `json:"seam,omitempty"` // 渲染时是否 MotionContext 接缝(合成转场硬切依据)
    RenderedAt  int64  `json:"at"`
}
type manjuManifest struct {
    Shots map[string]*manjuShotManifest `json:"shots"` // key = 镜头号字符串 "03"
}
```

路径：`analysis/<EP>_manifest.json`（`manju_manifest.go:34-36`），原子写（`manju_manifest.go:50-52`）。

### 4.2 记录的输入指纹：`shotRenderFingerprint`

```go
// manju_manifest.go:57-70
func (ctx *manjuCtx) shotRenderFingerprint(s manjuShot) string {
    lora := str(R["turbo_lora"]); if r2v := str(R["turbo_lora_r2v"]); r2v != "" { lora += "|r2v=" + r2v }
    spec := turboLoRASpecOf(str(R["turbo_lora"]))
    ovFp := shotOverrideFingerprint(ctx.shotOverrideFor(s.ID))
    q := fmt.Sprintf("|steps=%d|sampler=%s|sched=%s|lora=%s|sage=%v|policy=%s|ov=%s",
        ctx.steps, spec.Sampler, spec.Scheduler, lora, R["sage_attention"], ctx.seedPolicy, ovFp)
    return md5Hex(ctx.shotCondFingerprint(s) + "|a=" + ctx.assetsFingerprint() + q)
}
```

即：**条件指纹（第 3.2 节全部维度）+ 资产指纹 + 采样质量参数**。

* `assetsFingerprint()`（`manju_pipeline.go:5646-5670`）：对 `assets/characters` + `assets/scenes` 下所有 `.png` 的 `文件名@mtime纳秒|size` 排序拼接后 md5，取 **8 位**。这是"换定妆照→全项目产物 stale"的兜底防线（条件指纹只看被该镜引用的图，资产指纹看全部图）。
* 质量参数块 `q` 字段：`steps`、`sampler`、`sched`、`lora`(含 r2v)、`sage`、`policy`(seed_policy)、`ov`（按镜覆盖指纹）。
* **seed 有意不入指纹**（注释 `manju_manifest.go:54-56`：避免改 seed 触发全量重渲，定点重渲/fresh 覆盖）。
* `shotOverrideFingerprint`：镜头调试面板的按镜覆盖（steps/sampler/lora/pdd/sage/neg_prompt/seed）变化 → 该镜 stale，其它镜不受影响（`manju_manifest.go:64-66`）。

### 4.3 时效判定：**实际只有 3 态**

```go
// manju_manifest.go:103-119
func (ctx *manjuCtx) shotManifestStatus(s manjuShot) string {
    p := filepath.Join(ctx.clipsDir, ctx.episode, fmt.Sprintf("%02d.mp4", s.ID))
    if !fileExists(p) { return "missing" }
    m := ctx.manifestLoad()
    e := m.Shots[strconv.Itoa(s.ID)]
    if e == nil { return "stale" }                          // 无记录=不可信,重渲
    if e.Fingerprint != ctx.shotRenderFingerprint(s) { return "stale" }
    return "current"
}
```

| 态 | 触发条件 | 处置 |
|---|---|---|
| `missing` | `clips/<EP>/NN.mp4` 不存在 | 直接渲染 |
| `stale` | 产物存在但 ① **清单无该镜记录**，或 ② `fp` 与当前重算值不等 | `clearShotArtifacts` 删 mp4 + 删清单记录 + 删渲染检查点，再渲 |
| `current` | 产物存在且 `fp` 完全相等 | 跳过 |

> ⚠️ **README 与代码注释的第 4 态 `unknown` 已不存在**：
> * 文档注释仍写「current / stale / missing / unknown（产物在但清单无记录）」（`manju_manifest.go:99`）和文件头「unknown(无记录,旧项目兼容视为 current)」（`manju_manifest.go:6`）。
> * 但 2026-08-26 的修改把 `unknown` 语义**并入 `stale`**：`manju_manifest.go:100-102` 注释「旧版 unknown 视为 current 直接跳过……漫剧项目库已清空重建档,无兼容包袱:unknown 一律按 stale 删旧重渲」。
> * 测试固化了新语义：`manju_render_upgrade_test.go:302-304`「无记录应 stale（不可信重渲,2026-08-26 语义）」，但测试函数名/注释仍写「unknown 兼容」（`manju_render_upgrade_test.go:281-282`）。
> **准确表述：`missing / stale / current` 三态，`unknown` 已折叠进 `stale`。**

### 4.4 触发重渲的完整路径

| 触发点 | 代码 | 行为 |
|---|---|---|
| 普通渲染循环 | `manju_pipeline.go:7237-7240` | `stale` → 打日志「产物已过期(输入已变),删旧重渲(含条件缓存)」→ `clearShotArtifacts` |
| 智能体渲染 | `manju_agent.go:1434-1440` | 同上（草稿模式检查定稿产物） |
| 智能体返工轮 | `manju_agent.go:1632-1635` | 定稿轮 stale 删重渲 |
| 智能体审片前 | `manju_agent.go:1715` | stale 检查 |
| 产物面板角标 | `manju.go:2568-2572`（`manjuOutputs`） | 给前端 clip 打 `stale:true` → UI 显示「⚠️ 已过期」（`manju.js:4811`） |
| 镜头列表 | `manju.go:2734-2738` | 同上，统计 `stale` 计数 |
| 删除动作 | `manju_agent.go:1268`（`clearShotArtifacts`） | `manifestRemove` 同步清记录（`manju_manifest.go:88-97`） |

`clearShotArtifacts` 的完整动作（`manju_agent.go:1257-1272`）：
1. `os.Remove(clips/<EP>/NN.mp4)`
2. `manifestRemove(s.ID)`
3. `renderCKClear("<ID>")` + `renderCKClear("<ID>@d")`
4. **不删条件缓存 .pt**（注释说明：cacheName 已含指纹，显式删除会造成"新编的 .pt 被删 → 撞 ComfyUI 节点缓存短路 → CondLoad 失败"的事故，`manju_agent.go:1259-1265`）。

---## 5. 渲染检查点与崩溃恢复

### 5.1 文件与数据结构

```go
// manju_render_ck.go:19-29
type manjuRenderCK struct { Shots map[string]string `json:"shots"` } // 镜头key → prompt_id
func (ctx *manjuCtx) renderCKPath() string {
    return filepath.Join(ctx.analysisDir, ctx.episode+"_render_ck.json")
}
```

* 镜头 key：定稿 = `"03"`；**草稿 = `"03@d"`**（`manju_pipeline.go:7337-7339`：`if dstDir == ctx.draftDir() { ckKey += "@d" }`）。注释在 `manju_render_ck.go:20`。
* 读写全部走 `manjuRenderCKMu` 互斥 + 原子写（`manju_render_ck.go:25/31-69`）。

### 5.2 何时落盘、何时清除

| 时机 | 动作 | 证据 |
|---|---|---|
| `comfy.Submit` 返回 prompt_id 后**立即** | `ctx.renderCKSet(ckKey, pid)` | `manju_pipeline.go:7425-7426`（注释「提交即落盘:崩溃后可按 prompt_id 收回,绝不重复烧 GPU」） |
| `comfy.Wait` 返回错误 | `ctx.renderCKClear(ckKey)` 后 return error | `manju_pipeline.go:7428-7435` |
| 产物复制成功 | `ctx.renderCKClear(ckKey)` | `manju_pipeline.go:7445` |
| 决定重渲（attempt>0 或 fresh） | 先 `renderCKClear` 再重提，**跳过 tryReclaim** | `manju_pipeline.go:7354-7356` |
| `clearShotArtifacts` | 清 `"NN"` 与 `"NN@d"` | `manju_agent.go:1270-1271` |

`renderCKSet` 是 4 处 `renderCK*` 调用里唯一写盘入口；`.pt` 编码阶段**没有**对应检查点（编码任务丢失只能靠条件缓存文件是否落盘判断，`manju_pipeline.go:5857-5862`）。

### 5.3 崩溃后收回：`tryReclaim`

```go
// manju_render_ck.go:80-122（精要）
func (ctx *manjuCtx) tryReclaim(pid, dst string, lg *manjuLogger) (bool, error) {
    deadline := time.Now().Add(manjuReclaimLostWait)  // 默认 90s，变量可被测试缩短
    for {
        entry := ctx.comfy.History(pid)
        if entry != nil {
            st := entry["status"]            // status_str == "error" → 返回错误(调用方清 ck 重提)
            if done := st["completed"] == true {
                rel := comfyOutputVideo(entry)
                copyFile(filepath.Join(ctx.comfyOutput, rel), dst)
                lg.logf("  🔧 崩溃恢复:收回上次已完成的渲染产物 -> " + dst)
                return true, nil
            }
            ctx.comfy.Wait(pid, 3600*time.Second, 10*time.Second, lg.stopped) // 在跑 → 等完再回到上面收
            continue
        }
        if time.Now().After(deadline) {
            inQ, qerr := ctx.comfy.InQueue(pid)
            if qerr != nil { deadline = time.Now().Add(30*time.Second) }   // 查询失败=不确定,延长
            else if !inQ { return false, nil }                             // 明确丢失 → 调用方重提
            else { deadline = time.Now().Add(30*time.Second) }             // 仍在排队 → 延长
        }
        time.Sleep(3 * time.Second)
    }
}
```

* 查询端点：`GET /history/<prompt_id>`（`comfy/client.go:87-99`）与 `GET /queue`（`comfy/client.go:123-148`）。
* 调用点：`renderShotTo` 开头，且**只在 `attempt==0 && !fresh` 时**执行（`manju_pipeline.go:7340-7356`）。原因注释：定点重渲/质检自愈/agent 返工若收回旧产物，会把"已决定重渲"的镜头静默替换成旧产物。
* 丢失判定要求**三条件同时满足**：history 无记录 + 超窗（≥90 s）+ `/queue` 明确查不到（`manju_render_ck.go:107-118`）。查询失败视为"ComfyUI 忙"，不算丢失（防长队列误判导致同一镜头双任务烧两遍 GPU，注释 `manju_render_ck.go:108-110`）。
* `comfy.Wait` 里还有一条独立的丢失检测：连续 30 次（~5 min，轮询 10 s）"可达且不在队列"才判 `ComfyUI 任务丢失`（`comfy/client.go:182-231`）。

### 5.4 "白名单"的准确含义

README 写「渲染检查点……白名单含 `all`，断电重启自动续跑」。代码里有两处"白名单"候选，**真正对应的是开机自动恢复的阶段白名单**：

```go
// manju.go:2069-2077
stage := ds.Stage
if stage == "" { stage = ds.CurrentStage }
// 渲染链路阶段才自动恢复。注意:前端一条龙/续跑都发 phase="all",磁盘 Stage 落盘恒为 "all",
// 运行期不更新磁盘阶段——白名单必须含 "all",否则一条龙崩溃后永不自动恢复(实测发现的坑)。
if stage != "all" && stage != "encode" && stage != "render" && stage != "qc" {
    continue
}
```

* **白名单 = `{"all", "encode", "render", "qc"}`**（`manju.go:2073-2077`）。
* `plan`/`assets` **不在白名单**：注释明确「plan/assets 无 GPU 消耗,用户按需重跑」（`manju.go:2068`）。
* 为什么必须有 `all`：磁盘 `run_state.json` 的 `Stage` 在运行期**恒为 `"all"`**（`startManjuRun` 写 `Stage: orDefault(phase,"all")`，`manju.go:1914`），只在 `manjuFinish` 才写 `CurrentStage`（`manju.go:2117`）。所以崩溃残留读到的 `Stage` 永远是 `"all"`。
* 其他"白名单"与此无关，勿混淆：
  * `manjuPhases`（`manju.go:75-77`）＝ 可寻址 phase 白名单（含 `all`）。
  * `fs.go:445` `/api/fs/*` 根目录安全白名单；`manju_pipeline.go:340-355` 由项目 config 动态扩白名单（小说路径越界不注册，防任意文件读）。
  * `manju_shot_edit.go:18` 可编辑字段白名单；`manju_agent_health.go:741` LLM 可触发动作白名单；`manju_llm.go:503` 中文美术风格词白名单。

### 5.5 断电/服务重启后的自动续跑链路

`AutoRecoverRendering()`（`manju.go:2046-2103`），启动时被调用：

1. 遍历 `<manju_root>` 下每个子目录，读 `run_state.json`（`manju.go:2057-2060`）。
2. 跳过条件：`ds == nil || !ds.Running || isPidAlive(ds.PID)`（进程仍存活则不接管，`manju.go:2061-2063`）。
3. 阶段白名单过滤（上述 4 个值），否则 continue（`manju.go:2073-2077`）。
4. `config.json` 存在性检查（`manju.go:2078-2081`）。
5. ComfyUI 不在线则尝试拉起，拉不起就跳过（避免空转，`manju.go:2079-2085`）。
6. **定点参数回读**：从 `config.render.shots` 取回 `only`，防「定点重渲崩溃后恢复成全量重渲」（`manju.go:2087-2095`）。
7. 自动分集识别：`autoByChapter := ds.Episode == "0"`（防把 "0" 当单集生成 `analysis/0_*`、`clips/0/` 幽灵集，`manju.go:2096-2099`）。
8. 调 `startManjuRun(cfgPath, "", episode, "all", resumeOnly, "", autoByChapter, false, false)` —— **phase 固定 `"all"`**（`manju.go:2101`）。
9. 不 break：`startManjuRun` 返回"已有任务运行中"是已恢复第一个任务后的正常状态，其余项目留待下次启动（`manju.go:2102-2103`）。

前端手动「▶ 续跑」走的也是 `phase="all"`（`manju.js:2257` / `manju.js:3368`，横幅按钮 `manju.js:391`，提示语 `manju.js:199-201`）。

### 5.6 停止语义（与检查点交互）

`manjuKill`（`manju.go:2111-2165`）：
1. `manjuState.stopped = true`（`manju.go:2117`）。
2. 用该项目 `config.render.comfy_url`（不再硬编码 8190，`manju.go:2120-2128`）。
3. `POST /interrupt` 中断当前采样（`manju.go:2133-2138`）。
4. `POST /queue {"clear": true}` 清空 pending 队列 —— 注释指出"只 interrupt 会停当前镜，已提交排队的后续镜头仍会继续跑"（`manju.go:2139-2145`）。
5. 最多等 3 s 让管线感知（`manju.go:2148-2159`）。

`comfy.Wait` 内部也做 `interruptOnStop`：停止感知触发时主动 `POST /interrupt`（`comfy/client.go:167-181`），且 `stoppedOnce` 保证只发一次。
`manjuFinish` 里 `stopped` 时把 `rc` 强制改为 0 并提示"已完成镜头保留,可直接续跑"（`manju.go:2095-2099`）。

---

## 6. 断点续跑的边界

### 6.1 阶段级：哪些自动恢复、哪些不

| 阶段 | 崩溃后自动恢复 | 机制 / 原因 | 证据 |
|---|---|---|---|
| `env` | 不适用（不是管线阶段） | 前端健康卡按需调用 | `manju.go:1793` |
| `plan` | ❌ **不自动** | 白名单不含 plan；无 GPU 消耗，用户按需重跑 | `manju.go:2073-2077` |
| `assets` | ❌ **不自动** | 白名单不含 assets；同上 | 同上 |
| `encode` | ✅ 自动 | 白名单含 encode；`.pt` 存在即跳过 | `manju.go:2075`、`manju_pipeline.go:5781/5909` |
| `render` | ✅ 自动 | 白名单含 render（含磁盘 Stage="all" 的情况）；检查点收回 + 已有 mp4 跳过 | `manju.go:2075`、`manju_render_ck.go:80` |
| `qc` | ✅ 自动 | 白名单含 qc；**质检本身不幂等，每次重跑** | `manju.go:2075`、`manju_pipeline.go:7559` |
| `assemble` | ❌ 不单独自动 | 白名单不含 assemble，但从 `all` 恢复会走到它 | `manju.go:2075` |
| `upscale` | ❌ 不自动 | 独立任务，无检查点/无恢复；已完成的 2k 产物保留，重跑时需重新提交（未见跳过逻辑） | `manju_upscale.go:402-413` |

### 6.2 镜头级：为什么"全阶段幂等"

| 阶段 | 幂等键 | 跳过条件 | 证据 |
|---|---|---|---|
| 场景/角色资产 | 目标文件存在 | `fileExists(dst)` | `manju_pipeline.go:5448/5619/5454` |
| 条件编码 | `<项目>_v2_c<指纹>.pt` | 文件存在 | `manju_pipeline.go:5781` |
| 渲染 | `clips/<EP>/NN.mp4` + manifest `fp` | 存在且 `current` | `manju_pipeline.go:7233-7270` |
| 草稿渲染 | `clips/<EP>/_draft/NN.mp4` | 存在（智能体模式下若定稿已存在则整镜跳过并 continue） | `manju_agent.go:1441-1446` |
| 质检 | 无（每次都跑） | 无跳过，但 `qc_skip_shots` 可用户级排除 | `manju_pipeline.go:7656-7667` |
| 合成 | 无（每次都重跑 FFmpeg 覆盖成片） | 无 | `manju_pipeline.go:7820` |
| 2K | 无（`manjuUpscaleOne` 未见 fileExists 跳过） | — | `manju_upscale.go:285-320` |

### 6.3 全局闸门（"运行中"拒绝）

| 操作 | 拒绝方式 | 证据 |
|---|---|---|
| 重复启动任务 | `if manjuState.running { return 已有任务运行中 }` | `manju.go:1886-1889` |
| 云端 2K 与管线互斥 | `manjuUpscaleRun` 同样检查 `manjuState.running` | `manju_upscale.go:327-333` |
| 删除项目 | `manjuDeleteProject` 运行中拒绝 | `manju_delete.go:41` |
| 高级清理 | `manjuCleanup*` 运行中拒绝 | `manju_cleanup.go:47/154` |
| 资产删除 | 运行中拒绝（防与渲染撕扯产物） | `manju_agent.go:2410` |

---

## 7. 并发模型

### 7.1 全景

| 环节 | 并发度 | 控制变量 / 常量 | 默认 | 代码 |
|---|---|---|---|---|
| 逐镜 H3 提示词生成 | 4 | **硬编码 `sem := make(chan struct{}, 4)`** | 4 | `manju_pipeline.go:3589` |
| 下一镜条件预编码（渲染期间重叠） | 1（只预编下一镜） | 无配置 | 1 | `manju_pipeline.go:7264-7278`；agent 同款 `manju_agent.go:1423`、定稿轮 `manju_agent.go:1622` |
| 同条件编码去重 | — | `manjuEncSingleflight` + `encGate` | — | `manju_pipeline.go:5765-5800` |
| 视觉审片（VLM 判分） | `judge_concurrency` | 项目 `config.agent.judge_concurrency`，**范围 1–4，默认 2** | 2 | `manju_agent.go:1404`；读取 `manju_agent.go:136-137`；钳制 `manju_agent.go:94-95`（settings）/`2221-2222`、`2256-2257`（项目） |
| 批量 ASR 与视觉审片并行 | 1 + N | `asrWg` 独立 goroutine | — | `manju_agent.go:1481-1487` |
| 空镜 Context IR 扩写 | 2 | `manjuIRConcurrency = 2`（云端限流友好） | 2 | `manju_ir.go:28/163-165` |
| 渲染主循环 | **串行**（GPU 任务逐镜提交等待） | — | 1 | `manju_pipeline.go:7226-7296` |
| assets 生成 | **串行**（逐角色、逐场景 for 循环） | — | 1 | `manju_pipeline.go:5437-5643` |
| encode 主循环 | **串行** | — | 1 | `manju_pipeline.go:5904-5919` |
| 云端 2K | **串行** | — | 1 | `manju_upscale.go:402-413` |
| 定时释放显存 | 1 个延时 goroutine | `render.idle_free_minutes`（默认 10，0=关） | 10 min | `manju_pipeline.go:2059-2081` |

### 7.2 并发度如何配置

* **提示词并发 4 与 IR 并发 2 是编译期常量**，配置文件改不了（`manju_pipeline.go:3589`、`manju_ir.go:28`）。
* **唯一可配的并发度是审片 `judge_concurrency`**：
  * 全局默认：`Settings.Agent.JudgeConcurrency`，合法区间 1–4（`manju_agent.go:94-95`）。
  * 项目覆盖：`config.json` 的 `agent.judge_concurrency`，>0 即覆盖（`manju_agent.go:136-137`）。
  * 对外暴露：`out["judgeConcurrency"]`（`manju_agent.go:334`）。
  * 前端保存时会再次钳制 1–4（`manju_agent.go:2221-2222`）。
* 渲染相关整型字段的范围表在 `manju.go:82-92`（`width/height/fps/steps/turbo_steps/min_shot_seconds/max_shot_seconds/shots_per_take/motion_audio_context`）——**里面没有任何并发字段**，佐证并发度基本不可配。

### 7.3 GPU 串行化怎么保证

**结论：Go 侧没有 GPU 互斥锁，串行化外包给 ComfyUI 自身的单队列。**

证据：
* 全仓 `grep 'gpuMu|comfyMu|renderMu|sync.Mutex'` 在 `internal/manju` + `internal/comfy` 下只找到：`manjuState.mu`（任务状态）、`manjuManifestMu`、`manjuRenderCKMu`、`manjuAgentMu`、`manjuStatsMu`、`manjuNotesMu`、`manjuConfigLocks`（config 读改写）、`encGate.mu`、`logger.mu`、`novelAutoMu/novelWriteMu`、`comfy.installer.mu`（安装器）——**没有一个是围绕 ComfyUI 提交/GPU 的**。
* `comfy.Client.Submit` 就是裸 `POST /prompt`，无任何锁（`comfy/client.go:56-84`）。
* 串行化的实际依据是 ComfyUI 服务端把 `/prompt` 放进单一执行队列：`/queue` 的 `queue_running` 最多 1 条，其余在 `queue_pending`（`comfy/client.go:106-148`）。
* **有意的"并行提交、串行执行"设计**：渲染当前镜时后台预提交下一镜的 Qwen3-VL 编码（`manju_pipeline.go:7262-7276`，注释「GPU 渲染与文本编码可并行,镜头间空窗从「编码+渲染」串行缩短为约一帧渲染时长」）——两者都进 ComfyUI 队列，由 ComfyUI 决定执行顺序。
* 去重防线在 Go 侧只覆盖"同 cacheName 的编码"，不覆盖"两个不同镜头的渲染"——但渲染主循环本身就是串行的，所以不会出现两镜同时渲。
* **风险点（代码已自认）**：`manju_agent.go:1404` 的注释「judgeConcurrency 默认 2，免费档调高易 429」；`manju_agent.go:1399-1402` 说明"渲染不空等——审片期间 GPU 继续渲下一镜"，即审片（网络 API）与渲染（本地 GPU）并行，二者不争 GPU。
* 停止时清队列是必要的，正因为预提交会让队列里堆积任务：`manjuKill` 必须 `POST /queue {"clear":true}`（`manju.go:2141-2145`）。

### 7.4 失败传播

| 场景 | 行为 | 证据 |
|---|---|---|
| 提示词并发中任一失败 | 记录 `firstErr`，`wg.Wait()` 后整体返回 error（阶段失败） | `manju_pipeline.go:3595-3614` |
| 下一镜预编码失败 | **不失败本镜**，只告警「下一镜将串行重试」 | `manju_pipeline.go:7288-7290` |
| 审片判分调用失败 | `jd.Status=="pending" && jd.Error!=""` → 与"不合格"分流，直接升级待人拍板（不空烧 GPU/LLM/VLM） | `manju_agent.go:1500-1508` |
| 渲染 ComfyUI wait 错误 | 立即返回 error，**不自动重试**（旧逻辑会静默重提交，被移除） | `manju_pipeline.go:7428-7436` |
| 云端 2K 单镜失败 | 累计 `failed`，其余继续，最后汇总告警 | `manju_upscale.go:410-414` |
| goroutine panic | 统一走 `safeGo(tag, lg, fn)`（`manju_agent.go:191`）兜底；管线顶层还有 `recover` 写 `logs/crash.log` 并落盘失败态 | `manju_agent.go:191`、`manju.go:2010-2022` |

---

## 8. 项目磁盘目录布局

### 8.1 根路径解析链

`internal/paths/paths.go:36-49`，优先级：**settings.json 显式值 → exe 自包含子目录（存在才用）→ 旧硬编码路径**。

| 变量 | 自包含默认 | 旧硬编码 | 证据 |
|---|---|---|---|
| `ManjuRootDir` | `<exe>/manju` | `C:\Mi\Ai\WorkBench\manju` | `paths.go:37`、`paths.go:28` |
| `NovelRootDir` | `<exe>/novel` | `C:\Mi\Ai\WorkBench\novel` | `paths.go:38`、`paths.go:29` |
| `ComfyRootDir` | `<exe>/comfyui/ComfyUI` | Comfy-Desktop 安装目录 | `paths.go:39`、`paths.go:30` |
| `ComfySharedDir` | `<exe>/comfyui/shared` | Comfy-Desktop Shared | `paths.go:40`、`paths.go:31` |
| `NovelSkillDir` | `<exe>/skills/NiliX-Novel` | `...\.agents\skills\NiliX-Novel` | `paths.go:41`、`paths.go:32` |
| `AssetLibDir` | `<exe>/asset_lib` | — | `paths.go:46` |
| `VoiceLibDir` | `<exe>/asset_lib/voices` | 旧 `<exe>/voice_lib` 自动迁入 | `paths.go:47`、`paths.go:78-79` |
| `CharLibDir` | `<exe>/asset_lib/characters` | 旧 `<exe>/char_lib` 自动迁入 | `paths.go:48`、`paths.go:78` |

`InitPaths` 末尾调 `MigrateAssetLibs`（幂等合并，重名跳过不覆盖）并触发 `onPathsMigrated()` 重建资产索引（`paths.go:49-51`）。

### 8.2 完整 ASCII 树

```
<exe>/                                    # 自包含根（换机整体拷贝即迁移）
├─ NiliX.exe
├─ server/settings.json                   # 漫剧默认 API Key（明文，见 manju.go:35）
├─ manju/                                 # ManjuRootDir（全部项目 + 平台级日志）
│  ├─ logs/
│  │  ├─ media/manju_media.py             # go:embed 运行时释放（每次覆盖）
│  │  │        face_detection_yunet_2023mar.onnx
│  │  ├─ diagnose/<项目>_diagnose.json     # 任务结束自动快照
│  │  ├─ crash.log                        # panic 堆栈
│  │  └─ notify.json                      # 微信推送配置
│  │  （注意：asset_lib 在 <exe>/asset_lib，不在 manju/ 下；见下方）
│  └─ <项目名>/                            # === 一个项目 ===
│     ├─ config.json                      # 渲染配置 + paths + agent 节
│     ├─ run_state.json                   # 运行状态（running/Stage/CurrentStage/shotCur/logTail…）
│     ├─ run.log                          # 运行日志（追加分隔线；>512KB 截尾保留 256KB）
│     ├─ agent_state.json                 # 审片报告/升级/学习记忆（换集清空）
│     ├─ notes.json                       # 项目笔记（manju_notes.go）
│     ├─ <EP>_成片.mp4                     # 合成产物
│     ├─ <EP>_成片.srt                     # 字幕（srt_out 默认开）
│     ├─ <EP>_成片_微创版.mp4               # micro_voiceover 默认开
│     ├─ <EP>_预告片.mp4                    # agent 模式预告片
│     ├─ analysis/                        # 方案与记账
│     │  ├─ <EP>_direct_plan.json         # ★ 方案（characters/scenes/shots + chapters/novel_fp/script_parse_ver）
│     │  ├─ <EP>_characters.json          # 角色+场景卡（无 shots，抽卡/角色管理用）
│     │  ├─ <EP>_shots_prompts.json       # 逐镜 h3_prompt 缓存（缺失才生成）
│     │  ├─ <EP>_manifest.json            # ★ 产物时效清单（fp/size/seam/at）
│     │  ├─ <EP>_render_ck.json           # ★ 渲染检查点（"03"→prompt_id，"03@d"→草稿）
│     │  ├─ fingerprints.jsonl            # 反同质化指纹（每集一行，同集幂等替换）
│     │  └─ _frames/<EP>/<NN>/            # 审片抽帧（可清理）
│     ├─ assets/                          # 资产
│     │  ├─ asset_map.json                # characters/scenes 路由表 + views_gen/q_gen/portrait_gen
│     │  ├─ characters/
│     │  │  ├─ <cid>.png                  # 主定妆
│     │  │  ├─ <cid>_face.png             # 正脸特写（YuNet 裁切）
│     │  │  ├─ <cid>_full.png             # 全身（832×1248）
│     │  │  ├─ <cid>_side.png
│     │  │  ├─ <cid>_detail.png
│     │  │  ├─ <cid>_q.png                # Q 版
│     │  │  ├─ <cid>_form2.png            # 真身形态（有 second_form）
│     │  │  └─ _gacha/                    # 抽卡候选（可清理）
│     │  ├─ scenes/
│     │  │  ├─ <sid>.png                  # 场景图 1024×1024
│     │  │  └─ <sid>_end.png              # FL2VA 尾帧（开关开时）
│     │  └─ <封面图>                       # copyProjectCover 从小说目录检索
│     ├─ clips/                           # 逐镜产物
│     │  └─ <EP>/
│     │     ├─ 01.mp4 … NN.mp4            # 定稿（%02d.mp4）
│     │     ├─ _draft/                    # 草稿预审产物（定稿轮完成后整目录删除）
│     │     │  └─ NN.mp4
│     │     └─ 2k/                        # 云端 2K 产物
│     │        └─ NN.mp4
│     ├─ qc/
│     │  ├─ <EP>_qc.json                  # 质检报告
│     │  └─ <EP>_rerun.json               # 质检自愈重试轮数侧车（跨运行单调）
│     └─ script/                          # 脚本直出模式（多集）：EP01.md … EPxx.md
│
├─ novel/                                 # NovelRootDir 小说库（fs 白名单根）
├─ asset_lib/                             # 跨项目资产库（paths.AssetLibDir）
│  ├─ characters/                         # CharLibDir
│  │  ├─ index.json                       # 汇总索引（七字段，技能侧直读）
│  │  └─ <角色名>/
│  │     ├─ card.json                     # 角色卡 + fingerprint
│  │     └─ <角色名>{.png,_front,_full,_side,_detail,_q,_form2}.png
│  └─ voices/                             # VoiceLibDir
│     ├─ index.json
│     └─ audio/
│        ├─ lib_<key>.mp3                 # 音色参考音频
│        ├─ lib_<key>.txt                 # 念白文本 meta（升级自愈锚）
│        └─ lib_<key>.src                 # 外部音源标记（存在则跳过 edge-tts）
│
├─ comfyui/
│  ├─ ComfyUI/                            # ComfyRootDir（main.py / .venv）
│  │  └─ output/h3_context/<项目>_<EP>/clip_%05d.safetensors   # 接缝 latent
│  └─ shared/                             # ComfySharedDir
│     ├─ models/
│     │  ├─ conditioning/<项目>_v2_c<指纹>.pt                   # ★ 条件缓存
│     │  ├─ diffusion_models|unet/        # H3 / Z-Image / Krea-2
│     │  ├─ text_encoders|clip/
│     │  ├─ vae/
│     │  └─ loras|pdd_acc/
│     ├─ input/                           # ComfyUI 输入（参考图副本 dir_char_*/dir_scene_*、audio/）
│     └─ output/                          # ComfyUI 原始产物（收回后 copy 走）
│
└─ skills/NiliX-Novel/                    # 小说续作技能
```

### 8.3 布局关键证据

| 路径 | 写入者 | 证据 |
|---|---|---|
| 项目目录 + 5 个子目录 | `manjuCreateProject`：`""`, `analysis`, `assets/characters`, `assets/scenes`, `clips` | `manju_pipeline.go:8228-8234` |
| `config.paths.*` 默认值 | `manjuDefaultConfig` | `manju_pipeline.go:8463-8478` |
| `run_state.json` / `run.log` | `manjuRunStatePath` / `manjuRunLogPath` | `manju.go:170-177` |
| `agent_state.json` | `manjuAgentStatePath` | `manju_agent.go:281` |
| `<EP>_manifest.json` | `manifestSave` | `manju_manifest.go:34-52` |
| `<EP>_render_ck.json` | `renderCKSave` | `manju_render_ck.go:27-45` |
| `qc/<EP>_qc.json` / `_rerun.json` | `qcReportPath` / `qcRerunPath` | `manju_pipeline.go:7750-7786` |
| `analysis/_frames/<EP>/<NN>/` | `manjuAgent` 抽帧 | `manju_agent.go:953` |
| `clips/<EP>/_draft/` | `draftDir` | `manju_pipeline.go:1906-1908` |
| `clips/<EP>/2k/` | `manjuUpscale2kDir` | `manju_upscale.go:281-283` |
| `models/conditioning/*.pt` | `h3CachePath` | `manju_comfy.go:545-547` |
| `output/h3_context/<ns>/clip_%05d.safetensors` | `h3ContextLatentPath`；`ns = <项目>_<EP>` 去非单词字符 | `manju_comfy.go:541-543`、`manju_pipeline.go:390-392` |
| `logs/media/manju_media.py` + onnx | `ensureMediaHelper`（**总是覆盖**） | `manju_pipeline.go:7998-8008` |
| `logs/diagnose/<项目>_diagnose.json` | `manjuWriteDiagnoseSnapshot`（每次任务结束） | `manju_diagnose.go:19-24/74` |
| `logs/crash.log` | `manjuWriteCrash` | `manju_diagnose.go:97-103` |
| `asset_lib/characters/<名>/` + `index.json` | `manjuCharLibStore` | `manju_char_lib.go:14-25/216`、`manju_asset_index.go:85` |
| `asset_lib/voices/audio/lib_*.mp3` | `genVoiceLibAudio` | `manju_pipeline.go:6589-6647` |

### 8.4 清理保护边界

`manju_cleanup.go` 的护栏：**只清 `_gacha`/`_frames`/`2k` 三类缓存，绝不触碰定妆照 `characters/*.png`、场景图 `scenes/`、成片 `_成片.mp4`、2k 产物本身**（`manju_cleanup.go:127`，目标枚举 `manju_cleanup.go:63-67` 与 `391-393`）。

高级清场（`manju_cleanup.go:154+`）会额外清理：
* `analysis` 下的方案类文件（保留 `_frames`/`_gacha` 子目录，`manju_cleanup.go:166`）；
* 非运行态下的 `run_state.json` / `agent_state.json` 残留（`manju_cleanup.go:177-179`）；
* `workdir/<EP>_成片.mp4` / `_预告片.mp4`（注释说明"此前护栏保住成片，清场后产物面板仍挂着，观感即没清干净"，`manju_cleanup.go:239-244`）；
* 角色资产重置（主图/视图/角色板/Q版/`_gacha`/`adopted.json`，`manju_cleanup.go:273-281`）；
* 全部项目 `run.log` 截断（`manju_cleanup.go:287-307`）；
* `logs/diagnose/*` 旧快照（`manju_cleanup.go:310-316`）。

（注意 `manju_cleanup.go:273` 仍提到"角色板"，但 `manju_pipeline.go:5545-5547` 说明 `_board.png` 已废弃并在 `views_gen` 升级时统一删除——文档/代码有轻微不一致。）

---

## 9. 关键数字与常量速查

| 常量 / 配置 | 值 | 位置 |
|---|---|---|
| `manjuStageOrder` | 8 项 | `manju.go:73` |
| 执行阶段数 | 6 | `manju_pipeline.go:2010` |
| `manjuPhases` | all + 6 | `manju.go:75-77` |
| 条件缓存代数 | `manjuCacheVer = "v2"` | `manju_pipeline.go:5676` |
| 条件指纹长度 | 前 10 位 hex | `manju_pipeline.go:5750-5752` |
| 资产指纹长度 | 前 8 位 hex | `manju_pipeline.go:5665-5669` |
| 渲染指纹算法 | `md5(condFp + "|a=" + assetsFp + q)` | `manju_manifest.go:69` |
| 解析器代数 | `manjuScriptParseVer = 25` | `manju_script_parse.go:338` |
| 视图/ Q版 / 主图代数 | `manjuViewGen=10` / `manjuQGen=23` / `manjuPortraitGen=6` | `manju_pipeline.go:3928/3969/3982` |
| 分辨率档位 | `draft:416, standard:768, fhd:1088`（短边） | `manju_comfy.go:337/349-358` |
| 默认渲染 | 768×1344@24fps，steps 20，turbo_steps 8，seed 1688 | `manju_pipeline.go:8406`、`manju_pipeline.go:386/403` |
| 默认草稿预审 | `draft_judge=true`，`draft_scale=0.5` | `manju_pipeline.go:8411-8412` |
| 默认时长/连拍 | `min_shot_seconds=5`，`max_shot_seconds=7`，`shots_per_take=2` | `manju_pipeline.go:8410` |
| 默认 seed 策略 | `increment` | `manju_pipeline.go:8413` |
| 提示词并发 | 4（硬编码） | `manju_pipeline.go:3589` |
| IR 并发 | `manjuIRConcurrency = 2` | `manju_ir.go:28` |
| 审片并发 | `judge_concurrency` 1–4，默认 2 | `manju_agent.go:94-95/1404` |
| 编码等待超时 | 1800 s（`ensureEncodedAt` 内 `Wait`） | `manju_pipeline.go:5857` |
| 编码 singleflight 等待窗 | 1900 s | `manju_pipeline.go:5804` |
| 渲染等待超时 | 3600 s | `manju_pipeline.go:7429` |
| 图片等待超时 | 900 s | `manju_pipeline.go:5332` |
| 检查点丢失判定窗 | 90 s（`manjuReclaimLostWait`），查询失败/在队列则每次 +30 s | `manju_render_ck.go:72/113/117` |
| ComfyUI 任务丢失计数 | 连续 30 次（轮询 10 s ≈ 5 min） | `comfy/client.go:191/221` |
| 质检自愈上限 | 2 次换 seed，之后提示逃生门 | `manju_pipeline.go:7258-7265` |
| 空闲释放显存 | `idle_free_minutes` 默认 10，0=关 | `manju_pipeline.go:2059-2066` |
| 运行日志截断 | >512 KB → 保留尾部 256 KB | `manju.go:183-196` |
| status 返回日志上限 | 500 KB | `manju.go:2242-2244` |
| 定妆/视图尺寸 | 1024×1024 / 832×1248 | `manju_pipeline.go:3897/3902` |
| 云端 2K 轮询 | 每 10 s，上限 30 min | `manju_upscale.go:310-312` |
| 自动恢复白名单 | `all / encode / render / qc` | `manju.go:2073-2077` |

---

## 10. 与 README/注释不一致之处（调研发现）

| # | 不一致 | 代码事实 | 证据 |
|---|---|---|---|
| 1 | README「六阶段」vs 运行态 `stageTotal=8` | 两者都对，但口径不同：8 = 执行 6 阶段 + `env` + `upscale` 两个旁路任务；`env`/`upscale` 都不可作为 `phase` 寻址 | `manju.go:73` vs `manju_pipeline.go:2010`、`manju.go:75-77` |
| 2 | README「current/stale/missing **四态**」 | 实际只有 3 态；`unknown` 已于 2026-08-26 折叠进 `stale`（函数只有 3 个 return） | `manju_manifest.go:103-119` vs `manju_manifest.go:99`（过时注释） |
| 3 | README 架构图写 `internal/api/manju_pipeline.go` | 实际在 `internal/manju/manju_pipeline.go`；`internal/api/` 下没有 manju 文件 | `ls internal/api`（仅 api/harness/island/kb/render/script/sysmon/zcode） |
| 4 | README「两阶段条件缓存」 | 描述准确，但隔离仅靠指纹里的 `w=`/`h=`，没有独立命名空间；且 `res_tier=draft`(416) 与 `draft_scale` 草稿预审是两个不同概念 | `manju_pipeline.go:5694`、`manju_comfy.go:337` |
| 5 | 「白名单含 all」 | 正确，但指的是 **`AutoRecoverRendering` 的阶段白名单** `{all,encode,render,qc}`，容易与 `manjuPhases` 白名单混淆 | `manju.go:2073-2077` |
| 6 | 进度条到不了 100% | 分母恒 8，实际 currentStage 最大 `assemble`（索引 6）→ 上限 ≈81% | `manju.go:2190`、`manju.go:73` vs `manju_pipeline.go:2010` |
| 7 | 云端 2K 期间阶段显示为空 | `manjuUpscaleRun` 不调 `logStage`，而 `currentStage` 从日志横幅解析 → `stageIdx=-1`、`progress=0` | `manju_upscale.go:329` vs `manju.go:2174-2188` |
| 8 | 前端阶段条缺 `upscale` | `FLOW`/`STAGE_ORDER` 均只有 7 项 | `manju.js:60-68/4127` |
| 9 | 编码阶段无崩溃检查点 | 只有 `.pt` 落盘与否的事后校验；编码任务丢失无 prompt_id 可收回（编码是同步等待，未提供 reclaim 路径） | `manju_pipeline.go:5857-5862`、`manju_render_ck.go` 仅覆盖渲染 |
| 10 | 云端 2K 无幂等 | `manjuUpscaleOne` 未见 `fileExists(dst)` 跳过；重复点会重新提交云端任务 | `manju_upscale.go:285-320` |
| 11 | `manju_cleanup.go` 仍提「角色板」 | `_board.png` 已废弃并在 `views_gen` 换代时删除 | `manju_cleanup.go:273` vs `manju_pipeline.go:5545-5547` |

---

## 11. 未在代码中确认的问题（诚实标注）

1. **`env` 是否会作为 `manjuPipelineRun` 的隐式前置**：未找到任何"跑管线前自动跑 manjuEnvCheck"的调用。前端只在健康卡片里独立调用（`manju.js:3583`）。管线各阶段自带 `ensureComfyReady`（`manju_pipeline.go:171-191`），但不做完整环境自检。
2. **`upscale` 与其他阶段的实际协作**：未见自动触发（例如 assemble 后自动升 2K）的代码；只有显式 API 入口。`manjuAgentPipelineRun` 的阶段 switch（`manju_agent.go:406-440`）不含 upscale。
3. **检查点文件是否会无限增长**：`renderCKClear` 在成功/失败/重渲三条路径都会清，但若进程在 `Submit` 成功后、`Wait` 返回前被硬杀且 ComfyUI 也被重启，该 key 会残留至下次 `tryReclaim` 判丢失后清除——未找到定期 GC。
4. **`/queue` 接口在 ComfyUI 高负载（加载几十 GB 模型）时的超时行为**：代码用"查询失败=忙，重置/延长"来处理（`comfy/client.go:216`、`manju_render_ck.go:112-113`），但 `Client.HTTP` 超时仅 30 s（`comfy/client.go:23`），实际行为依赖 ComfyUI 版本。
5. **多项目并行的可能性**：`AutoRecoverRendering` 只恢复第一个成功的项目（`manju.go:2102-2103` 注释），但 `manjuState` 是全局单例任务（`manju.go:147`），因此**同时只有一个管线任务**；未找到按项目并行的设计。
