"""
B1 · 对话覆盖度审计（移植自 wind-comic `lib/dialogue-coverage.ts`，纯函数零 LLM）。

## 上游的原话（必须原样保留，这是这个模块存在的理由）

  > 「漫剧约 **50%** 镜头是对话场景。真实导演会用『正反打』（shot/reverse）……
  >  但我们当前 Writer 输出常常**一段连续对话用 1 个 wide shot 涵盖**，
  >  显得非常『AI 一遍跑完』。**这是漫剧『AI 感』最大来源之一。**」

  解法三步，关键是**不强行让 Writer 改写**：
  ① 先 audit 对话覆盖度，找出哪个对话场景缺反打；
  ② 把 warning 展示在节奏 tab / SSE；
  ③ 再拿这份报告去约束后续生成（`buildDialogueCoverageBlock()`）。

## 检测什么

  - `needsReverseShot`（缺正反打）：**多角色**对话场景**只有 1 镜**。
  - `needsCloseUp`（缺特写）：多角色对话场景 **≥2 镜但全是 wide**（全景/远景），无近景/特写。

  `coverageScore = (多角色场景数 - 缺反打数 - 缺特写数) / 多角色场景数 × 100`

## 分组规则（上游 L122-123 注释）

  「**同 location 一定并入当前场景** —— 即使发言人切换：shot/reverse 的本质就是
   切换说话人」；「**中间隔非对话镜则切分**」。

  但对我们的镜头表有个现实问题：**schema 里没有 `location` 字段**（`vm.shots.Shot`
  只有 id/sec/chars/seed/prompt/shot_size/camera/dialogue/narration/costume）。
  所以分组分三档，并把用了哪一档**写进报告**（不许静默降级）：

  | 模式 | 触发条件 | 行为 |
  | --- | --- | --- |
  | `location` | 每一镜都有显式 `location`/`place`/`scene_id` | 上游原语义：同 key 并入，换 key 切分 |
  | `consecutive`（默认降级） | 没有显式 location | 连续对话镜并入，遇非对话镜切分（**可能把两场不同地点的对话并成一组 → 只会漏报，不会误报**） |
  | `heuristic` | 显式传 `group_by="heuristic"` | 用从提示词里猜的地点词分组。**实测在本项目上会在同一段对话内来回翻转（`well`/`temple`/`hall`），造成大量假"缺正反打" —— 默认关闭，仅作诊断。** |

## 本仓库扩展（与上游严格分开，全部为 `info`，不计入 `coverageScore`）

  - `monotone-shot-size`：≥3 个对话镜**全是同一景别**（且不是近景/特写）→ 幻灯片感。
  - `no-two-shot`：整段对话**没有任何双人同框**（空间关系只靠台词交代）。
  - `ambiguous-speaker`：一镜同框 ≥2 角色却没有 speaker 字段 → 台词归属不可判定
    （对应 schema v2 的 `dialogue: [{char, quote, src_span}]`）。

  **诚实标注**：这三条是我们加的，不是上游的；`no-two-shot` 尤其容易误报
  （一段对话之外、紧邻的三人全景陈述镜不含在内），评估见 `README.md`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from typing import Any, Sequence

from vm.audit import (
    dialogue_text,
    dialogue_units,
    field,
    is_dialogue_shot,
    is_wide_class,
    shot_size_class,
)

__all__ = [
    "DialogueScene",
    "location_key",
    "detect_dialogue_scenes",
    "audit_dialogue_coverage",
]

# 地点词表：上游 `locationKey()` 是「取 sceneDescription 首个逗号前段 + 剥掉镜头语言修饰词」，
# 对英文提示词直接照抄会把 "Cinematic realism with natural film lighting" 当地点。
# 所以这里改成**受控词表**（宁缺勿错：认不出就返回空，空 key 在 consecutive 模式下无害）。
DEFAULT_PLACE_WORDS: tuple[str, ...] = (
    # 中文（本作用中文写正文，提示词是英文，两边都收）
    "山门", "山路", "山坳", "半山腰", "破庙", "寺庙", "庙门", "庙里", "殿后", "大殿",
    "井边", "井口", "井台", "院子", "客房", "厢房", "石阶", "青石", "松林", "枯松",
    "殿", "庙", "寺", "井", "路", "院", "门", "桥", "河", "湖", "林", "山", "岩", "石",
    # 英文
    "mountain road", "mountain path", "temple gate", "temple hall", "temple courtyard",
    "courtyard", "shrine", "altar", "ravine", "ridge", "temple", "hall", "well",
    "gate", "path", "road", "bridge", "river", "forest", "pine", "chamber", "room",
    "door", "rock", "cliff", "cave",
)

# 提示词的固定风格前缀（本仓库 plan.py 生成）：注释里出现过，剥离后再找地点词
_STYLE_PREFIX_RE = re.compile(r"^\s*Cinematic realism[^.]*\.\s*", re.IGNORECASE)
_SHOT_TAG_RE = re.compile(r"\[Shot\s*\d+\]\s*", re.IGNORECASE)
_LOCATION_FIELDS = ("location", "place", "scene_location", "location_id", "scene_id")
_SCENE_TEXT_FIELDS = ("scene_description", "scene", "action")


def _prompt_scene_text(shot: Any) -> str:
    """从提示词里取出"场景描述"（detailed_description 正文，剥掉风格前缀与 [Shot N]）。"""
    prompt = str(field(shot, "prompt", "") or "")
    body = prompt.split("detailed_description:")[-1]
    body = _STYLE_PREFIX_RE.sub("", body.strip())
    body = _SHOT_TAG_RE.sub("", body)
    return body.strip()


def location_key(shot: Any, vocabulary: Sequence[str] = DEFAULT_PLACE_WORDS) -> tuple[str, str]:
    """
    这一镜的地点键 → `(key, source)`，`source ∈ {explicit, scene-text, prompt, none}`。

    `source` 是**报告的一部分**：只有每一镜都是 `explicit` 时才敢用上游的
    "换地点就切分场景"语义（否则会把一段对话切成 1 镜一段 → 满屏假警报）。
    """
    for name in _LOCATION_FIELDS:
        v = field(shot, name, None)
        if isinstance(v, (str, int)) and str(v).strip():
            return str(v).strip(), "explicit"
    for name in _SCENE_TEXT_FIELDS:
        v = field(shot, name, None)
        if isinstance(v, str) and v.strip():
            key = _match_place(v, vocabulary)
            if key:
                return key, "scene-text"
    key = _match_place(_prompt_scene_text(shot), vocabulary)
    if key:
        return key, "prompt"
    return "", "none"


def _match_place(text: str, vocabulary: Sequence[str]) -> str:
    """
    在文本里找**最早出现**的地点词；同一位置取更长的词（"temple gate" 胜过 "temple"）。

    只看前两个子句（逗号/冒号/句号切）：一处地点如果整段描述里到处出现，
    那它就不是"这一镜在哪"，而只是环境提到（实测"井"会在井边的每一镜反复出现）。
    """
    if not text:
        return ""
    head = re.split(r"[，,。；;：:]", text, maxsplit=2)
    head_text = "，".join(head[:2]).lower()
    best: tuple[int, int, str] | None = None  # (位置, 词长, 词)，位置优先
    for word in vocabulary:
        pos = head_text.find(word.lower())
        if pos < 0:
            continue
        cand = (pos, -len(word), word)
        if best is None or cand < best:
            best = cand
    return best[2] if best else ""


# ── 场景分组 ────────────────────────────────────────────────────────────────


@dataclass
class DialogueScene:
    """一个对话场景（连续对话镜的分组结果）。`shots` 是**表内 1-based 镜号**。"""

    index: int
    shots: list[int]
    ids: list[str]
    chars: list[str] = dc_field(default_factory=list)
    speakers: list[str] = dc_field(default_factory=list)
    size_classes: list[str] = dc_field(default_factory=list)
    locations: list[str] = dc_field(default_factory=list)
    location: str = ""
    location_source: str = "none"
    bridged: int = 0  # 组内被"桥接"的非对话镜数（扩展场景才有）

    @property
    def dialogue_shots(self) -> list[int]:
        """组内真正承载台词的镜号（扩展场景会夹着反应镜）。"""
        return list(self.shots) if self.bridged == 0 else self._dialogue_only

    _dialogue_only: list[int] = dc_field(default_factory=list)

    @property
    def is_multi_char(self) -> bool:
        """多角色对话（上游 `multiChar` 的定义：场景内登场角色 >1）。"""
        return len(self.chars) >= 2

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "shots": list(self.shots),
            "ids": list(self.ids),
            "chars": list(self.chars),
            "speakers": list(self.speakers),
            "size_classes": list(self.size_classes),
            "location": self.location,
            "location_source": self.location_source,
            "bridged_non_dialogue": self.bridged,
            "is_multi_char": self.is_multi_char,
        }


def _scene_from(shots: Sequence[Any], indices: list[int], *, index: int, bridged: int,
                dialogue_only: list[int], vocabulary: Sequence[str]) -> DialogueScene:
    chars: list[str] = []
    speakers: list[str] = []
    sizes: list[str] = []
    locs: list[str] = []
    ids: list[str] = []
    for i in indices:
        s = shots[i - 1]
        for c in (field(s, "chars", []) or []):
            if isinstance(c, str) and c.strip() and c.strip() not in chars:
                chars.append(c.strip())
        for u in dialogue_units(s):
            if u["char"] and u["char"] not in speakers:
                speakers.append(u["char"])
        sizes.append(shot_size_class(str(field(s, "shot_size", "") or "")))
        loc, _src = location_key(s, vocabulary)
        locs.append(loc)
        ids.append(str(field(s, "id", "") or f"#{i}"))
    key, src = location_key(shots[indices[0] - 1], vocabulary)
    return DialogueScene(
        index=index,
        shots=list(indices),
        ids=ids,
        chars=chars,
        speakers=speakers,
        size_classes=sizes,
        locations=locs,
        location=key,
        location_source=src,
        bridged=bridged,
        _dialogue_only=list(dialogue_only),
    )


def detect_dialogue_scenes(
    shots: Sequence[Any],
    *,
    group_by: str = "auto",
    vocabulary: Sequence[str] = DEFAULT_PLACE_WORDS,
    bridge_non_dialogue: int = 0,
) -> tuple[list[DialogueScene], dict]:
    """
    把连续对话镜分组。返回 `(scenes, meta)`，`meta` 里写明用了哪种分组模式。

    `bridge_non_dialogue=N`：允许组内夹 N 个非对话镜（**仅扩展场景用**；
    上游语义是 `N=0`）。实测一个反应镜（`dialogue=""` 但 action 写"听着"）
    就能把一段对话劈成两半，所以建议级检查需要 `N=1` 的视图。
    """
    shots = list(shots)
    meta: dict[str, Any] = {"requested": group_by, "location_sources": {}, "warnings": []}

    dialogue_idx = [i for i, s in enumerate(shots, 1) if is_dialogue_shot(s)]
    if not dialogue_idx:
        return [], {**meta, "mode": "consecutive", "dialogue_shots": 0}

    explicit = 0
    for i in dialogue_idx:
        _k, src = location_key(shots[i - 1], vocabulary)
        meta["location_sources"][src] = meta["location_sources"].get(src, 0) + 1
        if src == "explicit":
            explicit += 1

    mode = str(group_by or "auto").lower()
    if mode == "auto":
        mode = "location" if explicit == len(dialogue_idx) else "consecutive"
    if mode == "location" and explicit != len(dialogue_idx):
        meta["warnings"].append(
            f"group_by='location' 但只有 {explicit}/{len(dialogue_idx)} 个对话镜有显式 location："
            "缺 location 的镜头会与任意相邻组并入，可能把两场对话并成一组（漏报）"
        )
    meta["mode"] = mode
    meta["bridge_non_dialogue"] = bridge_non_dialogue

    scenes: list[DialogueScene] = []
    cur: list[int] = []
    cur_dialogue: list[int] = []
    pending: list[int] = []  # 被"桥接"进来的非对话镜（扩展场景专用）
    gap = 0

    def close() -> None:
        nonlocal cur, cur_dialogue, pending, gap
        if cur and cur_dialogue:
            scenes.append(
                _scene_from(
                    shots, cur, index=len(scenes) + 1, bridged=len(cur) - len(cur_dialogue),
                    dialogue_only=cur_dialogue, vocabulary=vocabulary,
                )
            )
        cur, cur_dialogue, pending, gap = [], [], [], 0

    for i, s in enumerate(shots, 1):
        if not is_dialogue_shot(s):
            gap += 1
            if gap > bridge_non_dialogue:
                close()
            else:
                pending.append(i)  # 允许桥接的间隙镜：先把镜号记下，合并时一起并进场景
            continue
        if cur:
            compatible = True
            if mode in ("location", "heuristic"):
                key, src = location_key(s, vocabulary)
                # 只有可信的 key 才允许"换地点就切分"：heuristic 模式用启发式 key
                key_ok = bool(key) and (mode == "heuristic" or src == "explicit")
                if key_ok and cur_key and key != cur_key:
                    compatible = False
            if gap > bridge_non_dialogue:
                compatible = False
            if not compatible:
                close()
        if not cur:
            cur_key, _ = location_key(s, vocabulary)
            if mode == "consecutive":
                cur_key = ""
        cur.extend(pending)
        pending = []
        cur.append(i)
        cur_dialogue.append(i)
        gap = 0
    close()
    return scenes, meta


# ── 审计 ────────────────────────────────────────────────────────────────────


def _hint_reverse(scene: DialogueScene, shots: Sequence[Any]) -> str:
    speaker = scene.speakers[0] if scene.speakers else (scene.chars[0] if scene.chars else "说话人")
    other = next((c for c in scene.chars if c != speaker), "对方")
    return (
        f"在第 {scene.shots[0]} 镜之后插 1 镜：听众「{other}」的反应镜（近景/特写，听的表情）；"
        f"「{speaker}」的台词改成画外音继续，切回后再回到 {speaker}。"
        f"同一地点一段 2 人对话至少 3 切（A 说 → B 反应 → A 再说）。"
    )


def _hint_close_up(scene: DialogueScene, shots: Sequence[Any]) -> str:
    keep = scene.shots[0]
    return (
        f"第 {scene.shots[0]}~{scene.shots[-1]} 镜全是全景/远景，缺微表情张力："
        f"保留第 {keep} 镜作交代镜头，把其余镜头改成近景或特写"
        f"（正反打：说话人近景 ↔ 听众反应近景），同一 location 内构图轴线保持一致。"
    )


def audit_dialogue_coverage(
    shots: Sequence[Any],
    *,
    group_by: str = "auto",
    vocabulary: Sequence[str] = DEFAULT_PLACE_WORDS,
    bridge_non_dialogue: int = 1,
) -> dict:
    """
    B1 主入口。返回可直接进 `audit.json` 的 dict（含 `findings` / `coverage_score`）。

    `bridge_non_dialogue` 只影响**建议级**检查（扩展场景视图）；两个上游检查
    严格用 `bridge=0` 的原始分组，保证 `coverageScore` 与上游同口径。
    """
    shots = list(shots)
    strict, meta = detect_dialogue_scenes(
        shots, group_by=group_by, vocabulary=vocabulary, bridge_non_dialogue=0
    )
    extended, _ = detect_dialogue_scenes(
        shots, group_by=group_by, vocabulary=vocabulary, bridge_non_dialogue=bridge_non_dialogue
    )

    findings: list[dict] = []

    def add(code: str, severity: str, scene: DialogueScene | None, message: str, hint: str,
            shots_list: list[int] | None = None, chars: list[str] | None = None) -> None:
        findings.append(
            {
                "code": code,
                "severity": severity,
                "module": "B1",
                "ext": code not in ("needs-reverse-shot", "wide-only-dialogue"),
                "scene": scene.index if scene else None,
                "from_shot": (shots_list or (scene.shots if scene else [None]))[0],
                "to_shot": (shots_list or (scene.shots if scene else [None]))[-1],
                "shots": list(shots_list or (scene.shots if scene else [])),
                "chars": list(chars if chars is not None else (scene.chars if scene else [])),
                "message": message,
                "rewrite_hint": hint,
            }
        )

    multi_char = [s for s in strict if s.is_multi_char]
    needs_reverse: list[dict] = []
    needs_close: list[dict] = []

    for scene in multi_char:
        if len(scene.shots) == 1:
            msg = (
                f"第 {scene.shots[0]} 镜（{scene.ids[0]}）：{ '/'.join(scene.chars) } 的对话场景"
                f"只有 1 镜，缺正反打 —— 真实导演会切到对方的反应。"
            )
            add("needs-reverse-shot", "warning", scene, msg, _hint_reverse(scene, shots))
            needs_reverse.append(scene.shots[0])
        elif all(is_wide_class(c) for c in scene.size_classes):
            msg = (
                f"第 {scene.shots[0]}~{scene.shots[-1]} 镜（{len(scene.shots)} 镜）"
                f"{ '/'.join(scene.chars) } 的对话全是全景/远景，无近景或特写 —— 缺微表情张力。"
            )
            add("wide-only-dialogue", "warning", scene, msg, _hint_close_up(scene, shots))
            needs_close.append(scene.shots[0])

    # ── 本仓库扩展（info）──────────────────────────────────────────────────
    for scene in strict:
        if len(scene.shots) < 3:
            continue
        known = [c for c in scene.size_classes if c != "unknown"]
        if known and len(set(known)) == 1 and known[0] not in ("close", "extreme-close"):
            a, b = scene.shots[0], scene.shots[-1]
            add(
                "monotone-shot-size",
                "info",
                scene,
                f"第 {a}~{b} 镜连续 {len(scene.shots)} 个对话镜全是「{known[0]}」，"
                f"没有一次景别变化（幻灯片感）。",
                f"把第 {a}~{b} 镜里的 1-2 镜改成近景或特写（正反打），"
                f"或在中间插 1 个反应特写；其余保持「{known[0]}」建立基线。",
            )

    for scene in extended:
        if len(scene.shots) < 2 or not scene.is_multi_char:
            continue
        two_shots = [i for i in scene.shots if len(field(shots[i - 1], "chars", []) or []) >= 2]
        if not two_shots:
            a, b = scene.shots[0], scene.shots[-1]
            add(
                "no-two-shot",
                "info",
                scene,
                f"第 {a}~{b} 镜（{ '/'.join(scene.chars) }）整段对话没有任何双人同框镜头："
                f"空间关系只靠台词交代。",
                f"考虑在第 {a}~{b} 镜之间插 1 个中景双人镜（two-shot / 过肩），"
                f"确立两人的位置与距离，再切回单人正反打。",
            )

    for i, s in enumerate(shots, 1):
        chars = [c for c in (field(s, "chars", []) or []) if isinstance(c, str) and c.strip()]
        if len(chars) < 2 or not is_dialogue_shot(s):
            continue
        if any(u["char"] for u in dialogue_units(s)):
            continue
        add(
            "ambiguous-speaker",
            "info",
            None,
            f"第 {i} 镜同框 {len(chars)} 个角色（{ '/'.join(chars) }）但台词没有 speaker 字段："
            f"「{dialogue_text(s)[:18]}…」归属不可判定，成片会出现「谁在说话」歧义。",
            f"把第 {i} 镜升级为 schema v2 的 dialogue: [{{char, quote, src_span}}]，"
            f"写明谁在说这句（配音选音色、审计判正反打都要它）。",
            shots_list=[i],
            chars=chars,
        )

    total = len(multi_char)
    score = 100.0 if total == 0 else (total - len(needs_reverse) - len(needs_close)) / total * 100.0

    return {
        "module": "B1",
        "grouping": meta,
        "scene_count": len(strict),
        "dialogue_shot_count": sum(1 for s in shots if is_dialogue_shot(s)),
        "shot_count": len(shots),
        "multi_char_scene_count": total,
        "coverage_score": round(score, 1),
        "needs_reverse_shot_shots": needs_reverse,
        "needs_close_up_shots": needs_close,
        "scenes": [s.to_dict() for s in strict],
        "extended_scenes": [s.to_dict() for s in extended],
        "findings": findings,
    }
