"""
B5 · 对白零丢失加固：从「拼串 substring」升级为**可量化、可进 manifest、可做 CI 门禁**的覆盖率报告。

## 为什么这是我们领先的地方（不是补课）

两个深挖项目都做不到「小说原文对白 → 分镜对白零丢失」：

  - **storyforge**：原文在 `_summarize_chapter()` 第 1 步就被截断到 12K 字符 + LLM 摘要，
    此后全程只基于摘要工作，`dialogue` 是 LLM **重新创作**的（架构级错误，走错很难补）。
  - **wind-comic**：只对「已是剧本格式」的输入靠确定性正则做到了原文保留；
    纯小说输入走 LLM 改编，同样不保证。

我们的 `vm/plan.py` 已有 `chapter_dialogues()` / `_missing_dialogues()`（29 句逐字比对，实测 0 丢失）。
本模块把它升级成门禁，**复用同一套抽句/归一函数**（`_norm_speech` / `chapter_dialogues`），
所以规则永远不会和 plan 阶段漂移：

  1. **顺序化字符流核算**（替代 `substring`）：把所有镜头的台词按顺序拼成一条"字符流"，
     每个字符记录它来自第几镜；原文对白按顺序在这条流里贪婪定位。
     - 命中 → `adopted`，并记下它落在**哪几镜**（长对白被拆到连续两镜时自动跨镜）；
     - 未命中 → `missing`，再用 `difflib` 找出**最像的那一镜**，区分「真丢了」还是「被改写/截断」。
     - 流里**没有被任何原文span覆盖的字符** → `extraneous`：LLM 自创或改写的台词。
       （`_missing_dialogues()` 只能查"少了什么"，查不了"多了什么"。）

  2. **`src_span` 逐字校验**（schema v2 的 `dialogue: [{char, src_span:[a,b], quote}]`）：
     `chapter_text[a:b] == quote` —— 纯字符串比对，零成本、零歧义。
     没有 `src_span` 的旧表**不报错**，退回上面的字符流核算（安静降级）。

  3. **覆盖率 + 门禁**：`原文 29 句，采纳 29 句，遗漏 0 句` 直接进 `audit.json`
     与 `to_manifest_entry()`；`gate.passed` 可让 CI 在**渲染前**拦住丢台词的稿子。

## 实测（`projects/西游记` 52 镜）

原文 29 句 → 采纳 29 句，遗漏 0 句，自创/改写 0 字。达标。
注意有个反例证明了本模块的必要性：**朴素 substring 判据会误报 3 处**
（第 28/36/40 镜把两段中间夹着叙述文字的原文对白合并进了一镜），
顺序化字符流核算把它们正确判为"采纳"。
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Any, Sequence

from vm.audit import dialogue_text, field
from vm.plan import _norm_speech, chapter_dialogues  # 刻意复用 plan 的同口径实现

__all__ = [
    "DEFAULT_GATE",
    "extract_source_quotes",
    "locate_chapter_text",
    "audit_dialogue_fidelity",
    "coverage_line",
    "to_manifest_entry",
]

# 门禁默认值：**零丢失是这层的存在意义**，所以默认就是最严的 100%/0 字自创。
# 要放宽必须显式传参（并在 CLI 上留痕），绝不静默降低标准。
DEFAULT_GATE: dict[str, Any] = {
    "min_coverage_pct": 100.0,
    "max_extraneous_chars": 0,
    "max_span_failures": 0,
    "max_out_of_order": 0,
}


def extract_source_quotes(chapter_text: str) -> list[dict]:
    """
    从正文抽出"真对白"单元（复用 `vm.plan.chapter_dialogues` 的判据）。

    返回 `[{index, quote, norm, norm_len}]`，`index` 是**原文第几句**（1-based，报告里用）。
    """
    out: list[dict] = []
    for i, q in enumerate(chapter_dialogues(chapter_text or ""), 1):
        norm = _norm_speech(q)
        out.append({"index": i, "quote": q, "norm": norm, "norm_len": len(norm)})
    return out


def locate_chapter_text(
    project_dir: Path, explicit: str | Path | None = None
) -> tuple[str, str | None, list[str]]:
    """
    找这一集对应的原文。返回 `(text, path, warnings)`。

    没有章节↔镜头表的显式映射（`project.json` 里没有这个字段），所以这里的规则
    是**照抄 `pipeline.py` 的现状**：`novel/` 下按文件名排序取第一个（`chapters[0]`）。
    多于一个章节文件时给 warning（与 pipeline 的"本次只拆第 1 个"一致）。
    """
    warnings: list[str] = []
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            return "", None, [f"--chapter 指定的文件不存在：{p}"]
        return p.read_text(encoding="utf-8"), str(p), warnings

    novel = Path(project_dir) / "novel"
    if not novel.is_dir():
        return "", None, [f"没有 novel/ 目录：{novel}（无法做原文对白覆盖率比对）"]
    chapters = sorted(novel.glob("*.md")) + sorted(novel.glob("*.txt"))
    if not chapters:
        return "", None, [f"novel/ 下没有章节文件（.md/.txt）：{novel}"]
    if len(chapters) > 1:
        warnings.append(
            f"novel/ 下有 {len(chapters)} 个章节文件，按 pipeline.py 现有规则只用第 1 个："
            f"{chapters[0].name}（其余章节请用 --chapter 显式指定）"
        )
    return chapters[0].read_text(encoding="utf-8"), str(chapters[0]), warnings


# ── 字符流核算 ──────────────────────────────────────────────────────────────


def _shot_norms(shots: Sequence[Any]) -> list[str]:
    return [_norm_speech(dialogue_text(s)) for s in shots]


def _greedy_adopt(source: list[dict], stream: str) -> tuple[list[dict], list[dict]]:
    """
    按原文顺序在字符流里贪婪定位每句对白。

    返回 `(adopted, missing)`；`adopted[i]` 带 `span=(start,end)` 字符区间。
    "按顺序"是刻意的：分镜表顺序 = 成片播放顺序，错序本身就是问题（`out-of-order`）。
    """
    adopted: list[dict] = []
    missing: list[dict] = []
    pos = 0
    for q in source:
        norm = q["norm"]
        if not norm:
            continue
        k = stream.find(norm, pos)
        if k < 0:
            # 只在"当前位置之后"找不到才算问题：先标记，后面统一分类
            missing.append(dict(q, found_at=stream.find(norm)))
            continue
        adopted.append(dict(q, span=[k, k + len(norm)]))
        pos = k + len(norm)
    return adopted, missing


def _classify_missing(
    missing: list[dict], stream: str, shot_norms: Sequence[str]
) -> list[dict]:
    """
    给未采纳的原文对白分类：`absent`（真丢了）/ `partial`（被改写或截断）/ `out-of-order`（顺序错了）。

    `partial` 用 `difflib.SequenceMatcher` 找最长公共块（标准库、确定性、无依赖），
    相似度 ≥50% 才算"被改写"，否则算"整句丢失" —— 阈值写成常量，人可复核。
    """
    out: list[dict] = []
    for q in missing:
        norm = q["norm"]
        best = (0, None)  # (最长公共块长度, 镜号)
        for i, sn in enumerate(shot_norms, 1):
            if not sn:
                continue
            m = difflib.SequenceMatcher(None, norm, sn, autojunk=False).find_longest_match(
                0, len(norm), 0, len(sn)
            )
            if m.size > best[0]:
                best = (m.size, i)
        ratio = (best[0] / len(norm)) if norm else 0.0
        # 相似度太低（<30%）时不硬指一个"最像的镜"——那只会误导人去改一镜无关的台词
        best_shot = best[1] if ratio >= 0.3 else None
        if q.get("found_at", -1) is not None and q.get("found_at", -1) >= 0:
            kind = "out-of-order"
        elif ratio >= 0.5:
            kind = "partial"
        else:
            kind = "absent"
        out.append(
            dict(
                q,
                kind=kind,
                best_shot=best_shot,
                similarity=round(ratio, 3),
                found_at=q.get("found_at"),
            )
        )
    return out


def _extraneous(
    stream: str, provenance: Sequence[int], adopted: list[dict], shots: Sequence[Any]
) -> tuple[list[dict], int]:
    """
    字符流里没被任何原文 span 覆盖的字符 → 按镜归组。

    这是"零丢失"的镜像检查：**多出来的台词同样是保真度问题**
    （`_missing_dialogues()` 查不到，因为 substring 只问"有没有"）。
    """
    covered = bytearray(len(stream))
    for a in adopted:
        for i in range(a["span"][0], a["span"][1]):
            covered[i] = 1
    by_shot: dict[int, list[str]] = {}
    for idx, ch in enumerate(stream):
        if not covered[idx]:
            by_shot.setdefault(provenance[idx], []).append(ch)
    out: list[dict] = []
    for si, chars in sorted(by_shot.items()):
        s = shots[si - 1]
        text = "".join(chars)
        out.append(
            {
                "shot": si,
                "id": str(field(s, "id", "") or f"#{si}"),
                "leftover_chars": len(text),
                "leftover": text[:60],
                "dialogue": dialogue_text(s),
            }
        )
    return out, sum(item["leftover_chars"] for item in out)


def _span_checks(shots: Sequence[Any], chapter_text: str) -> dict:
    """
    校验 schema v2 的 `dialogue[].src_span`：`chapter_text[a:b] == quote`。

    这是最硬的一条：不依赖任何抽句启发式，只要 LLM 给了 span，就能**逐字**证明
    "这句台词就是原文那一段"，或**逐字**证伪"它改了原文"。
    """
    provided = 0
    exact_ok = 0
    norm_ok = 0
    failures: list[dict] = []
    for i, s in enumerate(shots, 1):
        for u in _units(s):
            span = u.get("src_span")
            if not span:
                continue
            provided += 1
            a, b = span
            quote = u["quote"]
            if not (0 <= a <= b <= len(chapter_text)):
                failures.append(
                    {
                        "shot": i,
                        "id": str(field(s, "id", "") or f"#{i}"),
                        "span": [a, b],
                        "kind": "out-of-range",
                        "quote": quote,
                    }
                )
                continue
            slice_ = chapter_text[a:b]
            if slice_ == quote:
                exact_ok += 1
                norm_ok += 1
                continue
            if _norm_speech(slice_) == _norm_speech(quote):
                norm_ok += 1
                continue
            failures.append(
                {
                    "shot": i,
                    "id": str(field(s, "id", "") or f"#{i}"),
                    "span": [a, b],
                    "kind": "mismatch",
                    "quote": quote,
                    "chapter_slice": slice_[:80],
                }
            )
    return {
        "provided": provided,
        "exact_ok": exact_ok,
        "normalized_ok": norm_ok,
        "failures": failures,
    }


def _units(shot: Any) -> list[dict]:
    """局部包装（避免在文件顶部再导入一个名字，保持 import 面窄）。"""
    from vm.audit import dialogue_units

    return dialogue_units(shot)


def coverage_line(report: dict) -> str:
    """「原文 29 句，采纳 29 句，遗漏 0 句（列出）」—— 任务书要求的原话格式。"""
    src = report["source"]["quotes"]
    adopted = report["adopted"]
    missing = report["missing"]
    tail = ""
    if missing:
        names = "；".join(
            f"第 {m['index']} 句「{m['quote'][:14]}…」({m['kind']})"
            for m in report["missing_quotes"][:5]
        )
        more = f"（共 {missing} 句，仅列前 5 句）" if missing > 5 else ""
        tail = f"：{names}{more}"
    return f"原文 {src} 句，采纳 {adopted} 句，遗漏 {missing} 句{tail}"


def to_manifest_entry(report: dict) -> dict:
    """
    压缩成可进 manifest 的一行（供 `vm/state.py` 的 manifest 消费）。

    为什么单独给一个函数：manifest 是"产物事实"的记录，塞整个 report 会让
    manifest 膨胀且难比对；这里只留**可做 CI 门禁的标量**。
    """
    g = report.get("gate", {})
    return {
        "b5_schema_version": 1,
        "source_quotes": report["source"]["quotes"],
        "adopted": report["adopted"],
        "missing": report["missing"],
        "coverage_pct": report["coverage_pct"],
        "extraneous_chars": report["extraneous"]["chars"],
        "span_checks": report["span_checks"]["provided"],
        "span_failures": len(report["span_checks"]["failures"]),
        "gate_passed": bool(g.get("passed")),
    }


# ── 主入口 ──────────────────────────────────────────────────────────────────


def audit_dialogue_fidelity(
    shots: Sequence[Any],
    chapter_text: str,
    *,
    chapter_path: str | None = None,
    min_coverage_pct: float = DEFAULT_GATE["min_coverage_pct"],
    max_extraneous_chars: int = DEFAULT_GATE["max_extraneous_chars"],
    extra_warnings: Sequence[str] = (),
) -> dict:
    """
    B5 主入口。`chapter_text` 为空时**只报"无从比对"**（warning），不假装通过。

    兼容性承诺：旧表（`dialogue` 是字符串、无 `src_span`）走字符流核算，不报错；
    新表（`dialogue: [{char, src_span, quote}]`）额外走逐字 span 校验。
    """
    shots = list(shots)
    warnings: list[str] = list(extra_warnings)
    source = extract_source_quotes(chapter_text or "")
    shot_norms = _shot_norms(shots)

    # 字符流 + 每个字符的来源镜号
    chars: list[str] = []
    provenance: list[int] = []
    for i, n in enumerate(shot_norms, 1):
        for ch in n:
            chars.append(ch)
            provenance.append(i)
    stream = "".join(chars)

    adopted, missing_raw = _greedy_adopt(source, stream) if source else ([], [])
    missing = _classify_missing(missing_raw, stream, shot_norms) if missing_raw else []
    extraneous, extraneous_chars = _extraneous(stream, provenance, adopted, shots)
    span = _span_checks(shots, chapter_text or "")

    total = len([q for q in source if q["norm"]])
    adopted_n = len(adopted)
    coverage = 100.0 if total == 0 else round(adopted_n / total * 100.0, 1)

    findings: list[dict] = []

    def add(code: str, severity: str, message: str, hint: str,
            shots_list: list[int] | None = None) -> None:
        findings.append(
            {
                "code": code,
                "severity": severity,
                "module": "B5",
                "ext": False,
                "from_shot": (shots_list or [None])[0],
                "to_shot": (shots_list or [None])[-1],
                "shots": list(shots_list or []),
                "message": message,
                "rewrite_hint": hint,
            }
        )

    if not chapter_text:
        add(
            "chapter-text-missing",
            "warning",
            "找不到这一集的正文：无法做原文对白覆盖率比对"
            "（B5 只能报告「无从比对」，不能算通过）。",
            "用 --chapter <正文路径> 指定，或确认 projects/<名>/novel/ 下有章节文件。",
        )

    for m in missing:
        if m["kind"] == "out-of-order":
            add(
                "dialogue-reordered",
                "warning",
                f"原文第 {m['index']} 句「{m['quote'][:16]}…」在分镜里的顺序与正文不一致"
                f"（流位置 {m['found_at']}）。",
                "按正文顺序重排镜头，或把该句并回它前后相邻的那一镜。",
                shots_list=[m["best_shot"]] if m["best_shot"] else [],
            )
        elif m["kind"] == "partial":
            add(
                "dialogue-rewritten",
                "error",
                f"原文第 {m['index']} 句「{m['quote'][:18]}…」被**改写或截断**："
                f"与第 {m['best_shot']} 镜的台词最长公共片段只占 {int(m['similarity'] * 100)}%。"
                "（这种丢失在成片里完全无声无息）",
                f"把第 {m['best_shot']} 镜的 dialogue 逐字改回原文，"
                "长句按语义拆到连续的 2-3 镜（原文一个字都不能删改）。",
                shots_list=[m["best_shot"]] if m["best_shot"] else [],
            )
        else:
            where = (
                f"最像的是第 {m['best_shot']} 镜（相似度 {int(m['similarity'] * 100)}%）"
                if m.get("best_shot") else "分镜里没有任何相似的台词"
            )
            add(
                "dialogue-missing",
                "error",
                f"原文第 {m['index']} 句「{m['quote'][:18]}…」整句丢失（{where}）。"
                "没有角色卡的角色（如沙僧）的台词最容易被整句丢掉。",
                "把这句补进最合适的镜头 dialogue（没有角色卡也要保留为画外音，"
                "并在该镜 action 里写明是谁在画外说话）。",
                shots_list=[m["best_shot"]] if m.get("best_shot") else [],
            )

    for item in extraneous:
        add(
            "dialogue-extraneous",
            "warning",
            f"第 {item['shot']} 镜有 {item['leftover_chars']} 个字的台词在原文里找不到："
            f"「{item['leftover']}…」（LLM 自创或改写）。",
            f"把第 {item['shot']} 镜的 dialogue 与正文逐字核对；原文没有的台词要么删掉，"
            "要么改回原文对应那句。",
            shots_list=[item["shot"]],
        )

    for f in span["failures"]:
        if f["kind"] == "out-of-range":
            add(
                "src-span-out-of-range",
                "error",
                f"第 {f['shot']} 镜的 src_span={f['span']} 超出正文长度"
                f"（{len(chapter_text)} 字）：这个溯源是坏的。",
                "重跑该镜的拆镜（span 由拆镜阶段按正文偏移计算），或先删掉坏 span 再审计。",
                shots_list=[f["shot"]],
            )
        else:
            add(
                "src-span-mismatch",
                "error",
                f"第 {f['shot']} 镜 src_span={f['span']} 指向的正文是「{f.get('chapter_slice', '')}」，"
                f"但 quote 写的是「{f['quote'][:24]}」—— **逐字比对不一致**。",
                "以正文为准修正 quote（或修正 src_span）；span 校验是零成本的确定性判据，"
                "不允许「差不多就行」。",
                shots_list=[f["shot"]],
            )

    failures: list[str] = []
    if not chapter_text:
        # 没有正文 = 什么都没比过。**门禁绝不能在"没做检查"时报通过**
        # （否则 B5 会成为"装了但没通电"的又一个实例）。
        failures.append("没有正文可比对，B5 未实际执行")
    else:
        if coverage < float(min_coverage_pct):
            failures.append(
                f"覆盖率 {coverage}% < 门槛 {float(min_coverage_pct)}%"
                f"（遗漏 {len(missing)} 句）"
            )
        if extraneous_chars > int(max_extraneous_chars):
            failures.append(
                f"自创/改写台词 {extraneous_chars} 字 > 上限 {int(max_extraneous_chars)} 字"
            )
    if span["failures"]:
        failures.append(f"src_span 校验失败 {len(span['failures'])} 处")
    if any(m["kind"] == "out-of-order" for m in missing):
        failures.append("原文对白顺序与分镜顺序不一致")

    gate = {
        "passed": not failures,
        "level": "error",
        "thresholds": {
            "min_coverage_pct": float(min_coverage_pct),
            "max_extraneous_chars": int(max_extraneous_chars),
            "max_span_failures": DEFAULT_GATE["max_span_failures"],
            "max_out_of_order": DEFAULT_GATE["max_out_of_order"],
        },
        "failures": failures,
    }

    return {
        "module": "B5",
        "source": {
            "chapter": chapter_path,
            "quotes": total,
            "chars": len(chapter_text or ""),
        },
        "mode": "src-span+stream" if span["provided"] else "substring-stream",
        "adopted": adopted_n,
        "missing": len(missing),
        "coverage_pct": coverage,
        "source_quotes": [
            {
                "index": q["index"],
                "quote": q["quote"],
                "adopted": any(a["index"] == q["index"] for a in adopted),
            }
            for q in source
        ],
        "adopted_spans": [
            {
                "index": a["index"],
                "quote": a["quote"],
                "stream_span": a["span"],
                "shots": sorted({provenance[i] for i in range(a["span"][0], a["span"][1])}),
            }
            for a in adopted
        ],
        "missing_quotes": missing,
        "extraneous": {"chars": extraneous_chars, "shots": extraneous},
        "span_checks": span,
        "gate": gate,
        "warnings": warnings,
        "findings": findings,
    }
