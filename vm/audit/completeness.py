"""
completeness.py —— B3：逐镜 7 维完备性矩阵 + blockers（零 LLM）。

## 为什么需要
"第 5 镜有问题"这种信息密度太低。真正有用的是：
  「第 5 镜的**角色参考图还只是 draft**」
  「第 12 镜有产物，但**分镜图没出**，所以没经过渲染前审片」
把 `{维度} × {状态}` 摊成矩阵，缺什么一眼可见。

## ★ 维度必须按**我们的真实管线**定义，不能照抄调研原文
调研原文的 7 维是 `{角色参考图, 场景参考图, 分镜图, 视频片段, 台词, 配音, 配乐}`。
但我们的形态不同：
  · 场景**没有参考图**（是文字锚定：同场景注入同一段描述）
  · 配音与配乐**不是独立资产** —— H3 在生成视频时一起产出，含在片段里
硬套那 7 个会得到 3 个恒定空的列，看板变成了噪音。
所以本模块用**我们真实存在的 7 个维度**（见 DIMENSIONS）。

## 状态语义（四态）
  ok       存在且与当前定义一致
  stale    存在但**指纹不匹配**（定义改过，产物是旧的）→ 复用了 `vm.state` 的指纹口径
  draft    存在但**未经确认**（推断值 / 用的默认值）
  missing  不存在

`tone` 只用于 UI 着色；判定逻辑不依赖它。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 维度顺序 = 展示顺序：先是渲染**前置**，再是**产物**。
# `blocks_render=True` 的维度不齐时，渲染这一镜会降级或失败。
DIMENSIONS: list[dict] = [
    {"key": "char_ref",   "label": "角色参考图", "phase": "前置", "blocks_render": True},
    {"key": "costume",    "label": "服装定义",   "phase": "前置", "blocks_render": False},
    {"key": "scene",      "label": "场景锚定",   "phase": "前置", "blocks_render": False},
    {"key": "props",      "label": "道具锚定",   "phase": "前置", "blocks_render": False},
    {"key": "storyboard", "label": "分镜图",     "phase": "前置", "blocks_render": False},
    {"key": "clip",       "label": "视频片段",   "phase": "产物", "blocks_render": False},
    {"key": "qc",         "label": "质检",       "phase": "产物", "blocks_render": False},
]

STATE_RANK = {"missing": 0, "draft": 1, "stale": 2, "ok": 3}
TONE = {"ok": "ok", "stale": "warn", "draft": "warn", "missing": "idle"}


@dataclass
class Cell:
    state: str            # ok | stale | draft | missing
    detail: str = ""      # 人话说明，UI 直接显示

    def to_dict(self) -> dict:
        return {"state": self.state, "tone": TONE.get(self.state, "idle"), "detail": self.detail}


@dataclass
class ShotRow:
    id: str
    scene_id: str
    chapter: int
    cells: dict[str, Cell] = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "scene_id": self.scene_id, "chapter": self.chapter,
            "cells": {k: v.to_dict() for k, v in self.cells.items()},
            "blockers": self.blockers,
        }


def _cn_chapter(shot_id: str) -> int:
    seg = str(shot_id or "").split("-")[0]
    try:
        return int(seg)
    except ValueError:
        return 0


def build(project: str | Path) -> dict:
    """
    生成矩阵。**纯 CPU、零 LLM、只读**（不改任何产物）。

    返回 {shots[], dimensions[], summary{}, blockers[]}。
    """
    import sys
    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from vm.state import Project
    from vm.shots import load_shots_dir
    from vm.taskctl import resolve_project

    # 项目名（如「西游记」）或**已经是路径**都能吃。
    # ⚠️ 不要再无条件 resolve_project(project)：`taskctl.completeness_table` 传进来的
    # 已经是绝对路径，而 `resolve_project` 对**含 `/` 的相对路径**会直接拒绝
    # （它的防穿越校验），于是重复解析会抛「非法项目名：projects/西游记」。
    _p = Path(project)
    pdir = _p if _p.is_absolute() else Path(resolve_project(project))
    proj = Project(pdir)
    shots = load_shots_dir(proj.shots_dir)
    manifest = proj.manifest

    # ── 各维度的项目级素材 ──
    refs = {p.stem[5:]: p for p in proj.refs_dir.glob("char_*.png")} if proj.refs_dir.is_dir() else {}
    try:
        costumes = json.loads((proj.root / "costumes.json").read_text(encoding="utf-8"))
        costume_chars = costumes.get("characters") or {}
    except (json.JSONDecodeError, OSError, FileNotFoundError):
        costume_chars = {}
    try:
        scenes = {x["id"]: x for x in (json.loads((proj.root / "scenes.json").read_text(encoding="utf-8")).get("scenes") or [])
                  if isinstance(x, dict) and x.get("id")}
    except (json.JSONDecodeError, OSError, FileNotFoundError):
        scenes = {}
    try:
        props = {x["id"]: x for x in (json.loads((proj.root / "props.json").read_text(encoding="utf-8")).get("props") or [])
                 if isinstance(x, dict) and x.get("id")}
    except (json.JSONDecodeError, OSError, FileNotFoundError):
        props = {}

    # 分镜图索引（含指纹）。
    # ★ 必须用 SB.list_status —— 它是该模块公开的出图状态接口（我在第一版里编了个
    #   `SB.storyboard_state`，AttributeError 之后只给一个 key 就 break，
    #   兜底分支因此没跑，结果是 **52 张图全被判成 missing**。矩阵报假缺失比不报更糟。）
    sb_rows: dict[str, dict] = {}
    sb_error = ""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from vm import storyboard as SB
        sb_index = SB.SBIndex.load(Path(proj.state_dir) / SB.INDEX_FILENAME)
        cfg = SB.QIConfig()
        for r in SB.list_status(shots, cfg, sb_index, proj, negative="", seed=None,
                                with_subject_defs=False):
            if isinstance(r, dict) and r.get("id"):
                sb_rows[str(r["id"])] = r
    except Exception as e:
        # 负面清单里那条：**只写 log 的判定 = 装了但没通电**。
        # 所以这里不静默 —— 把原因带进返回值，UI 上看得见。
        sb_rows = {}
        sb_error = f"{type(e).__name__}: {e}"
        sys.stderr.write(f"[completeness] 分镜图状态读取失败，退化为文件存在性检查：{sb_error}\n")
    if not sb_rows:
        # 兜底：只看文件在不在（拿不到指纹时**不要假装知道 stale**）
        sb_dir = proj.root / "storyboard"
        for s in shots:
            p = sb_dir / f"{s.id}.png"
            sb_rows[s.id] = {"status": "current" if p.is_file() else "missing"}

    # 质检
    try:
        qc = json.loads((proj.state_dir / "qc.json").read_text(encoding="utf-8"))
        qc_results = qc.get("results") or {}
    except (json.JSONDecodeError, OSError, FileNotFoundError):
        qc_results = {}

    rows: list[ShotRow] = []
    for s in shots:
        cells: dict[str, Cell] = {}

        # ① 角色参考图
        missing_refs = [c for c in (s.chars or []) if c not in refs]
        if not s.chars:
            cells["char_ref"] = Cell("draft", "本镜没有登记角色（纯环境镜也要挂一个角色）")
        elif missing_refs:
            cells["char_ref"] = Cell("missing", f"缺参考图：{'、'.join(missing_refs)}")
        else:
            cells["char_ref"] = Cell("ok", f"{len(s.chars)} 个角色参考图齐全")

        # ② 服装定义
        if not s.chars:
            cells["costume"] = Cell("draft", "无角色，跳过")
        else:
            bad = [c for c in s.chars if c not in costume_chars]
            chosen = s.costume or {}
            undef = []
            for c, vid in chosen.items():
                variants = [v.get("id") for v in ((costume_chars.get(c) or {}).get("variants") or [])]
                if vid not in variants:
                    undef.append(f"{c}={vid}")
            if undef:
                cells["costume"] = Cell("missing", f"指定的变体不存在：{'、'.join(undef)}")
            elif bad:
                cells["costume"] = Cell("draft", f"{'、'.join(bad)} 未定义服装，将用默认行为")
            elif chosen:
                cells["costume"] = Cell("ok", "变体：" + "、".join(f"{k}={v}" for k, v in chosen.items()))
            else:
                cells["costume"] = Cell("draft", "用默认变体（未显式指定）")

        # ③ 场景锚定
        if not s.scene_id:
            cells["scene"] = Cell("missing", "没有 scene_id（旧版提示词无场景锚定）")
        elif s.scene_id not in scenes:
            cells["scene"] = Cell("missing", f"scene_id={s.scene_id} 在 scenes.json 里不存在")
        else:
            cells["scene"] = Cell("ok", scenes[s.scene_id].get("name") or s.scene_id)

        # ④ 道具锚定
        pids = list(s.prop_ids or [])
        bad_p = [p for p in pids if p not in props]
        inferred = [p for p in pids if p in props and props[p].get("inferred")]
        if bad_p:
            cells["props"] = Cell("missing", f"prop_ids 里有不存在的：{'、'.join(bad_p)}")
        elif inferred:
            cells["props"] = Cell("draft", f"{'、'.join(props[p].get('name', p) for p in inferred)} 是推断外观，需人工复核")
        elif pids:
            cells["props"] = Cell("ok", "、".join(props[p].get("name", p) for p in pids))
        else:
            cells["props"] = Cell("ok", "本镜无道具（正常）")

        # ⑤ 分镜图
        sb = sb_rows.get(s.id) or {"status": "missing"}
        st = str(sb.get("status") or "missing")
        if st == "current":
            cells["storyboard"] = Cell("ok", "已出图")
        elif st == "stale":
            cells["storyboard"] = Cell("stale", "提示词改过，分镜图是旧的")
        elif st == "bad-prompt":
            cells["storyboard"] = Cell("missing", str(sb.get("note") or "提示词不合格")[:60])
        else:
            cells["storyboard"] = Cell("missing", "未出图（没经过渲染前审片）")

        # ⑥ 视频片段
        e = manifest.shots.get(s.id)
        clip = proj.clip(s.id)
        if not clip.is_file():
            cells["clip"] = Cell("missing", "没有片段")
        elif e and e.fp:
            st2 = manifest.status(s.id, e.fp, clip)
            cells["clip"] = (Cell("ok", "已渲染") if st2 == "current"
                             else Cell("stale", "指纹不匹配（定义改过，片段是旧的）"))
        else:
            cells["clip"] = Cell("draft", "有文件但 manifest 里没有指纹记录")

        # ⑦ 质检
        q = qc_results.get(s.id)
        if not q:
            cells["qc"] = Cell("missing", "未质检")
        elif cells["clip"].state != "ok":
            cells["qc"] = Cell("stale", "片段已变，质检结论不作数")
        else:
            v = q.get("verdict") or ""
            cells["qc"] = Cell("ok" if v == "pass" else ("stale" if v == "suspicious" else "missing"),
                               {"pass": "通过", "suspicious": "可疑（待人工）"}.get(v, v or "-"))

        blockers = [d["label"] for d in DIMENSIONS
                    if d["blocks_render"] and cells[d["key"]].state != "ok"]
        rows.append(ShotRow(id=s.id, scene_id=s.scene_id or "", chapter=_cn_chapter(s.id),
                            cells=cells, blockers=blockers))

    # ── 汇总 ──
    per_dim: dict[str, dict[str, int]] = {}
    for d in DIMENSIONS:
        c: dict[str, int] = {}
        for r in rows:
            st = r.cells[d["key"]].state
            c[st] = c.get(st, 0) + 1
        per_dim[d["key"]] = c
    blocked = [r.id for r in rows if r.blockers]

    return {
        "project": proj.root.name,
        "dimensions": DIMENSIONS,
        "shots": [r.to_dict() for r in rows],
        "summary": {
            "shots": len(rows),
            "per_dimension": per_dim,
            "blocked_shots": len(blocked),
            "ready_to_render": len(rows) - len(blocked),
        },
        "blockers": blocked,
        "storyboard_source": ("index" if not sb_error else "file-existence-fallback"),
        "storyboard_error": sb_error,
        "notes": (([f"⚠️ 分镜图指纹索引读取失败，已退化为文件存在性检查：{sb_error}"] if sb_error else [])) + [
            "维度按**我们的真实管线**定义，不是照抄调研原文："
            "我们没有独立存在的『场景参考图』（场景是文字锚定）、也没有独立的『配音/配乐』资产"
            "（H3 生成视频时一起产出）。",
            "`stale` 复用 `vm.state` 的指纹口径：定义改过而产物没重出，就是 stale。",
        ],
    }


def render(matrix: dict) -> str:
    """人类可读的矩阵表（终端用）。"""
    dims = matrix["dimensions"]
    heads = ["镜头"] + [d["label"][:6] for d in dims]
    sym = {"ok": " ✅ ", "stale": " ⚠旧", "draft": " ○ ", "missing": " ✗ "}
    out = ["  ".join(f"{h:<7}" for h in heads), "-" * (9 * len(heads))]
    for r in matrix["shots"]:
        line = [f"{r['id']:<7}"]
        for d in dims:
            line.append(f"{sym.get(r['cells'][d['key']]['state'], ' ? '):<7}")
        out.append("  ".join(line))
    s = matrix["summary"]
    out.append("")
    out.append(f"共 {s['shots']} 镜；可渲染 {s['ready_to_render']}；被前置阻塞 {s['blocked_shots']}")
    for d in dims:
        c = s["per_dimension"][d["key"]]
        out.append(f"  {d['label']:<12} " + " ".join(f"{k}={v}" for k, v in sorted(c.items())))
    return "\n".join(out)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("用法: python3 -m vm.audit.completeness <项目> [--json]", file=sys.stderr)
        raise SystemExit(2)
    m = build(sys.argv[1])
    if "--json" in sys.argv:
        print(json.dumps(m, ensure_ascii=False, indent=2))
    else:
        print(render(m))
