"""
queue.py —— 项目级作业队列。

## 为什么需要（用户的原话）
> 「涉及到抽卡，我点完现在就没法点了，我也不能一直看着啊」

抽卡类操作（场景/道具概念图、角色定妆）的特点是：
  · **单次几十秒**（Qwen-Image 10s/张，H3 定妆 ~40s/张）
  · 但用户想**连着点十几个**（8 个场景/道具各抽 2 张 = 16 次）
  · 而且**不该要求人盯着**

原来的做法是**同步 HTTP**：浏览器等 20 秒、按钮禁用、一次只能来一个。
（角色抽卡走的是任务模型所以没这个问题；场景/道具是我新加时写成同步的，不一致。）

现在改成：**点一下 = 入队，立刻返回**；一个 worker 顺序把队列抽干。
用户点完就可以走开，回来看到结果。

## 与"同项目只允许一个任务"的关系
不冲突：队列里的作业由**一个** drainer 任务顺序处理，同一时刻仍然只有一个 GPU 作业。
队列解决的是"**排队**"，锁解决的是"**互斥**"，两件事。
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

QUEUE_FILENAME = "queue.json"

# 支持的作业类型。
# value = (人话标签, 需要的参数键)
JOB_KINDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "asset_gen":     ("素材概念图", ("kind", "id", "n")),
    "chars_gacha":   ("角色抽卡",   ("name", "n")),
    "storyboard":    ("分镜图",     ("id", "n")),
}

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELED = "canceled"


@dataclass
class Job:
    id: str
    kind: str
    args: dict = field(default_factory=dict)
    status: str = STATUS_PENDING
    created_at: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0
    note: str = ""
    label: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "args": self.args, "status": self.status,
                "created_at": self.created_at, "started_at": self.started_at,
                "finished_at": self.finished_at, "note": self.note,
                "label": self.label or JOB_KINDS.get(self.kind, ("作业",))[0]}

    @staticmethod
    def from_dict(d: dict) -> "Job":
        return Job(
            id=str(d.get("id") or ""), kind=str(d.get("kind") or ""),
            args=dict(d.get("args") or {}), status=str(d.get("status") or STATUS_PENDING),
            created_at=float(d.get("created_at") or 0), started_at=float(d.get("started_at") or 0),
            finished_at=float(d.get("finished_at") or 0), note=str(d.get("note") or ""),
            label=str(d.get("label") or ""),
        )


def _path(proj) -> Path:
    return Path(proj.state_dir) / QUEUE_FILENAME


# ── 并发保护 ────────────────────────────────────────────────────────────────
# 为什么必须加锁：queue.json 的每次改写都是「读整个文件 → 改 → 整个写回」，
# 而写它的一边是 **Web 的多个请求线程**、另一边是 **drainer 独立进程** ——
# 不上锁时两边的读改写会互相覆盖：丢作业、状态回退（诊断 B3，grep 证实全文无锁）。
# 两层锁缺一不可（与 taskctl._project_lock 同一模式）：
#   · threading.Lock —— 同进程内多请求线程互斥（Web 是 ThreadingHTTPServer）
#   · fcntl.flock    —— 跨进程互斥（drainer 是独立 worker 进程）
# 为什么自建而不用 taskctl 的 task.lock：队列的读改写发生在**任务运行期间**
# （drainer 正跑作业时 Web 还要能入队/取消），不能蹭任务启动的互斥语义 ——
# 那会把入队拖进"同项目只允许一个任务"的锁里。锁文件独立：state/queue.lock。

_QUEUE_LOCKS: dict[str, threading.Lock] = {}
_QUEUE_LOCKS_GUARD = threading.Lock()


@contextmanager
def _queue_lock(proj):
    """queue.json 读改写的临界区。所有**写**路径必须包在里面。"""
    p = _path(proj)
    with _QUEUE_LOCKS_GUARD:
        tlock = _QUEUE_LOCKS.setdefault(str(p), threading.Lock())
    with tlock:
        p.parent.mkdir(parents=True, exist_ok=True)
        lf = open(p.with_suffix(".lock"), "a+")
        try:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
            finally:
                lf.close()


def read_all(proj) -> list[Job]:
    p = _path(proj)
    if not p.is_file():
        return []
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return [Job.from_dict(x) for x in (d.get("jobs") or []) if isinstance(x, dict)]


def write_all(proj, jobs: list[Job]) -> None:
    p = _path(proj)
    p.parent.mkdir(parents=True, exist_ok=True)
    # 只保留最近 200 条，避免队列文件无限增长
    keep = jobs[-200:]
    data = {"version": 1, "jobs": [j.to_dict() for j in keep]}
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def add(proj, kind: str, args: dict) -> Job:
    """入队。**立即返回** —— 调用方（HTTP handler）不该等出图。"""
    if kind not in JOB_KINDS:
        raise ValueError(f"未知作业类型：{kind}（可选 {', '.join(JOB_KINDS)}）")
    j = Job(
        id=uuid.uuid4().hex[:12], kind=kind, args=dict(args),
        status=STATUS_PENDING, created_at=time.time(),
        label=JOB_KINDS[kind][0],
    )
    with _queue_lock(proj):
        jobs = read_all(proj)
        jobs.append(j)
        write_all(proj, jobs)
    return j


def claim_next(proj) -> Job | None:
    """
    取下一个待处理作业并标记为 running。

    「重读-改-写」整个包在锁里才叫原子 —— 原来裸跑时，Web 的 add/cancel 和
    另一个 drainer 的 claim 会互相覆盖整份文件（B3）。
    """
    with _queue_lock(proj):
        jobs = read_all(proj)
        for j in jobs:
            if j.status == STATUS_PENDING:
                j.status = STATUS_RUNNING
                j.started_at = time.time()
                write_all(proj, jobs)
                return j
    return None


def finish(proj, job_id: str, *, ok: bool, note: str = "") -> None:
    with _queue_lock(proj):
        jobs = read_all(proj)
        for j in jobs:
            if j.id == job_id:
                j.status = STATUS_DONE if ok else STATUS_FAILED
                j.finished_at = time.time()
                j.note = note[:400]
                break
        write_all(proj, jobs)


def release_to_pending(proj, job_id: str) -> bool:
    """
    把 running 的作业**放回 pending**（预算不足暂停时用）。

    为什么单独一个函数而不是复用 cancel：「等批准」不是「失败」——
    作业没做成就标 failed 会骗人，用户得手动重加。
    """
    with _queue_lock(proj):
        jobs = read_all(proj)
        hit = False
        for j in jobs:
            if j.id == job_id and j.status == STATUS_RUNNING:
                j.status = STATUS_PENDING
                j.started_at = 0.0
                hit = True
                break
        if hit:
            write_all(proj, jobs)
        return hit


def cancel(proj, job_id: str) -> bool:
    """取消一个**还没开始**的作业（正在跑的只能靠「停止」按钮）。"""
    with _queue_lock(proj):
        jobs = read_all(proj)
        hit = False
        for j in jobs:
            if j.id == job_id and j.status == STATUS_PENDING:
                j.status = STATUS_CANCELED
                j.finished_at = time.time()
                hit = True
                break
        if hit:
            write_all(proj, jobs)
        return hit


def cancel_pending(proj) -> int:
    """取消全部待处理（已完成/失败的记录保留，便于对账）。"""
    with _queue_lock(proj):
        jobs = read_all(proj)
        n = 0
        for j in jobs:
            if j.status == STATUS_PENDING:
                j.status = STATUS_CANCELED
                j.finished_at = time.time()
                n += 1
        if n:
            write_all(proj, jobs)
        return n


def clear_finished(proj) -> int:
    """清掉已完成/失败/取消的记录。"""
    with _queue_lock(proj):
        jobs = read_all(proj)
        keep = [j for j in jobs if j.status in (STATUS_PENDING, STATUS_RUNNING)]
        n = len(jobs) - len(keep)
        if n:
            write_all(proj, keep)
        return n


def stats(proj) -> dict:
    jobs = read_all(proj)
    c: dict[str, int] = {}
    for j in jobs:
        c[j.status] = c.get(j.status, 0) + 1
    pend = [j for j in jobs if j.status == STATUS_PENDING]
    return {
        "total": len(jobs),
        "pending": c.get(STATUS_PENDING, 0),
        "running": c.get(STATUS_RUNNING, 0),
        "done": c.get(STATUS_DONE, 0),
        "failed": c.get(STATUS_FAILED, 0),
        "canceled": c.get(STATUS_CANCELED, 0),
        "next_label": (pend[0].label if pend else ""),
        "jobs": [j.to_dict() for j in jobs[-60:]],
    }


def drain(proj, log=lambda m: print(m), should_stop=None) -> dict:
    """
    把队列抽干。由 `pipeline.py --stage queue` 调用（独立 worker 进程）。

    单个作业失败**不中断队列** —— 继续跑下一个，失败原因记进 note。
    每处理完一个就写回队列文件，所以 UI 是实时可见的。

    ★ 预算护栏（S4）：**每个作业开跑前**都过一次 budget.check ——
      入队时没超不代表抽到现在还没超；抽卡队列就是"连点 N 张烧 GPU"的现场，
      护栏必须罩住这个出口。超了就把作业放回 pending 并停 drainer
      （「等批准」不是「失败」），用户点「放行」后下次唤醒继续跑。
    """
    from vm import assets as A
    from vm import budget as B

    # budget 按「项目目录」定位 —— 一律传绝对路径，别让裸名字被解析到默认根去（BUG-2 同类）
    root = Path(proj.root).resolve()
    done = failed = 0
    while True:
        if should_stop and should_stop():
            log("⊘ 收到停止请求，队列里剩下的作业保持 pending")
            break
        j = claim_next(proj)
        if j is None:
            break
        label = f"{j.label} {j.args.get('id') or j.args.get('name') or ''}".strip()
        est = B.job_estimate(j)
        chk = B.check(root, est)
        if chk.get("needs_approval") and not B.consume_approval(root):
            release_to_pending(proj, j.id)
            log(f"⏸ [{j.id}] {label} 预算不足，已放回队列等批准：" + "；".join(chk.get("reasons") or []))
            log("  点「放行」（或 python3 -m vm.budget <项目> --approve）后队列会继续")
            break
        log(f"▶ [{j.id}] {label}")
        try:
            if j.kind == "asset_gen":
                kind = str(j.args.get("kind") or "")
                aid = str(j.args.get("id") or "")
                n = int(j.args.get("n") or 2)
                made = A.gen_candidates(proj, kind, aid, n=n,
                                        log=lambda m: log("  " + str(m)))
                note = f"新出 {len(made)} 张"
                log(f"✅ [{j.id}] {label} — {note}")
            elif j.kind == "chars_gacha":
                # 角色抽卡原来走的是 gacha 阶段（任务模型，已是非阻塞的）。
                # 这里把它接进统一队列，好处是**能和场景/道具的作业排在同一条队里** ——
                # 用户不必关心"这次点的是哪种抽卡"。
                import json as _json
                from vm import chars as _chars
                from vm.state import Project as _P
                name = str(j.args.get("name") or "")
                n = int(j.args.get("n") or 2)
                params = _json.loads((Path(proj.root) / "project.json").read_text(encoding="utf-8"))
                made = _chars.gen_candidates(proj, name, params, n=n,
                                             log=lambda m: log("  " + str(m)))
                note = f"新出 {len(made)} 张"
                log(f"✅ [{j.id}] {label} — {note}")
            else:
                raise ValueError(f"不支持的作业类型：{j.kind}")
            finish(proj, j.id, ok=True, note=note)
            # 按**作业实际张数**记账 —— start_async 对 queue 阶段不再预记账，
            # 避免"启动时把整个队列估进去 + 每个作业再记一笔"的重复计费
            B.record(root, f"queue:{j.kind}", est, ok=True, note=label)
            done += 1
        except Exception as e:                                  # noqa: BLE001
            msg = f"{type(e).__name__}: {e}"
            log(f"❌ [{j.id}] {label} 失败：{msg}")
            finish(proj, j.id, ok=False, note=msg)
            B.record(root, f"queue:{j.kind}", est, ok=False, note=f"{label} {msg}")
            failed += 1
    return {"done": done, "failed": failed, "stats": stats(proj)}
