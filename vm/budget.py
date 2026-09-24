"""
budget.py —— E3 预算护栏：**暂停等人批，而不是硬失败**。

## 为什么
调研原话：
  · 「一次『一键成片』内部连着发几十次付费调用，**中途没有任何刹车点**」
  · 「**一道通向不了人的告警，和没有告警是一回事。**」

我们现在的 `一键成片`（`--stage all`）= 拆镜(LLM) → 定妆(GPU) → 渲染(GPU 41.8s/镜) →
质检(CPU) → 合成(CPU)。52 镜一次跑完是 **36 分钟 GPU + 几万 token**，
而按下按钮时**没有任何一处告诉你要花这么多**。

## 设计取舍（三条）
1. **默认不设预算 = 不拦**。只有 `project.json` 里显式写了 `budget` 才启用 ——
   否则会破坏现有项目的行为（向后兼容优先）。
2. **超预算不是报错，是暂停**。返回结构化的"待批准"载荷，UI 拿它弹窗；
   人点"继续"后**一次性放行**，不用重新跑。
3. **放行载荷必须能直接回答三个问题**（调研要求）：
   已经花了多少 / 卡在哪一步 / **再放行多少才能继续**。

## 成本基线是**实测值**，不是估的
  · H3 视频：52 镜共 36.2 分钟 → **41.8 s/镜**
  · Qwen 分镜图：52 张共 521.1s → **10.0 s/张**
  · LLM：plan 实测 51227 tokens / 32 次调用、23070 / 13 次 → **~1700 tokens/次**
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

LEDGER = "cost.jsonl"
APPROVAL = "approval.json"
DAY_ROTATE = True  # 成本台账按日轮转（storyforge 的做法：按日轮转便于对账）

# ── 实测基线 ────────────────────────────────────────────────────────────────
COST = {
    "h3_sec_per_shot": 41.8,      # H3 Ref2VA 单镜渲染（含模型换入换出）
    "qwen_sec_per_shot": 10.0,    # Qwen-Image 单张分镜图
    "llm_tokens_per_call": 1700,  # DeepSeek 单次调用
    "chars_sec_per_run": 41.8 * 2,  # 定妆：每角色一次 T2VA 出图 + 抽帧
}

# 每个阶段的成本构成：(描述, 单位数从哪来)
#   "shots"  = 涉及的镜头数
#   "chars"  = 涉及的角色数
#   "calls"  = 固定次数的 LLM 调用
STAGE_COST: dict[str, dict] = {
    "plan":      {"gpu_sec_per_shot": 0.0,               "llm_calls": 3,  "per_shot_llm": 0.06},
    "chars":     {"gpu_sec_per_shot": 0.0,               "llm_calls": 1},
    "gacha":     {"gpu_sec_per_shot": 0.0,               "llm_calls": 0},
    "render":    {"gpu_sec_per_shot": COST["h3_sec_per_shot"], "llm_calls": 0},
    "qc":        {"gpu_sec_per_shot": 0.0,               "llm_calls": 0},
    "assemble":  {"gpu_sec_per_shot": 0.0,               "llm_calls": 0},
    "storyboard": {"gpu_sec_per_shot": COST["qwen_sec_per_shot"], "llm_calls": 0},
    "all":       {"gpu_sec_per_shot": COST["h3_sec_per_shot"], "llm_calls": 4, "per_shot_llm": 0.06},
}


def _pdir(project: str | Path) -> Path:
    from vm.taskctl import resolve_project
    p = Path(project)
    return p if p.is_absolute() else Path(resolve_project(project))


def ledger_path(pdir: Path, at: float | None = None) -> Path:
    """成本台账路径。按日轮转：`state/cost-YYYYMMDD.jsonl`。"""
    from datetime import datetime
    d = datetime.fromtimestamp(at or time.time()).strftime("%Y%m%d") if DAY_ROTATE else ""
    name = f"cost-{d}.jsonl" if d else LEDGER
    return Path(pdir) / "state" / name


def read_ledger(pdir: Path, days: int = 7) -> list[dict]:
    """读最近 N 天的成本台账。坏行跳过（台账不该因为一行坏就全读不出来）。"""
    base = Path(pdir) / "state"
    if not base.is_dir():
        return []
    files = sorted(base.glob("cost-*.jsonl"))[-days:] or ([base / LEDGER] if (base / LEDGER).is_file() else [])
    out: list[dict] = []
    for f in files:
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except OSError:
            continue
    return out


def spent(pdir: Path, days: int = 1) -> dict:
    """今日已花（用于"已经花了多少"那一问）。"""
    rows = [r for r in read_ledger(pdir, days) if r.get("at", 0) >= time.time() - days * 86400]
    return {
        "runs": len(rows),
        "gpu_sec": round(sum(float(r.get("gpu_sec") or 0) for r in rows), 1),
        "llm_tokens": int(sum(int(r.get("llm_tokens") or 0) for r in rows)),
        "shots": int(sum(int(r.get("shots") or 0) for r in rows)),
    }


def estimate(project: str | Path, stage: str, *, only: list[str] | None = None,
             count: int = 0) -> dict:
    """估算这次要花多少。数字全部来自实测基线，缺失字段用保守值。"""
    from vm.shots import load_shots_dir
    from vm.state import Project

    pdir = _pdir(project)
    proj = Project(pdir)

    # ── 队列 drainer 要单独算（2026-09-24 修）────────────────────────────────
    # `stage="queue"` 不是"把整个项目渲一遍"，它是**抽卡队列**：跑几张小图
    # （Qwen-Image ~10s/张）。原来它走到下面的 `STAGE_COST.get("queue", render)`
    # → 按全项目 31 镜视频估成 **21.6 分钟** → 超过预算 → 抛 NeedsApproval →
    # **drainer 永远起不来**，队列一直 pending。
    # 实测表现：西游记（没配预算）正常，雨夜地铁（配了预算）卡死。
    if stage == "queue":
        from vm import queue as _Q
        st = _Q.stats(proj)
        pend = st.get("pending", 0)
        jobs = [j for j in (_Q.read_all(proj)) if j.status == "pending"]
        # 按作业类型分别估：素材图 / 角色定妆走 Qwen-Image；分镜图也是 Qwen-Image
        gpu_sec = float(COST.get("qwen_sec_per_shot", 10.0)) * max(1, pend)
        return {
            "schema": 1, "stage": "queue", "shots": 0, "shots_pending": pend,
            "gpu_sec": round(gpu_sec, 1), "gpu_min": round(gpu_sec / 60.0, 2),
            "llm_calls": 0, "llm_tokens": 0,
            "jobs_pending": pend,
            "job_kinds": sorted({j.kind for j in jobs}),
            "note": f"{pend} 个排队作业（抽卡类，Qwen-Image ~{COST.get('qwen_sec_per_shot', 10):.0f}s/张）",
            "baseline": "实测：Qwen-Image 10.0s/张",
        }

    all_shots = load_shots_dir(proj.shots_dir)
    if only:
        ids = set(only)
        shots = [s for s in all_shots if s.id in ids]
    else:
        shots = all_shots
    n = len(shots)

    # 已有的：渲染只需算**还没渲的**（有指纹跳过），这才是真实增量
    if stage in ("render", "all"):
        rows = [s for s in shots if not proj.clip(s.id).is_file()]
        n_new = len(rows)
    else:
        n_new = n

    spec = STAGE_COST.get(stage, STAGE_COST["render"])
    gpu = float(spec.get("gpu_sec_per_shot") or 0) * n_new
    # 定妆：每个角色一次出图
    n_chars = len({c for s in shots for c in (s.chars or [])})
    if stage in ("chars", "all"):
        gpu += COST["chars_sec_per_run"] * max(0, n_chars)
    llm_calls = int(spec.get("llm_calls") or 0) + int((spec.get("per_shot_llm") or 0) * n)
    if count:
        gpu += COST["qwen_sec_per_shot"] * 0  # gacha 的张数由 count 决定
        gpu += float(spec.get("gpu_sec_per_shot") or 0) * max(0, count - 1)

    return {
        "stage": stage,
        "shots": n,
        "shots_pending": n_new,
        "chars": n_chars,
        "gpu_sec": round(gpu, 1),
        "gpu_min": round(gpu / 60, 1),
        "llm_calls": llm_calls,
        "llm_tokens": llm_calls * COST["llm_tokens_per_call"],
        "wall_min": round((gpu + llm_calls * 3) / 60, 1),
        "baseline": COST,
    }


def budget_of(project: str | Path) -> dict:
    """读 `project.json` 的 budget 段。**没有就返回空 dict = 不启用护栏。**"""
    pdir = _pdir(project)
    try:
        cfg = json.loads((Path(pdir) / "project.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, FileNotFoundError):
        return {}
    b = cfg.get("budget")
    return b if isinstance(b, dict) else {}


def check(project: str | Path, est: dict) -> dict:
    """
    是否该暂停等人批。

    返回 {needs_approval, reasons[], message, est, spent, budget}。
    **没有任何预算配置 → 永远 needs_approval=False**（向后兼容）。
    """
    pdir = _pdir(project)
    b = budget_of(project)
    sp = spent(pdir)
    reasons: list[str] = []
    if not b:
        return {"needs_approval": False, "reasons": [], "message": "", "est": est,
                "spent": sp, "budget": {},
                "note": "project.json 未配置 budget → 护栏未启用（向后兼容）"}

    max_min = b.get("max_gpu_minutes")
    if max_min is not None and est["gpu_min"] > float(max_min):
        reasons.append(f"本次 GPU 约 {est['gpu_min']} 分钟，超过上限 {max_min} 分钟")
    max_shots = b.get("max_shots_per_run")
    if max_shots is not None and est["shots_pending"] > int(max_shots):
        reasons.append(f"本次涉及 {est['shots_pending']} 镜待处理，超过上限 {max_shots}")
    max_tok = b.get("max_llm_tokens")
    if max_tok is not None and est["llm_tokens"] > int(max_tok):
        reasons.append(f"本次预计 {est['llm_tokens']} tokens，超过上限 {max_tok}")
    max_daily = b.get("max_gpu_minutes_per_day")
    if max_daily is not None and (sp["gpu_sec"] / 60 + est["gpu_min"]) > float(max_daily):
        reasons.append(
            f"今日已用 {sp['gpu_sec'] / 60:.1f} 分钟 + 本次 {est['gpu_min']} 分钟，"
            f"超过每日上限 {max_daily} 分钟"
        )
    if not reasons:
        return {"needs_approval": False, "reasons": [], "message": "", "est": est,
                "spent": sp, "budget": b}

    # ★ 放行载荷必须直接回答三个问题（调研要求）
    msg = (
        f"这一步要花 **{est['gpu_min']} 分钟 GPU / 约 {est['llm_tokens']} tokens**"
        f"（{est['shots_pending']} 镜待处理）。\n"
        f"已花：今天 {sp['gpu_sec'] / 60:.1f} 分钟 GPU、{sp['llm_tokens']} tokens（{sp['runs']} 次任务）\n"
        f"卡在：阶段 `{est['stage']}`\n"
        f"再放行 {est['gpu_min']} 分钟 GPU / {est['llm_tokens']} tokens 即可继续。\n"
        + "\n".join(f"· {r}" for r in reasons)
    )
    return {"needs_approval": True, "reasons": reasons, "message": msg,
            "est": est, "spent": sp, "budget": b}


# ── 放行（一次性） ──────────────────────────────────────────────────────────


def grant_approval(project: str | Path, *, by: str = "user", stage: str = "",
                   note: str = "") -> dict:
    """批准一次。**一次性**：下一个任务消费掉就没了（防止永久放行）。"""
    pdir = _pdir(project)
    p = Path(pdir) / "state" / APPROVAL
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"granted_at": time.time(), "by": by, "stage": stage, "note": note}
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return payload


def pending_approval(project: str | Path) -> dict | None:
    pdir = _pdir(project)
    p = Path(pdir) / "state" / APPROVAL
    if not p.is_file():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return d if isinstance(d, dict) else None


def consume_approval(project: str | Path) -> dict | None:
    """消费放行（读完即删）。"""
    a = pending_approval(project)
    if a:
        try:
            (Path(_pdir(project)) / "state" / APPROVAL).unlink()
        except OSError:
            pass
    return a


def record(project: str | Path, stage: str, est: dict, *, ok: bool = True,
           note: str = "", elapsed_sec: float | None = None) -> None:
    """记一笔成本。写入 `state/cost-YYYYMMDD.jsonl`（一行一条，便于对账）。"""
    pdir = _pdir(project)
    f = ledger_path(pdir)
    f.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "at": time.time(),
        "stage": stage,
        "shots": est.get("shots"),
        "shots_pending": est.get("shots_pending"),
        "gpu_sec": est.get("gpu_sec"),
        "gpu_min": est.get("gpu_min"),
        "llm_calls": est.get("llm_calls"),
        "llm_tokens": est.get("llm_tokens"),
        "ok": ok,
        "elapsed_sec": elapsed_sec,
        "note": note,
    }
    try:
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


if __name__ == "__main__":
    import argparse
    import sys

    ap = argparse.ArgumentParser(prog="python3 -m vm.budget",
                                 description="E3 预算护栏：估算 / 查看成本台账 / 放行一次。")
    ap.add_argument("project")
    ap.add_argument("--stage", default="all", help="要估算的阶段（默认 all）")
    ap.add_argument("--only", default="", help="只算这些镜头，逗号分隔")
    ap.add_argument("--approve", action="store_true", help="放行一次（下一个任务消费）")
    ap.add_argument("--ledger", action="store_true", help="打印今天的成本台账")
    a = ap.parse_args()
    if a.approve:
        print(json.dumps(grant_approval(a.project, stage=a.stage), ensure_ascii=False, indent=2))
        raise SystemExit(0)
    if a.ledger:
        pdir = _pdir(a.project)
        print(f"今日：{json.dumps(spent(pdir), ensure_ascii=False)}")
        for r in read_ledger(pdir, 1):
            print("  ", json.dumps(r, ensure_ascii=False))
        raise SystemExit(0)
    only = [x.strip() for x in a.only.split(",") if x.strip()] or None
    est = estimate(a.project, a.stage, only=only)
    print(json.dumps(est, ensure_ascii=False, indent=2))
    chk = check(a.project, est)
    print()
    print("需要批准" if chk["needs_approval"] else "✓ 未超预算，可直接跑")
    if chk["needs_approval"]:
        print(chk["message"])
        raise SystemExit(3)
