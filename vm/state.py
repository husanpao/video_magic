"""
state.py —— 幂等与断点续跑的状态层。

这是整套流水线的地基，解决两个痛点：
  ① 产物是否"真的"过期（不能只看文件存不存在）
  ② 崩溃/断线后能不能接着跑，而不是重烧一遍 GPU

设计要点（借鉴 NiliX 的 manifest 指纹 + 渲染检查点，但按本项目需求精简）：

  指纹（fingerprint）
      把「所有会影响画面结果的输入」拼起来取 md5：提示词、角色、参考图(路径@mtime:size)、
      模型、LoRA、步数、分辨率、帧数、种子。任何一项变了 → 指纹变 → 判 stale → 重渲。
      反面教材：只判 os.path.exists()，改了提示词却静默复用旧片。

  三态
      missing  产物文件不存在
      stale    没有指纹记录，或指纹与当前输入不一致
      current  指纹一致，可跳过

  渲染检查点（render checkpoint）
      ComfyUI 的 /prompt 提交后立刻把 prompt_id 落盘。崩溃重启后先查 /history：
      已完成 → 直接收回产物，绝不重复烧 GPU；在跑 → 等它；查不到 → 重新提交。

  原子写
      manifest / checkpoint 都先写 .tmp 再 os.replace()，避免掉电/被杀留下半个文件。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

MANIFEST_VERSION = 1

# 三态
MISSING = "missing"
STALE = "stale"
CURRENT = "current"


def _atomic_write_json(path: Path, data: Any) -> None:
    """先写临时文件再 rename，保证不会出现半截 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def ref_stamp(path: Path) -> str:
    """
    参考图指纹片段：`文件名@mtime纳秒:字节数`。

    用 mtime+size 而不是内容 md5 —— 参考图是几百 KB 到几 MB 的 PNG，
    每次算全量哈希在 118 个镜头上会白白多花几十秒；而 mtime+size 足以
    捕捉"换了图/重新生成/覆盖"这几种真实变更。
    """
    try:
        st = path.stat()
    except OSError:
        return f"{path.name}@MISSING"
    return f"{path.name}@{st.st_mtime_ns}:{st.st_size}"


def shot_fingerprint(
    prompt: str,
    chars: Iterable[str],
    ref_paths: Iterable[Path],
    render: dict,
    frames: int,
    seed: int,
) -> str:
    """
    计算单个镜头的输入指纹。字段顺序固定，保证可复现。
    `render` 里取会影响画面的子集（模型/LoRA/步数/分辨率），不取端口之类无关项。
    """
    parts = [
        "v1",
        "prompt=" + (prompt or ""),
        "chars=" + ",".join(sorted(chars or [])),
        "refs=" + ",".join(sorted(ref_stamp(Path(p)) for p in (ref_paths or []))),
        "unet=" + str(render.get("unet", "")),
        "lora=" + str(render.get("lora", "")),
        "steps=" + str(render.get("steps", "")),
        "w=" + str(render.get("width", "")),
        "h=" + str(render.get("height", "")),
        "frames=" + str(frames),
        "seed=" + str(seed),
    ]
    return hashlib.md5("\n".join(parts).encode("utf-8")).hexdigest()[:16]


@dataclass
class Entry:
    """manifest 里单个镜头的记录。"""

    fp: str
    file: str
    size: int = 0
    at: int = 0
    qc: str = "unknown"  # pass / fail / unknown
    note: str = ""
    # A2：ffprobe 回读的真实时长。时间轴（拼片/字幕/EDL）一律用它，不要用请求值。
    sec_actual: float | None = None

    # ── E1 资产锁定语义（2026-09-23）──
    # 依据 storyforge 的资产表设计：`selected` / `favorite` / `locked` 是**三个独立布尔位**，
    # 不是互斥状态（一个镜头可以既被选中又被收藏又被锁定）。
    # 为什么需要：在此之前**任何产物都能被覆盖** —— 改定妆照、重跑渲染、批量重渲，
    # 都可能把一个你已经满意的镜头盖掉，而且没有提示。
    # `locked=True` 的镜头：渲染层拒绝覆盖（除非显式 force），编辑层拒绝改字段。
    locked: bool = False
    locked_at: int = 0
    locked_by: str = ""       # 记录"谁何时锁的"，出问题能追溯
    selected: bool = False    # 已采用（用户认可这一版）
    favorite: bool = False    # 收藏（想留着备选）

    @classmethod
    def from_dict(cls, d: dict) -> "Entry":
        return cls(
            fp=str(d.get("fp", "")),
            file=str(d.get("file", "")),
            size=int(d.get("size", 0) or 0),
            at=int(d.get("at", 0) or 0),
            qc=str(d.get("qc", "unknown")),
            note=str(d.get("note", "")),
            sec_actual=(
                float(d["sec_actual"]) if isinstance(d.get("sec_actual"), (int, float)) else None
            ),
            locked=bool(d.get("locked", False)),
            locked_at=int(d.get("locked_at", 0) or 0),
            locked_by=str(d.get("locked_by", "")),
            selected=bool(d.get("selected", False)),
            favorite=bool(d.get("favorite", False)),
        )

    def to_dict(self) -> dict:
        return {
            "fp": self.fp,
            "file": self.file,
            "size": self.size,
            "at": self.at,
            "qc": self.qc,
            "note": self.note,
            "sec_actual": self.sec_actual,
            "locked": self.locked,
            "locked_at": self.locked_at,
            "locked_by": self.locked_by,
            "selected": self.selected,
            "favorite": self.favorite,
        }


@dataclass
class Manifest:
    """
    产物时效清单。记录每个镜头渲染时的输入指纹，据此判定 current / stale / missing。
    """

    path: Path
    shots: dict[str, Entry] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "Manifest":
        if not path.exists():
            return cls(path=path)
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except (json.JSONDecodeError, OSError):
            # 坏文件不静默重建：挪到 .corrupt 留证，再以空清单继续（否则会永久读不了）
            try:
                path.replace(path.with_suffix(path.suffix + ".corrupt"))
            except OSError:
                pass
            return cls(path=path)
        shots = {
            str(k): Entry.from_dict(v) for k, v in (d.get("shots") or {}).items()
        }
        return cls(path=path, shots=shots)

    def save(self) -> None:
        _atomic_write_json(
            self.path,
            {"version": MANIFEST_VERSION, "shots": {k: v.to_dict() for k, v in self.shots.items()}},
        )

    def status(self, shot_id: str, fp: str, clip_path: Path) -> str:
        """返回 missing / stale / current。"""
        if not clip_path.exists() or clip_path.stat().st_size == 0:
            return MISSING
        e = self.shots.get(shot_id)
        if e is None:
            return STALE  # 无指纹记录 = 不可信
        if e.fp != fp:
            return STALE
        # 记录里的大小与实际不符（被截断/替换过）也算 stale
        if e.size and e.size != clip_path.stat().st_size:
            return STALE
        return CURRENT

    def mark(
        self,
        shot_id: str,
        fp: str,
        clip_path: Path,
        qc: str = "unknown",
        note: str = "",
        sec_actual: float | None = None,
    ) -> None:
        size = clip_path.stat().st_size if clip_path.exists() else 0
        prev = self.shots.get(shot_id)
        # ★ 关键：重新渲染**不能把锁定/选中/收藏状态清掉** ——
        #   否则"锁了再重渲一次就自动解锁"，锁形同虚设。
        self.shots[shot_id] = Entry(
            fp=fp, file=str(clip_path), size=size, at=int(time.time()),
            qc=qc, note=note, sec_actual=sec_actual,
            locked=prev.locked if prev else False,
            locked_at=prev.locked_at if prev else 0,
            locked_by=prev.locked_by if prev else "",
            selected=prev.selected if prev else False,
            favorite=prev.favorite if prev else False,
        )

    def drop(self, shot_id: str) -> None:
        self.shots.pop(shot_id, None)

    def is_locked(self, shot_id: str) -> bool:
        """该镜头是否被锁定（锁定后渲染层拒绝覆盖、编辑层拒绝改字段）。"""
        e = self.shots.get(shot_id)
        return bool(e and e.locked)

    def set_flag(self, shot_id: str, flag: str, value: bool, by: str = "") -> bool:
        """
        置位 locked / selected / favorite。返回是否成功（镜头不在清单里则 False）。

        **锁定会同时记录时间与操作者**（storyforge 的 `locked_at`/`locked_by`），
        因为"谁什么时候锁的"是排查"为什么这个镜头改不动"的唯一线索。
        """
        if flag not in ("locked", "selected", "favorite"):
            raise ValueError(f"未知标记：{flag}")
        e = self.shots.get(shot_id)
        if e is None:
            return False
        setattr(e, flag, bool(value))
        if flag == "locked":
            e.locked_at = int(time.time()) if value else 0
            e.locked_by = (by or "user") if value else ""
        self.save()
        return True


@dataclass
class Checkpoint:
    """
    渲染检查点：镜头 → ComfyUI prompt_id。

    提交后立刻落盘。崩溃重启时，对上过盘但没收到的镜头先查 /history：
      completed → 直接收回产物（免重渲）；在跑 → 等；查不到 → 判定丢失，重新提交。
    """

    path: Path
    # 值是 {"pid":…,"at":…}（新格式）或 str（旧格式），get()/age() 双格式兼容
    shots: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "Checkpoint":
        if not path.exists():
            return cls(path=path)
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except (json.JSONDecodeError, OSError):
            return cls(path=path)
        # ★ 只 str(k)，**值原样保留**（BUG-1）：值是 {"pid":…,"at":…} 字典或旧格式
        #   字符串，`str(v)` 会把字典转成 "{'at': …, 'pid': …}" 字符串 ——
        #   get() 回读的 prompt_id 是字典字符串（/history 回收必查不到 → 重复提交
        #   白烧 GPU），age() 因值不再是 dict 恒 inf（A6 宽限期失效）。
        #   get()/age() 本来就有 dict/字符串双格式兼容分支，喂原样值即可对齐。
        return cls(path=path, shots={str(k): v for k, v in (d.get("shots") or {}).items()})

    def save(self) -> None:
        _atomic_write_json(self.path, {"shots": dict(self.shots)})

    def set(self, shot_id: str, prompt_id: str, at: int | None = None) -> None:
        """
        记录 镜头 → prompt_id **及其提交时间**。

        为什么要记时间（A6，2026-09-23）：崩溃恢复时"历史里查不到、队列里也没有"
        不足以判定任务已丢失 —— 刚提交的任务可能还没进 /history，队列也可能瞬时为空。
        没有时间戳就会**误判丢失 → 重复提交 → 白烧 GPU + 产物被覆盖**。
        调用方用 age() 判断是否超过宽限期。
        """
        self.shots[shot_id] = {
            "pid": prompt_id,
            "at": int(at if at is not None else time.time()),
        }
        self.save()

    def get(self, shot_id: str) -> str | None:
        """取 prompt_id。兼容旧格式（直接存字符串）。"""
        v = self.shots.get(shot_id)
        if isinstance(v, dict):
            return str(v.get("pid") or "") or None
        return str(v) if v else None

    def age(self, shot_id: str) -> float:
        """该检查点距今多少秒。旧格式（无时间戳）返回一个很大的值，视为已过宽限期。"""
        v = self.shots.get(shot_id)
        if not isinstance(v, dict) or not v.get("at"):
            return float("inf")
        try:
            return max(0.0, time.time() - float(v["at"]))
        except (TypeError, ValueError):
            return float("inf")

    def clear(self, shot_id: str) -> None:
        if shot_id in self.shots:
            del self.shots[shot_id]
            self.save()


class Project:
    """一个项目的目录约定与状态载体。"""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.novel_dir = self.root / "novel"
        self.shots_dir = self.root / "shots"
        self.refs_dir = self.root / "refs"
        self.prompts_dir = self.root / "prompts"
        self.clips_dir = self.root / "clips"
        self.final_dir = self.root / "final"
        self.state_dir = self.root / "state"

    def ensure(self) -> "Project":
        for d in (
            self.novel_dir,
            self.shots_dir,
            self.refs_dir,
            self.prompts_dir,
            self.clips_dir,
            self.final_dir,
            self.state_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)
        return self

    @property
    def manifest(self) -> Manifest:
        return Manifest.load(self.state_dir / "manifest.json")

    @property
    def checkpoint(self) -> Checkpoint:
        return Checkpoint.load(self.state_dir / "render_ck.json")

    def clip(self, shot_id: str) -> Path:
        return self.clips_dir / f"{shot_id}.mp4"
