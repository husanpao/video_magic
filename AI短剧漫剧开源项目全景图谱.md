# GitHub「AI 短视频 / 短剧 / 漫剧生成流水线」开源全景图谱与功能对照

> 调研目标：为自建 AI 漫剧流水线（小说 → LLM 拆镜 → 角色定妆照 → 逐镜本地渲染 ComfyUI + MiniMax H3 → 机械质检 → 合成 + 中文字幕，Python / 单卡 RTX 5090 / 极简 Web 控制台）找到可抄的设计与可复用的代码。
> 数据快照时间：**2026-09-23（UTC）**。

---

## 0. 一页速览（TL;DR）

1. **同类项目比想象中多，且 2026 年集中爆发。** 本次共对 **1,082** 个候选仓库拉取元数据，人工筛选出 **106 个**直接相关项目，逐个下载并深读 README（本地快照），覆盖你列出的 8 个方向；其中 **57 个**给了完整卡片，其余以速览表收录。
2. **和我们技术栈最像的三个项目（重点看这三个就够）**：
   - **`qiukaihui/comfyui-auto-drama`**（⭐47，MIT，Python 标准库零依赖）——**ComfyUI + MiniMax H3 ref2va 的自动化短剧流水线**，包含参考资产质检、链式衔接、失败码体系、H3 开头杂音裁剪。同栈同模型，代码量小，最适合直接读代码。
   - **`YiIimini/NiliX`**（⭐2，**无开源许可证，README 写明"私有项目，仅限本机使用"**，Go）——六阶段流水线 + **产物时效清单（current/stale/missing）+ 条件指纹缓存 + ComfyUI prompt_id 渲染检查点 + VLM 审片八维度打分返工闭环**。它把"断点续跑/幂等/审片"做到了本次调研的最深水位，**设计值得整套抄，代码不能抄**（语言不同且非开源许可）。
   - **`wicm84266964/grokbot-ai-manju`**（⭐1，MIT）——**目录契约 + `episode_map.yaml` + `characters/<id>/` 三视图与音色档案 + `qc.md` + 按 `shot_id` 单镜重渲**。工程骨架极简，是把"产物契约"落到文件系统的样板。
3. **一个必装级的 ComfyUI 节点**：**`NikoDemon80/ComfyUI-H3-Motion-Context`**（⭐1,006，GPL-3.0）——从 **latent 层**把上一段 H3 片段的尾帧与音频接到下一段，避免"解码→缩放→重编码"造成的色彩漂移与糊化，并让音轨真正**续播**（而不是重新生成一段听起来像的）。还带 **Seam Probe** 判断接缝是"真延续"还是"看起来像"。
4. **行业共识的硬门槛**：多阶段流水线、Web UI、镜头级编辑、角色一致性、分镜 schema 标准化、断点续跑、任务队列、进度日志、多集管理——**端到端类项目里出现率 78%~100%**（详见 §5）。其中**断点续跑 + 结构化 schema** 是"能不能持续生产"的分水岭。
5. **最被低估的两件事**：**产物幂等指纹**（端到端类里只有 6/18 做到）和 **换装/多形态**（4/18）。这两件恰好是漫剧的痛点（改一句提示词就全片重渲 vs. 换套衣服就换人）。
6. **不要做**：多租户/计费/支付、Selenium 多平台自动发布、分布式渲染农场、无限画布节点编辑器、通用 DAG 平台当运行时、Electron 桌面壳 + 硬件超频。详见 §7。

---

## 1. 调研方法与数据口径

**采集方式**

| 环节 | 做法 | 产出 |
|---|---|---|
| 候选发现 | GitHub Search API 34 条中英文查询（下发 35 条，1 条因响应体过大失败）（`novel to video`、`AI 短剧`、`漫剧`、`comfyui queue batch`、`screenplay JSON`、`video generation pipeline orchestration` 等）+ 手工检索 + 2 份 awesome 列表交叉验证 | 1,082 个仓库元数据 |
| 元数据 | GitHub API 的 `stargazers_count` / `license.spdx_id` / `pushed_at` / `language` / `archived`，批量 search 接口补齐 | `meta.json` |
| 深读 | 逐个从 `raw.githubusercontent.com` 下载 README 到本地，按方向分组做结构化抽取（定位 / 核心功能 / 最该学的点 / 技术栈 / 是否依赖闭源 API / 20 项功能矩阵 / **原文引用**） | 106 份分析 |
| 交叉验证 | 对 `comfyui-auto-drama`、`NiliX`、`kt-ai-Studio`、`grokbot-ai-manju`、`Slate`、`story-shot-agent`、`ComfyUI-H3-Motion-Context` 等关键项目由人工复核 README 原文 | 见各卡片 |

**功能矩阵的判定规则（重要）**

- **● 有**：README 有明确原文证据；
- **◐ 部分**：README 有间接/模糊线索（例如只说"可自定义每个环节"）；
- **○ 无**：README 明确说不做，或架构上不可能；
- **? 未验证**：README 未覆盖，或 README 过短（<500 字节）/是模板。**`?` 不等于"没有"**，只表示本次未取到证据。

**已知偏差（请在使用结论时打折）**

1. 本次判定**只依据 README**，未逐仓库读源码；功能做得比 README 写得多的项目会被低估。
2. 功能矩阵由 LLM 辅助抽取 + 人工抽样复核，个别单元可能有误；表格用于**横向筛选**，选型前建议再点开目标仓库确认 1~2 个关键点。
3. star 数反映"关注度"而非"质量"。本次榜单里**最贴合我们栈的 3 个项目 star 都极低（2 / 47 / 1）**，而 star 高的多为泛化短视频工具。**不要用 star 排序做选型。**
4. 部分仓库存在改名/转移（如 `ltdrdata/ComfyUI-Manager` → `Comfy-Org/ComfyUI-Manager`、`AIDC-AI/Pixelle-Video` → `ATH-MaaS/Pixelle-Video`、`SamurAIGPT/AI-Youtube-Shorts-Generator` → `Anil-matcha/AI-Youtube-Shorts-Generator`），本文一律用**当前 API 返回的规范名**。
5. 活跃度判定：最近推送 ≤90 天记 **活跃**，90~365 天记 **半活跃**，>365 天记 **已停更**（相对 2026-09-23）。

---

## 2. 你给的起点项目（不重复研究，仅列名与定位）

| 项目 | ⭐ | 许可证 | 最近推送 | 一句话定位 |
|---|---:|---|---|---|
| [`yi1108/printfilm`](https://github.com/yi1108/printfilm) | 2,225 | MIT | 2026-09-23 | PRINTFILM：AI 视频获客与 AI 短剧创作平台（Python，单仓库含完整前后端） |
| [`chengshudong/storyforge`](https://github.com/chengshudong/storyforge) | 2 | 未标注 | 2026-05-26 | 故事/剧本生产方向的 Python 项目（仓库描述为空，未展开研究） |
| [`JYE-HC/Director-WebUI`](https://github.com/JYE-HC/Director-WebUI) | 37 | GPL-3.0 | 2026-08-25 | 导演/分镜向的 WebUI 控制台（Python） |
| [`mainza-ai/milimovideo`](https://github.com/mainza-ai/milimovideo) | 88 | 未标注 | 2026-03-16 | 米粒视频：AI 视频生成相关工具（Python） |
| [`honolulu0/ai-drama-generator`](https://github.com/honolulu0/ai-drama-generator) | 4 | 未标注 | 2026-01-17 | AI 短剧生成器（仓库描述为空） |
| [`iceemperor-gcempire/vcc-manager`](https://github.com/iceemperor-gcempire/vcc-manager) | 3 | MIT | 2026-09-23 | 视频创作/合成管理控制台（JavaScript） |
| [`ChrisChen667788/wind-comic`](https://github.com/ChrisChen667788/wind-comic) | 583 | MIT | 2026-09-23 | 一句话→成片的多智能体漫剧流水线：剧本、电影化分镜、角色一致视频；Provider 无关（OpenAI/Claude、MJ、Minimax、Veo/Sora、fal、ComfyUI） |

> 注：上表 star/许可/时间为 GitHub API 实时值，定位取自仓库 description，未做进一步研究（按你的要求）。

---

## 3. 八个方向的项目卡片

阅读方式：每个方向先给 **代表项目**（完整卡片：定位 / 核心功能 / 最该学 / 技术栈与兼容性），再给 **同向速览**（只有名字、star、定位）。所有卡片内容均来自对应仓库 README，引用原文见各项目的 `evidence`（本次抽取的原始引用保留在 `_research/out/*.json`，可复查）。


### 方向 1 文本→短视频自动化

这一族的目标是「给一个主题/一句 prompt → 出一条短视频」。它们的**合成链路（TTS/字幕/BGM/拼接）与 Provider 抽象**最值得我们抄；但它们几乎不做跨镜角色一致性，也不做镜头级资产，所以不能整体照搬。

#### 代表项目

#### `harry0703/MoneyPrinterTurbo` — ⭐12.5万 · MIT · 最近推送 2026-09-21 · 活跃
- **定位**：输入主题或关键词即可一站式自动生成脚本、素材、配音、字幕与成片的高清短视频工具
- **核心功能**：（1）提供 AI Agent、WebUI、API、CLI 四种入口，从主题自动完成脚本、配音、素材、字幕、配乐和剪辑；（2）脚本可 AI 生成或改写并支持多语言，兼容 Kimi、OpenAI、Claude、Gemini、DeepSeek、通义千问等云端模型；（3）素材可上传本地图片视频，或取 Pexels/Pixabay/Coverr 库存，并接入 Seedance、MiniMax H3 等文生视频；（4）配音集成 Edge TTS（免费无需 Key）、Azure、ElevenLabs、MiniMax 等十余种，支持音色试听与完整配音预览；（5）字幕支持 edge 时间戳与本地 faster-whisper 两种模式，可调字体、位置、颜色、大小、描边和背景样式；（6）支持 9:16、16:9、1:1 三种画幅，可批量生成多条成片、记录任务历史，并一键发布到 TikTok 等平台
- **最该学**：把脚本到成片拆成可替换的完整流水线，每个环节都允许用自定义内容覆盖
- **技术栈/兼容性**：Python 3.11+ / Streamlit WebUI + FastAPI + ffmpeg + faster-whisper，支持 Docker 部署；两者都支持（云端 LLM/TTS/素材，也可本地 Whisper/Ollama/EdgeTTS）；可复用度 **高：Python 全栈实现，流水线分层清晰，可直接借鉴与二次开发**

#### `calesthio/OpenMontage` — ⭐6.1万 · AGPL-3.0 · 最近推送 2026-09-06 · 活跃
- **定位**：面向 AI 编码助手的开源 agentic 视频制作流水线，覆盖研究到渲染全流程
- **核心功能**：（1）固定七阶段流水线：research→proposal→script→scene_plan→assets→edit→compose；（2）11 条成品管线：解说、动画、数字人口播、电影感、Clip Factory、纪录片蒙太奇、本地化配音等；（3）打分选型：100+ 工具与 60+ 供应商按 7 个维度自动评分选型，并记录含备选项的决策审计日志；（4）质量闸门：合成前校验交付承诺与 slideshow 风险，渲染后用 ffprobe 抽帧查黑帧、音频电平与字幕；（5）人工审批闸门：proposal/script/scene plan/assets 等阶段暂停等签字，Backlot 看板逐镜审片；（6）预算管控：执行前预估、预留、对账，支持 observe/warn/cap 模式与单次动作审批阈值（默认 $0.50）
- **最该学**：把质量闸门、人工审批、决策审计与预算管控工程化，且规则以 YAML/Markdown 可读文件暴露
- **技术栈/兼容性**：Python 3.10+ 工具层 + Node.js（Remotion/HyperFrames 渲染）+ FFmpeg；YAML 清单与 Markdown 技能驱动；两者都支持：零 key 走 Piper/免费素材/本地模型，可选 FAL/Kling/ElevenLabs/Suno 等云 API；可复用度 **高：Python 工具层、JSON Schema 与质检流程可直接借鉴**

#### `ATH-MaaS/Pixelle-Video` — ⭐2.8万 · Apache-2.0 · 最近推送 2026-06-14 · 半活跃
- **定位**：输入一个主题即可全自动产出短视频的 AI 引擎：文案、配图、配音到成片
- **核心功能**：（1）输入主题自动撰写文案、生成 AI 配图/视频、合成语音解说与 BGM 并合成视频；（2）模块化流程：文案生成 → 配图规划 → 逐帧处理 → 视频合成，各环节可替换模型；（3）支持 ComfyUI 本地工作流、RunningHub 云端，以及直连 DashScope/OpenAI/Seedance/Kling 等 API；（4）HTML 视觉模板（static_/image_/video_）按竖屏/横屏/方形分组，可自定义模板；（5）TTS 支持 Edge-TTS、Index-TTS 等，并可上传参考音频做声音克隆；（6）扩展流水线含数字人口播、图生视频、动作迁移，历史记录页可批量创建任务
- **最该学**：原子能力可插拔（ComfyUI/RunningHub/直连 API）加 HTML 模板出片，零成本可跑
- **技术栈/兼容性**：Python + Streamlit Web UI + ffmpeg；uv 管理依赖，Windows 整合包分发；两者都支持；可复用度 **高 - 现成文案→配图→TTS→合成全链路，可直接复用改造**

#### `linyqh/NarratoAI` — ⭐1.1万 · MIT · 最近推送 2026-09-17 · 活跃
- **定位**：一站式 AI 影视解说与自动化剪辑工具，用 LLM 完成文案、剪辑、配音和字幕
- **核心功能**：（1）基于 LLM 撰写影视解说文案，并自动完成视频剪辑、配音与字幕生成的一站式流程；（2）支持阿里 Qwen2-VL、可选 TwelveLabs Pegasus 作为视频理解后端，原生理解整段画面挑高光写解说；（3）支持短剧混剪与短剧解说：一键合并素材、一键转录、一键清理缓存，并可导出剪映草稿；（4）TTS 引擎多：腾讯云、豆包、IndexTTS-1.5/2 本地语音克隆（含 Apple Silicon MLX）、OmniVoice；（5）支持可选 AI 自动配乐（含 Sonilo AI），Fun-ASR 一键转录字幕，并做逐帧分析与抽帧缓存优化；（6）提供 Windows/macOS 整合包、Docker 与 uv 本地运行三种方式，Streamlit WebUI 默认 8501 端口
- **最该学**：先用视频理解模型读懂整段画面再挑高光写解说，而不是先生成文案再硬配素材
- **技术栈/兼容性**：Python 3.12+ / Streamlit / uv / Docker / ffmpeg，接入多个云端与本地 TTS、ASR 引擎；两者都支持（云端 LLM/TTS，也支持 IndexTTS 本地语音克隆）；可复用度 **高：影视解说垂类流水线完整，视频理解与剪映草稿导出值得借鉴**

#### `RayVentura/ShortGPT` — ⭐7,969 · MIT · 最近推送 2025-02-10 · 已停更
- **定位**：面向短视频的 AI 视频自动化框架，用 LLM 可读的编辑语言驱动脚本到成片
- **核心功能**：（1）ContentShortEngine 面向短剧，从脚本生成一直到最终渲染，并自动补充 YouTube 元数据；（2）ContentVideoEngine 面向长视频，负责生成音频、自动搜刮背景素材、对齐字幕时间轴并准备背景资产；（3）ContentTranslationEngine 输入视频文件或 YouTube 链接，转录后翻译并为目标语言配音加字幕，产出新语种视频；（4）EditingEngine 用 Editing Markup Language 和 JSON 把剪辑拆成 LLM 可理解、可定制的编辑块；（5）配音支持免费 EdgeTTS（30+ 语言）与 ElevenLabs；素材来自 Pexels 与 Bing Image；（6）用 TinyDB 持久化自动化编辑变量，Gradio 界面默认跑在 31415 端口，支持 Docker 与 Colab
- **最该学**：把剪辑过程抽象成 LLM 可读写的编辑标记语言与 JSON 块，这层 DSL 抽象最值得学
- **技术栈/兼容性**：Python / MoviePy / Gradio / TinyDB / Docker，LLM 走 OpenAI；两者都支持（LLM 用 OpenAI、可选 ElevenLabs，配音也可用免费 EdgeTTS）；可复用度 **高：纯 Python 框架，引擎分层与编辑 DSL 的抽象方式可直接借鉴**

#### `ddean2009/MoneyPrinterPlus` — ⭐7,138 · GPL-3.0 · 最近推送 2025-03-07 · 已停更
- **定位**：AI 批量生成与混剪短视频，并自动发布到抖音、快手、小红书、视频号的一站式工具
- **核心功能**：（1）用大模型一键批量生成短视频：从关键词生成文案，再配音、配背景音乐、加字幕并合成，页面显示步骤与进度；（2）批量混剪：最多配置 5 个片段，每片段一个素材目录加一个文案 txt，随机挑一行文案产出大量不重复视频；（3）批量自动发布：selenium 依附已开 debug 端口的 Chrome/Firefox 模拟点击，发到抖音、快手、小红书、视频号；（4）支持 30+ 转场特效、多种分辨率与比例、片段最小最大时长、100+ 配音音色与语速调节试听；（5）LLM 接 OpenAI、Azure、Kimi、千帆、百川、通义千问、DeepSeek 与本地 Ollama；（6）语音还可走本地 chatTTS、fasterwhisper、GPT-SoVITS；一次最多批量生成 100 个视频
- **最该学**：把批量混剪做成素材目录加文案池随机组合的配方，一次产出上百条不重复视频
- **技术栈/兼容性**：Python 3.10/3.11 / Streamlit / ffmpeg 6.1.1 / selenium，接入云端与本地 LLM、TTS；两者都支持（云 LLM 与云语音可选，也有本地 Ollama/ChatTTS/fasterwhisper）；可复用度 **中：功能覆盖广但工程偏脚本化，混剪配方与发布自动化值得参考**

#### `FujiwaraChoki/MoneyPrinter` — ⭐1.4万 · MIT · 最近推送 2026-03-26 · 半活跃
- **定位**：Ollama 优先的 YouTube Shorts 生成器，输入主题即自动出片
- **核心功能**：（1）输入一个视频主题即自动生成 YouTube Shorts，目标是全流程自动化出片；（2）脚本生成与元数据完全由本地 Ollama 模型驱动，启动 Ollama 拉模型后在 UI 选择即可；（3）用 DB 支撑的生成队列（API + worker + Postgres，Docker 部署）实现可靠、可重启续跑的处理；（4）支持发布到 TikTok，需用户从浏览器 cookie 中复制 sessionid 提供给工具；（5）ImageMagick 在 Linux/macOS/Windows 自动探测，失败可在 .env 手动指定可执行文件路径；（6）提供交互式安装脚本 setup.sh，文档集中在 docs/（quickstart、architecture、docker、testing）
- **最该学**：用 DB 队列加 worker 加 Postgres 把生成做成 restart-safe 的任务处理，可靠性设计值得学
- **技术栈/兼容性**：Python + 本地 Ollama；API/worker/Postgres 通过 Docker 运行，依赖 ImageMagick；纯本地开源（脚本与元数据全用本地 Ollama）；可复用度 **中：体量小、架构清晰，主要可学队列化与 Docker 化可靠性设计**

#### `Anil-matcha/AI-Youtube-Shorts-Generator` — ⭐5,095 · MIT · 最近推送 2026-09-17 · 活跃（原名 `SamurAIGPT/AI-Youtube-Shorts-Generator`）
- **定位**：把长视频自动切成带评分与钩子句的 9:16 短视频，支持云端与本地双模式
- **核心功能**：（1）下载与转写：API 模式用 MuAPI，本地模式用 yt-dlp 与 faster-whisper（CPU/CUDA）离线转写；（2）爆款高亮排序：LLM 按钩子、情绪峰值、观点炸弹等框架给候选打 0-100 分并附 hook 句与理由；（3）长视频分块：超过 30 分钟的视频按 20 分钟重叠分块，跨块镜头不会被漏掉；（4）重叠去重：候选取重叠超过 50% 时按分数合并，保留高分片段避免近似重复；（5）智能竖屏裁切：API 用 MuAPI autocrop，本地用 OpenCV 人脸跟踪加运动平滑；（6）CLI 加 Python 库：命令行或 import generate_shorts，--output-json 输出全部候选与结果
- **最该学**：高亮算法可插拔可编辑：把爆款框架写成 prompt 并对候选打分去重，而非黑盒
- **技术栈/兼容性**：Python 3.10+；yt-dlp、faster-whisper、ffmpeg、OpenCV，LLM 可接 OpenAI/Gemini；两者都支持：默认 MuAPI 云，--mode local 本地仅 LLM 远程；可复用度 **高：纯 Python 库，可直接 import 复用高亮排序与裁剪流程**

#### 同向速览（未展开卡片）

| 项目 | ⭐ | 许可证 | 最近推送 | 状态 | 一句话定位 |
|---|---:|---|---|---|---|
| [`YILS-LIN/short-video-factory`](https://github.com/YILS-LIN/short-video-factory) | 5,475 | AGPL-3.0 | 2026-09-22 | 活跃 | 桌面端开源短视频工厂，用提示词加分镜素材自动完成文案、配音、字幕与混剪 |
| [`IgorShadurin/app.yumcut.com`](https://github.com/IgorShadurin/app.yumcut.com) | 883 | 自定义(未标准) | 2026-09-03 | 活跃 | 开源可自托管的 AI 竖屏短视频生成器，从想法到可发布成片全流程自动化 |
| [`zhouxiaoka/autoclip`](https://github.com/zhouxiaoka/autoclip) | 8,790 | MIT | 2026-09-23 | 活跃 | 用 AI 分析字幕找高光，把长视频自动剪成短视频切片并发布 |
| [`hypit-ai/hypit`](https://github.com/hypit-ai/hypit) | 1.5万 | 自定义(未标准) | 2026-09-23 | 活跃 | 让编码 Agent 用声明式 SVML 复刻爆款视频工作流的开源工具 |
| [`Vincentwei1021/video-shotcraft`](https://github.com/Vincentwei1021/video-shotcraft) | 9,285 | Apache-2.0 | 2026-09-09 | 活跃 | 把编码 Agent 变成动效工作室、用 Remotion 出产品片的 Skill |
| [`krillinai/OpenCreator`](https://github.com/krillinai/OpenCreator) | 1.2万 | Apache-2.0 | 2026-09-23 | 活跃 | 以 Codex CLI 为执行引擎的本地 AI 创作工作台，覆盖翻译、配音与生成 |
| [`rushindrasinha/youtube-shorts-pipeline`](https://github.com/rushindrasinha/youtube-shorts-pipeline) | 2,294 | MIT | 2026-06-09 | 半活跃 | 开源 AI 短视频流水线，选题到发布 Shorts 全自动，约 $0.11/条 |
| [`elebumm/RedditVideoMakerBot`](https://github.com/elebumm/RedditVideoMakerBot) | 1.3万 | GPL-3.0 | 2026-09-17 | 活跃 | 抓取 Reddit 帖子或指定版块，自动生成短视频，全程无需人工剪辑或整理素材 |


### 方向 2 小说/漫画→漫剧/短剧视频

与我们同赛道的一族。本节按「离我们技术栈的远近」排序：**前 4 个是 ComfyUI / MiniMax H3 本地向的同类**（重点看），后面是端到端平台与国产新锐。

#### 代表项目

#### `qiukaihui/comfyui-auto-drama` — ⭐47 · MIT · 最近推送 2026-08-26 · 活跃
- **定位**：基于 ComfyUI 与 MiniMax H3 的自动化短剧流水线，控制台零第三方依赖且本地云端可切换
- **核心功能**：（1）剧本流水线：粘贴片段或导入剧本 JSON，AI 从编剧角度改写并逐段扩写成 H3 规范提示词，可手动编辑；（2）提示词合规：对白强制 (S1)/(S2) 说话人 ID 与音色锁定、防穿帮负向约束，R2V 提交自动包装官方 Ref2VA 六段式；（3）参考资产：一键生成角色四视图/场景图/分镜图，多模态模型自动质检人数、服装、穿帮，可改词重生；（4）批量生成与链式衔接：T2V/I2V/R2V 多参考模式，上段末帧接下段首帧，步数 4-50 可调且低步数自动加速；（5）状态与合成：SQLite 记录版本化任务，链式守护进程自动推进下一段，一键合成带字幕/CTA 成片并裁掉 H3 开头杂音；（6）失败码定位：生成失败自动打码（F-SUBMIT-API/F-TIMEOUT/F-COUNT），本地与云端主备自动降级
- **最该学**：零依赖 Python 标准库控制台加链式守护与失败码，把 H3 分段长视频生成串成可监控流水线
- **技术栈/兼容性**：Python 3.10+ 标准库控制台 + 原生前端，SQLite，ComfyUI(MiniMax H3)，ffmpeg，可选 MLX/Boogu-Image；两者都支持（本地与云端可切换，含 DeepSeek/OpenAI/DashScope）；可复用度 **高：控制台零依赖纯 Python 标准库，SQLite 状态与守护进程易直接复用**

#### `YiIimini/NiliX` — ⭐2 · 未标注 · 最近推送 2026-09-08 · 活跃
- **定位**：Go 单服务的本地 AI 漫剧工作台，覆盖小说到成片全流程并内置智能体审片返工与系统监测
- **核心功能**：（1）六阶段流水线：方案→资产→编码→渲染→质检→合成，支持断点续跑、条件缓存与脚本直出免拆镜；（2）角色资产：定妆照→角色板→多视图，YuNet 人脸检测中心裁剪；跨项目资产库按形象 sha256 指纹自动复用；（3）智能体调度：剧本师复核→审片官 VLM 逐镜八维度加权判分→修复师定点改写提示词返工→预算耗尽自动拍板；（4）渲染保真与恢复：分辨率档位、草稿预审、seed 重试、SageAttention 加速与查 ComfyUI history 的崩溃续跑；（5）成片表现：硬切/闪黑/叠化转场、BGM 对白自动闪避混音、字幕烧录、faststart 与剪映草稿导出；（6）系统与部署：自包含 ComfyUI 拷贝即迁移、一键安装断点续传、灵动岛监测 CPU/GPU/风扇并可调性能模式
- **最该学**：全阶段幂等断点续跑配合审片返工闭环：产物指纹标 stale 自动重渲，判分在 Go 侧计算不信任模型自报
- **技术栈/兼容性**：Go 1.22+ 单服务 + WebView2 前端，ComfyUI(MiniMax H3/Z-Image/SDXL)，FFmpeg，少量内嵌 Python 媒体脚本；两者都支持（DeepSeek 生成剧本、智谱 VLM 审片，渲染本地）；可复用度 **低：主体为 Go，仅 manju_media.py 等少量 Python 媒体脚本被嵌入释放**

#### `oskey/kt-ai-Studio` — ⭐87 · 未标注 · 最近推送 2026-05-26 · 半活跃
- **定位**：LLM 驱动的 ComfyUI 自动化漫剧/图像/视频生成工作室，无需训练 LoRA 即可保持一致性
- **核心功能**：（1）LLM 规划与提示词：内置结构化 Prompt 模板，先向 LLM 取人物/场景信息，再取适配 ComfyUI 的最终提示词；（2）自动剧情拆解：输入一段剧情梗概，LLM 自动提取出场人物、分幕场景、场景类型与角色对白并生成结构化数据；（3）一致性方案：基础立绘加多视角三视图固定人物特征，场景指纹与画风锁定防漂移，无需训练 LoRA；（4）ComfyUI 深度集成：FastAPI + WebSocket 双向通信，工作流排队、中断与进度实时监控，节点 ID 对齐即可换模型；（5）任务队列与 Web 控制台：后台任务管理器支持批量异步处理，控制台含项目/人物/场景/视频/日志/设置页；（6）视频生成：内置 Wan2.2 i2v 与 LTX2（含运镜 LoRA）等工作流，对白驱动说话动作，FPS 与时长自适应
- **最该学**：不训练 LoRA 也能锁定人/景/风格一致，并把 LLM 拆解剧本与 ComfyUI 批量出图出视频串成自动流
- **技术栈/兼容性**：Python（FastAPI + Jinja2 控制台 + 后台任务管理器），ComfyUI API/WebSocket，OpenAI 兼容 LLM（本地或远程）；两者都支持（推荐 LLM 走 API，图像视频本地）；可复用度 **高：本体即 FastAPI 的 Python 项目，ComfyUI 集成与任务队列可直接借鉴**

#### `wicm84266964/grokbot-ai-manju` — ⭐1 · MIT · 最近推送 2026-09-06 · 活跃
- **定位**：由 Grok Bot 驱动的本地 AI 漫剧工厂，覆盖整本小说到分集视频与发布包的全流程
- **核心功能**：（1）全小说输入：解析世界观、势力、地点与连续性，产出 bible/ 与 plan/episode_map.yaml 分集映射；（2）分集生产：剧本、对白、分镜、关键帧、本地视频到终剪，按文件夹契约固定每集产物路径；（3）角色一致性：characters/<id>/ 存放三视图与 voice/profile.yaml 音色档案，可投参考图建角色卡；（4）本地渲染：走 ComfyUI HTTP API 无需节点 UI，Wan 2.1 T2V 竖屏路径已冒烟验证，H3 可带参考音色；（5）QC 与返工：留 qc.md 质检记录，可按 shot_id 重渲单个镜头；（6）发布包：封面、标题条、简介与竖屏安全区，导出统一收在 shows/<show>/epXX/export/
- **最该学**：用自然语言让 Grok Bot 接管全流程，以文件夹契约加 thin scheduler 固化 I/O，产物路径唯一
- **技术栈/兼容性**：Grok Bot Skill + 文件夹契约 + Python CLI scaffold，ComfyUI(Wan/H3) 与 FFmpeg；两者都支持（需闭源 Grok Bot 驱动，渲染本地）；可复用度 **中：含 CLI scaffold 与 tools/ 下 Python wheels，调度器仍在开发**

#### `Forget-C/Jellyfish` — ⭐6,480 · Apache-2.0 · 最近推送 2026-07-30 · 活跃
- **定位**：AI 短剧端到端工作台：从剧本到分镜、镜头准备、视频生成与导出的生产流程。
- **核心功能**：（1）剧本理解与分镜拆解：章节剧本切成镜头，抽取角色/场景/道具/服装/对白，并做优化与一致性检查。；（2）镜头准备确认流：抽取刷新资产与对白候选，接受、忽略或链接，用统一 ready 状态判定镜头可生成。；（3）资产一致性复用：角色/演员/场景/道具/服装共享实体模型，跨镜头复用稳定风格与身份。；（4）镜头级生成工作台：管理关键帧、参考图与视频 prompt，支持单镜和批量生成并回写镜头媒体系统。；（5）统一异步任务中心：文本/图像/视频任务统一状态、耗时、结果与取消，可跳回项目/章节/镜头。；（6）模型与 Prompt 基础设施：多供应商多模型管理、按类默认模型、Prompt 模板、OpenAPI 生成前端类型。
- **最该学**：把镜头准备态与生成中分离：候选资产/对白先人工确认到 ready，再进生成工作台。
- **技术栈/兼容性**：后端 FastAPI（Python）+ MySQL + Redis + RustFS；前端 React + Vite + TypeScript（pnpm）；OpenAPI 驱动契约；依赖闭源API(多供应商需自配，未点名厂商)；可复用度 **高；后端 FastAPI/Python 分层清晰，架构可借鉴。**

#### `ArcReel/ArcReel` — ⭐5,142 · AGPL-3.0 · 最近推送 2026-09-23 · 活跃
- **定位**：多智能体驱动的开源 AI 视频工作台：从小说到剧本、分镜、生成、合成与导出。
- **核心功能**：（1）多智能体流水线：编排 Skill 检测项目状态并调度聚焦 Subagent，完成角色线索提取、分集、剧本 JSON 与资产生成。；（2）多供应商图像/视频/文本：预置 Gemini、火山方舟、Grok、OpenAI，可全局或项目级切换并接兼容端点。；（3）异步任务队列：RPM 速率限制加 Image/Video 独立并发通道，lease-based 调度并支持断点续传。；（4）渐进式分集规划：peek 探测→Agent 建议断点→用户确认→物理切分，长篇小说按需制作。；（5）一致性与线索追踪：先生成角色设计图供后续分镜与视频参考，关键道具场景标为线索跨镜连贯。；（6）费用追踪与导出：多供应商费用统计与三级费用预估对比，FFmpeg 合成并导出剪映 5.x/6+ 草稿 ZIP。
- **最该学**：编排 Skill 加聚焦 Subagent：小说原文留在子 Agent 内，主 Agent 只收摘要。
- **技术栈/兼容性**：后端 FastAPI + Python 3.12 + SQLAlchemy 2.0 async/Alembic + SQLite/PostgreSQL；前端 React 19 + TS + Tailwind 4 + Vite；Claude Agent SDK；FFmpeg/Pillow；两者都支持；预置四家云，也可接 Ollama/vLLM；可复用度 **高；FastAPI 后端加 Claude Agent SDK 编排可借鉴。**

#### `Anning01/novelvids` — ⭐334 · 自定义(未标准) · 最近推送 2026-09-22 · 活跃
- **定位**：开源 AI 短剧平台：小说或参考视频自动完成拆解、实体提取、分镜与多模态成片。
- **核心功能**：（1）从书稿到项目：解析 txt/doc/docx/pdf 与章节结构，Agent 自动或人工逐步完成项目分析与资产规划。；（2）重制工坊：上传 mp4/mov 或整套按集数命名的文件夹，PySceneDetect 切镜并逐段生成专业分镜 Prompt。；（3）设定资产与衍生形态：角色/场景/道具批量生成，保留版本历史，支持变装与年龄状态等衍生形态及独立音色。；（4）分镜策略与 Prompt：电影感/旁白两套策略，Prompt 以 @{资产名}、@音频N 显式绑定角色、场景与音色。；（5）视频生成与连续性：参考图与首尾帧生视频，批量按上一镜尾帧顺序无人值守连续生成，FFmpeg 兜底提尾帧。；（6）创作助手与成本：Pydantic AI 工具循环读写设定分镜并给可撤销回执，成本看板记录定价快照与折扣。
- **最该学**：用 @{资产名}/@音频N 显式绑定资产与音色，并用上一镜尾帧当下一镜首帧保证镜头连续。
- **技术栈/兼容性**：后端 FastAPI + Python 3.12 + Tortoise ORM + Pydantic AI + PySceneDetect/FFmpeg；前端 Vue 3 + TS + Vite + Pinia + Vue Flow；SQLite/PostgreSQL + 阿里云 OSS；依赖闭源API(Seedance/Seedream、MiniMax、Wan3)；可复用度 **高；纯 Python 后端分层清晰，Prompt 组织可复用。**

#### `chatfire-AI/huobao-drama` — ⭐1.5万 · 自定义(未标准) · 最近推送 2026-09-23 · 活跃
- **定位**：全栈 TypeScript 短剧平台：四个 Agent 串起小说到分镜与合成
- **核心功能**：（1）四个 Mastra Agent：小说改写、角色/场景/道具提取去重、剧本拆镜、图像与视频提示词生成；（2）分镜页可逐镜查看修改提示词，@角色 自动映射到参考图，一键批量生成视频并重试失败任务；（3）FFmpeg 完成逐镜合成与字幕处理，勾选镜头后整集合成导出，合并前校验视频文件存在；（4）SQLite 单文件数据库由 Drizzle 管理，首启幂等建表，支持从旧 MySQL 一次性导入并校验行数；（5）多供应商：文本接 OpenAI/Gemini，图像接火山，视频接 Seedance 2.0、MiniMax H3、通义万相；（6）同一代码支持 Nuxt 3 网页、Electron 桌面端与 Docker 部署，数据与技能目录落在用户数据目录
- **最该学**：用 Mastra Agent 加可在线编辑的 Skill 文件把小说到成片固定成四段流程，并配批量重试与整集导出
- **技术栈/兼容性**：TypeScript / Node.js 20+ / Hono / Drizzle ORM + better-sqlite3 / Mastra + AI SDK / FFmpeg(fluent-ffmpeg) / Sharp / Nuxt 3 + Vue 3 / Electron / Docker；依赖闭源API(OpenAI/Gemini/火山 Seedance 2.0/MiniMax H3/通义万相)，密钥在设置页配置入库；可复用度 **低：TS 全栈，仅 Agent 分工与 Skill 机制可借鉴**

#### 同向速览（未展开卡片）

| 项目 | ⭐ | 许可证 | 最近推送 | 状态 | 一句话定位 |
|---|---:|---|---|---|---|
| [`HBAI-Ltd/Toonflow-app`](https://github.com/HBAI-Ltd/Toonflow-app) | 1.6万 | Apache-2.0 | 2026-09-23 | 活跃 | 本地 AI 短剧工作台，用无限画布与三层 Agent 闭环走完策划到出片全流程 |
| [`dramaclaw/dramaclaw`](https://github.com/dramaclaw/dramaclaw) | 6,332 | 自定义(未标准) | 2026-09-23 | 活跃 | 自托管 AI 短剧生产流水线：无限节点画布与剧集流水线双轨，内置导演 Agent |
| [`shuyu-labs/BigBanana-AI-Director`](https://github.com/shuyu-labs/BigBanana-AI-Director) | 2,263 | 自定义(未标准) | 2026-09-22 | 活跃 | 工业化 AI 短剧与漫剧生产工作台，以剧本→资产→关键帧流程控制角色一致性与镜头 |
| [`alibaba/lumenx`](https://github.com/alibaba/lumenx) | 1,301 | MIT | 2026-08-11 | 活跃 | AI 原生短漫剧视频创作平台，流水线完成剧本到成片，兼带独立生成工具台 |
| [`Stonewuu/ai-fusion-video`](https://github.com/Stonewuu/ai-fusion-video) | 1,524 | MIT | 2026-09-22 | 活跃 | Agent 驱动的视频创作平台，把项目、剧本、分镜、素材与生成整合进统一工作区 |
| [`LingGuoAI/LingGuo-Drama`](https://github.com/LingGuoAI/LingGuo-Drama) | 1,529 | 未标注 | 2026-08-21 | 活跃 | 开源 AI 短剧后台工作台：剧本、角色场景、分镜生成到 FFmpeg 合成可二开 |
| [`869413421/ai-moive-studio`](https://github.com/869413421/ai-moive-studio) | 1,572 | Apache-2.0 | 2026-09-16 | 活跃 | 开源 AI 视频创作工作台，以无限画布和自然语言助手完成小说剧本到角色分镜成片 |
| [`appolloqin/huohuo-drama`](https://github.com/appolloqin/huohuo-drama) | 10 | 未标注 | 2026-09-20 | 活跃 | 全栈开源 AI 短剧与小说平台：文本经剧本、分镜、素材到成片，含配音与逐镜混流。 |
| [`xxx-888/super_gen`](https://github.com/xxx-888/super_gen) | 18 | 未标注 | 2026-09-18 | 活跃 | AI 短剧 Web 平台：剧本解析、@引用提示词、节点画布与在线剪辑收敛成生产链 |
| [`Agions/novella`](https://github.com/Agions/novella) | 114 | MIT | 2026-09-08 | 活跃 | Tauri + Rust 桌面端多智能体漫剧平台，从剧本或小说推导 4K 漫剧。 |
| [`xinzhuzi/MYStudio`](https://github.com/xinzhuzi/MYStudio) | 7 | AGPL-3.0 | 2026-09-22 | 活跃 | 本地优先的 AI 漫剧/短剧桌面工作台，把小说到成片的各环节放进同一条可追踪工作流 |
| [`123Mr-king/waoowaoo`](https://github.com/123Mr-king/waoowaoo) | 12 | 自定义(未标准) | 2026-07-17 | 活跃 | 小说文本自动生成分镜、角色、场景并合成完整视频的 AI 短剧制作工具。 |
| [`hyyyyyyz/dramai`](https://github.com/hyyyyyyz/dramai) | 33 | Apache-2.0 | 2026-05-04 | 半活跃 | 纯浏览器零后端的 AI 短剧工具，从故事素材到分镜、图、视频与剪映草稿一站完成 |
| [`xuanyustudio/LocalMiniDrama`](https://github.com/xuanyustudio/LocalMiniDrama) | 1,829 | MIT | 2026-09-14 | 活跃 | 本地优先的 AI 短剧/漫剧桌面工具，素材不出本机，列表与画布双视图贯通全流程 |
| [`LingyiChen-AI/AIComicBuilder`](https://github.com/LingyiChen-AI/AIComicBuilder) | 1,878 | Apache-2.0 | 2026-04-27 | 半活跃 | AI 驱动的漫剧生成器，从剧本导入到分镜、逐镜头视频与合成成片的全自动流水线 |
| [`TypeTale/TypeTale`](https://github.com/TypeTale/TypeTale) | 850 | 未标注 | 2026-08-21 | 活跃 | 面向小说推文与 AI 短剧的桌面 AIGC 创作软件，覆盖文案分镜到成片发布全链路 |
| [`xhongc/ai_story`](https://github.com/xhongc/ai_story) | 1,653 | 未标注 | 2026-09-17 | 活跃 | AI 驱动的故事视频自动化平台，输入主题即自动完成文案、分镜、图片、运镜到视频全流程 |
| [`freestylefly/director_ai`](https://github.com/freestylefly/director_ai) | 1,768 | 未标注 | 2026-05-01 | 半活跃 | 未验证 |
| [`morsoli/aimangastudio`](https://github.com/morsoli/aimangastudio) | 1,479 | 未标注 | 2026-08-23 | 活跃 | AI 漫画创作工具，端到端覆盖剧情脚本、分镜排版、角色风格设定与多页导出 |
| [`alecm20/story-flicks`](https://github.com/alecm20/story-flicks) | 2,532 | 未标注 | 2025-03-12 | 已停更 | 输入故事主题即由大模型生成含 AI 图片、故事文案、配音与字幕的故事视频 |
| [`BidingCC/BuildingAI`](https://github.com/BidingCC/BuildingAI) | 1,895 | Apache-2.0 | 2026-08-21 | 活跃 | 企业级开源智能体平台，可视化零代码搭建带知识库、MCP 与计费的 AI 应用 |
| [`jmilinovich/comicgen`](https://github.com/jmilinovich/comicgen) | 1 | 未标注 | 2025-12-15 | 半活跃 | 用 Gemini 生成漫画页的 Python 工具，按项目与期刊组织合订 PDF |


### 方向 3 ComfyUI 工作流编排/管理

我们最大的工程风险在「单卡上把成百上千个镜头排队跑完且不重复烧 GPU」。这一族的答案分两类：**工作流/模板管理**（Manager、Workspace Manager、SwarmUI）与**队列/执行后端**（SmartQueue、comfy-executors、ComfyScript），另有 H3 专用的跨段衔接节点。

#### 代表项目

#### `Comfy-Org/ComfyUI-Manager` — ⭐1.6万 · GPL-3.0 · 最近推送 2026-09-23 · 活跃（原名 `ltdrdata/ComfyUI-Manager`）
- **定位**：ComfyUI 节点与模型安装更新、快照恢复与安全管理的扩展。
- **核心功能**：（1）一键安装、卸载、启用、禁用自定义节点与模型，内置节点/模型数据库列表与缺失节点检测；（2）三种 DB 模式：Channel 一天缓存、Local 本地库、Channel 远程，并对接官方注册表；（3）快照管理：Update All 或手动 Save snapshot 保存当前安装状态，重启后经 startup-scripts 还原；（4）安全分级 security_level 与 allow_git_url_install / allow_pip_install 独立安装开关；（5）cm-cli 命令行工具，不启动 ComfyUI 也能用 Manager 功能；（6）组件复制粘贴与拖放 .pack/.json，支持把工作流分享到 comfyworkflows、openart 等平台
- **最该学**：用快照加清单把安装环境固化成可还原状态，并给高风险安装动作分级门禁。
- **技术栈/兼容性**：Python（ComfyUI 扩展）、JavaScript 前端、JSON 节点数据库、pip/uv/git；纯本地开源（安装更新要联网）；可复用度 **高：直接管理 Python 依赖与节点包，环境治理可复用**

#### `mcmonkeyprojects/SwarmUI` — ⭐4,595 · MIT · 最近推送 2026-09-23 · 活跃
- **定位**：模块化 AI 图像与视频生成 WebUI，多 GPU 并行出图，易用且可扩展。
- **核心功能**：（1）Generate 标签提供参数化生成表单，Comfy Workflow 标签保留原始图给高级用户自由编辑；（2）多 GPU「swarm」：多张显卡同时为同一用户生成，README 明确针对大网格批量出图；（3）可自动安装并以 ComfyUI 为后端，也支持 A1111 后端；支持 Flux、Wan、LTX-2 等图像视频模型；（4）自动工作流生成、内置图像编辑器、Grid Generator 等效率工具，前端支持 extension 扩展；（5）跨平台安装：Windows/Linux/Mac/Docker/Colab，另有 Runpod、Vast.ai 云 GPU 模板；（6）可自动安装 YOLOv8 人脸检测、IP Adapter Face（insightface）以及审美/评分模型
- **最该学**：参数化前端自动生成 ComfyUI 图，把复杂图变成易用表单加工具集。
- **技术栈/兼容性**：C#/.NET 8 后端、JavaScript/HTML 前端、Python（ComfyUI 后端）；纯本地开源（另有云 GPU 模板）；可复用度 **中：主体为 .NET，Python 仅作 ComfyUI 后端被调用**

#### `11cafe/comfyui-workspace-manager` — ⭐1,453 · MIT · 最近推送 2025-04-16 · 已停更
- **定位**：集中管理工作流、模型与生成图的工作区扩展，已停止维护。
- **核心功能**：（1）工作流管理：一键切换、文件夹分类、拖放插入子工作流、批量导入与打包 zip 下载；（2）版本历史：每次保存生成一条版本记录，可像 git 一样回退切换，自动保存可开关；（3）与本地 /ComfyUI/my_workflows 双向同步，用系统文件管理器移动文件也会同步；（4）模型管理：从 civitai 一键装到对应 models 子目录，缩略图浏览，可拖入 Load 节点；（5）图像/视频画廊：生成结果按当前工作流归档，可把任意一张设为工作流封面；（6）元数据主要存浏览器 IndexedDB 并备份到扩展目录 db/，换浏览器会看不到旧元数据
- **最该学**：把工作流当代码来管：版本历史加本地目录双向同步。
- **技术栈/兼容性**：JavaScript/React 前端（npm 构建）、Python（ComfyUI 扩展）、IndexedDB；两者都支持（装模型靠 civitai）；可复用度 **中：后端仅作扩展宿主，主要逻辑在前端**

#### `Chaoses-Ib/ComfyScript` — ⭐704 · MIT · 最近推送 2026-07-18 · 活跃
- **定位**：把 ComfyUI 工作流转译成 Python 代码并运行的运行时库。
- **核心功能**：（1）作为 ComfyUI 工作流的可读格式，方便比较与复用片段，也便于训练 LLM 生成工作流；（2）Transpiler 把 ComfyUI 工作流转译成 Python 脚本，并随 SaveImage 写入图片元数据；（3）Runtime 让脚本直接出图，可跑本地或远程 ComfyUI 服务器，异步队列并支持取消；（4）Real mode 把 ComfyUI 节点当函数库用，做 ML 研究、复用节点、调试与缓存优化；（5）反向生成 ComfyUI 工作流，可用循环拼出巨大图；也能检索工作流信息并转换 web UI 与 API 格式；（6）加载后自动生成类型存根与枚举便于补全，另带 ipywidgets 与 Solara UI 组件
- **最该学**：用真正的 Python 代码替代节点图，循环、复用与 LLM 生成都成为可能。
- **技术栈/兼容性**：Python、ComfyUI 节点运行时、Jupyter/ipywidgets/Solara；纯本地开源（可连远程服务）；可复用度 **高：纯 Python 库，可直接编排 ComfyUI 节点**

#### `Infinishot/comfy-executors` — ⭐0 · 未标注 · 最近推送 2024-11-08 · 已停更
- **定位**：在本地或云 GPU 上运行 ComfyUI 工作流的 Python 执行器库。
- **核心功能**：（1）统一执行后端：本地或远程 ComfyUI 服务器，以及 RunPod、Modal 无服务器 GPU；（2）用 Jinja 模板参数化 workflow.json，可注入 prompt、input_images_dir、batch_size 等变量；（3）异步提交与迭代：submit_workflow_async 以异步方式流式返回 WorkflowOutputImage；（4）透明批处理：executor 可配置 batch_size，num_samples 决定单个作业内跑多少批；（5）结果对象携带输出图像、ComfyUI 设定的文件名与原始子目录，多批次同作业复用节点缓存；（6）使用前需在 ComfyUI 开发者设置中启用并导出 API 格式的工作流
- **最该学**：用 Jinja 模板把工作流参数化并做透明批处理，跨本地与云后端复用。
- **技术栈/兼容性**：Python 库（pip 安装）、Jinja2 模板、异步 IO、comfy_api_client；两者都支持（RunPod/Modal）；可复用度 **高：纯 Python 库，参数化与批处理调度可直接复用**

#### `light-and-ray/Minimalistic-Comfy-Wrapper-WebUI` — ⭐140 · AGPL-3.0 · 最近推送 2026-09-23 · 活跃
- **定位**：把 ComfyUI 工作流按节点标题映射成极简推理界面的 WebUI 扩展。
- **核心功能**：（1）给节点标题加 <名称:类别:序号> 标记即可生成推理表单，改完点 Refresh 立即生效；（2）与 ComfyUI 共用同一批工作流，无需复制或导出 api 格式，Comfy 里改图后刷新即同步；（3）增强队列：可改任务顺序与优先级、暂停恢复，关闭 ComfyUI 或重启电脑都不丢任务；（4）Prompt 预设一键取用；批量支持媒体输入匹配、批量预设与 batch count（seed）；（5）内置极简图像编辑器：可加视觉提示、裁剪旋转；移动端可用，支持装成 PWA 走局域网；（6）所有操作状态存浏览器本地存储，刷新或关闭页面都不丢，仅在项目更新时重置
- **最该学**：用节点标题当声明式界面 schema，零导出把任意工作流转成表单。
- **技术栈/兼容性**：Python（ComfyUI 扩展/独立服务器）、JavaScript 前端（PWA）；纯本地开源；可复用度 **中：Python 侧为扩展与服务器，界面逻辑主要在前端**

#### `CraftopiaStudio/ComfyUI-SmartQueue` — ⭐1 · MIT · 最近推送 2026-09-14 · 活跃
- **定位**：ComfyUI 的 GPU 感知队列自动驾驶扩展，持久化队列并按温控节流。
- **核心功能**：（1）三条独立且默认关闭的自动驾驶规则：温度过高、显存不足、连续作业超限时暂停，带滞回防抖动；（2）手动暂停：把未开始任务移出 ComfyUI 队列并按原顺序放回，重启 ComfyUI 后依然生效；（3）侧边栏持久队列与历史：任务名、缩略图、单任务时长、实时 GPU 温度/显存，可搜索重命名；（4）批量操作与拖拽排序：多选右键重命名/取消/取消并重排，拖拽会真实改变执行顺序；（5）Smart Cooldown & Pause 节点：固定延迟、等温、卸载模型清显存、wait_for_click 人工确认闸门；（6）状态存 user 目录下 SQLite，NVML 读 GPU 指标，fail-open 容错，配 230 个单元测试
- **最该学**：用带滞回阈值的 GPU 自动驾驶守护长批量渲染，fail-open 绝不卡住队列。
- **技术栈/兼容性**：Python（ComfyUI 扩展 + aiohttp 中间件）、JavaScript 前端、SQLite、nvidia-ml-py/NVML；纯本地开源；可复用度 **高：纯 Python 扩展，队列持久化与节流规则可借鉴**

#### `NikoDemon80/ComfyUI-H3-Motion-Context` — ⭐1,006 · GPL-3.0 · 最近推送 2026-09-06 · 活跃
- **定位**：ComfyUI 插件，把 MiniMax H3 视频片段首尾串成运动与声音连续的长链
- **核心功能**：（1）从上一段 clip 的 latent 直接切出尾帧作为锚点，避免解码再编码的像素往返；（2）把已播放的音频尾部钉在新 clip 的时间轴上，实现音轨真正延续而非另起一段；（3）Trim 节点按 trim_frames 切掉被钉住的头部，画面与声音一起交付；（4）Save/Load Latent 节点以 clip_00002.safetensors 编号文件跨运行传递片段；（5）Chain 节点循环生成：Run/Re-roll 重生成当前片段，Approve 推进索引，segments 控制条数；（6）Seam Probe 与 seam_probe/level_step/freeze_detect 脚本机械测量接缝相关性、响度跳变与定格
- **最该学**：用 latent 切片替代像素往返、把音频坐标重写进新时间轴，实现无缝合链
- **技术栈/兼容性**：Python（ComfyUI 自定义节点）+ numpy 测试；需 ComfyUI 0.34.0+ 与 MiniMax H3；纯本地开源；可复用度 **高 - latent 级续接与接缝质检脚本可直接移植借鉴**

#### 同向速览（未展开卡片）

| 项目 | ⭐ | 许可证 | 最近推送 | 状态 | 一句话定位 |
|---|---:|---|---|---|---|
| [`RmaNMetaverse/ComfyUI-Orchestrator-LAN`](https://github.com/RmaNMetaverse/ComfyUI-Orchestrator-LAN) | 3 | 未标注 | 2026-08-16 | 活跃 | 把 ComfyUI 工作流分发到局域网多台 GPU 并集中回收产物的编排器。 |
| [`pydn/ComfyUI-to-Python-Extension`](https://github.com/pydn/ComfyUI-to-Python-Extension) | 2,383 | MIT | 2026-05-10 | 半活跃 | 把 ComfyUI 可视化工作流一键导出为可直接运行的 Python 脚本 |
| [`6174/comflowyspace`](https://github.com/6174/comflowyspace) | 2,347 | 自定义(未标准) | 2024-08-30 | 已停更 | 面向 AI 图像与视频生成的开源桌面工具，宣称交互体验优于 SDWebUI 与 ComfyUI |
| [`ATH-MaaS/ComfyUI-Copilot`](https://github.com/ATH-MaaS/ComfyUI-Copilot) | 5,526 | MIT | 2026-09-11 | 活跃 | ComfyUI 的 LLM 智能助手，覆盖工作流生成、调试、改写与参数调优 |
| [`willmiao/ComfyUI-Lora-Manager`](https://github.com/willmiao/ComfyUI-Lora-Manager) | 1,480 | GPL-3.0 | 2026-09-23 | 活跃 | ComfyUI 模型管理器：LoRA/Checkpoint 的整理、下载、配方与一键套用 |
| [`zanllp/infinite-image-browsing`](https://github.com/zanllp/infinite-image-browsing) | 1,350 | MIT | 2026-08-22 | 活跃 | 本地 AI 出图/出片浏览器：索引检索、标签分析与多平台元数据解析 |


### 方向 4 AI 分镜/故事板工具

分镜是漫剧流水线的中枢产物。这一族里既有传统故事板工具（storyboarder），也有把分镜当**契约/单一事实源**的（Slate 的 storyboard.json、story-shot-agent 的结构化 JSON 输出），还有纯 Skill（无代码）方案。

#### 代表项目

#### `coracoo/Slate` — ⭐13 · 未标注 · 最近推送 2026-09-21 · 活跃
- **定位**：面向短片制作与拉片分析的本地 Web 工作台，剧本分镜到集视频一体化管理
- **核心功能**：（1）制作线：剧本生成→分镜生成→素材生成→音色绑定→演员表现→平面推演→创作生成；（2）三层视频：S 转场镜头、V 分镜视频、E 集视频，支持关键帧采用与尾帧续接；（3）拉片线：影片解构、台词 OCR/ASR、逐帧提取、深度动作与镜头讲解；（4）生成入口：本地 ComfyUI、ChatGPT 网页生图与已适配云端 API 统一排队；（5）后台任务：异步提交轮询、产出记录、失败原因与顶部通知，不盲目重发；（6）本地资产：人物、场景、道具、角色音色、音乐、关键帧和视频集中管理
- **最该学**：用分镜 JSON 契约与三层视频结构，把从分镜到集视频的链路管住
- **技术栈/兼容性**：Python 3.12(uv 固定依赖) + Vue 前端 + FFmpeg；ComfyUI 与 Chrome 扩展；Windows 本机部署；两者都支持（本地 ComfyUI 与云端 API 均可）；可复用度 **高：Python 后端与任务、厂商适配层，契约设计可参考**

#### `neopen/story-shot-agent` — ⭐203 · MIT · 最近推送 2026-09-23 · 活跃
- **定位**：LangGraph 多智能体剧本转分镜系统，输出镜头级双语提示词并保障跨片段连续
- **核心功能**：（1）智能剧本解析：自动识别场景、对话与动作指令，支持长文本分段处理；（2）精准时序规划：按镜头粒度切分内容并分配时长，适配视频模型时长限制；（3）提示词输出：生成中英双语画面描述、负面提示词与配套音频提示词；（4）连续性守护：三级记忆池加 Chroma 向量检索，保持角色、场景与剧情一致；（5）质量审计与修复：质量审计智能体、循环检查、错误处理与人工干预节点；（6）多协议集成：Python SDK、REST API、LangGraph 节点、A2A 与 MCP 服务
- **最该学**：多智能体流水线加三级记忆与向量检索，把连续性和可追溯做成机制
- **技术栈/兼容性**：Python 3.10+；LangChain + LangGraph、LlamaIndex、Chroma、Redis、FastAPI、Docker；两者都支持（可接 Ollama 本地，也支持云端 LLM API）；可复用度 **高：纯 Python 包，附 SDK 与 MCP 示例，可直接集成**

#### `gzxx-2025/aid-studio` — ⭐577 · MIT · 最近推送 2026-09-23 · 活跃
- **定位**：开源可自部署的 AI 漫剧与短剧创作平台，串起剧本、分镜、视频与配音全流程
- **核心功能**：（1）剧本与分集管理：以项目/剧本/分集组织内容，AI 辅助创作并提取场景资产；（2）分镜工作台：生成分镜脚本与分镜图，拆分镜头组并产出视频提示词；（3）图像与视频生成：文生图/图生图/多图融合，图生视频、首尾帧、多镜头批量出片；（4）配音合成：TTS 多音色、音色库管理与对口型，角色可绑定专属配音；（5）多厂商编排：文本/图片/视频/语音统一 Provider 接入，能力与计费按模型配置；（6）统一任务系统：排队、并发调度、进度推送、失败重试、补偿与结果回收
- **最该学**：统一任务系统与多厂商 Provider 编排，把排队重试与媒体生成解耦
- **技术栈/兼容性**：Java 17 + Spring Boot 3.5 + MyBatis-Plus + MySQL 5.7 + Redis + RocketMQ；React 前端；Go 升级器；两者都支持（平台自部署，生成依赖所配模型厂商）；可复用度 **低：Java/Spring 单体后端，非 Python 生态**

#### `c-wang-dev/storyboard-master` — ⭐1 · MIT · 最近推送 2026-08-25 · 活跃
- **定位**：知识库驱动的决策端分镜 Agent，把剧本转成可解释可评测的生图生视频提示词包
- **核心功能**：（1）确定性决策引擎：六维特征经五张决策表与冲突仲裁查表，同样输入同样输出；（2）领域知识库：32 份结构化 Markdown 覆盖镜头语言、表演、视觉风格与 6 张模型卡；（3）多模型分流：选定生图或生视频模型后死锁对应模型卡，杜绝参数污染；（4）表演一致性四层：性格范式库、角色锚定卡、禁止清单与远景体态降级；（5）跨片段一致性：角色绑定表别名归一加定妆照、场景设定图双参考图；（6）质量评测闭环：九要素帧级检测、双轨评测、成本台账与废片归因
- **最该学**：决策纯规则可回归加九要素质检闭环，让分镜输出可解释可量化
- **技术栈/兼容性**：Python 标准库规则引擎(零第三方依赖)；Markdown/Obsidian 双链知识库；多模型 API；ffmpeg；两者都支持（可 --no-llm 离线，LLM 可选）；可复用度 **高：零依赖 Python 引擎与 18 个单元测试，规则层可直接借鉴**

#### `wonderunit/storyboarder` — ⭐3,855 · 未标注 · 最近推送 2024-03-17 · 已停更
- **定位**：手绘分镜桌面工具，用鼠标或数位板快速画板并回放动画预览故事
- **核心功能**：（1）极简绘图：4 种笔刷各 3 档粗细与 5 色板，支持直线、整体移动与缩放；（2）动画预览：按顺序翻页回放 boards 形成 animatic，可向他人演示提案；（3）剧本对接：可直接打开 Fountain 格式剧本开始画板；（4）辅助绘制：参考图层、洋葱皮与网格、中心线、三分、角度等 5 种叠加；（5）外部编辑：一键在 Photoshop 中打开编辑，保存后自动回写更新；（6）导出与打印：打印工作表、导出多种格式，并可测量线条里程
- **最该学**：刻意不做图层的极简设计加洋葱皮与参考层，让画分镜几乎零学习成本
- **技术栈/兼容性**：README 未说明技术栈；为可下载的本地手绘桌面应用，支持 Wacom/数位板；纯本地开源（手绘工具，无生成式 API）；可复用度 **低：手绘桌面应用，与 Python 生成管线无复用点**

#### `RhythmicWave/AICreation` — ⭐113 · MIT · 最近推送 2025-10-20 · 半活跃
- **定位**：本地部署的 AI 小说转漫剧系统，从文本创作、角色提取到分镜图片与视频合成
- **核心功能**：（1）文本创作：支持直接生成与续写两种模式，由 LLM 产出小说文本；（2）角色与场景提取：AI 分析文本提取实体，建立实体库并生成或上传参考图；（3）章节分割与分镜：自动提取场景、切分镜并把实体引用写入提示词；（4）图像生成：集成 ComfyUI，参考图像模式配合 Flux Kontext 多图工作流；（5）音频生成：集成 EdgeTTS，对选中的分镜片段生成配音；（6）视频合成：按分镜图做横向或纵向平移生成视频，可选 NVENC 硬编码加速
- **最该学**：用角色与场景参考图配合 Flux Kontext 多图工作流维持分镜画面一致性
- **技术栈/兼容性**：Python FastAPI + LangChain；Vue3/TypeScript/Element Plus；ComfyUI(Flux Kontext/nunchaku)、EdgeTTS；两者都支持（图像本地 ComfyUI，LLM 需兼容 API）；可复用度 **高：Python FastAPI 后端，Pydantic 与 JSON Schema 可直接借鉴**

#### 同向速览（未展开卡片）

| 项目 | ⭐ | 许可证 | 最近推送 | 状态 | 一句话定位 |
|---|---:|---|---|---|---|
| [`eternityspring/shuohao-skills`](https://github.com/eternityspring/shuohao-skills) | 3,730 | Apache-2.0 | 2026-09-14 | 活跃 | AI 短剧制作 skill 集合：从小说拆角色、排大纲、出设定、写剧本到切分镜 |
| [`zenstory-ai/drama-skills`](https://github.com/zenstory-ai/drama-skills) | 2,216 | MIT | 2026-09-18 | 活跃 | 面向编剧与漫剧工作室的 11 个 AI 短剧技能，从原著到分集剧本与成片 |
| [`liangdabiao/Seedance2-Storyboard-Generator`](https://github.com/liangdabiao/Seedance2-Storyboard-Generator) | 2,441 | 未标注 | 2026-09-21 | 活跃 | 基于 Skill 与 Seedance 2.0 的 AI 多集短视频生产工作流 |


### 方向 5 角色一致性/换装

角色不漂移是漫剧的生命线。研究层面（InstantID / IP-Adapter / PuLID / PhotoMaker）已成熟，我们要做的是**工程化封装**：定妆照→参考图注入→跨镜复用→多形态管理。注意这几个项目近期多数已停更。

#### 代表项目

#### `instantX-research/InstantID` — ⭐1.2万 · Apache-2.0 · 最近推送 2024-07-18 · 已停更
- **定位**：基于单张人脸图的零样本身份保持生成方法，免微调即可在 SDXL 上还原人物身份。
- **核心功能**：（1）单张参考人脸零样本生成，免针对人物微调，官方称 tuning-free 并支持多种下游任务；（2）用 InsightFace antelopev2 提取人脸 embedding 与关键点，只取画面中最大人脸作参考；（3）IdentityNet（ControlNet 关键点条件）与 ip-adapter 双权重可调，平衡相似度与文字可控性；（4）兼容 LCM-LoRA 加速，支持 enable_model_cpu_offload 与 enable_vae_tiling 降低显存；（5）提供 infer.py / infer_full.py 推理脚本与本地 Gradio demo（含 MultiControlNet 版本）；（6）适配 Kolors 底模版本（发布时仍在训练中），可同时生成 ID 与文字
- **最该学**：单图免训练就拿到高保真人脸身份，ID embedding 加关键点双条件解耦的做法最值得学。
- **技术栈/兼容性**：Python / PyTorch / diffusers / InsightFace / OpenCV / Gradio；纯本地开源（权重需自行从 HuggingFace 下载）；可复用度 **高：diffusers 管线与纯 Python 推理脚本可直接嵌入自研流程。**

#### `tencent-ailab/IP-Adapter` — ⭐6,693 · Apache-2.0 · 最近推送 2024-06-28 · 已停更
- **定位**：轻量即插即用的图像提示适配器，仅 22M 参数为文生图扩散模型补上图像提示能力。
- **核心功能**：（1）仅 22M 参数的适配器，为预训练文生图模型加图像提示，效果可比微调过的图像提示模型；（2）图像提示可与文本提示同时生效完成多模态生成，用 scale 调节两者权重；（3）提供 IP-Adapter-Plus 细粒度特征版与 face 人脸提示版，并扩展出 FaceID 系列；（4）可泛化到同底模微调的社区模型，也能与 ControlNet、T2I-Adapter 组合做结构控制；（5）支持 SD1.5 与 SDXL 1.0，附 image-to-image、inpainting 等示例 notebook；（6）开源训练代码（accelerate 多卡、两阶段训练策略）与权重转换脚本，可自训适配器
- **最该学**：用极小参数量把图像提示做成可插拔适配器、不动底模即可复用，这套轻量接入方式值得学。
- **技术栈/兼容性**：Python / PyTorch / diffusers / CLIP（OpenCLIP-ViT-H）/ accelerate / Jupyter；纯本地开源；可复用度 **高：pip 安装即用，notebook 与 diffusers 管线均为标准 Python 调用。**

#### `cubiq/ComfyUI_IPAdapter_plus` — ⭐6,136 · GPL-3.0 · 最近推送 2025-04-14 · 已停更
- **定位**：ComfyUI 的 IPAdapter 参考实现节点，用参考图迁移主体或风格。
- **核心功能**：（1）作为 ComfyUI 参考实现，提供 IPAdapter 全部功能节点与 examples 目录示例工作流；（2）统一模型加载器按文件名自动识别模型类型，legacy 加载器可用任意文件名手动选择；（3）覆盖基础/Plus/Plus-Face/light 等 SD1.5 与 SDXL 权重，以及 bigG / ViT-H 视觉编码器；（4）支持 FaceID 系列模型，按命名约定自动加载配套 LoRA，需在环境内安装 insightface；（5）兼容 Kolors 与社区 composition 模型，可经 extra_model_paths.yaml 指向自定义模型目录；（6）IPAdapter Advanced 节点可调 weight 与 weight type，并给出降权重、加步数的调参建议
- **最该学**：把多套 IPAdapter 权重收敛成统一加载器加命名约定、免手工配对，模型分发设计值得学。
- **技术栈/兼容性**：Python（ComfyUI 自定义节点）/ PyTorch / insightface；纯本地开源；可复用度 **中：以 ComfyUI 节点形式使用，非独立 Python 库，可借鉴其节点与加载器设计。**

#### `ToTheBeginning/PuLID` — ⭐3,552 · Apache-2.0 · 最近推送 2025-07-31 · 已停更
- **定位**：对比对齐实现的纯身份定制方法，免微调在 SDXL 与 FLUX 上保人脸 ID。
- **核心功能**：（1）免微调身份定制，提供 SDXL 版 v1/v1.1 与 FLUX 版 v0.9.0/v0.9.1 共四组权重；（2）核心用对比对齐（contrastive alignment），提升 ID 相似度的同时保留提示词可编辑性；（3）提供本地 Gradio demo：app.py 跑 v1，app_v1.1.py --base 指定底模跑 v1.1；（4）PuLID-FLUX 优化后可在 16GB 显卡运行，本地 Gradio demo 已支持 12GB 显卡；（5）提供 requirements_fp8.txt 支持 flux-fp8 部署，把 FLUX 放到消费级 GPU；（6）README 汇总 ComfyUI、WebUI、Colab、Replicate 等第三方实现与在线 demo
- **最该学**：提升 ID 相似度又不牺牲提示词可编辑性，用对比对齐解耦身份与文本控制的做法值得学。
- **技术栈/兼容性**：Python / PyTorch(>=2.0，fp8 需 >=2.4.1) / diffusers / Gradio / Conda；纯本地开源（权重需自行从 HuggingFace 下载）；可复用度 **高：提供可直接运行的推理与 Gradio 脚本，便于集成进自研管线。**

#### `TencentARC/PhotoMaker` — ⭐1.0万 · 自定义(未标准) · 最近推送 2024-10-31 · 已停更
- **定位**：堆叠 ID embedding 定制真人照片的免训练方法，秒级生成且身份保真。
- **核心功能**：（1）免额外 LoRA 训练，秒级完成人物定制，兼顾 ID 保真、多样性与文字可控性；（2）把多张输入 ID 图片堆叠为 ID embedding，上传更多人物照片可提升 ID 保真度；（3）可作 Adapter 与社区底模及其他 LoRA 组合，用触发词 img 配合类别词完成定制；（4）V2 提供与 ControlNet、T2I-Adapter、IP-Adapter 集成的推理脚本，可叠加 LCM 加速；（5）提供 notebook demo、本地 Gradio demo，以及风格化 Style 版本示例；（6）最低 11G 显存即可运行；不支持 bfloat16 的 GPU 改用 float16 可大幅提速
- **最该学**：把多张参考图堆叠成 ID embedding 并做成可插拔 Adapter，这条免训练定制路径值得学。
- **技术栈/兼容性**：Python / PyTorch / diffusers / SDXL / Gradio；纯本地开源（权重自动从 HuggingFace 下载或手动下载）；可复用度 **高：PhotoMakerStableDiffusionXLPipeline 可直接 import 到 Python 代码中调用。**

#### `18yz153/ComfyUI-Persona-Director` — ⭐20 · Apache-2.0 · 最近推送 2026-08-30 · 活跃
- **定位**：面向一致性角色的 ComfyUI 状态机节点，用 LLM 维护身份与服装状态。
- **核心功能**：（1）configs/*.json 驱动的确定性状态机：身份、服装、动作、场景、构图、风格各为一个 JSON 字段；（2）LLM 用 function calling 只输出发生变化的字段并按 JSON schema 校验，不再重写整段状态；（3）低能力或本地模型不支持工具调用时自动回退 JSON echo，也可用 tool_mode 手动切换；（4）状态保存为 .json 文件，可暂停后隔日恢复继续，角色文件可刷新后重新选择加载；（5）两个节点分别输出 Danbooru/SDXL 标签与自然语言，适配 Pony、Anima、FLUX、SD3；（6）支持 tag:(...) 语法强制注入标签；走 OpenAI 兼容协议，可接闭源 API 或本地 LLM
- **最该学**：用 schema 化角色状态替代整段提示词重写，改姿势不丢服装，状态机加函数调用值得学。
- **技术栈/兼容性**：Python（ComfyUI 自定义节点）/ openai SDK / JSON Schema；两者都支持（本地端点或闭源 API）；可复用度 **中：以 ComfyUI 节点使用，但状态机与 schema 设计可移植到 Python 流程。**

#### 同向速览（未展开卡片）

| 项目 | ⭐ | 许可证 | 最近推送 | 状态 | 一句话定位 |
|---|---:|---|---|---|---|
| [`cubiq/ComfyUI_InstantID`](https://github.com/cubiq/ComfyUI_InstantID) | 1,842 | Apache-2.0 | 2025-04-14 | 已停更 | ComfyUI 原生 InstantID 节点，单张参考图保持人脸身份 |
| [`Gourieff/ComfyUI-ReActor`](https://github.com/Gourieff/ComfyUI-ReActor) | 1,367 | GPL-3.0 | 2026-09-21 | 活跃 | ComfyUI 快速换脸节点扩展，本地完成图像/视频换脸、遮罩与面部修复 |
| [`diodiogod/TTS-Audio-Suite`](https://github.com/diodiogod/TTS-Audio-Suite) | 1,211 | 自定义(未标准) | 2026-09-19 | 活跃 | ComfyUI 多引擎 TTS 扩展，统一 19 种语音引擎与 SRT 工作流 |
| [`debpalash/VoiceStudio`](https://github.com/debpalash/VoiceStudio) | 3.5万 | AGPL-3.0 | 2026-09-23 | 活跃 | 本地优先的开源语音克隆、设计、配音与转录桌面工具，支持 646 种语言 |


### 方向 6 AI 视频编辑/NLE

结论先给：**不要自研 NLE**。这一族里 star 最高的（OpenCut 9.1 万）也不是为生成流程设计的。我们真正需要的是「合成 + 字幕 + 简单转场」的可编程层（moviepy / auto-editor / ffmpeg）与「导出给剪映精修」。

#### 代表项目

#### `OpenCut-app/OpenCut` — ⭐9.1万 · MIT · 最近推送 2026-08-10 · 活跃
- **定位**：免费开源的跨端视频编辑器，正用 Rust 核心彻底重写
- **核心功能**：（1）免费开源视频编辑器，目标覆盖网页、桌面与移动端，统一 Rust 核心；（2）规划 Editor API 与插件优先架构，允许第三方插件扩展编辑器；（3）规划 MCP server，让 AI agent 接入编辑器操作；（4）规划 headless 模式，用于自动化与批量渲染；（5）规划编辑器内置脚本标签页（scripting tab）；（6）当前处于重写阶段，旧版为 opencut-classic，官网仍运行旧版
- **最该学**：以 Rust 核心 + Editor API + MCP 为首要目标，值得学其 API 化与插件化规划
- **技术栈/兼容性**：Rust 核心 + Web/桌面/移动多端；构建用 proto/moon（README 未列前端框架）；纯本地开源 — MIT 许可，README 未提及云端依赖；可复用度 **低 — 重写中，无 Python 接口，仅未来 MCP 可间接接入**

#### `0xsline/OpenChatCut` — ⭐1,979 · AGPL-3.0 · 最近推送 2026-09-21 · 活跃
- **定位**：开源 ChatCut 替代：本地优先、agent 原生的多轨 AI 视频编辑器
- **核心功能**：（1）多轨时间轴：移动、裁剪、分割、波纹编辑、吸附、关键帧、撤销重做；（2）转录驱动剪辑：词级转写、文本剪口、停顿压缩、说话人、联动字幕；（3）内置 Agent 与外部 MCP（Codex/Claude Code/Qoder）共用同一套编辑工具；（4）AI 生成图片、视频、语音、音乐、音效，生成任务带进度跟踪；（5）WebGL 特效、LUT、抠像、变焦、转场与自定义着色器；（6）导出 MP4/音频/字幕/FCPXML 与完整项目，含导出队列与硬件加速
- **最该学**：编辑写回真实可编辑时间轴，Agent 改动走草稿加人工审批且可撤销
- **技术栈/兼容性**：TypeScript / React 19 / Vite / Electron 43 / Remotion / WebGL / FFmpeg / MCP；两者都支持 — 时间轴本地，AI 生成与转写需自配第三方 API key（BYOK）；可复用度 **低 — 全 TS/Electron 实现，仅通过 MCP 供外部 agent 调用**

#### `pireel/pireel` — ⭐1,226 · AGPL-3.0 · 最近推送 2026-09-18 · 活跃
- **定位**：面向人类与 AI agent 的开源本地 AI 视频剪辑工作台
- **核心功能**：（1）画布与时间轴直接手工剪辑，导入本地媒体后开始编辑；（2）对话式剪辑：用文字描述剪口、节奏、字幕、版式与视觉想法；（3）通过 pireel-agent 插件接入 Codex、Claude Code 等外部 AI agent；（4）用可复用的剪辑 Skills 与视觉 Frames 塑造视频；（5）同一素材产出多平台、多格式、多受众的多个版本；（6）开源版专注本地剪辑，内置 Chat 与连接服务属托管版
- **最该学**：Studio 手剪、Chat 对话、外部 Agent 三种方式自由组合，人保留终审
- **技术栈/兼容性**：TypeScript / Node.js（pnpm；README 未指明具体前端框架）；两者都支持 — 开源版本地剪辑，内置 Chat 与连接服务在托管版 pireel.com；可复用度 **低 — TS 前端编辑器，无 Python 接口，仅经 agent 插件间接协作**

#### `remotion-dev/remotion` — ⭐6.0万 · 自定义(未标准) · 最近推送 2026-09-23 · 活跃
- **定位**：用 React 代码编程生成视频的框架，覆盖本地与云端渲染
- **核心功能**：（1）以 React 代码为唯一真源，用数据与代码组合管理复杂视频；（2）三种工作流随时切换：agent 生成、拖拽交互编辑、程序化编写；（3）批量渲染：可在自有基础设施上渲染海量视频并复用设计系统资产；（4）多种渲染入口：Node.js SSR API、Lambda、Vercel Sandbox 与客户端渲染；（5）提供 Elements、Effects、Transitions、Captions、SFX、字体等组件；（6）Player 与 Editor Starter、Mediabunny 支持自建视频编辑器与应用
- **最该学**：把视频变成可编程 React 组件，天然数据驱动且可百万级批量渲染
- **技术栈/兼容性**：TypeScript / React / Node.js；可部署到 AWS Lambda、Vercel；两者都支持 — 本地 Node/浏览器渲染，Lambda/Vercel 为云渲染；许可需注意；可复用度 **低 — React/Node 生态，Python 只能经 Node 渲染器间接调用**

#### `Zulko/moviepy` — ⭐1.5万 · MIT · 最近推送 2026-08-26 · 活跃
- **定位**：Python 视频编辑库，做剪辑、拼接、合成与自定义特效
- **核心功能**：（1）链式剪辑 API：subclipped、with_volume_scaled 等完成剪切与音量处理；（2）把视频帧与声音转成 numpy 数组，任意像素可编程，特效几行代码可写；（3）CompositeVideoClip/TextClip 实现标题插入与非线性视频合成；（4）支持读写 GIF 及常见音视频格式，跨 Windows/Mac/Linux，Python 3.9+；（5）底层用 FFmpeg 编解码，灵活但比直接调 ffmpeg 慢，因数据导入导出开销；（6）v2.0 引入破坏性变更，v1 文档不再维护，并提供 v1 升级指南
- **最该学**：把每一帧暴露为 numpy 数组，让视频特效像写数组运算一样可编程
- **技术栈/兼容性**：Python 3.9+，依赖 NumPy 与 FFmpeg；纯本地开源 — MIT 许可，本地 FFmpeg 编解码；可复用度 **高 — 原生 Python 库，可直接嵌入 Python 流水线做合成与特效**

#### `WyattBlue/auto-editor` — ⭐5,341 · Unlicense · 最近推送 2026-09-19 · 活跃
- **定位**：命令行自动粗剪工具，按音频响度或画面运动剪掉静音与静止段
- **核心功能**：（1）`--edit` 选自动剪口方法：audio 响度阈值/dB、motion 运动量，可 or/and 组合；（2）`--margin` 给剪点前后加留白（如 0.2s 或 0.3s,1.5s）调节成片节奏；（3）标签体系：0 静音、1 有效，可扩到 255 类并用 --when:N 执行加速等动作；（4）多轨可分别设阈值，如 --edit "(or audio:stream=0 audio:threshold=10%,stream=1)"；（5）导出时间线工程到 Premiere/Resolve/FCP/ShotCut/Kdenlive 或剪辑序列；（6）反向导出被剪掉的片段：--when-active cut --when-inactive nil
- **最该学**：用响度阈值一键完成第一遍粗剪，并把时间线导出给主流 NLE 继续精修
- **技术栈/兼容性**：Nim 编写的命令行应用，依赖 FFmpeg 生态；纯本地开源 — 仓库公共领域，官网在线/桌面版专有资产另授权；可复用度 **中 — 命令行可由 subprocess 调用，无 Python 绑定**

#### `chatman-media/timeline-studio` — ⭐217 · MIT · 最近推送 2026-07-01 · 活跃
- **定位**：Tauri 加 Next.js 的专业 AI 视频编辑器，一次上传产出多平台成片
- **核心功能**：（1）一次上传自动剪出 TikTok/YouTube/Instagram 等多平台成片并直传；（2）100+ AI 工具：时间轴创作、场景检测、质量分析、音乐同步、导出优化；（3）多 AI 提供商：Claude、OpenAI、DeepSeek 与本地 Ollama；（4）Rust 工作区提供 render-job、montage、publish 与 timeline CLI 无头入口；（5）GPU 硬件编码 NVENC/QuickSync/VideoToolbox，本地处理保隐私；（6）插件系统与 15 语言界面（含 RTL），MIT 加 Commons Clause 许可
- **最该学**：把一条素材自动裂变为多平台成片，用无头契约与 bot 流水线标准化生产
- **技术栈/兼容性**：Tauri v2 + Next.js 15/React 19 + TypeScript + Rust workspace + FFmpeg + XState；两者都支持 — 支持 Claude/OpenAI/DeepSeek 云 API，也可用 Ollama 本地模型；可复用度 **中 — 有 headless CLI 与 ProjectSchema，可由 Python 调度**

#### 同向速览（未展开卡片）

| 项目 | ⭐ | 许可证 | 最近推送 | 状态 | 一句话定位 |
|---|---:|---|---|---|---|
| [`palmier-io/palmier-pro`](https://github.com/palmier-io/palmier-pro) | 1.4万 | GPL-3.0 | 2026-09-23 | 活跃 | 面向 AI 的 macOS 原生视频编辑器，内置模型生成与 MCP 代理接入 |
| [`MartinDelophy/ai-video-editor`](https://github.com/MartinDelophy/ai-video-editor) | 855 | MIT | 2026-09-21 | 活跃 | 浏览器内本地优先的 AI 视频编辑器，多轨时间轴加 WebGPU 模型 |
| [`m1guelpf/auto-subtitle`](https://github.com/m1guelpf/auto-subtitle) | 2,283 | MIT | 2024-07-12 | 已停更 | 用 Whisper 与 ffmpeg 自动为视频生成并烧录字幕的命令行工具 |
| [`mageh21/video-editor-source-code`](https://github.com/mageh21/video-editor-source-code) | 5 | 自定义(未标准) | 2026-02-07 | 半活跃 | 纯浏览器运行的多轨视频编辑器，Remotion 预览加 wasm 导出 |
| [`hoangminhanhtai/openreel-video`](https://github.com/hoangminhanhtai/openreel-video) | 0 | MIT | 2026-05-07 | 半活跃 | 浏览器端开源 CapCut 替代，WebCodecs 加 WebGPU 剪辑 |


### 方向 7 LLM 长文拆解/剧本结构化

小说→镜头的结构化是整个流水线的地基。这一族里最有价值的不是更强的 LLM，而是**契约设计**：稳定 ID、版本化状态、append-only 记录、严格的输出 schema 与校验工具。

#### 代表项目

#### `siyuan-liu31/agentic-comic-drama-studio` — ⭐1 · 未标注 · 最近推送 2026-09-04 · 活跃
- **定位**：Codex 漫剧生产系统：用模板、资产注册表与校验工具维持长链路漫剧设定一致
- **核心功能**：（1）六阶段工作流：剧本审计→角色圣经与场景GEO→分镜与表演→能力画像→模型原生提示词→Take 日志与单变量修复；（2）Series Wiki、时间线、决策记录与剧集交接文档，让跨会话的长篇连载设定保持一致；（3）锁定角色身份、声音、表演、服装与状态描述符，剧集与提示词只引用稳定 ID 而不复制描述；（4）资产按复用范围分 shared/series/episode 三级存放，catalog.yaml 注册稳定 ID 与版本；（5）提供 manju_repo.py 的 validate、lint-prompt --strict、new-series 等命令与仓库单测；（6）规定每个模型生成片段必须为整数 5–15 秒，超长按换景、遮挡、角度或连续动作切分
- **最该学**：用单一权威来源加稳定资产 ID、只追加 Take 日志与单变量修复，把长链路一致性做成可校验工程
- **技术栈/兼容性**：Codex Skill/插件与 Markdown、YAML 模板为主，配 Python3 校验与脚手架脚本（manju_repo.py、unittest）；纯本地开源（仅含方法与模板，生成交由外部模型工具）；可复用度 **低：Python 仅用于仓库校验与脚手架脚本，无生成管线代码**

#### `appolloqin/huohuo-drama` — ⭐10 · 未标注 · 最近推送 2026-09-20 · 活跃
- **定位**：全栈开源 AI 短剧与小说平台：文本经剧本、分镜、素材到成片，含配音与逐镜混流。
- **核心功能**：（1）双流水线：每集可选 AI 图生视频，或用关键帧序列加 Ken Burns 运动做成帧幻灯，两者独立可并行。；（2）Digital Writer：服务端批量 brief→初稿→一致性审查，章末因果链变更记录维持长篇连贯。；（3）Digital Director：服务端批量跑脚本→分镜→素材→mux→合并，客户端断开或刷新后可恢复进度。；（4）小说转短剧：导入章节或整本 AI 小说，改写为可拍剧本后复用同一分镜与视频流水线。；（5）多供应商矩阵：文本/图像/视频/TTS 分别接 OpenAI、Gemini、DeepSeek、MiniMax、Vidu 等。；（6）移动指挥台与积分计费：uni-app 手机端派发批量任务看进度，支付接 Stripe/PayPal/微信/支付宝。
- **最该学**：双流水线兜底：没有视频模型额度时，用关键帧+Ken Burns+FFmpeg 也能产出整集成片。
- **技术栈/兼容性**：Node.js 22+ / Hono API、Nuxt 3 + Vue 3 + TypeScript、Drizzle ORM（SQLite/MySQL）、Mastra + AI SDK、FFmpeg/Sharp；两者都支持；云端多家，也可接本地 Ollama；可复用度 **低；全栈为 Node.js/TypeScript，无 Python 组件。**

#### `neopen/story-shot-agent` — ⭐203 · MIT · 最近推送 2026-09-23 · 活跃
- **定位**：LangGraph 多智能体剧本转分镜系统，输出镜头级双语提示词并保障跨片段连续
- **核心功能**：（1）智能剧本解析：自动识别场景、对话与动作指令，支持长文本分段处理；（2）精准时序规划：按镜头粒度切分内容并分配时长，适配视频模型时长限制；（3）提示词输出：生成中英双语画面描述、负面提示词与配套音频提示词；（4）连续性守护：三级记忆池加 Chroma 向量检索，保持角色、场景与剧情一致；（5）质量审计与修复：质量审计智能体、循环检查、错误处理与人工干预节点；（6）多协议集成：Python SDK、REST API、LangGraph 节点、A2A 与 MCP 服务
- **最该学**：多智能体流水线加三级记忆与向量检索，把连续性和可追溯做成机制
- **技术栈/兼容性**：Python 3.10+；LangChain + LangGraph、LlamaIndex、Chroma、Redis、FastAPI、Docker；两者都支持（可接 Ollama 本地，也支持云端 LLM API）；可复用度 **高：纯 Python 包，附 SDK 与 MCP 示例，可直接集成**

#### `coracoo/Slate` — ⭐13 · 未标注 · 最近推送 2026-09-21 · 活跃
- **定位**：面向短片制作与拉片分析的本地 Web 工作台，剧本分镜到集视频一体化管理
- **核心功能**：（1）制作线：剧本生成→分镜生成→素材生成→音色绑定→演员表现→平面推演→创作生成；（2）三层视频：S 转场镜头、V 分镜视频、E 集视频，支持关键帧采用与尾帧续接；（3）拉片线：影片解构、台词 OCR/ASR、逐帧提取、深度动作与镜头讲解；（4）生成入口：本地 ComfyUI、ChatGPT 网页生图与已适配云端 API 统一排队；（5）后台任务：异步提交轮询、产出记录、失败原因与顶部通知，不盲目重发；（6）本地资产：人物、场景、道具、角色音色、音乐、关键帧和视频集中管理
- **最该学**：用分镜 JSON 契约与三层视频结构，把从分镜到集视频的链路管住
- **技术栈/兼容性**：Python 3.12(uv 固定依赖) + Vue 前端 + FFmpeg；ComfyUI 与 Chrome 扩展；Windows 本机部署；两者都支持（本地 ComfyUI 与云端 API 均可）；可复用度 **高：Python 后端与任务、厂商适配层，契约设计可参考**

#### `eternityspring/shuohao-skills` — ⭐3,730 · Apache-2.0 · 最近推送 2026-09-14 · 活跃
- **定位**：AI 短剧制作 skill 集合：从小说拆角色、排大纲、出设定、写剧本到切分镜
- **核心功能**：（1）novel-outline：小说改编短剧大纲五件套（改编说明、人物表、爽点表、分集梗概、资产清单），14 道质量门脚本检查；（2）novel-characters：角色设定集，产出人物画像、形象提示词、音色提示词与角色设定图，吃 outline.json 预填角色表；（3）novel-art：美术设定集（场景 + 叙事道具），含一致性锚点、光照与状态变体、尺度参照、无人无手白底提示词，11 道质量门；（4）novel-script：场次 + 节拍流剧本，逐集时长按语速确定性折算，台词本按角色聚合带音色提示词对接 TTS，10 道质量门；（5）novel-storyboard：段（≤15 秒）→ 分镜（2–5 秒硬门）→ 分镜图，钉切点逐字对账，17 道质量门；（6）report.mjs：把五段报告合成单页（导航切换、深链、平铺全部），配 92 项断言自测
- **最该学**：分镜只做输出不做新决定，各段用确定性脚本质量门 + 零依赖自测把上游结论钉死
- **技术栈/兼容性**：Node.js ≥18（ESM .mjs，仅标准库、无 npm 依赖）+ Markdown SKILL.md；Claude Code / codex 双端软链安装；出图走 codex 内置 $imagegen；两者都支持（脚本纯本地零依赖；文本与出图用当前 agent 会话模型额度、codex 内置 $imagegen 闭源服务）；可复用度 **中，实现是 Node，但质量门清单与流水线分层设计可直接移植**

#### `jxnl/instructor`
- **数据缺失**：未采集到该项目元数据。

#### 同向速览（未展开卡片）

| 项目 | ⭐ | 许可证 | 最近推送 | 状态 | 一句话定位 |
|---|---:|---|---|---|---|
| [`zenstory-ai/drama-skills`](https://github.com/zenstory-ai/drama-skills) | 2,216 | MIT | 2026-09-18 | 活跃 | 面向编剧与漫剧工作室的 11 个 AI 短剧技能，从原著到分集剧本与成片 |
| [`liangdabiao/Seedance2-Storyboard-Generator`](https://github.com/liangdabiao/Seedance2-Storyboard-Generator) | 2,441 | 未标注 | 2026-09-21 | 活跃 | 基于 Skill 与 Seedance 2.0 的 AI 多集短视频生产工作流 |
| [`calesthio/OpenMontage`](https://github.com/calesthio/OpenMontage) | 6.1万 | AGPL-3.0 | 2026-09-06 | 活跃 | 面向 AI 编码助手的开源 agentic 视频制作流水线，覆盖研究到渲染全流程 |


### 方向 8 流水线编排框架

通用编排框架能不能用在单机 AI 生成上？**结论：概念要抄，框架不要引。**

我们真正需要的三件事——**阶段级幂等**、**断点续跑**、**产物指纹/增量**——这些框架都有，但每个都附带我们不需要的重量：

| 框架 | 它最强的地方（值得抄的概念） | 自托管代价 | 对单机自用的判断 |
|---|---|---|---|
| Prefect | `@flow/@task` 装饰器 + 内置缓存/重试/UI | 需要 Prefect server（或 Cloud） | 概念最贴，但仍引入服务端与元数据层 |
| Dagster | **「资产(asset)而非任务」**的心智模型 + 血缘 | 元数据库 + webserver 常驻 | 心智模型与我们最同构，重量不划算 |
| Snakemake | 按文件依赖做**增量重跑**（只跑变化的部分） | 纯本地，无服务端 | **最贴近我们需求的一个**，只是 DSL 偏生信风格 |
| Luigi | 依赖解析 + **原子文件操作**（不留半成品数据） | 中央调度器进程 | 架构偏旧，抄「原子落地」即可 |
| Kedro | 工程化项目模板 + **Data Catalog** 统一读写与版本化 | 无服务端 | Data Catalog 的思路可抄（= 产物登记表） |
| Metaflow | **步骤检查点 + 失败恢复 + 版本化**，原型与生产同一套代码 | 本地可跑，云端需基础设施 | 检查点/版本化机制很贴生成流水线 |
| Hamilton | **函数即节点 + 定义与执行分离** + `@check_output` 校验 | 无服务端，纯库 | **最轻**，可直接嵌进现有 Python 项目 |
| DBOS | 装饰器 + Postgres **持久化工作流**，崩溃后从已完成步骤续跑 | 需 Postgres | 续跑语义最正，但引入 Postgres |
| doit / redun / pypyr | 文件依赖增量 / 哈希增量+血缘 / YAML 顺序步骤 | 无 | 轻量可参考，社区小 |

**我们的建议**：不引入上述任何框架作为运行时。用 **SQLite 一张 `stage_state` 表（阶段名 + 输入指纹 + 产物路径 + 状态 + 时间戳）** 实现 DBOS/Prefect 的核心语义，用 **文件哈希做增量**（Snakemake 的核心），用 **写 `.tmp` 再原子改名**（Luigi 的核心）保证不留半成品。这三条加起来不到 200 行，却是本次调研里"活得最久"的项目（`YiIimini/NiliX`）真正的做法。

#### 代表项目

#### `PrefectHQ/prefect` — ⭐2.4万 · Apache-2.0 · 最近推送 2026-09-23 · 活跃
- **定位**：Python 工作流编排框架，把脚本变成可调度、可重试、可观测的生产流水线。
- **核心功能**：（1）用 @flow/@task 装饰器把普通 Python 函数编排成流水线，几行代码即可运行。；（2）内置调度、缓存、重试与事件驱动自动化，失败后按策略自动重跑。；（3）可把 flow 变成 deployment，用 cron 调度，也可从 UI 或 CLI 手动触发。；（4）自托管 Prefect server 或 Prefect Cloud 统一跟踪每次运行的活动与日志。；（5）支持复杂分支与依赖逻辑，并可在运行中响应外部事件。
- **最该学**：把普通函数装饰成生产级 flow：调度、重试、缓存与 UI 观测开箱即用。
- **技术栈/兼容性**：Python 3.10+；@flow/@task 装饰器；自托管 Prefect server 与 Web UI；两者都支持（可自托管 Prefect server，也可用托管 Prefect Cloud）；可复用度 **高 — Python 原生，缓存/重试/UI 可直接复用。**

#### `dagster-io/dagster` — ⭐1.6万 · Apache-2.0 · 最近推送 2026-09-22 · 活跃
- **定位**：云原生数据流水线编排器：把表/数据集/ML 模型等「数据资产」声明成 Python 函数，由 Dagster 决定何时运行并保持资产最新
- **核心功能**：（1）软件定义资产：用 @dg.asset 装饰普通 Python 函数声明资产与依赖，Dagster 自动解析依赖图并按需运行、保持资产最新；（2）内置血缘与可观测性：Web UI 展示资产图、每次运行的元数据、诊断与目录（cataloging / lineage）；（3）声明式编程模型与一等公民的可测试性：资产函数可直接单测，本地开发→单测→集成测试→staging→生产用同一套代码；（4）面向规模的生产编排引擎：README 自述为 multi-tenant、multi-tool 引擎，可按组织与技术规模扩展；（5）大量现代数据栈集成库，可部署到自有基础设施；Python 3.9–3.14，uv add 三个包即可起本地 Web UI；（6）强调数据质量与 CI/CD 实践：复用组件、尽早发现数据质量问题（README: spot data quality issues）
- **最该学**：「资产（asset）而不是任务（task）」的心智模型与我们的「产物 + 产物依赖」天然同构——把镜头/定妆照/成片都当资产声明，增量更新与血缘是内建的；但代价是元数据库 + 常驻 Web 服务，单机自用偏重
- **技术栈/兼容性**：Python 3.9–3.14；@dg.asset 声明式 API；dagster-webserver Web UI；需要元数据库（本机可用 SQLite，生产常用 Postgres）；纯本地开源（Apache-2.0；另有托管版 Dagster+）；可复用度 **中 — 资产式建模 + 血缘 + 增量思想值得抄，但常驻服务与元数据库对「单卡自用」偏重**

#### `snakemake/snakemake` — ⭐2,878 · MIT · 最近推送 2026-09-20 · 活跃
- **定位**：基于 Python 语法的可复现工作流管理系统，同一份定义可扩展到集群与云。
- **核心功能**：（1）用人类可读的 Python 语法描述工作流，不依赖外部 DSL。；（2）工作流可声明所需软件，自动部署到任意执行环境以保证可复现。；（3）同一份工作流定义可无缝扩展到服务器、集群、网格与云环境。；（4）面向可复现、可扩展的数据分析，2024 年平均每周新增 14 次引用。
- **最该学**：同一份工作流定义可无缝从本机扩到集群与云，并自动部署所需软件环境。
- **技术栈/兼容性**：Python 3；Python 语法的规则 DSL；支持集群/云与容器化软件依赖；纯本地开源（可自建集群或云环境执行）；可复用度 **高 — Python 语法描述工作流，可复现增量执行值得借鉴。**

#### `spotify/luigi` — ⭐1.9万 · Apache-2.0 · 最近推送 2026-07-18 · 活跃
- **定位**：Python 批量作业流水线框架，处理依赖解析、失败处理与可视化。
- **核心功能**：（1）在 Python 内定义任务与依赖关系，构建含上千任务的批处理流水线。；（2）处理依赖解析、工作流管理与失败情况，并提供命令行集成。；（3）内置 Hadoop、Hive、Pig、Spark 等常用任务的工具箱。；（4）HDFS 与本地文件系统抽象，文件操作保持原子性以避免半成品数据。；（5）自带 Web visualiser，可按状态搜索过滤任务并查看依赖图。
- **最该学**：用原子文件操作保证流水线不会留下部分数据、不会崩在中间状态。
- **技术栈/兼容性**：Python 3.10–3.14；中央调度器 + worker；Web visualiser；HDFS/本地文件抽象；纯本地开源；可复用度 **中 — 依赖解析与原子文件操作值得借鉴，架构偏旧。**

#### `kedro-org/kedro` — ⭐1.1万 · 自定义(未标准) · 最近推送 2026-09-22 · 活跃
- **定位**：面向生产的数据工程与数据科学流水线工具箱，强调模块化与可复现。
- **核心功能**：（1）Cookiecutter 项目模板统一目录结构，产出可维护的工程化项目。；（2）Data Catalog 用轻量连接器统一读写多格式多文件系统，并支持数据与模型版本化。；（3）自动解析纯 Python 函数之间的依赖，生成流水线并用 Kedro-Viz 可视化。；（4）内置 pytest、Sphinx、ruff 与标准日志的编码规范。；（5）支持单机与分布式部署，可落到 Argo、Prefect、Kubeflow、AWS Batch、Databricks。
- **最该学**：用工程化项目模板与 Data Catalog 把数据流水线做成可维护、可复用的软件。
- **技术栈/兼容性**：Python 3.10–3.14；Cookiecutter 模板；Data Catalog；Kedro-Viz；纯本地开源（部署到 Argo/Kubeflow 等需自建）；可复用度 **中 — 工程规范与 Data Catalog 有参考价值，但框架较重。**

#### `Netflix/metaflow` — ⭐1.0万 · Apache-2.0 · 最近推送 2026-09-22 · 活跃
- **定位**：以人为中心的 AI/ML 工作流框架，统一代码、数据与算力，从原型直通生产。
- **核心功能**：（1）提供 Pythonic API，notebook 原型与生产部署共用同一套代码。；（2）内置实验跟踪、版本化与结果可视化，便于比较多次运行。；（3）foreach 支持大规模 embarrassingly parallel 与 gang-scheduled 计算。；（4）提供检查点与失败恢复机制，让远程任务可靠且高效。；（5）一键部署到高可用生产编排器，并支持事件触发的响应式编排。
- **最该学**：从 notebook 原型到生产部署保持同一套代码，并内置版本化与恢复能力。
- **技术栈/兼容性**：Python；@step 流 API；云对象存储；AWS Batch/Kubernetes 计算后端；两者都支持（本地开源可跑，云端需自建基础设施或 Outerbounds）；可复用度 **高 — 步骤/检查点/版本化机制很贴合生成流水线。**

#### `DAGWorks-Inc/hamilton` — ⭐2,598 · Apache-2.0 · 最近推送 2026-09-23 · 活跃
- **定位**：用 Python 函数定义数据转换 DAG 的轻量库，专注可校验的数据流。
- **核心功能**：（1）用带参数的普通 Python 函数声明依赖，库自动拼装数据转换 DAG。；（2）DAG 定义与执行分离，同一份定义可在脚本、Airflow、FastAPI 中复用。；（3）用 @check_output 校验节点输出，SchemaValidator 适配器跟踪 dataframe schema。；（4）Function modifiers 与 @config.when() 按环境切换 DAG 分支，避免 if/else。；（5）Hamilton UI 自动生成数据目录、血缘与执行跟踪，并按项目查看。；（6）一行 driver 代码即可构建 DAG，并可把多个 Python 模块组装成流水线。
- **最该学**：函数即节点、定义与执行分离，让数据流可读、可测、可校验。
- **技术栈/兼容性**：Python 3.8+；函数式 DAG；pandas/polars/Ibis 适配；可选 Hamilton UI；纯本地开源（Apache 2.0，UI 可本地运行或 Docker 自托管）；可复用度 **高 — 函数即节点，数据校验与 UI 易嵌入现有项目。**

#### `dbos-inc/dbos-transact-py` — ⭐1,586 · MIT · 最近推送 2026-09-22 · 活跃
- **定位**：基于 Postgres 的轻量持久化工作流库，崩溃重启后自动从已完成步骤续跑。
- **核心功能**：（1）把普通函数标注为 workflow/step，状态自动 checkpoint 到 Postgres。；（2）程序失败重启后，工作流自动从最后一个已完成步骤继续执行。；（3）持久化队列支持并发上限、限流、超时、去重与优先级。；（4）以事件 ID 作幂等键，实现 webhook/Kafka 事件的恰好一次处理。；（5）支持 cron 调度与 durable sleep，中断重启后仍能按时唤醒。；（6）工作流以 Postgres 行存储，可用 client 查询、批量暂停恢复、从指定步骤 fork 重启。
- **最该学**：几行装饰器即获得 Postgres 级持久化与断点自动续跑，无需额外编排服务。
- **技术栈/兼容性**：Python；装饰器 API；Postgres 作为状态与队列表；纯本地开源（需自备 Postgres）；可复用度 **高 — 装饰器 + Postgres 持久化，续跑实现可直接借鉴。**

#### 同向速览（未展开卡片）

| 项目 | ⭐ | 许可证 | 最近推送 | 状态 | 一句话定位 |
|---|---:|---|---|---|---|
| [`pydoit/doit`](https://github.com/pydoit/doit) | 2,087 | MIT | 2026-09-21 | 活跃 | 纯 Python 的任务自动化工具，按文件依赖做增量构建，只重跑变化的部分。 |
| [`insitro/redun`](https://github.com/insitro/redun) | 603 | Apache-2.0 | 2026-07-17 | 活跃 | 以惰性表达式定义工作流的引擎，自动并行、哈希增量计算并记录血缘。 |
| [`pypyr/pypyr`](https://github.com/pypyr/pypyr) | 645 | Apache-2.0 | 2023-12-19 | 已停更 | 用 YAML 定义顺序步骤的通用任务运行器，把多语言脚本串成一条流水线。 |
| [`hatchet-dev/hatchet`](https://github.com/hatchet-dev/hatchet) | 7,989 | MIT | 2026-09-23 | 活跃 | 面向后台任务与持久工作流的编排平台，自带队列、重试与实时 Web UI。 |

## 4. 功能对照表

**怎么读这张表**

- **表 A** 是"端到端流水线类"（从文本/小说一路做到成片），24 列，是和我们最可比的一群；**表 B** 是组件/基础设施类（ComfyUI 运维、角色一致性、NLE、编排框架），26 列。拆两张表纯粹是为了宽度可读，功能点是同一套 20 项。
- 单元格：**● 有** / **◐ 部分** / **○ 无** / **? 未验证（README 未覆盖，不等于没有）**。
- 代号与仓库的对应见每张表上方的图例。所有行列数据来自 2026-09-23 快照的 README 深读结果。

### 表 A：端到端流水线类项目（24 列）

**图例**：● 有　◐ 部分　○ 无　? 未验证（README 未覆盖）

| 代号 | 项目 | 代号 | 项目 |
|---|---|---|---|
| `MPT` | harry0703/MoneyPrinterTurbo ⭐12.5万 | `CAD` | qiukaihui/comfyui-auto-drama ⭐47 |
| `OMT` | calesthio/OpenMontage ⭐6.1万 | `NLX` | YiIimini/NiliX ⭐2 |
| `PXL` | ATH-MaaS/Pixelle-Video ⭐2.8万 | `HBO` | chatfire-AI/huobao-drama ⭐1.5万 |
| `NRT` | linyqh/NarratoAI ⭐1.1万 | `TFL` | HBAI-Ltd/Toonflow-app ⭐1.6万 |
| `SGT` | RayVentura/ShortGPT ⭐7,969 | `SPG` | xxx-888/super_gen ⭐18 |
| `WWW` | 123Mr-king/waoowaoo ⭐12 | `DRM` | dramaclaw/dramaclaw ⭐6,332 |
| `HHD` | appolloqin/huohuo-drama ⭐10 | `BBN` | shuyu-labs/BigBanana-AI-Director ⭐2,263 |
| `JLY` | Forget-C/Jellyfish ⭐6,480 | `LMX` | alibaba/lumenx ⭐1,301 |
| `ARC` | ArcReel/ArcReel ⭐5,142 | `AMS` | 869413421/ai-moive-studio ⭐1,572 |
| `NVD` | Anning01/novelvids ⭐334 | `SHT` | neopen/story-shot-agent ⭐203 |
| `NVA` | Agions/novella ⭐114 | `SLT` | coracoo/Slate ⭐13 |
| `KTA` | oskey/kt-ai-Studio ⭐87 | `COM` | jmilinovich/comicgen ⭐1 |

| 功能点 | MPT | OMT | PXL | NRT | SGT | WWW | HHD | JLY | ARC | NVD | NVA | KTA | CAD | NLX | HBO | TFL | SPG | DRM | BBN | LMX | AMS | SHT | SLT | COM |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **多阶段流水线** | ● | ● | ● | ● | ● | ● | ● | ● | ● | ● | ● | ● | ● | ● | ● | ● | ● | ● | ● | ● | ● | ● | ● | ◐ |
| **断点续跑** | ◐ | ● | ◐ | ◐ | ◐ | ? | ● | ● | ● | ● | ? | ? | ● | ● | ● | ◐ | ● | ● | ? | ◐ | ? | ● | ◐ | ◐ |
| **产物幂等指纹** | ? | ? | ? | ◐ | ? | ? | ? | ? | ? | ◐ | ? | ? | ◐ | ● | ◐ | ? | ◐ | ? | ? | ? | ? | ? | ◐ | ◐ |
| **镜头级编辑** | ◐ | ◐ | ◐ | ◐ | ◐ | ◐ | ◐ | ● | ◐ | ● | ◐ | ? | ● | ● | ● | ● | ● | ◐ | ● | ◐ | ◐ | ◐ | ● | ● |
| **批量操作** | ● | ● | ● | ? | ? | ? | ● | ● | ◐ | ● | ? | ● | ● | ? | ● | ◐ | ● | ● | ● | ● | ? | ◐ | ● | ● |
| **抽卡/多候选挑选** | ◐ | ● | ? | ◐ | ? | ? | ? | ● | ◐ | ◐ | ? | ? | ◐ | ? | ? | ? | ? | ● | ● | ● | ? | ? | ◐ | ? |
| **角色一致性** | ? | ● | ◐ | ○ | ? | ● | ◐ | ● | ● | ● | ● | ● | ● | ● | ● | ◐ | ● | ● | ● | ◐ | ◐ | ● | ◐ | ● |
| **换装/多形态** | ? | ? | ? | ? | ? | ? | ? | ● | ? | ● | ? | ? | ◐ | ● | ? | ? | ? | ◐ | ● | ? | ? | ◐ | ? | ? |
| **分镜 schema 标准化** | ◐ | ● | ● | ? | ● | ? | ◐ | ◐ | ● | ● | ? | ● | ● | ● | ◐ | ◐ | ◐ | ● | ◐ | ● | ◐ | ● | ● | ◐ |
| **机械质检** | ◐ | ● | ? | ? | ? | ? | ◐ | ◐ | ? | ◐ | ? | ? | ● | ● | ◐ | ◐ | ◐ | ◐ | ◐ | ? | ? | ◐ | ◐ | ◐ |
| **人工审片** | ? | ● | ◐ | ? | ? | ? | ? | ● | ● | ◐ | ◐ | ? | ● | ◐ | ◐ | ◐ | ◐ | ● | ● | ? | ? | ◐ | ◐ | ? |
| **字幕** | ● | ● | ? | ● | ● | ? | ? | ? | ? | ? | ● | ○ | ● | ● | ● | ? | ● | ● | ? | ? | ● | ? | ◐ | ? |
| **BGM/音效** | ● | ● | ● | ● | ? | ? | ● | ? | ◐ | ● | ◐ | ? | ? | ● | ? | ? | ◐ | ● | ◐ | ? | ? | ◐ | ● | ? |
| **TTS/配音** | ● | ● | ● | ● | ● | ● | ● | ? | ◐ | ● | ● | ? | ◐ | ◐ | ? | ? | ● | ● | ● | ● | ● | ◐ | ● | ? |
| **时间轴编辑** | ◐ | ◐ | ? | ◐ | ◐ | ? | ◐ | ? | ◐ | ◐ | ◐ | ? | ? | ◐ | ◐ | ◐ | ● | ◐ | ● | ● | ? | ◐ | ◐ | ? |
| **任务队列** | ◐ | ? | ● | ? | ? | ● | ● | ● | ● | ● | ● | ● | ● | ◐ | ● | ◐ | ● | ● | ◐ | ◐ | ? | ● | ● | ? |
| **进度与日志** | ◐ | ● | ● | ? | ? | ? | ● | ● | ● | ● | ? | ● | ● | ● | ● | ◐ | ● | ● | ● | ● | ◐ | ◐ | ● | ◐ |
| **成本统计** | ? | ● | ◐ | ? | ? | ? | ● | ? | ● | ● | ? | ? | ? | ● | ? | ◐ | ● | ◐ | ◐ | ? | ◐ | ? | ? | ? |
| **Web UI** | ● | ● | ● | ● | ● | ● | ● | ● | ● | ● | ● | ● | ● | ● | ● | ● | ● | ● | ● | ● | ● | ◐ | ● | ○ |
| **多项目/多集管理** | ◐ | ● | ◐ | ? | ? | ? | ● | ● | ● | ● | ? | ● | ● | ● | ● | ● | ● | ● | ● | ? | ● | ◐ | ● | ● |


### 表 B：组件 / 基础设施类项目（26 列）

| 代号 | 项目 | 代号 | 项目 |
|---|---|---|---|
| `CMD` | Comfy-Org/ComfyUI-Manager ⭐1.6万 | `OCP` | OpenCut-app/OpenCut ⭐9.1万 |
| `SWM` | mcmonkeyprojects/SwarmUI ⭐4,595 | `OCC` | 0xsline/OpenChatCut ⭐1,979 |
| `MCW` | light-and-ray/Minimalistic-Comfy-Wrapper-WebUI ⭐140 | `PRL` | pireel/pireel ⭐1,226 |
| `CEX` | Infinishot/comfy-executors ⭐0 | `MPY` | Zulko/moviepy ⭐1.5万 |
| `CSC` | Chaoses-Ib/ComfyScript ⭐704 | `AED` | WyattBlue/auto-editor ⭐5,341 |
| `CSQ` | CraftopiaStudio/ComfyUI-SmartQueue ⭐1 | `RMT` | remotion-dev/remotion ⭐6.0万 |
| `WSM` | 11cafe/comfyui-workspace-manager ⭐1,453 | `PRF` | PrefectHQ/prefect ⭐2.4万 |
| `H3M` | NikoDemon80/ComfyUI-H3-Motion-Context ⭐1,006 | `DSG` | dagster-io/dagster ⭐1.6万 |
| `IID` | instantX-research/InstantID ⭐1.2万 | `SNK` | snakemake/snakemake ⭐2,878 |
| `IPA` | cubiq/ComfyUI_IPAdapter_plus ⭐6,136 | `LUI` | spotify/luigi ⭐1.9万 |
| `PLD` | ToTheBeginning/PuLID ⭐3,552 | `KED` | kedro-org/kedro ⭐1.1万 |
| `PHM` | TencentARC/PhotoMaker ⭐1.0万 | `MFL` | Netflix/metaflow ⭐1.0万 |
| `DPD` | 18yz153/ComfyUI-Persona-Director ⭐20 | `DBX` | dbos-inc/dbos-transact-py ⭐1,586 |

| 功能点 | CMD | SWM | MCW | CEX | CSC | CSQ | WSM | H3M | IID | IPA | PLD | PHM | DPD | OCP | OCC | PRL | MPY | AED | RMT | PRF | DSG | SNK | LUI | KED | MFL | DBX |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **多阶段流水线** | ? | ? | ? | ? | ◐ | ◐ | ? | ◐ | ◐ | ? | ? | ◐ | ◐ | ? | ? | ? | ? | ◐ | ? | ● | ? | ◐ | ● | ● | ● | ● |
| **断点续跑** | ◐ | ? | ● | ? | ? | ● | ◐ | ● | ? | ? | ? | ? | ● | ? | ● | ? | ? | ? | ? | ● | ? | ? | ◐ | ? | ● | ● |
| **产物幂等指纹** | ? | ? | ? | ◐ | ◐ | ? | ? | ? | ? | ? | ? | ? | ? | ? | ◐ | ? | ? | ? | ? | ● | ? | ? | ◐ | ? | ◐ | ● |
| **镜头级编辑** | ? | ? | ? | ? | ? | ? | ? | ● | ◐ | ? | ◐ | ◐ | ◐ | ? | ◐ | ? | ? | ◐ | ? | ? | ? | ? | ? | ? | ? | ? |
| **批量操作** | ● | ◐ | ● | ● | ◐ | ● | ● | ● | ? | ? | ? | ◐ | ? | ◐ | ● | ? | ? | ? | ● | ? | ? | ? | ◐ | ◐ | ● | ◐ |
| **抽卡/多候选挑选** | ? | ? | ? | ◐ | ● | ? | ? | ◐ | ? | ? | ? | ◐ | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ◐ | ? |
| **角色一致性** | ? | ◐ | ? | ? | ? | ? | ? | ? | ● | ● | ● | ● | ● | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? |
| **换装/多形态** | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ◐ | ● | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? |
| **分镜 schema 标准化** | ◐ | ? | ● | ◐ | ◐ | ? | ? | ? | ? | ? | ? | ? | ● | ? | ◐ | ? | ? | ◐ | ? | ? | ? | ? | ? | ◐ | ? | ? |
| **机械质检** | ? | ◐ | ? | ? | ? | ? | ? | ● | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? |
| **人工审片** | ? | ? | ? | ? | ◐ | ● | ? | ● | ? | ? | ? | ? | ? | ? | ● | ◐ | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? |
| **字幕** | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ● | ● | ◐ | ? | ● | ? | ? | ? | ? | ? | ? | ? |
| **BGM/音效** | ? | ◐ | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ● | ? | ? | ? | ◐ | ? | ? | ? | ? | ? | ? | ? |
| **TTS/配音** | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ● | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? |
| **时间轴编辑** | ? | ? | ? | ? | ? | ? | ? | ◐ | ? | ? | ? | ? | ? | ? | ● | ● | ◐ | ● | ◐ | ? | ? | ? | ? | ? | ? | ? |
| **任务队列** | ● | ◐ | ● | ◐ | ● | ● | ? | ● | ? | ? | ? | ? | ? | ? | ● | ? | ? | ? | ? | ? | ? | ◐ | ◐ | ◐ | ◐ | ● |
| **进度与日志** | ◐ | ? | ? | ? | ● | ◐ | ? | ◐ | ? | ? | ? | ? | ? | ? | ● | ? | ? | ? | ? | ● | ? | ? | ◐ | ? | ◐ | ◐ |
| **成本统计** | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? |
| **Web UI** | ● | ● | ● | ? | ◐ | ● | ● | ● | ● | ● | ● | ● | ● | ● | ● | ● | ? | ◐ | ◐ | ● | ? | ? | ● | ● | ◐ | ? |
| **多项目/多集管理** | ? | ? | ? | ? | ? | ? | ◐ | ? | ? | ? | ? | ? | ? | ? | ● | ? | ? | ? | ? | ◐ | ? | ? | ? | ? | ◐ | ◐ |

---

## 5. 业界公认的必备功能（大多数项目都有；我们缺了就是硬伤）

### 5.1 全样本普及率（106 个项目，README 证据口径）

| 功能点 | 明确"有" | "有+部分" | 占比（有+部分） |
|---|---:|---:|---:|
| 多阶段流水线 | 63 | 83 | 78% |
| Web UI | 76 | 87 | 82% |
| 分镜 schema 标准化 | 30 | 64 | 60% |
| 进度与日志 | 39 | 61 | 58% |
| 镜头级编辑 | 26 | 58 | 55% |
| 任务队列 | 31 | 57 | 54% |
| 断点续跑 | 32 | 56 | 53% |
| 多项目/多集管理 | 36 | 53 | 50% |
| 角色一致性 | 38 | 50 | 47% |
| 时间轴编辑 | 15 | 45 | 42% |
| TTS/配音 | 33 | 44 | 42% |
| 人工审片 | 13 | 43 | 41% |
| 字幕 | 34 | 41 | 39% |
| 机械质检 | 13 | 34 | 32% |
| 批量操作 | 45 | 68 | 64% |
| 抽卡/多候选挑选 | 12 | 32 | 30% |
| BGM/音效 | 22 | 32 | 30% |
| 产物幂等指纹 | 9 | 29 | 27% |
| 成本统计 | 14 | 24 | 23% |
| 换装/多形态 | 7 | 16 | 15% |

### 5.2 只在"端到端流水线类"18 个代表项目里统计（更贴近我们的场景）

| 功能点 | 具备（有+部分） | 结论 |
|---|---:|---|
| 多阶段流水线 | **18/18** | **硬门槛**，没有就不算同类 |
| Web UI | **18/18** | **硬门槛**（唯一例外是纯 CLI/Skill 型项目） |
| 镜头级编辑 | 17/18 | **硬门槛**：不能只重跑全片 |
| 角色一致性 | 15/18 | **硬门槛**（漫剧尤其） |
| 分镜 schema 标准化 | 15/18 | **硬门槛**，是断点续跑/质检/编辑的前提 |
| 断点续跑 | 14/18 | **硬门槛**（渲染动辄数小时） |
| TTS/配音 | 14/18 | 必备（H3 原生对白可部分替代） |
| 任务队列 | 14/18 | 必备 |
| 进度与日志 | 14/18 | 必备 |
| 多项目/多集管理 | 14/18 | 必备（长篇小说必然多集） |
| 批量操作 | 12/18 | 必备 |
| 人工审片 | 12/18 | 必备（质量兜底） |
| 时间轴/拼接 | 12/18 | 必备（哪怕只是 concat + 转场） |
| 机械质检 | 11/18 | 强需求 |
| BGM/音效 | 11/18 | 强需求 |
| 字幕 | 10/18 | 强需求（我们有中文硬字幕需求，必须做） |
| 成本统计 | 8/18 | 云端 API 项目必备；纯本地可弱化 |
| 抽卡/多候选挑选 | 8/18 | 强需求（直接决定成片率） |
| 产物幂等指纹 | **6/18** | **最被低估**，见 §6.1 |
| 换装/多形态 | **4/18** | **最被低估**，见 §6.2 |

### 5.3 对我们流水线的"硬伤"清单（按优先级）

| 优先级 | 缺口 | 为什么是硬伤 | 现成参考 |
|---|---|---|---|
| P0 | **产物幂等指纹 + stale 清单** | 改一句提示词/换一张定妆照就要全片重渲，单卡 5090 上这是几小时到几天的浪费 | `NiliX` 的 `manifest.json` + 条件指纹；`comfyui-auto-drama` 的版本化任务 ID |
| P0 | **ComfyUI 渲染检查点** | ComfyUI 崩溃/显存溢出重启后必须能从 `history` 收回已完成任务，否则重复烧 GPU | `NiliX`（prompt_id 落盘 + 查 history）；`comfyui-auto-drama`（SQLite + 链式守护） |
| P0 | **镜头级重渲 + 分镜 schema** | 没有结构化 `shots.json` 就只能整集重跑，也无法做逐镜质检与逐镜审片 | `grokbot-ai-manju`（`shot_id` 重渲）；`Slate`（storyboard.json 单一事实源）；`Jellyfish`（镜头就绪状态机） |
| P1 | **角色一致性落到数据结构** | 靠 prompt 描述角色必然跨镜漂移；要用共享实体 + 定妆照/参考图显式注入 | `Jellyfish`（共享实体模型 + 名称查重）；`novelvids`/`super_gen`（`@资产名` 显式引用）；`ArcReel`（角色设计图贯穿） |
| P1 | **机械质检**（分辨率/时长/黑帧/段尾冻结/人脸数/服装） | 单卡批量出片必然有坏镜，人工肉眼过一遍上百镜不现实 | `OpenMontage`（ffprobe+抽帧+音频电平+字幕检查）；`NiliX`（成片质检含段尾冻结） |
| P1 | **人工审片门 + 通过/打回** | 全自动出片质量不可控；需要"逐镜评分→定点返工"的闭环而不是全片重来 | `NiliX`（VLM 八维度判分 + 修复师返工）；`Jellyfish`（候选确认闸门）；`OpenMontage`（审批门 + checkpoint 拒签） |
| P1 | **多集/多项目管理与目录契约** | 一部长篇 = 多集 × 每集 11~17 镜 × N 个产物，没有契约目录会失控 | `grokbot-ai-manju`（folder contract + `episode_map.yaml`）；`agentic-comic-drama-studio`（三级资产目录 + catalog.yaml） |
| P2 | **中文硬字幕 + 时间轴合成** | 我们明确要中文字幕；字幕必须与 TTS/对白时长对齐 | `moneyprinterturbo`（edge 时间戳 / whisper 两路字幕）；`Slate`（三层视频与合并）；`moviepy`/`ffmpeg` |
| P2 | **成本/用量统计** | 只要用了 DeepSeek/云端 TTS 或云端补渲，就需要按项目统计；本地渲染则至少统计 GPU 时长 | `novelvids`（定价快照 + 按 H3 输出秒数计费）；`OpenMontage`（预估/预留/对账 + cap） |
| P2 | **抽卡与多候选挑选** | 直接影响成片率；没有候选挑选就得反复手改 prompt | `Jellyfish`（候选确认）；`BigBanana`（Script-to-Asset-to-Keyframe 反抽卡）；`NiliX`（seed 重试策略） |

---

## 6. 少数项目有、但很有价值的差异化功能（值得偷）

> 判定依据：在端到端 18 个代表项目里出现率低（≤8/18），但工程价值高。每条都给出处与 README 依据。

### 6.1 幂等与续跑三件套（出现率 6/18，但决定长期产能）

1. **产物时效清单 `manifest.json`（current / stale / missing 四态）** — `YiIimini/NiliX`
   记录每镜渲染时的**全部输入指纹**（提示词、角色、场景、画幅、帧数，外加资产指纹、定妆照 mtime、视图代数 `views_gen`）。换定妆照或改提示词后，旧产物**自动标 stale 并删旧重渲**，含条件缓存的隐性失效防线。
   > README 原文："`analysis/<ep>_manifest.json` 记录每镜渲染时全部输入指纹……current/stale/missing 四态——换定妆照/改提示词后旧产物自动标 stale 并删旧重渲"

2. **条件指纹缓存（同条件镜头共享编码）** — `YiIimini/NiliX`
   > "条件指纹 = md5(提示词+角色+场景+画幅+帧数)——同条件镜头共享编码；草稿/定稿分辨率各自独立缓存。"
   漫剧里同场景同角色的镜头极多，这一条能直接砍掉大量重复编码。

3. **解析器/契约的"代数指纹"自愈** — `YiIimini/NiliX`
   `script_parse_ver` 随解析器升级自动失效旧产物，**根治"解析失败静默回退 LLM 直出后坏方案被永久复用"**。任何做 schema 演进的系统都该有这个版本号。

4. **ComfyUI 渲染检查点（prompt_id 落盘 + 查 history 收回）** — `YiIimini/NiliX`
   > "渲染检查点：ComfyUI prompt_id 提交即落盘，崩溃/重启后续跑先查 history 收回已完成任务（绝不重复烧 GPU）；白名单含 all，断电重启自动续跑。"
   `qiukaihui/comfyui-auto-drama` 用另一种做法：SQLite 状态 + `chain_daemon.py` 链式守护进程自动推进下一段。

### 6.2 MiniMax H3 / ComfyUI 专属的工程细节（只有真正跑过 H3 的项目才有）

5. **H3 开头"起始音节"杂音自动裁剪** — `qiukaihui/comfyui-auto-drama`
   > "H3 每段开头自带约 0.12-0.16s『起始音节』，一键合成时已自动裁剪；单段预览保留原始开头。"
   分段生成的漫剧一定会在每个拼接点听到"咔"声，这一条不解决就永远拿不到可交付成片。

6. **对白说话人 ID `(S1)/(S2)` + 音色锁定 + Ref2VA 六段式自动包装** — `qiukaihui/comfyui-auto-drama`
   提交带参考图的 R2V 任务时自动包装官方 Ref2VA 六段式（`subject_definitions` / `summary` / `retention_analysis` …）；对白强制说话人 ID；还**在提交前检测服务器是否加载了 `r2v.unet` 的 Ref2VA 权重，缺失则回退 FL2VA 并在界面提示"身份锁定弱"**。这种"能力探测 + 显式降级提示"比静默出烂片强。

7. **H3 跨段衔接：latent 级接续 + 音频续播 + Seam Probe** — `NikoDemon80/ComfyUI-H3-Motion-Context`（⭐1,006，GPL-3.0）
   > "This pack slices the previous clip's tail straight out of its latent, so the pinned frames are the same numbers they were, bit for bit."（避免每次接缝都"解码→缩放→重编码"造成色偏与糊化）
   > "the pinned window has to END at the join and reach backwards into the sound that already played. That is the difference between the model continuing your track and the model writing something that sounds like it."
   还带 **Seam Probe**："measures whether a join is a real continuation or a convincing imitation"，并在启动时对 ComfyUI 布局做算术校验，**对不上就拒绝运行**（"A loud failure beats a bad render you don't notice."）。
   这比常见的"上段末帧→下段首帧"高一个档次，且直接可用（ComfyUI 0.34.0+）。

8. **尾帧缺失的 FFmpeg 兜底提取与自动注入** — `Anning01/novelvids`
   > "供应商未返回尾帧时，后端通过 FFmpeg 从成片提取；下一镜头自动把该图片作为首帧参考。"
   云端/本地视频模型"不保证返回尾帧"是常态，这个兜底让"無人值守逐镜执行"真正跑得通。

### 6.3 一致性落到数据结构（而不是 prompt 技巧）

9. **不可变稳定资产 ID + 版本化状态变体 + append-only Take Log** — `siyuan-liu31/agentic-comic-drama-studio`
   > "Give stable state variants—costume, wet/dry, damaged/intact, door open/closed—separate versioned IDs."
   > "已被剧集引用的资产禁止覆盖，只能新建版本并显式迁移引用"；配 `manju_repo.py validate`、`lint-prompt --strict` 校验与单变量诊断修复。**这套 YAML 契约是我们最该抄的 schema 设计。**

10. **镜头就绪状态机 + 候选确认闸门 + 共享实体模型** — `Forget-C/Jellyfish`（⭐6,480，Apache-2.0）
    > "script breakdown → shot preparation → candidate confirmation → shot ready → generation workspace"
    > "Centralized character, scene, prop, and costume management reduces drift across shots." + 名称查重鼓励复用 + 单镜/批量 pre-check。

11. **跨项目角色资产库（形象指纹 sha256 命中零渲染）+ 双形态契约** — `YiIimini/NiliX`
    `char_lib/` 独立目录跨项目复用；"双形态契约"（`真身提示词：` → `second_form` → `_form2.png` → 前端"真身·角色名"切换）；`novelvids` 更进一步支持变装/年龄状态衍生形态，**每个形态独立配图片与音色**并可章节级绑定。**漫剧里"同一角色不同形态"是高频需求，但 18 个项目里只有 4 个做了。**

12. **`@资产名 / @音频N` 显式引用语法** — `Anning01/novelvids`、`xxx-888/super_gen`
    > "最终 Prompt 使用 `@{资产名}` 与 `@音频N` 显式绑定角色、场景、道具和角色音色"；super_gen 更进一步：Tiptap 编辑器里 @ 芯片引用，**被引用素材自动作为 `reference_image/video/audio` 随请求发出，且提示词"原文直发"不注入任何自动绑定语**。这解决了"防止自动注入破坏我们手调的 prompt"这个真实痛点。

### 6.4 质检、审片与花钱

13. **VLM 审片八维度 + 修复师定点返工 + 程序侧算分** — `YiIimini/NiliX`
    主体 20 / 场景 12 / 动作 15 / 运镜 10 / 可见性 15（近黑防线）/ 技术 15 / 风格 8 / 口型 5，**加权分在 Go 侧计算，不信任模型自报**；审不过由"修复师"改写提示词**定点返工**；预算耗尽自动拍板；还有**草稿预审**（半分辨率审片→定稿全分辨率零返工）。

14. **渲染后机械质检清单 + slideshow 风险分** — `calesthio/OpenMontage`（⭐6.1万，AGPL-3.0）
    > "after every render, the runtime runs ffprobe validation, extracts frames at 4 positions to check for black frames and broken overlays, analyzes audio levels…"
    外加 6 维 slideshow 风险打分，专门防"会动的 PPT"。这是**不依赖大模型**的廉价质检层，和 13 的 VLM 审片互补。

15. **人工审批门 + checkpoint 拒签** — `calesthio/OpenMontage`
    > "checkpoint writer 拒绝没有记录审批的『已完成』门禁阶段"；proposal/script/scene plan/assets/publish 五道门强制暂停等签字。
    把"人工审"写成**状态机的约束**而不是 UI 上的一个按钮，是这套设计最硬的地方。

16. **预算治理：执行前预估 → 预留 → 事后对账 + observe/warn/cap + 单笔阈值审批** — `calesthio/OpenMontage`（默认总上限 $10，单笔 >$0.50 需批准）
    `Anning01/novelvids` 做了更细的计费账本："记录 token/张数秒数/输入素材/定价快照/折扣/金额/请求时长，成本看板按项目过滤汇总；MiniMax H3 按输出秒数与输入图片/视频分别计费"；`xxx-888/super_gen` 有 `/credits/estimate` 生成前预估 + **失败自动退款**。

### 6.5 长文与工程可用性

17. **长文四层连续性记忆 + Change Record 因果链** — `appolloqin/huohuo-drama`
    > "four-layer continuity memory: a global state snapshot, previous-chapter tail, earlier summaries, and keyword-retrieved ledgers — all injected with hard size caps"
    > "each chapter ends with a Change Record block where every state shift must spell out trigger → process → outcome"
    把长篇一致性做成**可存储、可注入、可审计**的机制，而不是"塞一个超长 prompt"。

18. **"服务端继续跑 + 刷新恢复进度"** — `appolloqin/huohuo-drama`（`GET /api/v1/batch-jobs/active`）、`Anning01/novelvids`（定时 reconcile 排队/生成中任务，关页面后完成、计费、尾帧提取与下一镜注入继续执行）
    单卡流水线一次跑几小时，**"关掉浏览器不能停"是基本要求**。

19. **批量清单启动前统一预检** — `harry0703/MoneyPrinterTurbo`（⭐12.5万）
    > "清单最多包含 100 个任务且不超过 1 MiB。所有条目会在第一个任务启动前完成参数与本地文件预检"
    避免"跑到第 37 条才发现路径写错"。

20. **多 Provider 能力表 + 提交前参数校验 + "未知结果不盲目重发"** — `coracoo/Slate`
    `workbench/tools/video_profiles.py` 定义各视频型号的分辨率/时长能力，界面按型号过滤，**未知型号或不兼容参数在提交前拒绝**；任务失败记录失败原因，**未知名结果不盲目重发**（云 API 计费下这是省钱刚需）。它还有"公网素材出口"设计：用带临时签名的 URL 把本地参考音视频暴露给云端模型（默认 24h 有效期），解决"云端模型读不到我本地参考图"的问题。

21. **剪映草稿导出（把精修让给别人）** — `linyqh/NarratoAI`、`YiIimini/NiliX`（`pyJianYingDraft`）、`ArcReel/ArcReel`、`Agions/fablr`
    与其自研完整 NLE，不如把"视频+字幕轨"导出成剪映草稿，让人工在成熟工具里做最后 5%。**这可能是"不要自研编辑器"派最有力的证据。**

22. **三层视频模型 S/V/E（转场镜头→分镜视频→集视频）** — `coracoo/Slate`
    参考帧、视频、宫格分别使用不同提示词；支持编辑、模型优化、关键帧采用和尾帧续接。这套分层让"分镜级产物"和"集级产物"解耦，比"一镜一文件然后 concat"更抗改。

---

## 7. 明确的"不要做清单"（过度设计 / 不适合单机自用）

依据：这些能力在本次调研中大量出现，但**要么与"单人单卡产漫剧"无关，要么其复杂度远超收益**。

| 不要做 | 谁在做 | 为什么不要做 |
|---|---|---|
| **多租户 / 团队协作 / 角色权限组** | `xxx-888/super_gen`（多组织多租户）、`123Mr-king/waoowaoo`（BYOK 计费）、`appolloqin/huohuo-drama`（SaaS 化） | 单机自用没有第二个租户；多租户会污染数据模型（每张表都要带 tenant_id），拖慢一切 |
| **积分计费 / 支付渠道 / 发票** | `huohuo-drama`（Stripe/PayPal/微信/支付宝）、`super_gen`（积分账户+失败退款） | 我们只需要"这次花了多少钱"的成本统计，不需要钱包。成本统计照做，计费系统不做 |
| **Selenium 多平台自动发布（抖音/快手/小红书/视频号/TikTok）** | `ddean2009/MoneyPrinterPlus`、`FujiwaraChoki/MoneyPrinter`、`MoneyPrinterTurbo` | 与"生成质量"无关，且极易碎（登录态、风控、DOM 变更）。漫剧交付是文件，不是发布 |
| **分布式渲染农场 / 多机 LAN 编排** | `RmaNMetaverse/ComfyUI-Orchestrator-LAN`、`mcmonkeyprojects/SwarmUI`（多用户/多会话） | 我们只有 1 张 5090。单机瓶颈是显存与串行队列，不是调度拓扑。队列做好即可 |
| **无限画布 / React Flow 节点编排器** | `HBAI-Ltd/Toonflow-app`、`xxx-888/super_gen`、`869413421/ai-moive-studio`、`Agions/novella` | 交互复杂度极高（节点连线/引用解析/状态同步），但对我们"小说→漫剧"的线性流水线边际收益很低。**极简 Web 控制台更适合：表单 + 表格 + 逐镜预览** |
| **通用 DAG 平台作为运行时（Airflow / Dagster / Prefect / Flyte / Temporal）** | 见方向 8 | 它们解决的是"跨团队、跨集群、数千任务的调度与可观测"，代价是元数据库、调度器常驻进程、概念负担。我们需要的只是**阶段级幂等 + 断点续跑 + 产物指纹**，一张 SQLite 状态表 + 一个 `run_stage()` 循环就够。**抄概念（幂等键/重试策略/产物指纹），不要引框架** |
| **3D 白模 / Blender 预演 / 深度与剪影** | `coracoo/Slate`（拉片线：深度动作、2D/3D 白模） | 是"拉片分析"的附加价值，与生成链路无关；引入 Blender/推理依赖会让部署变重 |
| **数字人 / 口型同步 / 动作迁移 / 换脸** | `ATH-MaaS/Pixelle-Video`（扩展模块）、`Gourieff/ComfyUI-ReActor` | 漫剧是二次元绘画角色，"对口型"通常用 H3 原生对白 + `(S1)/(S2)` 说话人锁定已够。这类模块会显著拉长单镜耗时 |
| **电商带货 / 多平台分发 / 消重矩阵** | `xixihhhh/clipforge`、`Agions/fablr`（5 级消重合规矩阵） | 面向"批量铺量营销"，与"做一部好看的漫剧"目标相反。消重会主动降低画质与一致性 |
| **Electron 桌面壳 / 灵动岛 HUD / 风扇超频 / 硬件控制** | `YiIimini/NiliX`（灵动岛、雷神 EC 通道、NVAPI 超频） | 与生成无关的"产品化装饰"。**但它的提示词纪律 / 审片闭环 / 缓存指纹要抄，外壳不要抄** |
| **自研 MCP / A2A / 黑板式多智能体架构** | `Agions/novella`（ProjectBlackboard + Hub-and-Spoke）、`neopen/story-shot-agent`（MCP+A2A） | 多智能体编排在"阶段固定、产物固定"的流水线里引入不确定性（谁在什么时候改了哪个产物）。**用确定性 DAG + 少量 LLM 调用点更可控** |
| **自研完整 NLE（多轨时间轴 + 关键帧 + 特效）** | `chatman-media/timeline-studio`、`pireel/pireel`、`OpenCut-app/OpenCut` | 这是几个月的工作量且永远打不过剪映/达芬奇。**导出剪映草稿或 SRT + 用 ffmpeg 做最终合成**即可 |
| **Postgres + Redis + BullMQ + MinIO 全家桶** | `123Mr-king/waoowaoo`（Redis+BullMQ+MinIO）、`Forget-C/Jellyfish`（MySQL+Redis+RustFS） | 面向多用户与水平扩展。单机自用 SQLite + 文件系统 + 进程内队列更快、更好备份、更好调试（`NiliX` 用 SQLite/JSON 落盘跑得非常好） |
| **把"自动发布/定时任务/多平台账号"做进核心数据模型** | 多个项目 | 会让项目/集/镜头的核心模型被外围需求污染 |

---

## 8. 数据来源、可复查文件与"未验证"说明

### 8.1 本次调研产出的可复查文件（均在工作目录 `_research/` 下）

| 文件 | 内容 |
|---|---|
| `_research/meta.json` | 1,082 个仓库的 `stars / license / pushed_at / language / archived / forks` 快照 |
| `_research/pool.json` | 34 条 GitHub Search 查询的原始结果（用于"有没有漏掉大项目"的复核） |
| `_research/merged.json` | 106 个项目的合并数据集（元数据 + 定位 + 核心功能 + 20 项 flags + 原文引用 evidence） |
| `_research/out/*.json` | 按 16 个分组落盘的原始结构化抽取结果（每条判断都带 README 原文引用，便于复核） |
| `_research/readmes/*.md` | 106 份 README 本地快照（判定依据的原始文本） |
| `_research/cards.md` / `tables.md` | 本报告 §3 / §4 的生成结果 |

### 8.2 明确"未验证"的部分

- **单细胞级别的功能判定**：表格中的 **?** 一律表示"README 未覆盖"，不代表项目没有该功能。特别是"产物幂等指纹""机械质检""成本统计"这三项，很多项目在代码里有但 README 未写，**被低估的概率很高**。
- **`Agions/novella`、`xinzhuzi/MYStudio`、`xxx-888/super_gen`、`appolloqin/huohuo-drama`、`coracoo/Slate` 等中低 star 项目的"完成度"**：README 描述的功能不等于已实现（例如 `oskey/kt-ai-Studio` 的一键合成与人物参考图重构明确列在 Roadmap 未完成项；`grokbot-ai-manju` README 自述"H3 原生音频链路与生产调度 CLI 仍在进行中"，版本 v0.0.1）。
- **`wjmboss/ArcReel` 与 `ArcReel/ArcReel` 的关系**：两者描述高度相似，API 显示 `wjmboss/ArcReel`（⭐2，AGPL-3.0，2026-05-09 推送）与 `ArcReel/ArcReel`（⭐5,142，AGPL-3.0，2026-09-23 推送）并存。本报告按"组织版为当前主仓库"处理，**该判断未验证**。
- **已经消失/改名的仓库**：`dzhng/structured-output`（本次调研时 API 返回 404，已从清单剔除）、`AIDC-AI/Pixelle-Video → ATH-MaaS/Pixelle-Video`、`ltdrdata/ComfyUI-Manager → Comfy-Org/ComfyUI-Manager`、`SamurAIGPT/AI-Youtube-Shorts-Generator → Anil-matcha/AI-Youtube-Shorts-Generator`。
- **star 数**：为 2026-09-23 快照值，会有正常波动（复核时发现数小时内 ±10 的漂移属正常）。
- **`jmilinovich/comicgen`、`c-wang-dev/storyboard-master`、`hoangminhanhtai/openreel-video`、`Infinishot/comfy-executors` 等 ⭐≤5 的项目**：README 可读但缺乏社区验证，判断仅代表"作者自述"，选型前需自行试跑。

### 8.3 一句话结论

**对我们最有价值的不是"找一个能直接用的项目"，而是三件事的组合：**
1. **抄 `comfyui-auto-drama` 的 H3 工程细节**（说话人 ID、Ref2VA 六段式、开头杂音裁剪、失败码）+ **直接装 `ComfyUI-H3-Motion-Context`** 解决分段衔接；
2. **抄 `NiliX` 的幂等/续跑/审片闭环设计**（manifest 指纹、条件缓存、prompt_id 检查点、八维度判分与定点返工、双形态与跨项目资产库），但**用 Python 重写、不上它那套桌面壳**；
3. **抄 `agentic-comic-drama-studio` + `grokbot-ai-manju` 的产物契约**（稳定资产 ID、版本化形态、`shot_id` 重渲、目录契约、`qc.md`），把"分镜 schema"当成整个系统的第一等公民——这是后续所有能力（断点续跑、机械质检、人工审片、成本统计、多集管理）的地基。
