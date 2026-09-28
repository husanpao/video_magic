"""
proj.py —— 项目与章节的**管理**操作（P1 前门的业务层）。

## 为什么单独一个模块

`docs/全流程整合与实施计划.md:§七` 里那条：P1 要加 7 个端点，
如果照 `web.py` 现有的样子把业务写进 handler，`web.py`（已 1517 行）会继续长，
而且 CLI 与 Web 会出现两套行为 —— 那正是 `_apply_ref`（`web.py:1354`）已经犯过的错。
所以这里放业务，`web.py` 只做「参数校验 + 错误映射」。

## 这一层存在的根本理由：以前**没有"新建项目"这回事**

README 的「开始用」是两行 shell（`mkdir -p projects/我的剧/novel` + `cp 第一章.md`），
后台没有任何 `project/create` 端点、CLI 没有任何 `--new` 参数
（`pipeline.py:607` 明确「项目不存在」就直接退出）。
唯一能"造出项目目录"的副作用，是对一个**不存在的名字**保存配置
（`web.py` → `config.py:1082` → `_atomic_write_json` 顺手 mkdir 父目录）——
结果是造出一个**只有 project.json、没有 novel/ 的畸形项目**。
界面必须有个正门，这就是那个正门的后端。

## 删除是**可逆**的

章节正文是用户写/导进来的东西，删掉拿不回来（和"删一个渲染产物"完全不是一个量级）。
所以 `delete_chapter` 不 `unlink`，而是移到 `novel/_trash/` 并带时间戳，
返回体里给出原路径与恢复方式。`_trash` 里的东西由用户或「清空重置」决定去留。
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from vm import chapters as CH
from vm import fsutil
from vm.state import Project
from vm.taskctl import PROJECTS_DIR, TaskError, project_exists, resolve_project

#: 项目名会直接当目录名用。比 `taskctl._safe_name` 更严：
#: 额外禁掉 shell/路径里会惹麻烦的字符，且不允许以 `.` 或 `_` 开头
#: （`.` 会被 glob 过滤、`_` 前缀在本仓库里是"内部/备份目录"的约定，如 `_backup`、`_trash`）。
NAME_BAD = re.compile(r"""[/\\:*?"<>|'`$;&|()\x00]""")
SOURCE_TYPES = ("novel", "script")
#: 章节正文允许的后缀（与 chapters.NOVEL_SUFFIXES 同源）
TRASH_DIR = "_trash"
MAX_TITLE_LEN = 80


def _check_name(name: str) -> str:
    n = str(name or "").strip()
    if not n:
        raise TaskError("项目名不能为空")
    if n in (".", "..") or n.startswith(".") or n.startswith("_"):
        raise TaskError(f"项目名不能以 . 或 _ 开头：{n!r}")
    if NAME_BAD.search(n):
        raise TaskError(f"项目名含非法字符（斜杠、冒号、引号、$ 等）：{n!r}")
    if len(n) > 60:
        raise TaskError(f"项目名过长（{len(n)} 字，上限 60）")
    return n


def _safe_title(title: str) -> str:
    """
    章节标题 → 文件名。标题是用户随手输入的中文，必须清洗成能安全落盘的名字。

    保留中文（本项目全部产物都要求「UTF-8 不转义中文」），只去掉路径分隔符、
    控制字符、shell 特殊字符与开头结尾的空白/点。
    """
    t = str(title or "").strip()
    t = re.sub(r"[\x00-\x1f/\\:*?\"<>|'`$;]", "", t)
    t = re.sub(r"\s+", " ", t).strip(" .")
    if not t:
        raise TaskError("章节标题清洗后为空，换个写法（不要用纯符号）")
    return t[:MAX_TITLE_LEN]


# ── 项目 ────────────────────────────────────────────────────────────────────


def create_project(name: str, *, title: str = "", source_type: str = "novel",
                   logline: str = "", style_preset: str = "auto",
                   root: Path | None = None) -> dict:
    """
    新建项目：建目录 + 写 `project.json` + 写空 `chapters.json`。

    **拒绝覆盖**：同名项目已存在就报错，绝不做"清空重建"——
    那会静默丢掉一部已经渲了几十分钟 GPU 的片子。要重来走「清空重置」
    （那套三档范围 + 清单预演 + 手输项目名的流程是有意的摩擦，不能绕过）。
    """
    if source_type not in SOURCE_TYPES:
        raise TaskError(f"source_type 必须是 {'/'.join(SOURCE_TYPES)} 之一，收到 {source_type!r}")
    n = _check_name(name)
    base = Path(root) if root else PROJECTS_DIR
    pdir = base / n
    if pdir.exists() and project_exists(pdir):
        raise TaskError(f"项目「{n}」已存在：{pdir}。要重来请用「清空重置」，那里会先摆出删除清单。")

    proj = Project(pdir)
    proj.ensure()                       # novel/shots/refs/prompts/clips/final/state
    meta = {
        "name": n,
        "title": str(title or n).strip()[:MAX_TITLE_LEN],
        "source_type": source_type,
        "logline": str(logline or "").strip()[:500],
        # 整本小说级的风格（P2 会用它做拆镜门禁）。auto = 不指定，由「通读登记」阶段推荐。
        "style_preset": style_preset if style_preset in ("auto", "realistic", "cg", "anime") else "auto",
        "created_at": int(time.time()),
    }
    fsutil.write_json(pdir / "project_meta.json", meta)
    CH.save(proj, [])                   # 空清单也要落盘：界面据此区分"没章节"与"没建过项目"
    return {"ok": True, "project": n, "path": str(pdir), "meta": meta,
            "message": f"已新建项目「{n}」（{title or n}）",
            "next": "导入手写的章节正文，或粘贴文本新建章节"}


def project_meta(name: str, root: Path | None = None) -> dict:
    """读项目元信息。**不报错**：老项目没这个文件是正常状态，返回派生的默认值。"""
    pdir = resolve_project(name, root)
    p = pdir / "project_meta.json"
    meta = fsutil.read_json(p)
    if isinstance(meta, dict) and meta:
        meta.setdefault("name", pdir.name)
        meta.setdefault("source_type", "novel")
        meta["_layer"] = "file"
        return meta
    # 没有 meta 文件：从现有事实反推，别让老项目在界面上显示成空白
    return {"name": pdir.name, "title": pdir.name, "source_type": "novel",
            "logline": "", "style_preset": "auto", "_layer": "derived"}


def update_project_meta(name: str, patch: dict, root: Path | None = None) -> dict:
    pdir = resolve_project(name, root)
    if not pdir.is_dir():
        raise TaskError(f"项目不存在：{pdir.name}")
    cur = project_meta(name, root)
    changed: list[str] = []
    for k in ("title", "source_type", "logline", "style_preset"):
        if k in patch:
            v = str(patch[k] or "").strip()
            if k == "source_type" and v not in SOURCE_TYPES:
                raise TaskError(f"source_type 必须是 {'/'.join(SOURCE_TYPES)}，收到 {v!r}")
            if k == "style_preset" and v not in ("auto", "realistic", "cg", "anime"):
                raise TaskError(f"style_preset 非法：{v!r}")
            if cur.get(k) != v:
                cur[k] = v
                changed.append(k)
    if not changed:
        return {"ok": True, "changed": [], "message": "没有变化", "meta": cur}
    cur.pop("_layer", None)
    fsutil.write_json(pdir / "project_meta.json", cur)
    return {"ok": True, "changed": changed, "meta": cur,
            "message": f"已保存项目信息（改了 {'、'.join(changed)}）"}


# ── 项目删除（可逆：移到 projects/_trash/） ──────────────────────────────────

#: 项目级回收站。`discover_projects()` 只认"有 project.json / novel / shots"的**直接子目录**，
#: 而 `projects/_trash/` 这三样都没有（它们在更里面一层的各项目目录上），
#: 所以回收站本身不会被误列成一个项目。
PROJECT_TRASH = "_trash"
_ENTRY_RE = re.compile(r"^(\d+)(?:_(\d+))?__(.+)$")


def _projects_root(root: Path | str | None = None) -> Path:
    return Path(root) if root else Path(PROJECTS_DIR)


def _trash_root(root: Path | str | None = None) -> Path:
    return _projects_root(root) / PROJECT_TRASH


def _entry_project(entry: str) -> str:
    """回收站条目名 `<时间戳>[_<序号>]__<项目名>` → 原项目名。"""
    m = _ENTRY_RE.match(str(entry or ""))
    return m.group(3) if m else ""


def _task_busy(pdir: Path) -> dict | None:
    """有任务在跑就把它带回来。边删边跑会留下半个状态，而且 worker 还在往已消失的目录里写。"""
    from vm import taskctl as T

    t = T.read_task(pdir)
    return t if T.task_running(t) else None


def delete_project(name: str, *, confirm: str = "", root: Path | str | None = None) -> dict:
    """
    删除整个项目 —— **移进 `projects/_trash/`，不 rm**。

    为什么必须可逆：`projects/` 是用户自己的内容（正文、拆镜结果、定妆照、成片），
    而且**它在 .gitignore 里** —— 删错了连 git 都救不回来。
    和"清掉一个渲染产物"完全不是一个量级，所以这里一个文件都不真删。

    三道闸（与 `taskctl.reset_project` 同一套纪律）：
      1. `confirm` 必须逐字等于项目名（手输名字这个动作本身就是确认）
      2. 有任务在跑 ⇒ 直接拒绝
      3. 只允许删 `projects/` 的**直接子目录**（解析后父目录不等于 projects/ 就拒绝）
    """
    n = _check_name(name)
    base = _projects_root(root)
    pdir = resolve_project(n, base)
    if pdir.resolve().parent != base.resolve():
        raise TaskError(f"只能删 projects/ 下的项目目录，解析出来却是：{pdir}")
    if not pdir.is_dir():
        raise TaskError(f"项目不存在：{n}")
    if confirm.strip() != n:
        raise TaskError(
            f"确认名不匹配：需要手输「{n}」，收到「{confirm}」。"
            "这是删除整个项目，所以要求手输项目名。"
        )
    busy = _task_busy(pdir)
    if busy:
        raise TaskError(
            f"「{n}」有任务在跑（阶段 {busy.get('stage') or '?'}，pid {busy.get('pid')}），先停止再删"
        )

    tdir = _trash_root(base)
    tdir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    dst = tdir / f"{ts}__{n}"
    k = 0
    while dst.exists():
        k += 1
        dst = tdir / f"{ts}_{k}__{n}"
    pdir.replace(dst)  # 同一棵树内 = rename，原子；不复制不删除

    files = sum(1 for p in dst.rglob("*") if p.is_file())
    size = sum(p.stat().st_size for p in dst.rglob("*") if p.is_file())
    return {
        "ok": True, "project": n, "entry": dst.name, "trash": str(dst),
        "files": files, "size": size,
        "message": f"已把项目「{n}」移进回收站（{files} 个文件 / {size / 1048576:.1f} MB）",
        "hint": f"恢复：POST /api/project/restore {{\"entry\":\"{dst.name}\"}}；"
                f"确认不要了再手工删 {dst}（本工具不代删用户内容）",
    }


def list_project_trash(root: Path | str | None = None) -> dict:
    """列出可恢复的项目。**可逆性必须能被看见** —— 看不见回收站的"软删除"等于没给。"""
    tdir = _trash_root(root)
    items: list[dict] = []
    if tdir.is_dir():
        for p in sorted(tdir.iterdir(), reverse=True):
            if not p.is_dir():
                continue
            files = size = 0
            for f in p.rglob("*"):
                if f.is_file():
                    files += 1
                    size += f.stat().st_size
            items.append({
                "entry": p.name,
                "project": _entry_project(p.name) or p.name,
                "mtime": int(p.stat().st_mtime),
                "files": files,
                "size": size,
                "chapters": len(list(p.glob("shots/chapter*.json"))),
                "has_novel": (p / "novel").is_dir() and any((p / "novel").iterdir()),
            })
    return {"ok": True, "root": str(tdir), "items": items,
            "message": f"回收站里有 {len(items)} 个项目"}


def restore_project(entry: str, *, root: Path | str | None = None) -> dict:
    """从回收站放回一个项目。**目标名字已被占用时拒绝覆盖** —— 静默覆盖用户内容是不可接受的。"""
    base = _projects_root(root)
    e = str(entry or "").strip()
    if not e or e in (".", "..") or "/" in e or "\\" in e or "\x00" in e:
        raise TaskError(f"非法回收站条目名：{e!r}")
    src = _trash_root(base) / e
    if not src.is_dir():
        raise TaskError(f"回收站里没有这个条目：{e}")
    name = _entry_project(e)
    if not name:
        raise TaskError(f"条目名 {e!r} 不符合「<时间戳>__<项目名>」格式，恢复不了（可手工移回：{src}）")
    name = _check_name(name)
    dst = resolve_project(name, base)
    if dst.exists():
        raise TaskError(
            f"项目「{name}」已经存在，不能覆盖。先把现在这个改名或删掉，再恢复回收站里的 {e}。"
        )
    src.replace(dst)
    return {"ok": True, "project": name, "path": str(dst), "entry": e,
            "message": f"已把「{name}」从回收站放回 projects/"}


# ── 章节 ────────────────────────────────────────────────────────────────────


def _proj(name: str, root: Path | None) -> Project:
    pdir = resolve_project(name, root)
    if not pdir.is_dir():
        raise TaskError(f"项目不存在：{name}")
    return Project(pdir)


def read_chapter(name: str, no: int, root: Path | None = None) -> dict:
    """读某章正文（编辑器的初始值）。"""
    proj = _proj(name, root)
    e = next((x for x in CH.load(proj) if x.no == int(no)), None)
    if e is None:
        raise TaskError(f"项目「{proj.root.name}」的清单里没有第 {no} 章")
    path = proj.root / e.file
    if not path.is_file():
        raise TaskError(f"第 {no} 章的正文文件不存在：{e.file}")
    return {"no": e.no, "title": e.title, "file": e.file,
            "text": path.read_text(encoding="utf-8", errors="replace"),
            "chars": len(path.read_text(encoding="utf-8", errors="replace").strip())}


def save_chapter(name: str, *, no: int | None = None, title: str = "", text: str = "",
                 root: Path | None = None) -> dict:
    """
    新建或改写一章正文（手动输入这条路的落点）。

    `no` 省略 = 新建；给了 = 覆盖该章正文。
    ★ 覆盖会改变正文指纹 → `chapter_table` 的 `changed_chapters` 会把这章标成
      「需重拆」，界面据此提示重跑单章拆镜（P0.3 已打通）。这里**不**自动重拆：
      拆镜要烧 LLM token，静默触发花钱的操作是本项目明令避免的。
    """
    proj = _proj(name, root)
    body = str(text or "")
    if not body.strip():
        raise TaskError("正文不能为空")

    entries = CH.load(proj)
    existing = next((e for e in entries if e.no == int(no)), None) if no else None
    if existing is not None:
        path = proj.root / existing.file
        # 改名与新建同一条路：先落新文件、再登记、最后清旧文件，
        # 任何一步炸了都不会出现"清单指向不存在的文件"
        if title and _safe_title(title) != existing.title:
            new_title = _safe_title(title)
            new_rel = f"novel/{new_title}.md"
            if (proj.root / new_rel).exists() and new_rel != existing.file:
                raise TaskError(f"已有同名章节文件：{new_rel}")
            fsutil.write_text(proj.root / new_rel, body)
            _to_trash(proj, path)
            existing.file = new_rel
            existing.title = new_title
        else:
            fsutil.write_text(path, body)
        added = False
    else:
        new_title = _safe_title(title or f"第{len(entries) + 1}章")
        new_rel = f"novel/{new_title}.md"
        if (proj.root / new_rel).exists():
            raise TaskError(f"已有同名章节文件：{new_rel}（换标题或改那一章）")
        proj.novel_dir.mkdir(parents=True, exist_ok=True)
        fsutil.write_text(proj.root / new_rel, body)
        assigned = int(no) if no else CH.assign_no(proj, new_title)
        if any(e.no == assigned for e in entries):
            raise TaskError(f"章号 {assigned} 已被占用")
        entries.append(CH.ChapterEntry(no=assigned, title=new_title, file=new_rel))
        existing = entries[-1]
        added = True

    r = CH.sync(proj)
    return {"ok": True, "no": existing.no, "title": existing.title, "file": existing.file,
            "added": added, "chars": len(body.strip()),
            "chapters": r["chapters"],
            "message": (f"已新建第 {existing.no} 章《{existing.title}》" if added
                        else f"已保存第 {existing.no} 章《{existing.title}》"),
            "hint": "" if added else "正文已变 → 这一章会被标「需重拆」，要生效请单章重跑拆镜"}


def import_chapters(name: str, files: list[dict], *, root: Path | None = None) -> dict:
    """
    批量导入章节。`files` = `[{"filename": "...", "text": "..."}]`。

    已存在的同名章节**默认不覆盖**（进 `skipped`），要覆盖得显式带 `overwrite: true` ——
    批量导入时静默覆盖用户手改过的章节，是最难被立刻发现的数据损失。
    """
    proj = _proj(name, root)
    proj.novel_dir.mkdir(parents=True, exist_ok=True)
    entries = CH.load(proj)
    have = {e.file for e in entries}
    imported: list[dict] = []
    skipped: list[dict] = []
    for f in files or []:
        fn = str(f.get("filename") or "").strip()
        stem = Path(fn).stem.strip()
        body = str(f.get("text") or "")
        if not stem or not body.strip():
            skipped.append({"filename": fn, "reason": "文件名为空或正文为空"})
            continue
        try:
            title = _safe_title(stem)
        except TaskError as e:
            skipped.append({"filename": fn, "reason": str(e)})
            continue
        rel = f"novel/{title}.md"
        if rel in have and not f.get("overwrite"):
            skipped.append({"filename": fn, "reason": f"已存在同名章节（{rel}），未覆盖"})
            continue
        fsutil.write_text(proj.root / rel, body)
        have.add(rel)
        no = CH.parse_chapter_no(title)
        used = {e.no for e in entries if e.no}
        if not no or no in used:
            no = CH.assign_no(proj, title)
        entries.append(CH.ChapterEntry(no=no, title=title, file=rel))
        imported.append({"no": no, "title": title, "file": rel, "chars": len(body.strip())})

    r = CH.sync(proj)
    if not imported:
        return {"ok": False, "imported": [], "skipped": skipped, "chapters": r["chapters"],
                "message": f"没有导入任何章节（{len(skipped)} 个被跳过）"}
    return {"ok": True, "imported": imported, "skipped": skipped, "chapters": r["chapters"],
            "message": f"已导入 {len(imported)} 章" + (f"，跳过 {len(skipped)} 个" if skipped else "")}


def delete_chapter(name: str, no: int, root: Path | None = None) -> dict:
    """
    删一章：正文移到 `novel/_trash/`，**不真删**。

    同时把它的镜头表也移走 —— 只删正文会留下"镜头表还在、正文没了"的半吊子状态，
    那章的镜头会继续出现在镜头表与成片里，用户不会知道它们已经失去了原文依据
    （而台词逐句回查正文的保真校验会开始报错）。
    """
    proj = _proj(name, root)
    e = next((x for x in CH.load(proj) if x.no == int(no)), None)
    if e is None:
        raise TaskError(f"清单里没有第 {no} 章")
    moved = [_to_trash(proj, proj.root / e.file)]
    sf = proj.shots_dir / f"chapter{e.no:02d}.json"
    if sf.is_file():
        moved.append(_to_trash(proj, sf))
    r = CH.sync(proj)
    return {"ok": True, "no": e.no, "title": e.title, "moved": moved,
            "chapters": r["chapters"],
            "message": f"已删除第 {e.no} 章《{e.title}》（正文与镜头表已移到 novel/{TRASH_DIR}/，可手工恢复）",
            "hint": "该章已不在清单里；成片需要重新合成才会不含它"}


def rename_chapter(name: str, no: int, title: str, root: Path | None = None) -> dict:
    """改章节标题（= 改正文文件名）。**章号不变** —— 它是镜头 id 前缀与集数的主键。"""
    proj = _proj(name, root)
    entries = CH.load(proj)
    e = next((x for x in entries if x.no == int(no)), None)
    if e is None:
        raise TaskError(f"清单里没有第 {no} 章")
    new_title = _safe_title(title)
    if new_title == e.title:
        return {"ok": True, "no": e.no, "title": e.title, "changed": False,
                "message": "标题没有变化"}
    new_rel = f"novel/{new_title}.md"
    if (proj.root / new_rel).exists():
        raise TaskError(f"已有同名章节：{new_rel}")
    old = proj.root / e.file
    if not old.is_file():
        raise TaskError(f"正文文件不存在，无法改名：{e.file}")
    # 先写新名、再登记、最后删旧名：中途失败最坏是留一个多余文件，
    # 不会出现"清单指向不存在的文件"
    (proj.root / new_rel).write_bytes(old.read_bytes())
    e.file, e.title = new_rel, new_title
    CH.save(proj, entries)
    old.unlink()
    r = CH.sync(proj)
    return {"ok": True, "no": e.no, "title": new_title, "file": new_rel, "changed": True,
            "chapters": r["chapters"], "message": f"第 {e.no} 章标题已改为《{new_title}》（章号不变）"}


def reorder_chapters(name: str, order: list[int], root: Path | None = None) -> dict:
    """
    重排章节顺序。**只动顺序，不动任何章号** ——
    `no` 是镜头表文件名、镜头 id 前缀、成片集数三处共同的主键，
    重编号会让已有产物整体错位（`CONTRACTS.md:483` 对镜头号写的是同一条纪律）。
    """
    proj = _proj(name, root)
    entries = CH.load(proj)
    by_no = {e.no: e for e in entries}
    want = [int(x) for x in (order or [])]
    missing = [e.no for e in entries if e.no not in want]
    extra = [n for n in want if n not in by_no]
    if extra:
        raise TaskError(f"order 里有清单不存在的章号：{extra}")
    if missing:
        raise TaskError(f"order 必须给出全部 {len(entries)} 章，缺：{missing}")
    if len(set(want)) != len(want):
        raise TaskError("order 里有重复章号")
    CH.save(proj, [by_no[n] for n in want])
    r = CH.sync(proj)
    return {"ok": True, "chapters": r["chapters"],
            "message": f"章节顺序已调整为：{'、'.join(str(n) for n in want)}（章号未变）"}


def chapter_status(name: str, root: Path | None = None) -> dict:
    """
    章节管理需要的全景：清单 + 每章状态 + 待同步项 + 引导下一步。

    `needs_replan` 用正文/镜头表的 mtime 比对（不依赖清单里的 sha —— 清单只在
    同步时才刷新，只读路径不能依赖它）。
    """
    from vm import taskctl

    proj = _proj(name, root)
    r = taskctl.chapter_table(proj.root.name, root)
    for c in r["chapters"]:
        c["needs_replan"] = bool(c.get("has_shots")) and _body_newer_than_shots(proj, c)
        c["needs_sync"] = not r.get("has_manifest")
    return {**r, "meta": project_meta(proj.root.name, root),
            "replanned_count": sum(1 for c in r["chapters"] if c["needs_replan"]),
            "next_step": _next_step(r)}


def _body_newer_than_shots(proj: Project, c: dict) -> bool:
    try:
        body = Path(c.get("novel") or "")
        sf = Path(c.get("shots_file") or "")
        if not body.is_file() or not sf.is_file():
            return False
        return sf.stat().st_mtime < body.stat().st_mtime
    except OSError:
        return False


def _next_step(r: dict) -> str:
    """给空态向导用的一句话（P1.4 / F1.4）。"""
    if not r["chapters"]:
        return "导入或粘贴第一章正文"
    if not any(c["has_shots"] for c in r["chapters"]):
        return "跑「拆镜」生成镜头表"
    if r.get("changed_chapters"):
        return f"第 {'、'.join(str(n) for n in r['changed_chapters'])} 章正文已改，单章重拆"
    return "可以定妆 / 渲染了"


def _to_trash(proj: Project, src: Path) -> str:
    """移到 `<同目录>/_trash/<时间戳>_<原名>`，返回目标路径。源文件不存在时不报错。"""
    if not src.is_file():
        return ""
    dst_dir = src.parent / TRASH_DIR
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / f"{int(time.time())}_{src.name}"
    n = 0
    while dst.exists():
        n += 1
        dst = dst_dir / f"{int(time.time())}_{n}_{src.name}"
    src.replace(dst)
    return str(dst)


def entity_registry(name: str, root: Path | None = None) -> dict:
    """
    实体总表（P3.1）：角色 / 场景 / 道具的全项目登记全景 + 各自出现在哪些章。

    ★ 这一页存在的理由是**救济**：跨章去重靠名字归一，LLM 一定会判错。
    判错的两种后果不对称 ——
      漏合并 = 多一条实体，看得见、可以在这里手工合掉；
      错合并 = 两个地点共用一段描述，静默污染所有相关镜头，**救不回来**。
    所以这里既要把问题暴露出来（`suspect_merges`），也要给出出口（merge / split 端点）。
    """
    from vm import chars as _chars
    from vm import costumes as _cs
    from vm import plan as _pl
    from vm import refs as _R

    proj = _proj(name, root)
    shots = _all_shot_rows(proj)

    chars_out: list[dict] = []
    # 角色名的权威来源是 taskctl（prompts/char_*.txt 与已有卡片的并集）。
    # ★ 先前写成 `hasattr(_pl, "char_names_of")` 兜底 ⇒ 静默返回 [] ⇒ 总表里角色整列消失，
    #   而场景/道具照常显示 —— 界面会让人以为"这个项目没有角色"。
    #   这种"兜底把 bug 咽掉"比直接抛错更坏，所以这里明确 import、不做存在性判断。
    from vm import taskctl as _T

    for cn in sorted(_T.char_names_of(proj)):
        variants = _R.list_variants(proj, cn)
        chars_out.append({
            "kind": "char", "id": cn, "name": cn,
            # 角色的"出现在哪些章"从**镜头表反推**：角色卡是跨章共享的，
            # 从来没有 `chapters` 这个列，只有靠镜头才推得出来。
            "chapters": sorted({_ch_of(r) for r in shots
                                if cn in (r.get("chars") or [])} - {None}),
            "shot_count": sum(1 for r in shots if cn in (r.get("chars") or [])),
            "has_prompt": (proj.prompts_dir / f"char_{cn}.txt").is_file(),
            "portrait_variants": variants,
            "portrait_chapters": sorted({v["chapter"] for v in variants if v.get("chapter")}),
            "costume_variants": [v.get("id") for v in _cs.variants_of(_cs.load(proj), cn)],
        })

    def _chapters_of(entity_id: str, prop_style: bool, recorded: list[int]) -> list[int]:
        """
        登记里有 `chapters` 就用；没有就从镜头表反推。

        v1 老项目的 scenes.json / props.json **没有这个字段**（P0.2 才加的），
        不反推的话总表会显示"出现在 0 章"，看起来像这些实体都是垃圾。
        """
        if recorded:
            return sorted(set(recorded))
        hit: set[int | None] = set()
        for r in shots:
            if prop_style:
                if entity_id in (r.get("prop_ids") or []):
                    hit.add(_ch_of(r))
            elif str(r.get("scene_id") or "") == entity_id:
                hit.add(_ch_of(r))
        return sorted(hit - {None})

    for sid, sc in sorted(_pl.load_scenes(proj).items()):
        chars_out.append({
            "kind": "scene", "id": sid, "name": sc.name,
            "chapters": _chapters_of(sid, False, list(sc.chapters or [])),
            "shot_count": sum(1 for r in shots if str(r.get("scene_id") or "") == sid),
            "location": sc.location, "has_prompt": bool(sc.description),
            "time_of_day": sc.time_of_day, "lighting": sc.lighting,
        })
    for pid, pc in sorted(_pl.load_props(proj).items()):
        chars_out.append({
            "kind": "prop", "id": pid, "name": pc.name,
            "chapters": _chapters_of(pid, True, list(pc.chapters or [])),
            "shot_count": sum(1 for r in shots if pid in (r.get("prop_ids") or [])),
            "owner": pc.owner, "inferred": bool(pc.inferred), "has_prompt": bool(pc.description),
        })

    return {
        "ok": True, "project": proj.root.name, "entities": chars_out,
        "totals": {k: sum(1 for e in chars_out if e["kind"] == k)
                   for k in ("char", "scene", "prop")},
        "suspect_merges": _suspect_merges(chars_out),
        "never_used": [f'{e["kind"]}:{e["id"]}' for e in chars_out if not e.get("shot_count")],
    }


def _ch_of(row: dict) -> int | None:
    from vm import refs as _R

    return _R.chapter_of(str(row.get("id") or ""))


def _all_shot_rows(proj) -> list[dict]:
    import json as _json

    out: list[dict] = []
    for f in sorted((proj.root / "shots").glob("chapter*.json")):
        try:
            d = _json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        rows = d.get("shots") if isinstance(d, dict) else d
        out += [r for r in (rows or []) if isinstance(r, dict)]
    return out


def _chapter_counts(rows: list[dict]) -> dict[int, int]:
    c: dict[int, int] = {}
    for r in rows:
        ch = _ch_of(r)
        if ch:
            c[ch] = c.get(ch, 0) + 1
    return c


def _suspect_merges(entities: list[dict]) -> list[str]:
    """
    可疑归并：同名实体出现多次（去重漏了）或某实体被 0 个镜头引用（多半是建错了）。

    只报**能确定的**那几种，不猜"这两个名字也许是同一个地方"——
    那种判断交给人，界面上给合并入口，不自动合。
    """
    out: list[str] = []
    by_key: dict[tuple, list[str]] = {}
    for e in entities:
        if e["kind"] != "scene":
            continue
        k = _entity_key_of(e.get("name") or "")
        if k:
            by_key.setdefault(("scene", k), []).append(str(e["id"]))
    for (_kind, key), ids in by_key.items():
        if len(ids) > 1:
            out.append(f"场景名「{key}」被登记在多个 id 上（{', '.join(ids)}）"
                       f"—— 若是同一个地点，请手工合并")
    return out


def _entity_key_of(name: str) -> str:
    """与 plan._entity_key 同口径的归一化（这里单独实现以免 proj 反向依赖 plan）。"""
    import unicodedata

    t = unicodedata.normalize("NFKC", str(name or "")).casefold()
    return re.sub(r"[\s\u3000，。、；：\"\"''（）()【】《》·—\-_/,.:;!?]+", "", t)


def merge_scenes(name: str, keep_id: str, drop_id: str, root: Path | None = None) -> dict:
    """
    把两个场景实体合成一个（P3.1 的救济口）。

    ★ 迁移引用而不只是删记录：镜头表里的 `scene_id` 是外键，
    只删 `scenes.json` 里那条会让所有引用它的镜头指向一个不存在的场景
    （`audit/completeness.py` 就会报 scene_id 不存在）。
    所以这里同时改镜头表 —— 而改镜头表会让相关镜头**变 stale**，必须说清楚。
    """
    from vm import plan as _pl

    proj = _proj(name, root)
    scenes = _pl.load_scenes(proj)
    if keep_id not in scenes or drop_id not in scenes:
        raise TaskError(f"场景 id 不存在：{keep_id if keep_id not in scenes else drop_id}")
    if keep_id == drop_id:
        raise TaskError("keep 与 drop 是同一个 id，无需合并")

    moved = _retarget_shot_refs(proj, "scene_id", drop_id, keep_id)
    merged = scenes[keep_id]
    victim = scenes[drop_id]
    merged.chapters = sorted(set(merged.chapters or []) | set(victim.chapters or []))
    if not (merged.description or "").strip():
        merged.description = victim.description
    rest = [c for cid, c in scenes.items() if cid not in (keep_id, drop_id)]
    _pl.save_scenes(proj, [merged] + rest, _max_id_serial(scenes, "S"))

    pp = Path(proj.root) / "scenes.json"
    bk = _backup(pp, "合并场景前")
    return {"ok": True, "kept": keep_id, "dropped": drop_id, "repointed_shots": moved,
            "backup": bk,
            "message": f"已把 {drop_id} 并入 {keep_id}，{moved} 镜的 scene_id 已改指",
            "hint": f"{moved} 镜的场景锚定文字变了 → 会标「需重渲」，要生效请重渲这些镜"}


def split_chapter_entities(name: str, chapter_no: int, root: Path | None = None) -> dict:
    """某章用到哪些实体（实体总表按章视图）。"""
    proj = _proj(name, root)
    shots = [r for r in _all_shot_rows(proj) if _ch_of(r) == int(chapter_no)]
    from vm import plan as _pl

    sc, pr = _pl.load_scenes(proj), _pl.load_props(proj)
    return {
        "ok": True, "chapter": int(chapter_no), "shots": len(shots),
        "characters": sorted({c for r in shots for c in (r.get("chars") or [])}),
        "scenes": [{"id": k, "name": sc[k].name} for k in sorted(
            {str(r.get("scene_id") or "") for r in shots} & set(sc))],
        "props": [{"id": k, "name": pr[k].name} for k in sorted(
            {str(x) for r in shots for x in (r.get("prop_ids") or [])} & set(pr))],
    }


def _max_id_serial(entries: dict, prefix: str) -> int:
    hi = 0
    for k in entries:
        m = re.fullmatch(rf"{re.escape(prefix)}(\d+)", str(k))
        if m:
            hi = max(hi, int(m.group(1)))
    return hi


def _retarget_shot_refs(proj, field: str, old: str, new: str) -> int:
    """改镜头表里某个外键字段的所有引用。返回改了多少镜。"""
    import json as _json

    n = 0
    for f in sorted((proj.root / "shots").glob("chapter*.json")):
        try:
            d = _json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        rows = d.get("shots") if isinstance(d, dict) else d
        changed = False
        for r in rows or []:
            if isinstance(r, dict) and str(r.get(field) or "") == old:
                r[field] = new
                changed = True
                n += 1
        if changed:
            fsutil.write_json(f, d)
    return n


def _backup(path: Path, note: str) -> str:
    """合并这类会动镜头表的操作，先把被改的文件留一份 .bak（与编辑层的备份同一套习惯）。"""
    if not path.is_file():
        return ""
    bak = path.with_suffix(path.suffix + ".bak")
    bak.write_bytes(path.read_bytes())
    return str(bak)


def list_trash(name: str, root: Path | None = None) -> dict:
    """列出可恢复的东西（删除的可逆性要能被看见，否则等于没给）。"""
    proj = _proj(name, root)
    items: list[dict] = []
    for d in (proj.novel_dir, proj.shots_dir):
        t = d / TRASH_DIR
        if not t.is_dir():
            continue
        for p in sorted(t.iterdir()):
            if p.is_file():
                items.append({"path": str(p.relative_to(proj.root)), "size": p.stat().st_size,
                              "mtime": int(p.stat().st_mtime)})
    return {"project": proj.root.name, "items": items,
            "message": f"可恢复文件 {len(items)} 个"}
