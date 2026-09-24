# 待办清单（来自 GitHub 调研 + 本机节点包发现）

> 每份调研报告到货后追加。按**投产比**排序，最终统一处理。
> 状态：`待定` = 等用户拍板 / `可做` = 已确认值得做 / `已完成`

---

## 一、正确性隐患（最高优先）

| # | 项 | 依据 | 状态 |
| --- | --- | --- | --- |
| 1 | **`gen.py` 的 no-ref 分支段落结构错误**：空镜走 `MiniMaxH3AudioConditioningT8`（FL2VA/T2VA 系），但传进去的是**六段式**；实测结论是 FL2VA 系应用**三段式**（`integrated_multimodal_description` / `overall_soundscape` / `non_diegetic_music`） | Director-WebUI `timelineProject.ts:531-543` 与 `test/timelineProject.test.ts:152`（显式断言 `not.toContain("subject_definitions:")`） | 待定 |
| 2 | **concat 音画不同步根治**：无音轨段补 `anullsrc` 静音；concat 文件**显式写 `duration`**；音频滤镜链 `aresample=48000:async=1:first_pts=0,apad`。建议 **concat 与字幕烧录分两步**（第二步才有可靠时间轴基准） | Director-WebUI `media.py:514-644` | 待定 |
| 3 | **Autogrow 稠密槽位 fail-closed**：`ref_images.ref_image_N` 若出现稀疏槽位，**直接报错，绝不静默重编号**（Autogrow 展示序 = 提示词编号）。我们**服装变体删中间图时必踩** | Director-WebUI `native_templates.py:1250-1259` | 待定 |
| 4 | **重启后先定向 cancel 已知 prompt_id 再重提交**，不假设"缺席 = 已死"（防"旧 prompt 迟到入队"） | Director-WebUI `docs/architecture.md:146-154` | 待定 |

## 二、能力增强（已确认本机支持，未使用）

| # | 项 | 依据 | 状态 |
| --- | --- | --- | --- |
| 5 | **尾帧续接**：`MiniMaxH3ImageToVideo` 的 `first_frame`+`last_frame`（模型名 `fl2va` 本就为 First-Last 设计）。完整裁剪数学见报告：`ImageFromBatch(batch_index=-N,length=N)` → `AddGuide(frame_idx=0)` → 采样 `align(F+N)` → 保存前 `ImageFromBatch(batch_index=N,length=F)`；N ∈ {5,22,39,56} | 本机 `/object_info` + Director-WebUI `native_templates.py:1361-1421` | 待定（建议先做 3 镜对照实验） |
| 6 | **长片编排**：`MiniMaxH3LongVideoOrchestratorT8` + `LongVideoPlannerT8`（固定窗口时间轴、每段不同提示词、从未接受的 manifest 段续跑、完成时阻止多生成一段） | 本机 `/object_info` | 待定 |
| 7 | **latent 级检查点**：`MiniMaxH3NativeLatentCheckpointSave/LoadT8Advanced`（无 pickle `.h3latent.safetensors`，原子写、不覆盖） | 本机 `/object_info` | 待定 |
| 8 | **接缝漂移修复**：`LongVideoSeamDriftT8Advanced` + `LongVideoColorMatchT8Advanced` | 本机 `/object_info` | 待定 |
| 9 | **角色音色锁定**：`VoiceProfileT8` + `VoiceLibrarySave/LoadT8` + `SpeechConditioningT8` | 本机 `/object_info` | 待定 |
| 10 | **台词边界精确对齐**：`DialogueBoundaryAnalyzerT8`（本地 faster-whisper，只在"恰好一个连续精确匹配"时报边界） | 本机 `/object_info` | 待定 |
| 11 | **帧数上限可放宽**：实测合法上限 **498 帧 = 20.75s**（我们现限 362 = 15.08s）。想拍更长镜头时可放开 | Director-WebUI `native_templates.py:245-252` | 待定 |

## 三、工程与体验

| # | 项 | 依据 | 状态 |
| --- | --- | --- | --- |
| 12 | **LoRA 按文件名路由映射表**（fail-closed）：`*_T8`(4/8step) → `LoraLoaderBypassModelOnly`；`*_comfyui_bf16` → `LoraLoaderModelOnly`。**我们的 `fl2v_turbo_8step_..._comfyui_bf16` 属于哪类要核实**（名字同时含 `8step` 与 `comfyui_bf16`） | Director-WebUI `docs/native-workflow-execution.md:155-162` | 待定（**需核实，可能影响画质**） |
| 13 | **加权进度阶段模型**：`{id,label,node_id,kind,weight}`，采样 0.70 / 解码 0.15 / 封装 0.10 / 落盘 0.05，且拒绝伪造进度 | Director-WebUI `workflow/execution_hints.py` | 待定 |
| 14 | **模板 Bundle 版本冻结 + golden 回归**：manifest 指纹加 `template_bundle_version` 维度 | Director-WebUI `native_prompt_goldens.json`（372 KB） | 待定 |
| 15 | **ffmpeg 场景检测**（`select=gt(scene,T),showinfo`，三档阈值 0.55/0.35/0.18）给 DeepSeek 拆镜做**视觉交叉验证** | Director-WebUI `media.py:685-740` | 待定 |
| 16 | **提示词与配置的分字段批量传播**：8 个布尔开关 + `scope: following/selected`，**prompt 默认不传播**，prompt 与 promptReferences 必须同时勾选 | Director-WebUI `timelineProject.ts:239-260,1589-1672` | 待定 |
| 17 | **结构化错误码**（`(code, message, remediation)` 三件套，它的 zh-CN.json 有 ~30 条可照抄形态） | Director-WebUI `zh-CN.json:119-165` | 待定 |
| 18 | **显存优化**：它是**故意不加 unload 节点**、靠稳定 loader ID 让 ComfyUI 复用缓存 → **我们不该主动清显存**。可试**把 CLIP/VAE 放 CPU**（独立设备放置）给 UNET 腾空间，代价是 encode/decode 变慢 | Director-WebUI `docs/architecture.md:183-184` + `workflow/interpreters/` | 待定 |

## 四、我们的净优势（**不要去补**）

| 项 | Director-WebUI 的状况 |
| --- | --- |
| 角色/定妆/服装系统 | **完全没有**（`角色`/`服装`/`persona`/`costume` 排除测试后 0 命中），它自己承认没做"人物/服装/场景连续性 bible" |
| 中文字幕 | **完全没有**（`subtitles=`/`.srt` 0 命中） |
| 机械质检 | **完全没有**（`blackdetect`/`freezedetect`/`silencedetect` 全 0） |
| 抽卡（多候选 + 用户挑选 + accepted/stale 生命周期） | **比我们弱**：每 job 每段 1 个 take，靠重跑累积；文档承认"不会把最新自动持久化为用户已接纳版本" |
| 服务端提示词结构校验 + `<Subject N>` 交叉引用 | **它零强制力**，六段式只是前端一个按钮；只查 7000 字符长度 |
| 纪律行（CAMERA/POSITION DISCIPLINE） | **全仓库 0 命中** |

## 五、明确不要做（负面清单）

1. 三位数 KB 单体文件（`app.py` 463KB/~11k 行等）——**借机制，不借形态**
2. 三代编译器并存（V4/V5/V6 + `legacy.py` 46KB）——存量包袱，我们从零自建只做一代
3. RayLight 多卡 + 222 文件 bundled fork——单卡 5090 收益为零
4. CK Attention 7 种 reason code 状态机——调试成本极高
5. 8 个 localStorage WAL key 的迁移考古——抄它**当前形态**即可
6. 全局 `keep_until_switch` 显存常驻——多卡机制
7. **它没有自动重试/指数退避、无 OOM 检测、选 turbo LoRA 不自动改 steps**（默认 25）——**别抄这些缺陷**，我们已有更正确的做法

---

## 附：报告文件

- [Director-WebUI-调研报告.md](Director-WebUI-调研报告.md)（1393 行，附录 A 是完整常量速查表，可直接当实现 spec）
- 其余 4 份调研（storyforge+wind-comic / printfilm+ai-drama-generator+milimovideo / 生态全景 / Slate+队列管理）到货后追加

---

# 第二批：Slate / MCWW / acqm / storyboard-master 调研

> 报告：`research/reports/07-external-research-slate-mcww-acqm-sbmaster.md`（919 行）
> 全部 clone 读真实源码（MCWW 例外：仓库 99% 是文档截图，改用 GitHub Tree API 精准抓 15 个源码文件）

## ★★ 两项推翻既有结论的发现（最高优先）

| # | 项 | 为什么重要 |
| --- | --- | --- |
| 19 | **`lines[]` 镜内台词轨** `{at, dur, speaker, text}` **取代 `dialogue` + `narration`**；`narrator` 是保留字不是角色 | **推翻我"52 镜是结构下限"的结论**。我之前判定"每镜只能一句台词 → 镜数下限 = 对白句数（29）→ 19 镜目标不可能"，但那是**我们 schema 的限制，不是 H3 的限制**。有了镜内台词轨，一镜可承载多句对白 → 短镜可以合并 |
| 20 | **`units[]` V 层聚合**：H3 是 4-15s/次，**3 秒镜和 15 秒镜显存占用相同**。按"相邻且同 scene_id"确定性分组，**2-3 个短镜合并成一次 9-12s 请求，段内切镜由模型完成** | **直接解决"拆太碎"**：52 个逻辑镜头 → 约 20 次 GPU 请求。保留分镜粒度（用户能逐镜编辑）同时把渲染次数压下来。校验必须"按原顺序完整覆盖、不漏不重不调序"；共享负面**不能把各镜负面求并集**；组合提示词按 `0-3秒/S1：景别；角度；焦距；运镜；动作与视线；台词与环境声` 组织，**时间与图片序号由编译器填，不靠 LLM 数编号** |
| 21 | **`sec_actual` 必须用 ffprobe 回读**：`sec`(规划) / `sec_override` / `sec_actual` 三层；**concat 必须用 sec_actual** | H3 有 `frames % 17` 步进，**请求 6s 实出 5.96/6.04s**，按请求值算时间轴会**累积漂移** → 字幕错位。这是正确性问题，不是优化 |

## 正确性 / schema 卫生

| # | 项 | 依据 |
| --- | --- | --- |
| 22 | **`camera` 拆成枚举 + 英文参数**：我们的 `camera` 现在是自由文本 `"缓推（Push In, small amplitude, slow speed）"`——**英文渲染细节泄漏进中文枚举位**。应拆 `camera`(枚举 15 值) + `camera_detail`(英文参数)，并补 `angle`(8 值) | `creation_pipeline.py:714-731,798-802`、`prompt_modules.py:329-334` |
| 23 | **受控词表 + 契约归一 + 校验失败拒绝落盘**：shot_size 8 值 / camera_move 15 值 / angle 8 值 / transition 9 值；词表外回退首项、dur clamp [1.5,15]、id 重复改名；有 error 就 exit(1) 不写半成品 | `creation_pipeline.py:714-731,798-802` |
| 24 | **排队宽限**：超时先查 `/queue` 位置，仍在队列就宽限并打日志；只有"不在队列也无 history"才判 ComfyUI 重启丢失 | Slate `comfyui_client.py:374-385`；MCWW `queueing.py:267-285` |
| 25 | **取消用两段式**：先 `POST /queue {delete:[pid]}` 再 `POST /interrupt {prompt_id}`（比单发 interrupt 精确） | MCWW `comfyAPI.py:60-96` |
| 26 | **状态 key 带源码/契约哈希 → 更新即作废，零迁移代码**：加 `schema_version` + `prompt_version` + `script_rev`（章节内容指纹，`script_rev > board_rev` 则 UI 标 stale）；控制台前端状态 key 用 `vm/shots.py` 内容哈希 | MCWW `utils.py:189-205`、`queueing.py:493-513` |

## 能力增强

| # | 项 | 依据 |
| --- | --- | --- |
| 27 | **表演 beat 化**：① 有台词/走位的镜头让 LLM 出 `beats[]={at,duration,posture,gaze,gesture,expression,voice}`，机械校验 `at+dur ≤ sec`、角色必须在场、不得新增未登记角色；② `facts[]` + `known_facts`（**事实默认对角色不可见**）防"第 3 镜角色说出第 10 镜才发生的事" | `演员表演契约.md:7-47` |
| 28 | **决策表后处理**（storyboard-master）：景别表（信息焦点→远景~大特写）、角度表（权力关系→仰/俯/平/POV/荷兰角）、运镜表（运动目的→跟移/推/拉/摇/手持/固定）、光影表（情绪→低调冷色/高调暖色/硬侧光/逆光剪影…）、节奏表（对白 3-5s 少切 / 动作 1-2s 快切 / 悬念缓慢推近+停帧）。**仲裁优先级写死：情绪基调 > 信息层级 > 权力关系**。可做 `plan.py` 拆镜后的**确定性后处理** | `knowledge/分镜决策引擎.md`、`decision.py`、`grammar.py` |
| 29 | **人物数量 → 机位语法**：1 人 POV/客观；2 人 外反拍/内反拍/过肩（**保持关系线一侧**）；3 人 三角形+枢轴；4+ 主镜头先行→局部切入→反应镜头。附**越轴提示**与匹配原则（位置+动作+视线，切点在动作中段） | `grammar.py`；Slate 也在提示词里写死了同样规则（`prompt_modules.py:338`） |
| 30 | **时长用"信息点定档"而非字数/语速**：`info_point_count = len(dialogues) + len(actions)` → ≤1:5s / ≤3:10s / 其他:15s；再按运镜复杂度修正（固定 1、推/拉/摇 2、跟移/手持 2-3） | storyboard-master（**全仓无字数÷语速计算**——我们现在用的 4 字/秒是唯一口径，可作交叉校验） |
| 31 | **角色锚定卡**：性格范式 / 标志微表情 / **分档情绪矩阵**（轻→中→重→爆发）/ **禁止表演清单挂在角色上而非每镜重写** | storyboard-master |
| 32 | **尾帧续接两模式**：`tail_context`（ffmpeg 截末帧 → Vision 出描述文本，注入提示词）vs `tail_first_frame`（末帧当真实首帧）。**前者先用现有能力就能做，不需要换节点** | Slate `reference_contract.py` |
| 33 | **机位→几何映射做越轴/景别一致性质检**：景别→fov→距离对照表 | Slate `analysis_to_storyboard.py:8-14,69-70`；`previs_system/` 的 2D 是纯确定性 Python（numpy+PIL+cv2） |
| 34 | **队列能力**（MCWW 逐条核实）：优先级（default 1 / max 3，同优先级 FIFO）、桶内上移/下移、暂停/恢复、取消排队项、**软取消**（跑完当前再停批，`cancelBatchSoft`）、并发硬约束 = 1、队列持久化（15s autosave，maxQueueSize=200） | `mcww/queueing.py` |
| 35 | **失败熔断**：`_handleProcessingError` 捕获 `ComfyIsNotAvailable`/`UnqueuedByComfyUI` 后**自动 `_paused = True`，绝不重投** | MCWW `processing.py` |

## 三条要避开的坑（报告第七节）

1. **契约不要分裂**：Slate 有 **3 套 shots 契约 + 2 个桥接器**（previs / dialogue / production），自己正在为分裂付成本。→ 我们**只允许一套 shots 契约 + 版本号**，白模/宫格这类新用途用**可选旁挂数组**（`units[]`/`grids[]`），不新建第二套 shot 结构
2. **不要抄 acqm 的"参数存在但不用"**：它的 `priority` 参数被接收+校验范围，**既不在模型里也不在表里**，所有查询 `ORDER BY created_at DESC`；执行体是 `time.sleep(0.1); return str(uuid4())` 的桩。→ **加字段要么有消费方要么别加，并用测试断言"字段被读"**
3. **不要让请求时长冒充实际时长**（见 #21）

## 分镜表 schema v2 落地顺序（报告 7.3 节有完整字段表）

| 阶段 | 内容 | 预估 |
| --- | --- | --- |
| **P0**（半天，零风险） | `schema_version` / `prompt_version` / `script_rev` + `camera` 收枚举补 `angle`/`lighting`/`negative` | 立刻消除"英文参数泄漏进枚举位" |
| **P1**（1 天） | 指纹按阶段拆 + `sec` 三层 + **concat 用 `sec_actual`** | 消除时间轴累积漂移 |
| **P2**（1-2 天） | `lines[]` + `narrator` 保留字 + `chars` 改角色 ID + `asset_revision`/依赖图 | 解决"一镜多句对白" |
| **P3**（2-3 天，**收益最大**） | **`units[]` 聚合 + 编译器**：让 H3 一次请求出 2-3 个短镜 | 渲染次数 52 → 约 20 |
| **P4**（可选） | `beats[]` 表演层 + `tail_context` 尾帧描述注入 + 决策表后处理 | 质量提升 |

## 局限（报告作者如实标注）
- MCWW 未完整 clone（网络被并发克隆占满），改用 Tree API 抓 11 个 py + 4 个 js；**未深挖「节点标题约定 → UI 元素」映射**
- Slate 的 Vue 前端只读了两个视图片段；**三层视频 S/V/E 是设计稿不是已上线功能**（CreateView.vue 仅 167 行）
- 三个项目**均未实际运行**，结论来自静态阅读；标「推断」处无法静态确认
- storyboard-master 的「9.8/10 评测分」是作者自评，未复现、未采信

---

# 第三批：GitHub 生态全景（1082 仓库 → 106 项目深读）

> 报告：`AI短剧漫剧开源项目全景图谱.md`（887 行）；原始数据 `_research/`（106 份 README 快照 + 20 项功能矩阵）
> star/license/推送时间为 2026-09-23 快照

## ★★★ 最高价值的单条行动：直接装 `ComfyUI-H3-Motion-Context`

| 项 | 内容 |
| --- | --- |
| 仓库 | `NikoDemon80/ComfyUI-H3-Motion-Context`（**⭐1006**，GPL-3.0，ComfyUI ≥0.34.0） |
| 它做什么 | **从 latent 层**把上一段 H3 的**尾帧 + 音频**接到下一段 —— 避开了"解码→缩放→重编码"造成的**色偏与糊化** |
| 音频 | 锚定窗口"**以接缝为终点向前回看**"，实现真正续播（不是从下一段开头重新起音） |
| 诊断 | 带 **Seam Probe** 判断接缝是"真延续"还是"模仿" |
| 为什么比我们想的强 | 比常见的"上段末帧→下段首帧"**高一档**：我们是解码后拿帧再重编码，它是 latent 直连 |
| 对应我们的痛点 | 正是"一镜一镜像幻灯片"的根因 |

**注意许可**：GPL-3.0。**只装节点使用**（ComfyUI 自定义节点，与我们的 Python 代码分离），不抄它的代码进我们仓库。

## ★★ 同栈项目：`qiukaihui/comfyui-auto-drama`（⭐47，MIT，Python 标准库零依赖）

**ComfyUI + MiniMax H3 ref2va 短剧流水线** —— 与我们完全同栈，可抄的工程细节：

| # | 项 | 价值 |
| --- | --- | --- |
| 36 | **H3 每段开头 0.12-0.16s「起始音节」杂音自动裁剪** | 具体、可立即验证的音频修复。我们的成片很可能有这个毛病 |
| 37 | 对白 `(S1)/(S2)` 说话人 ID + **音色锁定** | 跨镜音色一致（我们完全没有） |
| 38 | **R2V 提交自动包装官方 Ref2VA 六段式** | 我们已做 ✅ |
| 39 | **失败码体系**（`F-SUBMIT-API` / `F-TIMEOUT` / `F-COUNT`） | 比我们"字符串里带 ❌"更可程序化处理 |
| 40 | **提交前探测服务器是否加载 `r2v.unet` Ref2VA 权重**，否则回退 FL2VA 并**显式提示** | 正对应我们发现的"FL2VA/Ref2VA 两套段落结构"问题 |

## ★ 产物契约样板：`wicm84266964/grokbot-ai-manju`（⭐1，MIT）

目录契约 + `plan/episode_map.yaml` + `characters/<id>/`（**三视图 + 音色档案**）+ `qc.md` + **按 `shot_id` 单镜重渲**。
→ 产物契约落到文件系统的样板，**把分镜 schema 当第一等公民**。

## 量化结论（端到端流水线类 18 个代表项目）

| 功能 | 覆盖率 | 我们 |
| --- | --- | --- |
| 多阶段流水线 | 18/18 | ✅ |
| Web UI | 18/18 | ✅ |
| 镜头级编辑 | 17/18 | ✅（P1 刚做完） |
| 角色一致性 | 15/18 | ✅ |
| 分镜 schema | 15/18 | ✅ |
| 断点续跑 | 14/18 | ✅ |
| 多集管理 | 14/18 | ❌ **缺**（现在是单项目单章） |
| **产物幂等指纹** | **6/18（最被低估）** | ✅ **我们已有** |
| **换装/多形态** | **4/18（最被低估）** | ✅ **我们刚做完（P2）** |

**→ 报告判定"我们当前 P0 缺口 = ①幂等指纹+stale 清单 ②渲染检查点 ③镜头级重渲"，但这三项我们都已经有了**（researcher 是按项目平均推断，不是看我们的代码）。**结论：我们在"最被低估的两项"上已经领先于 18 个项目中的绝大多数。**

## 值得偷的差异化功能

| # | 项 | 来源 |
| --- | --- | --- |
| 41 | **VLM 审片闭环 + 定点返工**（八维度加权判分，**分在程序侧算，不信模型自报**） | NiliX（`YiIimini/NiliX`，⭐2，**无许可证，只能抄设计不能抄代码**） |
| 42 | **渲染后机械质检清单**：ffprobe + 抽帧查黑帧 + 音频电平 + 字幕存在性 + **slideshow 风险分** | OpenMontage |
| 43 | **人工审批门写进 checkpoint**："拒绝没有审批记录的已完成阶段" | OpenMontage |
| 44 | **预算治理**：预估 → 预留 → 对账 + observe/warn/cap + 定价快照/失败退款 | OpenMontage / novelvids / super_gen |
| 45 | **`@{资产名}` / `@音频N` 显式引用绑定**参考素材，且提示词"**原文直发**"不自动注入 | novelvids / super_gen |
| 46 | **长文四层连续性记忆 + Change Record 因果链** | huohuo-drama |
| 47 | **不可变稳定资产 ID + 版本化状态变体 + append-only Take Log + 单变量诊断修复** | agentic-comic-drama-studio |
| 48 | **解析器代数指纹 `script_parse_ver` 自愈**（解析器升级后自动判定旧产物过期） | NiliX |

## ★ 不要做清单（明确反对）

| 反对项 | 理由 / 替代 |
| --- | --- |
| **通用 DAG 平台（Airflow/Dagster/Prefect/Flyte/Temporal）当运行时** | **只抄概念**：SQLite 一张 `stage_state`（阶段名+输入指纹+产物路径+状态+时间戳）+ 文件哈希增量 + 写 `.tmp` 原子改名，**<200 行**就能实现 DBOS/Prefect/Snakemake/Luigi 的核心语义 |
| 多租户 / 积分计费 / 支付渠道 | 单人自用 |
| Selenium 多平台自动发布 | 不是我们的目标 |
| 分布式渲染农场 / LAN 编排 | 单卡 |
| 无限画布 React Flow 节点编辑器 | 与"零框架零构建"冲突 |
| 3D 白模 / Blender 预演 | 成本高、收益低（Slate 的做法也不适用） |
| 数字人 / 口型同步 / 换脸 | 超出范围 |
| **Electron 桌面壳 + 灵动岛 + 风扇超频** | NiliX 的**提示词纪律与审片闭环**要抄，**外壳不要**（我们已砍掉 ✅） |
| **自研完整 NLE** | 改为**导出剪映草稿** |
| Postgres + Redis + BullMQ + MinIO 全家桶 | 单机 **SQLite + 文件系统**更好调试 |

## 调研者的结论（我认同）

> 不要指望找到能直接用的项目；最优解是三者组合：
> **抄 `comfyui-auto-drama` 的 H3 工程细节 + 直接装 `ComfyUI-H3-Motion-Context`** 解决分段衔接；
> **抄 NiliX 的幂等/续跑/审片闭环设计但用 Python 重写**；
> **抄产物契约，把分镜 schema 当成系统第一等公民**。

## 未验证项（报告作者声明）
- 功能矩阵**只依据 README**，标 `?` 的不代表没有
- `wjmboss/ArcReel`(⭐2) 与 `ArcReel/ArcReel`(⭐5142) 并存，按组织版为主仓库处理
- `ltdrdata/ComfyUI-Manager` 已改名 `Comfy-Org/ComfyUI-Manager`；`AIDC-AI/Pixelle-Video` 已改名 `ATH-MaaS/Pixelle-Video`

---

# 第四批：UI/UX 调研（printfilm / milimovideo / ai-drama-generator）

> 报告：`research/reports/08-ui-ux-research-printfilm-milimo-drama.md`（992 行 / 107 处 `file:line` 证据）
> printfilm 与 milimovideo 因 clone 断连改用 jsDelivr 逐文件镜像（307 / 78 文件）；ai-drama-generator 是 Manus 生成的 MVP，**能偷的少**

## ★ 三个核心判断

1. **printfilm 最接近我们要做的**：分集工作台是**四栏 `168px | 300px | 1fr | 420px` + 底部胶片条**（`drama.css:4173-4185`），且 `lib/status.ts` 有**「不信任数据库状态字段，按素材完备度推导镜头展示态」**的完整函数 —— **直接解决用户抱怨的"哪一镜没渲看不见"**
2. **milimovideo 提供可移植的 NLE 微观交互**（像素/秒缩放、15px 磁吸、选中才出现的裁剪手柄、拖拽换序、J/K/L、20 步撤销），但它的重活（SAM3 遮罩追踪、多轨波形、漂移校正）对我们**全是负收益**
3. **镜头级筛选/搜索/多选，三个项目都没有；逐镜质检可视化，三个项目也都没有**（printfilm 只有 `AUDITING:'审核中'` 一个阶段名，`i18n/locales/zh/shell.ts:202`）→ **这两块我们必须自己设计，也正好是超越它们的地方**

## 可抄的具体设计（按我们 P3 需要）

| # | 项 | 细节 |
| --- | --- | --- |
| 49 | **派生式镜头状态纯函数** | `shotDisplayKind()` 派生 `failed\|generating\|wait_image\|wait_video\|image_ready\|video_ready`（`status.ts:138-149`），任务态来自 `project.active_tasks`（`isShotGenerating`），整片级任务用 `isProjectWideBusy` 区分。**★务必加 `stale` 档（用我们的指纹比对得出"需重渲"）—— 两个项目都缺这档** |
| 50 | **表格化清单 > 卡片网格** | 7 列 `场景\|画面\|旁白\|逐段分镜\|时长\|状态\|操作`，`table-layout:fixed`，列宽 `3rem/6.6rem/22%/30%/4rem/5.6rem/8.4rem`，行 hover 淡青柠（`printfilm.css:3985-4150`）。**卡片网格只适合窄屏，信息密度远不如表格** |
| 51 | **一眼看出状态的三重表达** | ① 缩略图格子里直接写「待出图/生成中」② 状态列三色（完成 `#3d6500`+16px lime ✓ / 警告 `#8a5a12` / 失败 `#a1261a`）③ **操作按钮随状态改名**（`生成画面↔重绘画面`、`出视频↔重生视频`），disabled 用 `title` 说明"请先生成该镜画面" |
| 52 | **聚焦式编辑弹层** | 点单元格 → 弹层 + 三聚焦模式（narration / segment / full），底部横排切换按钮（`StoryboardPage.tsx:1362-1544`）。**每字段下一行 12px 灰字写"改了会影响什么"**（如"改完需重新生成配音/成片"）—— 这就是"改完提示重渲"的最低成本答案 |
| 53 | **`@duration` 时长闸门** | 非法则 disable 保存；「逐段脚本」是**唯一真相源**，改旁白写回脚本、改脚本回填旁白+首帧+时长（`StoryboardPage.tsx:549-587`） |
| 54 | **底部胶片条** | 104px/项、选中 lime 描边、`+` 增镜、`←/→` 切镜 |
| 55 | **右下角全局队列 FAB** | badge=进行中数、面板头"3 项进行中/1 项失败/全部完成"、`排队 #N` FIFO 序号、失败就地展开"标题+原因+建议"、可取消全部/清空已结束、展开态存 localStorage、**新任务自动展开**、进行中用**不确定态动画条而非假百分比** |
| 56 | **错误三级文案 + 从历史挖根因** | 从同镜最近 20 条历史任务里挖根因、跳过"跳过重复任务/重试超过上限"噪声、提供"查看原始错误"折叠 `<pre>`（`lib/dramaGenError.ts` ≈20 条 `模式 → {title,message,suggestion}`） |
| 57 | **筛选/搜索/批量（三项目都没有，我们自建）** | 筛选 pill + 搜索 pill + hover 复选框 + 底部批量条 + **带计数的条件批量按钮**"生成未渲的 7 镜" |
| 58 | **迷你时间轴**（只读 + 点击跳镜 + 拖拽换序） | 缩放 `20px/s`（范围 5-300，Ctrl+滚轮 ×0.9/×1.1）、刻度用 **CSS 渐变 `background-size:${zoom}px`**、播放头 1px 红 + `0 0 4px` 光晕、时间码 `m:ss:ff`、磁吸 `SNAP_THRESHOLD_PX=15`、裁剪手柄**仅选中时出现**、拖拽 <5px 视为点击、拖拽换序＝**中心比较 + splice** |
| 59 | **分段播放器**（成片预览） | 单 video 顺序播、播完自动跳下一镜并回调让胶片条和中栏跟随；进度条**按每镜时长等比例分段**（`flex: segment.durationSec`）—— **既是进度条又是时长分布又是导航**。合成按钮自带分阶段文案；缺镜时先弹确认"有 3 镜还没有视频，将只拼接已生成的 16 镜" |
| 60 | **逐镜质检可视化**（三项目都没有，我们自建） | 建议：状态列多一档 `qc_failed` + 缩略图角标 + 右栏质检 tab 复用 `is-error/is-warn` 两级列表 + **用分段进度条的段底色表达绿/黄/红** |
| 61 | **面板级错误边界** | `PanelErrorBoundary`：面板级错误只坏一块，带 Retry 重置该面板 |
| 62 | **骨架屏 / 空态** | 骨架屏 `linear-gradient 200% + 1.2s ease-in-out infinite`；空态"图标+标题+一句解释+CTA"，**文案按前置条件分叉**；设计规范：空态"**无 emoji 无 3D**"、加载态"细 lime 进度条 + 队列中·预计 1 分钟 + 取消" |

## ★ 可直接抄的视觉数值（printfilm）

```css
--pf-lime:#b6ff00; --pf-ink:#111318; --pf-muted:#6b7280;
--pf-bg:#f7f8fa; --pf-card:#fff; --pf-line:rgba(17,19,24,.1);
--pf-radius:14px/10px; --pf-shadow:0 8px 28px rgba(17,19,24,.06); --pf-nav-h:64px;
/* 字体 Noto Sans SC / PingFang SC */
/* 统一焦点环 */
border-color: color-mix(lime 70%, ink);
box-shadow: 0 0 0 3px color-mix(lime 28%, transparent);
/* 时长输入 56×32 pill；弹层 width:min(560px,100%); radius:18px;
   animation:panel-in .28s cubic-bezier(.22,1,.36,1)；数字一律 tabular-nums */
```
milimovideo 暗色：`#050505/#0a0a0a/#0f0f0f/#111` + 卡片 `#1a1a1a`、主色 `#06b2eb`、小标签 10px uppercase tracking-widest white/40、滚动条 6px。
**ai-drama-generator 是"纯黑+亮红+0 圆角"粗野主义，不建议抄。**

## ★ 实现时必须补的坑（调研者特别点名）

1. **键盘处理要排除 `contentEditable`**：milimovideo 只排除 `INPUT`/`TEXTAREA`（`if tagName==='INPUT'||tagName==='TEXTAREA' return`），**我们在台词/提示词内联编辑时必须补 `isContentEditable`**，否则打字会触发播放/拆分快捷键
2. **反例（别学 ai-drama-generator）**：进度条设一次 `{0,N}` 再不更新（**假进度**）；所有卡片共享同一个 `isPending` → **点一镜全表转圈**（`Storyboards.tsx:187-190,365`）
3. **contentEditable 提示词编辑器的三个坑**：chip 要能整块退格删除、paste 只插纯文本、**后端存 token 不存 HTML**

## 花哨但不适合我们（15 条中摘要）

自由画布/无限节点（与一维分镜顺序冲突）、SAM3 遮罩+局部重绘+追踪、多轨 NLE+音频波形+漂移校正、**逐帧 trim**（AI 片段不可无损裁剪）、转场/关键帧/字幕字体工具条（**三个项目都没做**）、模板商店/社区/平台分发/分享二维码、admin 后台（85 文件）、计费钱包、中英双语 i18n、微信客服浮标、shadcn 全量组件库、可拖拽宽度侧栏+移动端适配、tRPC+Drizzle+S3 全栈。

## 单人本地明确没必要做（12 条中摘要）

登录/注册/鉴权/权限/多租户、云同步/团队协作/评论审批、服务端分页、**队列去重+项目互斥锁+用户级并发限额**（前端 disable + 后端一把锁足矣 —— 我们已有）、对象存储/断点续传、公网级指数退避心跳、通知中心/邮件/Webhook、多分辨率导出/平台封装/分享链接、模板市场、i18n/移动端断点（**只保留 `aria-live` 与 `role=dialog/tablist/listbox` 这类低成本无障碍**）、KPI 仪表盘（printfilm 自己的 Negative 清单就禁止首屏 KPI 条）。

---

# 第五批：小说→剧集多智能体流水线（storyforge / wind-comic）

> 报告：`research/novel-to-drama-开源调研.md`（1592 行，逐条 `file:line`）
> storyforge clone 成功（227 文件）；wind-comic clone 两次失败，改用 Trees API + 逐文件拉 109 个关键源文件

## 🔴 改变优先级排序的战略判断（来自 wind-comic README，调研者认为比技术细节更重要）

> 「**制作只占总成本 7.5%，投流占 70-85%**……这意味着『把制作成本再砍一半』对项目盈亏几乎无影响 —— 任何以『更便宜地出片』为唯一卖点的工具，**价值天花板极低**」
> 「AI 漫剧爆款率不足 **0.1%**、约 **90%** 公司亏损」「平台正给纯 AI 生成降权」
> **结论：价值在「拉高成品率、减少废片」，而「废片率才是这门生意的真实杀手」。**

**→ 这直接支持把工程资源投向审计/质检，而不是省 GPU 时间**（与下面 #63 的优先级一致，也让"units 聚合省时间"的排序下调）。

## 🟢 两个项目共同的最大空位 = 小说原文对白零丢失 —— **而我们已经有了**

| 项目 | 状况 |
| --- | --- |
| storyforge | **完全做不到**：原文在 `_summarize_chapter()` 就被截断到 12K 字符 + LLM 摘要，此后所有环节（分集/拆场景/分镜）**只基于摘要工作，原文再也不出现**；最终 `dialogue` 是 LLM **重新创作**的。`grep dialogue backend/` 证实全程无一处引用小说原文 |
| wind-comic | **只对「已是剧本格式」的输入做到**：`lib/script-parser.ts`（33KB）确定性正则解析器保留原文；纯小说输入走 LLM 改编，同样不保证 |

**→ 我们的 `plan.py` 已有「正文对白零丢失」检查（29 句逐字比对，实测 0 丢失），这是两个项目都没做到的事。** 报告建议的加强版：分镜表加 `dialogue: [{char, src_span:[start,end], quote}]`，**确定性校验 `chapter_text[a:b] == quote`（纯字符串比对，零成本）** + 对白覆盖率审计（「原文 47 句，采纳 44 句，遗漏 3 句」）作为可进 manifest / 可做 CI 门禁的量化指标。

## ★ 最高性价比：零 LLM 的纯函数审计层

| # | 项 | 细节 |
| --- | --- | --- |
| 63 | **对话覆盖度审计** `dialogue-coverage` | 把连续同 location 对话镜分组（同 location **一定并入**，注释：「shot/reverse 的本质就是切换说话人」），检测两类病：**多角色对话只有 1 镜**（缺正反打）、**≥2 镜但全 wide 无特写**。输出带镜号 rewriteHint。**作者自述：「漫剧约 50% 镜头是对话场景……一段连续对话用 1 个 wide shot 涵盖，显得非常『AI 一遍跑完』。这是漫剧『AI 感』最大来源之一」** |
| 64 | **节奏/冲突形状审计** `pacing-audit-v2` | `analyzeConflictShape`（最小二乘斜率 + `peakProminence<1.5`→no-climax / `slope<-0.15`→front-loaded）、`findDragSegments`（连续 ≥3 镜低于阈值）、`auditOpening`（前 1/3）、`auditDurationRhythm`（**变异系数 `cv<0.12`→呆板**）。动机：「**平均分会把『高开低走』和『层层递进』算成同一个数**」（`[1,2,4,9]` 与 `[9,4,2,1]` 平均都是 4）；「报『平均 5.2 分』没用，要指出**第几到第几镜是观众划走的地方**」 |
| 65 | **落地方式** | 新建 `audit/` 纯 Python 模块（无 I/O 可单测）。冲突分让 DeepSeek 拆镜时**顺带输出（复用同一次调用，零额外 token）**。**价值：这是渲染前质检 —— 发现「3~5 镜连续低冲突」时还没花一分钱 GPU** |

## 其它高价值项

| # | 项 | 细节 |
| --- | --- | --- |
| 66 | **逐秒 micro-beat 契约（含时长守恒校验）** | `MicroBeat = {ts, startSec, endSec, action, camera, dialogue?, characters?, mood?, microExpression?, speedRamp?}`，约束「2-4 条，每条 2-5s，**时长之和 = duration**」。`action` 必须是「**可被视频引擎执行的动词链，禁止静态描写**」，`camera` 与 `action` **分离声明**。**必须加确定性校验 `abs(sum-sec)<0.05`**，不满足则本地缩放或带错重出，别只靠 prompt |
| 67 | **角色一致性三层体系**（可只拿一层） | (a) **参考图优先级链**：`cref: 用户锁定 > 该镜出场角色定妆照 > 第一个角色`；`cw: 锁脸 125 / 主角 100 / **配角 80**` —— **「防止 MJ 把所有人都画成主角脸」**，⚠️ **我们做服装变体可能正踩此坑（权重太高脸被锁死、服装改不动）**。另一好东西 `SceneAnchorRegistry`：**同一 location 只保留首张作基线锚点**，可持久化 + 重跑回灌（首张优先不覆盖）。(b) **Style Bible 全局单锚**：先生成 1 张**不含人物不含脸的 canonical key art** 作第 1 张参考图（**必须含 `no specific characters, no faces, no body figures`**，否则被误当场景污染）。(c) **Vision 抽 DNA** 8 维 + 反幻觉约束（「**拒绝 beautiful/elegant 等抽象词**」、未识别写空串】**名字三级匹配**（原样精确→归一精确→子串双向≥2字符）修的是「林小满(镜头) vs 小满(dnaMap)」的**静默漏注入** —— ⚠️ **我们用中文名做 key，极可能有这个 bug** |
| 68 | **「成片事实表」as-shipped registry** | wind-comic 的 `voice-cast` 语义（本次最精巧的设计）：「它记录的是**成片事实**，不是配置：这一集已经播出去的声音就是这样。所以**已在表里的角色后续一律读表、不再参与轮转（哪怕阵容变了）**；表里没有的新角色在**避开已占用音色**的前提下分配并写回表；**用户手动覆盖优先级最高** —— 那是人的明确意志」。→ 抽象成通用「成片事实表」，**角色最终定妆照、场景基线锚图、关键道具外观**都适用，跨集复用有统一防漂移机制 |
| 69 | **三层成本护栏（暂停而非硬失败）** | 「`budget-enforce` 管按月，粒度太粗 —— 一次『一键成片』内部连着发几十次付费调用，**中途没有任何刹车点**；等月上限拦住时这一单已经花掉了」。**一条从真实事故学来的约束（值得贴墙上）**：「**暂停必须产出可被人看见、可被人回应的东西。一道通向不了人的告警，和没有告警是一回事。**」→ `pause` 带出「已花多少 / 卡在哪步 / **再放行多少才能继续**」；`neededCny` 注释：「**没有这个数字，人无法决定批不批**」。`HARD_MULTIPLIER=10`：「即便人一直批，也不该无限追加」。**改法：对我们换成「预计还需 N 分钟 GPU / N 次 H3 调用」，并加 GPU 时间预算** |
| 70 | **Provider 能力声明 + `diagnose_empty_chain()`** | 解决一个极隐蔽的坑：请求 30s 但所有引擎上限 <30s ⇒ 链空 ⇒ `tried` 也是空数组 ⇒ 上游拼出「**no providers**」（**误导性**）⇒ **静默降级出 10s 片，而决策日志上没有任何一行说时长被降过**。「真因是一个**静态可知**的事实」。→ **我们必须确保任何「能力不满足导致的降级」都在 manifest 显式留行** —— 本地 ComfyUI 显存上限会直接限制时长，这个坑我们一定会遇到 |
| 71 | **`spokenDialogue` 与 `visualPrompt` 分离** | 「台词原文**仅原生音频 provider 读取，不进主 visualPrompt**（非原生引擎看不到 → **不会把 CJK 渲染成画面文字**）」。**这是防「AI 把台词画成乱码字」的结构性解法**，比只在负面词加 `text` 可靠 |
| 72 | **「导演台」三态 + 下游失效预告** | 三态 `empty/ready/stale`；`stale` 两来源：(a) **被显式标记失效**（重跑上游时主动置位，**比时间戳比较更可靠**）、(b) 比上游旧（兜底）。`buildRerunPlan(assets, target) → {target, invalidates[], affected_asset_ids[], sequence[]}`：**在用户点重跑之前就预告下游影响**（「重跑『分镜表』会作废『定妆+渲染+质检+成片』，具体影响这 47 个资产 id」） |
| 73 | **情绪 → TTS 韵律的确定性映射** | 约 200 行、零 LLM：16 条中文情绪正则 → 基线 `(speed,pitch,vol)`（如 `/悲痛\|绝望\|崩溃/→{0.85,-3,0.72}`、`/温柔\|温暖/→{0.96,+1,0.80}`）；`emotion_temperature`(-10~+10) 归一后 ±；**角色性别/年龄偏置**（注释：「男角默认女声语调、老者用少年语速的**廉价感**」）。配合 #63：让 DeepSeek 拆镜时逐镜输出 `emotion` + `emotion_temperature`（同一次调用，零额外 token） |
| 74 | **资产账本（逐镜 7 维完备性矩阵）** | `{characterRef, sceneRef, storyboardImg, videoClip, dialogue, voiceover, musicCue} × {missing\|draft\|approved}` + `blockers[]`。比「第 5 镜有问题」信息密度高得多 —— 能说「**第 5 镜的角色参考图还只是 draft**」 |
| 75 | **片段重拍（Segment Retake）工程** | 解决「引擎有最短时长，想生成 2 秒生成不出来」+「缝回后总时长必须**一字不差**不变」→ 切点**先吸附帧栅格**；其余部分 **`-c copy` 字节复制**不重编码；**因时长不变，压缩时间轴/配音延迟/字幕起点/EDL record-in 全不用重算**，下游只需作废两样：成片 + 该镜口型对齐分；帧提取用**精确 seek（`-ss` 放在 `-i` 之后**） |
| 76 | **诚实的降级（10 条 verifiable 标准）** | `cameoNeedsReview`（重生仍不达标交人工而非无限重试）、`cameoRetried`（一次重生上限）、`lipsync-language-gate`（ja/ko/ru 直接 **honest skip** 而非产坏结果）、「**a failed shot becomes a labeled animatic, never a still image masquerading as video**」。Vision 阈值：Cameo <75 自动重生、Style Audit 4 维取 min（<70 重生、<85 warning）、脚本贴合度 4 维（≥75 pass / 50-74 warn / <50 fail） |
| 77 | **字幕的正确做法** | **(a) 从视频 prompt 剥离台词文本 + 激进负面词（no text/no chinese/no captions）+ 后处理用系统 CJK 字体真烧** —— 「**不要让模型画字，字由 ffmpeg 画**」；(b) **响度归一 -14 LUFS**（平台标准）+ 成片 ffprobe 体检 |
| 78 | **storyforge 的工程规范**（`docs/MODEL_POLICY.md`，本次最有复用价值的单份文档） | 分级 TTL 缓存 + 内容寻址 key（embedding 永久 / summary 24h / **scene script 仅 1h 因易重生** / health 30s，`X-Bypass-Cache` 专用于重生成）；**成本 JSONL 强制字段**按日轮转；6 级 fallback + 30s 健康检查 + 熔断 + 降级表；**可重试/不可重试显式分类**（`RETRYABLE={429,500,502,503,504}`，401/403/404/413/422 立即换）+ 分 provider 配退避；**SecretLoader 启动校验 + 拦截占位值 `change-me`/`your-key-here` + fail fast**；`asset_refs` 符号占位（LLM 只声明 `BG_OLD_STUDY` 式 SCREAMING_SNAKE_CASE，资产由后续阶段解析）；受控词表（camera 10 / emotion 12 / transition 5 硬性枚举） |
| 79 | **资产版本与锁定语义（直接抄）** | `Asset` 表有 **`variation_of` 自引用外键**（候选图谱系）、`batch_id`、**`selected`/`favorite`/`locked` 三个独立布尔位**（不是互斥状态）；`CharacterVersion` 存 `profile_snapshot` + `diff` + `created_by` + **rollback API**；`locked/locked_at/locked_by` 记录**谁何时锁的**，`PATCH` 有 lock check **409**、`DELETE` 锁定资产 **409**。**这套「lock → 409 拒改 → 需显式解锁」比我们现在「任何产物都能被覆盖」安全得多** |
| 80 | **CI 门禁 `gate:consumer`（零容忍）** | 把「改了守卫却没跟到消费方」固化成入库门禁，「上线即抓到 2 个人肉复检漏掉的真 SSRF」 |
| 81 | **工程哲学（该贴 CI 墙上）** | 「抽成纯函数不是为了复用，是为了**可测**：原来这段三元表达式内联在组件里，测试只能断言源码里出现过 `PLACEHOLDER_LABEL` 字样 —— 把判断条件改成 `false` 那条断言依然绿。**锁写法不锁行为，等于没锁。**」→ 我们的机械质检若只断言「某函数被调用过」而非「给定输入产出正确判定」，就是同一个病 |

## 明确不推荐（10 条，含理由）

| # | 反对项 | 理由 |
| --- | --- | --- |
| 1 | **storyforge 的摘要式小说链路** | **最严重反面教材，架构级错误**：原文第 1 步就被截断+摘要丢弃，**对白必丢、细节必丢**，走错后面很难补 |
| 2 | **storyforge 的「多角色同框 = 每角色一个视频任务」** | **概念上就是错的**（`video_generation.py:90-129` 遍历 `characters_present`，每角色单独提交只带自己参考图的任务）→ 2 人对话产出 2 段互不相干视频存在同一 `scene_id` 下且无合并逻辑。替代 = wind-comic 多参挂载 + **逐角色独立评分**（取 min） |
| 3 | **storyforge 的非阻塞质检** | `scene_agent.py:242-248` 发现 issues **只写 log 然后原样返回** —— **「装了但没通电」**。要求：每个质检判定必须三选一 —— **阻塞 / 触发重生 / 写进 manifest 且 UI 可见** |
| 4 | **storyforge 宣称向量去重但传全零向量** | `character_agent.py:236-252` 拿到 embedding 却不用，Qdrant 查询传 `[0.0]*384`，返回的 `results` 从未被消费。「文档承诺 > 代码实现」 |
| 5 | **storyforge 的 `MemorySaver()` 检查点** | 内存检查点，**进程重启即全丢**，与「Resumable Workflow」宣称直接矛盾 |
| 6 | **storyforge 的「超时也走完整退避重试」** | 长 context 任务超时后重试代价 = 再等最多 120s + 7s 退避。**我们本地渲染超时应直接失败 + 保留检查点**，重试只留给明确瞬时错误 |
| 7 | **wind-comic 的巨型单文件架构** | orchestrator **254KB**（自陈 5471 行/113 处 any）、composer 105KB、i18n 154KB。它自己的注释就是病历：「113 处 any，其中 6 处在**公开方法签名**上 —— 这是 agent 之间的契约面，一旦是 any，**重构无从谈起**」 |
| 8 | **wind-comic 的自我推销式 README** | 大量 Elo 排名、竞品对比。数字无法验证、时效性极强、**会误导阅读者把宣传当能力**（「8-agent pipeline」实际是同一 LLM 的 8 段 system prompt） |
| 9 | **wind-comic 的双 orchestrator 并存** | 演示版文件名（`agent-orchestrator`）比生产版（`hybrid-orchestrator`）更「正」。**启示：「主路径修好了、旁路没跟上」是最隐蔽的 bug 类别之一**；旧路径要么删、要么显式标 `legacy` 且禁止新调用方进入 |
| 10 | **两个项目都缺成片链路** | storyforge **根本没有** concat/字幕/BGM（`TASK_010 Editing` = 🔲 未完成，`grep concat` 零命中），**它产出不了成片** |

---

# 第六批：qc-asm 结单后的两个待定项（Lead 已裁决）

| # | 项 | 裁决 |
| --- | --- | --- |
| 82 | **冻结阈值 0.5 的边界**（`1-40-01` 0.593 与 `1-4-02` 0.494 被切在两侧） | **维持 0.5，不降到 0.4。** 依据 qc-asm 的逐窗剖面：`1-40-01` 8 等分剖面 `5.41→5.36→2.05→1.32→0.87→0.62→0.68→0.56→0.50` 是**持续减速但仍在运动**，且 `freezedetect=-60dB:d=0.5` 在其上 **0 事件**。降到 0.4 只会多误报。 |
| 83 | **二期是否上"相对判据"** `freeze_pixel_diff / freeze_body_diff` | **二期候选。** qc-asm 的数据显示它能剔除"全片都慢"的 `1-42-01`(0.56) / `1-21-01`(0.78)，保留"只有尾巴塌"的 `1-37-01`(0.22) / `1-39-01`(0.17) / `1-4-02`(0.30)。**本期严格按绝对阈值，不擅自加** —— 先让用户看那 6 个片段，确认哪些是他认可的"真冻结"，再决定要不要上相对判据。 |
