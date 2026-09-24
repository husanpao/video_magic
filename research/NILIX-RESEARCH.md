# NiliX 深度调研报告

> 调研对象：`NiliX-main.zip`（用户提供的权威最新版，HEAD 提交 6a382e4 / 2026-09-06）
> 调研方式：源码逐文件精读 + 作者自带 32 篇文档交叉验证 + 本机实测日志溯源
> 调研日期：2026-09-23

---

## 一、结论摘要

**一句话**：NiliX 不是一个"脚本集合"，而是一个**已完成产品化的本地 AI 漫剧生产工作台**——
Go 单体服务（58,258 行 / 243 文件）+ 原生 JS 前端（18,688 行）+ 116 个测试文件（15,581 行），
把「小说 → 分镜 → 定妆照 → 逐镜视频 → 质检 → 成片」整条链路做进了**一个可断点续跑的幂等流水线**。

**三个最重要的判断**：

1. **它的价值 90% 在"工程化的坑"上，不在"功能列表"上。** NiliX 自己写了一份 166 行的全面审计报告，
   列出 10 个严重 + 17 个高 + 19 个中缺陷（静默吞错、指纹漏参数、接缝 latent 跨项目污染、
   判分失败被当不合格空转烧钱……），**并逐条修复归档**。这份"踩坑 + 修复"记录才是真正稀缺的资产。

2. **源码是 Windows 专用的，但移植已经实证可行——不是推测。** 平台绑定集中在
   `main.go` / `island`（WebView2 桌面壳）/ `sysmon`（雷神 EC 超频）/ `autostart`（注册表）
   这几个**外围包**；占 76% 代码量的 `internal/manju`（44,143 行）是纯业务逻辑。
   调研中已经**真的构建并运行了一个 headless Linux 版 NiliX**
   （32.5MB ELF，`/api/stats` 返回真实 GPU 指标），
   阻断点共 **24 文件 / 66 处**，headless 档约 **30 文件 / 700–900 行**。
   **移植成本远低于重写成本。**


3. **它的默认策略与本机现状存在正面冲突**：NiliX 当前默认是「质量档」
   （PDD Acc 8step LoRA + fp16 VAE + `ref_image_size=max`），而本机实测基准是「提速档」
   （4/8 步 fl2v Turbo LoRA）；且本机缺 Ref2VA 权重、Z-Image/SDXL 资产模型与 PDD-Acc 节点。
   接入前必须先做这个取舍。

---

## 二、项目画像

| 维度 | 实测值 |
| --- | --- |
| 语言 / 构建 | Go 1.25，`go.mod` module `nilix` |
| Go 代码 | **58,258 行 / 243 文件**（`internal/manju` 独占 44,143 行 / 140 文件） |
| 前端 | **18,688 行**原生 JS/CSS，无框架；`web/kb/js/manju.js` 单文件 **6,057 行** |
| 测试 | **116 个 `_test.go` / 15,581 行** |
| 文档 | 32 篇 Markdown / 3,162 行（含审计报告与 20+ 篇迭代记录） |
| 依赖 | 仅 5 个直接依赖：wails v3、go-webview2、gopsutil、x/sys、x/text |
| 入口 | `main.go` 单文件 **59,155 字节**（HTTP + 托盘 + 灵动岛 + 看门狗 + 设备控制） |
| 端口 | 8787（服务）/ 8190（ComfyUI，**本机实际在 8188**） |

**成熟度信号**：有 `CONTRIBUTING.md`、`docs/ARCHITECTURE.md`（含依赖铁律）、
按日期归档的迭代文档（2026-08-25 至 2026-09-04 共 20 篇）、
以及一份带"修复日志"表的自查审计报告。这个工程纪律水平明显高于同类个人项目。

---

## 三、架构：6 阶段执行 + 2 个非阶段能力

> **⚠️ 先纠正一个普遍误读**（PIPELINE-DESIGN.md §1.1 和我上一版报告都中招）：
> NiliX 里确实有一个 8 元素的 `manjuStageOrder`，但它**只用于计算前端总进度条**，
> 不代表 8 个可执行阶段。真正被执行的管线是 **6 阶段**
> （`manju_pipeline.go:2010`）。`env` 与 `upscale` **不在** `manjuPhases` 白名单里
> （`manju.go:75-77`），传 `phase=env` 会被拒绝。

**权威定义**（`internal/manju/manju.go:73`）——这是**进度条口径**：

```go
// 管线阶段顺序(与前端 FLOW 一致,用于计算总体进度)
var manjuStageOrder = []string{
    "env", "plan", "assets", "encode", "render", "qc", "assemble", "upscale",
}
```

**真正执行的口径**（`internal/manju/manju_pipeline.go:2010`）：

```go
stages := []string{"plan", "assets", "encode", "render", "qc", "assemble"}
```

| 名称 | 是阶段吗 | 实际身份 |
| --- | --- | --- |
| `env` | ❌ | 只读自检，前端健康卡片调 `POST /api/manju/env` |
| `upscale` | ❌ | 独立后台任务（云端 2K），自己 `manjuFinish`（`manju_upscale.go:325-345`） |
| 其余 6 个 | ✅ | 可 `phase=` 调度、可断点续跑 |

对应的阶段函数（全部在 `internal/manju/manju_pipeline.go`）：

| 阶段 | 函数位置 | 输入 → 输出 |
| --- | --- | --- |
| `assets` | `manju_pipeline.go:5348` | 角色/场景卡 → 定妆照 `assets/characters/<id>.png` + `_face.png` + `asset_map.json` |
| `encode` | `manju_pipeline.go:5870` | 逐镜提示词 → Qwen3-VL 条件缓存 `.pt`（同条件镜头共享） |
| `plan` | `manju_pipeline.go:6166` | 小说章节 → LLM 直出分镜方案 + 角色卡 + 场景卡 + 逐镜 H3 提示词 |
| `render` | `manju_pipeline.go:7176` | 提示词 + 参考图 → ComfyUI 生成 → `clips/<ep>/NN.mp4` |
| `qc` | `manju_pipeline.go:7559` | 片段 → 机械质检（PyAV）+ 视觉审片 |
| `assemble` | `manju_pipeline.go:7820` | 镜头序列 → 成片（转场/字幕/BGM/打码） |

> **结论**：PIPELINE-DESIGN.md §1.1 说 NiliX「把小说→漫剧拆成 8 个阶段，全部阶段幂等、
> 可断点续跑」——**一半对**。划分是对的（这些确实是系统里的 8 个能力），
> 但「全部幂等可续跑」不成立：只有 6 个是阶段，`env`/`upscale` 是旁路能力。
> 复刻时如果照搬 8 阶段状态机，会把两个非阶段能力错误地纳入续跑逻辑。

**并发与 GPU 串行化（容易设计错的地方）**：

- **Go 侧没有全局 GPU 锁**（全库 `grep sync.Mutex` 无渲染锁），
  串行化完全**依赖 ComfyUI 自身的单队列**。这与我上次评审建议的"加 flock"方向相反——
  NiliX 的选择是"把并发控制交给 ComfyUI"，代价是无法防多个 Go 实例同时提交。
- Go 侧唯一的去重是**同条件编码 singleflight**（`encGate`，`manju_pipeline.go:5765-5800`），
  避免同条件镜头重复提交 Qwen3-VL 编码。
- 并发点共 5 处：逐镜提示词 4 路、下一镜预编码 1 路（与渲染重叠）、
  审片 `judge_concurrency`（默认 2，域 1-4）、ASR 与审片并行、IR 空镜扩写 2 路。

**依赖铁律**（`docs/ARCHITECTURE.md:32`）：`util ← paths ← manju/comfy ← api ← main` 单向依赖，
禁止域包互相 import。这解释了为什么业务内核能独立于平台外壳存在。

---

## 四、最值得复用的 10 个设计（按价值排序）

### 1. 两阶段条件缓存 + 内容指纹

- 缓存文件名 `shotCacheNameAt`：`<项目清洗>_<版本v2>_c<内容指纹>`
- 内容指纹 = `MD5(p=H3Prompt|w|h|len|chars|scene|prompt)` 取 10 位
- **草稿/定稿分辨率独立记账**（`漫剧渲染视频技术文档.md:113`）

**价值**：多镜头共享文本编码（Qwen3-VL 32B 编码很贵），省下大量 GPU 时间。

### 2. 产物时效清单（manifest）三态自愈

`internal/manju/manju_manifest.go:103`：

```go
func (ctx *manjuCtx) shotManifestStatus(s manjuShot) string {
    if !fileExists(p) { return "missing" }
    e := m.Shots[strconv.Itoa(s.ID)]
    if e == nil { return "stale" }                     // 无指纹记录=不可信,重渲
    if e.Fingerprint != ctx.shotRenderFingerprint(s) { return "stale" }
    return "current"
}
```

**关键洞察**（作者在审计报告 H7 中自曝并修复）：指纹**必须覆盖渲染参数**
（`vae_video` / `turbo_lora` / `steps` / `ref_image_size`），否则改了画质配置
却被"跳过（已存在）"静默忽略——升级静默失效。这正是我在 PIPELINE-DESIGN.md 评审里
指出的 P0 缺口，NiliX 已经用工程手段解决了。

> **注**：README 与设计文档都称 manifest 有"四态"，实测**只有 3 态**
> （`manju_manifest.go` 全文件只有 3 个 `return` 字符串）。
> 第 4 态 `unknown` 已在 2026-08-26 合并进 `stale`。另外草稿与定稿缓存的隔离
> **不靠独立命名空间**，而是靠指纹里的 `w=`/`h=` 维度自然分离
> （草稿分辨率 = 定稿 × `draft_scale`，默认 0.5，对齐 32）。

### 3. 渲染检查点 + 崩溃收回（绝不重复烧 GPU）

- 提交 ComfyUI 即落盘 `prompt_id` 到 `analysis/<ep>_render_ck.json`，收产物即清
- 崩溃重启后 `tryReclaim` 查 ComfyUI `history` 收回「已提交未收」任务：
  已完成→直接收、在跑→等完、丢失 90s→重新提交
- **坑**（已修）：重渲路径（`attempt>0` / `fresh`）必须跳过 `tryReclaim`，
  否则收回旧产物覆盖重渲

### 4. 自动恢复的精确边界

`AutoRecoverRendering`（启动后 3s）：只在
`阶段 ∈ {all, encode, render, qc}` 且环境就绪时自动续跑。

> **坑**（已修）：前端一条龙发 `phase="all"`，磁盘 `Stage` 恒落盘 `"all"`——
> 白名单不含 `"all"` 就永不自动恢复。

这条"plan/assets 不自动恢复（成本低、涉及用户意图）"的边界判断，
与 PIPELINE-DESIGN.md §4.2 抄录的 NiliX 经验完全一致 ✅。

> ⚠️ 但"全阶段幂等"是**不成立**的：`qc` 与 `assemble` 每次重跑都会重新执行
> （镜头级无幂等键）。只有 assets / encode / render / upscale 有跳过逻辑。
> 详见 §四附 ⑦。

### 5. 八维度 Go 侧加权打分（不信任模型自报）

`internal/agent/agent.go:79-86`：

| 维度 | 中文 | 权重 | 对应能力 |
| --- | --- | --- | --- |
| `identity` | 主体一致性 | **20** | Ref2VA 参考保持 |
| `scene` | 场景还原 | 12 | Ref2VA retention |
| `action` | 动作符合 | 15 | 复杂多模态指令遵循 |
| `camera` | 运镜符合 | 10 | 官方运镜三要素语言 |
| `visibility` | 主体可见性 | 15 | 亮度护栏（近黑帧） |
| `tech` | 技术质量 | 15 | 面部扭曲/闪烁/文字水印 |
| `style` | 风格统一 | 8 | 风格句约束 |
| `lips` | 口型对白 | 5 | `<d>` 原生对白（帧级可靠性低故低权重） |

`WeightedScore`（`agent.go:155`）：**缺失维度按 70 兜底并标记 `fallback`**，
注释写明「缺失≠0，防单维拉崩」——这就是设计文档 §5.4 提到的"看不清给中性分 70"。

### 6. 渲染自愈闭环 + 逃生门

- 质检失败镜头**下次渲染自动删旧重渲**（轮数封顶 2，换 seed 打破死循环）
- `seedFor(attempt)`：`fixed` 模式首渲恒定，**重渲时 `seed+attempt`**
  （注释写明：打破"重渲同画面永远不过质检"死循环，曾是真 bug）
- 逃生门 `POST /api/manju/qc/decision`：`skip`（跳过失败镜）/ `accept`（接受坏片）

### 7. 提示词纪律体系（六段式 + 表演层）

- Ref2VA 六段式 / FL2VA 三段式，`<Subject N>`、`<Picture N>`、`<d>中文</d>` 原生对白
- **说话人硬约束**：`dialogue` 前缀"角色:"必须在场；角色台词禁混入旁白
- **跨镜外观锁定**：同一角色本集所有镜 Subject 描述**逐字一致**（以首镜为准）
- **表演层纪律**：情绪三层（外部动作/生理反应/量化指标）+ 微表情五维 +
  哭戏四梯度 + 节奏模型（5s≈3-4 拍 / 10s≈5-7 拍 + 峰值刹车）+ 近景补偿
- **CFG-distilled 无负面词**：负面概念逐条转译为正面排除句

**⚠️ 一个与 PIPELINE-DESIGN.md 冲突的发现**：设计文档 L190-194 称六段式"段落顺序与唯一性
被插件源码 `_validate_official_ref2va_prompt` 强制校验，违反直接报错"。但在 NiliX 里
**顺序只由提示词模板与写作规范约束，没有任何代码级校验**——机器侧只做
"段落存在性"检查 + 渲染前契约对齐兜底（`manju_pipeline.go:3674-3716`），
唯一性靠写作规范 + 台词去重器（`manju_script_parse.go:1336`）。
即：**六段顺序写错，NiliX 不会拦**。官方校验器只在你走官方插件那条路时才生效。

**其它代码级确认**：FL2VA/Ref2VA 选择判据就一行 `len(s.Characters) > 0`
（有角色→六段式，空镜→三段式，`manju_pipeline.go:3676`）；
`script_parse_ver = 25`（整数代数常量）；旁白预算超限自动延长时长并 **clamp 到 15s**
（`charsPerSec` 默认 4、域 [2,8]）；
`MotionContext.audio_context_length` 默认 `"1"`（≈25ms），专治多重配音叠音。

**⚠️ 又一个与 PIPELINE-DESIGN.md 冲突的发现：拆镜密度**

设计文档 L125 抄录 NiliX 的「拆镜密度 11-17 镜/章」。但我在源码与前端全库 grep
`11-17`，**只命中 `README.md:120` 一处，代码零命中**——README 自身已过时。
现行规则（`manju_llm.go:854`，2026-08-30 用户规则）恰恰相反：

> 约每 **80-130 字一镜** …… **宁多镜不压缩，拆镜数不设上限**

同时 `internal/storyboard/generate.go:47` 还留着一套老路径，硬写「单集 3-8 个镜头」，
与主链规则矛盾。**复刻时不要照抄文档里的密度数字。**

**⚠️ 表演层纪律是"软约束"，且对脚本直出模式完全失效**

规则 29-33（情绪三层 / 微表情五维 / 哭戏四梯度 / 非对称克制 / 原子需求台账）
全部**只是 LLM 系统提示词里的写作规范，零机械强制**（`manju_llm.go:1133-1140`），
注释自称来自「知识库整合」。而脚本直出模式下
`h3_prompt` 缺失会**直接报错、不用 LLM 补**（`manju_pipeline.go:3539-3544`），
即这些纪律完全依赖技能侧脚本自己写对——**脚本模式享受不到任何表演层保障**。

### 8. 解析器代数指纹（防坏产物永久复用）

`script_parse_ver` 随解析器升级强制重解析。
作者的总结值得抄进任何"程序化优先 + LLM 兜底"的系统：

> 程序化优先、LLM 兜底的结构必须带版本自愈，
> 杜绝「解析失败静默回退 LLM 直出后坏方案永久复用」。

### 9. 质检的三道防线

| 防线 | 判据 |
| --- | --- |
| 近黑帧 | 整帧亮度 < 20 |
| 静音 | RMS < 0.02 |
| **段尾冻结** | 末尾 25% 采样窗口相邻灰度均值差 < 0.8 占比 > 0.6 |

> 「段尾冻结」是 H3 的典型病（段尾提前到达 Last Frame 静止），
> 这条是 PIPELINE-DESIGN.md 完全没有覆盖的检查项。

### 10. 云端 2K 的付费前预校验（fail-loud）

`/v2/video_regeneration` 提交前本地校验：
32 整除 / 面积 ≤ 768×1344 / 24fps / 帧数 ≡ 5 (mod 17) / 含音轨 / ≤ 50MB。
费用预估 0.80 元/秒。**避免花钱买 400 错误**。

---

### 附：渲染层实现速查（来自 `04-comfy-render.md`，含对前述假设的修正）

**① 工作流是 Go 代码，不是 JSON 模板**（重要架构事实）

`internal/manju` 的节点图 **100% 由 Go 代码拼装**（`map[string]any` 字面量 +
`wfAdd` 自增 ID + `refOf` 文本接线，`manju_comfy.go:192-196`）。
官方 JSON 模板（`video_minimax_h3_r2v.json`）只是**人工抄读的依据，运行时不读取**；
全仓无任何 workflow 模板加载。`internal/render/*` 是另一套早期实现，节点 ID 硬编码 `"1".."15"`。

> **含义**：加一个节点 = 改 Go 代码。这与"改 JSON 模板"的常见做法完全不同，
> 移植时必须连同这套装配器一起搬，不能只搬 JSON。

**② 默认是 9:16 竖屏 768×1344**，不是本机实测用的 864×480

`config.go:164-165` / `manju_pipeline.go:8406`：默认 `768×1344`、`steps=20`、
`turbo_steps=8`、`seed=1688+镜头号`。档位 `manjuResTiers{draft:416, standard:768, fhd:1088}`
对齐 32 → `768×1344` / `416×736` / `1088×1920`。
**本机基准（864×480，9:5 横屏）与 NiliX 默认（9:16 竖屏）比例完全不同**，接入需重测显存与耗时。

**③ 修正我上次评审的假设：NiliX 没有任何 offload 配置**

先前我推断"VRAM 的真正杠杆是 offload/block-swap"。实测：NiliX **无 LowVRAM 节点、
无 `--lowvram` 启动开关、无任何 offload 配置**。显存完全交给 ComfyUI 自身的模型管理，
外加三处显式 `POST /free {unload_models, free_memory}`（编码前、Agent 渲染前、空闲 10 分钟）
与 `/queue` 忙闲门控。所以本机"剪枝版与完整版同速"的现象，
**不是 NiliX 的 offload 策略造成的**，而是 ComfyUI 层面的权重换入行为——
这一点需要在接入后重新测量才能定论。

**④ 提交与容错**

- `POST /prompt` → 拿 `prompt_id` **立即落盘检查点**
- 纯轮询（`/history/<id>` + `/queue`，10s 间隔），**全仓无 WebSocket**
- 超时：渲染 3600s / 预编码 1800s；停止感知主动 `POST /interrupt`
- **OOM 无自动重试**，只做渲染前 `/free` 卸模型

**⑤ R2V 参考图注入**

最多 **3 个角色**（`validatePlan` 硬约束「每镜登场角色 ≤3」），
每角色按视图预算 4/3/2 张，加场景图共 **≤8 张**；
节点输入用 **Autogrow 平铺键 `ref_images.ref_image_N`**（不是数组）；
`ref_image_size=max`（保留最高 2048 短边编码）。

> 注意与 PIPELINE-DESIGN.md §3.1 的差异：文档说节点上限是「参考图 11 / 视频 3 / 音频 9」，
> **节点能力上限与 NiliX 的自限策略（≤8）是两回事**，别混为一谈。

**⑥ 一键安装器的模型清单（含 Ref2VA 的确切文件名）**

`comfy_install.go:65-74` 列出 5 个模型：

| 文件 | 说明 |
| --- | --- |
| `diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors` | 本机已有 ✅ |
| **`diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors`** | **本机缺 —— 这就是要补的文件** |
| `vae/minimax_h3_video_vae_fp16.safetensors` | 本机已有 ✅ |
| `vae/minimax_h3_audio_vae_fp32.safetensors` | 本机已有 ✅ |
| `text_encoders/qwen3vl_32b_minimax_h3.safetensors`（**约 60GB**） | 本机是量化版 `..._nvfp4_awq`（15.7GB） |

> ⚠️ 安装器下载的是 **60GB 全精度 CLIP**，而 config 默认用的是 15.7GB 的 nvfp4 量化版——
> 两者不一致。接入时**不要跑一键安装的 CLIP 条目**，直接用本机现成的量化版。

**⑦ 幂等边界（并非"全阶段幂等"）**

| 阶段 | 镜头级幂等键 | 可否重跑 |
| --- | --- | --- |
| assets | 文件存在 | ✅ |
| encode | `.pt` 存在 | ✅（但**无崩溃检查点**，仅事后校验，`manju_pipeline.go:5857-5862`） |
| render | `mp4` + manifest `current` | ✅ |
| qc | — | ❌ 每次重跑（成本低：抽帧 + ffprobe） |
| assemble | — | ❌ 每次重跑（成本 = 整集重编码） |
| **upscale（云端 2K，付费）** | **`2k/NN.mp4` 存在即跳过** | ✅ **幂等**（`manju_upscale.go:289-290`） |

> **勘误**：`01-pipeline-core.md` 称「2K 不幂等、每次重跑」。**这是错的**——
> `manju_upscale.go:289-290` 明确 `if fileExists(dst) { 跳过（已有 2K 产物） }`。
> 不存在重复付费风险。（这条我实测复验后推翻了调研员的结论。）

**⑧ 两个附带缺陷**

- **进度条永远到不了 100%**：分母恒为 `len(manjuStageOrder)`=8，
  而 `upscale` 从不调 `logStage`，最大 `currentStage` 是 `assemble`（索引 6）
  → 显示停在 **75%**（`manju.go:2188-2192`）。
- **`seed` 有意不进 manifest 指纹**（`manju_manifest.go:57-70`）：改 seed 不会触发 stale。
  配合"全剧固定 seed"策略是有意为之，但换成 `random` seed 策略后，
  **改 seed 不会重渲**——这是个潜在陷阱。

**⑨ 合成与质检的实现栈（与 PIPELINE-DESIGN.md 的 FFmpeg 方案不同）**

- **主管线合成用 PyAV，不用 FFmpeg CLI**：`internal/manju/scripts/manju_media.py`
  （2028 行，`go:embed`）里 `import av` / `av.open` 是主路径，ffmpeg 命令行只有 2 处。
  转场是**自研的** cut/fade/dissolve（不是 `xfade`），音频淡入用 5%→100% 斜线近似 `acrossfade`。
  **音量归一化也没用 `loudnorm`**，而是峰值全局增益（`<0.25` → `min(4.0, 0.7/peak)`）。
  旧链路 `internal/assemble/*` 才用 FFmpeg CLI + `loudnorm=I=-16:TP=-1.5:LRA=11`。
  → 我上次评审建议的"concat 必须绝对路径 + loudnorm"只适用于旧链路，**主管线不适用**。
- **`verify.go` 里没有段尾冻结检测**（README:83 有误）：`verify.go` 只查时长/视频流/音频流/faststart；
  近黑、冻结、静音、采样率、OCR 字幕位等阈值**全在 `manju_media.py`**。
  §四.9 的"三道防线"归属应为 `manju_media.py`，不是 `internal/verify`。

**⑩ 端口不止一个（headless 移植时必须一并处理）**

| 端口 | 用途 |
| --- | --- |
| 8787 | 主服务（HTTP + 网页工作台） |
| **8799** | 主窗口控制（`/open` `/minimize` `/close` `/move` `/resize`，前端自定义标题栏走这里） |
| **8788** | 灵动岛胶囊（`/size` `/close`，✕ 会退出整个应用） |
| 8190 | ComfyUI（本机实际 8188） |

→ headless 移植时，前端若还引用 8799/8788，相关按钮会静默失效，需要一起摘掉。

**⑪ 一个已证实的真 bug：同名角色导入静默 no-op**

`internal/manju/manju_char_lib.go:459-469`：

```go
for _, x := range charsArr {
    if m, ok := x.(map[string]any); ok && str(m["id"]) == body.Name {
        m = card          // Go 里 m 是 map 头的副本，只重绑局部变量
        found = true
        break
    }
}
```

`charsArr` 里的元素**从未被替换**——导入与方案中已有角色同名的角色卡时，
操作看起来成功、实际什么都没改。应在 `for i, x := range` 上用 `charsArr[i] = card`。
（同类"看着成功实则没生效"的静默失败，与 §五 审计里的 QC 静默通过是同一族。）

**⑫ README 还有一处过时**：角色板 `_board.png` 已于 2026-08-27 被用户裁决删除
（零消费纯成本，`manju_pipeline.go:5501-5504`），README:34 的
"定妆照→角色板→多视图"流程已不存在。实际流程是
库复用 → 主定妆 → `second_form` 双形态 → YuNet 正脸 → full/side/detail/q 多视图。



---

## 五、工程质量：作者自查审计的结论

`docs/NiliX全面审计报告与升级方案.md` 是这份源码里最有价值的一篇文档。
它用 4 路并行审计 + 官方文档对照，列出 **10 严重 + 17 高 + 19 中**，并附修复日志。

**最有启发性的 5 条自曝缺陷**（均已修复）：

| 缺陷 | 教训 |
| --- | --- |
| **QC 静默通过**：`err` 只在失败镜头 > 0 分支消费；脚本崩溃 → 失败集空 → 返回 nil → 坏片直进成片 | **与 PIPELINE-DESIGN.md §5.2 的"静默失败"是同一类病**——质检必须区分「通过」与「未执行」 |
| **判分失败被当不合格**：视觉 API 故障与判分未过同入失败集 → 修复师瞎改 → 删产物重渲 → 烧满重试轮 | 必须把「服务不可用」与「质量不合格」分流 |
| **h3_context 接缝 latent 跨项目污染**：文件名只含镜头序号、无项目命名空间且从不清理 | 任何中间态缓存都必须带命名空间 + 生命周期 |
| **运行中可删项目**：delete/cleanup 不检查 running → 与渲染并发 `os.RemoveAll` | 破坏性操作必须有任务闸门 |
| **升级静默失效**：指纹漏渲染参数（steps/turbo_lora/sampler）→ 改参数被"跳过" | 指纹必须覆盖所有影响产物的输入 |

**这份审计的价值**：它把"一个能跑的流水线"和"一个可信的流水线"之间的差距，
用可执行的清单固定下来了。自建时应该直接把它当**验收检查表**用。

---

## 六、智能体审片闭环（代码级验证）

> 本节结论已由独立调研员复核 + Lead 抽样验证，完整报告见
> `research/reports/02-agent-review.md`（1024 行）

### 6.1 闭环状态机

```
render 阶段逐镜：渲完 → 后台判分（与批量 ASR 并行）
   ↓ 未达标
弱项提取(WeakDims, 阈值 60 硬编码) → 修复师改写 H3 提示词
   → clearShotArtifacts（删产物）→ retries+1 → 队尾重渲
   ↓ 轮数耗尽
终审官 ArbiterDecide → accept / regenerate
```

关键设计（值得抄）：

- **判分"调用失败"与"质量不合格"严格分流**：视觉 API 故障（`pending`+`Error`）
  直接升级、**不进入返工循环**（`manju_agent.go:1500-1511`）。
  这正是审计报告 S5 的修复成果——避免"API 故障 → 修复师瞎改 → 删产物重渲 → 烧满预算"。
- **模型只输出维度分，不输出总分**：`{"dimensions":{8键},"issues":[],"suggestion":""}`
  （`judge.go:61-62`），Go 侧只按 `Dims` 白名单取键（`judge.go:87-93`）后加权。
  **模型自报总分没有任何读取通路** —— 设计文档 §5.4 说的"不信模型自报"是真的。
- 机械质检 `QCFlags` 非空时**无条件进失败集**（`manju_agent.go:1209-1221`），
  不受视觉模型影响。

### 6.2 视觉模型调用链（抗过载设计很扎实）

| 机制 | 值 |
| --- | --- |
| 内置降级链 | `glm-4.6v-flash` → `glm-4v-flash`（支持自定义逗号链） |
| 退避 | 4s / 10s / 20s，每模型 4 次尝试（单模型最坏 34s） |
| 粘性降级 | 429 后 15min 冷却窗直连备模型 |
| 整链熔断 | 3min |
| 超时 / 参数 | 180s / max_tokens 4096 / temperature 0.1 |
| token 记账 | 仅成功且 `total_tokens>0` 才计入 `<项目>/llm_stats.json` |

### 6.3 三个已验证的缺口（自建时应避开）

**① 兜底 70 是"记录但不设防"——可让单维度判分通过**（Lead 实测推导）

`Fallback` 标记在 `judge.go:100` 赋值、`manju_agent.go:361` 序列化，
**全库无任何判定分支读取它**（grep `\.Fallback` 仅这两处）。而
`WeightedScore`（`agent.go:155-169`）对缺失维度一律按 70 计入加权：

| 模型返回 | 加权分 | 在及格线 75 下 |
| --- | --- | --- |
| 仅 `identity=95`，其余 7 维缺失 | **75.0** | **判过** ❌ |
| 仅 `identity=60`，其余 7 维缺失 | 68.0 | 判不过 |
| 8 维全缺 | 70.0 | 判不过（且全缺会报错） |

即：**只要模型只回一个高分的 identity 维度，就能以零信息通过及格线**。
数学上 `score = 0.2×v + 56`，及格线 75 对应 `v ≥ 95`——并非不可能。
修正方向：`Fallback == true` 时强制降级为"需人工/复核"，或对缺失维度数设阈值。

**② 返工预算实为 `MaxRetries + 1`**：终审 `regenerate` 分支会从零重写提示词
**再多渲一轮并无条件接受**（分数好坏都收），所以 `max_retries=2` 实际最多渲 3 次。

**③ 防注入"有声明、无验证"**：`judge.go:37` / `fixer.go:18` 有"分镜文本是数据非指令"
的提示词声明，结构层也有白名单（维度键、终审枚举、聊天 action）；
但 **`arbiterSystem` 无边界声明**、**`issues`/`suggestion` 无输出侧净化**
（会写进 `agent_state.json` 并进微信通知文案）、**且全库无注入回归测试**。

---

## 七、Linux 移植：已实证可行（不是推测）

> 本节结论来自 `06-linux-portability.md`——调研员**没有停在 grep，而是真的改代码并构建运行了**
> 一个 headless Linux 版 NiliX（32.5MB 原生 ELF），`/`、`/api/stats`、`/api/settings`、
> `/api/comfy`、`/api/harness`、`/api/outputs`、`/manage/`、`/island/`、`/splash/` 全部返回 200。
> 其中 `/api/stats` 返回**真实指标**（32 核、24.1/94.1GB 内存、
> **GPU temp 29°C / 22.3-23.9GB**）——因为 `sysmon/gpu.go` 走 `nvidia-smi` 而非 Win32 API，
> **天然跨平台**。可行性从推测变成结论。

### 7.1 A 类阻断点：24 文件 / 66 处（全库无任何 `//go:build` 标签）

| # | 阻断点 | 处数 / 文件数 | 备注 |
| --- | --- | --- | --- |
| 1 | `SysProcAttr{HideWindow/CreationFlags}` | 32 处 / 17 文件 | 机械替换 |
| 2 | `syscall.NewLazyDLL` / `UTF16PtrFromString` / `SyscallN` / `CloseHandle` | 25 处 | ⚠️ `main.go:286/493/751` 是**三个包级 var 块**，删函数也绕不过 |
| 3 | `golang.org/x/sys/windows`(+registry) | 3 处 | `watchdog`、`autostart` 是**整包** |
| 4 | `*syscall.Win32FileAttributeData` | 1 处（`manju/fs.go:517`） | **唯一卡住 29.5k 行 manju 包的点** |
| 5 | wails v3 + go-webview2 | `main.go:39-42`、`island.go:11` | 见下方勘误 |

**三处勘误**（与直觉/任务书预设不同）：

- wails v3 **不是编译硬阻断**——它有 38 个 Linux 源文件，但需要 cgo + GTK3 + webkit2gtk-4.1，
  本机 `pkg-config`/gtk3/webkit **全缺**（所以实际仍要砍掉）
- `-tags server` **救不了 `main.go`**：`application_server.go` 不提供 `Window`/`SystemTray`
- `rsrc_windows_amd64.syso` **不是**阻断点（Go 按文件名后缀自动过滤）

### 7.2 逐包可编译性

`internal/` 18 个包：**11 个零成本可编译**，**6 个仅需 `HideWindow` stub**，
**仅 `island` 真正无法编译**（纯装饰）；`autostart` / `watchdog` 需整包重写。

### 7.3 headless 最小子集（已实证）

保留 `main.go` 的 `1-54`（删 `39-42` 桌面壳 import）/`56-69` embed/`518-538`/`559-688`
（核心装配 + HTTP:8787）/`1024-1031`/`1553-1582` onExit/`1624-1634` openBrowser/`1667-1673`；
砍掉 `256-517`（含 `--mainwin` **死代码**——全库无人 spawn，加 3 个 win32 var 块）、
`690-741`、`743-1023`、`1049-1262`（8799/8788 诱饵端口）、`1263-1542`（托盘）、
`1583-1623`、`1635-1666`。

建议 `main.go` → `main_windows.go` + 新 `main_linux.go`，**Windows 分支零回归**。

### 7.4 工作量

| 档位 | 改动量 | 风险 |
| --- | --- | --- |
| **headless（够用）** | ≈ **30 文件 / 700–900 行** | 低，**已实证跑通** |
| 全功能对等 | ≈ 42–48 文件 / 2,500–3,500 行 | 中高：Wayland 下灵动岛不可对等、雷神 EC/NVAPI 无对应物、GNOME 托盘需扩展、wails v3 是 beta |

### 7.5 ⚠️ 三个会让"移植成功但用不了"的坑

**① `HasNode` 恒真 → SageAttention 自动降级是死的（Lead 已在本机实测证实）**

`internal/comfy/client.go:26-36`：

```go
func (c *Client) HasNode(name string) bool {
	resp, err := c.HTTP.Get(c.Base + "/object_info/" + name)
	if err != nil { return false }
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<20))  // 读掉但丢弃
	return resp.StatusCode == 200                                 // 只看状态码
}
```

而本机 ComfyUI（0.36，端口 8188）**实测**：

| 请求 | HTTP | body 长度 |
| --- | --- | --- |
| `/object_info/ThisNodeDoesNotExistXYZ` | 200 | **2**（`{}`） |
| `/object_info/PathchSageAttentionKJ`（未装 KJNodes） | 200 | **2**（`{}`） |
| `/object_info/MiniMaxH3ImageToVideo`（真实存在） | 200 | 1311 |

**结论：`HasNode` 对任何名字都返回 true。**

后果（`manju_pipeline.go:112-127`）：`sageAttnGuard` 探测恒真 → `sageOK=true`、
`sageNodeName="PathchSageAttentionKJ"` → 工作流注入不存在的节点 →
ComfyUI 返回 400 `missing_node_type`。**代码里那句「SageAttn 已自动关闭继续渲染」的兜底永远不会执行。**
本机没装 KJNodes，**必然踩中**。

> 讽刺的是，作者在 `manju_comfy.go:254` 的注释里**明确写过**「ComfyUI 对缺失节点
> `/object_info` 也返回 200 空对象，提交时 400 missing_node_type」——他知道这个行为，
> 但 `HasNode` 没跟着修。
>
> `pddGuard`（`:145-166`）同样恒真，但因为后面**又 AND 了一次文件存在性检查**，
> PDD 降级侥幸存活——不过判据顺序反了（先信节点、再查文件）。
>
> **修复**：`HasNode` 需判断 body 长度/JSON 非空（如 `len(body) > 2`），
> 或直接解析 JSON 后判空对象。**这是一行修复，但是移植前必须做的。**

**② 模型文件名大小写 + CLIP 体积不一致 → Linux 上"装好了仍报缺模型"**

安装器清单（`comfy_install.go:65-74`）用小写 `minimax_h3_*`，
而默认配置引用大写 `MiniMax_H3_*`；安装器下的 CLIP 是
`qwen3vl_32b_minimax_h3.safetensors`（**约 60GB**），配置默认却是
`qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors`（15.7GB）。
Windows 大小写不敏感掩盖了这个 bug，**Linux 大小写敏感会直接暴露**。
（本机 `00-environment-recon.md` 已实测同类命名冲突。）

**③ 有两套并行实现，参考图键名不同 → 复用即静默失效**

`internal/render/*`（早期实现）用 `ref_image_N` 平铺键；
`internal/manju`（生产管线）用 `ref_images.ref_image_N`。
作者自己记录过事故：「传数组静默失效 → 六镜同画面」。
**移植时只能用 `internal/manju` 那套，别碰 `internal/render`。**


---

## 八、本机环境差距（详见 `00-environment-recon.md`）

| 状态 | 项 |
| --- | --- |
| ✅ 就绪 | RTX 5090 24GB、ComfyUI（8188）、H3 FL2VA 完整版(34GB)+剪枝版(21GB)、fl2v Turbo LoRA 4/8 步、Qwen3VL-32B 编码器、视频/音频 VAE、T8 音频节点、VideoHelperSuite、磁盘 277GB |
| ❌ 缺失 | **Ref2VA UNet**、Ref2VA Turbo LoRA、**Z-Image 三件套**、SDXL、**PDD-Acc 节点**（约 25–30GB 下载） |
| ⚠️ 命名冲突 | NiliX 默认 LoRA 名 `minimax_h3_fl2va_pdd_acc_8step_comfyui.safetensors` 与本机 `minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors` 不一致，需映射层 |

**基准数据已溯源验证**：PIPELINE-DESIGN.md §2 的六组耗时（30/50/110/170/80 秒）
与原始日志 `h3-bench.log` 等**逐条吻合**，可信。但补充两个文档没说的事实：

- **4 步 LoRA 实测 30.0s vs 8 步 50.1s（快 1.67×）** → 「≈11 秒/视频秒」是 8 步口径，4 步约 6 秒/视频秒
- **剪枝版(21GB)与完整版(34GB)耗时完全相同** → 24GB 卡必然 offload，
  瓶颈可能是**权重换入带宽而非算力**

**可复用资产**：`~/comfyui/drama-project/` 是**现成的端到端回归语料**
（15 个场景镜头表 + 7 张角色参考图 + 5 集成片 + 全剧合集），新流水线应直接拿它做验收基线。

---

## 九、「做一个这个」的三条路线

| 路线 | 做法 | 工作量 | 适合场景 |
| --- | --- | --- | --- |
| **A. 移植复用**（推荐） | 以 NiliX 源码为基座，砍掉桌面壳 → headless HTTP 服务，替换 Windows 命令，接本机 ComfyUI 8188，加模型名映射层 | **已实证**：headless ≈30 文件 / 700–900 行 | 想最快拿到可用工作台，且认可 NiliX 的架构与纪律体系 |
| **B. Python 重写** | 按 PIPELINE-DESIGN.md 的分层重写 | 大（44k 行业务内核 + 15.6k 行测试要重踩一遍坑） | 团队以 Python 为主、不接受 Go |
| **C. 只借方法论** | 不搬代码，只把 NiliX 的**审计清单 + 提示词纪律 + 指纹/检查点机制**作为设计输入 | 小-中 | 目标是轻量脚本流水线，不需要 Web 工作台 |

### 建议：路线 A，按这个顺序走

**第 0 步（必须先做，约 1 小时）——修 3 个"移植成功也用不了"的坑**：

1. **修 `HasNode`**（`internal/comfy/client.go:26`）：改为判断 body 非空。
   不修则 SageAttention 探测恒真、渲染必 400 失败（本机无 KJNodes，实测已证实）。
2. **建模型名映射层**：把 NiliX 的大写 `MiniMax_H3_*` / `pdd_acc_8step` 默认值
   映射到本机实际的 `minimax_h3_*` / `fl2v_turbo_8step_v1.0`。Linux 大小写敏感，硬碰必失败。
3. **改 ComfyUI 生命周期为"只探测不启停"**：Linux 上你的 ComfyUI 是**常驻共享服务**
   （还跑着别的工作），而 NiliX 的 `onExit` 会调 `comfy.ComfyStop()` 按端口 taskkill
   ——**会杀掉你自己的 8188 实例**。这条不改，用一次就伤一次。
   同时注意端口有两个互不相通的来源：`settings.render.comfy_url` **和**
   `sysmon.go:333` 的 `const ComfyURL`（驱动 HUD 状态灯），只改前者 HUD 仍显示离线。

**第 1 步（半天）——R2V 身份锁定 A/B 对照**
用现有 FL2VA 模型 + 内置 `MiniMaxH3ReferenceToVideo` 节点跑一次
（本机 `h3-r2vtest.log` 显示 FL2VA 跑 R2V 能出片，50.1s，但身份锁定强度未知）。
**这决定要不要下 21GB 的 Ref2VA 权重**（文件名为
`diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors`）。

**第 2 步（1–2 天）——切 headless 内核**：让 8787 + 网页工作台在 Linux 跑起来，
用 `drama-project` 做端到端回归。注意默认分辨率是 **768×1344 竖屏**，
与本机实测的 864×480 横屏比例不同，需重测显存与耗时。

**第 3 步——补资产模型**：Z-Image/SDXL 到位后打通 plan→assets→render 全链路。


---

## 十、待确认的关键问题

1. **目标形态**：要的是「能用的漫剧工作台」，还是「自己掌控的轻量流水线」？
   （决定 A / B / C）
2. **质量档 vs 提速档**：接受 5 秒镜从 50s 变 100s 换取官方最佳实践画质吗？
3. **Ref2VA 是否必须**：先跑 A/B 对照再决定下载
4. **交付形态**：Linux headless 服务（浏览器访问）还是也要桌面窗口？

---

## 附：证据索引

### 六份子系统调研报告（各自 700–1200 行，含逐行证据）

| 报告 | 主题 | 覆盖 |
| --- | --- | --- |
| `reports/00-environment-recon.md` | 本机环境勘察 | GPU / ComfyUI / 模型资产 / 基准溯源 |
| `reports/01-pipeline-core.md` | 流水线内核与状态管理 | 阶段真相、条件缓存、manifest、检查点、并发 |
| `reports/02-agent-review.md` | 智能体审片与返工闭环 | 八维度、评分合成、视觉链、终审、防注入 |
| `reports/03-prompt-script.md` | 提示词体系与剧本解析 | 六段式、纪律注入、表演层、解析器、LLM 层 |
| `reports/04-comfy-render.md` | ComfyUI 集成与渲染 | 节点图、降级探测、模型基线、部署、云端 2K |
| `reports/05-api-frontend-assets-qc.md` | API / 前端 / 资产 / 质检 | 路由、鉴权、前端栈、知识库、机械质检、合成 |
| `reports/06-linux-portability.md` | Linux 移植评估 | A 类阻断点、逐包判定、headless 子集、实证构建 |

### 关键结论的证据位置

| 结论 | 证据位置 |
| --- | --- |
| 执行 6 阶段（非 8） | `internal/manju/manju_pipeline.go:2010`；白名单 `manju.go:75-77` |
| 8 元素仅用于进度条 | `internal/manju/manju.go:73` |
| 六阶段函数 | `manju_pipeline.go:5348/5870/6166/7176/7559/7820` |
| 八维度权重（硬编码） | `internal/agent/agent.go:79-86`；前端副本 `web/kb/js/manju.js:71-75` |
| 兜底 70 且 `Fallback` 无判定读取 | `agent.go:155-168`；`judge.go:100`；全库无 `.Fallback` 读取 |
| 判分失败不返工 | `internal/manju/manju_agent.go:1500-1511` |
| 视觉降级链 | `internal/agent/vision.go:23-30,63-66,95` |
| 终审拍板（预算 = MaxRetries+1） | `internal/manju/manju_agent.go:1901` |
| manifest 三态 | `internal/manju/manju_manifest.go:103-119` |
| 自动续跑白名单 | `internal/manju/manju.go:2071-2074` |
| 2K 幂等（推翻调研员结论） | `internal/manju/manju_upscale.go:289-290` |
| 进度上限 75% | `internal/manju/manju.go:2188-2192` |
| seed 不入指纹 | `internal/manju/manju_manifest.go:57-70` |
| **`HasNode` 恒真** | `internal/comfy/client.go:26-36`；本机实测 200 + `{}` |
| 工作流 = Go 代码 | `internal/manju/manju_comfy.go:135-140,192-196` |
| 默认 768×1344 竖屏 | `internal/config/config.go:164-165` |
| 无 offload 配置，靠 `/free` | 04 报告 §1.9；`manju_pipeline.go:5877`、`manju_agent.go:1341` |
| 安装器清单（含 Ref2VA 文件名） | `internal/comfy/comfy_install.go:65-74` |
| 端口两个独立来源 | `config.go:163` + `internal/sysmon/sysmon.go:333` |
| 六段式顺序无代码校验 | `manju_pipeline.go:3674-3716` |
| 11-17 镜/章仅存于 README | `README.md:120`（源码零命中）；现行 `manju_llm.go:854` |
| 审计缺陷与修复 | `docs/NiliX全面审计报告与升级方案.md` |
| 画质 5 折损点 | `docs/upgrade-20260904-quality.md` |

