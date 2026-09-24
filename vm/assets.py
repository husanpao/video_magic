"""
assets.py —— 场景 / 道具的**概念图 + 抽卡**。

## 为什么场景和道具也要图（但和角色的用处不同）
|      | 图能否注入渲染 | 图的用处 |
| ---- | -------------- | -------- |
| 角色 | ✅ `<Picture N>` | 身份锁定（同一个人跨镜不变） |
| 场景 | ❌ | **渲染前确认"这是我想要的景吗"** |
| 道具 | ❌ | 确认道具长相 |

场景/道具的图**不能**像角色参考图那样喂给 H3：H3 的 `<Picture N>` 槽位语义是
「主体」，一镜最多 3 个 —— 把场景图塞进去会**抢掉角色槽位**。
（我们的场景一致性走的是**文字锚定**：同场景逐字注入同一段描述。）

那图有什么用？**数量级**：西游记的 S4 古井覆盖 **24 镜**。若这个景的调子不对，
24 镜全废（≈16 分钟 GPU）。**出 2 张候选图 = 20 秒**让你先看一眼，这笔账很清楚。
道具同理 —— 金箍棒出现在 10 镜里。

## 出图提示词的两条纪律
1. **场景图必须排除人物**：否则 Qwen-Image 会按描述塞几个人进去，而我们要的是
   「空景」——判断景本身对不对，不该被路人干扰。
2. **道具图用中性背景**：道具要看清形状/材质/磨损，背景越干净越好。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ASSETS_DIRNAME = "assets"
INDEX_FILENAME = "assets.json"
SEED_BASE = 7100

# 场景图：明确排除人物（否则模型会按描述塞人）
SCENE_SUFFIX = (
    " — empty establishing shot of the location itself, COMPLETELY EMPTY, "
    "no people, no characters, no figures, no silhouettes, no crowds; "
    "architectural and environmental detail only, cinematic lighting"
)
# 道具图：中性背景 + 静物
PROP_SUFFIX = (
    " — single object reference shot, the object centered and fully visible, "
    "plain neutral seamless background, soft even studio lighting, "
    "high material detail (shape, material, colour, wear), no people, no hands"
)


@dataclass
class Candidate:
    seed: int
    file: str
    mtime: int = 0
    size: int = 0

    def to_dict(self) -> dict:
        return {"seed": self.seed, "file": self.file, "mtime": self.mtime, "size": self.size}


@dataclass
class AssetRecord:
    kind: str            # scene | prop
    id: str              # S1 / P1
    name: str
    prompt: str          # 实际送给出图的提示词
    candidates: list[Candidate] = field(default_factory=list)
    adopted: str = ""    # 采纳的候选文件名

    def to_dict(self) -> dict:
        return {"kind": self.kind, "id": self.id, "name": self.name,
                "prompt": self.prompt, "adopted": self.adopted,
                "candidates": [c.to_dict() for c in self.candidates]}


def _key(kind: str, aid: str) -> str:
    return f"{kind}:{aid}"


def asset_dir(proj, kind: str, aid: str) -> Path:
    return Path(proj.root) / ASSETS_DIRNAME / f"{kind}_{aid}"


def index_path(proj) -> Path:
    return Path(proj.state_dir) / INDEX_FILENAME


def load_index(proj) -> dict[str, AssetRecord]:
    p = index_path(proj)
    if not p.is_file():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    out: dict[str, AssetRecord] = {}
    for k, v in (d.get("assets") or {}).items():
        if not isinstance(v, dict):
            continue
        out[k] = AssetRecord(
            kind=str(v.get("kind") or ""), id=str(v.get("id") or ""),
            name=str(v.get("name") or ""), prompt=str(v.get("prompt") or ""),
            adopted=str(v.get("adopted") or ""),
            candidates=[Candidate(seed=int(c.get("seed") or 0), file=str(c.get("file") or ""),
                                  mtime=int(c.get("mtime") or 0), size=int(c.get("size") or 0))
                        for c in (v.get("candidates") or []) if isinstance(c, dict)],
        )
    return out


def save_index(proj, recs: dict[str, AssetRecord]) -> Path:
    p = index_path(proj)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {"version": 1, "assets": {k: v.to_dict() for k, v in sorted(recs.items())}}
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return p


# ── 提示词 ──────────────────────────────────────────────────────────────────


def build_prompt(proj, kind: str, aid: str) -> tuple[str, str]:
    """返回 (name, prompt)。复用项目的自动风格句，保证概念图和成片调性一致。"""
    from vm import plan as P

    if kind == "scene":
        scenes = P.load_scenes(proj)
        s = scenes.get(aid)
        if s is None:
            raise ValueError(f"场景 {aid} 不在 scenes.json 里")
        body = s.description or s.name
        # 风格句挂在项目级 style 上（西游记是写实、雨夜地铁是冷青调）
        return s.name, f"{body}{SCENE_SUFFIX}"
    props = P.load_props(proj)
    pr = props.get(aid)
    if pr is None:
        raise ValueError(f"道具 {aid} 不在 props.json 里")
    return pr.name, f"{pr.description}{PROP_SUFFIX}"


# ── 出图 ────────────────────────────────────────────────────────────────────


def gen_candidates(proj, kind: str, aid: str, *, n: int = 2, force: bool = False,
                   log=lambda m: print(m)) -> list[Candidate]:
    """为场景/道具出 n 张候选图。已有的候选会被保留（追加，不覆盖）。"""
    from vm.qi import QIConfig, QwenImage, QIResult
    from vm.state import Project

    proj = Project(proj) if not hasattr(proj, "root") else proj
    name, prompt = build_prompt(proj, kind, aid)
    out_dir = asset_dir(proj, kind, aid)
    out_dir.mkdir(parents=True, exist_ok=True)

    recs = load_index(proj)
    key = _key(kind, aid)
    rec = recs.get(key) or AssetRecord(kind=kind, id=aid, name=name, prompt=prompt)
    rec.name, rec.prompt = name, prompt

    qi = QwenImage(QIConfig())
    qi.require_ready()
    comfy = qi.comfy

    # 已有多少个 → 从那儿接着编 seed（可重复：同 seed 同图）
    have = {c.seed for c in rec.candidates}
    made: list[Candidate] = []
    for i in range(int(n)):
        seed = SEED_BASE + (abs(hash((kind, aid))) % 900) + len(rec.candidates) + i
        if seed in have and not force:
            continue
        log(f"  ▶ {kind} {aid}（{name}）seed={seed}")
        from vm import style as _st
        _neg = _st.negative_for(params.get("style_preset")) if isinstance(params, dict) \
            else _st.negative_for(None)
        res: QIResult = qi.generate(
            prompt, negative=_neg, seed=seed,
            filename_prefix=f"VM_ASSET_{kind}_{aid}",
            log=lambda m: log(f"    ⚠ {m}"),
        )
        img = res.images[0]
        dst = out_dir / f"seed{seed}.png"
        comfy.download(img, dst)
        c = Candidate(seed=seed, file=dst.name,
                      mtime=int(dst.stat().st_mtime), size=dst.stat().st_size)
        rec.candidates.append(c)
        made.append(c)
        log(f"    ✅ {dst.name}  {res.seconds:.1f}s  {dst.stat().st_size // 1024} KB")

    recs[key] = rec
    save_index(proj, recs)
    return made


def adopt(proj, kind: str, aid: str, file: str) -> AssetRecord:
    """采纳一张候选作为"定稿"。"""
    from vm.state import Project
    proj = Project(proj) if not hasattr(proj, "root") else proj
    recs = load_index(proj)
    key = _key(kind, aid)
    rec = recs.get(key)
    if rec is None:
        raise ValueError(f"还没有 {kind} {aid} 的候选图，先出图")
    if file not in {c.file for c in rec.candidates}:
        raise ValueError(f"{file} 不是 {kind} {aid} 的候选")
    if not (asset_dir(proj, kind, aid) / file).is_file():
        raise ValueError(f"候选文件不存在：{file}")
    rec.adopted = file
    recs[key] = rec
    save_index(proj, recs)
    return rec


def list_assets(project: str | os.PathLike[str], root: Path | None = None) -> dict:
    """场景 + 道具的候选总览（给控制台用）。"""
    from vm.state import Project
    from vm.taskctl import resolve_project
    from vm import plan as P
    pdir = Path(project)
    pdir = pdir if pdir.is_absolute() else Path(resolve_project(project, root))
    proj = Project(pdir)
    recs = load_index(proj)
    out: list[dict] = []

    def _row(kind: str, aid: str, name: str, have_def: bool) -> dict:
        rec = recs.get(_key(kind, aid))
        d = asset_dir(proj, kind, aid)
        cands = []
        if rec:
            for c in rec.candidates:
                f = d / c.file
                cands.append({**c.to_dict(), "exists": f.is_file(),
                              "url": f"/view?project={pdir.name}&kind=asset"
                                     f"&name={kind}_{aid}&file={c.file}"})
        return {
            "kind": kind, "id": aid, "name": (rec.name if rec else name),
            "has_definition": have_def,
            "prompt": (rec.prompt if rec else ""),
            "adopted": (rec.adopted if rec else ""),
            "candidates": cands,
            "n": len(cands),
        }

    try:
        for sid, sc in sorted(P.load_scenes(proj).items()):
            out.append(_row("scene", sid, sc.name, True))
    except Exception:                                       # noqa: BLE001
        pass
    try:
        for pid, pr in sorted(P.load_props(proj).items()):
            out.append(_row("prop", pid, pr.name, True))
    except Exception:                                       # noqa: BLE001
        pass
    return {"project": pdir.name, "assets": out,
            "scenes": [a for a in out if a["kind"] == "scene"],
            "props": [a for a in out if a["kind"] == "prop"],
            "dir": str(Path(pdir) / ASSETS_DIRNAME)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python3 -m vm.assets",
        description="场景 / 道具的概念图与抽卡（复用 Qwen-Image 通路）。",
    )
    ap.add_argument("project")
    ap.add_argument("--kind", choices=["scene", "prop"], help="出哪一种")
    ap.add_argument("--id", help="场景 id（S1）或道具 id（P1）；省略则全部")
    ap.add_argument("--n", type=int, default=2,
                    help="每个出几张候选（默认 2 —— 用户明确要求过「抽卡抽 2 张就行」）")
    ap.add_argument("--force", action="store_true", help="忽略已有候选，重新出")
    ap.add_argument("--adopt", help="采纳某个候选文件（配合 --kind/--id）")
    ap.add_argument("--upload", metavar="IMG",
                    help="上传自有图作为候选（配合 --kind/--id）")
    ap.add_argument("--list", action="store_true", help="只列候选状态，不出图")
    a = ap.parse_args(argv)

    if a.list:
        r = list_assets(a.project)
        for x in r["assets"]:
            ad = f"  定稿={x['adopted']}" if x["adopted"] else ""
            print(f"  {x['kind']:<5} {x['id']:<4} {x['name']:<12} 候选 {x['n']}{ad}")
        return 0

    if a.upload:
        if not (a.kind and a.id):
            print("--upload 需要同时给 --kind 和 --id", file=sys.stderr)
            return 2
        from vm.state import Project
        from vm.taskctl import resolve_project
        src = Path(a.upload)
        if not src.is_file():
            print(f"文件不存在：{src}", file=sys.stderr)
            return 2
        c = add_upload(Project(resolve_project(a.project)), a.kind, a.id,
                       src.name, src.read_bytes())
        print(f"已上传 {a.kind} {a.id} → {c.file}（{c.size // 1024} KB）")
        return 0

    if a.adopt:
        if not (a.kind and a.id):
            print("--adopt 需要同时给 --kind 和 --id", file=sys.stderr)
            return 2
        from vm.state import Project
        from vm.taskctl import resolve_project
        rec = adopt(Project(resolve_project(a.project)), a.kind, a.id, a.adopt)
        print(f"已采纳 {a.kind} {a.id} → {rec.adopted}")
        return 0

    from vm.state import Project
    from vm.taskctl import resolve_project
    proj = Project(resolve_project(a.project))
    targets: list[tuple[str, str]] = []
    if a.kind and a.id:
        targets = [(a.kind, a.id)]
    else:
        from vm import plan as P
        if a.kind in (None, "scene"):
            targets += [("scene", s) for s in sorted(P.load_scenes(proj))]
        if a.kind in (None, "prop"):
            targets += [("prop", p) for p in sorted(P.load_props(proj))]
    if not targets:
        print("没有可出图的场景/道具（先跑 plan 抽出 scenes.json / props.json）", file=sys.stderr)
        return 2
    total = 0
    for kind, aid in targets:
        total += len(gen_candidates(proj, kind, aid, n=a.n, force=a.force))
    print(f"\n完成：新出 {total} 张候选")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

def add_upload(proj, kind: str, aid: str, filename: str, raw: bytes) -> Candidate:
    """
    上传自有图，**作为一个候选**（和抽卡出的候选同等待遇，可被采纳）。

    为什么做成"候选"而不是直接定稿：
      · 你手上可能有好几张参考图，想比一比再定
      · 沿用已有的采纳流程，UI 不用多一套状态
      · 定稿只有一个，但候选可以有很多 —— 抽卡的、上传的混在一起挑

    文件名：`upload_<时间戳>_<安全化的原名>`，避免同名覆盖，也便于看出是上传的。
    """
    from vm.state import Project
    proj = Project(proj) if not hasattr(proj, "root") else proj
    if kind not in ("scene", "prop"):
        raise ValueError(f"kind 必须是 scene 或 prop，收到 {kind!r}")
    if not raw:
        raise ValueError("上传内容为空")
    name, _prompt = build_prompt(proj, kind, aid)   # 顺带校验 id 存在

    out_dir = asset_dir(proj, kind, aid)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.\-]", "_", Path(filename).name)[-48:] or "image.png"
    if not safe.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
        safe += ".png"
    fname = f"upload_{int(time.time())}_{safe}"
    dst = out_dir / fname
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    with open(tmp, "wb") as f:
        f.write(raw)
    os.replace(tmp, dst)

    recs = load_index(proj)
    key = _key(kind, aid)
    rec = recs.get(key) or AssetRecord(kind=kind, id=aid, name=name, prompt=_prompt)
    rec.name, rec.prompt = name, _prompt
    c = Candidate(seed=0, file=fname, mtime=int(dst.stat().st_mtime), size=dst.stat().st_size)
    rec.candidates.append(c)
    recs[key] = rec
    save_index(proj, recs)
    return c
