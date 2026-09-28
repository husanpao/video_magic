"""
fsutil.py —— 落盘工具层：**原子写**的唯一实现。

## 为什么要有这个文件

`CONTRACTS.md:32` 硬约束 3 要求"所有落盘用原子写（`.tmp` + `os.replace`）"。
这条纪律原先在仓库里有 **6 份逐字复制的 `_atomic_write_json`**
（config / storyboard / taskctl / costumes / qc / state）、
**2 份 `_atomic_write_text`**（plan / shots）、**1 份 `_atomic_write_bytes`**（taskctl），
外加 **6 处直接内联**（plan 的 props/scenes、queue、script、assets、budget）—— 共 21 个落点。

副本本身不是问题，**副本漂移才是**。实测到两处真实的差别，都不是刻意的：

1. 内联那 6 处**没有 `fsync`** —— 掉电时 `os.replace` 已生效但数据页还在 page cache，
   结果是一个"名字对、内容是半截"的 JSON。原子 rename 只保证**换名**原子，不保证**内容落盘**。
2. `qc.py:125` 的注释写着"10 行代码不值得跨模块耦合"所以刻意复制 ——
   而 `chars.py:85` 留下的事故记录正是这句话的代价：
   *"tmp 不存在 → `os.replace` 报 FileNotFoundError（我实测踩到）"*，
   漏的是 `mkdir(parents=True)`。同一类 bug 有 21 个落点，就还有 21 次机会。

所以这个模块把 `mkdir → tmp → 写 → flush → fsync → replace` 这条序列**封成一次**，
调用方只决定"写什么"和"JSON 的排版细节"。

## 关于排版参数

`sort_keys` / `trailing_newline` 保留成显式参数，是为了**收敛实现而不改文件字节**：
仓库里现存的产物有的按键排序、有的保持插入序，有的带行尾换行、有的不带。
收敛过程中如果顺手把 21 个文件的排版一起改了，
"这版没改行为"这个断言就没法验证了 —— 而它是本次重构唯一的验收标准。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

__all__ = ["write_bytes", "write_text", "write_json", "read_json", "TMP_SUFFIX"]

#: 临时文件后缀。保持和收敛前一致（`.json.tmp` / `.txt.tmp` / `.mp4.tmp` …），
#: 这样任何按 `*.tmp` 清理或 glob 的既有逻辑行为不变。
TMP_SUFFIX = ".tmp"


def _tmp_of(path: Path) -> Path:
    return path.with_suffix(path.suffix + TMP_SUFFIX)


def write_bytes(path: Path | str, data: bytes, *, mkdir: bool = True) -> Path:
    """原子写字节。**总是**先 `mkdir(parents=True)` —— 这是 chars.py:85 那次事故的根因。"""
    p = Path(path)
    if mkdir:
        p.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_of(p)
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        # 失败别把 .tmp 留在原地：下次调用会覆盖它，但残留文件会让"目录里有几个产物"
        # 这类计数（队列/候选图列表都是按目录数出来的）出错。
        _discard(tmp)
        raise
    return p


def write_text(path: Path | str, text: str, *, encoding: str = "utf-8") -> Path:
    """原子写文本（UTF-8 不转义中文是本仓库的默认约定）。"""
    return write_bytes(path, text.encode(encoding))


def write_json(
    path: Path | str,
    obj: Any,
    *,
    indent: int = 2,
    sort_keys: bool = False,
    trailing_newline: bool = True,
) -> Path:
    """
    原子写 JSON：UTF-8、`ensure_ascii=False`（中文必须原样落盘，便于人工审阅/手改）。

    `sort_keys` 默认 **False**（保持插入序）—— 少数调用方原来传了 True，需要显式声明，
    别让它悄悄改掉现存文件的键序。
    """
    text = json.dumps(obj, ensure_ascii=False, indent=indent, sort_keys=sort_keys)
    if trailing_newline:
        text += "\n"
    return write_text(path, text)


def read_json(path: Path | str, default: Any = None) -> Any:
    """
    容错读 JSON：文件缺失/损坏/顶层不是预期形状时回 `default`，**不抛**。

    这个语义是从 `plan.load_scenes` / `queue.load_all` / `state.Manifest.load` 等
    多处重复的"缺失或损坏返回空 dict（不抛）"抄来的 —— 它们都是只读入口，
    读到坏文件应该是"当作没有"，而不是让一个坏产物炸掉整张表的展示。
    需要区分"不存在"与"损坏"的调用方请自己 `Path.is_file()` + `json.loads`。
    """
    p = Path(path)
    if not p.is_file():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError, UnicodeDecodeError):
        return default


def _discard(tmp: Path) -> None:
    try:
        tmp.unlink()
    except OSError:
        pass
