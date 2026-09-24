"""
storyboard.py —— 分镜图：渲染前审片的静态关键帧。

为什么要它（用户给的行业数据）：
    制作只占总成本 7.5%，投流占 70-85% ⇒ "更便宜地出片"价值天花板极低；
    AI 漫剧爆款率不足 0.1%、约 90% 公司亏损 ⇒ **废片率才是真实杀手**。
    52 镜渲一遍视频 = 35 分钟 GPU。先用一张静态图看构图/光影/畸形，
    比渲完视频再发现"这镜没法用"便宜两个数量级。

我们已有的 B 档审计只查**结构**（对话覆盖度/节奏/时长），查不了**画面**。
这个模块补的就是那一段：把镜头表里的画面描述喂给 Qwen-Image，逐镜出图、
按提示词指纹缓存、落盘并记索引。

产物约定（与 state.py 的幂等思路一致）：
    projects/<项目>/storyboard/<镜头id>.png     一张图一个文件
    projects/<项目>/state/storyboard.json       索引（含 prompt 指纹 + 渲染指纹）
三态：missing（没图）/ stale（改了提示词或参数）/ current（可跳过）。
`--force` 强制重出。

提示词口径（**不重新调 LLM**）：
    六段式 prompt 的 `detailed_description` 就是"要画的画面"。
    喂给 Qwen-Image 之前剥掉两类只对 H3 有意义的东西：
      ① `<d>台词</d>` —— 台词是给 H3 原生音频的。把它喂给图像模型，
         轻则浪费 token，重则触发"AI 把台词画成画面里的乱码字"（见统一方案 A4）。
      ② CAMERA / POSITION DISCIPLINE 行 —— 那是给 H3 的运镜/站位纪律，
         对一张静态帧没有意义（而且它们本来就排在段外，靠正则兜住位置漂移）。

⚠️ 一条必须写下来的**能力边界**：本模块是 t2i（纯文生图），**不含参考图**。
   所以出图**判不了"这个角色还是不是这个角色"**（脸/服装一致性靠 H3 的
   Ref2VA + 定妆照）。它能审的是：构图、景别、光影、场景、肢体/结构畸形、
   风格一致性。要审角色一致性，得另做 Qwen-Image-Edit 的参考图路径。

不做的事（和 comfy.py 同一纪律）：不启停/更新 ComfyUI、不调 /free、
不清队列、不调 /interrupt、不动 ComfyUI output/ 下的文件。
Ctrl-C 只是**本地停止等待**：已经提交的任务留在用户的队列里继续跑，
我们绝不用 /interrupt 去打断共享服务上的其他任务。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .comfy import Comfy, ComfyError
from .qi import (
    DEFAULT_BASE,
    SIZE_MULTIPLE,
    QIConfig,
    QIResult,
    QwenImage,
    qi_fingerprint,
    snap_size,
    text_fingerprint,
)
from .shots import DISCIPLINE_PREFIXES, Shot, load_shots_dir
from .state import CURRENT, MISSING, STALE

INDEX_VERSION = 1
STORYBOARD_DIRNAME = "storyboard"
INDEX_FILENAME = "storyboard.json"

# 六段式里"画面"那一段；单段式表用 integrated_multimodal_description
PRIMARY_SECTIONS = ("detailed_description", "integrated_multimodal_description")

# 段名（小写字母+下划线，行首锚定）与纪律行（大写标签，行首锚定）。
# 与 shots.py 的解析口径保持一致：纪律行不是"段"，它寄生在它后面那一段的正文里。
_HEADER_RE = re.compile(r"(?m)^[ \t]*([a-z][a-z_]{2,})[ \t]*:")
# 纪律行：**不锚行首**。官方注入器把它们放在段尾/段标题前，正常都在行首；
# 但合成用例与手工改过的提示词里会出现"A rainy alley. CAMERA DISCIPLINE: …"这种
# 同行粘连（自测抓到的），所以从标签一直吃到行尾，位置无关。
_DISC_LINE_RE = re.compile(
    r"[ \t]*(?:"
    + "|".join(re.escape(p) for p in DISCIPLINE_PREFIXES)
    + r")[ \t]*:[^\n]*"
)
_UPPER_LABEL_RE = re.compile(r"(?m)^[ \t]*([A-Z][A-Z ]{3,}?)[ \t]*:")
_D_TAG_RE = re.compile(r"<d>.*?</d>", re.S | re.I)
# 单段式/旧表里可能出现的 H3 参考图占位符
_PICTURE_REF_RE = re.compile(r"<Picture\s+\d+>", re.I)
_WS_RE = re.compile(r"[ \t]{2,}")


# ── 提示词清洗 ──────────────────────────────────────────────────────────────


def split_sections(prompt: str) -> dict[str, str]:
    """把提示词切成 {段名: 段正文}。纪律行天然落进它前面那一段的正文里。"""
    text = prompt or ""
    marks = [(m.start(), m.group(1)) for m in _HEADER_RE.finditer(text)]
    out: dict[str, str] = {}
    for i, (pos, name) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        body = text[pos:end]
        out[name] = (body.split(":", 1)[1] if ":" in body else "").strip()
    return out


def _clean_body(body: str) -> tuple[str, dict]:
    """剥 `<d>` 与纪律行，返回 (干净文本, 统计)。统计要进索引，方便回溯"当时剥掉了什么"。"""
    stats = {"dialogue": 0, "discipline_lines": 0, "extra_upper_labels": []}

    def _drop_d(m: re.Match) -> str:
        stats["dialogue"] += 1
        return ""

    text = _D_TAG_RE.sub(_drop_d, body or "")

    def _drop_disc(m: re.Match) -> str:
        stats["discipline_lines"] += 1
        return ""

    text = _DISC_LINE_RE.sub(_drop_disc, text)

    # 其它"全大写标签行"：**不剥**，只记下来给人看（宁可留着也不要误删正文）
    for m in _UPPER_LABEL_RE.finditer(text):
        label = m.group(1).strip()
        if label and label not in DISCIPLINE_PREFIXES and label not in stats["extra_upper_labels"]:
            stats["extra_upper_labels"].append(label)

    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    text = re.sub(r"\n{2,}", "\n", text).strip()
    return text, stats


# subject_definitions 的格式：
#   `<Subject 1> = a Chinese Buddhist monk ... shown in <Picture 1>: Refined pale face, ... <Picture 1> supplies the identity and appearance of <Subject 1>.`
# 外貌描述**已经在这段里**，所以 t2i 路径不必再去读角色卡 —— 直接解出来替换代号即可。
_SUBJECT_HEAD_RE = re.compile(r"<Subject\s+(\d+)>\s*=\s*", re.I)


def subject_appearance_map(subject_defs: str) -> dict[str, str]:
    """
    从 subject_definitions 解出 `<Subject N>` → 外貌描述。

    为什么要做：`detailed_description` 里写的是 `<Subject 1> (S1) stands in the left foreground`。
    这是 **H3 的参考图引用语法** —— t2i 路径没有参考图，模型看到 `<Subject 1>` 只会把它
    **当字面文字画进画面**（或忽略主体、画成无名路人）。所以必须换成真实外貌描述。
    """
    text = _PICTURE_REF_RE.sub(" ", subject_defs or "")
    heads = list(_SUBJECT_HEAD_RE.finditer(text))
    out: dict[str, str] = {}
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        body = text[m.end():end]
        # 去掉"参考图供身份"这类句子（对 t2i 无意义）
        body = re.sub(r"[^.]*supplies the identity and appearance[^.]*\.", " ", body, flags=re.I)
        body = re.sub(r"[^.]*reference still[^.]*\.", " ", body, flags=re.I)
        # 删掉 <Picture N> 后会留下 "shown in :" / "shown in ," 这类残渣，清掉
        body = re.sub(r"\bshown in\s*[:,]?\s*", " ", body, flags=re.I)
        body = re.sub(r"\s*:\s*", ": ", body)
        body = re.sub(r"\s{2,}", " ", body)
        body = _WS_RE.sub(" ", body).strip().strip(":").strip()
        if body:
            out[m.group(1)] = body
    return out


def resolve_subjects(text: str, subject_defs: str) -> tuple[str, int]:
    """
    把画面段里的 `<Subject N>` / `<Subject N> (SN)` 换成**外貌描述**。返回 (新文本, 替换次数)。

    换完仍是空（没解出外貌）时保留原样 —— 但调用方会因此记一条 warning，
    不会静默地把代号喂给模型。
    """
    amap = subject_appearance_map(subject_defs)
    if not amap or not text:
        return text, 0
    n = 0

    def _sub(m: re.Match[str]) -> str:
        nonlocal n
        body = amap.get(m.group(1))
        if not body:
            return m.group(0)
        n += 1
        return body

    # 连 `(SN)` 尾巴一起去掉 —— 留着会被当成画面里的字幕
    out = re.sub(r"<Subject\s+(\d+)>\s*(?:\(S\d+\))?", _sub, text, flags=re.I)
    return out, n


def _subject_defs_for_image(body: str) -> str:
    """把 subject_definitions 改成 t2i 能用的形态：去掉 <Picture N> 与"参考图供身份"的句子。"""
    text = _PICTURE_REF_RE.sub("", body or "")
    text = re.sub(r"[^.]*supplies the identity and appearance[^.]*\.", " ", text, flags=re.I)
    text = re.sub(r"[^.]*reference still[^.]*\.", " ", text, flags=re.I)
    text = _WS_RE.sub(" ", text)
    return re.sub(r"\s*\n\s*", " ", text).strip()


def image_prompt_of(prompt: str, *, with_subject_defs: bool = False) -> tuple[str, dict]:
    """
    镜头 prompt → 喂给 Qwen-Image 的画面提示词。返回 (文本, 说明)。

    说明（info）会进索引：用了哪一段、剥掉了什么、有没有走容忍分支、有什么警告。
    宁可多记一行，也不要让"图跟提示词对不上"变成不可追溯的事。
    """
    sections = split_sections(prompt or "")
    info: dict[str, Any] = {
        "sections": list(sections.keys()),
        "section_used": "",
        "stripped": {"dialogue": 0, "discipline_lines": 0},
        "extra_upper_labels": [],
        "with_subject_defs": bool(with_subject_defs),
        "subjects_resolved": 0,
        "warnings": [],
    }

    if sections:
        body = ""
        for name in PRIMARY_SECTIONS:
            if sections.get(name):
                info["section_used"] = name
                body = sections[name]
                break
        if not body:
            raise ValueError(
                "prompt 里找不到画面段（"
                + " / ".join(PRIMARY_SECTIONS)
                + "）。实际段落："
                + (", ".join(sections.keys()) or "(无)")
                + "。拒绝把整段六段式提示词直接喂给图像模型 —— "
                "retention_analysis 之类是给 H3 的指令，进图像模型只会污染画面。"
            )
        # 六段式里 CAMERA/POSITION DISCIPLINE 排在 retention_analysis 与
        # detailed_description 之间，会落在**前一段**的正文里；这里再对
        # retention_analysis 扫一遍，保证纪律行不会从别处漏进来。
        if sections.get("retention_analysis"):
            _, extra = _clean_body(sections["retention_analysis"])
            info["stripped"]["discipline_lines"] += extra["discipline_lines"]
        text, stats = _clean_body(body)
        info["stripped"]["dialogue"] += stats["dialogue"]
        info["stripped"]["discipline_lines"] += stats["discipline_lines"]
        info["extra_upper_labels"] = stats["extra_upper_labels"]
    else:
        # 旧表（drama-project 的 118 镜）是自由文本，没有段名。容忍，但**必须标注**：
        # 走这条分支说明提示词没按六段式写，出图质量不受本模块保证。
        text, stats = _clean_body(prompt or "")
        info["section_used"] = "raw(legacy: 无段名)"
        info["stripped"]["dialogue"] += stats["dialogue"]
        info["stripped"]["discipline_lines"] += stats["discipline_lines"]
        info["warnings"].append(
            "提示词没有段名（不是六段式/单段式），已按自由文本原样使用；"
            "建议先跑 plan 让提示词规范化"
        )

    # ★ 把 <Subject N> 换成外貌描述（不是"可选拼接"，是必须替换）——
    #   否则 detailed_description 里的主体只有代号，t2i 画出来是路人或干脆画成文字。
    resolved = 0
    if sections.get("subject_definitions"):
        text, resolved = resolve_subjects(text, sections["subject_definitions"])
        info["subjects_resolved"] = resolved

    if with_subject_defs and sections.get("subject_definitions"):
        defs = _subject_defs_for_image(sections["subject_definitions"])
        if defs:
            text = defs + "\n\n" + text
            info["warnings"].append(
                "已额外拼接 subject_definitions 的外观描述（本机 t2i 路径没有参考图，"
                "不拼的话主体只有代号没有外貌）；这属于**超出画面段**的补充，默认关闭"
            )

    if _PICTURE_REF_RE.search(text) or re.search(r"<Subject\s+\d+>", text, re.I):
        info["warnings"].append(
            f"画面提示词里仍有 <Picture N>/<Subject N> 占位符（已替换 {resolved} 处）："
            "它们是 H3 的参考图引用，t2i 路径没有对应参考图，模型可能把代号画成画面文字。"
            "若替换数为 0，说明 subject_definitions 里没解出外貌描述 —— 去查镜头表的这一段。"
        )

    if not text.strip():
        raise ValueError("清洗后的画面提示词为空（原文只有台词/纪律行？）")
    return text, info


# ── 索引（幂等 / stale）──────────────────────────────────────────────────────


def _atomic_write_json(path: Path, data: Any) -> None:
    """先写 .tmp 再 os.replace，避免留下半个 JSON（与 state.py 同一策略）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


@dataclass
class SBEntry:
    fp: str
    file: str
    prompt_fp: str = ""
    source_fp: str = ""
    size: int = 0
    width: int = 0
    height: int = 0
    at: int = 0
    seconds: float = 0.0
    prompt_id: str = ""
    seed: int = 0
    vram_peak_mb: int | None = None
    prompt_used: str = ""
    info: dict = field(default_factory=dict)
    note: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "SBEntry":
        return cls(
            fp=str(d.get("fp", "")),
            file=str(d.get("file", "")),
            prompt_fp=str(d.get("prompt_fp", "")),
            source_fp=str(d.get("source_fp", "")),
            size=int(d.get("size", 0) or 0),
            width=int(d.get("width", 0) or 0),
            height=int(d.get("height", 0) or 0),
            at=int(d.get("at", 0) or 0),
            seconds=float(d.get("seconds", 0.0) or 0.0),
            prompt_id=str(d.get("prompt_id", "")),
            seed=int(d.get("seed", 0) or 0),
            vram_peak_mb=(
                int(d["vram_peak_mb"]) if isinstance(d.get("vram_peak_mb"), (int, float)) else None
            ),
            prompt_used=str(d.get("prompt_used", "")),
            info=d.get("info") or {},
            note=str(d.get("note", "")),
        )

    def to_dict(self) -> dict:
        return {
            "fp": self.fp,
            "prompt_fp": self.prompt_fp,
            "source_fp": self.source_fp,
            "file": self.file,
            "size": self.size,
            "width": self.width,
            "height": self.height,
            "at": self.at,
            "seconds": round(self.seconds, 2),
            "prompt_id": self.prompt_id,
            "seed": self.seed,
            "vram_peak_mb": self.vram_peak_mb,
            "prompt_used": self.prompt_used,
            "info": self.info,
            "note": self.note,
        }


@dataclass
class SBIndex:
    """projects/<项目>/state/storyboard.json 的内存形态。"""

    path: Path
    model: dict = field(default_factory=dict)
    shots: dict[str, SBEntry] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "SBIndex":
        if not path.exists():
            return cls(path=path)
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except (json.JSONDecodeError, OSError):
            # 坏文件不静默重建：挪到 .corrupt 留证（与 state.Manifest 同策略）
            try:
                path.replace(path.with_suffix(path.suffix + ".corrupt"))
            except OSError:
                pass
            return cls(path=path)
        return cls(
            path=path,
            model=d.get("model") or {},
            shots={str(k): SBEntry.from_dict(v) for k, v in (d.get("shots") or {}).items()},
        )

    def save(self) -> None:
        _atomic_write_json(
            self.path,
            {
                "version": INDEX_VERSION,
                "model": self.model,
                "shots": {k: v.to_dict() for k, v in self.shots.items()},
            },
        )

    def status(self, shot_id: str, fp: str, png: Path) -> str:
        if not png.exists() or png.stat().st_size == 0:
            return MISSING
        e = self.shots.get(shot_id)
        if e is None or not e.fp:
            return STALE
        if e.fp != fp:
            return STALE
        if e.size and e.size != png.stat().st_size:
            return STALE
        return CURRENT

    def mark(
        self,
        shot_id: str,
        fp: str,
        png: Path,
        *,
        prompt_fp: str,
        width: int,
        height: int,
        seconds: float,
        prompt_id: str,
        seed: int,
        vram_peak_mb: int | None,
        prompt_used: str,
        info: dict,
        source_fp: str = "",
        note: str = "",
    ) -> None:
        self.shots[shot_id] = SBEntry(
            fp=fp,
            file=str(png),
            prompt_fp=prompt_fp,
            source_fp=source_fp,
            size=png.stat().st_size if png.exists() else 0,
            width=width,
            height=height,
            at=int(time.time()),
            seconds=seconds,
            prompt_id=prompt_id,
            seed=seed,
            vram_peak_mb=vram_peak_mb,
            prompt_used=prompt_used,
            info=info,
            note=note,
        )


# ── 显存采样（只读遥测）──────────────────────────────────────────────────────


class VramSampler:
    """
    生成期间轮询 `nvidia-smi` 记录**峰值**显存。

    为什么要有：本机 H3 单任务吃满 23.6GB / 24GB，而 Qwen-Image 出图时
    ComfyUI 会自动把 H3 换出。不实测就不知道"审片这一步到底会不会把显存打爆"。
    只读查询，不影响任何进程；nvidia-smi 不可用时记 None（不是能力降级）。
    """

    def __init__(self, interval: float = 0.5):
        self.interval = interval
        self.baseline_mb: int | None = None
        self.peak_mb: int | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @staticmethod
    def _read_mb() -> int | None:
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if out.returncode != 0:
            return None
        vals = [int(v) for v in re.findall(r"\d+", out.stdout)]
        return max(vals) if vals else None

    def _loop(self) -> None:
        while not self._stop.is_set():
            v = self._read_mb()
            if v is not None and (self.peak_mb is None or v > self.peak_mb):
                self.peak_mb = v
            self._stop.wait(self.interval)

    def start(self) -> "VramSampler":
        self.baseline_mb = self._read_mb()
        self.peak_mb = self.baseline_mb
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> tuple[int | None, int | None]:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        v = self._read_mb()
        if v is not None and (self.peak_mb is None or v > self.peak_mb):
            self.peak_mb = v
        return self.baseline_mb, self.peak_mb


# ── PNG 校验 ────────────────────────────────────────────────────────────────

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def png_info(path: Path) -> tuple[int, int, int]:
    """
    读 PNG 的 IHDR 拿真实宽高 + 字节数。**不信任请求值** ——
    产物是不是真的、尺寸对不对，要回读文件本身（和 ffprobe 回读时长同一个道理）。
    """
    size = path.stat().st_size
    with open(path, "rb") as f:
        head = f.read(26)
    if head[:8] != _PNG_MAGIC:
        raise ValueError(f"{path} 不是 PNG（头 8 字节 {head[:8]!r}）")
    w, h = struct.unpack(">II", head[16:24])
    return int(w), int(h), size


# ── 单镜生成 ────────────────────────────────────────────────────────────────


@dataclass
class ShotOutcome:
    shot_id: str
    action: str  # rendered / skipped / failed
    png: Path | None = None
    prompt_id: str = ""
    seconds: float = 0.0
    width: int = 0
    height: int = 0
    size: int = 0
    vram_peak_mb: int | None = None
    error: str = ""


def render_shot(
    proj: Any,
    shot: Shot,
    cfg: QIConfig,
    comfy: Comfy,
    *,
    index: SBIndex,
    force: bool = False,
    with_subject_defs: bool = False,
    negative: str = "",
    seed: int | None = None,
    log: Callable[[str], None] = print,
    dry: bool = False,
) -> ShotOutcome:
    """出一张分镜图（或按幂等跳过）。所有落盘都原子写。"""
    sb_dir = Path(proj.root) / STORYBOARD_DIRNAME
    png = sb_dir / f"{shot.id}.png"

    try:
        prompt, info = image_prompt_of(shot.prompt, with_subject_defs=with_subject_defs)
    except ValueError as e:
        return ShotOutcome(shot.id, "failed", error=str(e))

    use_seed = int(shot.seed if seed is None else seed)
    # 负向词兜底：调用方没给就用预设的（"不要让 ai 猜"的另一半 ——
    # 前向说清要什么，负向说清不要什么；`plastic skin` 这类正是假感的来源）
    if not negative:
        from vm import style as _st
        negative = _st.negative_for((params or {}).get("style_preset"))
    fp = qi_fingerprint(prompt, negative, cfg.render_dict(), use_seed)
    st = index.status(shot.id, fp, png)

    for wmsg in info.get("warnings", []):
        log(f"  ⚠ {shot.id}: {wmsg}")

    if st == CURRENT and not force:
        e = index.shots[shot.id]
        log(f"  ⏭ {shot.id} 已是最新（{e.width}×{e.height}, {e.size/1e3:.0f} KB）")
        return ShotOutcome(
            shot.id, "skipped", png=png, width=e.width, height=e.height, size=e.size,
            seconds=e.seconds, prompt_id=e.prompt_id, vram_peak_mb=e.vram_peak_mb,
        )

    if dry:
        log(f"  · {shot.id} [{st}] 将出图 {cfg.width}×{cfg.height} seed={use_seed}")
        log(f"      画面提示词（{len(prompt)} 字）: {prompt[:200]}{'…' if len(prompt) > 200 else ''}")
        return ShotOutcome(shot.id, "dry", png=None, width=cfg.width, height=cfg.height)

    qi = QwenImage(cfg, comfy=comfy)
    sampler = VramSampler().start()

    def on_tick(elapsed: float, running: int, pending: int) -> None:
        log(f"    … {shot.id} {elapsed:5.1f}s  running={running} pending={pending}")

    try:
        res: QIResult = qi.generate(
            prompt,
            negative=negative,
            width=cfg.width,
            height=cfg.height,
            seed=use_seed,
            filename_prefix=f"VM_SB_{shot.id}",
            on_tick=on_tick,
            log=lambda m: log(f"  ⚠ {shot.id}: {m}"),
        )
    except ComfyError as e:
        base, peak = sampler.stop()
        return ShotOutcome(shot.id, "failed", error=str(e), vram_peak_mb=peak)
    except KeyboardInterrupt:
        sampler.stop()
        raise

    base, peak = sampler.stop()

    # ★ 只 GET /view 取回，**不动** ComfyUI output/ 下的任何文件
    comfy.download(res.images[0], png)
    w, h, size = png_info(png)

    index.mark(
        shot.id, fp, png,
        prompt_fp=text_fingerprint(prompt),
        # source_fp 记的是**整段原始 prompt**（含台词/纪律行）。它不参与 stale 判定，
        # 因为台词与纪律行不改变画面：改了台词就不该白烧一次 GPU（这模块存在的意义就是省 GPU）。
        # 但留一个原始指纹在索引里，"这句话到底改没改过"永远查得到。
        source_fp=text_fingerprint(shot.prompt),
        width=w, height=h, seconds=res.seconds, prompt_id=res.prompt_id,
        seed=use_seed, vram_peak_mb=peak, prompt_used=prompt, info=info,
    )
    index.save()
    log(
        f"  ✅ {shot.id} → {png.name} | prompt_id={res.prompt_id} | {res.seconds:.1f}s | "
        f"{w}×{h} | {size/1e3:.0f} KB | 显存峰值 {peak} MB"
    )
    return ShotOutcome(
        shot.id, "rendered", png=png, prompt_id=res.prompt_id, seconds=res.seconds,
        width=w, height=h, size=size, vram_peak_mb=peak,
    )


# ── 批量 ────────────────────────────────────────────────────────────────────


def resolve_project(name: str) -> Any:
    """`西游记` → projects/西游记；也接受直接给路径。"""
    from .state import Project

    p = Path(name)
    if not p.is_dir():
        p = Path(__file__).resolve().parent.parent / "projects" / name
    if not p.is_dir():
        raise SystemExit(f"找不到项目目录：{name}（也不是 projects/{name}）")
    return Project(p).ensure()


def _fmt_at(ts: int) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def list_status(shots: list[Shot], cfg: QIConfig, index: SBIndex, proj: Any,
                *, negative: str, seed: int | None, with_subject_defs: bool) -> list[dict]:
    rows: list[dict] = []
    sb_dir = Path(proj.root) / STORYBOARD_DIRNAME
    for s in shots:
        try:
            prompt, _ = image_prompt_of(s.prompt, with_subject_defs=with_subject_defs)
        except ValueError as e:
            rows.append({"id": s.id, "status": "bad-prompt", "note": str(e)[:80]})
            continue
        use_seed = int(s.seed if seed is None else seed)
        # 负向词兜底：调用方没给就用预设的（"不要让 ai 猜"的另一半 ——
        # 前向说清要什么，负向说清不要什么；`plastic skin` 这类正是假感的来源）
        if not negative:
            from vm import style as _st
            negative = _st.negative_for(getattr(cfg, "style_preset", None))
        fp = qi_fingerprint(prompt, negative, cfg.render_dict(), use_seed)
        png = sb_dir / f"{s.id}.png"
        st = index.status(s.id, fp, png)
        e = index.shots.get(s.id)
        rows.append({
            "id": s.id,
            "status": st,
            "size": f"{e.width}×{e.height}" if e else "",
            "kb": round((e.size if e else 0) / 1e3),
            "seconds": round(e.seconds, 1) if e else 0.0,
            "at": _fmt_at(e.at) if e else "-",
        })
    return rows


def render_all(
    proj: Any,
    cfg: QIConfig,
    *,
    only: list[str] | None = None,
    force: bool = False,
    negative: str = "",
    seed: int | None = None,
    with_subject_defs: bool = False,
    dry: bool = False,
    limit: int | None = None,
    log: Callable[[str], None] = print,
) -> dict:
    """逐镜出图。返回统计（含总 GPU 秒数）。"""
    shots = load_shots_dir(Path(proj.shots_dir))
    if only:
        want = set(only)
        shots = [s for s in shots if s.id in want]
        missing = want - {s.id for s in shots}
        if missing:
            raise SystemExit(f"--only 里这些镜头不存在：{', '.join(sorted(missing))}")
    if limit:
        shots = shots[:limit]
    if not shots:
        raise SystemExit(f"{proj.shots_dir} 里没有镜头（或 --only 未匹配到）")

    index = SBIndex.load(Path(proj.state_dir) / INDEX_FILENAME)
    index.model = {
        "unet": cfg.unet_name, "clip": cfg.clip_name, "vae": cfg.vae_name,
        "clip_type": cfg.clip_type, "width": cfg.width, "height": cfg.height,
        "steps": cfg.steps, "cfg": cfg.cfg, "sampler": cfg.sampler,
        "scheduler": cfg.scheduler, "aura_shift": cfg.aura_shift,
    }

    comfy = Comfy(DEFAULT_BASE)
    if not dry:
        comfy.require_healthy()
        # 探测放在最前：缺节点/模型没下完，要在**提交任何任务之前**就报出来
        QwenImage(cfg, comfy=comfy).require_ready()

    log(f"项目 {proj.root.name}：{len(shots)} 个镜头，目标 {cfg.width}×{cfg.height}，"
        f"{cfg.steps} 步 cfg={cfg.cfg} {cfg.sampler}/{cfg.scheduler}")
    outcomes: list[ShotOutcome] = []
    t0 = time.time()
    for i, s in enumerate(shots, 1):
        log(f"[{i}/{len(shots)}] {s.id}（{s.sec}s，角色 {'、'.join(s.chars) or '无'}）")
        try:
            outcomes.append(render_shot(
                proj, s, cfg, comfy, index=index, force=force,
                with_subject_defs=with_subject_defs, negative=negative,
                seed=seed, log=log, dry=dry,
            ))
        except KeyboardInterrupt:
            log("⏹ 本地停止。⚠ 已提交的任务仍在 ComfyUI 队列里继续跑（本模块不调 /interrupt，"
                "不会打断共享服务上的其他任务）")
            break
        except (ComfyError, ValueError, OSError) as e:
            log(f"  ❌ {s.id} 失败：{e}")
            outcomes.append(ShotOutcome(s.id, "failed", error=str(e)))

    rendered = [o for o in outcomes if o.action == "rendered"]
    skipped = [o for o in outcomes if o.action == "skipped"]
    failed = [o for o in outcomes if o.action == "failed"]
    total_gpu = sum(o.seconds for o in rendered)
    return {
        "project": proj.root.name,
        "shots": len(shots),
        "rendered": len(rendered),
        "skipped": len(skipped),
        "failed": len(failed),
        "wall_seconds": round(time.time() - t0, 1),
        "gpu_seconds": round(total_gpu, 1),
        "per_image_seconds": round(total_gpu / len(rendered), 1) if rendered else None,
        "vram_peak_mb": max([o.vram_peak_mb or 0 for o in rendered], default=None) or None,
        "outcomes": [
            {
                "id": o.shot_id, "action": o.action, "file": str(o.png) if o.png else "",
                "prompt_id": o.prompt_id, "seconds": round(o.seconds, 2),
                "width": o.width, "height": o.height, "size": o.size,
                "vram_peak_mb": o.vram_peak_mb, "error": o.error,
            }
            for o in outcomes
        ],
    }


# ── CLI ─────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python3 -m vm.storyboard",
        description="分镜图（渲染前审片）：按镜头表逐镜出静态图，带指纹幂等。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例：\n"
            "  python3 -m vm.storyboard 西游记 --only 1-1-01        # 单镜\n"
            "  python3 -m vm.storyboard 西游记 --only 1-1-01 --dry-run\n"
            "  python3 -m vm.storyboard 西游记 --list               # 只看状态\n"
            "  python3 -m vm.storyboard 西游记 --check              # 只做前置检查\n"
            "  python3 -m vm.storyboard 西游记 --limit 4            # 先试 4 镜\n"
        ),
    )
    ap.add_argument("project", help="项目名（projects/ 下）或项目目录路径")
    ap.add_argument("--only", action="append", default=[],
                    help="只跑这些镜头，可逗号分隔或重复给：--only 1-1-01,1-2-01")
    ap.add_argument("--force", action="store_true", help="忽略指纹，强制重出")
    ap.add_argument("--list", action="store_true", help="只打印状态表，不出图")
    ap.add_argument("--check", action="store_true", help="只跑前置检查（节点/模型/尺寸）")
    ap.add_argument("--dry-run", action="store_true", help="打印提示词与参数，不提交")
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 镜")
    ap.add_argument("--width", type=int, default=None)
    ap.add_argument("--height", type=int, default=None)
    ap.add_argument("--steps", type=int, default=None)
    ap.add_argument("--cfg", type=float, default=None)
    ap.add_argument("--sampler", default=None)
    ap.add_argument("--scheduler", default=None)
    ap.add_argument("--aura-shift", type=float, default=None,
                    help="挂 ModelSamplingAuraFlow 指定 shift（默认不挂，用模型自带 shift）")
    ap.add_argument("--seed", type=int, default=None, help="覆盖镜头表的 seed（默认用镜头 seed）")
    ap.add_argument("--negative", default="", help="负向提示词（默认空）")
    ap.add_argument("--with-subject-defs", action="store_true",
                    help="把 subject_definitions 的外观描述拼到画面提示词前面（默认关闭）")
    ap.add_argument("--json", action="store_true", help="结果 JSON 打到 stdout，人类日志走 stderr")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    log: Callable[[str], None]
    if args.json:
        log = lambda m: print(m, file=sys.stderr, flush=True)  # noqa: E731
    else:
        log = lambda m: print(m, flush=True)  # noqa: E731

    proj = resolve_project(args.project)

    cfg = QIConfig()
    if args.width:
        cfg.width = args.width
    if args.height:
        cfg.height = args.height
    if args.steps:
        cfg.steps = args.steps
    if args.cfg is not None:
        cfg.cfg = args.cfg
    if args.sampler:
        cfg.sampler = args.sampler
    if args.scheduler:
        cfg.scheduler = args.scheduler
    cfg.aura_shift = args.aura_shift

    cfg.width, cfg.height, note = snap_size(cfg.width, cfg.height)
    if note:
        log(f"⚠ {note}")

    only: list[str] = []
    for chunk in args.only:
        only += [x.strip() for x in chunk.split(",") if x.strip()]

    if args.check:
        p = QwenImage(cfg).probe()
        log(p.report())
        log("")
        log(f"尺寸 {cfg.width}×{cfg.height}（{SIZE_MULTIPLE} 的整数倍："
            f"{cfg.width % SIZE_MULTIPLE == 0 and cfg.height % SIZE_MULTIPLE == 0}）")
        return 0 if not p.problems else 2

    if args.list:
        index = SBIndex.load(Path(proj.state_dir) / INDEX_FILENAME)
        shots = load_shots_dir(Path(proj.shots_dir))
        if only:
            shots = [s for s in shots if s.id in set(only)]
        rows = list_status(shots, cfg, index, proj, negative=args.negative,
                           seed=args.seed, with_subject_defs=args.with_subject_defs)
        log(f"{'镜头':<10} {'状态':<10} {'尺寸':<12} {'KB':>7} {'秒':>6}  {'时间':<12}")
        for r in rows:
            if r["status"] == "bad-prompt":
                log(f"{r['id']:<10} {'提示词问题':<10} {r['note']}")
            else:
                log(f"{r['id']:<10} {r['status']:<10} {r['size']:<12} {r['kb']:>7} "
                    f"{r['seconds']:>6}  {r['at']:<12}")
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    try:
        stats = render_all(
            proj, cfg, only=only or None, force=args.force, negative=args.negative,
            seed=args.seed, with_subject_defs=args.with_subject_defs,
            dry=args.dry_run, limit=args.limit, log=log,
        )
    except ComfyError as e:
        log(f"❌ {e}")
        if args.json:
            print(json.dumps({"error": str(e)}, ensure_ascii=False, indent=2))
        return 2
    except SystemExit as e:
        log(f"❌ {e}")
        return 2

    log("")
    per = stats["per_image_seconds"]
    vram = stats["vram_peak_mb"]
    log(f"完成：出图 {stats['rendered']} / 跳过 {stats['skipped']} / 失败 {stats['failed']}；"
        f"GPU 合计 {stats['gpu_seconds']}s，"
        f"单图均 {per if per is not None else '—'}s，"
        f"显存峰值 {vram if vram is not None else '—'} MB")
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0 if stats["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
