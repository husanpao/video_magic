# 模块接口契约（并行开发基准 · 冻结版 v1）

> 冻结日期：2026-09-23 · 依据 `PLAN.md` 的已确认决策
> **改这个文件 = 改接口，必须先跟 Lead 说**，否则并行开发的模块会接不上。
> **修订 R1（2026-09-23，Lead 批准）**：`check_clip` 追加可选 `target_sec`（契约 v1 漏了目标秒数）；
> 补记 `state/qc.json` 形状与 `ok` / `verdict` 语义。改动仅在 `vm/qc.py` 一节。
> **修订 R4（2026-09-23，Lead 指派）**：段尾冻结判据由「灰度均值差占比」改为「逐像素绝对差均值 < 0.5」
> （真实 52 镜旧判据 100% 误报）；`vm/assemble.py` 一侧补记 A2（时间轴用 `sec_actual`）与 C5（起始裁剪）行为。
> R2 / R3（控制台抽卡与镜头编辑）见文件末尾。

## 已确认的决策

| 项 | 决定 |
| --- | --- |
| 分辨率/帧率 | **864×480 / 24fps**（本机唯一实测跑通） |
| 帧数网格 | `17n+5`，范围 [56, 362] |
| 字幕 | **烧录进画面 + 同时导出 `.srt`** |
| BGM | 一期不做 |
| 拆镜 LLM | `deepseek-chat`，JSON 校验不合格**重写 1 轮** |
| Web UI | 能看 + **能点**（启动/停止/单镜重渲） |
| 验收语料 | 西游记第一章（4 角色定妆照已生成） |
| 模型 | unet=剪枝版 21GB，lora=fl2v turbo 8step，steps=8 |
| ComfyUI | `http://127.0.0.1:8188`，**只读+提交，绝不启停** |
| API Key | 解析顺序：环境变量 `DEEPSEEK_API_KEY` → 文件 `~/.config/video_magic/deepseek_key`（600 权限，用户级，**不在项目内**）。两者都没有就显式报错退出 |

## 硬约束（违反即打回）

1. **绝不启停 ComfyUI**，也绝不调用 `/free`、`/queue clear`、`/interrupt`（除用户显式点"停止"）
2. **绝不移动/删除 `/home/max/ComfyUI/output/` 下的任何文件**（会导致 ComfyUI 历史面板"看不到图"）
3. 所有落盘用**原子写**（`.tmp` + `os.replace`）
4. 质量检查必须区分「通过」与「未执行」——采样数为 0 要显式报错，不能静默判过
5. 不引入第三方依赖（只用标准库 + `ffmpeg`/`ffprobe` 命令行 + `requests` 可接受但优先 `urllib`）
6. **★ 任何"按 pid 杀进程"的写法，必须先锁定目标再取 pid。**
   反面教材（2026-09-23 真实事故，shot-edit 误杀 ComfyUI）：
   ```bash
   # ❌ head -1 取到的是"任意一个"监听 socket 的 pid，与端口无关 → 杀错进程
   ss -ltnp | grep -o 'pid=[0-9]*' | head -1
   # ✅ 先按端口过滤
   ss -ltnp 'sport = :8812' | grep -o 'pid=[0-9]*' | head -1
   lsof -ti :8812
   ```
   `head -1` 绝不可以用在未过滤的进程列表上。
7. **禁用 `pgrep -f` / `pkill -f` 匹配自己的命令行**（本会话已因自匹配自杀 3 次）。
   要按模式找进程就用 `/proc/<pid>/cmdline` 逐个核验，并排除自身 pid/ppid。
8. **误停用户关键服务（ComfyUI 等）时**：立刻上报 Lead，由 Lead 决定恢复；
   不要因为"硬约束写着不许启停"就放任服务挂着不恢复。

---

## 目录与文件

```
video_magic/
├── pipeline.py            # CLI 入口（webui 负责）
├── vm/
│   ├── state.py           # ✅ Lead 已写：指纹三态 + 检查点 + Project
│   ├── shots.py           # planner 负责
│   ├── plan.py            # planner 负责
│   ├── comfy.py           # Lead 负责
│   ├── gen.py             # Lead 负责
│   ├── chars.py           # Lead 负责
│   ├── qc.py              # qc-asm 负责
│   ├── assemble.py        # qc-asm 负责
│   └── web.py             # webui 负责
└── projects/<名>/         # 见 PLAN.md 第二节
```

---

## `vm/state.py`（已实现，勿改接口）

```python
MISSING, STALE, CURRENT = "missing", "stale", "current"

def ref_stamp(path: Path) -> str
def shot_fingerprint(prompt: str, chars: Iterable[str], ref_paths: Iterable[Path],
                     render: dict, frames: int, seed: int) -> str

@dataclass
class Manifest:
    @classmethod
    def load(cls, path: Path) -> "Manifest"
    def save(self) -> None
    def status(self, shot_id: str, fp: str, clip_path: Path) -> str   # 三态
    def mark(self, shot_id: str, fp: str, clip_path: Path, qc="unknown", note="") -> None
    def drop(self, shot_id: str) -> None

@dataclass
class Checkpoint:
    @classmethod
    def load(cls, path: Path) -> "Checkpoint"
    def save(self) -> None
    def set(self, shot_id: str, prompt_id: str) -> None   # 自动 save
    def get(self, shot_id: str) -> str | None
    def clear(self, shot_id: str) -> None                 # 自动 save

class Project:
    def __init__(self, root: Path)
    # 属性：root/novel_dir/shots_dir/refs_dir/prompts_dir/clips_dir/final_dir/state_dir
    def ensure(self) -> "Project"
    @property
    def manifest(self) -> Manifest
    @property
    def checkpoint(self) -> Checkpoint
    def clip(self, shot_id: str) -> Path
```

---

## `vm/shots.py`（planner）

```python
FPS = 24

@dataclass
class Shot:
    id: str
    sec: int
    chars: list[str]
    seed: int
    prompt: str
    shot_size: str = ""
    camera: str = ""
    dialogue: str = ""
    narration: str = ""

def seconds_to_frames(sec: float, fps: int = FPS) -> int:
    """对齐 17n+5 网格，clamp 到 [56, 362]（≈2.3s~15s）"""

def load_shots(path: Path) -> list[Shot]
def save_shots(path: Path, shots: list[Shot]) -> None          # 原子写，UTF-8，不转义中文
def load_shots_dir(d: Path) -> list[Shot]                       # 按文件名排序，合并全部 *.json
def validate_shots(shots: list[Shot], refs_dir: Path) -> list[str]:
    """返回问题清单（人类可读，含镜头号）。空列表=通过。
    检查：id 唯一 / sec∈[3,15] / chars 非空 / chars 在 refs_dir 有 char_<名>.png /
          prompt 非空 / seed 是 int / prompt 含六段式字段（Ref2VA）或单段式（FL2VA）"""
```

**镜头表 JSON 格式**（向后兼容用户现有 `drama-project/shots/*.json`）：
```json
[{"id":"1-1-01","sec":6,"chars":["孙悟空","唐僧"],"seed":3100,
  "prompt":"subject_definitions: ...","shot_size":"中景","camera":"缓慢推近",
  "dialogue":"...","narration":"..."}]
```
缺省字段要能容忍（旧表只有前 5 个键）。

### ★ 参考图顺序契约（plan.py ↔ gen.py 的强耦合，双方都必须遵守）
**`chars[0]` ↔ `ref_image_0` ↔ `<Picture 1>` / `<Subject 1>`，严格按 `chars` 列表顺序，一一对应。**

- `plan.py`：写提示词时，`<Subject N>`/`<Picture N>` 的 N 按 `shot.chars` 下标 +1 编号
- `gen.py`：挂载 `ref_images.ref_image_N` 时同样按 `chars` 顺序（已实现）
- **任何一方静默跳过缺失项，都会让索引整体前移 → 角色与参考图错位**。
  而画面仍能正常生成（只是人不对），属最难排查的一类 bug。
  因此 `gen.py` 对缺失参考图**直接报错**，不做降级（已实现）

### 纪律前移（plan.py）
`CAMERA DISCIPLINE:` / `POSITION DISCIPLINE:` 两行放在 `detailed_description:` 段标题**紧前面**
（即 `retention_analysis` 段尾）服从度最高。**它们不是新段落**，六段式结构仍是 6 段；
`shots.py` 的结构校验需把这些纪律前缀列入白名单，不参与段名计数。

---

## `vm/plan.py`（planner）

```python
@dataclass
class CharCard:
    name: str          # 中文名，如 "孙悟空"
    appearance: str    # 外貌描述（照参考图写）
    costume: str       # 服装
    portrait_prompt: str  # 定妆照用的 H3 单段式提示词（正面近景）

def plan_chapter(proj: Project, chapter_path: Path, cfg: dict, log) -> tuple[list[Shot], list[CharCard], dict]:
    """小说章节 → 镜头表 + 角色卡。写盘到 proj.shots_dir / proj.prompts_dir。
    返回 (shots, chars, stats)。stats 含 token 用量/重试次数，供 UI 展示。
    要求：
      - 用 DEEPSEEK_API_KEY 环境变量（缺失就报错退出，不要静默）
      - 输出必须过 JSON 校验；不合格按其意见重写 1 轮，仍不合格则报错
      - 六段式提示词必须含并只含这 6 段且按序：
        subject_definitions / summary / retention_analysis /
        detailed_description / overall_soundscape / non_diegetic_music
      - **文字外貌必须与参考图一致**（本地 A/B 实测：文字会压过参考图）"""

def write_char_prompts(proj: Project, chars: list[CharCard]) -> dict[str, Path]:
    """写 proj.prompts_dir/char_<名>.txt，返回 {名: 路径}"""
```

---

## `vm/comfy.py`（Lead）

```python
class ComfyError(Exception): ...

class Comfy:
    def __init__(self, base: str = "http://127.0.0.1:8188", submit_timeout: int = 300)
    def healthy(self) -> bool                       # GET /system_stats
    def submit(self, workflow: dict) -> str         # POST /prompt → prompt_id；**提交超时 300s**（大模型 staging 慢）
    def history(self, prompt_id: str) -> dict | None  # None = 查不到
    def wait(self, prompt_id: str, timeout: int = 1800, on_tick=None) -> dict
        """阻塞直到 completed；出错抛 ComfyError；on_tick(elapsed, running, pending) 供 UI 回调"""
    def outputs_of(self, hist_entry: dict) -> list[dict]   # [{"filename","subfolder","type"}]
    def download(self, item: dict, dst: Path) -> Path      # 经 /view 取回并原子落盘
    def has_node(self, name: str) -> bool
        """★ 修正版：要求 200 且 body 是非空 JSON 对象。
        （ComfyUI 对不存在的节点也返回 200 + {}；只判状态码会恒真，NiliX 就栽在这）"""
    def object_list(self, kind: str) -> list[str]   # kind: unet_name/lora_name/clip_name/vae_name
    def interrupt(self) -> None                     # 仅用户显式点"停止"时调用
```

---

## `vm/gen.py`（Lead）

```python
def build_render_workflow(shot: Shot, params: dict, ref_names: list[str],
                          frames: int, lora: str, unet: str) -> dict:
    """构造 H3 R2V 工作流（复刻本机已验证结构：VAELoader×2 + CLIPLoader + UNETLoader
    + LoraLoaderModelOnly + MiniMaxH3ReferenceToVideo + DualClockSampler + AVDecode
    + VHS_VideoCombine）。参考图键名必须是 Autogrow 平铺键
    `ref_images.ref_image_N`（传数组会静默失效）。"""

def render_shot(proj: Project, shot: Shot, params: dict, comfy: "Comfy",
                manifest: "Manifest", ck: "Checkpoint", log, *, force=False) -> Path | None:
    """单镜生成，含三态判定与检查点回收。返回产物路径，跳过则返回 None。"""

def render_all(proj: Project, params: dict, *, only: list[str] | None = None,
               force: bool = False, dry: bool = False, log=None) -> dict:
    """返回 {"rendered":[...], "skipped":[...], "reclaimed":[...], "failed":[...]}"""
```

---

## `vm/chars.py`（Lead）

```python
def gen_char(proj: Project, name: str, params: dict, log) -> Path
    """prompts/char_<名>.txt → H3 T2VA 生成 5s 1:1 → 抽第 24 帧 → refs/char_<名>.png
    已验证参数：--model pruned --steps 8 --size 1x1 --seconds 5（4/4 成功，每个 40s）"""

def gen_all_chars(proj: Project, params: dict, *, force=False, log=None) -> dict[str, Path]
```

---

## `vm/qc.py`（qc-asm）

```python
@dataclass
class QCResult:
    shot: str
    ok: bool                 # True = 无硬故障，可进成片；False = 有硬故障或检查未执行
    issues: list[str]        # 人类可读，带严重级别前缀：故障 / 可疑 / 未执行
    metrics: dict            # {"mean":..,"std":..,"rms":..,"freeze_ratio":..,"v_dur":..,"a_dur":..,"verdict":..}

def check_clip(path: Path, *, samples: int = 5, target_sec: float | None = None) -> QCResult
def check_all(proj: Project, log=None) -> dict[str, QCResult]     # 写 state/qc.json
```

> **修订 R1**：`check_clip` 的 `target_sec` 是**向后兼容的追加可选参数**。
> "时长与目标差 > 1s"必须有目标秒数才算得出来，契约 v1 漏写了；不传即跳过时长比对，
> 既有的 `check_clip(path, samples=5)` 调用方式完全不受影响。`check_all` 从镜头表 `shot.sec` 传入。
> `check_clip` 内部消化所有异常，坏文件返回 `verdict="error"` 而不是抛出 —— 调用方不会因为一个坏文件而 `continue` 掉整轮质检。

> **修订 R4（2026-09-23，Lead 指派）**：段尾冻结判据换成**逐像素绝对差**（详见判据表与下方说明）。
> 旧判据（相邻帧灰度均值差占比）在真实 52 镜上 100% 误报，已废弃但值仍保留在 `metrics["freeze_ratio"]`。

**三防线判据（必须照此实现）**：
| 检查 | 判据 |
| --- | --- |
| 纯色/故障 | 采样帧灰度 std < 3 **且** 色数 < 10 → 故障 |
| 近黑可疑 | std ≥ 3 且 平均亮度 < 20 → 可疑（**必须配合 std，否则夜戏误杀**；不要用 std<8 判故障） |
| 静音 | 音轨 RMS < 0.02 且峰值 < 0.06 |
| **段尾冻结** | 末尾 25% 窗口内，相邻帧**逐像素绝对差**均值 < 0.5（160×160 灰度，R4 改） |
| 时长 | 与目标差 > 1s |
| 音轨缺失 / 音画同步 | 无 audio stream / video-audio 时长差 > 0.5s |
| **未执行** | 采样成功帧数为 0 → **必须报错**，不可静默判过 |

> **★ R4：段尾冻结为什么必须用"逐像素差"而不是"灰度均值差"**
> 旧判据测的是物理量本身错了：**画面有运动时平均灰度几乎不变**（运动改变的是像素分布，不是均值）。
> 实测后果：真实 52 镜**全部**判可疑（ratio 0.97~1.00），全中等于没判。
> 人工标定（同源 `1-1-01`，160×160）：
>
> | 尾部冻结 | 旧判据 ratio | 新判据 pixel_diff |
> | --- | --- | --- |
> | 0s（原片） | 0.971 | 1.393 |
> | 0.6s | 0.974 | 0.887 |
> | 1.2s | 0.976 | **0.448** ✓检出 |
> | 1.8s | 1.000 | **0.068** ✓检出 |
>
> 旧值从 0.971 到 1.000 几乎不动（零敏感），新值单调跟随冻结时长。
> 判定用 `metrics["freeze_pixel_diff"]`；`metrics["freeze_ratio"]` **已废弃**，只留作对比。
> 另留 `metrics["freeze_body_diff"]`（非尾部窗口的逐像素差）供人区分"全片都慢"还是"只有尾巴静止"。
> 修复后真实 52 镜：46 pass / 6 可疑（均为尾部减速），0 硬故障。

**严重级别与 `ok` 语义（Lead 已裁决，勿改）**：

| verdict | 含义 | `ok` | 去向 |
| --- | --- | --- | --- |
| `pass` | 全绿 | True | 直接进成片 |
| `suspicious` | 可疑：近黑夜戏 / 段尾冻结 | **True（不阻塞）** | 进 `review` 人工看，**不进重渲队列** |
| `fail` | 硬故障：纯色 / 静音 / 无音轨 / 时长异常 / 音画不同步 | False | 进 `rerender` 重渲队列 |
| `error` | 检查未执行：文件缺失/损坏、抽帧失败、`samples=0` | False | 进 `rerender`，先查原因 |

> **为什么可疑不阻塞**：用户实测有一场亮度 10 / std 15.3 的**正常夜戏**，自动重渲既白烧 GPU
> 又永远修不好；段尾冻结判据对"亮度恒定但有真实运动"的内容同样会误报。
> 因此 `ok=True` 只代表"没有硬故障"，不代表"没毛病" —— 调用方要看 `verdict` / `issues`。

**`state/qc.json` 形状**：

```json
{
  "generated_at": 1758600000,
  "results": {
    "1-3-01": {"shot": "1-3-01", "ok": true, "verdict": "suspicious",
               "issues": ["可疑: …偏暗…需人工确认是否夜戏"], "metrics": {"mean": 14.4, "std": 12.5}}
  },
  "rerender": ["1-3-04"],
  "review": ["1-3-01"]
}
```

`rerender` = `ok=False` 的镜头号；`review` = `verdict=suspicious` 的镜头号。UTF-8 不转义中文、原子写。

---

## `vm/assemble.py`（qc-asm）

```python
def build_srt(shots: list[Shot], out: Path, *, fps: int = 24) -> Path
def assemble(proj: Project, *, episode: str = "EP01", transition: str = "cut",
             burn_subtitle: bool = True, skip_shots: list[str] | None = None, log=None) -> Path
    """clips/*.mp4 按镜头表顺序拼接 → final/<episode>.mp4
    规格：libx264 crf18 / yuv420p / aac 32kHz 128k / faststart
    concat 清单必须用绝对路径；参数不一致时 fallback 重编码
    transition: cut(硬切) / fade(闪黑) / dissolve(叠化)
    中文字体先探测存在性（Noto Sans CJK / 文泉驿），缺失要报错并给出安装提示"""
```

> **R4 补充（A2/C5，签名不变）**：
> · **A2**：concat 与字幕时间轴一律用 `sec_actual`（镜头表字段 → 没有就 `ffprobe -show_entries format=duration`
>   现读；**回读失败直接报错，绝不回退到请求的 `sec`**）。`build_srt` 拿不到文件路径，
>   只能用镜头表已有字段，缺字段时退回 `shots.timeline_sec()` 口径。
> · **C5**：起始裁剪由 `project.json` 的 `assemble.trim_start_sec`（或环境变量 `VM_TRIM_START_SEC`）控制，
>   **默认 0 = 关闭**；裁剪时长会从时间轴扣掉，并记入 `state/manifest.json` 的 note + `state/assemble.json`。
> · 落盘顺序：`.srt` 先写临时文件，成片与字幕一起 `os.replace` 生效 —— 编码失败不会留下"新字幕 + 旧成片"。

---

## `vm/web.py` + `pipeline.py`（webui）

```python
def serve(proj_root: Path, port: int = 8801) -> None
```
**HTTP API（GUI 与 CLI 共用同一套控制层）**：
| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/` | 单页 UI |
| GET | `/api/projects` | 项目列表 |
| GET | `/api/status?project=` | 阶段/进度/当前镜头/运行中标志 + 最近日志尾 |
| GET | `/api/shots?project=` | 镜头表 + 每镜三态 + 质检结果 |
| GET | `/api/log?project=&offset=` | 增量日志 |
| POST | `/api/run` | `{project, stage, only?, force?}` 启动（**同项目同时只允许一个任务**） |
| POST | `/api/stop` | 停止当前任务 |
| POST | `/api/rerender` | `{project, shot_id}` 单镜强制重渲 |
| GET | `/view?project=&shot=` | 产物预览（缩略图/播放） |

**CLI（`pipeline.py`）**：
```
python pipeline.py <项目> --stage plan|chars|render|qc|assemble|all
                         [--only 1-1-01,1-1-02] [--force] [--dry-run]
python pipeline.py --list
python pipeline.py <项目> --serve --port 8801
```
**任务控制要落状态**（`state/task.json`：`{stage, shot, pid, started_at, stopped}`），
这样 CLI 与 Web 不会各跑一份、也不会互相覆盖。

---

# 修订 R2：控制台补齐「抽卡 / 采纳 / 提示词编辑」

> 2026-09-23 追加。原因：抽卡功能是在 R1 契约之后才设计的，**没有进控制台**，
> 导致用户能生成 24 张候选却无法在 UI 里挑选 —— 与"用户自己操作控制台"的目标不符。
> 本节由 webui 实现；`vm/chars.py` 侧的 `gen_candidates / adopt_candidate /
> import_external_image / list_candidates / check_prompt_style` 已就绪（Lead 已写）。

## 新增阶段：`gacha`

纳入 `taskctl` 的统一任务模型（与 plan/chars/render/qc/assemble 同级），要求：
- `POST /api/run {project, stage:"gacha", only:["孙悟空"]}` → 走 taskctl 长任务
- 进度/日志/停止/并发锁全部复用现有机制，不另起一套
- worker 调 `vm.chars.gen_candidates(proj, name, params, n)`
- `n` 通过 `state/task.json` 的 `count` 字段传入（**默认 2**，2026-09-23 用户反馈改为 2）

## 新增 HTTP API

| 方法 | 路径 | 请求 | 响应 |
| --- | --- | --- | --- |
| GET | `/api/chars?project=X` | — | 见下方结构 |
| POST | `/api/chars/gacha` | `{project, name, n?}` | 启动 gacha 长任务（同项目并发锁生效） |
| POST | `/api/chars/adopt` | `{project, name, file}` | 采纳候选 → 覆盖 refs/char_<名>.png |
| POST | `/api/chars/upload` | `{project, name, filename, b64}` | 上传自有图当定妆照（`import_external_image`） |
| POST | `/api/chars/prompt` | `{project, name, text}` | 保存 `prompts/char_<名>.txt` |
| GET | `/view` | 扩展参数：`project,kind,name,file` | `kind ∈ ref/gacha/sheet/char_raw`；也保留原有 `shot` 用法 |

`GET /api/chars` 响应结构：
```json
{"ok": true, "chars": [
  {"name": "孙悟空",
   "prompt": "integrated_multimodal_description: ...",
   "prompt_warnings": [],                 // check_prompt_style 的结果，非空时 UI 要高亮告警
   "ref_url": "/view?project=西游记&kind=ref&name=孙悟空",
   "ref_mtime": 1758600000,
   "candidates": [{"file":"seed2741.png","seed":2741,
                   "url":"/view?project=西游记&kind=gacha&name=孙悟空&file=seed2741.png",
                   "adopted": false}, ...],
   "sheet_url": "/view?project=西游记&kind=sheet&name=孙悟空"}   // 可能为 null
]}
```
`adopted` 的判定：候选文件与 `refs/char_<名>.png` 内容相同（比 size+sha256 前 16 字节即可）。

## UI 要求（单页内新增「角色定妆」区）

1. **角色卡片列表**：每个角色一张卡，左图（当前 ref）右信息（名字/参考图修改时间/提示词编辑框）
2. **提示词编辑**：可直接改并保存（调 `/api/chars/prompt`）；若 `prompt_warnings` 非空，
   顶部显示黄色告警条，文案要点明"风格锚会把画面拉成廉价 3D 卡通，建议删掉"
3. **抽卡按钮**：填张数（默认 6）→ 调 `/api/chars/gacha` → 复用现有进度条与日志区
4. **候选网格**：每个候选一张缩略图 + seed 标签 + 「采纳」按钮；已采纳的那张加绿色边框与「当前」
5. **上传按钮**：选本地图片 → 转 base64 → `/api/chars/upload`
6. **采纳后**：刷新角色卡与镜头网格（相关镜头会变成 `stale`，UI 要能看出"待重渲"）

## 硬约束（不变）
不启停 ComfyUI、不调 `/free`、不动 `/home/max/ComfyUI/output/`、停止只按精确 PID。

---

# 修订 R3：P1 镜头详情/编辑接口（已实现）

> 2026-09-23 · shot-edit 交付，Lead 补录契约。签名以 `vm/web.py` 实际实现为准。

| 方法 | 路径 | 请求 |
| --- | --- | --- |
| GET | `/api/shot` | `?project=&id=` → 单镜详情含 `sections` 六段 + `missing_sections` |
| POST | `/api/shot/update` | `{project, id, patch}` |
| POST | `/api/shot/rewrite` | `{project, id, feedback?}` |
| POST | `/api/shot/split` | `{project, id, field?}`（field ∈ dialogue/narration） |
| POST | `/api/shot/merge` | `{project, id}` |
| POST | `/api/shot/insert` | `{project, after, shot?}` |
| POST | `/api/shot/delete` | `{project, id}` |
| POST | `/api/shot/undo` | `{project}`（撤销上一次编辑，靠 `shots/*.json.bak`） |
| POST | `/api/shots/renumber` | `{project, force?}` |
| POST | `/api/shots/bulk` | `{project, selector, patch}` |

## ★ R3.1 最重要的一条：**字段编辑必须"确定性同步进提示词"，否则指纹不变、改动永远不生效**

`shot_fingerprint` 只含 `prompt / chars / refs / 渲染参数 / frames / seed`。
**只改 `dialogue` 等字段而不同步 prompt，指纹不会变 → 状态仍是 `current` → 不会重渲 →
而 H3 念的是提示词里的旧台词 → 用户以为改了、其实永远无效。** 这是静默失效，比报错更糟。

已实现的确定性同步（**不通 LLM**）：

| 字段 | 同步方式 |
| --- | --- |
| `dialogue` / `narration` | 在 `<d>` 里逐字替换旧句；旧句不在提示词里则按 plan 的说话人推断追加（复用 `pl._repair_missing_lines`） |
| `camera` | 用 `plan._assemble_prompt` 的权威措辞重写 `CAMERA DISCIPLINE:` 行；没有则按"纪律前移"插到 `detailed_description` 前；认不出的运镜**只告警不删行** |
| `shot_size` / `action` | **无法机械同步 → 必须明确告警**"不参与指纹，要生效请点『重写提示词』" |
| `costume` | ⚠️ **见 R3.2**（P2 需补确定性重建） |

改台词后额外返回 `prompt_mismatch`：能识别 `<d>` 里"还多出别的字"（拆镜后两镜都挂着原句会被点出来）。

## ★ R3.2 P2 待补：`costume` 变更必须重建 prompt 的这两段

服装只改 `shot.costume` 字段不会动 prompt。需要在字段变更时**确定性重建**：
- `subject_definitions`：用 `costumes.identity_of()` + 该变体的 `prompt`
- `retention_analysis`：默认变体用严格措辞；非默认变体用放宽措辞（"CLOTHING is authoritative from the wording above …"）

复用 `plan._assemble_prompt()` 即可（它已支持 `costumes` / `shot_costume` 参数），不做 LLM 调用。

## R3.3 其它已实现的保护

- **只拒绝"本次引入的新问题"**（before/after 问题集合做差）：表里可能本来就有历史毛病，一律拒绝会让用户因为不相干的旧问题彻底改不动。历史问题照实回传 `issues` 让 UI 显示。
- **`validate_shots` 不检查单句上限**（`DIALOGUE_MAX`/`NARRATION_MAX` 只在 plan 阶段用），编辑层补了 20/15 字拦截。
- **拆分/插入不改任何已有镜头号**（用 `1-4-01_2` 后缀）：自动重排会让一批镜头的产物/清单记录错位，在渲染进行中尤其危险。要连号用 `/api/shots/renumber`（会连带搬移 clips + manifest + checkpoint，**有任务在跑时拒绝执行**，除非 `force`）。
- **拆/并/插的提示词是复制的** → 会渲出两个一样的镜头，所以响应带 warnings 提示"请重写提示词"（刻意不自动调 LLM，避免偷偷花钱）。
- 编辑用**独立锁，不占项目锁**：用户可以在渲染进行时改后面还没渲的镜头。
