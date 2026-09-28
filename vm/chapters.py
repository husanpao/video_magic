"""
chapters.py —— 章节清单（`chapters.json`）：项目里"有哪些章、按什么顺序"的唯一真相。

## 为什么要有这个文件

收敛前，"章的顺序"这件事**没有任何地方存着**，而是每次现算：
`sorted(novel_dir.glob("*.md"))` —— 按 **Unicode 码位**排文件名。

中文序号在码位表里的顺序是这样的：一(U+4E00) < 三(U+4E09) < 二(U+4E8C) < 十(U+5341) < 四(U+56DB)。
于是《第一章 / 第二章 / 第三章 / 第四章》实测排成
**「第一章, 第三章, 第二章, 第十章, 第四章」**。

这个排序同时被三个地方当真相用，所以它不是"显示顺序不好看"而是三个真实故障：

| 谁在用它 | 当作什么 | 排错的后果 |
|---|---|---|
| `pipeline.stage_plan` | "第 N 章"的 N | `--chapter 3` 拆到的不是第三章，是排序后第 3 个文件 |
| `taskctl.chapter_table` | `index` 字段 | `index` 与从文件名解析的 `no` 分叉，界面按 `no` 筛、按 `index` 号 |
| `plan.chapter_number` | 镜头表文件名 `chapterNN.json` | 两章都叫「第一章」时**后跑的覆盖先跑的** |

所以这里把顺序**存下来**（`chapters.json`），而不是继续每次现算。
顺带解决"用户手工排章节顺序"这件事需要一个能写的地方 —— 那也是 P1 章节管理的前置。

## 章号（`no`）是全局主键

`no` 决定了三件事：镜头表文件名 `shots/chapterNN.json`、镜头 id 前缀（`1-2-03` = 第 1 章）、
成片集数 `EP01.mp4`。所以：

* `no` **一旦分配就不再变**（改了会让已有产物错位 —— 和 `CONTRACTS.md:483` 里
  "拆/并/插不改已有镜头号"同一个道理）；
* 两个文件解析出同一个 `no` 时**报错**，不静默重排（静默重排 = 上一轮 P0.3 要修的
  "跨章 ID 歧义"的翻版）；
* 顺序变了（用户拖拽/插到中间）**不动 `no`**，只动清单里的位置。

## 与 novel/ 目录的关系

`novel/*.md` 是正文的唯一真相，`chapters.json` 只是**索引**。
它可以被删掉 —— `load()` 发现缺失就会从目录重建，
重建时按"正确的"顺序排（中文序数解析 → 数字 → 全名），并把 `no` 按解析结果分配。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import fsutil

MANIFEST = "chapters.json"
VERSION = 1
#: 正文允许的后缀。顺序 = 优先级（同名 md 优先于 txt）
NOVEL_SUFFIXES = (".md", ".txt")

_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100}
#: 「第X章 / 第X回 / 第X节」—— 回是章回体（西游记），节是常见的手稿分层
CHAPTER_RE = re.compile(r"第\s*([零一二两三四五六七八九十百\d]{1,8})\s*[章回节]")
NUM_PREFIX_RE = re.compile(r"^\s*0*(\d{1,4})(?![\d])")


@dataclass
class ChapterEntry:
    """清单里的一条。`file` 是**相对项目根**的路径，便于整目录搬迁。"""

    no: int
    title: str
    file: str
    chars: int = 0
    body_sha: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"no": self.no, "title": self.title, "file": self.file,
                "chars": self.chars, "body_sha": self.body_sha}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ChapterEntry":
        return cls(no=int(d.get("no") or 0), title=str(d.get("title") or ""),
                   file=str(d.get("file") or ""), chars=int(d.get("chars") or 0),
                   body_sha=str(d.get("body_sha") or ""))


# ── 解析 ────────────────────────────────────────────────────────────────────


def parse_chapter_no(stem: str) -> int | None:
    """从文件名解析章号：「第一章\_雨夜地铁」→ 1、「第10章」→ 10、「第回十二回」→ 12。"""
    m = CHAPTER_RE.search(stem)
    if m:
        try:
            n = parse_cn_number(m.group(1))
        except ValueError:
            n = None
        if n:
            return n
    m2 = NUM_PREFIX_RE.search(stem)
    if m2:
        n = int(m2.group(1))
        return n or None
    return None


def parse_cn_number(s: str) -> int:
    """
    中文数字 → int。`十`→10、`十二`→12、`二十`→20、`二十三`→23、`一百零八`→108。

    解析不出抛 ValueError（调用方按"这文件名里没有章号"处理）。
    比收敛前的 `taskctl._cn_num` 多支持"百"，并且**不再**把解析失败悄悄变成 0 ——
    旧实现里 `d.get(a, 0)` 对「万」「亿」这类别字直接返回 0，于是"第萬章"→ 0 →
    退化成排序序号，症状是"某章莫名其妙跑到最前面"。
    """
    s = (s or "").strip()
    if not s:
        raise ValueError("空的")
    if s.isdigit():
        return int(s)
    if all(c in _CN_DIGITS for c in s):
        if len(s) == 1:
            return _CN_DIGITS[s]
        raise ValueError(s)                       # 「一二三」这种不是数字
    total = 0
    current = 0
    seen = False
    for ch in s:
        if ch in _CN_DIGITS:
            current = _CN_DIGITS[ch]
            seen = True
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            # 「十二」的开头没有数字位，当作 1×十
            total += (current if current else 1) * unit
            current = 0
            seen = True
        else:
            raise ValueError(s)
    if not seen:
        raise ValueError(s)
    return total + current


def natural_key(stem: str) -> tuple:
    """
    重建清单时用的排序键。**先按解析出的章号，再按文件名**。

    旧写法 `sorted(glob)` 只有一级、且那一级是 Unicode 码位 —— 这就是全部问题所在。
    解析不出章号的文件（"序章"、"附录"、`00_intro.md`）落在有章号文件的后面还是前面，
    取决于 `no` 有没有：这里把"解析不出"统一放最后（按名字排），
    因为"序章/尾声"这种在实操里都是手工拖的，P1 给了拖拽入口，不该靠猜。
    """
    no = parse_chapter_no(stem)
    return (0, no, stem) if no is not None else (1, 0, stem)


def body_sha(text: str) -> str:
    """正文指纹（前 12 位）。P1.5 的「需重拆」判定就是比这个。"""
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]


# ── 读 / 写 ─────────────────────────────────────────────────────────────────


def root_of(proj) -> Path:
    """
    取项目根。接受 `Project` / `Path` / `str` 三种形态。

    ★ 不能写 `getattr(proj, "root", proj)`：**`pathlib.Path` 自己就有 `.root`**
    （绝对路径的是 `'/'`）。那样传 Path 进来会把项目根算成根目录，
    于是 `chapters.json` 试图写到 `/chapters.json`。实测踩过。
    """
    if isinstance(proj, (str, Path)):
        return Path(proj)
    root = getattr(proj, "root", None)
    return Path(root) if root is not None else Path(proj)


def manifest_path(proj) -> Path:
    return root_of(proj) / MANIFEST


def load(proj) -> list[ChapterEntry]:
    """
    读清单。**不**在读取时写盘（读路径产生写副作用，会让"打开界面"改变项目状态）。
    缺失/损坏 → 空列表，由调用方决定是 `rebuild()` 还是直接用目录。
    """
    data = fsutil.read_json(manifest_path(proj))
    if not isinstance(data, dict):
        return []
    out: list[ChapterEntry] = []
    for raw in data.get("chapters") or []:
        if not isinstance(raw, dict):
            continue
        e = ChapterEntry.from_dict(raw)
        if e.file:
            out.append(e)
    return out


def save(proj, entries: list[ChapterEntry]) -> Path:
    p = manifest_path(proj)
    data = {"version": VERSION,
            "chapters": [e.to_dict() for e in entries]}
    return fsutil.write_json(p, data)


def list_novel_files(proj) -> list[Path]:
    """novel/ 下的正文文件（隐藏文件与 .tmp 残骸不算）。**已按正确顺序排好**。"""
    d = root_of(proj) / "novel"
    if not d.is_dir():
        return []
    files: list[Path] = []
    for suf in NOVEL_SUFFIXES:
        files += [p for p in d.glob(f"*{suf}")]
    files = [p for p in files if not p.name.startswith(".") and not p.name.endswith(".tmp")]
    # 后缀优先级：同名不同后缀时 md 在前，保证顺序稳定
    rank = {s: i for i, s in enumerate(NOVEL_SUFFIXES)}
    return sorted(files, key=lambda p: (natural_key(p.stem), rank.get(p.suffix.lower(), 9), p.name))


def read_body(proj, rel_or_path) -> str:
    p = Path(rel_or_path)
    if not p.is_absolute():
        p = root_of(proj) / p
    if not p.is_file():
        return ""
    return p.read_text(encoding="utf-8", errors="replace")


def rebuild(proj) -> list[ChapterEntry]:
    """
    从目录重建清单（**不落盘**）。已存在的条目尽量保住 `no`：
    按文件名对上号的沿用旧 `no`，对不上的用解析结果，都没有就顺延分配。
    """
    old = load(proj)
    by_file = {e.file: e for e in old}
    used: set[int] = set()
    out: list[ChapterEntry] = []
    for p in list_novel_files(proj):
        rel = f"novel/{p.name}"
        prev = by_file.get(rel)
        if prev and prev.no and prev.no not in used:
            no = prev.no
        else:
            no = parse_chapter_no(p.stem)
            if not no or no in used:
                no = _first_free(used)
        used.add(no)
        text = p.read_text(encoding="utf-8", errors="replace")
        out.append(ChapterEntry(no=no, title=p.stem, file=rel,
                                chars=len(text.strip()), body_sha=body_sha(text)))
    return out


def _first_free(used: set[int], start: int = 1) -> int:
    n = start
    while n in used:
        n += 1
    return n


def sync(proj, *, write: bool = True) -> dict:
    """
    对齐清单与目录：新文件补进来、删掉的文件移出清单、正文字数/指纹刷新。

    ★ **已有条目的位置原样保留** —— 顺序是用户的东西（P1 会给拖拽），
    扫描目录这个动作没有资格重排它。新文件一律**追加到末尾**（多章一次导入时，
    末尾就是它们按 `natural_key` 排好的相对顺序）。
    空清单是唯一的例外：那时"追加"就等于整表按自然序建立。

    返回 `{"chapters": [...], "added": [...], "removed": [...], "changed": [...]}`
    """
    old = load(proj)
    files = list_novel_files(proj)
    rels = [f"novel/{p.name}" for p in files]
    present = set(rels)
    added: list[str] = []
    changed: list[str] = []
    used = {e.no for e in old if e.no}

    def _refresh(e: ChapterEntry) -> ChapterEntry:
        nonlocal changed
        text = (root_of(proj) / e.file).read_text(encoding="utf-8", errors="replace")
        sha, chars = body_sha(text), len(text.strip())
        if e.body_sha != sha or e.chars != chars:
            changed.append(e.file)
        e.body_sha, e.chars = sha, chars
        return e

    # 骨架：清单里已有的、且文件还在的，按清单原顺序
    out: list[ChapterEntry] = [_refresh(e) for e in old if e.file in present]
    # 新来的：按自然序追加
    have = {e.file for e in out}
    for rel in rels:
        if rel in have:
            continue
        p = Path(rel)
        no = parse_chapter_no(p.stem)
        if not no or no in used:
            no = _first_free(used)
        used.add(no)
        text = (root_of(proj) / rel).read_text(encoding="utf-8", errors="replace")
        out.append(ChapterEntry(no=no, title=p.stem, file=rel,
                                chars=len(text.strip()),
                                body_sha=body_sha(text)))
        added.append(rel)

    removed = [e.file for e in old if e.file not in present]
    if write and (added or removed or changed or not old):
        save(proj, out)
    return {"chapters": [e.to_dict() for e in out], "added": added,
            "removed": removed, "changed": changed,
            "file": str(manifest_path(proj))}


def ordered_files(proj) -> list[Path]:
    """
    拆镜/合成实际要走的文件顺序 —— 本模块对外的**唯一入口**。

    有清单按清单（清单就是顺序真相），没清单退回 `list_novel_files()` 的解析排序。
    两条路径都不再是"按码位排文件名"，这就是 P0.1 修的东西。
    """
    r = root_of(proj)
    entries = load(proj)
    if entries:
        out = [(r / e.file) for e in entries if (r / e.file).is_file()]
        if out:
            return out
    return list_novel_files(proj)


def plan_targets(proj) -> list[tuple[int, Path]]:
    """
    拆镜要走的 `(章号, 正文文件)` 序列 —— `pipeline.stage_plan` 的入口。

    单独一个函数而不是让调用方自己 `ordered_files()` + `parse_chapter_no()`，
    是因为**清单里的 `no` 才是真相**：撞车被顺延分配过（`第二回` 与 `第二章` 都解析成 2）
    的文件，名字里解析出来的数字和登记的 `no` 并不一致。
    调用方自己解析一遍，就会拆出和镜头表文件名不同的章号 —— 那是最难查的一类错位。
    """
    r = root_of(proj)
    entries = load(proj)
    if entries:
        out = [(e.no, r / e.file) for e in entries if (r / e.file).is_file()]
        if out:
            return out
    return [(parse_chapter_no(p.stem) or i, p) for i, p in enumerate(list_novel_files(proj), 1)]


def find_conflicts(proj) -> list[str]:
    """
    章号冲突 / 文件缺失的自检。返回人类可读的问题清单（空 = 干净）。

    为什么单独有它：`no` 是镜头表文件名与镜头 id 前缀的主键，
    两个条目同 `no` 会让后一章的 `chapterNN.json` **覆盖**前一章，
    而画面照样能渲出来（只是少了一章），属于最难发现的一类。
    """
    problems: list[str] = []
    seen: dict[int, str] = {}
    for e in load(proj):
        if not e.no:
            problems.append(f"章节「{e.title}」没有章号（no=0）")
        elif e.no in seen:
            problems.append(f"章号 {e.no} 被两个文件共用：「{seen[e.no]}」与「{e.title}」"
                            f"—— 镜头表 chapter{e.no:02d}.json 会被后者覆盖")
        else:
            seen[e.no] = e.title
        if not (root_of(proj) / e.file).is_file():
            problems.append(f"清单里的正文文件不存在：{e.file}（章节「{e.title}」）")
    return problems


def assign_no(proj, stem: str) -> int:
    """给一个新章节分配章号：优先用文件名解析出的，占用则顺延。"""
    used = {e.no for e in load(proj) if e.no}
    no = parse_chapter_no(stem)
    if not no or no in used:
        no = _first_free(used)
    return no


def move(proj, no: int, *, to_no: int | None = None, before_no: int | None = None) -> list[dict]:
    """
    调整某章在清单里的**位置**。只动位置，不改任何 `no`
    （改 `no` 会让镜头表文件名、镜头 id 前缀、成片集数三处一起错位）。
    """
    entries = load(proj)
    idx = next((i for i, e in enumerate(entries) if e.no == no), None)
    if idx is None:
        raise ValueError(f"清单里没有第 {no} 章")
    e = entries.pop(idx)
    tgt = None
    if before_no is not None:
        tgt = next((i for i, x in enumerate(entries) if x.no == before_no), None)
    if tgt is None and to_no is not None:
        tgt = next((i for i, x in enumerate(entries) if x.no == to_no), None)
        if tgt is not None:
            tgt += 1
    if tgt is None:
        entries.append(e)
    else:
        entries.insert(tgt, e)
    save(proj, entries)
    return [x.to_dict() for x in entries]


def dump(proj) -> str:
    """给人/CLI 看的清单摘要。"""
    entries = load(proj)
    if not entries:
        return "（chapters.json 不存在或为空）"
    lines = []
    for i, e in enumerate(entries, 1):
        mark = "" if (root_of(proj) / e.file).is_file() else "  ⚠ 正文缺失"
        lines.append(f"{i:>3}. 第 {e.no:>3} 章  {e.title:<28} {e.chars:>6} 字  {e.file}{mark}")
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(prog="python3 -m vm.chapters",
                                 description="章节清单（chapters.json）查看与重建")
    ap.add_argument("project")
    ap.add_argument("--rebuild", action="store_true", help="从 novel/ 目录重建清单并写盘")
    ap.add_argument("--check", action="store_true", help="自检章号冲突与缺失文件")
    a = ap.parse_args()
    from vm.state import Project
    from vm.taskctl import resolve_project

    pr = Project(resolve_project(a.project))
    if a.rebuild:
        entries = rebuild(pr)
        save(pr, entries)
        print(f"✓ 已按正文文件名重建 {len(entries)} 章 → {manifest_path(pr)}")
    if a.check:
        probs = find_conflicts(pr)
        print("✓ 无问题" if not probs else "⚠ 发现问题：")
        for x in probs:
            print(f"  · {x}")
        raise SystemExit(1 if probs else 0)
    print(dump(pr))
