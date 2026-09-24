"""
B2 · 节奏 / 冲突形状审计（移植自 wind-comic `lib/pacing-audit-v2.ts`，纯函数零 LLM）。

## 上游的动机（这是全模块最该保留的一段）

  > 「**平均分会把『高开低走』和『层层递进』算成同一个数**」
  > 「报『平均 5.2 分』没用，要指出**第几到第几镜是观众划走的地方**」
  > 「完播率由前段决定」

  `[1,2,4,9]` 与 `[9,4,2,1]` 的平均都是 4，但形状完全不同：前者层层递进（escalating），
  后者高开低走（front-loaded）。所以这里**只看形状，不看平均分**：

  | 函数 | 判定 | 阈值 |
  | --- | --- | --- |
  | `analyze_conflict_shape` | 最小二乘斜率 + 峰值显著度 | `peak_prominence < 1.5` → `no-climax`；`slope < -0.15` → `front-loaded`；`slope > 0.15` → `escalating`；否则 `flat` |
  | `find_drag_segments` | **连续 ≥3 镜**低于阈值 → `{fromShot, toShot, length, avg}` | 短剧 4 分 / 普通 3 分 |
  | `audit_opening` | 前 1/3（至少 2 镜、至多 5 镜）平均密度 | 短剧 5 分 / 普通 3.5 分 |
  | `audit_duration_rhythm` | 时长变异系数 `cv = std/mean` | `cv < 0.12` → **呆板**；连续 ≥3 个 >均值×1.4 的长镜 → 拖沓 |

## 输入问题：我们没有 `conflict` 字段

上游的 `scores[]` 来自 LLM 逐镜冲突分。我们的镜头表没有这个字段，所以：

  **(b) 先用确定性代理指标（默认，已实现）** —— 零依赖、零 token、立刻能跑。
      5 个分量，全部可在报告里逐项复核（不许出现「黑盒打分」）：

      | 分量 | 权重 | 定义 |
      | --- | --- | --- |
      | `pace` | 0.25 | 镜头越短 → 节奏越快：`10×(15-sec)/12` |
      | `speech` | 0.20 | 台词密度：`min(10, (台词字数/sec)/4字每秒 ×10)` |
      | `closeness` | 0.20 | 景别越近 → 画面压力越大（中景 5 / 近景 7.5 / 特写 9 / 全景 2 / 远景 1） |
      | `cast` | 0.15 | 同框角色数 0→2 / 1→4 / 2→7 / ≥3→10（人多 = 冲突潜力大） |
      | `action` | 0.20 | 提示词里的**动作动词**数 − 0.5×静态动词数（词表见 `MOTION_VERBS`），映射到 0-10 |

  **(a) 让 `plan.py` 拆镜时顺带输出 `conflict` 分（可选接口，已留）** ——
      `intensity_series(mode="auto")` 会优先消费镜头上已有的
      `conflict` / `conflict_score` / `intensity` 字段（复用同一次 LLM 调用 → 零额外 token）。
      → **推荐 Lead 后续给 `vm/plan.py` 加上这个字段**：本模块的实测结论
      （见 `README.md`「诚实评估」）是**代理分区分度不足**（σ≈0.8/10），
      形状判定只能当参考；显式 conflict 分才让 B2 达到上游的判别力。

## 严重度策略（诚实优先）

  代理分驱动的形状判定一律 `info`（低置信度），只有**显式 conflict 分**驱动的才升到
  `warning`。理由：把「σ=0.8 的代理分」当成「证据」来说事，正是本项目反对的
  「文档承诺 > 代码实现」。
"""

from __future__ import annotations

import re
import statistics
from typing import Any, Sequence

from vm.audit import closeness_score, dialogue_text, field, shot_size_class
from vm.shots import CHARS_PER_SEC, SEC_MAX, SEC_MIN, speech_chars

__all__ = [
    "PROXY_WEIGHTS",
    "proxy_scores",
    "score_breakdown",
    "intensity_series",
    "least_squares_slope",
    "analyze_conflict_shape",
    "find_drag_segments",
    "audit_opening",
    "audit_duration_rhythm",
    "weakest_window",
    "audit_pacing",
]

# ── 代理冲突分 ──────────────────────────────────────────────────────────────

PROXY_WEIGHTS: dict[str, float] = {
    "pace": 0.25,
    "speech": 0.20,
    "closeness": 0.20,
    "cast": 0.15,
    "action": 0.20,
}

# 动作动词词表：只收「能被视频引擎执行的动词链」（上游对 action 的硬要求），
# 静态动词用来扣分。刻意保持小而可读 —— 词表越大越像玄学，越难解释判定。
MOTION_VERBS = re.compile(
    r"\b(springs?|leaps?|lunges?|hurls?|strikes?|slams?|sweeps?|charges?|dashes|bursts?|"
    r"crashes|smashes|grabs?|seizes|thrusts?|swings?|kicks?|shoves|wrestl\w*|chases?|"
    r"dodges|rolls?|sprints?|rushes|climbs?|jumps?|splash\w*|explod\w*|erupt\w*|"
    r"shatter\w*|bares|snarls?|roars?|shouts?|growls?|raises?|lifts?|drops?|turns?)\b",
    re.IGNORECASE,
)
STILL_VERBS = re.compile(
    r"\b(sits?|seated|stands?|standing|waits?|watches?|gazes?|looks?|remains?|lingers?|"
    r"rests?|kneels?|bows?|lies|sleeps?|walks?)\b",
    re.IGNORECASE,
)
_STYLE_PREFIX_RE = re.compile(r"^\s*Cinematic realism[^.]*\.\s*", re.IGNORECASE)
_SHOT_TAG_RE = re.compile(r"\[Shot\s*\d+\]\s*", re.IGNORECASE)

EXPLICIT_SCORE_KEYS = ("conflict", "conflict_score", "intensity", "intensity_score")


def _clamp(v: float, lo: float = 0.0, hi: float = 10.0) -> float:
    return max(lo, min(hi, v))


def pace_score(sec: float) -> float:
    """镜头越短节奏越快（3s→10 分，15s→0 分）。"""
    try:
        s = float(sec)
    except (TypeError, ValueError):
        return 5.0
    return _clamp(10.0 * (SEC_MAX - s) / float(SEC_MAX - SEC_MIN))


def speech_score(dialogue: str, narration: str, sec: float) -> float:
    """台词密度（4 字/秒为满档）。无台词 → 0（安静镜不是高冲突镜）。"""
    try:
        s = float(sec)
    except (TypeError, ValueError):
        return 0.0
    if s <= 0:
        return 0.0
    n = speech_chars(dialogue, narration)
    if not n:
        return 0.0
    return _clamp((n / s) / CHARS_PER_SEC * 10.0)


def cast_score(n: int) -> float:
    """同框角色数 → 冲突潜力分。"""
    if n <= 0:
        return 2.0
    if n == 1:
        return 4.0
    if n == 2:
        return 7.0
    return 10.0


def action_text(shot: Any) -> str:
    """取「动作描述」文本：优先显式 `action`，否则提示词的 detailed_description 正文。"""
    act = field(shot, "action", None)
    if isinstance(act, str) and act.strip():
        return act
    prompt = str(field(shot, "prompt", "") or "")
    body = prompt.split("detailed_description:")[-1]
    body = _STYLE_PREFIX_RE.sub("", body.strip())
    return _SHOT_TAG_RE.sub("", body)


def action_score(text: str) -> float:
    """
    动作分：`clamp(5 + (动作动词数 − 0.5×静态动词数) × 2.5, 0, 10)`。

    为什么减静态动词：`Close on <Subject 1> crouched low ... as he looks back` 这类
    「看着/站着」的镜头就是划走高发段，正该给低分；只看动作动词数会把它们和打斗镜拉平。
    """
    if not text:
        return 5.0
    mix = len(MOTION_VERBS.findall(text)) - 0.5 * len(STILL_VERBS.findall(text))
    return _clamp(5.0 + mix * 2.5)


def score_breakdown(shots: Sequence[Any]) -> list[dict]:
    """逐镜 5 个分量 + 加权总分（报告里逐项可复核，不许黑盒）。"""
    out: list[dict] = []
    for i, s in enumerate(shots, 1):
        sec = field(s, "sec", 0) or 0
        dialogue = dialogue_text(s)
        narration = str(field(s, "narration", "") or "")
        chars_n = len([c for c in (field(s, "chars", []) or []) if c])
        comp = {
            "pace": round(pace_score(sec), 3),
            "speech": round(speech_score(dialogue, narration, sec), 3),
            "closeness": round(
                closeness_score(shot_size_class(str(field(s, "shot_size", "") or ""))), 3
            ),
            "cast": round(cast_score(chars_n), 3),
            "action": round(action_score(action_text(s)), 3),
        }
        out.append(
            {
                "shot": i,
                "id": str(field(s, "id", "") or f"#{i}"),
                "sec": sec,
                "total": round(sum(PROXY_WEIGHTS[k] * v for k, v in comp.items()), 3),
                **comp,
            }
        )
    return out


def proxy_scores(shots: Sequence[Any], *, weights: dict[str, float] | None = None) -> list[float]:
    """5 分量加权 → 每镜 0-10 的代理冲突分。"""
    w = dict(PROXY_WEIGHTS if weights is None else weights)
    return [round(sum(w[k] * row[k] for k in w), 3) for row in score_breakdown(shots)]


def intensity_series(
    shots: Sequence[Any], *, mode: str = "auto", keys: Sequence[str] = EXPLICIT_SCORE_KEYS
) -> tuple[list[float], dict]:
    """
    取「每镜冲突分」序列。返回 `(scores, meta)`。

    `mode`：`auto`（有显式分就用，否则代理）/ `proxy` / `explicit`（缺分即报错）。
    显式分若全部 ≤1（归一化写法）会 ×10 并在 `meta` 里显式记 `rescaled_from_unit`。
    """
    mode = str(mode or "auto").lower()
    shots = list(shots)
    explicit: list[float] = []
    used_key = ""
    missing = 0
    for s in shots:
        v: float | None = None
        for k in keys:
            raw = field(s, k, None)
            if isinstance(raw, bool) or raw is None:
                continue
            if isinstance(raw, (int, float)):
                v = float(raw)
                used_key = used_key or k
                break
        if v is None:
            missing += 1
        else:
            explicit.append(v)

    if mode == "explicit" and (missing or not shots):
        raise ValueError(
            f"mode='explicit' 但只有 {len(explicit)}/{len(shots)} 个镜头有冲突分字段"
            f"（{'/'.join(keys)}）"
        )
    if mode in ("auto", "explicit") and shots and not missing:
        meta = {"mode": "explicit", "key": used_key, "shots_scored": len(shots)}
        scores = list(explicit)
        if max(scores) <= 1.0:
            scores = [v * 10.0 for v in scores]
            meta["rescaled_from_unit"] = True
        return [round(_clamp(v), 3) for v in scores], meta
    if mode == "explicit":
        raise ValueError("mode='explicit' 但镜头表里没有可用的冲突分字段")

    return proxy_scores(shots), {"mode": "proxy", "key": "", "shots_scored": len(shots)}


# ── 形状分析 ────────────────────────────────────────────────────────────────


def least_squares_slope(values: Sequence[float]) -> float:
    """
    最小二乘斜率（x = 镜序 0..n-1）。n<2 → 0.0。

    不用 `statistics.linear_regression`：3.10 没有，而且这里必须**确定性**、零第三方依赖。
    """
    vals = [float(v) for v in values]
    n = len(vals)
    if n < 2:
        return 0.0
    mx = (n - 1) / 2.0
    my = sum(vals) / n
    den = sum((x - mx) ** 2 for x in range(n))
    if den == 0:
        return 0.0
    return sum((x - mx) * (v - my) for x, v in enumerate(vals)) / den


def analyze_conflict_shape(
    scores: Sequence[float],
    *,
    no_climax_prominence: float = 1.5,
    slope_threshold: float = 0.15,
) -> dict:
    """
    冲突曲线**形状**判定（不是平均分）。

    峰值显著度 `peak_prominence = max − mean`（上游定义）。
    判定顺序与上游一致：先看有没有峰，再看斜率正负。
    """
    vals = [float(v) for v in scores]
    if not vals:
        return {
            "shape": "empty", "n": 0, "slope": 0.0, "mean": 0.0, "std": 0.0,
            "peak_value": 0.0, "peak_index": None, "peak_prominence": 0.0,
            "borderline": False,
            "thresholds": {"no_climax_prominence": no_climax_prominence,
                           "slope": slope_threshold},
        }
    mean = sum(vals) / len(vals)
    std = statistics.pstdev(vals) if len(vals) > 1 else 0.0
    slope = least_squares_slope(vals)
    peak_value = max(vals)
    peak_index = vals.index(peak_value) + 1  # 1-based 镜号
    prominence = peak_value - mean

    if prominence < no_climax_prominence:
        shape = "no-climax"
    elif slope < -slope_threshold:
        shape = "front-loaded"
    elif slope > slope_threshold:
        shape = "escalating"
    else:
        shape = "flat"

    return {
        "shape": shape,
        "n": len(vals),
        "slope": round(slope, 4),
        "mean": round(mean, 3),
        "std": round(std, 3),
        "peak_value": round(peak_value, 3),
        "peak_index": peak_index,
        "peak_prominence": round(prominence, 3),
        "borderline": abs(prominence - no_climax_prominence) <= 0.25,
        "thresholds": {
            "no_climax_prominence": no_climax_prominence,
            "slope": slope_threshold,
        },
    }


def find_drag_segments(
    scores: Sequence[float], *, threshold: float = 4.0, min_run: int = 3
) -> list[dict]:
    """
    连续 ≥`min_run` 镜低于 `threshold` → 拖沓段。返回 `[{from_shot,to_shot,length,avg_score}]`。

    **这就是「第几到第几镜是观众划走的地方」的答案**（上游 actionable[] 的原话）。
    注意区间是**闭区间镜号**、`min_run` 是"连续"而不是"总计"。
    """
    vals = [float(v) for v in scores]
    segs: list[dict] = []
    run: list[int] = []
    for i, v in enumerate(vals, 1):
        if v < threshold:
            run.append(i)
            continue
        if len(run) >= min_run:
            segs.append(_seg(run, vals))
        run = []
    if len(run) >= min_run:
        segs.append(_seg(run, vals))
    return segs


def _seg(run: list[int], vals: Sequence[float]) -> dict:
    return {
        "from_shot": run[0],
        "to_shot": run[-1],
        "length": len(run),
        "avg_score": round(sum(vals[i - 1] for i in run) / len(run), 3),
        "shots": list(run),
    }


def find_long_runs(
    secs: Sequence[float], *, long_factor: float = 1.4, min_run: int = 3
) -> list[dict]:
    """连续 ≥`min_run` 个「长于均值×long_factor」的镜头 → 长镜堆叠（拖沓形态）。"""
    vals = [float(s) for s in secs]
    if not vals:
        return []
    mean = sum(vals) / len(vals)
    limit = mean * long_factor
    runs: list[dict] = []
    run: list[int] = []
    for i, v in enumerate(vals, 1):
        if v > limit:
            run.append(i)
            continue
        if len(run) >= min_run:
            runs.append(_seg(run, vals))
        run = []
    if len(run) >= min_run:
        runs.append(_seg(run, vals))
    return runs


def weakest_window(scores: Sequence[float], *, size: int = 3) -> dict | None:
    """
    全片**最低洼的 `size` 镜窗口**（滑动窗口，起点最早者优先）。

    「报平均 5.2 分没用」—— 即使没有低于绝对阈值的拖沓段，也要能回答
    「如果只改一处，改哪里」。这条在没有拖沓段时尤其有用。
    """
    vals = [float(v) for v in scores]
    size = max(1, int(size))
    if len(vals) < size:
        return None
    best: tuple[float, int] | None = None
    for start in range(0, len(vals) - size + 1):
        avg = sum(vals[start : start + size]) / size
        if best is None or avg < best[0] - 1e-12:
            best = (avg, start)
    assert best is not None
    avg, start = best
    return {
        "from_shot": start + 1,
        "to_shot": start + size,
        "length": size,
        "avg_score": round(avg, 3),
        "overall_mean": round(sum(vals) / len(vals), 3),
    }


def audit_opening(
    scores: Sequence[float], *, mode: str = "drama", fraction: float = 1.0 / 3.0,
    min_shots: int = 2, max_shots: int = 5,
) -> dict:
    """
    开场密度：取前 1/3，至少 `min_shots` 镜、至多 `max_shots` 镜（上游原规则）。

    「**完播率由前段决定**」→ 开场低于 `min_avg` 是最该修的。
    """
    vals = [float(v) for v in scores]
    n = len(vals)
    k = int(n * fraction)
    k = max(min_shots, min(max_shots, k))
    k = min(k, n) if n else 0
    avg = sum(vals[:k]) / k if k else 0.0
    min_avg = 5.0 if str(mode).lower() in ("drama", "短剧", "duanju") else 3.5
    return {
        "window": k,
        "avg_score": round(avg, 3),
        "min_avg": min_avg,
        "mode": mode,
        "passed": bool(k and avg >= min_avg),
    }


def audit_duration_rhythm(
    secs: Sequence[float], *, min_cv: float = 0.12, long_factor: float = 1.4, min_run: int = 3
) -> dict:
    """
    时长节奏：`cv = std/mean < min_cv` → **呆板**（全程一个时长 = 幻灯片）；
    另外找「连续 ≥3 个 >均值×1.4 的长镜」→ 拖沓。
    """
    vals = [float(s) for s in secs]
    if not vals:
        return {"n": 0, "mean": 0.0, "std": 0.0, "cv": 0.0, "min_cv": min_cv,
                "monotone": False, "cv_ratio": None, "long_runs": []}
    mean = sum(vals) / len(vals)
    std = statistics.pstdev(vals) if len(vals) > 1 else 0.0
    cv = (std / mean) if mean else 0.0
    return {
        "n": len(vals),
        "mean": round(mean, 3),
        "std": round(std, 3),
        "cv": round(cv, 4),
        "min_cv": min_cv,
        "monotone": bool(cv < min_cv),
        "cv_ratio": round(cv / min_cv, 3) if min_cv else None,
        "long_runs": find_long_runs(vals, long_factor=long_factor, min_run=min_run),
    }


# ── 主入口 ──────────────────────────────────────────────────────────────────


def audit_pacing(
    shots: Sequence[Any],
    *,
    mode: str = "auto",
    drag_threshold: float = 4.0,
    min_run: int = 3,
    min_cv: float = 0.12,
    opening_mode: str = "drama",
    relative_sigma: float = 1.0,
) -> dict:
    """
    B2 主入口。返回可直接进 `audit.json` 的 dict。

    `relative_sigma`：除上游的**绝对阈值**外，另按本片自身分布算一条校准阈值
    `mean − σ×relative_sigma` —— 因为代理分的尺度由我们自己定，
    直接用上游「4 分」既可能全不触发、也可能全触发。两条都报，人自己判断。
    """
    shots = list(shots)
    scores, source = intensity_series(shots, mode=mode)
    secs = [float(field(s, "sec", 0) or 0) for s in shots]

    shape = analyze_conflict_shape(scores)
    drag = {
        "threshold": drag_threshold,
        "min_run": min_run,
        "segments": find_drag_segments(scores, threshold=drag_threshold, min_run=min_run),
    }
    mean = float(shape.get("mean") or 0.0)
    std = float(shape.get("std") or 0.0)
    calibrated = round(max(0.0, mean - std * float(relative_sigma)), 3)
    relative = {
        "threshold": calibrated,
        "min_run": min_run,
        "segments": find_drag_segments(scores, threshold=calibrated, min_run=min_run),
    }
    weakest = weakest_window(scores, size=min_run)
    opening = audit_opening(scores, mode=opening_mode)
    duration = audit_duration_rhythm(secs, min_cv=min_cv, min_run=min_run)

    proxy_mode = source.get("mode") == "proxy"
    findings: list[dict] = []

    def add(code: str, severity: str, message: str, hint: str,
            shots_list: list[int] | None = None) -> None:
        findings.append(
            {
                "code": code,
                "severity": severity,
                "module": "B2",
                "ext": False,
                "from_shot": (shots_list or [None])[0],
                "to_shot": (shots_list or [None])[-1],
                "shots": list(shots_list or []),
                "message": message,
                "rewrite_hint": hint,
            }
        )

    if shape["shape"] == "no-climax":
        add(
            "no-climax",
            "info" if proxy_mode else "warning",
            f"冲突曲线无显著峰值：峰值 {shape['peak_value']} 在第 {shape['peak_index']} 镜，"
            f"显著度仅 {shape['peak_prominence']}（阈值 ≥1.5）。全片没有一个「该炸」的点。"
            + ("（代理分判定，低置信度）" if proxy_mode else ""),
            "把最强的那个反转/冲突往后压到 2/3 处并放大（加台词、加动作、缩短镜头），"
            "让曲线出现一个明确峰值；不要靠「平均分还行」自我安慰。",
        )
    elif shape["shape"] == "front-loaded":
        add(
            "front-loaded",
            "info" if proxy_mode else "warning",
            f"冲突曲线高开低走：最小二乘斜率 {shape['slope']}（阈值 <−0.15），"
            f"峰值在第 {shape['peak_index']} 镜而全片共 {shape['n']} 镜。",
            "把开头的强冲突后移或分散：开场留 1 个钩子即可，中段按"
            "「铺垫 → 升级 → 反转」补密度，别把最好的一手打光再走下坡路。",
        )
    elif shape["shape"] == "flat":
        add(
            "flat-shape",
            "info",
            f"冲突曲线平坦：斜率 {shape['slope']}（|slope| ≤0.15）、"
            f"峰值显著度 {shape['peak_prominence']} → 既非递进也非高开低走。",
            "给中后段安排一个明确的升级点（信息反转 / 动作冲突 / 台词压迫），"
            "否则观众感受不到「越来越紧张」。",
        )

    if shape.get("borderline"):
        add(
            "shape-borderline",
            "info",
            f"形状判定处于阈值边缘（显著度 {shape['peak_prominence']} vs 阈值 1.5，"
            f"σ={shape['std']}）：这个结论**不可靠**。",
            "让 vm/plan.py 拆镜时顺带输出逐镜 conflict 分（复用同一次 LLM 调用、零额外 token），"
            "再用 --scores-mode explicit 复核 B2 —— 代理分的区分度不足以定论。",
        )

    if proxy_mode and std < 1.0:
        add(
            "proxy-low-discrimination",
            "info",
            f"代理冲突分区分度低：σ={shape['std']}/10（mean={shape['mean']}）。"
            "镜头时长/景别高度同质时，代理分算不出形状。",
            "这不是「片子没问题」，而是「这个指标看不出来」。要真正判定节奏形状，"
            "需要 plan.py 的显式 conflict 分（见 README 的接口约定）。",
        )

    for seg in drag["segments"]:
        add(
            "drag-segment",
            "warning",
            f"第 {seg['from_shot']}~{seg['to_shot']} 镜连续 {seg['length']} 镜低冲突"
            f"（均分 {seg['avg_score']} < 阈值 {drag_threshold}）—— 这是最可能被划走的一段。",
            f"对第 {seg['from_shot']}~{seg['to_shot']} 镜：合并其中 2 镜、删掉 1 镜，"
            "或在这一段中间插一次反转（一句反问/一个动作/一次景别突变）。",
            shots_list=seg["shots"],
        )

    for seg in relative["segments"]:
        if any(
            s["from_shot"] == seg["from_shot"] and s["to_shot"] == seg["to_shot"]
            for s in drag["segments"]
        ):
            continue
        add(
            "relative-drag-segment",
            "info",
            f"第 {seg['from_shot']}~{seg['to_shot']} 镜相对偏平：连续 {seg['length']} 镜"
            f"均分 {seg['avg_score']}，低于本片自身校准阈值 {calibrated}（均值−σ）。"
            f"上游绝对阈值 {drag_threshold} 没触发，但它在**这一片里**是最低的一段。",
            f"若只想改一处，优先看第 {seg['from_shot']}~{seg['to_shot']} 镜："
            "插一次反转或把其中一镜换成近景+动作。",
            shots_list=seg["shots"],
        )

    if not duration["monotone"] and duration["cv_ratio"] is not None and duration["cv_ratio"] < 1.5:
        add(
            "duration-near-monotone",
            "info",
            f"时长变异系数 cv={duration['cv']} 仅略高于呆板线 {duration['min_cv']}"
            f"（{duration['cv_ratio']}×）：全片时长高度同质（均值 {duration['mean']}s）。",
            "按「对白 3-5s / 动作 1-2s / 悬念缓慢推近」给时长分档，"
            "别让每一镜都 5-6s。",
        )

    if duration["monotone"]:
        add(
            "duration-monotone",
            "warning",
            f"时长节奏呆板：cv={duration['cv']} < {duration['min_cv']}"
            f"（均值 {duration['mean']}s，σ={duration['std']}s）—— 全程一个时长节奏。",
            "把关键镜压到 3-4s、把交代镜放到 6-8s，用时长变化制造节奏（cv 提到 0.2 以上）。",
        )

    for run in duration["long_runs"]:
        add(
            "long-shot-drag",
            "warning",
            f"第 {run['from_shot']}~{run['to_shot']} 镜连续 {run['length']} 个长镜"
            f"（>均值×1.4）—— 长镜堆叠是拖沓的典型形态。",
            "把这几个长镜压到 4-5s，或把其中一个在中间切一刀（加反应镜）。",
            shots_list=run["shots"],
        )

    if not opening["passed"]:
        add(
            "opening-low",
            "warning",
            f"开场密度不足：前 {opening['window']} 镜均分 {opening['avg_score']} < "
            f"{opening['min_avg']}（{opening['mode']}档）—— 完播率由前段决定。",
            f"把第 1~{opening['window']} 镜里最强的一次冲突/悬念提到开场，"
            "或把开场交代镜合并成 1 镜。",
        )

    return {
        "module": "B2",
        "score_source": source,
        "weights": dict(PROXY_WEIGHTS),
        "scores": scores,
        # 逐镜分量只在"用的是代理分"时有意义；显式 conflict 分模式下留空，
        # 避免让人以为这些分量参与了最终判定。
        "breakdown": score_breakdown(shots) if proxy_mode else [],
        "shape": shape,
        "drag": drag,
        "relative_drag": relative,
        "weakest_window": weakest,
        "opening": opening,
        "duration": duration,
        "findings": findings,
    }
