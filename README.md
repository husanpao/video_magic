# video_magic · 小说 → 漫剧流水线

把小说章节喂进去，吐出一部带中文字幕的漫剧成片。

```
小说章节.md
   │
   ├─ ① 拆镜      DeepSeek 把章节拆成镜头表（谁 / 在哪 / 做什么 / 说什么 / 几秒）
   ├─ ② 定妆      每个角色出一张参考图（Qwen-Image，约 5 秒/张）
   ├─ ③ 渲染      MiniMax H3 (Ref2VA) 逐镜出视频（约 42 秒/镜，带原生配音）
   ├─ ④ 质检      机械判定：尾帧冻结 / 时长守恒 / 字幕同步
   └─ ⑤ 合成      concat + 烧中文字幕 → final/EP01.mp4
```

**这不是一个"一键成片"的黑盒。** 它把每一步的产物都摊开给你看、让你改：
拆出来的镜头表能逐行编辑、角色定妆能抽卡挑、场景道具能自己传图、
每个阶段的提示词都能在界面上直接改。

---

## 它解决什么问题

市面上的开源方案大多在**"摘要式链路"**上翻车：把小说压缩成梗概再生成，
原文第一步就丢了 —— 台词被改写、情节被合并、角色前后不一致。

本项目的纪律是**不压缩**：

| 纪律 | 落地方式 |
|---|---|
| **台词一句都不能丢** | 拆镜规范要求正文每句对白都进某个镜头；`vm/script.py` 逐句回原文核对 |
| **同一地点必须是同一个景** | 抽「场景实体」（`scenes.json`），同 `scene_id` 的镜头逐字注入同一段描述 |
| **同一角色跨镜要像同一个人** | 定妆参考图 + `<Picture N>` 注入；角色卡负责外貌，拆镜 LLM 禁止重写外貌 |
| **不让模型猜** | 生成提示词时逐项交代姿态/构图/服装/配饰/光位/色调/景深，见 `DETAIL_CHECKLIST` |
| **装了必须通电** | 每个判定都要有人消费 —— 质检结论进合成决策，预算护栏通向人，不做只写日志的告警 |

---

## 环境要求

| 依赖 | 版本 | 说明 |
|---|---|---|
| **ComfyUI** | 0.3.x | 需常驻运行，默认 `127.0.0.1:8188`。本项目**不会启停它** |
| Python | 3.10+ | 纯标准库，无第三方依赖 |
| Node.js | 18+ | 只用于构建前端 |
| ffmpeg / ffprobe | 任意较新版 | 抽帧、合成、字幕 |

### 需要的模型（放进 ComfyUI 的 `models/`）

```
models/diffusion_models/  MiniMax-H3 相关权重
models/diffusion_models/  qwen_image_nvfp4.safetensors       # 定妆 / 场景 / 道具 / 分镜图
models/text_encoders/     qwen_2.5_vl_7b_nvfp4.safetensors
models/vae/               qwen_image_vae.safetensors
```

### DeepSeek API key

解析顺序：

1. 环境变量 `DEEPSEEK_API_KEY`
2. 文件 `~/.config/video_magic/deepseek_key`（建议 `chmod 600`）

---

## 快速开始

```bash
git clone <repo> && cd video_magic

# 前端（首次、或改了 web/ 之后）
cd web && npm install && npm run build:only && cd ..

# 建一个项目：projects/<项目名>/novel/*.md 放小说章节，project.json 放配置
mkdir -p projects/我的剧/novel
cp 第一章.md projects/我的剧/novel/

# 开控制台
python3 pipeline.py 我的剧 --serve --port 8801
# → http://127.0.0.1:8801/
```

界面上按 **全链** 就跑完了。也可以分阶段跑：

```bash
python3 pipeline.py 我的剧 --stage plan      # 只拆镜
python3 pipeline.py 我的剧 --stage render --only 1-1-01,1-2-01 --force
python3 pipeline.py 我的剧 --stage assemble  # 逐章出一集
```

> **单项目同时只允许一个任务**（有项目锁）。抽卡类操作走**队列**：
> 点一下入队立即返回，可以连点十几个然后走开（`vm/queue.py`）。

### 命令行工具

```bash
python3 -m vm.script     我的剧        # 剧本视图（场次→节拍→人物/对白/舞台指示）+ 无损校验
python3 -m vm.assets     我的剧 --list # 场景/道具概念图候选
python3 -m vm.assets     我的剧 --kind scene --id S1 --n 2      # 抽卡出图
python3 -m vm.assets     我的剧 --kind prop --id P1 --upload a.png  # 传自己的图
python3 -m vm.budget     我的剧 --stage render                  # 成本估算
python3 -m vm.storyboard 我的剧 --list                          # 分镜图状态
python3 -m vm.audit.cli  我的剧                                 # 节奏/台词覆盖审计
```

---

## 界面

| 区域 | 内容 |
|---|---|
| **顶栏** | 项目切换 · 阶段按钮 · 真实进度 · 停止 · 清空重置 |
| **左栏** | 按场景实体分组的镜头导航 |
| **中栏** | 镜头表（可逐行编辑）+ 胶片条 + 分段进度条（既是进度、又是时长分布、又是导航） |
| **右栏** | 日志 / 角色定妆 / 剧本 / 场景 / 道具 / 分镜图 / 质检 / 审计 / 成片 |
| **右下 FAB** | 作业队列（待处理/进行中/失败，单项可取消） |

**页面是活的**：运行中镜头表、当前 tab、队列都在自动刷新，任务结束再补一次全量。

---

## 成本基线（本机实测，非估算）

| 项目 | 实测值 |
|---|---|
| H3 视频（864×480） | **41.8 秒/镜** |
| Qwen-Image 出图 | **10.0 秒/张**（定妆照 5s 上下） |
| DeepSeek 拆镜 | ~1700 tokens/次，一章通常 1-3 次 |
| 52 镜的整片渲染 | **≈36 分钟 GPU** |

`vm/budget.py` 按这些基线做**渲染前拦截**：超预算时任务**暂停等人批准**，
而不是跑完才发现花超了。

---

## 项目结构

```
pipeline.py          阶段入口（STAGES 的 DISPATCH）
vm/
  plan.py            ① 拆镜：章节 → 镜头表 + 角色卡 + 场景 + 道具 + 风格句
  chars.py           ② 定妆：角色参考图 + 抽卡
  gen.py             ③ 渲染：逐镜调 H3
  qc.py              ④ 质检：尾帧冻结 / 时长守恒 / 字幕同步
  assemble.py        ⑤ 合成：concat + 中文字幕
  script.py          剧本视图（由镜头表推导，零 token）+ 台词无损校验
  storyboard.py      分镜图（Qwen-Image）
  qi.py              Qwen-Image 客户端
  style.py           画面风格预设（realistic / cg / anime）
  budget.py          成本护栏 + 台账
  queue.py           作业队列
  wsclient.py        极简 WebSocket 客户端（读 ComfyUI 步级进度）
  state.py           项目状态：指纹、manifest、锁定
  shots.py           镜头表读写
  taskctl.py         任务模型：锁、日志、进度、停止
  web.py             HTTP API + 前端托管
  audit/             审计层：节奏 / 台词覆盖 / 台词保真 / 完备性矩阵
web/                 Vue 3 + Vite + Element Plus 控制台
docs/prompts/        发给 LLM 的全部提示词（导出视图）
projects/            你的片子（**不提交**）
```

### 关键机制

**镜头指纹**：`md5(prompt, chars, refs, unet, lora, steps, w, h, frames, seed)` → 三态
`missing / stale / current`。改了任何影响画面的输入，只有相关镜头变「需重渲」，
不是整片重来。

**渲染检查点**：提交前先把 `prompt_id` 落盘，崩溃后用 `/history` 回收，
不会重复烧 GPU（`RECLAIM_GRACE_SEC=90`）。

**帧数网格**：H3 要求 `frames = 17k + 5`，本机上限 362 帧（15.08s）。

---

## 设计取舍（有意为之）

- **不启停 ComfyUI**。它归你自己管，本项目只通过 HTTP API 说话，**不碰 `/free`、不碰 output 目录**。
- **不升级到 Qwen-Image 2.1**。本机 ComfyUI 0.36.0 的权重布局识别对不上 2.1（详见 `vm/qi.py`
  的风险备忘），且 2.1 仍是文生图，拿不到参考图预演。
- **场景/道具的图不注入渲染**。H3 的 `<Picture N>` 槽位语义是「主体」，塞场景图会抢角色槽位。
  场景一致性走**文字锚定**，图只用于渲染前确认。
- **同一项目单任务**。队列解决「排队」，任务锁解决「互斥」，是两件事。

## 已知局限

- 画面风格**由参考图决定**，文字风格句控不住画风 —— 想要二次元/建模风，必须让定妆照本身是那个风格。
- 分镜图与成片构图**不保证一致**（不同模型）。
- 长片能力受帧数网格限制（单镜上限 20.75 秒）。
- 连续性（跨镜动作衔接）**未做**，实测观感可接受。

---

## 测试

```bash
cd web && node ui-e2e.mjs          # 真浏览器 E2E（推荐日常用这个）
FAST=1 node ui-e2e.mjs             # 冒烟：只跑加载 + 关键元素 + 零错误

python3 -m unittest discover -s vm/audit/tests -q   # 审计层单测（66 项，纯标准库）
```

> E2E 用**真浏览器**（Playwright + Chromium），不是 DOM 桩 ——
> `vue-tsc` 和 `vite build` 全绿但界面不显示的情况，本项目遇到过多次。

---

## 许可

内部项目，未附许可。
