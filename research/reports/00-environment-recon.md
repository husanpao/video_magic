# 本机环境勘察报告（运行 NiliX / 自建流水线的现实约束）

> 勘察时间：2026-09-23 · 目标机：RTX 5090 D v2 24GB / 94GB RAM / Ubuntu 22.04
> 目的：判断「在 Linux 上做一个 NiliX 式的漫剧流水线」当前缺什么

---

## 一、硬件与运行时

| 项 | 实测值 |
| --- | --- |
| GPU | NVIDIA GeForce RTX 5090 D v2，24455 MiB（实测约 23.88 GiB） |
| 驱动 | 580.95.05 |
| 磁盘 | `/` 915G，已用 593G，**可用 277G** |
| ComfyUI | **正在运行**，端口 **8188**，根目录 `/home/max/ComfyUI` |
| ComfyUI 启动 | `/home/max/comfyui/venv/bin/python main.py --listen 0.0.0.0 --port 8188` |

**注意**：存在两个易混淆目录，职责不同：

- `/home/max/ComfyUI` —— **ComfyUI 本体 + 模型**（`models/`、`custom_nodes/`、`output/`），进程 cwd 在此
- `/home/max/comfyui` —— **工作目录**：`venv/`、`drama-project/`、`h3-project/`、`h3-*.py` 脚本、日志

> PIPELINE-DESIGN.md §6.2 写 `cd ~/comfyui` 后用相对路径调 `drama-project/*.py`，与模型实际所在目录无关，是对的但容易误读。

---

## 二、模型资产盘点（决定能跑哪些阶段）

### ✅ 已就绪

| 用途 | 文件 | 大小 |
| --- | --- | --- |
| H3 完整版 UNet（FL2VA） | `models/diffusion_models/minimax_h3_fl2va_int8_convrot.safetensors` | 34.0 GB |
| H3 剪枝版 UNet（FL2VA） | `models/diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors` | 21.0 GB |
| Turbo LoRA 4 步 | `models/loras/minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors` | 1.96 GB |
| Turbo LoRA 8 步 | `models/loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors` | 1.96 GB |
| 文本编码器 | `models/text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` | 15.7 GB |
| 视频 VAE | `models/vae/minimax_h3_video_vae_fp16.safetensors` | 5.2 GB |
| 音频 VAE | `models/vae/minimax_h3_audio_vae_fp32.safetensors` | 0.6 GB |
| 自定义节点 | `comfyui-minimax-h3-audio-T8`、`ComfyUI-VideoHelperSuite` | — |

### ❌ 缺失（阻塞对应阶段）

| 缺失项 | 阻塞的阶段 | 说明 |
| --- | --- | --- |
| **Ref2VA UNet**（`MiniMax_H3_ref2va_*`） | 角色镜渲染 | NiliX 默认 `unet_ref2va`；Ref2VA 是角色一致性锁定的核心 |
| **Ref2VA Turbo LoRA** | 角色镜加速 | NiliX 默认 `turbo_lora_r2v` |
| **Z-Image 三件套**（`z_image_turbo_bf16` / `qwen_3_4b` / `ae.safetensors`） | 定妆照、场景资产 | `models/` 全盘搜索无结果，`checkpoints/` 为空 |
| **SDXL**（`animagine-xl-3.1` / `sd_xl_base_1.0`） | 非 real 风格定妆照 | 同上 |
| **ComfyUI-MiniMax-H3-PDD-Acc 节点** | NiliX 当前「质量档」默认 | NiliX 未装时自动回退 20 步并告警 |
| **SageAttention** | 加速（可选） | NiliX 有双名探测自动降级，不阻塞 |

**估算**：补齐 Ref2VA UNet + LoRA + Z-Image 三件套约需 **25–30 GB** 下载（ModelScope 5MB/s 计约 1.5–2 小时），磁盘 277GB 足够。

### ⚠️ 命名不一致（会导致 NiliX 默认配置直接找不到模型）

NiliX `settings.json` 默认值 vs 本机实际文件名：

| NiliX 默认 | 本机实际 |
| --- | --- |
| `MiniMax_H3_fl2va_pruned_int8_convrot.safetensors` | `minimax_h3_fl2va_pruned_int8_convrot.safetensors`（大小写不同） |
| `minimax_h3_fl2va_pdd_acc_8step_comfyui.safetensors` | `minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors` |
| `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` | ✅ 一致 |
| `minimax_h3_video_vae_fp16.safetensors` | ✅ 一致 |

结论：**接上 NiliX 必须先过一遍「模型文件映射」**，这正是 NiliX README 里提到的「LoRA 文件名不匹配 → 建硬链接别名，不改工作流」那一类问题。

---

## 三、实测基准验证（原始日志 vs PIPELINE-DESIGN.md §2）

原始数据来自 `/home/max/comfyui/h3-bench.log`、`h3-10s.log`、`h3-15s.log`、`h3-640p.log`、`h3-test.log`。

| 配置 | 帧数 | 实测耗时 | 设计文档声称 | 结论 |
| --- | --- | --- | --- | --- |
| 864×480，**8 步**，剪枝 | 56（2.3s） | 30.0 s | 30 秒 | ✅ 吻合 |
| 864×480，8 步，剪枝 | 124（5.2s） | 50.1 s | 50 秒 | ✅ 吻合 |
| 864×480，8 步，**完整版** | 124（5.2s） | **50.1 s** | 50 秒 | ✅ 吻合（与剪枝版**完全同速**） |
| 864×480，8 步，剪枝 | 243（10.1s） | 110.2 s | 110 秒 | ✅ 吻合 |
| 864×480，8 步，剪枝 | 362（15.1s） | 170.3 s | 170 秒 | ✅ 吻合 |
| 1152×640，8 步，剪枝 | 124（5.2s） | 80.1 s | 80 秒 | ✅ 吻合 |

设计文档 §2.1/§2.2 的数字**全部可溯源且准确**，可信度高。

### 但补充了三条文档没说的关键信息

1. **4 步 LoRA 实测 30.0 s / 124 帧**，比 8 步的 50.1 s 快 **1.67×**。
   设计文档 §2.1 的「≈11 秒/视频秒」是**8 步**口径；4 步口径约 **6 秒/视频秒**。
   容量规划若不分步数口径，会高估一倍工时。
2. **剪枝版（21GB）与完整版（34GB）耗时完全相同（50.1s）**——而 24GB 显存装不下 34GB 权重，
   说明必然存在 offload/分块换入。**耗时瓶颈很可能不在算力，而在权重换入带宽**，
   这也解释了为何两版同速。设计文档「显存峰值 23.6GB / 对分辨率不敏感」的表述与此一致，
   但推论应为：**VRAM 的真实杠杆是 offload 配置，不是分辨率**。
3. **设计文档未提 4 步/8 步 LoRA 的取舍**。而 NiliX 的结论恰恰相反（见 §四）。

---

## 四、与 NiliX 默认策略的冲突点

NiliX `docs/upgrade-20260904-quality.md`（最新画质迭代）把**官方最佳实践**定为：

- VAE 用 **fp16**（非 int8）—— 本机已有 ✅
- Turbo LoRA 用 **PDD Acc 8step** —— 本机**没有**该 LoRA，也没有 PDD-Acc 节点 ❌
- `ref_image_size = max` —— 与版本无关，配置项 ✅
- `detailed_description` **250–350 词**（原 150–220）、`h3_prompt` 上限 2200 tokens
- 运镜用官方 15 词动词句式（`pushes in with small amplitude at slow speed`），
  禁用 `dolly/orbital/crane` 等黑话

即：**本机当前是「提速档」，NiliX 默认是「质量档」，两者耗时差约 2 倍**。
本地若走 NiliX 质量档，5 秒镜从 50s 变约 100s，全剧工时翻倍——这是接入前必须做的决策。

---

## 五、可复用的既有资产

| 资产 | 位置 | 价值 |
| --- | --- | --- |
| **完整成品项目** | `~/comfyui/drama-project/` | 15 个场景镜头表 JSON、7 张角色参考图、5 集成片 + 全剧合集 |
| 已有剧本项目 | `~/comfyui/h3-project/` | `分镜脚本.md` + 8 个片段提示词 + 2 张参考图 |
| 下载工具 | `~/comfyui/h3-download.py` | ModelScope 并发下载 + 断点续传 + SHA256；**但清单只有 FL2VA，需扩展 Ref2VA** |
| 工作流适配 | `~/comfyui/h3-patch-workflows.py` | 节点替换/LoRA 别名 |
| 单镜生成 | `~/comfyui/h3-test-run.py` | 已验证支持 `--ref` 参考图 R2V（用 FL2VA 模型跑通，50.1s） |

**重要发现**：`h3-r2vtest.log` 显示 **R2V 参考图流程是用 FL2VA 模型跑通的**
（`minimax_h3_fl2va_int8_convrot` + 两张 `--ref`，50.1 秒出片）。
说明本机已验证的 R2V 路径**不依赖独立的 Ref2VA 权重**——
这降低了「必须下载 Ref2VA」的紧迫性，但需确认该路径的身份锁定强度是否等同于官方 Ref2VA 节点。

**结论**：`drama-project/` 是现成的**端到端回归测试语料**（5 集、15 场、118+ 镜头），
新建流水线应直接拿它做验收基线，而不是另造测试数据。

---

## 六、一句话总结

本机 GPU、ComfyUI、H3 FL2VA 全链路、编码器、VAE、T8 节点**均已就绪且实测可跑**；
缺的是 **Ref2VA 权重 + Z-Image/SDXL 资产模型 + PDD-Acc 节点**（约 25–30GB 下载），
以及一个**模型文件名映射层**。既有 `drama-project` 可直接当回归语料。
