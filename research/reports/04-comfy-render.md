# NiliX 调研报告 04 — ComfyUI 集成、工作流驱动与模型管理

> 调研对象：`research/NiliX-main`（用户提供的 NiliX-main.zip 解压，权威最新版）
> 调研方式：只读源码逐行核对（`read`/`grep`），行号均为当前工作区实际行号
> 结论口径：**代码为准**；`docs/漫剧渲染视频技术文档.md`、`README.md` 作为交叉印证，凡与代码冲突处已在 §11 明确标注（该技术文档头部自述"基于 2026-08-20 代码梳理"，已明显落后于 2026-09-04 的画质升级）

---

## 0. 九个问题的结论速览

| # | 问题 | 结论（一句话） |
|---|---|---|
| 1 | 工作流如何组织 | **纯 Go 代码构造节点图**（`map[string]any` 字面量 + `wfAdd` 自增 ID + `refOf` 接线），**不加载任何 ComfyUI workflow JSON 模板**；JSON 只出现在 `/prompt` 请求体、项目方案文件与"版本管理"页的清单展示里 |
| 2 | 节点依赖与替换 | 全部靠 `/object_info/<节点名>` 探测 + 一次性缓存（每 run 一次）：SageAttn **双名探测**（`PathchSageAttentionKJ`→`PatchSageAttentionKJ`，KJNodes 上游拼错名）、PDD Acc 节点+文件双查、LoRA 文件名缺失按同族回退并写回 config、`MiniMaxH3Fl2VA` 节点不存在改用核心节点 `first_frame/last_frame` |
| 3 | 模型清单与基线 | 安装器清单 5 个模型文件（UNet×2 + VAE×2 + CLIP，CLIP 约 60GB）+ 2 个自定义节点；默认 768×1344@24fps、`steps=20`、`turbo_steps=8`（两者关系：全局 steps 是**无 Turbo LoRA 时的全步数**，挂 LoRA 后取 LoRA 参数表步数）、`seed=1688+镜头号`、帧数 `17k+5` |
| 4 | R2V 参考图注入 | 最多 **3 个角色**，每角色按视图预算 4/3/2 张，加场景图共 **≤8 张**；节点输入用 **Autogrow 平铺键 `ref_images.ref_image_N`**（不是数组）；`ref_image_size` 取 `max`（保留最高 2048 短边编码，身份保真） |
| 5 | 提交与轮询 | `POST /prompt` 拿 `prompt_id` 并**立即落盘检查点**；`/history/<id>` + `/queue` **纯轮询（10s）**，全仓无 WebSocket；超时渲染 3600s / 预编码 1800s；停止感知会主动 `POST /interrupt`；错误分类见 `comfyErrMsg`/`manjuDiagnoseError`；OOM 无自动重试，只做"编码/渲染前 `POST /free` 卸模型" |
| 6 | 分辨率档位与草稿预审 | `manjuResTiers{draft:416, standard:768, fhd:1088}`，短边对齐 32 → 768×1344 / 416×736 / 1088×1920；草稿预审 = 只在**审片返工轮**用 `draft_scale=0.5`（≈1/4 像素量）渲草稿判分，落定后全分辨率定稿重渲，条件缓存按分辨率独立记账 |
| 7 | 自包含部署 | 一键安装 7 步（7zr → portable.7z → `7z t` 完整性 → `7z x` → 搬目录 → git clone 2 节点 → 按清单下模型 → pip 装媒体依赖）；"断点续传"实为**已存在即跳过 + `.part` 原子改名**（无 HTTP Range 续传）；启动参数含 `--listen/--port/--disable-auto-launch/--input-directory/--output-directory` + `PYTHONIOENCODING=utf-8` |
| 8 | 云端 2K | `POST {base}/v2/video_regeneration`，`content=[text, video_url(base_video=data:video/mp4;base64)]`，`resolution=2K`；提交前本地预校验 6 条硬规则（付费前 fail-loud）；轮询 `/v2/query/video_generation/<task_id>`，10s 间隔 / 30min 超时 |
| 9 | 显存/加速 | SageAttention（KJNodes，缺则自动降级）；**无 LowVRAM 节点、无 `--lowvram` 启动开关、无 offload 配置**；显存靠 `POST /free {unload_models,free_memory}` 三处触发（编码前、Agent 渲染前、空闲 10 分钟）+ `/queue` 忙闲门控 |

---

## 1. 工作流如何组织：Go 代码构造节点图，不用 JSON 模板

### 1.1 结论与证据

`internal/manju` 是**生产管线**（`manjuCtx`，全进程 Go 实现），它的工作流 100% 由 Go 代码拼装：

- `internal/manju/manju_comfy.go:4`（文件头注释，权威设计意图）：
  > `节点图重建依据:ComfyUI /object_info 节点 schema + 官方 MiniMaxH3 模板(video_minimax_h3_r2v.json) + ComfyUI-H3-Motion-Context 示例工作流 + ComfyUI-H3-ConditioningCache 源码`
  —— 官方 JSON 模板只是**人工抄读的依据**，运行时不读取。
- `internal/manju/manju_comfy.go:192-196` `wfAdd`：节点 ID = `len(workflow)+1` 自增，值为 `{"class_type": ..., "inputs": ...}`；`manju_comfy.go:135-140` `refOf` 把 `"7"` / `"7[1]"` 文本引用转成 API 格式 `[]any{"7", 1}`。这两个函数就是全部"图编辑器"。
- `internal/manju/manju_comfy.go:86-132` `wfImage`：通用图生图装配器（`typ` ∈ sdxl/zimage/krea2），同样用 `add()` 闭包自增 ID。
- 另一套独立的早期实现 `internal/render/*`（H3 T2V/R2V/Z-Image 资产）也是 Go 字面量，节点 ID 硬编码为 `"1".."15"` / `"50".."59"`（`internal/render/render.go:47-65`、`internal/render/r2v.go:10-53`、`internal/render/asset.go:14-26`）。
- 全仓 `*.go` 中**没有任何** `ReadFile(workflow.json)` / `go:embed *.json workflow` 之类的工作流模板加载；JSON 只在这些地方出现：
  - `internal/comfy/client.go:57` 把 Go 构造的 map 序列化成 `/prompt` 请求体；
  - `internal/comfy/comfy_versions.go:163-189` **只读列出** `<ComfyRoot>/user/default/workflows` 与 `<ComfyRoot>/workflow_templates` 下的 `.json` 文件名/大小给"版本管理"弹窗看；
  - 项目侧的 `analysis/<ep>_direct_plan.json` 等是**方案 JSON**（分镜/提示词），不是 ComfyUI 工作流。

### 1.2 完整工作流 A：预编码（Qwen3-VL 条件缓存，`h3EncWorkflow`）

来源：`internal/manju/manju_comfy.go:205-272`（+`h3Loaders` 185-190、`wfAdd` 192-196）。
帧数 `length = h3Length(s.Duration, fps)`，`w/h` 为档位换算后的实际宽高。

| 节点 ID | class_type | 关键输入 | 说明 |
|---|---|---|---|
| 1 | `CLIPLoader` | `clip_name=<R.clip>`, `type="minimax"` | Qwen3-VL 32B 文本编码器（`h3Loaders` 186） |
| 2 | `VAELoader` | `vae_name=<R.vae_video>` | 视频 VAE（187） |
| 3 | `VAELoader` | `vae_name=<R.vae_audio>` | 音频 VAE（188） |
| 4..N | `LoadImage` | `image=<dir_char_<镜>_<角色序>_<视图序>.png>`；场景图 `image=<dir_scene_<镜>.png>`；尾帧 `image=dir_scene_<镜>_end.png` | 每张参考图一个 LoadImage（219/222/249/265） |
| N+1.. | `LoadAudio` | `audio="audio/voice_*.mp3"` 或 `audio/lib_<音色>.mp3` | 每个说话角色的音色参考（242） |
| 角色镜 | `MiniMaxH3ReferenceToVideo` | `clip=[1,0]`, `vae=[2,0]`, `audio_vae=[3,0]`, `prompt`, `width`, `height`, `length`, `ref_image_size="max"`, **平铺键** `ref_images.ref_image_0..k=[LoadImage,0]`, `ref_audios.ref_audio_0..m=[LoadAudio,0]` | `manju_comfy.go:210-245`；用 `hasChar` 分支 |
| 空镜 | `MiniMaxH3ImageToVideo` | `clip=[1,0]`, `vae=[2,0]`, `prompt`, `width`, `height`, `length`, 可选 `first_frame=[LoadImage,0]`, 可选 `last_frame=[LoadImage,0]` | 246-268；双帧插值走核心节点 |
| 末节点 | `MiniMaxH3CondSave` | `conditioning=[<cond>,0]`, `cache_name=<cacheName>` | 270；写 `models/conditioning/<name>.pt` |

### 1.3 完整工作流 B：采样渲染（`h3RenderWorkflow`）

来源：`internal/manju/manju_comfy.go:366-529`（+`turboLoRASpecOf` 290-332、`h3Fps` 531-537）。

| 节点 ID | class_type | 关键输入 | 生成条件 |
|---|---|---|---|
| 1 | `UNETLoader` | `unet_name = hasChar ? R.unet_ref2va : R.unet_fl2va`, `weight_dtype="default"` | 恒有（372-376） |
| 2 | `MiniMaxH3PDDAccApply` | `model=[1,0]`, `pdd_file=<lora 文件名>`, `nfe="8"`, `lora_strength=1.0`, `head_strength=1.0`, `on_off_grid="error"` | 仅 PDD Acc 模式且 `_pdd_ok`（414-425）；输出 `[0]=model`,`[1]=sigmas` |
| 2' | `LoraLoaderModelOnly` | `model=<上游>`, `lora_name=<turbo_lora[_r2v]>`, `strength_model=<spec.Strength>` | 普通 distill LoRA（437-439） |
| 3 | `PathchSageAttentionKJ` 或 `PatchSageAttentionKJ` | `model=<上游>`, `sage_attention="auto"`, `allow_compile=false` | `sageEnabled(R)` 且探测到节点；节点名取探测值（444-452） |
| 4 | `MiniMaxH3SigmaShift` | `model=<上游>`, `shift_video=<spec.VideoShift>`, `shift_audio=<spec.AudioShift>` | `spec.VideoShift>0 && !PDD`（456-460） |
| 5/6 | `VAELoader` ×2 | `vae_name=R.vae_video` / `R.vae_audio` | 恒有（461-462） |
| 7 | `MiniMaxH3CondLoad` | `cache_name=<cacheName>` | 恒有（464） |
| 8 | `EmptyMiniMaxH3LatentAV` | `width`, `height`, `length` | 恒有（465） |
| 9 | `MiniMaxH3MotionContextLoadLatent` | `latent_path="h3_context/<ns>"`, `clip_index=prevIdx` | 接缝镜（`chained`）才有（469-471） |
| 10 | `MiniMaxH3MotionContext` | `conditioning=[7,0]`, `vae=[5,0]`, `latent=[8,0]`, `context_length="22"`, `audio_context_length=<1..96>`, `context_latent=[9,0]` | 接缝镜（480-484）；输出 `[0]=conditioning`,`[1]=trim_frames` |
| 11 | `BasicGuider` | `model=<上游>`, `conditioning=<cond>`，PDD 模式加 `cfg=1.0` | 491-495 |
| 12 | `RandomNoise` | `noise_seed=<seedFor(s.ID, attempt)>` | 496 |
| 13 | `KSamplerSelect` | `sampler_name=<spec.Sampler>`（默认 `res_multistep`；PDD/lightx2v 为 `euler`） | 498 |
| 14 | `BasicScheduler` | `model=<上游>`, `scheduler=<spec.Scheduler>`, `steps=<steps>`, `denoise=1.0` | 非 PDD（500-503） |
| 15 | `SamplerCustomAdvanced` | `noise=[12,0]`, `guider=[11,0]`, `sampler=[13,0]`, `sigmas=<14 或 PDD[1]>`, `latent_image=[8,0]` | 504-507 |
| 16 | `VAEDecode` | `samples=[15,0]`, `vae=[5,0]` | 509 |
| 17 | `VAEDecodeAudio` | `samples=[15,1]`, `vae=[6,0]` | 510 |
| 18 | `MiniMaxH3MotionContextTrim` | `images=[16,0]`, `trim_frames=<10[1]>`, `audio=[17,0]`, `fps`, `match_tail=true` | 接缝镜裁 burn-in（514-520） |
| 19 | `CreateVideo` | `images`, `fps`, `audio` | 521 |
| 20 | `SaveVideo` | `video=[19,0]`, `filename_prefix="manju"`, `format="auto"`, `codec="auto"` | 522 |
| 21 | `MiniMaxH3MotionContextSaveLatent` | `latent=[15,0]`, `filename_prefix="h3_context/<ns>/clip"`, `clip_index=curIdx` | **无论是否接缝都保存**，供下一镜续接（525-527） |

节点端口语义（官方连线次序）在调试面板里同源复刻，可作交叉验证：`internal/manju/manju_shot_debug.go:241-247`（`MiniMaxH3MotionContext` 4 入 2 出、入序 `conditioning/vae/latent/context_latent`）。

### 1.4 完整工作流 C：定妆照/场景图（`wfImage` / `wfZImage` / `wfKrea2`）

`internal/manju/manju_comfy.go:86-132`，统一装配器按 `typ` 分支：

| 节点 | class_type | 关键输入 |
|---|---|---|
| L1 | `CheckpointLoaderSimple`（sdxl）**或** `UNETLoader`（zimage/krea2） | `ckpt_name` / `unet_name`+`weight_dtype="default"` |
| L2 | （zimage/krea2）`CLIPLoader` | `clip_name`, `type`：zimage→`qwen_image`（157），krea2→**`krea2`**（166-168，注释：用 `qwen_image` 会报 `Krea2 expects conditioning with 12x2560=30720 features`） |
| L3 | `VAELoader` | `vae_name` |
| 4 | `CLIPTextEncode` | 正向 prompt |
| 5 | `CLIPTextEncode` | 负向 `manjuNegPrompt` + 用户 `neg_prompt`（`negPrompt()` 3849 起） |
| 6 | `LoadImage` + `ImageScale`(`lanczos`, `width/height`, `crop="disabled"`) + `VAEEncode` | 仅 img2img（`initImage != ""`）：视图重绘/角色板（109-114） |
| 7 | `EmptyLatentImage`（sdxl）/ `EmptySD3LatentImage`（zimage,krea2） | `width`,`height`,`batch_size=1` |
| 8 | `KSampler` | `model/positive/negative/latent_image`, `seed`, `steps`（zimage/krea2=8）, `cfg=1.0`, `sampler_name="euler"`, `scheduler="normal"`, `denoise=<initStrength 或 0.6 或 1.0>` |
| 9 | `VAEDecode` | `samples=[8,0]`, `vae` |
| 10 | `SaveImage` | `images=[9,0]`, `filename_prefix` |

（`wfZImage` 见 155-159、`wfKrea2` 见 166-170；提交封装 `comfyGenImage` 5326。）

### 1.5 附：`internal/render` 包的 workflow（对照用，非生产路径）

- T2V：`internal/render/render.go:47-65`，15 节点：`UNETLoader(1)`→`CLIPLoader(2,type=minimax)`→`VAELoader(3)`/`VAELoader(4)`→`MiniMaxH3SigmaShift(5, shift 12/3)`→`MiniMaxH3ImageToVideo(6)`→`KSamplerSelect(7, res_multistep)`+`BasicScheduler(8, simple)`→`BasicGuider(9)`+`RandomNoise(10)`→`SamplerCustomAdvanced(11)`→`VAEDecode(12)`+`VAEDecodeAudio(13)`→`CreateVideo(14)`→`SaveVideo(15, format=mp4)`。
- R2V：`internal/render/r2v.go:10-53`，节点 `"50"` `MiniMaxH3ReferenceToVideo` + `"51".."59"`，参考图键为 **`ref_image_%d`**（21-25）——与生产路径的 `ref_images.ref_image_%d` **不同名**，见 §11.2。

---

## 2. 节点依赖探测与替换策略

### 2.1 探测入口：`HasNode` = `GET /object_info/<name>`

`internal/comfy/client.go:26-35`：

```go
// hasNode 查询 ComfyUI 是否装有某自定义节点(/object_info/<name>,200=有)
func (c *Client) HasNode(name string) bool {
	resp, err := c.HTTP.Get(c.Base + "/object_info/" + name)
	if err != nil { return false }
	...
	return resp.StatusCode == 200
}
```

`manju` 包通过别名直接使用：`internal/manju/http_helpers.go:30` `type comfyClient = comfy.Client`。

### 2.2 SageAttention 双名探测 + 自动降级

- 开关缺省即开：`internal/manju/manju_pipeline.go:105-110` `sageEnabled`（`R["sage_attention"]` 非 bool 时返回 `true`）。
- 探测与降级：`manju_pipeline.go:112-127` `sageAttnGuard`：

```go
for _, n := range []string{"PathchSageAttentionKJ", "PatchSageAttentionKJ"} {  // :117
    if ctx.comfy.HasNode(n) { ctx.sageOK = true; ctx.sageNodeName = n; return }
}
ctx.sageOK = false; ctx.sageNodeName = ""
lg.logf("  ⚠️ ComfyUI 缺少 PatchSageAttentionKJ/PathchSageAttentionKJ 节点(未装 ComfyUI-KJNodes),SageAttn 已自动关闭继续渲染…")  // :126
```

- 每 run 只探测一次（`ctx.sageChecked`，`manju_pipeline.go:82-84` 字段定义 + `:113` 早退），因为逐镜 HTTP 探测太慢。
- 结果**不写共享 `ctx.R`**，而由 `applySageToR`（`:130-138`）注入每个调用的 **R 副本**——注释 `:100-102` 说明原因：预编码 goroutine 与渲染主 goroutine 并发，避免 `map` 并发读写；`renderShotTo` 的 `rCopy`（`:7382-7388`）即此用途。
- 实际插入节点：`manju_comfy.go:444-452`，节点名取 `R["sage_node_name"]`，参数 `{"model":…, "sage_attention":"auto", "allow_compile":false}`。
- 提前降级点（让"生效参数总览"显示真实值）：`manju_pipeline.go:7182`（stageRender）、`manju_agent.go:1343`（Agent 流水线）；`renderShotTo:7324` 兜底所有路径。
- 体检与自检：`manju_agent_health.go:192-198`（缺节点 → `Status:"bad"`）、`manju_pipeline.go:8134-8149`（`manjuEnvCheck` 打印 `✅ ComfyUI 节点 <实际名> 可用`）。

### 2.3 KJNodes / LoRA 文件名不匹配的处理（两层，都是"按文件名族回退并写回 config"）

**(a) 画质档一次性迁移** `normalizeQualityUpgrade`（`manju_pipeline.go:446-497`，`quality_gen` 版本键防重复）：

| 迁移 | 触发条件 | 目标 |
|---|---|---|
| ① VAE int8→fp16 | `R.vae_video` 含 `int8_convrot` 且磁盘有 `minimax_h3_video_vae_fp16.safetensors` | `:454-459` |
| ② lightx2v 4step→PDD 8step | `turbo_lora` / `turbo_lora_r2v` 含 `4step` 且磁盘有 `minimax_h3_{fl2va,ref2va}_pdd_acc_8step_comfyui.safetensors` | `:461-474`（两键独立判定） |
| ③ `ref_image_size` → `max` | 非 `max` | `:482-488` |
| 收尾 | 写 `quality_gen=2`，原子写回项目 config | `:489-496` |

**(b) 渲染入口存在性归一** `normalizeTurboLora`（`manju_pipeline.go:505-564`）：
扫描 `models/loras/*.safetensors` **并** `models/pdd_acc/*.safetensors`（`:519-526`，因 PDD 单文件放 pdd_acc 而非 loras）；对 `turbo_lora`（族关键词 `fl2v`）与 `turbo_lora_r2v`（`ref2v`）分别执行 `fix(key, pat)`：当前值在任一目录都不存在 → 回退到"磁盘上第一个同族文件"（`firstOf`，`:530-537`）并 `writeManjuConfig` 写回（`:559-563`）。找不到同族文件则**保持原值不动**（`:504` 注释：不静默清配置）。

对应报错原话（`:501-503`）：`「minimax_h3_turbo_4step_ema.safetensors(未找到)」类报错的根治：该旧默认名随 Turbo LoRA 部署升级停发，存量项目经此自愈`。

**(c) 引擎不兼容时的参数侧摘除**（不是节点替换，但属同一策略族）：`manju_comfy.go:382-403`——FL2V 专用蒸馏 LoRA 遇到角色镜自动摘掉并回退全步数；`R2VOnly` LoRA 遇空镜对称回退。

### 2.4 节点不存在时的替代实现（`MiniMaxH3Fl2VA` 事件）

`manju_comfy.go:251-256`（代码注释即事故记录）：

```go
// 双帧(FL2VA 语义)与单帧统一走核心节点 MiniMaxH3ImageToVideo:
// ComfyUI 0.33+ 核心节点自带 first_frame + last_frame 双帧插值参数(last_frame 即尾帧锚点)。
// 不再用自定义节点 MiniMaxH3Fl2VA——该节点不存在
// (ComfyUI 对缺失节点 /object_info 也返回 200 空对象,提交时 400 missing_node_type,用户实测)。
```

即：**替换策略 = 用核心节点参数替代不存在的自定义节点**（`first_frame`/`last_frame`，`manju_comfy.go:260-267`、尾帧注入在 `ensureEncodedAt:5834-5844`）。

### 2.5 PDD Acc：节点 + 文件双查（`pddGuard`）

`manju_pipeline.go:145-158`：先 `HasNode("MiniMaxH3PDDAccApply")`（`:150`），再查 `models/pdd_acc/<turbo_lora|turbo_lora_r2v>` 文件存在（`:152-154`）；不可用则记 `ctx.pddOK=false` 并告警，`applyPddToR`（`:161-166`）写 `R["_pdd_ok"]`，`h3RenderWorkflow:414-435` 据此回退普通 `LoraLoaderModelOnly` 模式并把步数回退到 `R.steps`（全步数）。

**节点真实 schema**（`:408-412` 注释为事故修复记录）：
> `2026-09-04 实锤修复(镜1 提交 400 required_input_missing ×5):节点真实 schema 必填 pdd_file/nfe/lora_strength/head_strength/on_off_grid(源码 nodes.py INPUT_TYPES 权威),旧代码只传 model+lora_name——字段名就错了(lora_name 非该节点输入),PDD 模式此前从未跑通过。`

对应字段：`manju_comfy.go:416-423`。

### 2.6 ⚠️ 探测判据的自相矛盾（风险，见 §11.1）

`comfy/client.go:26` 断言"200=有"，但 `manju_comfy.go:254-255` 明确写着"ComfyUI 对缺失节点 `/object_info` 也返回 200 空对象"。两处注释直接冲突，而实现只判断 `StatusCode == 200`（`client.go:34`），**不解析响应体**。若后者为真，`sageAttnGuard` 会永远命中列表里第一个名字 `PathchSageAttentionKJ`，`pddGuard` 的节点判定也恒真——降级逻辑只在 ComfyUI 整体不可达时才触发。

---

## 3. 模型清单与参数基线

### 3.1 一键安装器的模型清单（权威"需要哪些文件"）

`internal/comfy/comfy_install.go:56-75` `comfyDefaultResources`（下载基址 `comfyModelBase = "https://huggingface.co"`，`:52`，可改 `hf-mirror.com`）：

| Kind | 名称 | 落盘相对路径（portable/tool→临时目录；node→`<ComfyRoot>/custom_nodes`；model→`<ComfyShared>/models`） | URL |
|---|---|---|---|
| portable | ComfyUI Windows Portable 约 1.5GB | `portable.7z` | `github.com/comfyanonymous/ComfyUI/releases/download/latest/ComfyUI_windows_portable_nvidia.7z` |
| tool | 7-Zip 解压器 | `7zr.exe` | `7-zip.org/a/7zr.exe` |
| node | ComfyUI-KJNodes（SageAttention） | `ComfyUI-KJNodes` | `github.com/kijai/ComfyUI-KJNodes` |
| node | ComfyUI-MiniMaxH3-Easy（H3 节点） | `ComfyUI-MiniMaxH3-Easy` | `github.com/nkxx188/ComfyUI-MiniMaxH3-Easy` |
| model | H3 FL2VA（文生视频/空镜） | `diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors` | `MiniMaxAI/MiniMax-H3/resolve/main/…` |
| model | H3 Ref2VA（参考图生视频/角色镜） | `diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors` | 同上 |
| model | H3 Video VAE | `vae/minimax_h3_video_vae_fp16.safetensors` | 同上 |
| model | H3 Audio VAE | `vae/minimax_h3_audio_vae_fp32.safetensors` | 同上 |
| model | H3 CLIP（Qwen3VL-32B，**约 60GB**） | `text_encoders/qwen3vl_32b_minimax_h3.safetensors` | 同上 |

> 注：清单里**没有** Z-Image 三件套、没有 Krea-2 三件套、没有 Turbo LoRA/PDD Acc 文件——但这些是资产阶段与渲染的实际依赖（见 §3.2 默认值）。安装器只覆盖"H3 视频主干"，图像资产模型与 LoRA 需另行放置（体检项会报缺，`manju_agent_health.go:336-355`）。

### 3.2 默认配置引用的模型（settings.json 与项目 config.json 两处）

- 全局默认：`internal/config/config.go:162-186`（`Default()`）
- 项目默认：`internal/manju/manju_pipeline.go:8397-8470`（`manjuDefaultConfig`）

| 字段 | 默认值 | 目录（体检/枚举口径） |
|---|---|---|
| `unet_fl2va` | `MiniMax_H3_fl2va_pruned_int8_convrot.safetensors` | `diffusion_models` → `unet`（`manju_pipeline.go:8128`） |
| `unet_ref2va` | `MiniMax_H3_ref2va_pruned_int8_convrot.safetensors` | 同上 |
| `clip` | `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` | `text_encoders` → `clip` |
| `vae_video` | `minimax_h3_video_vae_fp16.safetensors`（2026-09-04 画质升级后默认 fp16） | `vae` |
| `vae_audio` | `minimax_h3_audio_vae_fp32.safetensors` | `vae` |
| `turbo_lora` | `minimax_h3_fl2va_pdd_acc_8step_comfyui.safetensors` | `loras` → `pdd_acc` |
| `turbo_lora_r2v` | `minimax_h3_ref2va_pdd_acc_8step_comfyui.safetensors` | `loras` → `pdd_acc` |
| `ref_image_size` | `max` | — |
| `z_image_unet/_clip/_vae` | `z_image_turbo_bf16.safetensors` / `qwen_3_4b.safetensors` / `ae.safetensors` | `diffusion_models` / `text_encoders` / `vae` |
| `krea2_unet/_clip/_vae` | `krea2_turbo_fp8_scaled.safetensors` / `qwen3vl_4b_fp8_scaled.safetensors` / `qwen_image_vae.safetensors` | 同上 |
| `char_engine` | `krea2`（`:8442`；SDXL 已禁用，`animagine_ckpt=""`、`char_models={}`） | — |

**模型存在性校验**：`manju_agent_health.go:332-368` `missingModels()`——按"主目录 + 备选目录"双查（`diffusion_models|unet`、`text_encoders|clip`、`loras|pdd_acc`），缺失项进体检 `key:"models"` 告警（`:87-90`）；`manju_pipeline.go:8114-8153` `manjuEnvCheck` 同口径逐行 `✅/❌`。模型枚举给前端下拉：`manju.go:1760-1791`（扫 `manjuModelDirs`，`turbo_*` 字段合并 loras+pdd_acc）。

### 3.3 分辨率/帧率/时长

- 默认画幅：`manju_pipeline.go:372-381` → `ctx.w=768`、`ctx.h=1344`（9:16 竖屏）；`ctx.fps=24`（`:382-385`）；随后强制 32 对齐 `:429`。
- 分辨率上限依据（官方）：`docs/漫剧渲染视频技术文档.md:376` `分辨率上限 768×1344(短边 768,32 倍数)`；云端预校验同口径 `manju_upscale.go:115`。
- 时长：`min_shot_seconds` 默认 5（项目）/4（全局），`max_shot_seconds` 默认 7（项目）/12（全局）（`manju_pipeline.go:8409`、`config.go:169-170`）；区间校验 1-15（`manju.go:87-88`）。

### 3.4 帧数换算规则（17k+5）

`internal/manju/manju_comfy.go:70-79`（**生产口径**）：

```go
// h3Length 镜头时长(秒) → 帧数(H3 17k+5 网格 @24fps,与官方模板 ComfyMathExpression 同公式)
// 注意 Go 的 % 对负数取模与 Python 不同,须归一为非负(否则长度会少 17 的倍数)
func h3Length(seconds, fps int) int {
	base := max(5, (seconds*fps+1)/2*2)        // 先归偶
	delta := (5 - base%17) % 17
	if delta < 0 { delta += 17 }
	return base + delta                         // 向上取到 ≡5 (mod 17)
}
```

- 另一套实现 `internal/render/render.go:30-35` `alignFrames`（`for frames%17 != 5 { frames++ }`）与 `durationToFrames`（`:38-44`，下限 5）——同样向上取整，但**没有下限 107/上限 362 约束**。
- 云端 2K 预校验把网格写成显式区间：`manju_upscale.go:121-123` `frames ∈ [107,362] 且 (frames-5)%17==0`（等价的另一个表述：`步长17、≡5 mod 17`）。
- 官方训练区间（文档侧补充）：`docs/漫剧渲染视频技术文档.md:377` `帧数网格 17k+5(n%17==5,训练区间 124-362;音频 latent 40Hz)`，实现收敛为 107~362。
- 示例换算（按默认 24fps）：6s→`base=144`,`delta=14`→**158**；12s→`base=288`,`delta=6`→**294**；15s（max_shot_seconds 上限）→`base=360`,`delta=2`→**362**（正好等于云端上限，与 `fps≤60`+`max_shot_seconds≤15` 的校验组合自洽）。

### 3.5 steps 20 与 Turbo LoRA 8 步的关系

`manju_pipeline.go:403-414`：

```go
ctx.steps = 20
if n, ok := manjuToInt(R["steps"]); ok && n > 0 { ctx.steps = n }
if str(R["turbo_lora"]) != "" {
    if n, ok := manjuToInt(R["turbo_steps"]); ok && n > 0 { ctx.steps = n }
    else { ctx.steps = turboLoRASpecOf(str(R["turbo_lora"])).Steps }
}
```

即：**`steps=20` 是"不挂 Turbo LoRA 时的全步数"**；一旦配了 `turbo_lora`，生效步数改为 `turbo_steps`（默认 8）或按 LoRA 文件名查参数表得到的步数。README `README.md:118` 的表述"20 步（Turbo LoRA 8 步 ≈ 2.9× 提速）"即此语义。

Turbo LoRA 参数表（按文件名识别，数据驱动，`manju_comfy.go:290-332` `turboLoRASpecOf`）：

| 文件名含 | Strength | Sampler | Scheduler | Steps | shift(video/audio) | 适用范围 |
|---|---|---|---|---|---|---|
| `pdd_acc` | 1.0 | euler | simple | **8** | 12 / 3 | PDD 专用节点（`PDD:true`，`cfg=1.0` 强制） |
| `ref2v`+(`turbo`\|`step`) | 1.0 | euler | simple | 4（含 `8step`→8） | 12 / 3 | `R2VOnly`（角色镜） |
| `fl2v`\|`lightx2v`\|`kijai` | 1.0 | euler | simple | 4（含 `8step`→8） | **768p→6/3，否则 12/3** | `FL2VOnly`（空镜） |
| 其他（旧 larryvrh 系 4step EMA 等） | 0.8 | res_multistep | simple | 8 | 不挂 SigmaShift | FL2V/R2V 通用 |

参数依据（`:287-289` 注释）：`euler + 强度 1.0 + MiniMaxH3SigmaShift + simple`，`FL2V 768p 版 shift 6/3(训练分辨率 1344×768,与生产 768×1344 对口),544p/Ref2V 版 12/3`。

### 3.6 seed 策略

- 基线：`config.seed` 默认 **1688**（`manju_pipeline.go:386-389`、`config.go:171`）。
- 每镜独立基线：`seedFor(shotID, attempt)`（`manju_pipeline.go:580-601`）：

```go
if ov := ctx.shotOverrideFor(shotID); ov.Seed != nil { return *ov.Seed + attempt }  // 镜头调试面板按镜覆盖优先
base := ctx.seed + shotID
switch ctx.seedPolicy {
case "increment": return base + attempt
case "random":    if attempt > 0 { return randSeed() }
default:          if attempt > 0 { return base + attempt }   // fixed:重渲也换 seed
}
return base
```

- 设计动机（`:575-579` 注释）：**首渲** `seed + 镜头号`——否则"同 h3_prompt 的两镜共用条件缓存 + 同 seed 必出同画面"（用户实测镜头 4/7 渲染出相同视频）；**重渲** `attempt>0` 时 `fixed` 也 `+attempt`——否则"重渲同画面永远不过质检"的死循环无法打破。
- `seed_policy` 取值 `fixed`（默认）/`increment`/`random`，非法回退 `fixed`（`:415-419`），保存接口校验 `manju.go:743-751`。
- 质检自愈重渲轮次来自侧车文件（跨运行单调）：`manju_pipeline.go:7248-7256` + `loadQcRerunCounts`（`:7762`）。
- 抽卡随机 seed：`manju_comfy.go:550-552` `randSeed()`（`rand.Intn(1<<31-1)`）。

### 3.7 条件缓存与指纹（决定"什么时候必须重编码/重渲"）

- 缓存名：`<项目清洗>_<manjuCacheVer>_c<指纹10位>`（`manju_pipeline.go:5683-5686`）；落盘 `models/conditioning/<name>.pt`（`manju_comfy.go:545-547`）。
- 指纹输入（`manju_pipeline.go:5694-5754`）：`w|h|length|characters|scene|refs|fl2va_end|samp(vae_video,turbo_lora,turbo_lora_r2v)|steps|ref_image_size|prompt`；`refs` 为全部参考图的 `refStamp`（文件名@mtime，`refStamp` 定义 7153）+ 音色音频 `文件名@mtime:size`（`:5732-5736`）。**采样参数在指纹内**，故 §2.3(a) 的画质档升级会自动触发全量 stale 重编重渲（`:445`、`:5741-5743`）。

---

## 4. R2V 参考图注入

### 4.1 上限

| 维度 | 上限 | 证据 |
|---|---|---|
| 角色数 | **3**（超出的角色不进参考图，也不进 `<Picture N>` 编号） | `manju_pipeline.go:6224-6231`（`charRefNames`）、`:8753-8757`（`shotRefViews`） |
| 单角色视图数 | 1 角色=4（front/full/detail/side）；2 角色=各 3（front/full/side）；3 角色=3/2/2 | `manju_pipeline.go:8715-8748` `charViewRels` |
| 参考图总量 | 角色图 + 场景图 ≤ **8 张** | `charViewRels` 注释 `:8719-8720`「4 视图 ≤8 张预算」；最大组合 3+2+2+场景=8 |
| 音色参考 | 该镜每个说话角色一条 `LoadAudio` | `h3EncWorkflow:238-244`、`charVoiceNames:7067` |

### 4.2 chars 字段 → `ref_image_N` 的映射

1. `s.Characters`（方案里的角色 id 列表）按**镜头内登场顺序**遍历，`i` 为索引，`n=min(len,3)`（`charRefNames:6222-6240`）。
2. 每个角色经 `shotViewRelsFor(s, cid, i, n)`（`:6245-6262`）得到视图相对路径列表，规则优先级：
   - 内心戏（`Narration` 含 `内心·`）→ `[front, q]`（Q 版形象优先）；
   - 真身/化形镜（`shotWantsTrueForm`）且有 `_form2.png` 资产 → `[form2]`；
   - 否则 `charViewRels` 视图预算（front 缺失回退主图 `characters/<id>.png`，`:8734-8741`）。
3. 每张图**先复制进 ComfyUI input**，文件名确定性生成：`dir_char_<镜号>_<角色序>_<视图序>.png`（`:6232-6236`）；场景图由 `sceneRefName`（`:7161`）给出 `dir_scene_<镜号>.png`。
4. 拼装顺序 = **全部角色图（按角色序 → 视图序）在前，场景图在最后**，序号从 0 连续编号（`h3EncWorkflow:214-234`）：

```go
var refs []any
for _, cr := range charRefs { refs = append(refs, refOf(wfAdd(wf, "LoadImage", {"image": cr}))) }  // :215-220
if sceneRef != "" { refs = append(refs, refOf(wfAdd(wf, "LoadImage", {"image": sceneRef}))) }        // :221-223
if len(refs) > 0 {
    for i, r := range refs {
        inputs[fmt.Sprintf("ref_images.ref_image_%d", i)] = r   // :232 ← Autogrow 平铺键
    }
}
```

5. 提示词侧的 `<Picture N>` 编号必须与此顺序一一对应，双源同源函数保证一致：`shotRefViews`（`:8768-8790`）与 `charRefNames` 共用 `shotViewRelsFor`（代码注释 `:6216-6219`、`:8787-8789`）。

### 4.3 ⚠️ 关键实现细节：必须用 Autogrow 平铺键，不能传数组

`manju_comfy.go:224-234`（事故记录，权威）：

> `2026-08-24 实测修复:ref_images 必须用 Autogrow 平铺键(ref_image_0/1/2...)。旧代码传数组 []any——ComfyUI 新版(MiniMaxH3ReferenceToVideo 的 ref_images 是 COMFY_AUTOGROW_V3,TemplatePrefix "ref_image_")静默忽略数组,参考图从未编进条件缓存,渲染全部按纯文本生成 → 同一场景镜头画面趋同(用户实测 EP01 六镜几乎一模一样)。实测:数组/嵌套 dict 均只产出 3 tokens 纯文本缓存(63KB);平铺键产出 5120 tokens 含参考图编码(21MB)。补齐场景图后序号从角色图之后继续。`

音色同理用与 `ref_images` **并列**的 `ref_audios.ref_audio_N`（`:235-244`），与提示词里的 `<Audio N+1>` 对应。

### 4.4 `ref_image_size` 参数含义

- 取值二选一：`match` / `max`；非法或未配置 → `max`（`manju_comfy.go:274-283` `h3RefImageSize`；`internal/render/r2v.go:27-31` 同一语义的另一份实现）。
- 语义（`:276-277` 注释，引自官方）：`match = 缩到生成分辨率再编码(快,丢身份细节)`；`max = 保留最高 2048 短边编码(身份保真更强,预编码略慢)`。官方文档明示 `max` 对 identity fidelity 更好——定妆照（1024+）在 `match` 下会被降到 768 再编码，人脸细节折损。
- `max` 自 2026-09-04 起为默认：`config.go:181`、`manju_pipeline.go:8431`，且存量项目由 `normalizeQualityUpgrade` 强制升级（`:482-488`）。

---

## 5. 提交与轮询

### 5.1 提交 `/prompt` 与 `prompt_id`

`internal/comfy/client.go:56-84` `Submit`（生产路径；`backend.ComfyUIClient.SubmitPrompt` 是另一套，见 §5.6）：

```go
body, _ := json.Marshal(map[string]any{"prompt": workflow})          // :57  (不传 client_id)
resp, err := c.HTTP.Post(c.Base+"/prompt", "application/json", …)    // :58
// 连接类错误 → 人话提示                                             // :60-65
if resp.StatusCode != 200 { return "", fmt.Errorf("ComfyUI /prompt HTTP %d: %s", …) }  // :69-71
var r struct { PromptID string `json:"prompt_id"`; Number int; NodeErrors any }         // :72-76
if r.PromptID == "" { return "", fmt.Errorf("ComfyUI 未返回 prompt_id: %s", …) }        // :80-82
```

**注意**：响应里的 `NodeErrors` 被解析但**未检查**（`:75` 声明、`:83` 直接 return `r.PromptID`）——节点校验失败靠 HTTP 400 + 响应体文本暴露。对比 `internal/backend/comfyui.go:104-106` 会显式 `return nil, fmt.Errorf("ComfyUI 节点错误: %v", pr.NodeErrors)`。

提交后**立即落盘检查点**（崩溃恢复核心）：`manju_pipeline.go:7426` `ctx.renderCKSet(ckKey, pid)`；检查点文件 `analysis/<ep>_render_ck.json`，结构 `{shots: {"03": "<prompt_id>", "03@d": "<草稿 prompt_id>"}}`（`manju_render_ck.go:19-23`、`:27-29`；草稿用 `@d` 后缀区分，`manju_pipeline.go:7337-7340`）。

### 5.2 跟踪方式：纯轮询（无 WebSocket）

- 全仓检索 `websocket|ws://`：除 `internal/api/api.go:192-193,283-284` 的 CSP `connect-src` 白名单外，**没有任何 WebSocket 客户端实现**。ComfyUI 的进度事件通道未被使用。
- `internal/comfy/client.go:158-249` `Wait(promptID, timeout, poll, stopped...)` 循环体：
  - `History(promptID)`（`:87-99`，`GET /history/<id>`）取执行记录；`status.status_str=="error"` → 立即 `ComfyUI 任务失败: <异常信息>`（`:206-208`）；`status.completed==true` → 返回 nil（`:209-211`）。
  - history 无记录时用 `InQueue(promptID)`（`:123-148`，读 `/queue` 的 `queue_running`/`queue_pending` 里的 `prompt_id`）判断是否仍在排队。
  - **误判防护**（`:186-190` 注释 + `:213-231` 实现）：接口查询失败 = ComfyUI 忙（加载大模型时 HTTP 会超时）→ `missTicks=0`；只有"ComfyUI 根路径可达（`ComfyReachable`，`:149-157`）且明确不在队列"才累计，累计 **30 次**（轮询 10s ≈ 5min）才判 `ComfyUI 任务丢失(服务不可达或已重启)`。
  - 超时：`time.Now().After(deadline)` → `ComfyUI 等待超时(<timeout>)`（`:232-234`）。
- 实际调用节奏：
  | 场景 | timeout | poll | 证据 |
  |---|---|---|---|
  | 渲染等待 | 3600s | 10s | `manju_pipeline.go:7429` |
  | 预编码等待 | 1800s | 10s | `manju_pipeline.go:5857` |
  | 崩溃收回等待 | 3600s | 10s | `manju_render_ck.go:102` |
  | 参考图生成（`render` 包） | 15min 固定 | 3s | `internal/render/asset.go:39-42,72` |
  | 镜头渲染（`render` 包） | 15min 固定 | 5s | `internal/render/render.go:90,123` |
- 停止感知：`Wait` 的 `stopped` 回调每 ≤500ms 检查一次（`client.go:193-197,236-247`）；首次触发时**主动 `POST /interrupt`**（`:167-181`），避免"仅返回已停止但 ComfyUI 继续跑完当前任务白烧 GPU"。

### 5.3 产物取回

`manju_pipeline.go:7437-7445`：`History(pid)` → `comfyOutputVideo(entry)` 遍历所有节点输出找 `video` 键，找不到再回退 `images` 键（`manju_comfy.go:31-58`，注释说明 `SaveVideo` 在 history 里以 `images` 上报）→ 从 `ctx.comfyOutput` 复制到 `clips/<ep>/NN.mp4`。草稿目录产物**不入时效清单**（`:7446-7448`，`manifestMark` 仅定稿）。

### 5.4 错误分类

| 层 | 函数 | 分类 |
|---|---|---|
| ComfyUI 执行错误文本 | `client.go:251-267` `ComfyErrMsg` | 从 `status.messages[].data.exception_message` 提取，兜底 `status.exception_message`，再兜底"未知错误"，截断 300 字 |
| 提交期 | `client.go:59-71` | 连接被拒/`connectex`/`no such host` → `ComfyUI 未就绪(连接失败)——已尝试自动启动…`；非 200 → `HTTP <code>: <body>` |
| 阶段级诊断 | `manju_agent_health.go:886-906` `manjuDiagnoseError` | 关键词 → 结论 + 建议：`safetensors/checkpoint/not found`→模型缺失；`out of memory/cuda out/oom`→显存不足；`connection refused/connect/comfy`→未连通；`401/unauthorized`→Key 无效；`timeout`→超时；`json/parse`→LLM 输出异常；其余→未知错误 |
| 预编码落盘校验 | `manju_pipeline.go:5860-5865` | 任务 success 但 `.pt` 未落盘 → `疑似 ComfyUI 节点缓存短路,请重启 ComfyUI 后重试`（防静默继续，把问题推迟到渲染阶段白烧一次） |

### 5.5 OOM 处理

**没有自动降分辨率/自动重试**（检索确认：`manju_pipeline.go` 中 `retry` 仅出现在 LLM 生成与提示词修复路径，渲染失败即停，`manju_pipeline.go:7430-7435` 注释明确"旧逻辑『中断自动重试一次』会在 ComfyUI 异常/外部中断时静默重新提交,用户感知为『异常了还在自动烧 GPU』(升级优化:异常即停,不自动重试)"）。OOM 的实际应对是**预防性卸载**（§9）+ 诊断建议文案（`manju_agent_health.go:892-893`：`ComfyUI 面板 /free 释放显存,或降低画幅、分阶段渲染;小显存建议 768×1344 以下`）。

### 5.6 两套客户端的差异（易踩坑）

| | `internal/comfy/client.go`（manju 生产路径） | `internal/backend/comfyui.go`（`render`/`api` 路径） |
|---|---|---|
| 提交体 | `{"prompt": wf}`（无 `client_id`） | `{"prompt": wf, "client_id": "NiliX"}`（`:91`） |
| `node_errors` | 解析但不检查 | 检查并报错（`:104-106`） |
| 在线探测 | `Online()` 读 `/system_stats` 的 `system.comfyui_version`（`:37-53`） | `SystemStats()` 返回版本 + `devices[].vram_total/vram_free`（`:14-27`） |
| 超时 | 默认 30s；`Wait` 由调用方给 | 默认 15s；下载 5min（`:71-72`） |
| `/system_stats` 的 VRAM 用途 | — | 仅 `/api/test` 展示（`internal/api/api.go:373-378`），**不参与任何自适应决策** |

`internal/api/api.go:373-378` 是全仓唯一读取 `/system_stats` 的地方（除 `client.Online()` 只取版本号）：

```go
cui := backend.NewComfyUIClient(cfg.Render.ComfyURL)
if ss, err := cui.SystemStats(ctx); err != nil { res.ComfyUI = testItem{OK:false, Message: err.Error()} }
else { res.ComfyUI = testItem{OK:true, Message: "在线，ComfyUI " + ss.System.ComfyUIVersion} }
```

---

## 6. 分辨率档位与草稿预审

### 6.1 档位表与换算

`internal/manju/manju_comfy.go:334-358`：

```go
var manjuResTiers = map[string]int{"draft": 416, "standard": 768, "fhd": 1088}   // :337
func manjuAlign32(n int) int { if n < 32 { return 32 }; return (n + 16) / 32 * 32 }  // :340-345  四舍五入到 32 倍数
func manjuResTierDims(tier string, w, h int) (int, int, bool) {                    // :349-358
    short, ok := manjuResTiers[tier]; if !ok || w <= 0 || h <= 0 { return w, h, false }
    if w <= h { return manjuAlign32(short), manjuAlign32(h * short / w), true }     // 竖屏:短边=宽
    return manjuAlign32(w * short / h), manjuAlign32(short), true
}
```

应用点：`manju_pipeline.go:420-426`（`res_tier` 非 `custom`/空时覆盖手动宽高）；随后**手动/custom 宽高也强制 32 对齐**（`:427-429`，注释引审计 S10：未对齐会让 `EmptyMiniMaxH3LatentAV` 报 400 或产出破损）。保存接口校验合法值 `manju.go:726-735`（非法返回 `res_tier 非法(可选 draft/standard/fhd 或留空手动)`）。

按默认 768×1344 竖屏换算的实际档位：

| tier | 短边 | 计算 | 实际宽×高 | 面积比（相对 standard） | 用途/结论 |
|---|---|---|---|---|---|
| `draft` | 416 | w=416；h=align32(1344×416/768)=align32(728)=**736** | **416×736** | 0.297（≈1/3.4） | 快速试片/预告；`manju_agent.go:629` 称"约 1/3 像素量" |
| `standard` | 768 | — | **768×1344** | 1.0 | 默认；本地模型原生最优档（官方区域上限） |
| `fhd` | 1088 | w=1088；h=align32(1344×1088/768)=align32(1904)=**1920** | **1088×1920** | 2.02 | 文档 `:129` 明示"超上限约 2 倍,慢 4 倍且质量不可控,仅实验位；更高清晰度走云端 2K" |

### 6.2 草稿预审（`draft_judge`）如何省成本

只在 **AI 一条龙（Agent）模式且视觉模型就绪**时启用：`manju_agent.go:1363` `draftMode := acfg.VisionReady() && ctx.draftJudge`；普通六阶段管线不启用。

- 草稿分辨率：`manju_pipeline.go:1893-1904` `draftDims()` = 定稿宽高 × `draftScale`（默认 **0.5**，可配 0.2–0.95，`manju_pipeline.go:430-433`），再 32 对齐。注释即成本论证：
  > `判分与分辨率弱相关,0.5 缩放的像素量约为定稿 1/4,审片返工轮 GPU 时间等比下降`（`:1894-1896`）
- 草稿目录：`clips/<ep>/_draft`（`:1906-1908`），与定稿同集隔离，合成/集清单不读（`manju.go:2447` 跳过 `_draft`/`2k`）。
- 流程：审片返工轮全部在草稿分辨率迭代（`manju_agent.go:1366-1370`，`dst := judgeDir/NN.mp4`，`:1431`）；返工轮重渲按该镜返工序号换 seed（`:1449-1455`）；某镜一旦通过就不再重渲。全部落定后进入**定稿轮**：以审定后的提示词按全集顺序全分辨率重渲，接缝（MotionContext）按全集顺序保持（`:1591-1602`），已有定稿产物跳过（`:1636-1640`），完成后删草稿目录与草稿条件缓存（`:1650-1654`）。
- 缓存放大：草稿/定稿**各自独立条件缓存**（缓存名含 `w|h|length` 指纹，`manju_pipeline.go:5683-5686`、`:5744-5748`），互不挤占（`:5682` 注释）；定稿轮渲完一镜即删对应草稿 `.pt`（`:1651`）。
- 与档位的关系：`draft_scale`（草稿预审缩放）与 `res_tier=draft`（416 短边档位）是**两个独立机制**——前者是"定稿画幅的百分比"，后者是"直接按短边像素定档"，代码里没有互相引用。
- 前端联动与体检：`manju_agent.go:628-684`（AI 规划参数 `draft_judge`/`res_tier`/`seed_policy`）；`manju_agent_health.go:177-183`（开了草稿预审但没配视觉模型 → warn"草稿预审不会生效"）。

---

## 7. 自包含 ComfyUI 部署

### 7.1 一键安装流程（`internal/comfy/comfy_install.go:211-353` `installComfyUI`）

单实例防重（`:213-225`，`running` 时返回 `安装已在运行中`）；后台 goroutine + 状态机 `step ∈ portable/tool/node/model/media/done`（`:24-35`、`:93-97`）；日志双写内存与 `logs/comfy_install.log`（`:78-91`）。

| 步骤 | 行为 | 代码 |
|---|---|---|
| 入口校验 | 若 `<ComfyRootDir>/main.py` 已存在 → 拒绝（`{"ok":false,"error":"ComfyUI 已存在(如需重装请先改路径)"}`） | `:373-383` |
| 0 | 若 `ComfyRootDir` 目录已存在 → 跳过整个"程序部分" | `:256-257, 318-320` |
| 1 | 下载 `7zr.exe` 到临时目录 | `:259-265`（`tmpDir = <ComfyRootDir>/../_install_tmp`，`:249-254`） |
| 2 | 下载 `ComfyUI_windows_portable_nvidia.7z`（约 1.5GB） | `:266-278` |
| 3 | **完整性校验**：`7zr t <arc> -y -bso0 -bsp0`，失败即中止 | `:279-288`（注释 `:279-281`：下载中断/损坏直接解压会静默产出残缺程序；`下载源校验和待发布方提供后填入 downloadFile 的 sha256 参数` → **当前无哈希校验**） |
| 4 | `7zr x <arc> -o<stage> -y -bso0 -bsp0` 解压 | `:289-300` |
| 5 | 定位 `stage/ComfyUI_windows_portable/ComfyUI`（校验含 `main.py`）→ `os.Rename` 到 `ComfyRootDir`，跨卷失败回退 `copyTree` | `:301-317` |
| 6 | 逐个 `git clone --depth 1` 自定义节点（KJNodes、MiniMaxH3-Easy）到 `<ComfyRoot>/custom_nodes/`；已存在 `.git` 即跳过；失败仅告警不中断 | `:322-330`、`gitClone:176-191` |
| 7 | 按清单模型逐个下载到 `<ComfyShared>/models/<Rel>`（模型基址 + `resolve/main/...`）；单项失败仅告警"可稍后重试/改镜像" | `:332-342`、`downloadFile:100-173` |
| 8 | `pip install --no-input -q av faster-whisper pyJianYingDraft`（用 ComfyUI 的 venv python；失败不阻塞） | `:193-209, 344-347` |
| 9 | 收尾写 `🎉 ComfyUI 安装流程结束,点「启动」即可开始使用`，置 `step=done` | `:349`、`:232-248` |
| 停止 | `POST /api/comfy/install/stop` 置 `cancelled=true`，下载/克隆循环内检查并 `.part` 清理；收尾不覆盖用户停止终态 | `:390-403`、`downloadFile:132-141`、`:236-239` |

### 7.2 portable 下载与"断点续传"

`downloadFile`（`:100-173`）实际语义：

- **跳过**：目标文件存在且 `Size()>0` → `⏭ 已存在,跳过`（`:101-104`）——这是实际意义上的"续传"。
- **无 HTTP Range 续传**：`http.NewRequest("GET", url, nil)` 无 `Range` 头（`:110`）；中途失败会 `os.Remove(tmp)` 丢弃（`:160-162`），**下次从头再下**。
- **原子落盘**：写 `<dst>.part` → 成功后 `os.Rename`（`:124, 168-170`），防半成品被当成品。
- 超时 2 小时（`:115`）；每 256KB 检查取消（`:130-141`）；每 8MB 刷新 UI 进度 `"<label> · <N>MB"`（`:149-153`）；`User-Agent: NiliX-Installer/1.0`（`:114`）。
- 镜像可切：`comfyModelBase`（`:50-52`，注释"国内可切 https://hf-mirror.com；设置里未提供开关,直接改此变量或后续接入配置"）。

### 7.3 模型清单校验（安装后）

- 安装阶段**没有**模型级校验（只对 portable 压缩包做 `7z t`，`:282-288`）；模型失败只记日志。
- 运行期校验在项目侧：`manju_agent_health.go:332-368` `missingModels()`（体检项 `models`）+ `manju_pipeline.go:8114-8153` `manjuEnvCheck` 的 `🧠 H3 模型:` 逐行 `✅/❌`（含备选目录）——两者是"清单校验"的真实落点。
- 清单展示：`comfy_versions.go:78-214` `/api/comfy/versions` 按 `comfyModelDirs`（`:24-37`，含 `pdd_acc`）分组列出文件名/大小/时间 + ComfyUI 版本 + 插件 git 状态 + 工作流清单；`/api/comfy/plugins/check`（`:246-295`）逐插件 `git fetch` 统计落后提交（20s 超时、`HideWindow` 防黑窗），官方最新版走 GitHub releases（`:297-322`，10min 缓存、剥 `v` 前缀）。

### 7.4 启动参数（端口 / UTF-8 环境变量）

`internal/comfy/comfy.go:191-270` `startComfy`：

```go
py := filepath.Join(paths.ComfyRootDir, ".venv", "Scripts", "python.exe")   // :196 必须 python.exe 不能用 pythonw.exe
args := []string{
    filepath.Join(paths.ComfyRootDir, "main.py"),
    "--listen", comfyListenAddr(),        // :242 默认 127.0.0.1；settings.render.lan_access=true → 0.0.0.0
    "--port", port,                       // :243
    "--disable-auto-launch",              // :244
    "--output-directory", out,            // :245
    "--input-directory", in,              // :246
}
cmd.Dir = paths.ComfyRootDir                                            // :249
cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000}  // :250 CREATE_NO_WINDOW
cmd.Env = append(os.Environ(), "PYTHONIOENCODING=utf-8", "PYTHONUNBUFFERED=1")        // :254
cmd.Env = append(cmd.Env, comfySelfHeal()...)                                          // :255
```

- **端口自动避让**（`:208-239`）：`8190` 被占用时先探测 `8190-8209` 是否已有可用 ComfyUI（`probeComfyURL`，`:276-284`，HTTP 可达即复用，避免多实例抢 DB 锁）；否则取第一个空闲端口并 `SetComfyParams` 让全链路（探测/停止/HUD/项目 config）跟随。默认端口 `http://127.0.0.1:8190`（`:25`）。
- **UTF-8 环境变量**是关键修复（`:251-253` 注释）：`中文 Windows 默认 ANSI 编码(GBK):重定向 stdout 后 Python 打印 emoji 会抛 UnicodeEncodeError 直接崩溃(实测 ComfyUI 启动秒死、日志全空的根因)`。日志落 `logs/comfy.log`（`O_APPEND`，`:256-262`）。
- 启动参数单一数据源：`comfyParamsVal atomic.Pointer`（`:40-42`），由 main 用 settings.json 注入（`SetComfyParams:68-80`），HUD 卡片/Comfy 页/实际启动命令共用；`lan_access` 默认 false（`:44-57`，安全默认，防局域网无鉴权访问）。
- **自愈（每次启动幂等执行）** `comfy_selfcontain.go:22-30` `comfySelfHeal`：
  1. `extra_model_paths.yaml` 的 `base_path` 重写为当前 `ComfySharedDir`；文件缺失/无 `base_path` 时按 28 类子目录（含 2026-09-04 新增 `pdd_acc`）整体重建（`:34-80`）。
  2. `.venv/pyvenv.cfg` 的 `home` 解析失败时重写为 `<ComfyRootDir>/../standalone-env`（自包含旁置 base python，`:85-135`）。
  3. `models/conditioning` 存在则注入 `NILIX_COND_CACHE=<该目录>`（`:26-29`）。
- 停止与孤儿清扫：优先按本服务启动的 PID `taskkill /PID <pid> /T /F`（`:287-296`，注释 H4：防误杀同端口第三方进程）；兜底按端口找 PID；最后用 PowerShell `Get-CimInstance Win32_Process` 只杀命令行含本程序 ComfyUI 路径的 `python.exe`（`:316-324`，绝不误杀用户自装的 Comfy Desktop）。
- 在线判定刻意不用 HTTP：`ComfyOnline()`（`:343-350`）用"端口有监听进程"判在线（注释 `:339-342`：冷启动加载模型 10-60s 期间 HTTP 不响应，用 HTTP 会把"正在启动"误判为"未运行"→ 反复拉起 → 多实例抢 db 锁 + 黑窗闪）；`ComfyPortPID()`（`:354-356`）供托盘三色灯；`ComfyBusy()`（`:360-376`）读 `/queue` 判忙闲。
- 路由：`comfy.go:419-428`（`GET /api/comfy`、`POST /api/comfy/start|stop`、`POST /api/comfy/install/start`、`GET /api/comfy/install/status`、`POST /api/comfy/install/stop`、`GET /api/comfy/versions`、`POST /api/comfy/plugins/check`）。

---

## 8. 云端 2K（MiniMax `/v2/video_regeneration`）

文件：`internal/manju/manju_upscale.go`（设计意图见文件头 `:3-7`：本地 768×1344/24fps/17k+5 产物"恰好满足云端重生成接口的全部预校验"，本地 GPU 零负担升 2K）。

### 8.1 常量与鉴权

| 项 | 值 | 证据 |
|---|---|---|
| 默认 base | `https://api.minimax.io`（注释 `:22` 称国内平台为 `api.minimaxi.com`，可用 `render.minimax_base_url` 覆盖） | `:73-77`、`:149-154` |
| 模型 / 分辨率 | `MiniMax-H3` / `2K` | `:75-76` |
| Key 链 | 项目 `render.minimax_api_key` → `server/settings.json`（`manjuSettingsFile`）→ 环境变量 `MINIMAX_API_KEY` | `:133-147` |
| 单价 | `0.80 元/秒`（仅预估展示，数据驱动） | `:23-26`、`:65-66` |

### 8.2 预校验（付费前 fail-loud）

`manjuValidate2K`（`:107-131`）——本地 `probeVideo`（调 venv PyAV 的 `probe` 子命令，`:89-105`）拿到 `width/height/fps/frames/hasAudio/sizeBytes` 后逐条判：

| # | 规则 | 违规文案 |
|---|---|---|
| 1 | `width%32==0 && height%32==0` | `宽高须被 32 整除(WxH)` |
| 2 | `width*height ≤ 768*1344` | `面积超上限(WxH > 768x1344)` |
| 3 | `fps == 24`（恰好相等） | `帧率须恰好 24fps(实际 %.3f)` |
| 4 | `107 ≤ frames ≤ 362 && (frames-5)%17==0` | `帧数 N 不在允许网格(107~362,步长17,≡5 mod 17)` |
| 5 | 含音轨 | `无音轨`（H3 原生对白是升格保真的前提） |
| 6 | `sizeBytes ≤ 50MB` | `文件 %.1fMB 超 50MB 上限` |

### 8.3 提交 / 轮询 / 下载

- **提交** `manjuUpscaleSubmit`（`:156-199`）：`POST {base}/v2/video_regeneration`，`Authorization: Bearer <key>`，payload：

```json
{"model":"MiniMax-H3","resolution":"2K","content":[
  {"type":"text","text":"<镜头提示词，空则用默认升格提示词>"},
  {"type":"video_url","video_url":{"url":"data:video/mp4;base64,<本地 mp4>"},"role":"base_video"}]}
```

  默认提示词（`:162-164`）：`Upscale the base video to high resolution. Keep all motion, camera work, speech and audio exactly identical to the base video.` 响应取 `task_id`，兜底 `id`（`:191-198`）。
- **轮询** `manjuUpscalePoll`（`:201-251`）：`GET {base}/v2/query/video_generation/<task_id>`，每 **10s**，超时 **30min**（`:307`）；状态 `succeeded|success|done` → 取 `task.content.url`；`failed|cancel|cancelled` → 报 `task.error.message`；**下载地址强制 https**（回环 `127.0.0.1`/`localhost` 豁免，`:236-239`，防 SSRF/明文）。
- **下载** `manjuUpscaleDownload`（`:253-278`）：流式写 `<dst>.part` → `os.Rename` 原子落盘；客户端超时 10min（`:311`）。
- **单镜编排** `manjuUpscaleOne`（`:285-317`）：已存在 2K 产物即跳过（续跑幂等）→ 探测 → 校验 → 提交 → 轮询 → 下载到 `clips/<ep>/2k/NN.mp4`（`manjuUpscale2kDir:280-283`）。
- **整集编排** `manjuUpscaleRun`（`:319-422`）：`safeGo` panic 兜底；已有任务运行中返回 409；镜头集合 = 参数 `shots`（`"3"`/`"1,3"`）或本集全部顶层 `.mp4`；逐镜失败不中断（`failed++` 汇总），停止后已完成保留可续跑；提示词取方案里该镜的 `h3_prompt`（`:366-375`）。
- **路由**：`registerUpscaleRoutes`（`:425-468`）——`GET /api/manju/upscale2k/estimate`（`?config=&episode=&shots=` → `manjuUpscaleEstimate:27-67`：按方案时长求和或按目录 mp4 数 × 8s 估算 → 返回 `{shots, durationSec, costCNY}`）、`POST /api/manju/upscale2k`、`POST /api/manju/jianying`（剪映草稿导出，`:470-529`，依赖 venv 的 `pyJianYingDraft`）。

> 注：估算接口只返回 `{shots, durationSec, costCNY}`（`:66`），**不返回预校验结果**；真正的 2K 预校验只在 `manjuUpscaleOne` 提交前执行（付费前 fail-loud）。

---

## 9. 显存 / 加速

### 9.1 SageAttention

见 §2.2。补充：写入的是 `PatchSageAttentionKJ` / `PathchSageAttentionKJ`，参数 `sage_attention="auto"`、`allow_compile=false`（`manju_comfy.go:449-451`）；启动器把 KJNodes 列为可自动安装节点（`comfy_install.go:61-62`，"ComfyUI-KJNodes(SageAttention)"）。

### 9.2 显存管理：只有 `/free`，没有 LowVRAM/offload

- 全仓检索 `lowvram|low_vram|offload|reserve_vram|--lowvram`：**零命中**（`internal/**/*.go`）。启动参数里也没有 `--lowvram/--normalvram/--highvram/--reserve-vram`（`comfy.go:240-247` 是全部参数）。
- 显存靠 `freeComfyModels`（`manju_pipeline.go:189-213`）：

```go
body := bytes.NewBufferString(`{"unload_models": true, "free_memory": true}`)   // :194
req, _ := http.NewRequest("POST", ctx.comfy.Base+"/free", body)                // :195
… time.Sleep(2 * time.Second)                                                  // :209 等卸载完成
```

  三处触发：
  | 时机 | 证据 | 注释里的因果 |
  |---|---|---|
  | 预编码阶段开始（assets 之后） | `manju_pipeline.go:5877`（`stageEncode`） | `assets 阶段加载的 ZImage(7.6G)+Lumina2(11.7G) 常驻显存,而 H3 预编码需要加载 Qwen3-VL 32B(14.6G)——RTX 5090 24G 装不下两者,编码任务提交后 ComfyUI 加载阻塞(显存不足),NiliX 侧 wait 挂起、GPU 无动静(本 BUG 根因)`（`:190-192`） |
  | Agent 渲染前 | `manju_agent.go:1341` | 同上语义（`:1339-1340`） |
  | 渲染完成空闲 N 分钟 | `manju_pipeline.go:2062-2080` `scheduleIdleFree` | `idle_free_minutes` 默认 10（`:2063-2066`，0=关）；到点先 `QueueBusy()`（`client.go:106-121`）确认无 running/pending 才释放（`:2071-2079`） |
- 约束读取（未用于决策）：`backend/comfyui.go:15-27` 解析 `devices[].vram_total/vram_free`，唯一使用点是 `/api/test` 的展示文案（`api.go:373-378`）。
- 4bit/量化路径：CLIP 默认名 `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors`（NVFP4+AWQ，`config.go:174`）与 UNet `*_pruned_int8_convrot`（`comfy_install.go:65-68`）属"模型权重层面省显存"，不是运行时 offload。
- 其他降级开关：`fl2va_end_frame`（空镜双帧，默认关，场景图成本翻倍，`manju.go:104-108`）、`draft_judge`（§6.2）、`shots_per_take`（多切点长镜，1-3）。

---

## 10. 服务管控与前端可见面（补充）

- ComfyUI 页/灵动岛依赖的 `ServiceStatus`（含 `online/title/url/pid/logPath/logTail/startup`，`comfy.go:86-107`）：`probeComfy`（`:143-163`）900ms 超时探测根路径并正则抓 `<title>`（`:156-158`）；`tailFile` 读末 4KB（`:127-141`）并剥 ANSI（`:31,331`）；日志兜底 `C:\Mi\Ai\Comfy Desktop\logs\_comfyui_server.log`（`:27,326-332`）。
- `StartupInfo`（`:99-107`）把 URL/Root/Shared/Input/Output/Python/Port 统一输出，HUD、Comfy 页、启动命令同源（`:174-189` `currentStartup`）。
- 渲染空闲/忙碌：`ComfyBusy`（`:360-376`）与 `Container.QueueBusy`（`client.go:106-121`）两处实现同一语义。

---

## 11. 不一致与风险清单（对"在 Linux 上复刻该管线"最相关的部分）

### 11.1 `HasNode` 判据自相矛盾（优先级：高）

`comfy/client.go:26` 断言 `/object_info/<name>` "200=有"，实现只看状态码（`:34`）；但 `manju_comfy.go:254-255` 明确记录"ComfyUI 对缺失节点 `/object_info` 也返回 200 空对象"。两处冲突且**实现不解析响应体**。若后者为真，`sageAttnGuard` 恒命中列表首个名字 `PathchSageAttentionKJ`、`pddGuard` 节点判定恒真，SageAttn/PDD 的"自动降级"形同虚设（只在 ComfyUI 完全不可达时触发）。**建议**：探测改为解析 JSON 体（键存在且值非 null/非空）再做判定。本报告未联网验证 ComfyUI 服务端该路由的真实状态码，标为待复核。

### 11.2 两套客户端/两套工作流构建器并存，参考图键名不一致（优先级：高）

- 生产路径（`internal/manju`）用 `ref_images.ref_image_%d`（Autogrow 平铺键，`manju_comfy.go:232`）。
- `internal/render/r2v.go:24` 用 `ref_image_%d`（无 `ref_images.` 前缀），且 `internal/render/*` 的 `fmt.Sprintf("10%d", i)` 生成的是**字符串型**节点 ID（`r2v.go:22`）——一旦这套代码被复用，参考图会被 Autogrow 节点静默忽略（同 §4.3 的 63KB/3 tokens 事故）。
- 超时/轮询策略也不同（15min 硬超时 vs 3600s；3s/5s vs 10s；无 `/interrupt` 停止感知）。
- `internal/render` 还带一套 `assemble/verify/storyboard` 依赖（`render/manager.go:11-15`），与 manju 六阶段管线是**两条并行且未合并的实现**。

### 11.3 安装器清单与默认配置的模型名不一致（优先级：高，Windows 免疫 / Linux 致命）

| 安装器落盘名（`comfy_install.go:65-74`） | 默认配置名（`config.go:172-174`、`manju_pipeline.go:8417-8419`） | 差异 |
|---|---|---|
| `minimax_h3_fl2va_pruned_int8_convrot.safetensors` | `MiniMax_H3_fl2va_pruned_int8_convrot.safetensors` | **大小写**（`MiniMax_H3` vs `minimax_h3`） |
| `minimax_h3_ref2va_pruned_int8_convrot.safetensors` | `MiniMax_H3_ref2va_pruned_int8_convrot.safetensors` | 同上 |
| `qwen3vl_32b_minimax_h3.safetensors`（**约 60GB 全精度**） | `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors`（**15.7GB 量化**） | 不是同一文件，安装器下的量体在默认配置里查不到 |
| VAE 两个文件 | 一致（`fp16` / `fp32`） | — |

Windows NTFS 大小写不敏感 → 只暴露 CLIP 那一条；**Linux 上大小写敏感** → 即便按清单装好，默认配置也会报"模型缺失"（对照 `reports/00-environment-recon.md:55-60` 已在本机实测同类问题）。`missingModels` 只做 `fileExists`（`manju_agent_health.go:363`），不做大小写归一/模糊匹配。

### 11.4 安装流程的完整性保障不足（优先级：中）

- 无 sha256/ETag 校验（`comfy_install.go:279-281` 自述待补）。
- 无 HTTP Range 续传，失败即删 `.part` 重下（`:110, 160-162`）——60GB CLIP 在弱网下体验差。
- 模型下载失败不阻断安装（`:339-341`），安装"成功"不代表可用，只能靠事后体检。
- `installComfyUI` 只在目录已存在时拒绝重装（`:374-377`），没有"修复/补装缺失模型"入口。

### 11.5 无 WebSocket、无进度事件（优先级：中）

`Wait` 只能给出"排队中/执行中/完成/失败"，没有 ComfyUI 的 `progress`/`executing` 节点级进度，日志只有 `渲染提交 <pid 前8位>...`（`manju_pipeline.go:7427`）与完成耗时（`:7449`）。对长任务（4s 镜 30min 级）的可观测性靠经验预估文案（`stageEncode:5887-5902`、`manjuEncEstMin:5921-5926` 已从旧版"10min+外推"校正为固定保守 4 分钟）。

### 11.6 `h3Length` 无上限钳制（优先级：低，靠配置校验兜住）

`h3Length` 不含 `107~362` 区间钳制（`manju_comfy.go:72-79`），云端校验却要求 ≤362（`manju_upscale.go:121`）。当前靠 `fps ≤ 60` + `max_shot_seconds ≤ 15`（`manju.go:85-89`）在 24fps 下恰好收敛到 362；若放开时长/fps 校验，本地与云端网格会脱钩。`internal/render/render.go:30-35` 更宽松（下限 5 帧），产出的短片段一定过不了云端预校验。

### 11.7 文档与代码的偏差（供交叉阅读时警惕）

`docs/漫剧渲染视频技术文档.md` 自述基于 2026-08-20 代码，与现状的关键出入：

| 文档说法 | 代码现状 |
|---|---|
| `:38-39` 主逻辑在 `internal/api/manju*.go`、媒体脚本在 `internal/api/scripts/manju_media.py` | 已拆包为 `internal/manju/*`、`internal/comfy/*`、`internal/render/*`；`scripts/manju_media.py` 仍在（`README.md:77`） |
| `:102` `real` 风格用 Z-Image，其余 SDXL 按性别 | SDXL 全面禁用，`char_engine` 仅 `zimage`/`krea2` 且默认 `krea2`（`manju.go:738-748`、`manju_pipeline.go:8442`） |
| `:115` 参考图"最多 3 个"（角色） | 一致，但需补"每角色多视图，总量 ≤8"（`manju_pipeline.go:8719-8748`） |
| `:124` 提交后"中断自动重试一次" | 已改为**异常即停不自动重试**（`manju_pipeline.go:7430-7435`） |
| `:129` 默认 int8 VAE + 4step LoRA | 默认 fp16 VAE + PDD Acc 8step + `ref_image_size=max`（`config.go:177-181`、`normalizeQualityUpgrade`） |
| `:303` 布尔字段仅 `sage_attention / draft_judge` | 现有 6 个：`sage_attention/draft_judge/fl2va_end_frame/subtitle/voiceover/defreeze`（`manju.go:110`） |
| `:307` 默认模型含 SDXL 男女 checkpoint | 已清空（`config.go:185`、`manju_pipeline.go:8445-8446`） |
| `:377` 帧数区间 `124-362` | 云端校验用 `107-362`（`manju_upscale.go:121`） |

---

## 12. 关键文件索引

| 关注点 | 文件 |
|---|---|
| ComfyUI 进程管控/启动参数/端口避让/日志 | `internal/comfy/comfy.go` |
| ComfyUI HTTP 客户端（提交/历史/队列/轮询/停止中断） | `internal/comfy/client.go` |
| 一键安装编排 + 资源清单 + 下载器 | `internal/comfy/comfy_install.go` |
| 自包含自愈（extra_model_paths / pyvenv / 条件缓存目录） | `internal/comfy/comfy_selfcontain.go` |
| 版本/模型/插件/工作流清单 + PDD 状态 | `internal/comfy/comfy_versions.go` |
| 工作流构建（预编码/渲染/图像）+ 档位表 + 帧数公式 + Turbo LoRA 参数表 | `internal/manju/manju_comfy.go` |
| 管线上下文/守卫（sage/pdd）/seed/档位/草稿尺寸/预编码/渲染/体检/默认配置 | `internal/manju/manju_pipeline.go` |
| 渲染检查点与崩溃收回 | `internal/manju/manju_render_ck.go` |
| 云端 2K / 剪映导出 / 费用预估 | `internal/manju/manju_upscale.go` |
| Agent 草稿预审与定稿轮 | `internal/manju/manju_agent.go` |
| 模型缺失体检 / 错误诊断 | `internal/manju/manju_agent_health.go` |
| 单镜工作流可视化（节点/连线/参数，可作节点清单交叉验证） | `internal/manju/manju_shot_debug.go` |
| 设置存储（AES-GCM Key、原子写、损坏自愈）与 `RenderSettings` 默认值 | `internal/config/config.go` |
| 第二套 ComfyUI 客户端（`/system_stats`、node_errors 检查）+ LLM 客户端 | `internal/backend/comfyui.go`、`internal/backend/llm.go` |
| 第二套 H3 工作流（T2V/R2V/T2I）与任务管理 | `internal/render/render.go`、`r2v.go`、`asset.go`、`manager.go` |
| 渲染技术文档（含官方对齐表，注意 §11.7 的时效偏差） | `docs/漫剧渲染视频技术文档.md` |
