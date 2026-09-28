"""
refs.py —— 「这一镜该用哪个角色的哪张参考图」的**唯一**解析器（P3.2）。

## 为什么单独一个模块

解析参考图的代码原先有**两处**，各算各的：

· `_ref_names_for()`（`gen.py:318`）→ 决定**实际提交给模型**的是哪些图；
· `render_shot()` 里的 `ref_paths = [proj.refs_dir / f"char_{c}.png" ...]`（`gen.py:367`）
  → 决定**指纹**算的是哪些图。

两处今天恰好等价（都是 `char_<名>.png`），但"恰好"是最靠不住的：
P3 要加"同一角色在不同章用不同定妆照"（换装 / 受伤 / 回忆段落），
只要有一处跟上、另一处没跟上，就会出现 ——

  **指纹说"输入没变"，于是镜头不重渲；而实际提交的是另一张图。**
  或者反过来：图没变，指纹却因为路径不同而判 stale，白烧 GPU。

这正是本仓库在风格后缀那件事上刚踩过的同一类错（`chars.portrait_inputs()`
就是为了"指纹与提交同源"而存在的）。所以变体能力**必须**建立在这个单一出口上。

## 顺序契约（不可协商）

`chars[0] ↔ ref_image_0 ↔ <Picture 1>/<Subject 1>` 严格按 `shot.chars` 顺序一一对应
（`CONTRACTS.md:147-154`）。**任何缺项都必须显式报错，绝不静默跳过** ——
少一张会让后面所有索引整体前移，画面照常生成、质检照常通过，
只是每个人的脸都安在别人的身体上。这是本项目记录在案的"最难查的一类 bug"。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

VARIANT_SEP = "@ch"
VariantRef = tuple[Path, str]        # (路径, 出处说明)：default / chapter


def chapter_of(shot_id: str) -> int | None:
    """镜头号 `1-3-02` → 章号 1。解析不出返回 None（手工造的奇怪 id 走默认图）。"""
    m = re.match(r"^(\d+)-", str(shot_id or ""))
    return int(m.group(1)) if m else None


def variant_path(proj, char: str, chapter_no: int | None) -> Path | None:
    """某角色在某章的**专属**定妆照路径；没建过专属图时返回 None（**不去猜**）。"""
    if not chapter_no:
        return None
    p = Path(proj.refs_dir) / f"char_{char}{VARIANT_SEP}{int(chapter_no):02d}.png"
    return p if p.is_file() else None


def default_path(proj, char: str) -> Path:
    return Path(proj.refs_dir) / f"char_{char}.png"


def resolve_ref(proj, char: str, chapter_no: int | None) -> VariantRef | None:
    """
    一个角色在一镜里的参考图，按「章内专属 > 全局默认」两级取。

    ★ 章节专属图**只在文件真实存在时**才生效 —— 不做"就近向前一章借"之类的猜测。
    理由：借来的图是另一个造型，而提示词写的是当前造型，
    会产生"文字与图不一致"那条本仓库实测过会压过参考图的问题。
    没有专属图就老老实实用默认图，并在影响清单里说明这一章还没出过专属定妆。
    """
    v = variant_path(proj, char, chapter_no)
    if v is not None:
        return v, f"chapter{int(chapter_no):02d}"
    d = default_path(proj, char)
    if d.is_file():
        return d, "default"
    return None


def refs_for_shot(proj, chars: list[str], shot_id: str) -> list[VariantRef]:
    """
    按 `chars` 顺序解析一镜要用的全部参考图。**不**在这里报错，交给调用方决定。

    返回长度与 `chars` 一致，取不到的项为 `None` —— 保持位置，绝不能"跳过缺项后压缩列表"。
    """
    ch = chapter_of(shot_id)
    return [resolve_ref(proj, str(c), ch) for c in (chars or [])]


def require_refs(proj, chars: list[str], shot_id: str) -> list[VariantRef]:
    """
    同 `refs_for_shot`，但任何一项取不到就抛 —— 渲染路径必须用这个。

    错误信息里带上"这一章有没有专属定妆照"，因为最常见的成因是：
    新加了章节造型却没出图，用户以为出过了。
    """
    from vm.comfy import ComfyError

    got = refs_for_shot(proj, chars, shot_id)
    missing = [str(c) for c, r in zip(chars or [], got) if r is None]
    if missing:
        ch = chapter_of(shot_id)
        raise ComfyError(
            f"镜头 {shot_id} 的角色 {missing} 缺少参考图："
            f"refs/char_<名>.png"
            + (f"（第 {ch} 章也没有专属图 char_<名>{VARIANT_SEP}{ch:02d}.png）" if ch else "")
            + "。参考图顺序必须与 chars 一一对应，缺项会让后面所有索引前移、"
              "角色与图整体错位而画面仍正常生成 —— 所以不允许静默跳过。请先出定妆照。"
        )
    return [r for r in got if r is not None]


def ref_paths(proj, chars: list[str], shot_id: str) -> list[Path]:
    """只要路径列表（指纹用）。与 `require_refs` 同源，保证「提交什么就算什么」。"""
    return [p for p, _src in require_refs(proj, chars, shot_id)]


def list_variants(proj, char: str) -> list[dict]:
    """某角色的所有造型图（含全局默认），给实体总表 / 角色屏列出来。"""
    out: list[dict] = []
    d = default_path(proj, char)
    if d.is_file():
        out.append({"path": d.name, "chapter": None, "kind": "default",
                    "mtime": int(d.stat().st_mtime), "size": d.stat().st_size})
    for p in sorted(Path(proj.refs_dir).glob(f"char_{char}{VARIANT_SEP}*.png")):
        m = re.search(rf"{re.escape(VARIANT_SEP)}(\d+)\.png$", p.name)
        if m:
            out.append({"path": p.name, "chapter": int(m.group(1)), "kind": "chapter",
                        "mtime": int(p.stat().st_mtime), "size": p.stat().st_size})
    return out


def variant_filename(char: str, chapter_no: int | None) -> str:
    """落盘文件名。chapter 为 None ⇒ 全局默认图。"""
    if not chapter_no:
        return f"char_{char}.png"
    return f"char_{char}{VARIANT_SEP}{int(chapter_no):02d}.png"


def shots_using_variant(proj, chapter_no: int | None) -> list[str]:
    """某章的镜头 id 列表（给「删掉这章的专属图会影响几镜」这类提示用）。"""
    from vm.shots import load_shots_dir

    if not chapter_no:
        return []
    root = Path(proj.root) if hasattr(proj, "root") else Path(proj)
    return [s.id for s in load_shots_dir(root / "shots") if chapter_of(s.id) == chapter_no]
