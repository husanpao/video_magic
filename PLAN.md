# 自建漫剧流水线 · 实施计划（待评审）

> 目标：**摒弃 NiliX 的复杂度，保留它验证过的工程机制**，做一个你自己能读、能改、跑得稳的 Python 流水线。
> 输入：小说章节正文 → 输出：成片 MP4
> 状态：计划待你确认；`vm/state.py` 已写出第一版作为地基（可先审这一份代码风格与思路）

---

## 一、目标与非目标

### 要做到
1. **输入小说章节内容，自动分镜**（这是 NiliX 普通模式拒绝做的事，也是你最想要的）
2. 定妆 → 逐镜生成 → 质检 → 合成，**全链一条命令跑完**
3. **断点续跑**：崩溃/断线/手动中断后能接着跑，不重烧 GPU
4. **真幂等**：改了提示词/参考图/模型/步数/分辨率 → 自动重渲；没改 → 跳过
5. 极简 Web UI：进度 + 日志 + 产物预览
6. 单文件可读、可改；纯 Python，无 Go、无 Windows 依赖

### 一期不做（明确砍掉，避免重蹈 NiliX 的复杂）
| 砍掉 | 理由 |
| --- | --- |
| 智能体审片 8 维度 + 修复师返工闭环 | 要视觉模型 Key；一期人工看片更省，二期再议 |
| Krea-2 / Z-Image / SDXL 资产链 | **本机没这些模型**，NiliX 就是卡在这 |
| 云端 2K 升格、剪映草稿导出、预告片 | 锦上添花 |
| 多切点长镜（shots_per_take） | 实为性能优化；一期一镜一片，行为最可预测 |
| 条件缓存（.pt 复用）、知识库注入、剧情块、音色库 | 优化项，不影响正确性 |
| 桌面壳 / 托盘 / 灵动岛 / 手机端 | 无对等物，纯负担 |
| 脚本直出模式（技能侧 JSON 分镜脚本） | 那是 NiliX 的包袱；我们直接 LLM 拆镜 |

---

## 二、目录结构

```
video_magic/
├── pipeline.py              # CLI 入口：python pipeline.py <项目> --stage all
├── vm/
│   ├── state.py             # ★指纹三态幂等 + 渲染检查点（已写）
│   ├── comfy.py             # ComfyUI 客户端（提交/轮询/history 回收/节点探测）
│   ├── shots.py             # 镜头表读写与校验
│   ├── plan.py              # 小说章节 → LLM → 镜头表 + 六段式提示词
│   ├── chars.py             # 角色定妆照（H3 T2VA → 抽帧）
│   ├── gen.py               # 逐镜生成（单并发、注入参考图、断点续跑）
│   ├── qc.py                # 三防线机械质检
│   ├── assemble.py          # 合成（concat + 字幕 + BGM）
│   └── web.py               # 极简 Web UI
└── projects/<项目名>/
    ├── project.json         # 渲染参数（模型/LoRA/步数/分辨率/帧率）
    ├── novel/               # 小说章节（输入）
    ├── prompts/char_*.txt   # 角色定妆提示词（可手写，也可 plan 生成）
    ├── refs/char_*.png      # 角色参考图（定妆照产物 = R2V 输入）
    ├── shots/*.json         # 镜头表（plan 产出，可手改）
    ├── clips/<id>.mp4       # 逐镜产物
    ├── final/EP01.mp4       # 成片
    └── state/
        ├── manifest.json    # 指纹三态清单
        ├── render_ck.json   # 渲染检查点
        └── run.log          # 阶段日志（Web UI 读它）
```

---

## 三、数据契约（关键，先定死再写代码）

### 3.1 镜头表 `shots/*.json`
**向后兼容你现有的 schema**（`drama-project/shots/*.json` 可直接用），新增字段全部可选：

```json
[
  {
    "id": "1-3-01",              // 必填，场次-镜号；决定产物文件名 clips/1-3-01.mp4
    "sec": 6,                    // 必填，目标秒数；内部换算到 17n+5 帧网格
    "chars": ["孙悟空", "猪八戒"], // 必填，驱动参考图注入
    "seed": 3100,                // 必填，固定种子保证可复现
    "prompt": "...六段式...",     // 必填，H3 提示词
    "shot_size": "中景",          // 可选，仅记录/展示
    "camera": "缓慢推近",         // 可选，仅记录/展示
    "dialogue": "有庙就有斋饭！",  // 可选，字幕用；plan 阶段生成
    "narration": "..."            // 可选，画外音
  }
]
```

**校验规则**（`shots.py` 负责，报错要具体到镜头号）：`id` 唯一、`sec` ∈ [3,15]、
`chars` 非空、`seed` 整数、`prompt` 非空、`chars` 里的角色在 `refs/` 都有对应图。

### 3.2 指纹 `state/manifest.json`
```json
{"version": 1, "shots": {
  "1-3-01": {"fp":"a1b2c3…","file":"clips/1-3-01.mp4","size":1234567,"at":1758600000,"qc":"pass","note":""}
}}
```
指纹输入（**任何一项变化即 stale 重渲**）：
`提示词 | chars(排序) | 参考图(名@mtime纳秒:size，排序) | unet | lora | steps | 宽 | 高 | 帧数 | seed`

三态：`missing`（无文件）/ `stale`（无记录或指纹不符或 size 不符）/ `current`（可跳过）。

### 3.3 检查点 `state/render_ck.json`
```json
{"shots": {"1-3-01": "c2b33af5-b056-4163-93ed-0eeb6a73af0d"}}
```
提交 `/prompt` 拿到 id **立即落盘**；收到产物即清除。

### 3.4 项目参数 `project.json`
```json
{
  "comfy_url": "http://127.0.0.1:8188",
  "width": 864, "height": 480, "fps": 24,
  "unet": "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
  "lora": "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors",
  "steps": 8,
  "llm": {"base_url": "https://api.deepseek.com", "model": "deepseek-chat"}
}
```
> API Key 不写进项目文件，走环境变量 `DEEPSEEK_API_KEY`（避免再出现"Key 落在临时目录"的事）

---

## 四、流水线阶段

| 阶段 | 命令 | 输入 → 输出 | 幂等判据 |
| --- | --- | --- | --- |
| `plan` | `pipeline.py 西游记 --stage plan` | `novel/*.md` → `shots/*.json` + `prompts/char_*.txt` | 章节内容指纹；变了才重跑 |
| `chars` | `--stage chars` | `prompts/char_*.txt` → `refs/char_*.png` | 提示词指纹 + 文件存在 |
| `render` | `--stage render` | `shots/*.json` + `refs/` → `clips/*.mp4` | **manifest 三态** |
| `qc` | `--stage qc` | `clips/*.mp4` → `state/qc.json` + 重渲队列 | 无（每次重查，成本低） |
| `assemble` | `--stage assemble` | `clips/*.mp4` + 字幕 → `final/EP01.mp4` | 输入片段指纹 |
| `all` | `--stage all` | 全链顺序执行 | 各阶段自带 |

**`render` 的断点续跑逻辑**（核心）：
```
1. 若 manifest 判 current → 跳过
2. 否则查 render_ck：
     有 prompt_id → 查 /history/<id>
         completed → 直接收回产物（免重渲）
         在跑/排队 → 等它结束再收回
         查不到且超过判定窗 → 判丢失，清检查点
3. 提交新任务 → 落盘 prompt_id → 轮询 → 收产物 → 写 manifest → 清检查点
4. 单并发（显存已被单任务吃满：23.6/24GB），但"渲当前镜时后台预编码下一镜"二期再说
```

---

## 五、继承自 NiliX 的机制（每条都有验证依据）

| # | 机制 | 解决什么 | 依据 |
| --- | --- | --- | --- |
| 1 | manifest 指纹三态 | 改输入不重渲 / 存在即跳过的静默错误 | NiliX `manju_manifest.go`；你现有 `gen-shots.py` 的缺口 |
| 2 | 渲染检查点 + history 回收 | 崩溃/断线不重烧 GPU | NiliX `manju_render_ck.go`；你 118 镜断线两次的痛点 |
| 3 | QC 三防线：近黑 + **标准差**判据 | 夜戏被误杀 | NiliX 审计 S4 教训 |
| 4 | **段尾冻结检测** | H3 段尾提前静止（典型病） | NiliX `manju_media.py`；你 `qc.py` 没有 |
| 5 | 六段式 Ref2VA + `<Subject N>`/`<Picture N>` | 角色一致性 | 官方指南 + NiliX 实践 |
| 6 | 纪律前移（CAMERA/POSITION 放 `detailed_description:` 之前） | 提高指令服从 | NiliX 画质升级实测 |
| 7 | **文字必须与参考图一致** | 文字会压过参考图 | 我本机 A/B 实测（8 臂） |
| 8 | 质检区分"通过"与"未执行" | 静默失败比没质检更危险 | 你 PIPELINE-DESIGN §5.2 + NiliX 审计 |

---

## 六、里程碑与验收标准

| 里程碑 | 内容 | 验收（我能跑给你看） |
| --- | --- | --- |
| **M1** | 骨架 + 状态层（`state.py`、`shots.py`） | 用假产物单测三态判定：改提示词→stale、只碰 mtime→stale、不变→current；检查点原子写 |
| **M2** | `comfy.py` + `gen.py` | 单镜真渲一遍；**中途 kill -9 再续跑**，证明不重复提交、能收回已完成任务 |
| **M3** | `qc.py` | 三个合成用例（全黑片/静音片/段尾冻结片）都能被检出；正常片不误报 |
| **M4** | `assemble.py` | 用你 `drama-project/refs` + 现有片段拼一集成片，含中文字幕 |
| **M5** | `plan.py` | **西游记第一章 → 镜头表**（含六段式提示词 + 角色卡），你审阅分镜质量 |
| **M6** | `chars.py` + `web.py` | 定妆照批量生成；Web UI 能看进度/日志/产物 |
| **M7** | 端到端 | 西游记第一章 → 成片 MP4，全程一条命令 |

**每完成一个里程碑我停下来给你验，不闷头往下做。**

---

## 七、已知风险

| 风险 | 影响 | 对策 |
| --- | --- | --- |
| LLM 拆镜质量不稳定 | 分镜不合理、台词与镜头不匹配 | 拆镜提示词单独可迭代；输出过 JSON schema 校验；不合格重试；**镜头表可手改后重跑** |
| 完整模型 34GB 冷启动 staging >60s | 提交超时（我已踩过） | 用剪枝版 21GB；并把提交超时从 60s 放宽到 300s |
| 中文字幕字体 | 烧录乱码/方块 | 指定 `Noto Sans CJK` / `微软雅黑` 的 Linux 替代；先用 check 脚本验证字体存在 |
| 864×480 是否够用 | 观感 | 已验证安全；竖屏需另做一轮显存实测（你说过） |
| ComfyUI 被搞挂 | 你最在意的事 | 只读+提交，**不启停 ComfyUI**；不移动 output/ 里的任何文件；提交前健康检查 |

---

## 八、需要你拍板的点

1. **成片规格**：沿用 864×480 / 24fps？还是要竖屏 768×1344（需先实测显存）？
2. **字幕**：烧录进画面，还是只导出 `.srt`（后期可编辑）？
3. **BGM**：要吗？有现成音乐文件吗（我可以做"对白时段自动闪避"）？
4. **LLM 拆镜**：用 DeepSeek 的哪个模型（`deepseek-chat` 还是 `deepseek-reasoner`）？拆镜要不要"审稿不合格自动重写 1 轮"？
5. **Web UI 程度**：只要"看"（进度条+日志+产物缩略图），还是要能"点"（启动/停止/单镜重渲）？
6. **一期先跑哪个**：西游记第一章（需 4 个角色定妆，已有）还是 drama-project（已有全部素材，回归最快）？

---

## 附：已写的代码

`vm/state.py` —— 指纹三态 + 渲染检查点 + 原子写 + 项目目录约定。
你可以先审这一份：**风格、注释密度、接口是否合你意**。合意我就按此风格推进 M1，不合意现在就改。
