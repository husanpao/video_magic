"""
audit —— **零 LLM、零 GPU、零 I/O 的渲染前质检层**。

## 为什么审计比"更便宜地出片"值钱

5 份外部调研（含 106 个 GitHub 项目）里最该记住的是 wind-comic README 的行业数据：

  > 「制作只占总成本 **7.5%**，投流占 **70-85%**……任何以『更便宜地出片』为唯一卖点的
  >  工具，价值天花板极低」「AI 漫剧爆款率不足 **0.1%**、约 **90%** 公司亏损」

→ 真正的杀手是**废片率**，不是 GPU 秒数。所以这块质检必须在**烧 GPU 之前**跑：
输入只需要已有的分镜表 JSON，输出是"第几镜到第几镜有问题 + 人能照着改的 rewriteHint"。

## 三个模块

| 模块 | 治什么 | 来源 |
| --- | --- | --- |
| `dialogue_coverage`（B1） | 「一段连续对话用 1 个 wide shot 涵盖」的 **AI 一遍跑完感** | wind-comic `lib/dialogue-coverage.ts` |
| `pacing`（B2） | 节奏**形状**（高开低走 / 层层递进 / 呆板）+ **第几到第几镜观众会划走** | wind-comic `lib/pacing-audit-v2.ts` |
| `dialogue_fidelity`（B5） | 小说**原文对白零丢失**的量化门禁（覆盖率 + src_span 逐字校验） | 我们自研（两个深挖项目都没做到，见 `NEXT-ACTIONS.md` 第五批） |

## 契约（给 Lead / UI / CI 用）

  - 所有 `audit_*()` 都是**纯函数**：给定同样的镜头表 → 同样的判定（无时间戳、无随机、无 I/O）。
  - 每个判定带 `severity`：`error`（必须改） / `warning`（应该改） / `info`（建议改）。
    **"装了但没通电"是本项目明确反对的模式**（storyforge `scene_agent.py:242-248`）：
    本层不产生"只写日志"的判定，每条都进 `audit.json`、都能被 CLI 的 `--gate` 变成非零退出码。
  - CLI：`python3 -m vm.audit.cli <项目名>` → 人类可读报告 + `projects/<名>/state/audit.json`。

## 镜头输入的形状

审计层同时接受：

  - `vm.shots.Shot` dataclass（`load_shots_dir()` 的返回值）
  - 原始 `dict`（`json.load()` 的返回值）—— **这是有意为之**：
    `Shot` 会丢掉 schema v2 的新字段（`conflict` / `location` / `dialogue[].src_span`），
    而审计层恰恰要消费它们。所以这里用 `field()` 统一取值，两种形状都能跑。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Sequence

__all__ = [
    "SEVERITIES",
    "severity_rank",
    "as_mapping",
    "field",
    "dialogue_units",
    "dialogue_text",
    "is_dialogue_shot",
    "shot_size_class",
    "is_wide_class",
    "closeness_score",
    "SIZE_CLASSES",
    "atomic_write_json",
    "locate_project",
    "load_raw_shots",
]

# ── 严重度 ──────────────────────────────────────────────────────────────────
# 只有三档，且**每档都有消费方**：error 让 --gate 失败、warning 汇总进报告、
# info 给人看。不做第四档 —— 档位一多就没人认真对待了。
SEVERITIES = ("error", "warning", "info")
_RANK = {"error": 3, "warning": 2, "info": 1}


def severity_rank(severity: str) -> int:
    """severity → 数字（error=3/warning=2/info=1），未知档当 info。"""
    return _RANK.get(str(severity or "").strip().lower(), 1)


# ── 输入形状兼容 ────────────────────────────────────────────────────────────


def as_mapping(shot: Any) -> dict:
    """
    把 `Shot` dataclass 或 dict 统一成 dict（只为读，不改原件）。

    为什么不用 `shots.load_shots` 再 `_shot_to_dict`：那条路会**丢掉**审计需要的
    schema v2 字段（conflict / location / src_span），而审计的全部价值就在于消费它们。
    """
    if shot is None:
        return {}
    if isinstance(shot, dict):
        return shot
    if is_dataclass(shot):
        return asdict(shot)
    # 兜底：任何带 __dict__ 的对象
    return dict(getattr(shot, "__dict__", {}) or {})


def field(shot: Any, name: str, default: Any = None) -> Any:
    """读一个字段，dict / dataclass / 普通对象都支持。缺字段返回 default。"""
    if isinstance(shot, dict):
        return shot.get(name, default)
    if is_dataclass(shot):
        return getattr(shot, name, default)
    if hasattr(shot, name):
        return getattr(shot, name)
    return default


def _first_field(shot: Any, names: Sequence[str], default: Any = None) -> Any:
    for n in names:
        v = field(shot, n, None)
        if v not in (None, "", [], {}):
            return v
    return default


# ── 对白形状（str | [{char,src_span,quote}]）─────────────────────────────────


def dialogue_units(shot: Any) -> list[dict]:
    """
    把一镜的台词归一成 `[{char, quote, src_span}]`。兼容两种 schema：

      - 旧：`dialogue: "悟空，你听这风声"`（字符串，无说话人、无溯源）
      - v2：`dialogue: [{char: "唐僧", src_span: [12, 24], quote: "悟空，你听…"}]`

    为什么必须同时支持：现有 52 镜是旧形状，硬要它升级就没人能用这个审计；
    而 v2 的 `src_span` 才是 B5 "逐字比对"的立足点。**降级要安静，升级要能吃到。**
    """
    raw = field(shot, "dialogue", "")
    out: list[dict] = []
    if isinstance(raw, str):
        text = raw.strip()
        if text:
            out.append({"char": "", "quote": text, "src_span": None})
        return out
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, str):
                text, char, span = item.strip(), "", None
            elif isinstance(item, dict):
                text = str(item.get("quote") or item.get("text") or "").strip()
                char = str(item.get("char") or item.get("speaker") or "").strip()
                span = item.get("src_span")
            else:
                continue
            if not text:
                continue
            if isinstance(span, (list, tuple)) and len(span) == 2:
                try:
                    span = [int(span[0]), int(span[1])]
                except (TypeError, ValueError):
                    span = None
            else:
                span = None
            out.append({"char": char, "quote": text, "src_span": span})
        return out
    return []


def dialogue_text(shot: Any) -> str:
    """一镜全部台词拼成一个字符串（去空白）。无台词 → ""。"""
    return "".join(u["quote"] for u in dialogue_units(shot)).strip()


def is_dialogue_shot(shot: Any) -> bool:
    """
    这一镜是否"承载台词"。

    注意与"有说话人"的区别：**反应镜（反应特写）的 dialogue 为空但 action 写着
    『听着，眉头一皱』——它不打断对话场景**只在 wind-comic 的严格规则里才成立
    （上游明文「中间隔非对话镜则切分」）。我们照上游实现，同时用"扩展场景"补一个
    ≤1 镜的桥接用于建议级检查，见 `dialogue_coverage.detect_dialogue_scenes`。
    """
    return bool(dialogue_text(shot))


# ── 景别 ────────────────────────────────────────────────────────────────────

# 8 档景别（与 NEXT-ACTIONS #23 建议的受控词表一致）。中文/英文/缩写都收，
# 因为现有表的 `shot_size` 是自由文本（实测同一份表里只有中文，但历史表有英文）。
SIZE_CLASSES = (
    "extreme-close",
    "close",
    "medium-close",
    "medium",
    "medium-full",
    "full",
    "long",
    "unknown",
)

_SIZE_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("extreme-close", ("大特写", "极致特写", "extreme close", "extreme-close", "ecu")),
    ("close", ("特写", "近摄", "close-up", "closeup", "close up", "cu")),
    ("medium-close", ("中近景", "近景", "半身", "medium close", "medium-close", "mcu")),
    ("medium-full", ("中全景", "中远景", "medium full", "medium-full", "medium long", "mls")),
    ("medium", ("中景", "medium shot", "medium", "ms")),
    ("long", ("远景", "大远景", "极远景", "long shot", "extreme long", "extreme wide",
              "establishing", "els", "ews")),
    ("full", ("全景", "全身", "full shot", "full", "wide shot", "wide", "ws", "fs")),
]

# 上游 wide 集合：wide|long|full|establishing（`needsCloseUp` 判定用）
_WIDE = frozenset({"full", "long"})

_CLOSENESS = {
    "extreme-close": 10.0,
    "close": 9.0,
    "medium-close": 7.5,
    "medium": 5.0,
    "medium-full": 3.5,
    "full": 2.0,
    "long": 1.0,
    "unknown": 5.0,
}


def shot_size_class(size: str) -> str:
    """
    `shot_size` 自由文本 → 8 档枚举之一（认不出 → "unknown"）。

    匹配顺序**由紧到松**（先试 "extreme close" 再试 "close"），避免
    "medium close-up" 被吞进 "close"。长度相同时取先出现的档，保证确定性。
    """
    text = re.sub(r"[\s_]+", " ", str(size or "").strip().lower())
    if not text:
        return "unknown"
    best: tuple[int, str] | None = None
    for cls, pats in _SIZE_PATTERNS:
        for p in pats:
            if p in text:
                cand = (len(p), cls)
                if best is None or cand[0] > best[0]:
                    best = cand
    return best[1] if best else "unknown"


def is_wide_class(size_class: str) -> bool:
    """是否是"宽景"（全景/远景/大远景）。上游 `needsCloseUp` 只认这一个集合。"""
    return size_class in _WIDE


def closeness_score(size_class: str) -> float:
    """景别 → 0-10 的"画面压力"分（越近越高）。B2 的代理冲突分用它当一项。"""
    return _CLOSENESS.get(size_class, 5.0)


# ── I/O 辅助（只有这里碰文件；面向 CLI，纯函数不碰）─────────────────────────


def atomic_write_json(path: Path, obj: Any) -> None:
    """
    先写 `.tmp` 再 `os.replace`（与 `vm/state.py`、`vm/shots.py` 同一策略）。

    为什么审计也要原子写：`state/audit.json` 会被 UI / CI 读，
    半截 JSON 会让"质检 tab 打不开"这种问题看起来像质检本身坏了。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def locate_project(name_or_path: str, root: Path | None = None) -> Path:
    """
    项目名 / 路径 → 项目目录。`"西游记"` → `<root>/projects/西游记`。

    只做解析与存在性检查，不做别的猜测（猜错路径比报错更难查）。
    """
    p = Path(name_or_path)
    if p.is_dir() and (p / "shots").is_dir():
        return p
    base = Path(root) if root else Path.cwd()
    cand = base / "projects" / name_or_path
    if cand.is_dir():
        return cand
    raise FileNotFoundError(f"找不到项目目录：{name_or_path}（试过 {p} 与 {cand}）")


def load_raw_shots(shots_dir: Path) -> list[dict]:
    """
    读目录下全部 `*.json` **原样**返回 dict 列表（保留 schema v2 新字段）。

    排序与跳过规则与 `vm.shots.load_shots_dir` 保持一致（文件名排序、跳过 .tmp），
    这样审计看到的镜头顺序就是渲染顺序。
    """
    shots_dir = Path(shots_dir)
    rows: list[dict] = []
    for path in sorted(shots_dir.glob("*.json")):
        if path.name.startswith(".") or path.name.endswith(".tmp"):
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("shots") or []
        if not isinstance(data, list):
            raise ValueError(f"镜头表 {path} 顶层必须是数组或 {{\"shots\": [...]}}")
        for item in data:
            if isinstance(item, dict):
                rows.append(item)
    return rows
