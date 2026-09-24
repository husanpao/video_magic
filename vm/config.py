"""
config.py —— 配置中心（T3）：把「后台写死、用户抓瞎」的配置全部开放出来。

## 为什么有这个模块
用户原话：「目前很多配置都是后台固定的，用户抓瞎」。此前 ComfyUI 地址、渲染参数、
LLM、分镜图模型、字幕字体与样式、预算、风格、帧网格上限……散落在 DEFAULT_PARAMS /
环境变量 / 模块常量 / 手改 project.json 四个地方，UI 一个都改不了，改错了也没有人话报错。

## 三层配置（低 → 高，后者覆盖前者）
    ① 内置默认    taskctl.DEFAULT_PARAMS 与本文件 SCHEMA 的 default（= 契约冻结值）
    ② 用户全局    ~/.config/video_magic/config.json    （机器级手改，如 ComfyUI 装哪）
    ③ 工作区全局  projects/config.json                 （全局层的**写入目标**，随工作区走）
    ④ 项目覆盖    projects/<项目>/project.json          （项目层）
合并语义**沿用 taskctl.load_params**：顶层键按键覆盖、`llm` 段按键合并。
（budget / assemble 段只允许项目层，见下，不存在跨层合并问题。）

## 三条生效通道（实测各消费方的读取方式后定的，不是拍脑袋）
  A. params 通道  install() 把**全局层**物化进 taskctl.DEFAULT_PARAMS ——
     load_params 每次深拷它再叠 project.json，于是所有 load_params 调用点
     （web 的指纹计算 shot_table、worker 的渲染、chars/gen）自动拿到同一份生效值。
     ★ 为什么这样做：镜头指纹在 web 进程（shot_table）与 worker（gen）各算一份，
     两边参数必须逐字节一致，否则"刚渲完就显示需重渲"。物化进 DEFAULT_PARAMS 让
     **既有代码一行不改**就拿到两层合并结果，指纹天然一致。
  B. env 通道     QI_UNET / QI_CLIP / QI_VAE / COMFYUI_MODELS_ROOT /
     VM_SUBTITLE_FONT / VM_FRAME_MIN / VM_FRAME_MAX —— qi.py、assemble.py、
     shots.py 本来就靠环境变量切换，install() 按生效值注入。
  C. 直读通道     budget 段（budget.budget_of）与 assemble.trim_start_sec
     由消费方**直读 project.json**，全局层影响不到它们 —— 所以这两段只允许
     项目层覆盖（保存时拒收其他层，人话解释），绝不做"UI 显示生效、实际没生效"的骗子。

## ★ API Key 纪律（CONTRACTS.md，不可破坏）
Key 永远只走：环境变量 DEEPSEEK_API_KEY → ~/.config/video_magic/deepseek_key。
**配置文件、项目文件、API 请求/响应里都永远不出现 Key 本身** —— schema 是白名单，
key/token/secret 类键名直接拒收；接口只报 api_key_present（有没有），不报内容。

## 对外函数清单（web.py handler / pipeline.py / assemble.py / 测试用）
    get_config(project, root)                → GET  /api/config 的响应体
    save_config(project, changes, root, …)   → POST /api/config 的响应体
    validate_bundle(project, values, root)   → POST /api/config/validate 的响应体
    effective_values(project, root)          → 扁平生效值（本模块的权威合并结果）
    to_params(flat)                          → 扁平值 → load_params 形状（嵌套）
    coerce_values(values, layer)             → schema 校验 + 类型收敛（人话错误）
    impact_of(proposed, project, root)       → 危险项影响清单（哪些镜头变 stale）
    preflight(flat, root)                    → 环境预检（ComfyUI/模型/字体/ffmpeg/Key）
    install(project, root)                   → 装进当前进程（A+B 通道），返回最终 params
    apply_env(flat)                          → 只做 B 通道
    subtitle_settings(project, root)         → 给 vm/assemble.py 的字幕字体与样式
    read_layers(project, root)               → 各层原始值（UI 的继承/覆盖标记用）
风格：中文注释解释「为什么」，原子写（.tmp + os.replace），UTF-8 不转义中文。
"""

from __future__ import annotations

import copy
import json
import os
import re
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# 字体默认值从 assemble.py 引用（单一真相）：它硬编码的候选/兜底路径就是契约现状。
# 局部 try：并行开发期 assemble.py 可能还没就绪，schema 不该因此整页崩掉。
try:
    from .assemble import FONT_CANDIDATES as _FONT_CANDIDATES
    from .assemble import FONT_FILE_HINTS as _FONT_FILE_HINTS
except ImportError:  # pragma: no cover - 直接脚本运行时的兜底
    try:
        from assemble import FONT_CANDIDATES as _FONT_CANDIDATES  # type: ignore
        from assemble import FONT_FILE_HINTS as _FONT_FILE_HINTS  # type: ignore
    except ImportError:
        _FONT_CANDIDATES = []
        _FONT_FILE_HINTS = []

FONT_FILE_HINTS_DEFAULT = [f"{p}|{fam}" for p, fam in _FONT_FILE_HINTS]

# API Key 纪律的统一提示语（多处复用，措辞必须一致）
API_KEY_HINT = (
    "DeepSeek API Key 不走配置（纪律）：只认环境变量 DEEPSEEK_API_KEY 或 "
    "~/.config/video_magic/deepseek_key 文件（600 权限）。"
    "配置文件、项目文件、API 里永远不出现 Key 本身。"
)

# key/token/secret 类键名一律拒收（白名单之外的第一道防线）
_SECRET_KEY_RE = re.compile(r"(key|token|secret|password|passwd|credential)", re.I)

# ASS 颜色：&HAABBGGRR（AA=透明度，BBGGRR 是蓝/绿/红 —— 和 CSS 的 #RRGGBB 相反）
_COLOR_RE = re.compile(r"^&H[0-9A-Fa-f]{8}$")

LAYER_USER = "user"
LAYER_WORKSPACE = "workspace"
LAYER_PROJECT = "project"
LAYER_ENV = "env"
LAYER_BUILTIN = "builtin"

# UI 的分组顺序（任务要求：连接/渲染/模型/字幕/预算/风格 + 现有隐藏开关归「合成」）
GROUPS = ("连接", "渲染", "模型", "字幕", "合成", "预算", "风格")


class ConfigError(ValueError):
    """schema 校验失败。**继承 ValueError** —— web 的错误边界把 ValueError 映射成
    4xx 人话（配置填错是请求问题，不是服务端故障）。errors 带逐项问题清单给 UI。"""

    def __init__(self, message: str, errors: list[dict] | None = None):
        super().__init__(message)
        self.errors = list(errors or [])


@dataclass(frozen=True)
class Item:
    """一个配置项的完整元数据。说明文案三段式：是什么 / 改了影响什么 / 会不会让镜头变 stale。"""

    key: str                 # 点分路径 = JSON 存储路径（llm.base_url → {"llm":{"base_url":…}}）
    group: str               # GROUPS 之一
    label: str
    kind: str                # str / url / path / int / float / bool / enum / color / str_list
    default: Any             # 内置默认（None = 默认不设/不设限）
    what: str                # 是什么
    effect: str              # 改了影响什么
    stale: str               # 会不会让镜头变「需重渲」（人话）
    scope: str = "both"      # both / project（只许项目层）/ global（只许全局层）
    stale_impact: bool = False   # 进镜头指纹 → 改动让相关镜头变 stale（危险项）
    render_only: bool = False    # 影响画面但**不**进指纹 → 改了不会自动重渲（静默项）
    channel: str = "params"  # 生效通道：params / env / file（直读 project.json）
    env: str = ""            # channel=env 时的环境变量名
    enum: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    multiple: int | None = None
    nullable: bool = False
    placeholder: str = ""


def _it(*a, **kw) -> Item:
    return Item(*a, **kw)


SCHEMA: tuple[Item, ...] = (
    # ── 连接 ──────────────────────────────────────────────────────────────
    _it(
        "comfy_url", "连接", "ComfyUI 地址", "url", "http://127.0.0.1:8188",
        what="ComfyUI 服务地址。所有出图/出视频都提交到它（只读+提交，本项目绝不启停它）。",
        effect="换了它 = 换一台干活的机器（本机 8188，或局域网另一台 ComfyUI）。改错的症状是所有任务报「连不上」。",
        stale="不进镜头指纹 —— 已生成的镜头不会因此变「需重渲」。",
    ),
    _it(
        "comfy_input", "连接", "ComfyUI input 目录", "path", "/home/max/ComfyUI/input",
        what="ComfyUI 的 input 目录。参考图必须拷到这里才能被 LoadImage 读到（项目内 refs/ 只是留档）。",
        effect="ComfyUI 装在非默认位置时改这里。改错的症状是提交时提示「找不到图片」。",
        stale="不进镜头指纹。",
    ),
    _it(
        "llm.base_url", "连接", "LLM 接口地址", "url", "https://api.deepseek.com",
        what="拆镜 / AI 改写用的 LLM 接口（OpenAI 兼容）。",
        effect="换成其他兼容服务（如本地 vLLM / 中转）时改这里。" + API_KEY_HINT,
        stale="不进镜头指纹；对已拆好的镜头表无影响，下次拆镜才生效。",
    ),
    # ── 渲染（进镜头指纹的危险项）─────────────────────────────────────────
    _it(
        "width", "渲染", "宽度（像素）", "int", 864,
        what="出片分辨率的宽。",
        effect="像素越多越慢、越吃显存。864×480 是本机唯一实测跑通的组合，改大前先拿单镜试。",
        stale="⚠️ 会。分辨率进镜头指纹 —— 改了以后全部已生成镜头变「需重渲」。",
        stale_impact=True, minimum=64, maximum=2048, multiple=16,
    ),
    _it(
        "height", "渲染", "高度（像素）", "int", 480,
        what="出片分辨率的高。",
        effect="同宽度；必须是 16 的倍数（VAE 8× × patch 2× 的要求）。",
        stale="⚠️ 会。全部已生成镜头变「需重渲」。",
        stale_impact=True, minimum=64, maximum=2048, multiple=16,
    ),
    _it(
        "fps", "渲染", "帧率", "int", 24,
        what="成片帧率。",
        effect="影响每镜帧数（帧网格 17n+5 按它换算）与流畅度/渲染耗时。H3 原生 24fps，其他值未实测。",
        stale="⚠️ 会（帧数进指纹）—— 改了全部已生成镜头变「需重渲」。",
        stale_impact=True, minimum=8, maximum=60,
    ),
    _it(
        "steps", "渲染", "采样步数", "int", 8,
        what="渲染采样步数（turbo LoRA 配 8 步）。",
        effect="步数越高细节越多、每镜耗时线性上涨。用 turbo LoRA 时 8 步是实测甜点；去掉 LoRA 要把步数提上去。",
        stale="⚠️ 会。全部已生成镜头变「需重渲」。",
        stale_impact=True, minimum=1, maximum=40,
    ),
    _it(
        "unet", "渲染", "视频主模型（unet）", "str",
        "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
        what="H3 Ref2VA 视频主模型的文件名（放在 ComfyUI models/diffusion_models/ 下）。",
        effect="换模型 = 换画风与能力集；21GB 剪枝版是本机实测配置。换了它 LoRA/steps 通常要跟着调。",
        stale="⚠️ 会。全部已生成镜头变「需重渲」。",
        stale_impact=True,
        placeholder="文件名（可含子目录），不要写绝对路径",
    ),
    _it(
        "lora", "渲染", "渲染 LoRA", "str",
        "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors",
        what="渲染用的 LoRA（turbo 8 步加速，放在 models/loras/ 下）。",
        effect="去掉/换 LoRA 后步数要跟着调（turbo LoRA 是 8 步口径）。",
        stale="⚠️ 会。全部已生成镜头变「需重渲」。",
        stale_impact=True,
        placeholder="文件名（可含子目录），不要写绝对路径",
    ),
    _it(
        "frame_min", "渲染", "单镜最短帧数", "int", 56,
        what="帧数网格 17n+5 的下限（56 帧 ≈ 2.3 秒）。比它短的镜头会被拉到这个长度。",
        effect="机器级设置（显存/模型决定），只写全局层；值必须落在 17n+5 网格上（56/73/90/…）。",
        stale="⚠️ 会（帧数进指纹）—— 只有镜头的帧数因此变化时才影响它。",
        stale_impact=True, scope="global", channel="env", env="VM_FRAME_MIN",
        minimum=22, maximum=2000,
    ),
    _it(
        "frame_max", "渲染", "单镜最长帧数", "int", 362,
        what="帧数网格 17n+5 的上限（362 帧 ≈ 15.08 秒，本机显存实测上限）。",
        effect="机器级设置，只写全局层。改大前确认显存扛得住（爆显存的表现是 ComfyUI 任务直接失败）。",
        stale="⚠️ 会（帧数进指纹）—— 只有镜头的帧数因此变化时才影响它。",
        stale_impact=True, scope="global", channel="env", env="VM_FRAME_MAX",
        minimum=22, maximum=4096,
    ),
    # ── 模型 ──────────────────────────────────────────────────────────────
    _it(
        "llm.model", "模型", "LLM 模型名", "str", "deepseek-chat",
        what="拆镜 / AI 改写用的模型名。",
        effect="deepseek-chat 是「不压缩」提示词实测调教过的；换模型拆镜质量自负。",
        stale="不进镜头指纹；对已拆好的镜头表无影响，下次拆镜才生效。",
    ),
    _it(
        "llm.temperature", "模型", "LLM 温度", "float", 0.3,
        what="拆镜 LLM 的随机性（0=保守复读，1=放开发挥）。",
        effect="调高拆镜更跳、台词改写风险更大；本项目纪律是「不压缩」，建议保持 0.3 附近。",
        stale="不进镜头指纹；下次拆镜才生效。",
        minimum=0.0, maximum=2.0,
    ),
    _it(
        "vae_video", "模型", "视频 VAE", "str", "minimax_h3_video_vae_fp16.safetensors",
        what="H3 视频 VAE（模型配套件，放在 models/vae/ 下）。",
        effect="换主模型（unet）时通常要一起换。",
        stale="⚠️ 它**不进**镜头指纹 —— 改了不会自动重渲！要生效请对相关镜头「强制重渲」。",
        render_only=True,
    ),
    _it(
        "vae_audio", "模型", "音频 VAE", "str", "minimax_h3_audio_vae_fp32.safetensors",
        what="H3 音频 VAE（配音随视频一起出，靠它解码）。",
        effect="同视频 VAE，属于模型配套件。",
        stale="⚠️ 不进镜头指纹 —— 改了不会自动重渲，要生效请「强制重渲」。",
        render_only=True,
    ),
    _it(
        "clip", "模型", "视频模型文本编码器", "str", "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
        what="H3 侧的文本编码器（models/text_encoders/ 下），决定提示词怎么被理解。",
        effect="模型配套件；换它=换提示词的「读法」。",
        stale="⚠️ 不进镜头指纹 —— 改了不会自动重渲，要生效请「强制重渲」。",
        render_only=True,
    ),
    _it(
        "qi_unet", "模型", "分镜图模型（Qwen-Image）", "str", "qwen_image_nvfp4.safetensors",
        what="定妆照 / 场景道具概念图 / 分镜图用的文生图模型。",
        effect="Qwen-Image 2.1 本机 ComfyUI 0.36 加载不了（实测报 Could not detect model type），2.0 nvfp4 可跑。",
        stale="不进镜头指纹（分镜图不参与渲染指纹）；对之后的出图生效。",
        channel="env", env="QI_UNET",
    ),
    _it(
        "qi_clip", "模型", "分镜图文本编码器", "str", "qwen_2.5_vl_7b_nvfp4.safetensors",
        what="Qwen-Image 配套的文本编码器（models/text_encoders/ 下）。",
        effect="模型配套件，换 Qwen-Image 版本时一起换。",
        stale="不进镜头指纹；对之后的出图生效。",
        channel="env", env="QI_CLIP",
    ),
    _it(
        "qi_vae", "模型", "分镜图 VAE", "str", "qwen_image_vae.safetensors",
        what="Qwen-Image 配套的 VAE（models/vae/ 下）。",
        effect="模型配套件。",
        stale="不进镜头指纹；对之后的出图生效。",
        channel="env", env="QI_VAE",
    ),
    _it(
        "models_root", "模型", "ComfyUI models 目录", "path", "/home/max/ComfyUI/models",
        what="ComfyUI 的 models 根目录。设置页的「模型文件在不在」检查就看它（只读，绝不写/删）。",
        effect="ComfyUI 装在别处时改这里；改错的症状是预检说模型缺失（哪怕 ComfyUI 自己跑得好好的）。",
        stale="不进镜头指纹。",
        channel="env", env="COMFYUI_MODELS_ROOT",
    ),
    # ── 字幕 ──────────────────────────────────────────────────────────────
    _it(
        "subtitle_font", "字幕", "字幕字体", "str", "",
        what="烧字幕用的中文字体族名（如 Noto Sans CJK SC）。留空 = 自动探测。",
        effect="字体不对会烧出满屏方块（libass 缺字形）。合成阶段使用，重跑「合成」即生效。",
        stale="不进镜头指纹（字幕是合成期烧的）；重跑「合成」生效，不用重渲任何镜头。",
        channel="env", env="VM_SUBTITLE_FONT", placeholder="留空 = 自动探测（Noto Sans CJK → 文泉驿）",
    ),
    _it(
        "subtitle_font_candidates", "字幕", "字体探测顺序", "str_list", list(_FONT_CANDIDATES),
        what="自动探测字体时 fc-match 逐个尝试的候选顺序（族名）。",
        effect="本机实测存在：Noto Sans CJK SC / 文泉驿。把你要用的族名挪到最前可以强制优先。",
        stale="不进镜头指纹；重跑「合成」生效。",
    ),
    _it(
        "subtitle_font_file_hints", "字幕", "字体文件兜底", "str_list", FONT_FILE_HINTS_DEFAULT,
        what="fc-match 不可用（有的机器没装 fontconfig 匹配数据）时，按这些文件存在性兜底。每行「字体文件路径|族名」。",
        effect="只有自动探测失败时才会用到。",
        stale="不进镜头指纹；重跑「合成」生效。",
        placeholder="/usr/share/fonts/.../NotoSansCJK-Regular.ttc|Noto Sans CJK SC",
    ),
    _it(
        "subtitle_font_size", "字幕", "字幕字号", "int", 0,
        what="字幕字号。0 = 按画面高度自适应（高/22，与 libass 的 SRT 基准分辨率一致）。",
        effect="改了要重跑「合成」才看得到。",
        stale="不进镜头指纹；重跑「合成」生效。",
        minimum=0, maximum=200,
    ),
    _it(
        "subtitle_primary_color", "字幕", "字幕字颜色", "color", "&H00FFFFFF",
        what="字幕字颜色，ASS 格式 &HAABBGGRR（AA=透明度 00=不透明，BBGGRR 是蓝绿红 —— 和 CSS 的 #RRGGBB 相反）。&H00FFFFFF = 白色。",
        effect="夜戏多的片建议保持白字；改了要重跑「合成」。",
        stale="不进镜头指纹；重跑「合成」生效。",
    ),
    _it(
        "subtitle_outline_color", "字幕", "描边颜色", "color", "&H00000000",
        what="字幕描边颜色，格式同上。&H00000000 = 黑色（白字黑边是可读性最好的组合）。",
        effect="改了要重跑「合成」。",
        stale="不进镜头指纹；重跑「合成」生效。",
    ),
    _it(
        "subtitle_outline", "字幕", "描边宽度", "float", 2,
        what="字幕描边宽度（libass BorderStyle=1 的 Outline）。",
        effect="描边太细在浅色背景上读不清；太粗糊成一团。",
        stale="不进镜头指纹；重跑「合成」生效。",
        minimum=0, maximum=6,
    ),
    _it(
        "subtitle_margin_v", "字幕", "底边距", "int", 0,
        what="字幕距画面底部的距离（像素）。0 = 自适应（高/18）。",
        effect="调大可避开台标/字幕栏；改了要重跑「合成」。",
        stale="不进镜头指纹；重跑「合成」生效。",
        minimum=0, maximum=400,
    ),
    # ── 合成（现有隐藏开关，一并开放）─────────────────────────────────────
    _it(
        "include_qc_fail", "合成", "质检不合格镜头进成片", "bool", False,
        what="质检判定硬故障（纯色/静音/无音轨/时长异常）的镜头，默认不进成片。",
        effect="打开 = 强行把它们拼进去（一般只在质检误报时用）。正常做法是重渲这些镜头。",
        stale="不进镜头指纹；重跑「合成」生效。",
    ),
    _it(
        "assemble.trim_start_sec", "合成", "起始裁剪秒数", "float", 0.0,
        what="把每个片段开头裁掉多少秒（统一的片头黑场/杂音）。0 = 关闭。",
        effect="裁剪会从时间轴扣除并逐镜记入 manifest。实测本项目片段没有统一的起始杂音，默认关闭。",
        stale="不进镜头指纹；重跑「合成」生效。",
        scope="project", channel="file", minimum=0.0, maximum=60.0,
    ),
    # ── 预算（budget_of 直读 project.json → 只允许项目层）──────────────────
    _it(
        "budget.max_gpu_minutes", "预算", "单次 GPU 上限（分钟）", "float", None,
        what="单个任务的 GPU 时间上限。超过就**暂停等人批准**（不是失败），批一次放行一次。",
        effect="成本护栏的主开关：一项都不填 = 不启用护栏（向后兼容）。实测基线 41.8 秒/镜。",
        stale="与画面无关，不影响任何镜头。",
        scope="project", channel="file", nullable=True, minimum=0.1, maximum=100000,
    ),
    _it(
        "budget.max_gpu_minutes_per_day", "预算", "每日 GPU 上限（分钟）", "float", None,
        what="今天累计 GPU 时间上限（含本次估算）。",
        effect="防「一晚上烧穿显卡」的兜底闸。",
        stale="与画面无关。",
        scope="project", channel="file", nullable=True, minimum=0.1, maximum=100000,
    ),
    _it(
        "budget.max_shots_per_run", "预算", "单次镜头数上限", "int", None,
        what="单个任务最多碰多少个镜头。",
        effect="防止一次「全链」误把 52 镜全渲了。",
        stale="与画面无关。",
        scope="project", channel="file", nullable=True, minimum=1, maximum=10000,
    ),
    _it(
        "budget.max_llm_tokens", "预算", "单次 LLM token 上限", "int", None,
        what="单个任务的 LLM token 上限（拆镜实测约 1700 tokens/次调用）。",
        effect="防拆镜失控重写烧 token。",
        stale="与画面无关。",
        scope="project", channel="file", nullable=True, minimum=100, maximum=100000000,
    ),
    # ── 风格 ──────────────────────────────────────────────────────────────
    _it(
        "style_preset", "风格", "画面风格", "enum", "auto",
        what="画面风格预设：realistic 写实电影感 / cg 半写实 3D 建模 / anime 日式二维动画 / auto 不指定（按题材推导，等同清除本层设置）。",
        effect="⚠️ 画风主要由**定妆照参考图**决定，文字控不住画风（实测结论）—— 换风格请重新出定妆照，"
               "此设置保证角色卡/场景/道具/提示词的写法与之一致。只对之后的拆镜/定妆/出图生效。",
        stale="不影响已有镜头（风格句已固化在提示词里）；已拆好的镜头表不受影响。",
        enum=("auto", "realistic", "cg", "anime"),
    ),
)

_BY_KEY: dict[str, Item] = {it.key: it for it in SCHEMA}


# ---------------------------------------------------------------- 小工具


def _read_json(path: Path) -> dict:
    """容错读 JSON：缺失/损坏都当空对象 —— 一个坏配置文件不该让整个 UI 打不开。"""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _atomic_write_json(path: Path, data: Any) -> None:
    """原子写（CONTRACTS 硬约束 3）：.tmp + os.replace，掉电不留半截 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _dig_get(d: Any, dotted: str) -> Any:
    """按点分路径取嵌套值。任何一步不是 dict / 键不在 → None（表示"这一层没设"）。"""
    cur = d
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _dig_set(d: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur = d
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _dig_del(d: dict, dotted: str) -> None:
    """按键路径删除；顺手清掉因此空掉的中间段（否则 project.json 会留下 "budget": {} 垃圾）。"""
    parts = dotted.split(".")
    stack: list[tuple[dict, str]] = []
    cur = d
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            return
        stack.append((cur, part))
        cur = nxt
    cur.pop(parts[-1], None)
    for parent, part in reversed(stack):
        if not parent.get(part):
            parent.pop(part, None)


def default_root() -> Path:
    """projects 根目录。与 taskctl.PROJECTS_DIR 同口径（延迟 import，避免模块级耦合）。"""
    from vm import taskctl
    return taskctl.PROJECTS_DIR


def user_config_path() -> Path:
    return Path("~/.config/video_magic/config.json").expanduser()


def workspace_config_path(root: Path | None = None) -> Path:
    return (root or default_root()) / "config.json"


def project_config_path(project: str | os.PathLike[str], root: Path | None = None) -> Path:
    from vm import taskctl
    return taskctl.resolve_project(project, root) / "project.json"


# ---------------------------------------------------------------- 分层读取与合并


def _env_layer() -> dict[str, Any]:
    """环境变量层（比用户全局文件还低的一层）。

    为什么要有：QI_UNET / VM_SUBTITLE_FONT 这些环境变量本来就是官方切换方式
    （qi.py / assemble.py 的注释里写着"只需设环境变量…不用改代码"），
    老用户已经设了就不该被无视。配置文件显式设置时仍以文件为准。
    """
    out: dict[str, Any] = {}
    for it in SCHEMA:
        if it.channel != "env" or not it.env:
            continue
        if it.env in _APPLIED_ENV:
            # 这个环境变量是 apply_env 自己注入的（值来自文件层）——不能把它再当成
            # "用户 export 的一层"读回来，否则删掉配置键后会被自己的注入挡住，
            # 恢复继承/清除设置都检测不到变化。
            continue
        raw = os.environ.get(it.env, "").strip()
        if not raw:
            continue
        if it.kind in ("int", "float"):
            try:
                out[it.key] = int(raw) if it.kind == "int" else float(raw)
            except ValueError:
                continue
        elif it.kind == "bool":
            out[it.key] = raw.lower() in ("1", "true", "yes", "on")
        else:
            out[it.key] = raw
    return out


def read_layers(project: str | os.PathLike[str] | None = None,
                root: Path | None = None) -> dict[str, dict[str, Any]]:
    """各层的原始值（扁平点分键）。UI 靠它标「继承自哪层 / 项目是否覆盖」。"""
    layers: dict[str, dict[str, Any]] = {LAYER_ENV: _env_layer()}
    for name, path in (
        (LAYER_USER, user_config_path()),
        (LAYER_WORKSPACE, workspace_config_path(root)),
    ):
        d = _read_json(path)
        layers[name] = {it.key: v for it in SCHEMA if (v := _dig_get(d, it.key)) is not None}
    layers[LAYER_BUILTIN] = {it.key: it.default for it in SCHEMA}
    if project:
        d = _read_json(project_config_path(project, root))
        layers[LAYER_PROJECT] = {it.key: v for it in SCHEMA if (v := _dig_get(d, it.key)) is not None}
    else:
        layers[LAYER_PROJECT] = {}
    return layers


def _merge_layers(layers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """按 builtin ← env ← 用户 ← 工作区 ← 项目的顺序合并（顶层键按键覆盖、llm 段按键合并）。"""
    out: dict[str, Any] = {}
    for layer in (LAYER_BUILTIN, LAYER_ENV, LAYER_USER, LAYER_WORKSPACE, LAYER_PROJECT):
        out.update(layers[layer])
    return out


def effective_values(project: str | os.PathLike[str] | None = None,
                     root: Path | None = None) -> dict[str, Any]:
    """**权威生效值**（扁平点分键）：builtin ← env ← 用户全局 ← 工作区全局 ← 项目。
    顶层键按键覆盖、`llm` 段按键合并 —— 与 taskctl.load_params 的语义一致
    （扁平点分视角下 llm.* 的逐键合并恰好等价于 load_params 对 llm 段的特殊合并）。"""
    return _merge_layers(read_layers(project, root))


def to_params(flat: dict[str, Any]) -> dict[str, Any]:
    """扁平生效值 → taskctl.load_params 形状（llm / budget / assemble 嵌套，其余顶层）。

    这是「params 通道」的桥：install() 把全局层的这份形状物化进 DEFAULT_PARAMS。
    style_preset="auto" 与 nullable 的空值**不落键** —— 「不设置」必须真的是"键不存在"，
    否则 plan.resolve_preset("auto") 会把它当具体预设读走。
    """
    params: dict[str, Any] = {}
    for it in SCHEMA:
        if it.key not in flat:
            continue
        v = flat[it.key]
        if v is None or (it.key == "style_preset" and v == "auto"):
            continue
        _dig_set(params, it.key, copy.deepcopy(v))
    return params


# ---------------------------------------------------------------- schema 校验


def _coerce_one(it: Item, value: Any) -> Any:
    """类型收敛 + 约束校验。返回收敛后的值；不合法抛 ValueError（人话，带键名）。"""
    where = f"「{it.label}」（{it.key}）"
    if value is None:
        if it.nullable:
            return None
        raise ValueError(f"{where}不能留空（要恢复默认请用「恢复继承」）")

    if it.kind == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{where}只能是 true / false —— 当前 {value!r}")
        return value

    if it.kind in ("int", "float"):
        if isinstance(value, bool):  # bool 是 int 的子类，必须先挡（True=1 会静默溜进去）
            raise ValueError(f"{where}要填数字 —— 当前 {value!r}")
        if isinstance(value, str):
            try:
                value = float(value.strip())
            except ValueError:
                raise ValueError(f"{where}要填数字 —— 当前 {value!r}") from None
        if not isinstance(value, (int, float)):
            raise ValueError(f"{where}要填数字 —— 当前 {value!r}")
        if it.kind == "int":
            if float(value) != int(value):
                raise ValueError(f"{where}要填整数 —— 当前 {value!r}")
            num: int | float = int(value)
        else:
            num = float(value)
        if it.multiple and int(num) % it.multiple:
            raise ValueError(
                f"{where}必须是 {it.multiple} 的倍数（VAE 8× × patch 2× 的硬要求）—— 当前 {num}"
            )
        if it.key == "frame_min" or it.key == "frame_max":
            if (int(num) - 5) % 17:
                raise ValueError(
                    f"{where}必须落在 17n+5 帧网格上（56 / 73 / 90 / … / 362）—— 当前 {num}"
                )
        if it.minimum is not None and num < it.minimum:
            raise ValueError(f"{where}不能小于 {it.minimum} —— 当前 {num}")
        if it.maximum is not None and num > it.maximum:
            raise ValueError(f"{where}不能大于 {it.maximum} —— 当前 {num}")
        return num

    if it.kind == "enum":
        v = str(value).strip()
        if v not in it.enum:
            raise ValueError(f"{where}只能是：{' / '.join(it.enum)} —— 当前 {value!r}")
        return v

    if it.kind == "color":
        v = str(value).strip()
        if not _COLOR_RE.match(v):
            raise ValueError(
                f"{where}要是 ASS 颜色格式 &HAABBGGRR（如白色 &H00FFFFFF）—— 当前 {value!r}"
            )
        return v.upper().replace("&H", "&H")

    if it.kind == "str_list":
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise ValueError(f"{where}要是一串文本（每行一条）—— 当前 {value!r}")
        vals = [x.strip() for x in value if x.strip()]
        if it.key == "subtitle_font_file_hints":
            bad = [x for x in vals if "|" not in x]
            if bad:
                raise ValueError(
                    f"{where}每行要写成「字体文件路径|族名」—— 看不懂这行：{bad[0]!r}"
                )
        return vals

    # str / url / path
    v = str(value).strip()
    if not v and it.default == "":
        return v
    if not v:
        raise ValueError(f"{where}不能留空")
    if "\x00" in v:
        raise ValueError(f"{where}不能含非法字符")
    if it.kind == "url":
        u = urlparse(v)
        if u.scheme not in ("http", "https") or not u.netloc:
            raise ValueError(
                f"{where}要写完整的 http(s) URL，例如 http://127.0.0.1:8188 —— 当前 {value!r}"
            )
        return v.rstrip("/")
    if it.kind == "path":
        if not v.startswith("/"):
            raise ValueError(f"{where}要写绝对路径（以 / 开头）—— 当前 {value!r}")
        return v.rstrip("/")
    if it.key in ("unet", "lora", "vae_video", "vae_audio", "clip", "qi_unet", "qi_clip", "qi_vae"):
        # 模型名：ComfyUI 的对象名，允许子目录，但绝不允许路径穿越/绝对路径
        if v.startswith("/") or v.startswith("\\") or ".." in v.split("/"):
            raise ValueError(
                f"{where}只写文件名（可含子目录，如 sub/model.safetensors），不要写绝对路径 —— 当前 {value!r}"
            )
    return v


def coerce_values(values: dict[str, Any], layer: str) -> dict[str, Any]:
    """整包校验。收集**全部**问题一次报完（不要让用户改一项报一项）。

    layer ∈ user / workspace / project —— 用来执行 scope 约束
    （预算段只认项目层、帧网格只认全局层，见各 Item.scope 的注释）。
    """
    if not isinstance(values, dict):
        raise ConfigError("values 必须是「配置项 → 值」的对象", [])
    errors: list[dict] = []
    clean: dict[str, Any] = {}
    for key, value in values.items():
        key = str(key)
        it = _BY_KEY.get(key)
        if it is None:
            if _SECRET_KEY_RE.search(key):
                errors.append({"key": key, "message": f"「{key}」不走配置 —— " + API_KEY_HINT})
            else:
                errors.append({
                    "key": key,
                    "message": f"未知配置项「{key}」（拼错了？）—— 合法配置项见设置页分组列表",
                })
            continue
        if it.scope == "project" and layer != LAYER_PROJECT:
            errors.append({
                "key": key,
                "message": f"「{it.label}」只能在**项目层**设置（消费方直读 project.json，"
                           f"全局层设了也不会生效 —— 宁可不收，也不做假生效）",
            })
            continue
        if it.scope == "global" and layer == LAYER_PROJECT:
            errors.append({
                "key": key,
                "message": f"「{it.label}」是机器级设置（显存/模型决定），只能在**全局层**设置 —— "
                           f"否则 Web 端和渲染端会算出不同的帧数，指纹就乱了",
            })
            continue
        try:
            clean[key] = _coerce_one(it, value)
        except ValueError as e:
            errors.append({"key": key, "message": str(e)})
    if errors:
        raise ConfigError(
            f"配置有 {len(errors)} 处不合法，一条都没保存：" + "；".join(e["message"] for e in errors[:3]),
            errors,
        )
    return clean


# ---------------------------------------------------------------- 影响分析


def _shot_engine():
    """延迟拿 (shots 模块, Project, shot_fingerprint) —— 配置读取不该强制依赖渲染栈。"""
    from vm.state import Project, shot_fingerprint
    from vm import shots as shots_mod
    return shots_mod, Project, shot_fingerprint


def impact_of(proposed: dict[str, Any], *, project: str | os.PathLike[str] | None = None,
              root: Path | None = None) -> dict:
    """改这些配置会让哪些镜头变「需重渲」。保存前的危险项确认就靠它。

    口径与 taskctl.shot_table 逐字节一致：frames = seconds_to_frames(sec, fps)，
    指纹 = state.shot_fingerprint(prompt, chars, refs, render, frames, seed)。
    只把「现在是 current、指纹会变」的镜头列进 stale_shots —— 本来就缺/本来就 stale 的
    不受影响，不该吓唬用户。
    """
    cur = effective_values(project, root)
    prop = {**cur, **{k: v for k, v in proposed.items()}}
    changed = sorted(k for k in proposed if cur.get(k) != prop.get(k))
    dangerous = [k for k in changed if _BY_KEY[k].stale_impact]
    silent = [k for k in changed if _BY_KEY[k].render_only]
    out: dict[str, Any] = {
        "changed": changed,
        "dangerous": [
            {"key": k, "label": _BY_KEY[k].label, "old": cur.get(k), "new": prop.get(k),
             "stale": _BY_KEY[k].stale}
            for k in dangerous
        ],
        "silent": [
            {"key": k, "label": _BY_KEY[k].label, "old": cur.get(k), "new": prop.get(k),
             "note": _BY_KEY[k].stale}
            for k in silent
        ],
        "stale_shots": [], "stale_count": 0, "total_shots": 0,
        "message": "",
    }
    if not dangerous:
        out["message"] = "这些改动不影响镜头指纹，无需重渲。"
        return out
    if not project:
        # 全局层改动不挂项目 —— 没法列镜头号，但必须说清后果，不能假装无事
        out["message"] = (
            "这些改动涉及镜头指纹，而它们写在**全局层**：所有项目的已生成镜头都会变成「需重渲」。"
            "只想影响一个项目的话，把改动放到项目层再保存。"
        )
        return out

    shots_mod, Project, shot_fingerprint = _shot_engine()
    proj = Project(Path(project) if os.path.isabs(str(project)) else _resolve(str(project), root))
    shots = list(shots_mod.load_shots_dir(proj.shots_dir))
    out["total_shots"] = len(shots)

    def _frames(s, vals: dict[str, Any]) -> int:
        fps = int(vals.get("fps") or 24)
        try:
            return shots_mod.seconds_to_frames(
                getattr(s, "sec", 0) or 0, fps,
                fmin=vals.get("frame_min") or None, fmax=vals.get("frame_max") or None,
            )
        except TypeError:  # shots.py 还没带 fmin/fmax 参数的版本
            return shots_mod.seconds_to_frames(getattr(s, "sec", 0) or 0, fps)

    params_old, params_new = to_params(cur), to_params(prop)
    manifest = proj.manifest
    for s in shots:
        sid = str(getattr(s, "id", "") or "")
        chars = list(getattr(s, "chars", []) or [])
        refs = [proj.refs_dir / f"char_{c}.png" for c in chars]
        seed = int(getattr(s, "seed", 0) or 0)
        prompt = str(getattr(s, "prompt", "") or "")
        fp_old = shot_fingerprint(prompt, chars, refs, params_old, _frames(s, cur), seed)
        fp_new = shot_fingerprint(prompt, chars, refs, params_new, _frames(s, prop), seed)
        if fp_old == fp_new:
            continue
        clip = proj.clip(sid)
        if manifest.status(sid, fp_old, clip) == "current":
            out["stale_shots"].append(sid)
    out["stale_count"] = len(out["stale_shots"])
    names = "、".join(out["stale_shots"][:12]) + ("…" if out["stale_count"] > 12 else "")
    out["message"] = (
        f"改动涉及镜头指纹：{out['stale_count']} 个已生成镜头会变成「需重渲」"
        f"（{names}）—— 保存后需要重新渲染它们才会更新画面。"
        if out["stale_shots"] else
        "改动涉及镜头指纹，但当前没有已生成镜头受影响（新渲的镜头会用新参数）。"
    )
    return out


def _resolve(project: str, root: Path | None) -> Path:
    from vm import taskctl
    return taskctl.resolve_project(project, root)


# ---------------------------------------------------------------- 环境预检


def preflight(flat: dict[str, Any], *, root: Path | None = None) -> list[dict]:
    """保存前预检：ComfyUI 连通 / 模型文件在不在 / 字体找不找得到 / ffmpeg / API Key。

    只读检查 —— 不启停任何服务、不写不删任何文件（CONTRACTS 硬约束 1/2）。
    每项返回 {id, label, ok, detail}；ok=False 不阻塞保存（环境问题是提醒不是脏数据）。
    """
    v = {**effective_values(None, root), **flat}
    checks: list[dict] = []

    # 1) ComfyUI 连通（GET /system_stats，与 comfy.healthy 同口径；超时 3s 不拖 UI）
    url = str(v.get("comfy_url") or "")
    try:
        with urllib.request.urlopen(url + "/system_stats", timeout=3) as resp:
            ok = resp.status == 200
        checks.append({"id": "comfy", "label": "ComfyUI 连通", "ok": ok,
                       "detail": f"已连接 {url}" if ok else f"{url} 返回了异常状态"})
    except Exception as e:  # noqa: BLE001 —— 预检要把一切失败变成人话，不是抛栈
        checks.append({"id": "comfy", "label": "ComfyUI 连通", "ok": False,
                       "detail": f"连不上 {url}：{type(e).__name__}: {e}（ComfyUI 没启动？地址不对？）"})

    # 2) 模型文件存在性（只读）：按 ComfyUI 的目录约定逐一对照
    root_models = Path(str(v.get("models_root") or "/home/max/ComfyUI/models"))
    want = [
        ("视频主模型 unet", "unet", "diffusion_models"),
        ("渲染 LoRA", "lora", "loras"),
        ("视频 VAE", "vae_video", "vae"),
        ("音频 VAE", "vae_audio", "vae"),
        ("视频文本编码器", "clip", "text_encoders"),
        ("分镜图模型", "qi_unet", "diffusion_models"),
        ("分镜图文本编码器", "qi_clip", "text_encoders"),
        ("分镜图 VAE", "qi_vae", "vae"),
    ]
    missing = []
    for label, key, sub in want:
        name = str(v.get(key) or "")
        p = root_models / sub / name
        if not name or not p.is_file():
            missing.append(f"{label}（应放在 {root_models / sub}/{name}）")
    checks.append({
        "id": "models", "label": "模型文件", "ok": not missing,
        "detail": "8 个模型文件都在" if not missing else f"缺 {len(missing)} 个：" + "；".join(missing),
    })

    # 3) 中文字体（复用 assemble 的探测逻辑，口径必须一致 —— 否则预检说有、烧字幕时爆）
    try:
        from . import assemble as _asm
    except ImportError:  # pragma: no cover
        try:
            import assemble as _asm  # type: ignore
        except ImportError:
            _asm = None
    forced = str(v.get("subtitle_font") or "").strip()
    try:
        if _asm is None:
            raise RuntimeError("vm/assemble.py 未就绪，无法探测字体")
        cands = v.get("subtitle_font_candidates") or None
        hints = [tuple(x.split("|", 1)) for x in (v.get("subtitle_font_file_hints") or [])]
        fam = forced or _asm.detect_cjk_font(candidates=cands, hints=hints or None)
        checks.append({"id": "font", "label": "中文字体", "ok": True,
                       "detail": f"将使用字体「{fam}」" + ("（手动指定）" if forced else "（自动探测）")})
    except Exception as e:  # noqa: BLE001
        checks.append({"id": "font", "label": "中文字体", "ok": False, "detail": str(e)})

    # 4) ffmpeg / ffprobe（合成与质检都要）
    import shutil
    ff = shutil.which(os.environ.get("VM_FFMPEG", "ffmpeg"))
    fp = shutil.which(os.environ.get("VM_FFPROBE", "ffprobe"))
    checks.append({"id": "ffmpeg", "label": "ffmpeg / ffprobe", "ok": bool(ff and fp),
                   "detail": f"ffmpeg={ff or '缺失'}，ffprobe={fp or '缺失'}"
                             + ("" if (ff and fp) else "（sudo apt install ffmpeg）")})

    # 5) API Key：只报「有没有」，永远不报内容（纪律）
    key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    kf = Path("~/.config/video_magic/deepseek_key").expanduser()
    try:
        file_ok = kf.is_file() and bool(kf.read_text(encoding="utf-8").strip())
    except OSError:
        file_ok = False
    checks.append({"id": "api_key", "label": "DeepSeek API Key", "ok": bool(key) or file_ok,
                   "detail": "已配置（内容不展示）" if (key or file_ok) else API_KEY_HINT})
    return checks


# ---------------------------------------------------------------- API 载荷


def get_config(project: str | os.PathLike[str] | None = None, root: Path | None = None) -> dict:
    """GET /api/config 的响应体（不含 ok）。UI 的「继承/覆盖/一键恢复继承」全靠这里的分层值。"""
    layers = read_layers(project, root)
    eff = effective_values(project, root)
    items = []
    for it in SCHEMA:
        per = {name: layers[name].get(it.key) for name in (LAYER_ENV, LAYER_USER, LAYER_WORKSPACE, LAYER_PROJECT)}
        source = LAYER_BUILTIN
        for name in (LAYER_ENV, LAYER_USER, LAYER_WORKSPACE, LAYER_PROJECT):
            if per[name] is not None:
                source = name
        items.append({
            "key": it.key, "group": it.group, "label": it.label, "kind": it.kind,
            "value": eff.get(it.key), "default": it.default, "source": source,
            "layers": per, "overridden": per[LAYER_PROJECT] is not None,
            "scope": it.scope, "nullable": it.nullable,
            "enum": list(it.enum), "minimum": it.minimum, "maximum": it.maximum,
            "multiple": it.multiple, "placeholder": it.placeholder,
            "what": it.what, "effect": it.effect, "stale": it.stale,
            "stale_impact": it.stale_impact, "render_only": it.render_only,
            "channel": it.channel,
        })
    notes = [API_KEY_HINT]
    # 旧字段 style 与新键 style_preset 并存时要说清谁在生效（plan 层兼容读 style）
    proj_cfg = _read_json(project_config_path(project, root)) if project else {}
    if _dig_get(proj_cfg, "style") and not _dig_get(proj_cfg, "style_preset"):
        notes.append(
            f"project.json 里的旧字段 style={_dig_get(proj_cfg, 'style')!r} 仍在生效"
            f"（plan 层兼容读取）；建议统一改用「画面风格」设置，保存一次即可迁移。"
        )
    files = {
        "user": str(user_config_path()),
        "workspace": str(workspace_config_path(root)),
        "project": str(project_config_path(project, root)) if project else "",
    }
    return {
        "project": str(project or ""), "groups": list(GROUPS), "items": items,
        "layers": {k: (v if k != LAYER_BUILTIN else {}) for k, v in layers.items()},
        "files": files, "api_key_present": _api_key_present(), "notes": notes,
    }


def _api_key_present() -> bool:
    """Key 在不在（绝不返回内容）。与 taskctl.load_params 的判定口径一致。"""
    key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    try:
        kf = Path("~/.config/video_magic/deepseek_key").expanduser()
        return bool(key) or (kf.is_file() and bool(kf.read_text(encoding="utf-8").strip()))
    except OSError:
        return bool(key)


def validate_bundle(project: str | os.PathLike[str] | None = None, values: dict | None = None,
                    root: Path | None = None) -> dict:
    """POST /api/config/validate：schema 校验 + 环境预检 + 影响清单，一次拿全。

    `values` 是**提议值**（扁平点分键），叠在当前生效值上检查 —— 也就是"保存前预演"。
    """
    values = values or {}
    errors: list[dict] = []
    coerced: dict[str, Any] = {}
    layer = LAYER_PROJECT if project else LAYER_WORKSPACE
    for key, value in values.items():
        it = _BY_KEY.get(str(key))
        if it is None:
            errors.append({"key": str(key), "message": f"未知配置项「{key}」（拼错了？）"})
            continue
        try:
            coerced[str(key)] = _coerce_one(it, value)
        except ValueError as e:
            errors.append({"key": str(key), "message": str(e)})
    merged = {**effective_values(project, root), **coerced}
    impact = impact_of(coerced, project=project, root=root) if coerced else impact_of({}, project=project, root=root)
    return {
        "errors": errors,
        "checks": preflight(merged, root=root),
        "impact": impact,
        "ok": not errors,
    }


def save_config(project: str | os.PathLike[str] | None, changes: list[dict], *,
                root: Path | None = None, confirm_impact: bool = False) -> dict:
    """POST /api/config：保存一批改动。

    changes: [{key, layer, value}]，value=null 表示**从该层删除**（一键恢复继承）。
    危险项（进指纹的）改动在未 confirm_impact 时不落盘，返回 needs_confirm=True +
    影响清单 —— UI 摆出「哪些镜头会变需重渲」让人确认（先摆清单再动手，同 ResetDialog 纪律）。
    """
    if not isinstance(changes, list) or not changes:
        raise ConfigError("changes 必须是非空数组：[{key, layer, value}]", [])
    by_layer: dict[str, dict[str, Any]] = {}
    resets: dict[str, list[str]] = {}
    errors: list[dict] = []
    for i, ch in enumerate(changes):
        if not isinstance(ch, dict):
            errors.append({"key": f"changes[{i}]", "message": "每一项都要是 {key, layer, value} 对象"})
            continue
        key = str(ch.get("key") or "")
        layer = str(ch.get("layer") or "")
        if layer not in (LAYER_USER, LAYER_WORKSPACE, LAYER_PROJECT):
            errors.append({"key": key, "message": f"layer 只能是 user / workspace / project —— 当前 {layer!r}"})
            continue
        if ch.get("value", "__missing__") == "__missing__":
            errors.append({"key": key, "message": "每项都要带 value（恢复继承请传 value=null）"})
            continue
        value = ch.get("value")
        # style_preset="auto" =「不指定」= 清除本层设置（运行时没有 auto 预设，
        # 落键会被 plan 当成具体预设读走 —— 必须落成"键不存在"才真的是自动）
        if value is None or (key == "style_preset" and value == "auto"):
            resets.setdefault(layer, []).append(key)
        else:
            by_layer.setdefault(layer, {})[key] = value
    if errors:
        raise ConfigError(f"changes 格式不对（{len(errors)} 处）：" + "；".join(e["message"] for e in errors[:3]), errors)

    # 整包 schema 校验（分层做，scope 约束才判得准）；收集全部问题一次报完
    clean: dict[str, dict[str, Any]] = {}
    all_errors: list[dict] = []
    for layer, vals in by_layer.items():
        try:
            clean[layer] = coerce_values(vals, layer)
        except ConfigError as e:
            all_errors.extend(e.errors)
    for layer, keys in resets.items():
        for key in keys:
            it = _BY_KEY.get(key)
            if it is None:
                all_errors.append({"key": key, "message": f"未知配置项「{key}」，无从恢复继承"})
            elif it.scope == "project" and layer != LAYER_PROJECT:
                all_errors.append({"key": key, "message": f"「{it.label}」只存在于项目层，恢复继承请在项目层操作"})
            elif it.scope == "global" and layer == LAYER_PROJECT:
                all_errors.append({"key": key, "message": f"「{it.label}」是机器级设置，项目层没有可恢复的覆盖"})
    if all_errors:
        raise ConfigError(
            f"配置有 {len(all_errors)} 处不合法，一条都没保存：" + "；".join(e["message"] for e in all_errors[:3]),
            all_errors,
        )

    # 影响清单：按「保存后的真正生效值」算 —— **恢复继承（删键）也是改动**，
    # 它回落到下层值后帧数/指纹照样可能变，必须和改值一样过危险项确认
    layers_now = read_layers(project, root)
    for layer, vals in clean.items():
        layers_now[layer].update(vals)
    for layer, keys in resets.items():
        for key in keys:
            layers_now[layer].pop(key, None)
    new_eff = _merge_layers(layers_now)
    cur_eff = effective_values(project, root)
    touched = {k for vals in clean.values() for k in vals} | {k for ks in resets.values() for k in ks}
    proposed = {k: new_eff.get(k) for k in touched if new_eff.get(k) != cur_eff.get(k)}
    impact = impact_of(proposed, project=project, root=root)
    if impact["dangerous"] and not confirm_impact:
        return {"saved": [], "needs_confirm": True, "impact": impact}

    # 落盘：只动 schema 拥有的键，project.json 里别的内容（plan/chapter/…）一律原样保留
    written: list[str] = []
    for layer, vals in clean.items():
        path = _layer_path(layer, project, root)
        data = _read_json(path)
        for key, value in vals.items():
            _dig_set(data, key, copy.deepcopy(value))
            written.append(f"{layer}:{key}")
        _atomic_write_json(path, data)
    for layer, keys in resets.items():
        path = _layer_path(layer, project, root)
        data = _read_json(path)
        for key in keys:
            _dig_del(data, key)
            written.append(f"{layer}:{key}(清除)")
        _atomic_write_json(path, data)

    # 改完立刻装进本进程（web 进程的指纹计算马上用新值，不用重启）
    if project:
        install(project, root)
    else:
        install(None, root)
    return {"saved": written, "needs_confirm": False, "impact": impact}


def _layer_path(layer: str, project: str | os.PathLike[str] | None, root: Path | None) -> Path:
    if layer == LAYER_USER:
        return user_config_path()
    if layer == LAYER_WORKSPACE:
        return workspace_config_path(root)
    if not project:
        raise ConfigError("保存项目层配置必须给 project", [])
    return project_config_path(project, root)


# ---------------------------------------------------------------- 生效注入（A/B 通道）


# 本进程里由 apply_env 注入过的环境变量：名字 → 注入前的原值（None = 当时没有）。
# 清除配置时**恢复原值**而不是一删了之 —— 用户 shell 里自己 export 的 QI_UNET 之类
# 是比配置文件更低的一层（见 _env_layer），绝不能被我们顺手清掉。
_APPLIED_ENV: dict[str, str | None] = {}


def apply_env(explicit: dict[str, Any]) -> None:
    """B 通道：把 **文件层显式配置的** env 型设置注入当前进程环境。

    `explicit` 只含用户/工作区/项目层真正设了值的键 —— **不含内置默认**
    （默认值物化成环境变量会把「未设置」粘住），**也不含 env 层本身**
    （环境变量本来就在环境里，再"注入"一遍会让回收动作误伤用户的 export）。
    先恢复上次注入前的原值、再注入本次的：同一进程先后跑不同项目不残留。
    """
    for name, prev in list(_APPLIED_ENV.items()):
        if prev is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = prev
    _APPLIED_ENV.clear()
    for it in SCHEMA:
        if it.channel != "env" or not it.env or it.key not in explicit:
            continue
        v = explicit[it.key]
        if v is None or v == "":
            continue
        _APPLIED_ENV[it.env] = os.environ.get(it.env)
        os.environ[it.env] = str(v)


def install(project: str | os.PathLike[str] | None = None, root: Path | None = None) -> dict:
    """把配置装进当前进程，返回最终 params（= taskctl.load_params 的形状）。

    A 通道：全局层（env/用户/工作区，**不含项目层**）物化进 taskctl.DEFAULT_PARAMS ——
    load_params 深拷 DEFAULT_PARAMS 再叠 project.json，两层合并语义与手写完全一致，
    而且所有既有 load_params 调用点（web 指纹、worker 渲染）自动生效、指纹两边一致。
    项目层不物化：它本来就该由 project.json 覆盖（还有大量非 schema 键要透传）。

    B 通道：env 注入用**生效值**（含项目层）—— worker 是单项目进程，正好按项目生效；
    web 进程（project=None）只带全局层，QI_* 之类进程级设置按机器级理解（见 apply_env 注释）。
    """
    from vm import taskctl

    layers = read_layers(project, root)
    global_vals: dict[str, Any] = dict(layers[LAYER_BUILTIN])
    for name in (LAYER_ENV, LAYER_USER, LAYER_WORKSPACE):
        global_vals.update(layers[name])
    params = to_params(global_vals)
    base = taskctl.DEFAULT_PARAMS
    # 先把上次 install 写入的 schema 键清掉再写 —— 全局配置删了某项，不能残留在 DEFAULT_PARAMS
    if not hasattr(taskctl, "_CONFIG_BASE_DEFAULTS"):
        taskctl._CONFIG_BASE_DEFAULTS = copy.deepcopy(base)  # type: ignore[attr-defined]
    for it in SCHEMA:
        _dig_del(base, it.key)
    base.update(copy.deepcopy(params))
    # llm 段：DEFAULT_PARAMS 原有键（如 temperature 缺省）与全局层逐键合并（load_params 语义）
    if isinstance(params.get("llm"), dict):
        base["llm"] = {**copy.deepcopy(taskctl._CONFIG_BASE_DEFAULTS.get("llm", {})),  # type: ignore[attr-defined]
                       **params["llm"]}
    # env 通道按**生效值**注入（worker 是单项目进程，项目级 QI_*/字幕正好生效），
    # 但只注入文件层显式设置 —— 见 apply_env 注释（内置默认与 env 层都不注入）
    explicit: dict[str, Any] = {}
    for name in (LAYER_USER, LAYER_WORKSPACE, LAYER_PROJECT):
        explicit.update(layers[name])
    apply_env(explicit)
    return taskctl.load_params(project, root) if project else to_params(effective_values(None, root))


# ---------------------------------------------------------------- 消费方读取口


def subtitle_settings(project: str | os.PathLike[str] | None = None,
                      root: Path | None = None) -> dict:
    """给 vm/assemble.py：字幕字体与样式。缺省值与旧硬编码逐字节一致（不改配置零行为变化）。"""
    v = effective_values(project, root)
    hints = []
    for raw in (v.get("subtitle_font_file_hints") or []):
        if "|" in str(raw):
            p, fam = str(raw).split("|", 1)
            hints.append((p.strip(), fam.strip()))
    return {
        "font": str(v.get("subtitle_font") or "").strip(),
        "candidates": list(v.get("subtitle_font_candidates") or []) or None,
        "file_hints": hints or None,
        "font_size": int(v.get("subtitle_font_size") or 0),
        "primary_color": str(v.get("subtitle_primary_color") or "&H00FFFFFF"),
        "outline_color": str(v.get("subtitle_outline_color") or "&H00000000"),
        "outline": float(v.get("subtitle_outline") if v.get("subtitle_outline") is not None else 2),
        "margin_v": int(v.get("subtitle_margin_v") or 0),
    }


def frame_bounds() -> tuple[int, int]:
    """帧网格上下限的权威解析（vm/shots.py 自己读环境变量，这里是同一口径的函数版）。"""
    lo, hi = 56, 362
    try:
        if os.environ.get("VM_FRAME_MIN"):
            lo = int(os.environ["VM_FRAME_MIN"])
        if os.environ.get("VM_FRAME_MAX"):
            hi = int(os.environ["VM_FRAME_MAX"])
    except ValueError:
        return 56, 362
    return lo, hi


# ---------------------------------------------------------------- 自检入口


if __name__ == "__main__":  # pragma: no cover - 运维调试用
    import argparse
    import sys

    ap = argparse.ArgumentParser(prog="python3 -m vm.config", description="配置中心：看生效值 / 校验")
    ap.add_argument("project", nargs="?", help="项目名（不给=只看全局层）")
    ap.add_argument("--check", action="store_true", help="跑环境预检")
    a = ap.parse_args()
    data = get_config(a.project or None)
    print(json.dumps({i["key"]: {"value": i["value"], "source": i["source"]} for i in data["items"]},
                     ensure_ascii=False, indent=2))
    if a.check:
        for c in preflight(effective_values(a.project or None)):
            print(("✓" if c["ok"] else "✗"), c["label"], "——", c["detail"])
        sys.exit(0 if all(c["ok"] for c in preflight(effective_values(a.project or None))) else 1)
