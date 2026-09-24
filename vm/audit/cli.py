"""
B 档审计层的命令行入口。

```bash
python3 -m vm.audit.cli 西游记                 # 人类可读报告 + 写 state/audit.json
python3 -m vm.audit.cli 西游记 --json           # 同时把 JSON 打到 stdout
python3 -m vm.audit.cli 西游记 --gate           # CI 门禁：有 error/warning 就非零退出
python3 -m vm.audit.cli projects/西游记 --chapter novel/第一章_古寺夜哭.md
```

设计约束：

  - **只读**镜头表与正文，只**新建** `state/audit.json`（原子写），从不改任何现有产物。
  - 不连 ComfyUI、不起进程、不碰 `output/`。纯 CPU。
  - 退出码：`0` 通过；`1` `--gate` 判定失败；`2` 输入/用法错误。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path
from typing import Sequence

from vm.audit import SEVERITIES, atomic_write_json, load_raw_shots, locate_project, severity_rank
from vm.audit.dialogue_coverage import audit_dialogue_coverage
from vm.audit.dialogue_fidelity import (
    audit_dialogue_fidelity,
    coverage_line,
    locate_chapter_text,
    to_manifest_entry,
)
from vm.audit.pacing import audit_pacing

__all__ = ["build_report", "format_report", "main"]

SCHEMA_VERSION = 1

# 已知局限：**机器可读地承认自己看不到什么**。
# 这是本项目"未接线的能力必须显式标注"（NEXT-ACTIONS 负面清单）的直接落地。
LIMITATIONS: list[str] = [
    "B1 分组模式：镜头表没有显式 location/scene_id 字段时退化为 consecutive（连续对话镜成组）"
    "—— 这只会把两场不同地点的对话并成一组（漏报），不会误报；精确分组需要 schema v2 的 location。",
    "B1 的 no-two-shot / monotone-shot-size / ambiguous-speaker 是本仓库扩展（不是 wind-comic 上游），"
    "全部为 info 级、不计入 coverage_score；no-two-shot 只统计「扩展场景」窗口内，"
    "紧邻窗口外的群体陈述镜可能让它误报。",
    "B2 形状判定默认基于确定性代理分（镜头表无 conflict 字段）：代理分区分度有限"
    "（实测 σ≈0.87/10），因此代理驱动的形状判定一律 info 级。"
    "建议让 vm/plan.py 拆镜时顺带输出逐镜 conflict 分，再用 --scores-mode explicit 复核。",
    "B5 的抽句沿用 vm.plan.chapter_dialogues() 的启发式（引号 + 句末标点/言语动词），"
    "正则盲区会同时影响覆盖率的分母与分子；有 src_span 的数据可绕开该启发式（逐字校验）。",
    "audit.json 含 generated_at 时间戳，不是字节级幂等产物（判定结果本身是确定性的）。",
]


def build_report(
    project: str | Path,
    *,
    chapter: str | Path | None = None,
    scores_mode: str = "auto",
    group_by: str = "auto",
    drag_threshold: float = 4.0,
    min_cv: float = 0.12,
    opening_mode: str = "drama",
    min_coverage_pct: float = 100.0,
    max_extraneous_chars: int = 0,
) -> dict:
    """跑完 B1+B2+B5，返回完整报告 dict（纯计算，不落盘）。"""
    proj = locate_project(str(project))
    shots_dir = proj / "shots"
    rows = load_raw_shots(shots_dir)
    shot_files = sorted(p.name for p in shots_dir.glob("*.json") if not p.name.endswith(".tmp"))

    chapter_text, chapter_path, chapter_warnings = locate_chapter_text(proj, chapter)

    b1 = audit_dialogue_coverage(rows, group_by=group_by)
    b2 = audit_pacing(
        rows, mode=scores_mode, drag_threshold=drag_threshold, min_cv=min_cv,
        opening_mode=opening_mode,
    )
    b5 = audit_dialogue_fidelity(
        rows, chapter_text, chapter_path=chapter_path,
        min_coverage_pct=min_coverage_pct, max_extraneous_chars=max_extraneous_chars,
        extra_warnings=chapter_warnings,
    )

    findings: list[dict] = []
    for mod in (b1, b2, b5):
        findings.extend(mod.get("findings", []))
    order = {s: i for i, s in enumerate(SEVERITIES)}
    findings.sort(key=lambda f: (f.get("module", ""), -severity_rank(f.get("severity", "info")),
                                 f.get("from_shot") or 0))

    by_sev = {s: sum(1 for f in findings if f.get("severity") == s) for s in SEVERITIES}
    all_chars = sorted({c for r in rows for c in (r.get("chars") or []) if c})
    errors = [f"{f['module']} {f['code']}" for f in findings if f["severity"] == "error"]
    warnings = [f"{f['module']} {f['code']}" for f in findings if f["severity"] == "warning"]

    gates = {
        "b5": b5["gate"],
        "errors": {"passed": not errors, "failures": errors},
    }
    gate_passed = b5["gate"]["passed"] and not errors

    return {
        "schema_version": SCHEMA_VERSION,
        "tool": "vm.audit",
        "generated_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "project": {"name": proj.name, "path": str(proj)},
        "inputs": {
            "shots_dir": str(shots_dir),
            "shot_files": shot_files,
            "chapter": chapter_path,
            "shots": len(rows),
            "chars": len(all_chars),
            "total_sec": round(sum(float(r.get("sec") or 0) for r in rows), 3),
        },
        "summary": {
            "findings": by_sev,
            "total_findings": len(findings),
            "gate_passed": gate_passed,
            "errors": errors,
            "warnings": warnings,
        },
        "manifest_entry": to_manifest_entry(b5),
        "b1": b1,
        "b2": b2,
        "b5": b5,
        "findings": findings,
        "limitations": list(LIMITATIONS),
    }


# ── 人类可读报告 ────────────────────────────────────────────────────────────


def _ck(ok: bool) -> str:
    return "✓" if ok else "✗"


def _shot_list(shots: Sequence[int]) -> str:
    return "、".join(str(s) for s in shots)


def format_report(r: dict) -> str:
    """把报告格式化成给人看的文本（宽字符按 2 列对齐，中英混排也不会散）。"""
    inp = r["inputs"]
    b1, b2, b5 = r["b1"], r["b2"], r["b5"]
    out: list[str] = []
    w = out.append

    w("=" * 78)
    w(f"  vm.audit · 零 LLM 渲染前质检 · {r['project']['name']}")
    w("=" * 78)
    w(f"镜头表 : {inp['shots_dir']}/{', '.join(inp['shot_files'])}")
    w(f"        {inp['shots']} 镜 / {inp['chars']} 角色 / {inp['total_sec']:.0f} 秒")
    w(f"正文   : {inp['chapter'] or '(未找到)'}")
    w("")

    # ── B1 ──
    w("─" * 78)
    w(f"[B1] 对话覆盖度 —— 治「一段连续对话用 1 个 wide shot 涵盖」的 AI 一遍跑完感")
    w("─" * 78)
    g = b1["grouping"]
    w(f"对话镜 {b1['dialogue_shot_count']}/{b1['shot_count']} 镜，"
      f"对话场景 {b1['scene_count']} 组（多角色 {b1['multi_char_scene_count']} 组）")
    w(f"分组模式：{g['mode']}"
      + ("（镜头表无显式 location 字段 → 连续对话镜成组；只会漏报不会误报）"
         if g["mode"] == "consecutive" else "（同 location 并入，换 location 切分）"))
    for s in b1["scenes"]:
        w(f"  场景{s['index']:>2}: 第 {_shot_list(s['shots'])} 镜  "
          f"[{'/'.join(s['chars'])}]  {'/'.join(s['size_classes'])}")
    w(f"上游两项检查：缺正反打 {len(b1['needs_reverse_shot_shots'])} 处 / "
      f"全 wide 无特写 {len(b1['needs_close_up_shots'])} 处 → coverageScore = {b1['coverage_score']}")
    for f in b1["findings"]:
        w(_fmt_finding(f))

    # ── B2 ──
    w("")
    w("─" * 78)
    w("[B2] 节奏 / 冲突形状 —— 平均分会把「高开低走」和「层层递进」算成同一个数")
    w("─" * 78)
    sh, src = b2["shape"], b2["score_source"]
    w(f"打分模式：{src['mode']}" + (f"（字段 {src['key']}）" if src.get("key") else "（确定性代理分）"))
    w(f"形状判定：{sh['shape']}  peak={sh['peak_value']}@第{sh['peak_index']}镜  "
      f"prominence={sh['peak_prominence']}(<1.5→no-climax)  slope={sh['slope']}(±0.15)  "
      f"mean={sh['mean']} σ={sh['std']}"
      + ("  ⚠ borderline" if sh.get("borderline") else ""))
    d = b2["drag"]
    w(f"拖沓段（上游阈值 {d['threshold']}，连续≥{d['min_run']}镜）："
      + (_fmt_segs(d["segments"]) or "无"))
    rd = b2["relative_drag"]
    w(f"校准阈值 {rd['threshold']}（本片 均值−σ）：" + (_fmt_segs(rd["segments"]) or "无"))
    wk = b2["weakest_window"]
    if wk:
        w(f"最低洼窗口：第 {wk['from_shot']}~{wk['to_shot']} 镜 均分 {wk['avg_score']}"
          f"（全片均分 {wk['overall_mean']}）")
    op = b2["opening"]
    w(f"开场密度：前 {op['window']} 镜均分 {op['avg_score']} "
      f"{_ck(op['passed'])}（门槛 {op['min_avg']}，{op['mode']}档）")
    du = b2["duration"]
    w(f"时长节奏：cv={du['cv']} {_ck(not du['monotone'])}（呆板线 {du['min_cv']}，"
      f"{du['cv_ratio']}×；均值 {du['mean']}s）  长镜堆叠：{len(du['long_runs'])} 段")
    for f in b2["findings"]:
        w(_fmt_finding(f))

    # ── B5 ──
    w("")
    w("─" * 78)
    w("[B5] 对白零丢失 —— 覆盖率门禁（两个深挖项目都没做到的事）")
    w("─" * 78)
    w(f"{coverage_line(b5)}")
    w(f"自创/改写台词：{b5['extraneous']['chars']} 字"
      + (f"（第 {_shot_list([i['shot'] for i in b5['extraneous']['shots']])} 镜）"
         if b5["extraneous"]["shots"] else ""))
    sc = b5["span_checks"]
    w(f"src_span 逐字校验：{sc['provided']} 处（完全一致 {sc['exact_ok']} / 忽略标点一致 "
      f"{sc['normalized_ok']} / 失败 {len(sc['failures'])}）"
      + ("" if sc["provided"] else "  ← 旧表无 src_span，已安静退化为字符流核算"))
    w(f"对照模式：{b5['mode']}")
    w(f"门禁：{'PASS' if b5['gate']['passed'] else 'FAIL'}"
      + ("  " + "；".join(b5["gate"]["failures"]) if b5["gate"]["failures"] else ""))
    for f in b5["findings"]:
        w(_fmt_finding(f))

    # ── 汇总 ──
    w("")
    w("=" * 78)
    s = r["summary"]
    w(f"汇总：error {s['findings']['error']} / warning {s['findings']['warning']} / "
      f"info {s['findings']['info']}   门禁 {'PASS' if s['gate_passed'] else 'FAIL'}")
    for f in r["findings"]:
        if f["severity"] in ("error", "warning"):
            tag = f"[{f['module']}] {_loc(f.get('from_shot'), f.get('to_shot'))}" if f.get("from_shot") else f"[{f['module']}]"
            w(f"  {f['severity']:<7} {tag}: {f['message']}")
            w(f"          → {f['rewrite_hint']}")
    w("")
    w("已知局限（机器可读，见 JSON 的 limitations）：")
    for lim in r["limitations"]:
        w(f"  · {lim}")
    w("=" * 78)
    return "\n".join(out)


def _fmt_segs(segs: Sequence[dict]) -> str:
    return "；".join(
        f"第 {s['from_shot']}~{s['to_shot']} 镜（{s['length']} 镜，均分 {s['avg_score']}）"
        for s in segs
    )


def _loc(shot_from: int | None, shot_to: int | None) -> str:
    if not shot_from:
        return "全片"
    if shot_to and shot_to != shot_from:
        return f"第 {shot_from}~{shot_to} 镜"
    return f"第 {shot_from} 镜"


def _fmt_finding(f: dict) -> str:
    sev = {"error": "✗", "warning": "!", "info": "·"}.get(f["severity"], "·")
    loc = _loc(f.get("from_shot"), f.get("to_shot"))
    lines = [f"  {sev} [{f['severity']:<7}] {loc} {f['code']}: {f['message']}"]
    if f.get("rewrite_hint"):
        lines.append(f"        → {f['rewrite_hint']}")
    return "\n".join(lines)


# ── CLI ─────────────────────────────────────────────────────────────────────


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python3 -m vm.audit.cli",
        description="零 LLM 渲染前质检：B1 对话覆盖度 / B2 节奏形状 / B5 对白零丢失",
    )
    p.add_argument("project", help="项目名（projects/<名>）或项目目录路径")
    p.add_argument("--chapter", help="正文文件路径（默认 novel/ 下文件名排序第一个）")
    p.add_argument("--json", action="store_true", help="把完整 JSON 打到 stdout")
    p.add_argument("--out", help="audit.json 落盘路径（默认 <项目>/state/audit.json）")
    p.add_argument("--no-write", action="store_true", help="不落盘，只看报告")
    p.add_argument("--gate", action="store_true", help="门禁模式：有 error 级判定则退出码 1")
    p.add_argument("--scores-mode", choices=("auto", "proxy", "explicit"), default="auto",
                   help="B2 冲突分来源：auto=有显式 conflict 字段就用，否则代理分")
    p.add_argument("--group-by", choices=("auto", "location", "consecutive", "heuristic"),
                   default="auto", help="B1 场景分组模式")
    p.add_argument("--drag-threshold", type=float, default=4.0, help="B2 拖沓段阈值")
    p.add_argument("--min-cv", type=float, default=0.12, help="B2 时长呆板线")
    p.add_argument("--opening-mode", choices=("drama", "normal"), default="drama")
    p.add_argument("--min-coverage-pct", type=float, default=100.0,
                   help="B5 覆盖率门槛（默认 100，即零丢失）")
    p.add_argument("--max-extraneous-chars", type=int, default=0,
                   help="B5 允许的自创/改写台词字数（默认 0）")
    return p


def _attach_shot_ids(report: dict) -> None:
    """
    给每条 finding 补上镜头 **id**（`from_id` / `to_id` / `shot_ids`）。

    为什么必须补：finding 里的 `from_shot` / `to_shot` / `shots` 是**表内 1-based 序号**。
    序号是脆弱的 —— 在表中间插入或删除一个镜头，所有 finding 的序号都会**静默指错位置**，
    而消费方（UI、CI 门禁）拿着序号去定位时会显示到完全不相干的镜头上。

    这里在**报告组装层**统一解析一次（不在各模块里各写一遍），消费方直接用 id 即可。
    序号越界时置 None 而不是抛错 —— 审计报告不该因为表变了就读不出来。
    """
    try:
        rows = load_raw_shots(Path(report["project"]["path"]) / "shots")
        ids = [str(r.get("id") or "") for r in rows]

        def to_id(n):
            if not isinstance(n, int) or n < 1 or n > len(ids):
                return None
            return ids[n - 1] or None

        for f in report.get("findings") or []:
            if not isinstance(f, dict):
                continue
            f["from_id"] = to_id(f.get("from_shot"))
            f["to_id"] = to_id(f.get("to_shot"))
            f["shot_ids"] = [x for x in (to_id(i) for i in (f.get("shots") or [])) if x]
    except Exception:
        # 补 id 只是便利，失败不该让整份报告不可用
        pass


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = build_report(
            args.project,
            chapter=args.chapter,
            scores_mode=args.scores_mode,
            group_by=args.group_by,
            drag_threshold=args.drag_threshold,
            min_cv=args.min_cv,
            opening_mode=args.opening_mode,
            min_coverage_pct=args.min_coverage_pct,
            max_extraneous_chars=args.max_extraneous_chars,
        )
    except (FileNotFoundError, ValueError) as e:
        print(f"audit: 输入错误：{e}", file=sys.stderr)
        return 2

    _attach_shot_ids(report)

    print(format_report(report))

    out_path: Path | None = None
    if not args.no_write:
        out_path = Path(args.out) if args.out else (Path(report["project"]["path"]) / "state" / "audit.json")
        atomic_write_json(out_path, report)
        print(f"\n已写入 {out_path}")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))

    if args.gate:
        s = report["summary"]
        failed = (not s["gate_passed"]) or s["findings"]["error"] > 0
        return 1 if failed else 0
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
