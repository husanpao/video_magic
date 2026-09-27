"""
qi.py —— Qwen-Image 2.1 文生图客户端（分镜图的渲染引擎）。

职责只有四件事，和 comfy.py 的分工一致：**探测、拼工作流、提交等待、交回产物 URL**。
不落盘、不管镜头表、不算指纹（那是 storyboard.py 的事）——这样它也能被别的调用方复用。

为什么需要它（用户给的行业数据）：
    「制作只占总成本 7.5%」「AI 漫剧爆款率不足 0.1%、约 90% 公司亏损」
    「**废片率才是这门生意的真实杀手**」
    —— 52 镜渲一遍视频是 35 分钟 GPU。先用一张静态图审片，是最便宜的废片过滤器。

──── 三条来自本机实测的硬约束（都写在代码里，别当注释看） ────────────────

① **空 latent 按"代"选节点：两代语义不同，不能互换**
   · 2.1（照上游官方模板 `image_qwen_image_2_1_t2i.json`）：`EmptyLatentImage`（4 通道，
     输出带 `downscale_ratio_spacial: 8` 元数据，由 ComfyUI 侧适配 2.1 的 latent 格式）。
   · 2.0（本机老路径）：`EmptySD3LatentImage`（16 通道，Wan21 latent 格式）。
   实测数据（读 safetensors 头，不是猜的）：2.1 DiT `img_in.weight` = [4096, 64]；
   `TextEncodeQwenImage21` 自己还输出一个 `[1, 64, H//16, W//16]` 的**打包 latent**
   （t2i 用不上，编辑模式才用）。选错节点不是报错就是出噪声图，所以这里按 `gen` 分支。

② **尺寸必须能被 16 整除**（VAE 8× + patch 2×）。1024×576 合法（576/16=36），
   并且与成片 864×480 同为 16:9。非法尺寸在这里**显式吸附并打印**，不静默改。

③ **节点/模型缺失一律 fail-closed，给人话报错，绝不降级**
   comfy.py 的 has_node 是修正版（要求 200 且 body 非空 JSON 对象）——不存在的节点
   ComfyUI 也返回 200 + {}，只看状态码会恒真。模型文件没下完时，UNETLoader 的
   unet_name 列表里就没有它，提交只会得到一句难懂的校验错误；这里提前查列表，
   并且如果发现 `<名字>.part` 就报"还在下载中，x/y GB"。

⚠️ 历史风险（已解决）：本机 ComfyUI 原为 0.36.0（git 0d90172），**不认识 Qwen-Image 2.1
   的权重布局**（用 ComfyUI 自己的 model_detection 跑出来的结论，不是猜的）。
   2026-09-26 升级到 **v0.37.4** 后 2.1 可用：实测 1024×576 / 25 步 / cfg 1.0 **10 秒**出图。
   两代实现并存，设 `VM_QI_GEN=2.0` 可随时切回。详见文件末尾 qi_detect_risk_note。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .comfy import DEFAULT_BASE, Comfy, ComfyError

# t2i 全链路节点，**按代分开**。缺任何一个都直接报错（不换节点、不降级）。
#
# 2.0：CLIPTextEncode + EmptySD3LatentImage（16 通道）
REQUIRED_NODES_20 = (
    "UNETLoader",
    "CLIPLoader",
    "VAELoader",
    "CLIPTextEncode",
    "EmptySD3LatentImage",
    "KSampler",
    "VAEDecode",
    "SaveImage",
)

# 2.1：TextEncodeQwenImage21（自带正/负 conditioning）+ EmptyLatentImage（4 通道）
# 需 ComfyUI >= 0.37.0（PR #16400 引入 2.1 接线）。
REQUIRED_NODES_21 = (
    "UNETLoader",
    "CLIPLoader",
    "VAELoader",
    "TextEncodeQwenImage21",
    "EmptyLatentImage",
    "KSampler",
    "VAEDecode",
    "SaveImage",
)

# 向后兼容：老调用方/测试引用的名字 = 2.0 那套。
REQUIRED_NODES = REQUIRED_NODES_20

# 可选节点：只有显式配置时才需要它，届时同样 fail-closed。
CONDITIONAL_NODES = ("ModelSamplingAuraFlow",)          # 2.0：设 aura_shift 时才用
CONDITIONAL_NODES_21 = ("QwenImage21Cache",)             # 2.1：KV 缓存调优（默认带上）

CLIP_LOADER_TYPE = "qwen_image"  # 两代的 CLIPLoader type 都是它（官方 2.1 模板实测）

# ── 用哪一代 Qwen-Image：`gen` 是显式开关，不靠猜文件名 ──────────────────────
# 背景（2026-09-23 实测 → 2026-09-26 升级）：本机 ComfyUI 原先停在 0.36.0（git 0d90172），
# **不认识 2.1 的权重布局**（2.1 用 txt_in.text_norm/in_layer/out_layer，0.36.0 只认
# txt_norm + 单个 txt_in.weight）→ `RuntimeError: Could not detect model type`。
# 0.37.0 起有独立实现（comfy/ldm/qwen_image21/、QwenImage21 in supported_models.py），
# 所以升级 ComfyUI 到 >= 0.37.0 后即可用 2.1；两代实现**并存**，可随时切回。
GEN_20 = "2.0"
GEN_21 = "2.1"
GENS = (GEN_20, GEN_21)

# 2.0 权重（ComfyUI 0.36 起可跑）
D20_UNET = "qwen_image_nvfp4.safetensors"
D20_CLIP = "qwen_2.5_vl_7b_nvfp4.safetensors"
D20_VAE = "qwen_image_vae.safetensors"
# 2.1 权重（需 ComfyUI >= 0.37.0）。注意 2.1 的主文本编码器是 **qwen3vl_8b**，
# 另一个 `qwen3.5_9b_..._pe_t2i` 是"提示词增强器"，可选、不在本路径上。
D21_UNET = "qwen_image_2.1_int8_convrot.safetensors"
D21_CLIP = "qwen3vl_8b_int8_convrot.safetensors"
D21_VAE = "qwen_image_2.1_vae_bf16.safetensors"

GEN_MODELS = {
    GEN_20: {"unet": D20_UNET, "clip": D20_CLIP, "vae": D20_VAE},
    GEN_21: {"unet": D21_UNET, "clip": D21_CLIP, "vae": D21_VAE},
}

# 官方采样口径不同：2.0 是 20 步 / cfg 2.5（要负向词）；2.1 官方路径 cfg=1
# （模板起点 25 步，官方说 40-50 步更足）。cfg=1 时负向词不生效。
GEN_SAMPLING = {
    GEN_20: {"steps": 20, "cfg": 2.5},
    GEN_21: {"steps": 25, "cfg": 1.0},
}

# 默认"代"：由 VM_QI_GEN 决定。**2026-09-26 起缺省 2.1**（本机 ComfyUI 已升到 0.37.4、
# 2.1 三件套齐备，实测 1024×576 / 25 步 10 秒出图）。要回老路径：设 VM_QI_GEN=2.0。
DEFAULT_GEN = (os.environ.get("VM_QI_GEN") or GEN_21).strip() or GEN_21

# 兼容保留：默认代次的三个文件名。字段留空时由 GEN_MODELS 自动补（见 QIConfig.__post_init__），
# 所以这三个常量是"给人、给老代码看的默认值"，不是唯一真相。
DEFAULT_UNET = os.environ.get("QI_UNET") or GEN_MODELS[DEFAULT_GEN]["unet"]
DEFAULT_CLIP = os.environ.get("QI_CLIP") or GEN_MODELS[DEFAULT_GEN]["clip"]
DEFAULT_VAE = os.environ.get("QI_VAE") or GEN_MODELS[DEFAULT_GEN]["vae"]

# ComfyUI 的 models 根目录。只用于"文件在不在、下到多少了"的**只读**检查，
# 不写、不动、不删任何东西。
DEFAULT_MODELS_ROOT = os.environ.get("COMFYUI_MODELS_ROOT", "/home/max/ComfyUI/models")

# 尺寸对齐粒度：VAE 8× × patch 2×（与 EmptySD3LatentImage 的 step=16 一致）
SIZE_MULTIPLE = 16

DEFAULT_WIDTH = 1024
DEFAULT_HEIGHT = 576

# ── 采样默认值（Qwen-Image 官方 t2i 口径）──────────────────────────────────
# steps/cfg：Qwen-Image 系官方工作流是 20 步 / cfg 2.5（不是 SD 的 20/7）。
# sampler/scheduler：euler + simple。
DEFAULT_STEPS = 20
DEFAULT_CFG = 2.5
DEFAULT_SAMPLER = "euler"
DEFAULT_SCHEDULER = "simple"

MODEL_SUBDIR = {
    "unet_name": "diffusion_models",
    "clip_name": "text_encoders",
    "vae_name": "vae",
}


# ── 配置 ────────────────────────────────────────────────────────────────────


@dataclass
class QIConfig:
    """一次文生图所需的全部参数。指纹直接由它派生，所以字段要克制。"""

    # default_factory 而不是直接赋值：dataclass 的**环境相关**默认值在类定义时就固定了，
    # 那样 QI_UNET 之类环境变量在 import 之后设置就不生效。
    # 留空 = 跟随 gen 从 GEN_MODELS 自动选 —— 用户只要改「代次」一个开关，
    # 不必记住两代三个文件名的对应关系；显式设了 QI_UNET 就优先用它。
    unet_name: str = field(default_factory=lambda: os.environ.get("QI_UNET") or "")
    clip_name: str = field(default_factory=lambda: os.environ.get("QI_CLIP") or "")
    vae_name: str = field(default_factory=lambda: os.environ.get("QI_VAE") or "")
    clip_type: str = CLIP_LOADER_TYPE
    weight_dtype: str = "default"

    # 用哪一代（GEN_20 / GEN_21）。显式字段，**不从文件名猜**——猜错会出噪声图。
    gen: str = field(default_factory=lambda: (os.environ.get("VM_QI_GEN") or DEFAULT_GEN).strip() or GEN_20)

    width: int = DEFAULT_WIDTH
    height: int = DEFAULT_HEIGHT
    # steps/cfg 留 None ⇒ 按 gen 取官方口径（2.0=20/2.5，2.1=25/1.0，见 GEN_SAMPLING）。
    # 这样"换代"自动带上正确的采样参数，而显式传值仍然优先。
    steps: int | None = None
    cfg: float | None = None
    sampler: str = DEFAULT_SAMPLER
    scheduler: str = DEFAULT_SCHEDULER
    denoise: float = 1.0
    batch_size: int = 1

    # aura_shift=None ⇒ 不加 ModelSamplingAuraFlow，用模型自带的 shift
    # （supported_models.QwenImage.sampling_settings = {"shift": 1.15}）。2.0 专用。
    aura_shift: float | None = None

    # 2.1 专用：QwenImage21Cache（KV 缓存存放位置/精度）。
    # device 不影响画面（auto=用显存余量再退内存）；dtype 会轻微影响结果，故进指纹。
    use_cache: bool = True
    cache_device: str = field(default_factory=lambda: os.environ.get("VM_QI_CACHE_DEVICE", "auto"))
    cache_dtype: str = field(default_factory=lambda: os.environ.get("VM_QI_CACHE_DTYPE", "default"))

    # 2.1 的 TextEncodeQwenImage21 有个 resolution 参数，**只影响参考图缩放**；
    # 本路径(t2i)没有参考图，所以它不影响出图，也没必要进指纹。
    ref_resolution: int = 1024

    models_root: str = DEFAULT_MODELS_ROOT

    def __post_init__(self) -> None:
        """把 None 的 steps/cfg 落成该代的官方口径；空模型名按代补齐；gen 非法直接报错。"""
        if self.gen not in GEN_MODELS:
            raise ValueError(f"未知的 Qwen-Image 代：{self.gen!r}（可选 {'/'.join(GENS)}）")
        d = GEN_SAMPLING[self.gen]
        if self.steps is None:
            self.steps = int(d["steps"])
        if self.cfg is None:
            self.cfg = float(d["cfg"])
        models = GEN_MODELS[self.gen]
        if not self.unet_name:
            self.unet_name = models["unet"]
        if not self.clip_name:
            self.clip_name = models["clip"]
        if not self.vae_name:
            self.vae_name = models["vae"]

    def size(self) -> tuple[int, int]:
        return self.width, self.height

    def render_dict(self) -> dict:
        """参与指纹的渲染参数子集（不含端口/路径这类与画面无关的东西）。"""
        return {
            # ★ gen 必须进指纹：2.0 ↔ 2.1 换的是模型与节点，画面必然变，
            #   不进指纹就会出现"切了代但图还判 current"的静默错。
            "gen": self.gen,
            "unet": self.unet_name,
            "clip": self.clip_name,
            "vae": self.vae_name,
            "clip_type": self.clip_type,
            "weight_dtype": self.weight_dtype,
            "width": self.width,
            "height": self.height,
            "steps": self.steps,
            "cfg": self.cfg,
            "sampler": self.sampler,
            "scheduler": self.scheduler,
            "denoise": self.denoise,
            "batch_size": self.batch_size,
            "aura_shift": self.aura_shift,
            "cache_dtype": self.cache_dtype if self.gen == GEN_21 else None,
        }


def snap_size(width: int, height: int, multiple: int = SIZE_MULTIPLE) -> tuple[int, int, str]:
    """
    把尺寸吸到 `multiple` 的整数倍（向下取整，且不小于 multiple）。

    返回 (w, h, note)。note 为空表示没改过；非空时调用方**必须把它打印出来** ——
    "请求 1000×570 实际渲 992×560"这种事，静默发生就是坑。
    """
    def _snap(v: int) -> int:
        v = int(v)
        return max(multiple, (v // multiple) * multiple)

    w, h = _snap(width), _snap(height)
    if (w, h) == (int(width), int(height)):
        return w, h, ""
    return w, h, f"尺寸 {width}×{height} 不是 {multiple} 的整数倍，已吸附为 {w}×{h}"


# ── 指纹 ────────────────────────────────────────────────────────────────────


def text_fingerprint(text: str) -> str:
    """任意文本的短指纹（md5 前 16 位）。storyboard 用它记录"当时的提示词"。"""
    return hashlib.md5((text or "").encode("utf-8")).hexdigest()[:16]


def qi_fingerprint(prompt: str, negative: str, render: dict, seed: int) -> str:
    """
    一张分镜图的输入指纹：提示词 + 负向词 + 渲染参数 + 种子。

    字段顺序固定（dict 用 sort_keys 序列化），保证同输入同输出。
    任何一项变了 → 指纹变 → storyboard 判 stale → 重出图。
    """
    parts = [
        "qi-v1",
        "prompt=" + (prompt or ""),
        "negative=" + (negative or ""),
        "render=" + json.dumps(render, sort_keys=True, ensure_ascii=False),
        "seed=" + str(seed),
    ]
    return hashlib.md5("\n".join(parts).encode("utf-8")).hexdigest()[:16]


# ── 探测报告 ────────────────────────────────────────────────────────────────


@dataclass
class Probe:
    """preflight 的结果。给人看的，不是给机器判断的（机器判断看 problems）。"""

    healthy: bool = False
    nodes_present: list[str] = field(default_factory=list)
    nodes_missing: list[str] = field(default_factory=list)
    model_choices: dict[str, list[str]] = field(default_factory=dict)
    models_missing: list[str] = field(default_factory=list)
    partial: dict[str, str] = field(default_factory=dict)  # 名字 → "1.5/6.8 GB 下载中"
    problems: list[str] = field(default_factory=list)

    def report(self) -> str:
        lines = [f"ComfyUI: {'在线' if self.healthy else '无响应'}"]
        if self.nodes_present:
            lines.append("必需节点: " + ", ".join(self.nodes_present))
        if self.nodes_missing:
            lines.append("❌ 缺节点: " + ", ".join(self.nodes_missing))
        for kind, names in self.model_choices.items():
            lines.append(f"可选 {kind}（{len(names)} 个）: " + (", ".join(names) if names else "(空)"))
        if self.partial:
            lines.append("⏳ 正在下载: " + "; ".join(f"{k} {v}" for k, v in self.partial.items()))
        if self.models_missing:
            lines.append("❌ 缺模型文件: " + ", ".join(self.models_missing))
        if self.problems:
            lines.append("问题清单:")
            lines += [f"  - {p}" for p in self.problems]
        return "\n".join(lines)


def _human_gb(n: int) -> str:
    return f"{n / 1e9:.2f} GB"


class QwenImage:
    """Qwen-Image 2.1 的 t2i 客户端。**只提交，不启停 ComfyUI、不动它的 output/**。"""

    def __init__(
        self,
        cfg: QIConfig | None = None,
        base_url: str = DEFAULT_BASE,
        comfy: Comfy | None = None,
    ):
        self.cfg = cfg or QIConfig()
        self.comfy = comfy or Comfy(base_url)

    # ---------- 探测 ----------

    def probe(self, *, deep: bool = True) -> Probe:
        """
        fail-closed 的前置检查。deep=True 时还要查三个模型文件在不在 ComfyUI 的列表里
        （这是**只读**的 /object_info 查询）。
        """
        p = Probe()
        p.healthy = self.comfy.healthy()
        if not p.healthy:
            p.problems.append(
                f"ComfyUI 无响应（{self.comfy.base}）。本流水线**不负责启停它**（常驻共享服务），"
                "请先确认它已启动。"
            )
            return p

        req, cond = self._node_sets()
        for name in tuple(req) + tuple(cond):
            if self.comfy.has_node(name):
                p.nodes_present.append(name)
            elif name in req:
                p.nodes_missing.append(name)
        if p.nodes_missing:
            hint = ""
            if self.cfg.gen == GEN_21:
                hint = (
                    f"（Qwen-Image {GEN_21} 需要 ComfyUI >= 0.37.0：升级后 "
                    "/object_info/TextEncodeQwenImage21 应返回非空 schema；"
                    f"若暂时升不了，可设 VM_QI_GEN={GEN_20} 切回老路径。）"
                )
            p.problems.append(
                "ComfyUI 缺少必需节点："
                + ", ".join(p.nodes_missing)
                + "。不要降级换节点 —— 缺节点说明本机 ComfyUI 版本与目标工作流不匹配，"
                "需要先对齐版本。" + hint
            )

        if not deep:
            return p

        want = {
            "unet_name": self.cfg.unet_name,
            "clip_name": self.cfg.clip_name,
            "vae_name": self.cfg.vae_name,
        }
        for kind, name in want.items():
            try:
                choices = self.comfy.object_list(kind)
            except ComfyError as e:
                p.problems.append(f"读不到 {kind} 列表：{e}")
                continue
            p.model_choices[kind] = choices
            if name in choices:
                continue
            p.models_missing.append(name)
            note = self._partial_note(kind, name)
            if note:
                p.partial[name] = note
                p.problems.append(
                    f"{kind} 的 {name} 还在下载中（{note}），ComfyUI 看不到它。等下载完再跑。"
                )
            else:
                p.problems.append(
                    f"{kind} 的 {name} 不在 ComfyUI 的模型列表里。"
                    f"应放到 {Path(self.cfg.models_root) / MODEL_SUBDIR[kind]}/ 下。"
                )
        return p

    def _partial_note(self, kind: str, name: str) -> str:
        """
        若同名 .part 存在，报"下到多少了"。这是**只读** stat，不碰文件。

        为什么值得写：模型 16.2GB / 5.3MB/s，要下 50 分钟。没有这行提示，
        操作者只会看到"模型不在列表里"这种没有信息量的报错。
        """
        sub = MODEL_SUBDIR.get(kind)
        if not sub:
            return ""
        part = Path(self.cfg.models_root) / sub / (name + ".part")
        try:
            got = part.stat().st_size
        except OSError:
            return ""
        return f"{_human_gb(got)} 下载中"

    def require_ready(self) -> Probe:
        """探测不通过就抛 ComfyError（消息里带全部问题）。通过则返回 Probe。"""
        p = self.probe()
        if p.problems:
            raise ComfyError("Qwen-Image 前置检查未通过：\n  - " + "\n  - ".join(p.problems))
        return p

    # ---------- 工作流 ----------

    def _node_sets(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """按代给出（必需节点, 可选节点）。只有 2.1 带 QwenImage21Cache。"""
        if self.cfg.gen == GEN_21:
            return REQUIRED_NODES_21, (CONDITIONAL_NODES_21 if self.cfg.use_cache else ())
        return REQUIRED_NODES_20, CONDITIONAL_NODES

    def build(
        self,
        prompt: str,
        *,
        negative: str = "",
        width: int | None = None,
        height: int | None = None,
        seed: int = 0,
        filename_prefix: str = "VM_QI",
        extra_note: Callable[[str], None] | None = None,
    ) -> dict:
        """
        拼 t2i 工作流（纯代码，不用模板 JSON）。按 cfg.gen 分两代：

        2.0：1 UNETLoader ─(可选 ModelSamplingAuraFlow)─┐
             2 CLIPLoader ─ 5 正 / 6 负 CLIPTextEncode   │
             7 EmptySD3LatentImage(16ch) ────────────────┴─ 8 KSampler ─ 9 VAEDecode ─ 10 SaveImage

        2.1：1 UNETLoader ─ 11 QwenImage21Cache ─┐
             2 CLIPLoader ─ 5 TextEncodeQwenImage21（正/负两个 conditioning 输出）
             7 EmptyLatentImage(4ch) ─────────────┴─ 8 KSampler ─ 9 VAEDecode ─ 10 SaveImage
             （节点与连线照上游官方模板 image_qwen_image_2_1_t2i.json，不自行发明）
        """
        c = self.cfg
        w = c.width if width is None else int(width)
        h = c.height if height is None else int(height)
        w, h, note = snap_size(w, h)
        if note and extra_note:
            extra_note(note)
        if not (prompt or "").strip():
            raise ComfyError("提示词为空，拒绝提交（空提示词出的是随机噪声，纯烧 GPU）")

        if c.gen == GEN_21:
            return self._build_21(prompt, negative=negative, w=w, h=h, seed=seed,
                                  filename_prefix=filename_prefix)
        return self._build_20(prompt, negative=negative, w=w, h=h, seed=seed,
                              filename_prefix=filename_prefix)

    def _build_20(self, prompt: str, *, negative: str, w: int, h: int, seed: int,
                  filename_prefix: str) -> dict:
        """老 Qwen-Image 2.0 图（本机 0.36 起就能跑，升级后仍保留以便随时切回）。"""
        c = self.cfg
        g: dict[str, dict] = {
            "1": {
                "class_type": "UNETLoader",
                "inputs": {"unet_name": c.unet_name, "weight_dtype": c.weight_dtype},
            },
            "2": {
                "class_type": "CLIPLoader",
                "inputs": {"clip_name": c.clip_name, "type": c.clip_type, "device": "default"},
            },
            "3": {"class_type": "VAELoader", "inputs": {"vae_name": c.vae_name}},
            "5": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
            "6": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": negative or "", "clip": ["2", 0]},
            },
            # ★ EmptySD3LatentImage（16 通道），不是 EmptyLatentImage（4 通道）——见文件头 ①
            "7": {
                "class_type": "EmptySD3LatentImage",
                "inputs": {"width": w, "height": h, "batch_size": int(c.batch_size)},
            },
            "8": {
                "class_type": "KSampler",
                "inputs": {
                    "seed": int(seed),
                    "steps": int(c.steps),
                    "cfg": float(c.cfg),
                    "sampler_name": c.sampler,
                    "scheduler": c.scheduler,
                    "denoise": float(c.denoise),
                    "model": ["1", 0],
                    "positive": ["5", 0],
                    "negative": ["6", 0],
                    "latent_image": ["7", 0],
                },
            },
            "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
            "10": {
                "class_type": "SaveImage",
                "inputs": {"filename_prefix": filename_prefix, "images": ["9", 0]},
            },
        }

        if c.aura_shift is not None:
            g["4"] = {
                "class_type": "ModelSamplingAuraFlow",
                "inputs": {"model": ["1", 0], "shift": float(c.aura_shift)},
            }
            g["8"]["inputs"]["model"] = ["4", 0]
        return g

    def _build_21(self, prompt: str, *, negative: str, w: int, h: int, seed: int,
                  filename_prefix: str) -> dict:
        """
        Qwen-Image 2.1 图。要点（全部对着官方模板核对过）：
        · 文本编码换成 `TextEncodeQwenImage21`：**它同时给出 positive/negative**，
          并且自带第三个 latent 输出（编辑模式用；t2i 走 EmptyLatentImage）
        · latent 用 `EmptyLatentImage`（4 通道），**不是** 2.0 的 EmptySD3LatentImage
        · 官方口径 cfg=1（此时 negative 不生效）、euler/simple；
        · `QwenImage21Cache` 是 KV 缓存调优节点（device 只影响放哪、dtype 会轻微影响画面）
        """
        c = self.cfg
        g: dict[str, dict] = {
            "1": {
                "class_type": "UNETLoader",
                "inputs": {"unet_name": c.unet_name, "weight_dtype": c.weight_dtype},
            },
            "2": {
                "class_type": "CLIPLoader",
                "inputs": {"clip_name": c.clip_name, "type": c.clip_type, "device": "default"},
            },
            "3": {"class_type": "VAELoader", "inputs": {"vae_name": c.vae_name}},
            "5": {
                "class_type": "TextEncodeQwenImage21",
                "inputs": {
                    "clip": ["2", 0],
                    "prompt": prompt,
                    "negative_prompt": negative or "",
                    # 只影响参考图缩放；本路径无参考图，故不影响出图
                    "resolution": int(c.ref_resolution),
                },
            },
            "7": {
                "class_type": "EmptyLatentImage",
                "inputs": {"width": w, "height": h, "batch_size": int(c.batch_size)},
            },
            "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
            "10": {
                "class_type": "SaveImage",
                "inputs": {"filename_prefix": filename_prefix, "images": ["9", 0]},
            },
        }

        model_ref: list = ["1", 0]
        if c.use_cache:
            g["11"] = {
                "class_type": "QwenImage21Cache",
                "inputs": {"model": ["1", 0], "device": c.cache_device, "dtype": c.cache_dtype},
            }
            model_ref = ["11", 0]

        g["8"] = {
            "class_type": "KSampler",
            "inputs": {
                "seed": int(seed),
                "steps": int(c.steps),
                "cfg": float(c.cfg),
                "sampler_name": c.sampler,
                "scheduler": c.scheduler,
                "denoise": float(c.denoise),
                "model": model_ref,
                "positive": ["5", 0],
                "negative": ["5", 1],
                "latent_image": ["7", 0],
            },
        }
        return g

    # ---------- 提交 / 等待 ----------

    def generate(
        self,
        prompt: str,
        *,
        negative: str = "",
        width: int | None = None,
        height: int | None = None,
        seed: int = 0,
        filename_prefix: str = "VM_QI",
        timeout: int = 1800,
        on_tick: Callable[[float, int, int], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        log: Callable[[str], None] = print,
    ) -> "QIResult":
        """
        提交并等到出图。返回 QIResult（含 prompt_id、耗时、产物清单）。

        **不落盘**：产物由调用方经 comfy.download() 取回，这样 storyboard 能决定放哪。
        """
        w = self.cfg.width if width is None else int(width)
        h = self.cfg.height if height is None else int(height)
        wf = self.build(
            prompt, negative=negative, width=w, height=h, seed=seed,
            filename_prefix=filename_prefix, extra_note=log,
        )
        t0 = time.time()
        prompt_id = self.comfy.submit(wf)
        entry = self.comfy.wait(prompt_id, timeout=timeout, on_tick=on_tick, should_stop=should_stop)
        elapsed = time.time() - t0
        images = [it for it in self.comfy.outputs_of(entry) if it["filename"].lower().endswith(".png")]
        if not images:
            raise ComfyError(
                f"任务完成但没有 PNG 产物：prompt_id={prompt_id} outputs={entry.get('outputs')}"
            )
        return QIResult(
            prompt_id=prompt_id,
            seconds=elapsed,
            images=images,
            width=w,
            height=h,
            seed=int(seed),
            entry=entry,
        )


@dataclass
class QIResult:
    prompt_id: str
    seconds: float
    images: list[dict]
    width: int
    height: int
    seed: int
    entry: dict = field(default_factory=dict, repr=False)


# ── 已知风险备忘（给下一位工程师）─────────────────────────────────────────────
QI_DETECT_RISK_NOTE = """\
【已解决 · 留作历史】ComfyUI 0.36.0（git 0d90172，2026-09-19）对 Qwen-Image **2.1** 权重布局：

用 ComfyUI 自己的 comfy.model_detection.detect_unet_config() 跑 2.1 权重的真实
tensor 名/形状（meta tensor，不加载权重）得到 null（不认识），而老 Qwen-Image 布局
（对照组）正常识别。原因：0.36.0 的判定条件是 `txt_norm.weight` 存在，2.1 改成了
`txt_in.text_norm.weight`，文本投影也从单层 `txt_in.weight` 换成两层
`txt_in.in_layer.weight` + `txt_in.out_layer.weight`。
→ 加载时走 comfy/sd.py 的 `RuntimeError: ERROR: Could not detect model type of: ...`

**解法（2026-09-26 执行）**：把 /home/max/ComfyUI 升到 **v0.37.4**（2.1 支持自 v0.37.0
的 PR #16400 起），它带独立实现：
    comfy/ldm/qwen_image21/model.py     QwenImage21Transformer2DModel
    comfy/model_base.py                 class QwenImage21(QwenImage)
    comfy/supported_models.py           class QwenImage21 / "image_model": "qwen_image_2.1"
    comfy/text_encoders/qwen_image21.py QwenImage21Tokenizer
    comfy_extras/nodes_qwen.py          TextEncodeQwenImage21 / QwenImage21Cache
2.0 与 H3（MiniMax）实现同时保留，因此换代是**可逆**的（改 VM_QI_GEN 即可切回）。
"""
