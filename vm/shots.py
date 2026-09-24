"""
shots.py —— 镜头表的 schema、读写与校验。

镜头表是 plan 与 render 之间唯一的契约：plan 写它，gen 读它，人也可以手改它。
所以这个模块的职责不是"顺手存个 JSON"，而是**把错误挡在烧 GPU 之前**：

  ① 向后兼容
       用户现有的 `drama-project/shots/*.json` 只有 {id,sec,chars,seed,prompt} 五个键
       （118 个镜头全是这个形状），必须能原样读进来。缺省字段一律容忍，
       新增字段（shot_size/camera/dialogue/narration）只在非空时写回，避免把旧表撑大。

  ② sec 用秒不用帧
       帧数网格是 17n+5（本机实测唯一稳定区间 [56,362]）。让人手算 17n+5 是反人性的，
       所以表里只写目标秒数，渲染前由 seconds_to_frames() 统一换算。
       换算取**最近的网格点**（不是向上取整）：qc 的时长容差是 ±1s，
       最近点的偏差 ≤0.375s，比"永远多渲 0.6s"更省显存。

  ③ 校验具体到镜头号
       validate_shots() 返回的是给人看的问题清单（含镜头号），不是布尔值——
       118 个镜头里有一张参考图缺失时，"False" 毫无信息量。

  ④ 六段式结构检查
       H3 Ref2VA 要求六段**齐全、按序、不重复**，summary 还必须以官方枚举值
       `[reference generation]` 之类开头。这些是渲染侧静默劣化的高发区
       （少一段模型不报错，只是参考图不生效），必须在入队前查出来。

风格约定与 state.py 一致：中文注释解释"为什么"，原子写，UTF-8 不转义中文。
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# ── 帧网格与时长域（与 CONTRACTS.md 冻结值一致）──────────────────────────────
FPS = 24

FRAME_STRIDE = 17  # 帧数网格：frames = 17n + 5
FRAME_OFFSET = 5
FRAME_MIN = 56  # n=3  → ≈2.33s
FRAME_MAX = 362  # n=21 → ≈15.08s
_N_MIN = (FRAME_MIN - FRAME_OFFSET) // FRAME_STRIDE  # 3
_N_MAX = (FRAME_MAX - FRAME_OFFSET) // FRAME_STRIDE  # 21

SEC_MIN = 3
SEC_MAX = 15

# 中文配音语速：4 字/秒（NiliX 生产配置默认值，配置域 [2,8]）。
# 台词+旁白总字数 ÷ 4 超过镜头时长 → H3 会念一半就切，表现为"画面有字无配音"。
CHARS_PER_SEC = 4.0

# H3 参考图上限：每镜超过 3 个角色，多出来的角色没有参考图，必脸崩。
MAX_CHARS_PER_SHOT = 3

# 分镜密度目标：每镜约 90 字正文（对应 50-130 字/镜 的可接受区间）。
# 为什么要有这个数：没有它时 LLM 会把每句对白都单独成镜 —— 实测 1745 字的章节
# 被拆成 48 镜（平均 36 字/镜），结果是片子碎成幻灯片、GPU 时间翻倍。
# plan 阶段按 round(章节字数 / TARGET_CHARS_PER_SHOT) 算出目标镜数，
# 实际镜数超过目标的 1.5 倍即判"拆得过碎"，触发重写。
TARGET_CHARS_PER_SHOT = 90

# 六段式（Ref2VA）——官方顺序，缺一不可、不可重排、不可重复
SIX_SECTIONS = (
    "subject_definitions",
    "summary",
    "retention_analysis",
    "detailed_description",
    "overall_soundscape",
    "non_diegetic_music",
)

# 单段式（FL2VA）：无角色空镜/旧表用，只有主字段 + 两个声音段
FL2VA_MAIN = "integrated_multimodal_description"
FL2VA_SECTIONS = (FL2VA_MAIN, "overall_soundscape", "non_diegetic_music")

# 纪律行前缀：它们不是"段"，而是插在段内（紧贴 detailed_description 段标题前）的高服从位指令。
# 官方六段式计数时必须排除它们，否则会被误判成"多了第 7 段"。
# 依据：NiliX manju_pipeline.go injectCameraDiscipline/injectPositionDiscipline——纪律放段尾
# 服从度弱（实测运镜位移 0.00px），放到 detailed_description 段标题前才生效。
DISCIPLINE_PREFIXES = (
    "CAMERA DISCIPLINE",
    "POSITION DISCIPLINE",
    "CINEMATOGRAPHY",
    "CROWD DISTANCE",
    "SCREEN DISCIPLINE",
)

# summary 允许的任务类型（官方 guide_ref.md 枚举值）
TASK_TYPES = (
    "keyframe completion",
    "reference generation",
    "video editing",
    "video continuation",
    "audio reuse",
    "audio reference",
)


@dataclass
class Shot:
    """一个镜头。前 5 个字段是用户现有 schema 的全部；后 4 个是可选记录字段。"""

    id: str
    sec: int
    chars: list[str]
    seed: int
    prompt: str
    shot_size: str = ""
    camera: str = ""
    dialogue: str = ""
    narration: str = ""
    # ── 时长三层（A2，2026-09-23）──
    # 权威依据：Director-WebUI 的三层时长设计（`comfyui_h3.py:20-23`）。
    #   sec          规划时长（用户/LLM 写的）
    #   sec_override 人工覆盖（留空 = 用 sec）
    #   sec_actual   **ffprobe 回读的真实时长**
    # 为什么必须有第三层：H3 的帧数是 `17k+5` 格点，请求 6s 实出 141 帧 = **5.875s**。
    # 若 concat 与字幕时间轴用请求值，误差会**逐镜累积**（52 镜可达十几秒）。
    # ★ 凡是拼片/字幕/EDL 的地方，一律用 `sec_actual`，读不到才回退 ffprobe 现读。
    sec_override: float | None = None
    sec_actual: float | None = None

    # 本镜戏剧张力 0-10（不是打斗强度）。供 vm/audit/pacing.py 做**渲染前**节奏审计：
    # 检测"连续多镜低张力"（观众划走的地方）、"全片无高潮"、"高开低走"。
    # 缺省 None = 旧表没有该字段，审计层会退化成代理分并**显式标注判别力不足**。
    conflict: float | None = None

    # 场景实体 id（如 S1），指向项目 `scenes.json`。**不是 id 里的场次号**——
    # 那个只是编号；这个才是"同一个地点"。同 scene_id 的镜头共享同一段场景描述，
    # 这是跨镜场景一致性的锚点（文字锚定，不占 H3 参考图槽位）。
    scene_id: str = ""

    # 关键道具 id 列表（如 ["P1"]），指向项目 `props.json`。
    # 同道具出现的镜头注入**同一段**外观描述 —— 避免"金箍棒每镜长得不一样"。
    prop_ids: list[str] = field(default_factory=list)

    # 服装变体：{角色名: 变体id}，空 dict = 全部用角色默认服装。
    # 为什么挂在镜头上：用户明确"按章/按场次/按单镜换装都可能"，
    # 只有挂在镜头级才能覆盖全部三种场景（按章/场次换装由 UI 批量写，见 UI-PLAN.md §5.3）。
    # 它**不参与** shot_fingerprint —— 服装改的是提示词文字，而提示词本来就在指纹里，
    # 所以换装 → prompt 变 → 指纹变 → 自动 stale，链路已经通了。
    costume: dict[str, str] = field(default_factory=dict)


# ── 帧数换算 ────────────────────────────────────────────────────────────────


def effective_sec(s) -> float:
    """
    实际应提交的时长 = sec_override ?? sec（A2 三层时长的中间层）。

    注意这**不是**用于拼片/字幕的时长 —— 那必须用 sec_actual（回读值）。
    """
    ov = getattr(s, "sec_override", None)
    return float(ov) if ov else float(getattr(s, "sec", 0) or 0)


def timeline_sec(s) -> float:
    """
    时间轴口径（拼片 / 字幕 / EDL）—— 优先 sec_actual，其次 sec_override，最后 sec。

    调用方若拿到的是 None（还没回读过），应当自己 ffprobe 现读，**不要**直接用请求值。
    """
    for key in ("sec_actual", "sec_override", "sec"):
        v = getattr(s, key, None)
        if v:
            return float(v)
    return 0.0


def _frame_bounds(fmin: int | None = None, fmax: int | None = None) -> tuple[int, int]:
    """
    帧网格 clamp 上下限。缺省 = 契约冻结值 [56, 362]（不改配置时行为逐字节不变）。

    T3 配置中心：机器级设置 frame_min / frame_max 由 vm/config.install() 经环境变量
    VM_FRAME_MIN / VM_FRAME_MAX 注入（与 QI_UNET / VM_SUBTITLE_FONT 同款约定）。
    为什么走环境变量而不是改调用方：seconds_to_frames 的调用方散在 gen.py / taskctl.py /
    chars.py，环境变量是唯一不用逐个改它们的通道；fmin/fmax 参数留给未来显式传参的调用方。
    非法值（不在 17n+5 网格上 / 不是整数）一律回退默认 —— 帧格错一格 H3 直接拒收，宁可保守。
    """
    lo, hi = FRAME_MIN, FRAME_MAX
    for name, is_max in (("VM_FRAME_MAX", True), ("VM_FRAME_MIN", False)):
        raw = os.environ.get(name, "").strip()
        if not raw:
            continue
        try:
            v = int(raw)
        except ValueError:
            continue
        if (v - FRAME_OFFSET) % FRAME_STRIDE:  # 不在 17n+5 网格上 → 无视
            continue
        lo, hi = (lo, v) if is_max else (v, hi)
    if fmin is not None:
        lo = int(fmin)
    if fmax is not None:
        hi = int(fmax)
    if hi < lo:
        lo, hi = hi, lo
    return lo, hi


def seconds_to_frames(sec: float, fps: int = FPS, fmin: int | None = None,
                      fmax: int | None = None) -> int:
    """
    秒 → 对齐 17n+5 网格的帧数，clamp 到 [56,362]（T3 起上限可用 frame_min/frame_max 配置）。

    自证（契约要求）：seconds_to_frames(5)==124、seconds_to_frames(10)==243、
    seconds_to_frames(15)==362。
    """
    lo, hi = _frame_bounds(fmin, fmax)
    try:
        s = float(sec)
    except (TypeError, ValueError):
        s = 0.0
    if s <= 0:
        return lo
    target = s * fps
    # 取最近网格点：round-half-up（Python 内建 round 是银行家舍入，会在这里产生
    # 0.5 帧级的不可预期抖动，破坏"同输入同输出"的可复现性）
    n = int(math.floor((target - FRAME_OFFSET) / FRAME_STRIDE + 0.5))
    n = max((lo - FRAME_OFFSET) // FRAME_STRIDE, min((hi - FRAME_OFFSET) // FRAME_STRIDE, n))
    return FRAME_STRIDE * n + FRAME_OFFSET


def frames_to_sec(frames: int, fps: int = FPS) -> float:
    """帧数 → 秒。给 UI/质检显示用（表里存的是秒，渲染存的是帧）。"""
    return round(int(frames) / float(fps), 3)


# ── 语音时长预算 ────────────────────────────────────────────────────────────

# 说话人前缀： "(S1)孙悟空:" / "旁白：" / "内心·唐僧：" —— 只剥前缀，不剥正文里的冒号
_SPEECH_PREFIX_RE = re.compile(r"^\s*(?:\(S\d+\)\s*)?[^:：]{1,14}[:：]\s*")
_PUNCT_RE = re.compile(
    r"[\s\u3000，。！？、；：“”‘’（）《》〈〉【】…—～·,.!?;:'\"()\[\]{}<>|/\\*#`~^&+=@$-]+"
)


def speech_chars(dialogue: str = "", narration: str = "") -> int:
    """
    台词+旁白的"字数"（中文按字、英文按字符近似），剥说话人前缀与标点后计数。

    与 NiliX manjuSpeechChars 同口径：dialogue 与 narration **合并**预算——
    只算 dialogue 会让旁白超预算，是"画面有字无配音"的诱因。
    """
    total = 0
    for part in (dialogue, narration):
        if not part:
            continue
        text = re.sub(r"</?d>", "", str(part))
        text = _SPEECH_PREFIX_RE.sub("", text, count=1)
        total += len(_PUNCT_RE.sub("", text))
    return total


def seconds_needed(dialogue: str = "", narration: str = "", chars_per_sec: float = CHARS_PER_SEC) -> float:
    """这段语音最少需要多少秒。0 字返回 0。"""
    n = speech_chars(dialogue, narration)
    if not n or chars_per_sec <= 0:
        return 0.0
    return n / float(chars_per_sec)


def fit_sec_to_speech(
    sec: int,
    dialogue: str = "",
    narration: str = "",
    *,
    chars_per_sec: float = CHARS_PER_SEC,
    sec_max: int = SEC_MAX,
) -> tuple[int, str]:
    """
    语音超预算就延长镜头时长（clamp 到 sec_max）。返回 (新sec, 说明)。

    为什么不截台词：脚本/小说是权威，改内容会改变剧情。宁可长一点，也不能让 H3 念一半。
    为什么返回说明：调用方要把"我替你改过时长"写进日志/统计，不能静默改数据
    （静默替换正是本项目最想消灭的一类 bug）。
    """
    need = seconds_needed(dialogue, narration, chars_per_sec)
    if need <= 0 or need <= sec:
        return sec, ""
    n = speech_chars(dialogue, narration)
    adj = min(sec_max, int(math.ceil(need)))
    if adj <= sec:
        # 连 sec_max 都装不下：如实说明，交给上游决定拆镜还是精简台词
        return sec, (
            f"语音 {n} 字约需 {need:.1f}s，超过单镜上限 {sec_max}s，"
            f"当前 {sec}s 会截断（建议拆镜或精简台词）"
        )
    return adj, f"语音 {n} 字约需 {need:.1f}s > {sec}s，已自动延长到 {adj}s（防台词截断）"


# ── 提示词结构检查 ──────────────────────────────────────────────────────────

# 段名必须是行首的小写英文+下划线（严格锚定行首，避免把正文里的
# "he says: ..." 之类误判为段名）。纪律行是大写前缀，天然不匹配。
_HEADER_RE = re.compile(r"(?m)^[ \t]*([a-z][a-z_]{2,})[ \t]*:")
_DISC_LINE_RE = re.compile(r"^[ \t]*([A-Z][A-Z ]{3,}?)[ \t]*:")


def prompt_headers(prompt: str) -> list[tuple[int, str]]:
    """返回 [(字符偏移, 段名)]，按出现顺序。"""
    return [(m.start(), m.group(1)) for m in _HEADER_RE.finditer(prompt or "")]


def section_bodies(prompt: str) -> dict[str, str]:
    """把提示词切成 {段名: 段内容}。纪律行归入它后面紧跟的那一段。"""
    text = prompt or ""
    marks = prompt_headers(text)
    out: dict[str, str] = {}
    for i, (pos, name) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        body = text[pos:end]
        body = body.split(":", 1)[1] if ":" in body else ""
        out[name] = body.strip()
    return out


def _summary_issue(body: str) -> str:
    """summary 必须是官方任务类型枚举值开头的英文段落，不能是自由文本。"""
    m = re.match(r"^\s*\[([^\]]+)\]", body or "")
    if not m:
        return "summary 段必须以方括号任务类型开头（如 [reference generation]）"
    types = [t.strip() for t in m.group(1).split("+") if t.strip()]
    bad = [t for t in types if t not in TASK_TYPES]
    if bad:
        return "summary 的任务类型不是官方枚举值：" + ", ".join(bad)
    return ""


def check_prompt_structure(prompt: str) -> list[str]:
    """
    校验 H3 提示词结构：六段式（Ref2VA）或单段式（FL2VA）二选一。

    返回问题清单（空 = 通过）。检查的是**结构**，不是内容质量——
    内容质量由 plan 的 LLM 校验负责。
    """
    text = prompt or ""
    if not text.strip():
        return ["提示词为空"]

    names = [n for _, n in prompt_headers(text)]
    six_found = [n for n in names if n in SIX_SECTIONS]

    # 出现 2 个以上六段式段名，或以 subject_definitions 开头 → 按六段式严格校验
    # （后者覆盖"只写了 1 段、其余全缺"的半截输出，报错才能具体到缺哪几段）
    if len(six_found) >= 2 or (names and names[0] == SIX_SECTIONS[0]):
        problems: list[str] = []
        if not names or names[0] != SIX_SECTIONS[0]:
            first = names[0] if names else "(无字段名)"
            problems.append(
                f"六段式必须以 {SIX_SECTIONS[0]}: 开头，实际首个字段是 {first}:"
                "（六段式期间禁止在段外写任何内容）"
            )
        seq = [n for n in names if n in SIX_SECTIONS]
        missing = [s for s in SIX_SECTIONS if s not in seq]
        extra = [n for n in names if n not in SIX_SECTIONS]
        if missing:
            problems.append("六段式缺少段：" + ", ".join(missing))
        if extra:
            problems.append(
                "六段式出现非规范段（只允许这 6 段，纪律行不算段）："
                + ", ".join(dict.fromkeys(extra))
            )
        if not missing and not extra and seq != list(SIX_SECTIONS):
            problems.append("六段式顺序错误：实际 " + " -> ".join(seq))
        if "summary" in seq:
            issue = _summary_issue(section_bodies(text).get("summary", ""))
            if issue:
                problems.append(issue)
        return problems

    # 旧表/空镜的单段式：只认 integrated_multimodal_description 开头
    if names and names[0] == FL2VA_MAIN:
        return []

    if not names:
        return [
            "提示词既不是六段式（应有 subject_definitions 等 6 段），"
            f"也不是单段式（应以 {FL2VA_MAIN}: 开头）"
        ]
    return [
        f"提示词格式无法识别（首个字段是 {names[0]}:）；"
        f"六段式必须以 {SIX_SECTIONS[0]}: 开头，单段式必须以 {FL2VA_MAIN}: 开头"
    ]


# ── 读写 ────────────────────────────────────────────────────────────────────


def _as_int(value: Any, default: int | None = None) -> int | None:
    """宽进严出地取整数：容忍 "3100" / 6.0 / "6秒"，但不接受 True 或 "abc"。"""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else default
    if isinstance(value, str):
        t = value.strip()
        if re.fullmatch(r"-?\d+", t):
            return int(t)
        m = re.match(r"^(\d+)", t)
        if m:
            return int(m.group(1))
    return default


def _coerce_chars(value: Any, where: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [c for c in re.split(r"[,，、\s]+", value) if c]
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for c in value:
            if not isinstance(c, str):
                raise ValueError(f"{where}: chars 里含非字符串项 {c!r}")
            if c.strip():
                out.append(c.strip())
        return out
    raise ValueError(f"{where}: chars 必须是字符串数组，实际是 {type(value).__name__}")


def _coerce_shot(raw: Any, where: str) -> Shot:
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: 镜头表每一项都必须是 JSON 对象，实际是 {type(raw).__name__}")

    sid = str(raw.get("id") or "").strip()
    tag = sid or "(无 id)"

    sec = _as_int(raw.get("sec"))
    if sec is None:
        raise ValueError(f"{where}: 镜头 {tag} 的 sec 不是整数：{raw.get('sec')!r}")

    seed = _as_int(raw.get("seed"))
    if seed is None:
        # seed 是可复现性的根，缺了不能猜一个（猜出来的 seed 会让"重跑"变成新画面）
        raise ValueError(f"{where}: 镜头 {tag} 的 seed 不是整数：{raw.get('seed')!r}")

    return Shot(
        id=sid,
        sec=sec,
        chars=_coerce_chars(raw.get("chars"), where),
        seed=seed,
        prompt=str(raw.get("prompt") or ""),
        shot_size=str(raw.get("shot_size") or ""),
        camera=str(raw.get("camera") or ""),
        dialogue=str(raw.get("dialogue") or ""),
        narration=str(raw.get("narration") or ""),
        costume=_coerce_costume(raw.get("costume")),
        sec_override=_as_float(raw.get("sec_override")),
        sec_actual=_as_float(raw.get("sec_actual")),
        conflict=_as_float(raw.get("conflict")),
        scene_id=str(raw.get("scene_id") or "").strip(),
        prop_ids=[str(x) for x in (raw.get("prop_ids") or []) if x],
    )


def load_shots(path: Path) -> list[Shot]:
    """
    读单个镜头表文件。容忍：
      - 顶层是 [{...}]，或 {"shots": [{...}]}
      - 旧表缺 shot_size/camera/dialogue/narration
      - chars 写成 "孙悟空,唐僧" 这样的字符串
    不宽容：JSON 语法错误、seed 缺失/非法（会带文件名与镜头号报错）。
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ValueError(f"读不到镜头表 {path}：{e}") from e
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"镜头表 {path} 不是合法 JSON：{e}") from e
    if isinstance(data, dict):
        data = data.get("shots") or []
    if not isinstance(data, list):
        raise ValueError(f"镜头表 {path} 顶层必须是数组或 {{\"shots\": [...]}}")
    return [_coerce_shot(item, str(path)) for item in data]


def _shot_to_dict(s: Shot) -> dict:
    """可选字段只在非空时写回，保持旧表（5 键）的干净形状。"""
    d: dict[str, Any] = {
        "id": s.id,
        "sec": s.sec,
        "chars": list(s.chars),
        "seed": s.seed,
        "prompt": s.prompt,
    }
    for key in ("shot_size", "camera", "dialogue", "narration"):
        v = getattr(s, key)
        if v:
            d[key] = v
    if s.costume:
        d["costume"] = dict(s.costume)
    if s.scene_id:
        d["scene_id"] = s.scene_id
    if s.prop_ids:
        d["prop_ids"] = list(s.prop_ids)
    for key in ("sec_override", "sec_actual", "conflict"):
        v = getattr(s, key, None)
        if v is not None:
            d[key] = round(float(v), 4)
    return d


def _as_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _coerce_costume(raw: Any) -> dict[str, str]:
    """容忍形状不规范的 costume：只接受 {角色: 变体id} 的字符串映射，其余忽略。"""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        if isinstance(k, str) and k.strip() and isinstance(v, str) and v.strip():
            out[k.strip()] = v.strip()
    return out


def _atomic_write_text(path: Path, text: str) -> None:
    """先写 .tmp 再 os.replace，避免掉电/被杀留下半个 JSON（与 state.py 同一策略）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def dumps_shots(shots: Iterable[Shot]) -> str:
    """镜头表 → JSON 文本（UTF-8 不转义中文，便于人工审阅/手改）。"""
    return json.dumps([_shot_to_dict(s) for s in shots], ensure_ascii=False, indent=2) + "\n"


def save_shots(path: Path, shots: list[Shot]) -> None:
    _atomic_write_text(Path(path), dumps_shots(shots))


def load_shots_dir(d: Path) -> list[Shot]:
    """按文件名排序合并目录下全部 *.json（跳过 . 开头与 .tmp 残留）。"""
    d = Path(d)
    if not d.is_dir():
        return []
    out: list[Shot] = []
    for p in sorted(d.glob("*.json")):
        if p.name.startswith(".") or p.name.endswith(".tmp"):
            continue
        out.extend(load_shots(p))
    return out


# ── 校验 ────────────────────────────────────────────────────────────────────


def validate_shots(shots: list[Shot], refs_dir: Path) -> list[str]:
    """
    返回问题清单（人类可读、含镜头号）。空列表 = 通过。

    这些检查都能在"烧 GPU 之前"发现错误：id 撞车会互相覆盖产物、sec 出域会
    渲出模型不支持的帧数、chars 空则参考图不注入（角色一致性归零）、
    chars 有名字没图则 gen 会报错。参考图路径口径与 chars.py 一致：char_<名>.png。
    """
    refs_dir = Path(refs_dir)
    problems: list[str] = []
    seen: dict[str, int] = {}

    for idx, s in enumerate(shots, 1):
        tag = s.id or f"第 {idx} 个镜头"

        # id
        if not s.id:
            problems.append(f"第 {idx} 个镜头缺少 id")
        elif s.id in seen:
            problems.append(f"镜头 {s.id} 的 id 与第 {seen[s.id]} 个镜头重复（产物会互相覆盖）")
        else:
            seen[s.id] = idx
        if s.id and ("/" in s.id or "\\" in s.id or ".." in s.id):
            problems.append(f"镜头 {tag} 的 id 含路径字符，会写到 clips/ 之外")

        # sec
        if not isinstance(s.sec, int) or isinstance(s.sec, bool):
            problems.append(f"镜头 {tag} 的 sec 不是整数：{s.sec!r}")
        elif not (SEC_MIN <= s.sec <= SEC_MAX):
            problems.append(f"镜头 {tag} 的 sec={s.sec} 超出 [{SEC_MIN},{SEC_MAX}] 秒")

        # chars
        if not s.chars:
            problems.append(f"镜头 {tag} 的 chars 为空（参考图靠它注入，不能空）")
        else:
            if len(s.chars) > MAX_CHARS_PER_SHOT:
                problems.append(
                    f"镜头 {tag} 登场角色 {len(s.chars)} 个超过 {MAX_CHARS_PER_SHOT} 个"
                    "（H3 参考图上限，超员角色必脸崩，须拆镜）"
                )
            dup = {c for c in s.chars if s.chars.count(c) > 1}
            if dup:
                problems.append(f"镜头 {tag} 的 chars 有重复角色：{'、'.join(sorted(dup))}（同一张参考图会挂两次）")
            for name in s.chars:
                if not name or "/" in name or "\\" in name or ".." in name:
                    problems.append(f"镜头 {tag} 的角色名非法：{name!r}")
                    continue
                ref = refs_dir / f"char_{name}.png"
                if not ref.exists():
                    problems.append(f"镜头 {tag} 的角色「{name}」缺少参考图 {ref}")

        # seed
        if not isinstance(s.seed, int) or isinstance(s.seed, bool):
            problems.append(f"镜头 {tag} 的 seed 不是整数：{s.seed!r}")

        # prompt 结构
        if not (s.prompt or "").strip():
            problems.append(f"镜头 {tag} 的 prompt 为空")
        else:
            problems.extend(f"镜头 {tag} 提示词结构：{m}" for m in check_prompt_structure(s.prompt))

        # 语音时长预算
        if isinstance(s.sec, int) and s.sec > 0:
            n = speech_chars(s.dialogue, s.narration)
            need = n / CHARS_PER_SEC
            if need > s.sec:
                problems.append(
                    f"镜头 {tag} 台词+旁白 {n} 字约需 {need:.1f}s，但 sec 只有 {s.sec}s"
                    f"（{CHARS_PER_SEC:g} 字/秒，H3 会念一半就切；请加时长或拆镜）"
                )
    return problems
