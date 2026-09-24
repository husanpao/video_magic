"""
taskctl.py —— 任务控制层（CLI 与 Web 的唯一入口）。

为什么必须只有这一层
--------------------
最容易翻车的场景是「CLI 跑一份、Web 又跑一份，两个进程抢同一批产物」。
所以任何「启动 / 停止 / 查询任务」的动作都必须走这里：

    启动 → 拿项目锁 → 看 state/task.json 里记的 pid 还活着吗 → 派生 worker 子进程
    停止 → 只对这个 pid 发 SIGTERM（绝不 pkill -f、绝不按端口杀）
    状态 → task.json + pid 存活 一起判定（防止 pid 被回收后误判"还在跑"）

任务状态永远只有一份，落在 `<项目>/state/task.json`。

进程模型
--------
    CLI 前台 / Web 按钮 ──同一套 start_async()──▶ pipeline.py <项目> --stage X --_worker
                                                     └─ 新 session；pid 写进 task.json
                                                     └─ stdout 被本层收进 state/run.log

CLI 只负责「前台等待 + 转发信号」，不自己执行阶段代码；因此 Web 上点停止
能停掉 CLI 起的任务，反之亦然——两份控制逻辑不存在。

模块未就绪怎么办
----------------
并行开发期 vm/shots.py、vm/gen.py 等可能还不存在。本层对它们是**延迟导入**：
只做文件存在性检查（stage_readiness），真要用时才 import，并抛人话错误。
所以服务永远起得来、UI 永远打得开。
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import importlib
import json
import math
import os
import shutil
import re
import signal
import subprocess
import sys
import threading
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from vm.state import CURRENT, MISSING, STALE, Project, shot_fingerprint

# ---------------------------------------------------------------- 路径与常量

WORKSPACE = Path(__file__).resolve().parent.parent  # video_magic/
PROJECTS_DIR = WORKSPACE / "projects"
PIPELINE = WORKSPACE / "pipeline.py"

# 拆镜 LLM 的 Key 文件（与 plan.py 的解析顺序一致：环境变量优先，其次这个文件）
DEEPSEEK_KEY_FILE = Path("~/.config/video_magic/deepseek_key")

STAGES = ("plan", "chars", "gacha", "render", "qc", "assemble", "queue", "all")

# 抽卡默认张数（R2 契约：n 走 task.json 的 count 字段）
# 抽卡默认张数。用户实测反馈"抽 6 张太浪费时间"（每张约 40 秒，6 张要 4 分钟），
# 改成 2 张：先看风格对不对，不满意再补抽（已存在的 seed 会自动跳过）。
DEFAULT_GACHA_COUNT = 2

# 每个阶段真正依赖的 vm/*.py。用于「模块未就绪」的前置判断。
# 注意把间接依赖也列上（gen 会 import comfy），否则会在子进程里才炸。
STAGE_MODULES: dict[str, tuple[str, ...]] = {
    "plan": ("shots", "plan"),
    "chars": ("chars", "comfy", "gen", "shots"),
    "gacha": ("chars", "comfy", "gen", "shots"),
    "render": ("shots", "gen", "comfy"),
    "qc": ("shots", "qc"),
    "assemble": ("shots", "assemble"),
    # 队列 drainer：要能出素材图（assets→qi→comfy）也能角色抽卡（chars）
    "queue": ("shots", "queue", "assets", "qi", "comfy", "chars"),
}

# 并行开发期"文件在但函数还没写"是常态（例如 chars.py 只有 0 字节）。
# 这里列出各阶段真正要调用的入口函数，就绪判断连它一起查 —— 免得点了按钮才报 AttributeError。
STAGE_API: dict[str, dict[str, tuple[str, ...]]] = {
    "plan": {"plan": ("plan_chapter", "write_char_prompts")},
    "chars": {"chars": ("gen_all_chars",)},
    "gacha": {"chars": ("gen_candidates", "list_candidates", "check_prompt_style")},
    "render": {"gen": ("render_all",)},
    "qc": {"qc": ("check_all",)},
    "assemble": {"assemble": ("assemble",)},
    "queue": {"queue": ("drain", "stats", "claim_next", "finish")},
}

# --only 真正生效的阶段（其它阶段没有"按名字挑"的入口）
ONLY_STAGES = ("render", "chars", "gacha")

# 契约（CONTRACTS.md 已确认的决策）里定死的默认渲染参数。
# 项目没有 project.json 时用这套，保证 UI 与 CLI 在"空项目"下也能工作。
DEFAULT_PARAMS: dict[str, Any] = {
    "comfy_url": "http://127.0.0.1:8188",
    "width": 864,
    "height": 480,
    "fps": 24,
    "unet": "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
    "lora": "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors",
    "steps": 8,
    "llm": {"base_url": "https://api.deepseek.com", "model": "deepseek-chat"},
}

# 停止判定：worker 被 SIGTERM 后最多等多久才认为它"还在退"
STOP_WAIT = 10.0
# 看门狗：worker 收到停止信号后，若阶段函数卡住（例如等 ffmpeg），多久强退
WORKER_KILL_GRACE = 8.0

# 帧网格兜底（契约：17n+5，clamp [56,362]）。
# 只在本层需要"展示/判指纹"而 vm/shots.py 还没就绪时使用；权威实现永远在 shots.py。
_FRAME_MIN, _FRAME_MAX, _FRAME_STEP = 56, 362, 17


# ---------------------------------------------------------------- 异常


class TaskError(RuntimeError):
    """任务层通用错误（参数不对、没有章节文件……）。"""


class TaskBusy(TaskError):
    """同项目已有任务在跑 —— 对应 HTTP 409。"""


class NeedsApproval(TaskError):
    """
    E3：这一步超出预算，**暂停等人批**（不是失败）。

    调研原话：「一次『一键成片』内部连着发几十次付费调用，**中途没有任何刹车点**」，
    以及「**一道通向不了人的告警，和没有告警是一回事**」。
    所以这个异常必须携带**结构化载荷**，让 UI 能把三件事直接摆到人面前：
      已经花了多少 / 卡在哪一步 / 再放行多少才能继续。
    """

    def __init__(self, payload: dict):
        self.payload = payload or {}
        super().__init__(str(self.payload.get("message") or "超出预算，需要批准"))


class ModuleNotReady(TaskError):
    """依赖模块还没写完 —— 对应 HTTP 503，且必须是人话。"""


# ---------------------------------------------------------------- 小工具


def _atomic_write_json(path: Path, data: Any) -> None:
    """先写 .tmp 再 os.replace：掉电/被杀不会留下半截 JSON（与 state.py 同策略）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_json(path: Path, default: Any = None) -> Any:
    """容错读 JSON：文件不存在/损坏都返回 default，绝不因为一个坏文件把 UI 打挂。"""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def resolve_project(project: str | os.PathLike[str], root: Path | None = None) -> Path:
    """
    把项目名（或绝对路径）解析成项目目录。

    防目录穿越：项目名里出现 ".."/分隔符直接拒绝，避免 /api/status?project=../../etc
    把服务变成任意路径读取器。
    """
    p = Path(project)
    if p.is_absolute():
        return p
    name = str(project)
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
        raise TaskError(f"非法项目名：{name!r}")
    base = Path(root) if root else PROJECTS_DIR
    return base / name


def project_exists(pdir: Path) -> bool:
    """什么算一个"项目"：有 project.json / novel / shots 任一即可。"""
    return any((pdir / x).exists() for x in ("project.json", "novel", "shots"))


def discover_projects(root: Path | None = None) -> list[str]:
    base = Path(root) if root else PROJECTS_DIR
    if not base.is_dir():
        return []
    out = [d.name for d in sorted(base.iterdir()) if d.is_dir() and project_exists(d)]
    return out


def load_params(project: str | os.PathLike[str], root: Path | None = None) -> dict:
    """project.json + 契约默认值。API Key 永远来自环境变量/配置文件，不落项目文件。"""
    pdir = resolve_project(project, root)
    params = json.loads(json.dumps(DEFAULT_PARAMS))  # deep copy
    cfg = read_json(pdir / "project.json", default=None)
    if isinstance(cfg, dict):
        for k, v in cfg.items():
            if k == "llm" and isinstance(v, dict):
                params["llm"] = {**params.get("llm", {}), **v}
            else:
                params[k] = v
    # 按 plan.py 文档的解析顺序判断"Key 到底有没有"：环境变量 → ~/.config/video_magic/deepseek_key。
    # 只看环境变量会误报（服务进程常常没有 env，但 Key 文件在，拆镜照样能跑）。
    key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    try:
        params["api_key_present"] = bool(key) or (DEEPSEEK_KEY_FILE.expanduser().is_file()
                                                  and bool(DEEPSEEK_KEY_FILE.expanduser().read_text(encoding="utf-8").strip()))
    except OSError:
        params["api_key_present"] = bool(key)
    return params


def _module_file(name: str) -> Path:
    return Path(__file__).resolve().parent / f"{name}.py"


def module_ready(name: str) -> bool:
    return _module_file(name).is_file()


# 记录各模块上次导入时的 mtime：并行开发期队友会边写边改，
# 长驻的 Web 服务若一直用缓存的旧模块，界面就会和磁盘上的代码不一致。
_MODULE_MTIME: dict[str, float] = {}


def import_module(name: str):
    """
    延迟导入 vm.<name>。缺文件（或它自己的依赖缺文件）时给人话错误，
    而不是把 ModuleNotFoundError 的原始堆栈甩给用户。
    文件 mtime 变了会自动 reload（Web 服务不用重启就能看到队友刚改的代码）。
    """
    path = _module_file(name)
    if not path.is_file():
        raise ModuleNotReady(
            f"模块 vm/{name}.py 未就绪（可能还在并行开发中）：{path} 不存在"
        )
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    full = f"vm.{name}"
    cached = sys.modules.get(full)
    try:
        if cached is not None and _MODULE_MTIME.get(name) != mtime:
            mod = importlib.reload(cached)
        else:
            mod = importlib.import_module(full)
    except ModuleNotFoundError as e:
        missing = getattr(e, "name", "") or str(e)
        raise ModuleNotReady(
            f"模块 vm/{name}.py 存在，但它依赖的 {missing} 还没就绪（导入时报 ModuleNotFoundError）：{e}"
        ) from e
    except SyntaxError as e:
        # 队友正写到一半（半个文件）时会这样；不是我们的 bug，但要让人看得懂
        raise ModuleNotReady(
            f"模块 vm/{name}.py 现在读不进来（很可能正在被写入）：SyntaxError: {e}"
        ) from e
    _MODULE_MTIME[name] = mtime
    return mod


# ── stage_readiness 缓存（S8：readiness 按需）───────────────────────────────
# /api/status 每 2s 把 8 个阶段的 readiness 全问一遍，每次都读 vm/*.py 全文做
# 正则扫描（_module_missing_api）+ 逐模块 stat —— 而结果只在**队友改代码**时才变。
# 按所涉模块文件的 mtime+size 做键：文件没动就复用，动了立刻重算；TTL 兜底。
_READY_TTL = 30.0
_READY_CACHE: dict[tuple, tuple[float, dict]] = {}


def stage_readiness(stage: str) -> dict:
    """阶段是否可跑 + 缺了什么。UI 用它把按钮标灰并给出原因。"""
    if stage not in STAGE_MODULES:
        if stage == "all":
            need = sorted({m for mods in STAGE_MODULES.values() for m in mods})
        else:
            raise TaskError(f"未知阶段：{stage}（可选 {', '.join(STAGES)}）")
        api: dict[str, tuple[str, ...]] = {}
        for d in STAGE_API.values():
            for k, v in d.items():
                api[k] = tuple(sorted(set(api.get(k, ())) | set(v)))
    else:
        need = list(STAGE_MODULES[stage])
        api = STAGE_API.get(stage, {})

    # 缓存键 = (阶段, 所涉模块文件的 mtime/size) —— 只 stat 不读文件
    sig_list: list[tuple] = []
    for m in sorted(set(need) | set(api)):
        try:
            s = _module_file(m).stat()
            sig_list.append((m, int(s.st_mtime_ns), s.st_size))
        except OSError:
            sig_list.append((m, 0, 0))
    key = (stage, tuple(sig_list))
    hit = _READY_CACHE.get(key)
    if hit is not None and time.time() - hit[0] < _READY_TTL:
        return copy.deepcopy(hit[1])
    if len(_READY_CACHE) > 64:   # 每次改代码换一个键，别让缓存无限长
        _READY_CACHE.clear()

    missing = [m for m in need if not module_ready(m)]
    halffinished: list[str] = []
    for mod, attrs in api.items():
        if mod in missing:
            continue
        for a in _module_missing_api(mod, attrs):
            halffinished.append(f"vm/{mod}.py 里没有 {a}()")

    why = []
    if missing:
        why.append("缺文件：" + "、".join(f"vm/{m}.py" for m in missing))
    if halffinished:
        why.append("未写完：" + "、".join(halffinished))
    out = {
        "stage": stage,
        "ready": not missing and not halffinished,
        "missing": missing,
        "halffinished": halffinished,
        "message": "" if not (missing or halffinished) else "；".join(why) + "，该阶段暂不可运行",
    }
    _READY_CACHE[key] = (time.time(), copy.deepcopy(out))
    return out


def _module_missing_api(mod: str, attrs: tuple[str, ...]) -> list[str]:
    """
    看 vm/<mod>.py 里是否定义了这些入口函数。

    故意用"读文件 + 正则"而不是 import：并行开发期文件可能正在被写
    （半个文件会抛 SyntaxError），而且 import 结果会被缓存、看不到队友刚提交的版本。
    """
    try:
        text = _module_file(mod).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return list(attrs)
    return [a for a in attrs if not re.search(rf"^\s*(def|class)\s+{re.escape(a)}\b", text, re.M)]


def frames_for_seconds(sec: Any, fps: int = 24) -> int:
    """
    秒 → 17n+5 网格帧数。**直接代理 vm/shots.seconds_to_frames**（契约明文：权威在 shots.py）。

    为什么必须代理而不是各写各的（BUG-3 实锤）：两套换算一个用 round-half-even、
    一个用 half-up，`sec*fps` 落在半帧值时**差一格网格**（复现：
    frames_for_seconds(2.6875) 曾得 56、seconds_to_frames(2.6875) 得 73 帧）——
    同一个镜头在指纹/进度和渲染提交里帧数不一致，表看着好好的、渲出来对不上。

    兜底分支只在 vm/shots.py 缺失/正被写入时生效（服务永远起得来），
    数学与权威**逐行同语义**（half-up + [56,362] clamp），不许再出现第二套口径。
    """
    try:
        from vm.shots import seconds_to_frames
        return int(seconds_to_frames(sec, int(fps)))
    except Exception:  # noqa: BLE001 —— shots 未就绪时降级，不允许把服务拖挂
        # clamp 上下限与 vm/shots 同步读 VM_FRAME_MIN / VM_FRAME_MAX
        # （config-center 的帧网格配置经环境变量注入；缺省 56/362 —— 兜底口径
        #  必须和权威一致，包括配置覆盖之后）
        try:
            fmin = int(os.environ.get("VM_FRAME_MIN") or _FRAME_MIN)
            fmax = int(os.environ.get("VM_FRAME_MAX") or _FRAME_MAX)
        except ValueError:
            fmin, fmax = _FRAME_MIN, _FRAME_MAX
        try:
            s = float(sec)
        except (TypeError, ValueError):
            s = 0.0
        if s <= 0:
            return fmin
        n_min = (fmin - 5) // _FRAME_STEP
        n_max = (fmax - 5) // _FRAME_STEP
        n = int(math.floor((s * int(fps) - 5) / _FRAME_STEP + 0.5))
        n = max(n_min, min(n_max, n))
        return n * _FRAME_STEP + 5


# ---------------------------------------------------------------- pid 安全


def _proc_stat_state(pid: int) -> str:
    """读 /proc/<pid>/stat 的状态位（第 3 列）。僵尸进程 kill(pid,0) 也会成功，
    不查这一位就会把"已死的僵尸"当成"还在跑"，于是永远拒绝新任务。"""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    tail = data.rsplit(")", 1)
    if len(tail) != 2:
        return ""
    parts = tail[1].split()
    return parts[0] if parts else ""


def pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 别人的进程：存在，但肯定不是我们的
    return _proc_stat_state(pid) != "Z"


def proc_cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().decode("utf-8", "replace").replace("\x00", " ").strip()
    except OSError:
        return ""


def is_our_worker(pid: int, project: str) -> bool:
    """
    判断这个 pid 还是不是"我们派生的那个 worker"。

    这是停止前的最后一道闸：pid 会被系统回收复用，若只看 pid 存在就发信号，
    可能一枪打在别人的进程上。所以要求 cmdline 里同时出现
    pipeline.py + --_worker + 项目名。
    cmdline 读不到（/proc 不可读）时返回 False —— 宁可拒绝，也不误杀。
    """
    cmd = proc_cmdline(pid)
    if not cmd:
        return False
    return ("pipeline.py" in cmd) and ("--_worker" in cmd) and (project in cmd)


def _signal_worker(pid: int) -> str:
    """
    对精确 pid 发 SIGTERM。

    额外只做一件"胆子很小"的事：若该 pid 恰好是**我们自己**用 start_new_session
    建的会话首进程（pgid == pid == sid），就连它的进程组一起收 —— 目的是
    让 worker 拉起的 ffmpeg 子进程也停下，避免留下孤儿。
    这不等于"按端口/按名字杀"：目标集合完全由这一个 pid 推导，且加了双重身份校验。
    """
    sent = "pid"
    try:
        pgid = os.getpgid(pid)
        sid = os.getsid(pid)
    except OSError:
        pgid = sid = -1
    if pgid == pid and sid == pid:
        try:
            os.killpg(pid, signal.SIGTERM)
            return "session"
        except OSError:
            pass
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return "gone"
    return sent


# ---------------------------------------------------------------- 项目锁


_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


@contextmanager
def _project_lock(pdir: Path):
    """
    同项目互斥锁。两层：
      · threading.Lock —— Web 是多线程服务器，同进程内的并发请求要挡住
      · fcntl.flock    —— CLI 是另一个进程，跨进程要挡住
    只做一层，总会在另一半漏掉竞态。

    锁只覆盖"检查 + 派生 + 写 task.json"这段临界区，任务运行期间不持有：
    运行中的互斥靠 task.json 里的 pid 存活判定（这样 CLI/Web 才能互相看见）。
    """
    key = str(pdir)
    with _LOCKS_GUARD:
        tlock = _LOCKS.setdefault(key, threading.Lock())
    with tlock:
        state_dir = pdir / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        lf = open(state_dir / "task.lock", "a+")
        try:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
            finally:
                lf.close()


# ---------------------------------------------------------------- task.json


def state_dir(project: str | os.PathLike[str], root: Path | None = None) -> Path:
    return resolve_project(project, root) / "state"


def task_path(project: str | os.PathLike[str], root: Path | None = None) -> Path:
    return state_dir(project, root) / "task.json"


def log_path(project: str | os.PathLike[str], root: Path | None = None) -> Path:
    return state_dir(project, root) / "run.log"


def read_task(project: str | os.PathLike[str], root: Path | None = None) -> dict | None:
    d = read_json(task_path(project, root), default=None)
    return d if isinstance(d, dict) else None


def write_task(pdir: Path, data: dict) -> None:
    _atomic_write_json(pdir / "state" / "task.json", data)


def record_worker_exit(project: str | os.PathLike[str], pid: int, rc: int, root: Path | None = None) -> None:
    """
    worker 自报退出码。

    正常情况由监督者（CLI/Web）收尾时写；但如果监督者被强杀（服务重启、kill -9），
    就没人写了，UI 只能看到 exit_code=null。所以 worker 退出时也补一笔 ——
    加了项目锁 + pid 比对，两边写的是同一个值，不会互相覆盖。
    """
    pdir = resolve_project(project, root)
    try:
        with _project_lock(pdir):
            cur = read_task(pdir) or {}
            if int(cur.get("pid") or 0) != int(pid):
                return  # 已经被新任务顶掉，别污染新记录
            cur["exit_code"] = int(rc)
            cur["finished_at"] = cur.get("finished_at") or int(time.time())
            cur["stopped"] = bool(cur.get("stopped")) or rc in (-15, -2, 143, 130) or rc < 0
            write_task(pdir, cur)
    except Exception:
        pass  # 收尾失败绝不能影响 worker 的退出码


def task_running(task: dict | None) -> bool:
    """task.json + pid 存活 + 身份三者合判。"""
    if not task:
        return False
    if task.get("finished_at"):
        return False
    pid = int(task.get("pid") or 0)
    if not pid or not pid_alive(pid):
        return False
    if not proc_cmdline(pid):
        return True  # 读不到 cmdline 就保守当在跑，宁可拒绝第二次启动
    return is_our_worker(pid, str(task.get("project") or ""))


def task_public(task: dict | None, project: str) -> dict:
    """给 UI 的任务视图：额外算上 running / elapsed，不泄露内部字段。"""
    if not task:
        return {
            "project": project,
            "running": False,
            "stage": "",
            "shot": "",
            "pid": 0,
            "started_at": 0,
            "stopped": False,
            "elapsed": 0,
        }
    running = task_running(task)
    started = int(task.get("started_at") or 0)
    end = int(task.get("finished_at") or (time.time() if running else started))
    return {
        "project": task.get("project", project),
        "running": running,
        "stage": task.get("stage", ""),
        "shot": task.get("shot", "") or "",
        "pid": int(task.get("pid") or 0),
        "started_at": started,
        "stopped": bool(task.get("stopped")),
        "finished_at": task.get("finished_at"),
        "exit_code": task.get("exit_code"),
        "only": task.get("only") or [],
        "count": int(task.get("count") or 0),
        "force": bool(task.get("force")),
        "dry": bool(task.get("dry")),
        "error": task.get("error") or "",
        "elapsed": max(0, end - started) if started else 0,
    }


# ---------------------------------------------------------------- 进度

_PROGRESS_FILE = "progress.json"


def progress_path(project: str | os.PathLike[str], root: Path | None = None) -> Path:
    return state_dir(project, root) / _PROGRESS_FILE


def write_progress(project: str | os.PathLike[str], data: dict, root: Path | None = None) -> None:
    """worker 每 2 秒写一次；只有 worker 写这个文件，天然无写冲突。"""
    d = dict(data)
    d["at"] = int(time.time())
    _atomic_write_json(progress_path(project, root), d)


def read_progress(project: str | os.PathLike[str], root: Path | None = None) -> dict | None:
    d = read_json(progress_path(project, root), default=None)
    return d if isinstance(d, dict) else None


def _count(pattern_dir: Path, glob: str) -> int:
    try:
        return sum(1 for _ in pattern_dir.glob(glob))
    except OSError:
        return 0


def derive_progress(project: str | os.PathLike[str], stage: str = "", root: Path | None = None) -> dict:
    """
    从磁盘现状推导阶段进度。

    为什么不信任 worker 自报：阶段函数（契约里）没有 progress 回调，
    与其临时改接口，不如按"产物有没有落地"来算 —— 这个数字永远是真的，
    而且崩溃重启后也自然准确。当前镜头取自 manifest 三态里第一个未完成的。
    """
    pdir = resolve_project(project, root)
    proj = Project(pdir)
    stage = stage or "all"
    probe = stage

    if probe == "all":
        # all = 顺序跑，取第一个"还没完成"的子阶段作为当前阶段
        # （gacha 是交互式支线，不属于 all 链）
        for candidate in ("plan", "chars", "render", "qc", "assemble"):
            sub = derive_progress(pdir, candidate)
            if sub["total"] == 0 or sub["done"] < sub["total"]:
                probe = candidate
                break
        else:
            probe = "assemble"

    # --only 生效的阶段：进度只算被挑中的那些角色/镜头（否则进度条会一直显示"没动"）
    task = read_task(pdir) or {}
    only = [str(x) for x in (task.get("only") or [])]

    if probe == "plan":
        total = _count(proj.novel_dir, "*.md") + _count(proj.novel_dir, "*.txt")
        done = _count(proj.shots_dir, "*.json")
        label = "拆镜（章节 → 镜头表）"
    elif probe in ("chars", "gacha"):
        names = char_names(pdir)
        if only:
            names = [n for n in names if n in set(only)]
        if probe == "chars":
            total = len(names)
            done = sum(1 for n in names if (proj.refs_dir / f"char_{n}.png").exists())
            label = "定妆照（单张）"
        else:
            count = int(task.get("count") or DEFAULT_GACHA_COUNT)
            total = len(names) * count
            done = sum(len(_candidate_files(proj, n)) for n in names)
            label = f"抽卡（{len(names)} 角色 × {count} 张）"
    elif probe == "render":
        table = shot_table(pdir)
        if only:
            table_shots = [s for s in table["shots"] if s["id"] in set(only)]
        else:
            table_shots = table["shots"]
        total = len(table_shots)
        done = sum(1 for s in table_shots if s["status"] == CURRENT)
        current = next((s["id"] for s in table_shots if s["status"] != CURRENT), "")
        pct = int(done * 100 / total) if total else 0
        return {
            "stage": "render",
            "label": "逐镜生成",
            "done": done,
            "total": total,
            "pct": pct,
            "current": current,
        }
    elif probe == "qc":
        total = _count(proj.clips_dir, "*.mp4")
        done = len(_tolerant_qc(read_json(proj.state_dir / "qc.json", default=None)))
        label = "质检"
    elif probe == "assemble":
        total = 1
        done = len(episodes(proj))   # 逐章出集后可能是 EP01+EP02，不能只看 EP01
        label = "合成成片"
    else:
        total = done = 0
        label = stage

    # 别让"有记录但没产物"（例如 qc.json 里留着旧结果）算出 4/0 这种怪数字
    if total:
        done = min(done, total)
    elif done:
        done = 0
    pct = int(done * 100 / total) if total else (100 if done else 0)
    return {"stage": probe, "label": label, "done": done, "total": total, "pct": pct, "current": ""}


# ---------------------------------------------------------------- 镜头表视图


def _raw_shots(shots_dir: Path) -> tuple[list[dict], list[str]]:
    """直接读 shots/*.json 的兜底实现（vm/shots.py 未就绪时用）。"""
    shots: list[dict] = []
    issues: list[str] = []
    if not shots_dir.is_dir():
        return shots, issues
    for f in sorted(shots_dir.glob("*.json")):
        data = read_json(f, default=None)
        if data is None:
            issues.append(f"{f.name}: JSON 解析失败")
            continue
        items = data if isinstance(data, list) else [data]
        for it in items:
            if isinstance(it, dict):
                shots.append(it)
            else:
                issues.append(f"{f.name}: 镜头项不是对象")
    return shots, issues


def _as_list(v: Any) -> list:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


def _tolerant_qc(raw: Any) -> dict[str, dict]:
    """
    把 state/qc.json 归一化成 {shot: {ok, verdict, issues, metrics}}。

    qc 由队友实现，形状会微调（当前实际是 {"results": {...}, "rerender": [...]}），
    这里按"容器名"优先、再退回逐键识别的顺序宽容解析，绝不因为形状不同就 500。

    verdict 必须带上（不能只给 ok）：契约里 ok=True 只代表"没有硬故障"，
    suspicious（例如夜戏偏暗、段尾疑似冻结）也是 ok=True 但要单独显示成"可疑"
    而不是"通过" —— 界面上把可疑画成绿色就等于把人工复核这一档吞掉了。
    """
    out: dict[str, dict] = {}

    def _add(k: Any, v: Any) -> None:
        if isinstance(v, dict) and ("ok" in v or "issues" in v or "metrics" in v):
            metrics = v.get("metrics") if isinstance(v.get("metrics"), dict) else {}
            verdict = str(v.get("verdict") or metrics.get("verdict") or "")
            if not verdict:
                verdict = "pass" if v.get("ok") else "fail"
            out[str(k)] = {
                "ok": bool(v.get("ok")),
                "verdict": verdict,
                "issues": [str(x) for x in _as_list(v.get("issues"))],
                "metrics": metrics,
            }
        elif isinstance(v, bool):
            out[str(k)] = {"ok": v, "verdict": "pass" if v else "fail", "issues": [], "metrics": {}}

    if isinstance(raw, dict):
        for container in ("results", "shots", "clips"):
            c = raw.get(container)
            if isinstance(c, dict):
                for k, v in c.items():
                    _add(k, v)
                return out
        for k, v in raw.items():
            _add(k, v)
    elif isinstance(raw, list):
        for v in raw:
            if isinstance(v, dict) and v.get("shot"):
                _add(v["shot"], v)
    return out


# 质检语义（契约已裁决，勿改）：pass=全绿；suspicious=可疑但不阻塞（进 review 人工看）；
# fail=硬故障（进 rerender）；error=检查未执行（也算不通过）。
QC_VERDICT_CN = {
    "pass": "通过",
    "suspicious": "可疑",
    "fail": "不合格",
    "error": "未执行",
    "unknown": "未质检",
}


def qc_table(project: str | os.PathLike[str], root: Path | None = None) -> dict:
    """
    GET /api/qc 的全部数据：逐镜判定 + 汇总 + rerender/review 队列。

    为什么单独开一个接口而不是只看 /api/shots 里的 qc 字段：
    镜头表那一份是"每镜一行"的视图，缺 generated_at / rerender / review 这些
    整片级信息；质检 tab 要把「什么时候跑的、哪些要重渲、哪些只需人工看」一起说清楚。
    """
    pdir = resolve_project(project, root)
    proj = Project(pdir)
    raw = read_json(proj.state_dir / "qc.json", default=None)
    results = _tolerant_qc(raw)
    rerender = [str(x) for x in _as_list((raw or {}).get("rerender") if isinstance(raw, dict) else None)]
    review = [str(x) for x in _as_list((raw or {}).get("review") if isinstance(raw, dict) else None)]
    counts: dict[str, int] = {}
    for r in results.values():
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    return {
        "project": pdir.name,
        "generated_at": int((raw or {}).get("generated_at") or 0) if isinstance(raw, dict) else 0,
        "results": results,
        "rerender": rerender,
        "review": review,
        "counts": counts,
        "has_result": bool(results),
    }


def audit_table(project: str | os.PathLike[str], root: Path | None = None) -> dict:
    """
    GET /api/audit 的全部数据：原样返回 state/audit.json，并做两件**只读**加工：

      ① 把 findings 里的 `from_shot/to_shot/shots`（**1-based 镜头序号**，不是镜头号）
         映射成镜头号 —— 界面上要看的是 `1-7-01`，不是"第 8 镜"。
      ② 按 severity 汇总（info/warning/error）。审计队友刻意把不可信结论降为 info
         并自曝（B2 代理分、B1 分组退化），界面必须**分档语气**，info 不能画成红色告警。
    """
    pdir = resolve_project(project, root)
    proj = Project(pdir)
    raw = read_json(proj.state_dir / "audit.json", default=None)
    if not isinstance(raw, dict):
        return {
            "project": pdir.name, "ok": False, "has_audit": False,
            "summary": {"total_findings": 0, "findings": {}, "gate_passed": None},
            "findings": [], "limitations": [],
            "message": "还没有审计结果：跑一次审计阶段（vm/audit）后这里会有内容",
        }
    findings = [f for f in _as_list(raw.get("findings")) if isinstance(f, dict)]
    # 镜头表顺序 = 序号基准（与 vm/audit 的 b1/b2 一致）
    order: list[str] = []
    try:
        order = [str(r["id"]) for r in shot_table(project, root)["shots"]]
    except Exception:  # noqa: BLE001 - 表读不了也不能让审计接口 500
        order = []

    def _sid(idx: Any) -> str:
        try:
            n = int(idx)
        except (TypeError, ValueError):
            return ""
        return order[n - 1] if 1 <= n <= len(order) else ""

    out_findings: list[dict] = []
    sev_counts: dict[str, int] = {}
    for f in findings:
        # vm/audit 的报告组装层已经把序号解析成 id 了（一处改动，不在各模块各写一遍），
        # 优先用它；只有旧格式（没有 from_id/shot_ids）才退回本地按序号映射。
        raw_ids = [str(x) for x in _as_list(f.get("shot_ids")) if str(x or "").strip()]
        if not raw_ids:
            raw_ids = [x for x in (_sid(i) for i in _as_list(f.get("shots"))) if x]
        from_id = str(f.get("from_id") or "") or (raw_ids[0] if raw_ids else _sid(f.get("from_shot")))
        to_id = str(f.get("to_id") or "") or (raw_ids[-1] if raw_ids else _sid(f.get("to_shot")))
        sev = str(f.get("severity") or "info")
        sev_counts[sev] = sev_counts.get(sev, 0) + 1
        out_findings.append(
            {
                "code": str(f.get("code") or ""),
                "severity": sev,
                "module": str(f.get("module") or ""),
                "from_shot": f.get("from_shot"),
                "to_shot": f.get("to_shot"),
                "shot_ids": raw_ids,
                # 全片级 findings（no-climax / pacing 等）没有具体镜头 → 明确给空串，
                # 界面必须把它们放在项目级区域，**不能当成"第 0 镜"挂到某个镜头上**
                "project_level": not raw_ids and not from_id,
                "from_id": from_id or "",
                "to_id": to_id or "",
                "chars": [str(c) for c in _as_list(f.get("chars"))],
                "message": str(f.get("message") or ""),
                "rewrite_hint": str(f.get("rewrite_hint") or ""),
            }
        )
    # 项目级 findings 排前面（它们是"全片"结论，不属于任何一镜）
    out_findings.sort(key=lambda x: (0 if x["project_level"] else 1))
    summary = raw.get("summary") if isinstance(raw.get("summary"), dict) else {}
    return {
        "project": pdir.name,
        "ok": True,
        "has_audit": True,
        "generated_at": str(raw.get("generated_at") or ""),
        "inputs": raw.get("inputs") if isinstance(raw.get("inputs"), dict) else {},
        "summary": {**summary, "by_severity": sev_counts},
        "manifest_entry": raw.get("manifest_entry") if isinstance(raw.get("manifest_entry"), dict) else {},
        "b1": raw.get("b1") if isinstance(raw.get("b1"), dict) else {},
        "b2": raw.get("b2") if isinstance(raw.get("b2"), dict) else {},
        "b5": raw.get("b5") if isinstance(raw.get("b5"), dict) else {},
        "findings": out_findings,
        "limitations": [str(x) for x in _as_list(raw.get("limitations"))],
        "shot_order": order,
    }



def _shot_order(sid: str) -> tuple:
    """
    镜头 id → 可排序的数值元组，用于按**分镜顺序**排列。

    为什么不能直接按字符串排：`1-10-01` 会排在 `1-1-01` 前面（'-' < '0'），
    用户看到的镜头表顺序会整个错乱。
    """
    nums = [int(x) for x in re.findall(r"\d+", sid or "")][:3]
    return tuple(nums) + (0,) * (3 - len(nums))


# ── shot_table 缓存（S8：/api/status 减负）──────────────────────────────────
# 为什么：前端 2s 轮询一次 /api/status，每次全量重算指纹表（读全部 shots/*.json +
# 逐镜 md5 指纹 + validate + stat 全部产物）—— 空闲时输入根本没变，纯浪费。
# 策略 = **mtime 签名 + 短 TTL 双条件**：
#   · 签名（只 stat 不读文件）没变 → 复用缓存；任何输入一变（编辑/渲出一镜/换参考图）
#     立刻重算，写后即刷不受影响；
#   · TTL 是兜底上限：签名万一看漏了什么（例如 vm/shots.py 自身行为变化），
#     最坏陈旧 _SHOT_TABLE_TTL 秒后也强制重算。
# 返回一律 deepcopy —— 调用方会往结果里塞 ok/url 字段（web 层），不能污染缓存。
_SHOT_TABLE_TTL = 10.0
_SHOT_TABLE_CACHE: dict[str, tuple[float, tuple, dict]] = {}


def _shot_table_signature(pdir: Path) -> tuple:
    """输入签名：镜头表/参考图/产物/manifest/qc/params 的 (路径, mtime_ns, size)。"""
    proj = Project(pdir)
    items: list[Any] = []

    def _stamp(f: Path) -> None:
        try:
            s = f.stat()
            items.append((str(f), int(s.st_mtime_ns), s.st_size))
        except OSError:
            items.append((str(f), 0, 0))

    _stamp(pdir / "project.json")
    for d, pat in ((proj.shots_dir, "*.json"), (proj.refs_dir, "char_*.png"), (proj.clips_dir, "*.mp4")):
        try:
            names = sorted(x.name for x in d.glob(pat))
        except OSError:
            names = []
        items.append((str(d), tuple(names)))
        for n in names:
            _stamp(d / n)
    _stamp(proj.state_dir / "manifest.json")
    _stamp(proj.state_dir / "qc.json")
    return tuple(items)


def shot_table(project: str | os.PathLike[str], root: Path | None = None) -> dict:
    """
    镜头表 + 每镜三态 + 质检结果。Web 的 /api/shots 直接吐这个结构。

    三态优先用 vm/shots.py 的权威帧网格；它还没就绪时用本层兜底实现，
    并在返回里标 source=fallback（诚实标注，不假装精确）。
    """
    pdir = resolve_project(project, root)
    # ── 缓存命中：输入没动过 + 没超 TTL，直接复用（见 _SHOT_TABLE_CACHE 注释）──
    _sig = _shot_table_signature(pdir)
    _hit = _SHOT_TABLE_CACHE.get(str(pdir))
    if _hit is not None:
        _at, _hit_sig, _data = _hit
        if _hit_sig == _sig and time.time() - _at < _SHOT_TABLE_TTL:
            return copy.deepcopy(_data)
    proj = Project(pdir)
    params = load_params(pdir)
    issues: list[str] = []
    source = "fallback"

    mod = None
    if module_ready("shots"):
        try:
            mod = import_module("shots")
            source = "vm.shots"
        except ModuleNotReady as e:
            issues.append(str(e))
            mod = None

    shots: list[Any] = []
    if mod is not None:
        try:
            shots = list(mod.load_shots_dir(proj.shots_dir))
        except Exception as e:  # 队友的表里有脏数据也不能把 UI 打挂
            issues.append(f"load_shots_dir 失败：{e}")
            shots = []
    else:
        shots = _raw_shots(proj.shots_dir)[0]

    if mod is not None and shots:
        try:
            issues.extend(str(x) for x in mod.validate_shots(shots, proj.refs_dir))
        except Exception as e:
            issues.append(f"validate_shots 失败：{e}")

    qc = _tolerant_qc(read_json(proj.state_dir / "qc.json", default=None))
    manifest = proj.manifest
    rows: list[dict] = []

    for sh in shots:
        if isinstance(sh, dict):
            g = sh.get
        else:  # Shot dataclass
            g = lambda k, d=None, _s=sh: getattr(_s, k, d)  # noqa: E731
        sid = str(g("id", "") or "")
        sec = g("sec", 0) or 0
        chars = [str(c) for c in _as_list(g("chars", []))]
        seed = g("seed", 0)
        prompt = str(g("prompt", "") or "")
        try:
            frames = int(mod.seconds_to_frames(float(sec), params.get("fps", 24))) if mod else frames_for_seconds(sec, params.get("fps", 24))
        except Exception:
            frames = frames_for_seconds(sec, params.get("fps", 24))
        refs = [proj.refs_dir / f"char_{c}.png" for c in chars]
        clip = proj.clip(sid) if sid else proj.clips_dir / "_.mp4"
        try:
            fp = shot_fingerprint(prompt, chars, refs, params, frames, int(seed or 0))
            st = manifest.status(sid, fp, clip)
        except Exception:
            fp = ""
            st = MISSING if not clip.exists() else STALE
        try:
            cst = clip.stat()
            clip_info = {"exists": True, "size": cst.st_size, "mtime": int(cst.st_mtime)}
        except OSError:
            clip_info = {"exists": False, "size": 0, "mtime": 0}
        q = qc.get(sid)
        # 服装（P2 的字段，P1 先原样透给 UI 显示标签位）。
        # 允许它不是对象（旧表/手写表可能是 "孙悟空=armor" 这种字符串），
        # 一律塞进 __raw__，绝不让 UI 因为一个奇怪的值就崩掉。
        costume_raw = g("costume", None)
        if isinstance(costume_raw, dict):
            costume = {str(k): str(v) for k, v in costume_raw.items()}
        elif costume_raw:
            costume = {"__raw__": str(costume_raw)}
        else:
            costume = {}
        rows.append(
            {
                "id": sid,
                "sec": sec,
                "chars": chars,
                "seed": seed,
                "shot_size": str(g("shot_size", "") or ""),
                "camera": str(g("camera", "") or ""),
                "dialogue": str(g("dialogue", "") or ""),
                "narration": str(g("narration", "") or ""),
                "costume": costume,
                "prompt_head": prompt[:120],
                "prompt_len": len(prompt),
                "frames": frames,
                "status": st,
                "fp": fp,
                "fp_source": source,
                "clip": clip_info,
                "qc": q if q else None,
                "locked": bool(manifest.shots.get(sid) and manifest.shots[sid].locked),
                "selected": bool(manifest.shots.get(sid) and manifest.shots[sid].selected),
                # A2：时间轴口径。分段播放器按 sec_actual 等比例分段 —— 它既是进度条、
                # 又是"每镜多长"的分布图，也是导航。缺 sec_actual 时回退请求值并标明。
                # ★ 从**镜头表**读 sec_actual（回填的值在 shots/*.json 里），
                #   manifest 里那份只有"字段加入之后才渲染的镜头"才有 —— 两者取先有的那个。
                # 场景实体归属（`scene_id` 指向项目 scenes.json）。
                # ⚠️ 注意：**不是 id 里的场次号** —— 实测本片 52 镜被分成 47 个场次号，
                # 那只是编号；这个才是"同一个地点"。
                "scene_id": str(g("scene_id") or ""),
                "sec_actual": (
                    g("sec_actual", None)
                    or getattr(manifest.shots.get(sid), "sec_actual", None)
                ),
                "sec_override": g("sec_override", None),
                "sec_source": (
                    "sec_actual"
                    if (g("sec_actual", None) or getattr(manifest.shots.get(sid), "sec_actual", None))
                    else "planned"
                ),
            }
        )

    order = {MISSING: 0, STALE: 1, CURRENT: 2}
    counts = {
        "total": len(rows),
        "current": sum(1 for r in rows if r["status"] == CURRENT),
        "stale": sum(1 for r in rows if r["status"] == STALE),
        "missing": sum(1 for r in rows if r["status"] == MISSING),
        "qc_fail": sum(1 for r in rows if r["qc"] and not r["qc"].get("ok")),
    }
    # ★ 按**分镜顺序**排列，不要按状态排。
    #   2026-09-23 用户实测报障："右边镜头片段都显示无产物，实际有的"。
    #   原因就是这里原来按 (status, id) 排：48 个未渲染的排在前面、
    #   4 个已渲染的被挤到第 48-51 位，用户看到的前几十张全是"无产物"。
    #   状态标签在 UI 上已经单独显示了，顺序应该还原成剧本顺序，
    #   否则镜头表根本没法当分镜稿读（1-10-01 排在 1-1-01 前面也是同一类错误）。
    rows.sort(key=lambda r: _shot_order(r["id"]))
    _out = {
        "project": str(pdir.name),
        "source": source,
        "shots": rows,
        "issues": issues,
        "counts": counts,
        "shots_dir": str(proj.shots_dir),
    }
    # 存副本、返回原件：调用方会往结果里塞 ok / url 字段，不能污染缓存。
    # dict 赋值是原子的，Web 多线程并发时最坏只是各算各的，不会写坏。
    _SHOT_TABLE_CACHE[str(pdir)] = (time.time(), _sig, copy.deepcopy(_out))
    return _out


# ---------------------------------------------------------------- 镜头详情与编辑（P1）
#
# 为什么这一层自己读写 JSON、而不复用 vm/shots.py 的 load_shots → save_shots：
#   shots.py 的 Shot 只有 9 个字段，load→save 会把表里的其它键（action、costume，
#   以及 P2 之后新增的任何字段）**静默抹掉**。编辑是"改一个字段"，
#   绝不能顺手把别的字段丢了 —— 所以这里按**原始 dict** 编辑：
#   读到什么形状就写回什么形状，只改被点名的键。
#
# 四条硬规则（P1 任务书）：
#   ① 只写盘 + 标 stale，绝不触发渲染（指纹机制天然让相关镜头变"待重渲"）
#   ② 写盘前先备份 shots/*.json.bak（一步可撤销）
#   ③ 写盘前重跑 validate_shots；**本次改动引入的新问题**一律拒绝（HTTP 400 + 问题清单）
#      为什么判"新问题"而不是"任何问题"：表里可能本来就有历史问题（手改/旧数据），
#      按"任何问题"拒绝会让用户因为一个不相干的旧毛病而永远改不动 —— 那是把人锁死。
#      历史问题照实回报给 UI（issues 字段）让用户看见，但本次引入的必须拒绝。
#   ④ 编辑**不占项目锁**：用户要能在渲染进行时改后面还没渲的镜头。
#      渲染 worker 在 render_all() 开头就把镜头表读进内存、之后不再读盘，
#      所以写盘不会影响正在跑的那一批；这里另用一个**编辑锁**防止两个编辑互相覆盖。

SHOT_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")

# 六段式 / 单段式的段名与顺序（权威在 vm/shots.py；这里留一份本地副本，
# 是为了让"服务永远起得来"—— shots.py 正在被写（半个文件）时本模块仍可用。
SIX_SECTIONS = (
    "subject_definitions",
    "summary",
    "retention_analysis",
    "detailed_description",
    "overall_soundscape",
    "non_diegetic_music",
)
FL2VA_SECTIONS = ("integrated_multimodal_description", "overall_soundscape", "non_diegetic_music")

# 单镜可改的字段（patch）。id 不在其中：改 id 会让已有产物/指纹记录错位，
# 要改编号请用 renumber_shots()（它会把产物一起搬过去）。
SHOT_PATCH_FIELDS = (
    "dialogue", "narration", "camera", "shot_size", "sec", "chars", "seed", "prompt", "costume", "action",
)
# 批量改只允许"安全"的字段：批量改台词/旁白/提示词几乎一定是误操作。
BULK_PATCH_FIELDS = ("shot_size", "camera", "sec", "costume", "action")

# 拆分点候选标点（中英）
_SPLIT_BOUNDARY = "，。！？；：、…,.!?;:"

# 兜底上限。权威值在 vm/plan.py（DIALOGUE_MAX/NARRATION_MAX）与 vm/shots.py（SEC_MIN/SEC_MAX），
# 由 _limits() 在调用时懒加载真实值 —— 绝不在这里写死成"另一个真相"。
_FALLBACK_LIMITS = {"dialogue_max": 20, "narration_max": 15, "sec_min": 3, "sec_max": 15, "chars_per_sec": 4.0}


class ShotInvalid(TaskError):
    """表校验没过：带问题清单（HTTP 400 + issues）。"""

    def __init__(self, message: str, issues: list[str] | None = None):
        super().__init__(message)
        self.issues = list(issues or [])


class ShotLLMError(TaskError):
    """单镜重写的 LLM 失败/结果不合格：带问题清单，可能还带一版草稿正文。"""

    def __init__(self, message: str, issues: list[str] | None = None, payload: dict | None = None):
        super().__init__(message)
        self.issues = list(issues or [])
        self.payload = payload or {}


def _limits() -> dict:
    """语音/时长上限：优先取 vm/shots.py 与 vm/plan.py 的真值，取不到才用兜底。"""
    lim = dict(_FALLBACK_LIMITS)
    try:
        sh = import_module("shots")
        for key, attr in (("sec_min", "SEC_MIN"), ("sec_max", "SEC_MAX"), ("chars_per_sec", "CHARS_PER_SEC")):
            v = getattr(sh, attr, None)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                lim[key] = v
    except Exception:  # noqa: BLE001 - 模块未就绪时用兜底，不影响编辑
        pass
    try:
        pl = import_module("plan")
        for key, attr in (("dialogue_max", "DIALOGUE_MAX"), ("narration_max", "NARRATION_MAX")):
            v = getattr(pl, attr, None)
            if isinstance(v, int) and not isinstance(v, bool):
                lim[key] = v
    except Exception:  # noqa: BLE001
        pass
    return lim


def _sections_constants() -> tuple[tuple[str, ...], tuple[str, ...]]:
    try:
        sh = import_module("shots")
        six = tuple(getattr(sh, "SIX_SECTIONS", SIX_SECTIONS))
        fl2 = tuple(getattr(sh, "FL2VA_SECTIONS", FL2VA_SECTIONS))
        return six, fl2
    except Exception:  # noqa: BLE001
        return SIX_SECTIONS, FL2VA_SECTIONS


def _as_int_strict(value: Any) -> int | None:
    """整数强校验（拒绝 bool / 小数 / "abc"），用于 sec 与 seed。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value.strip())
    return None


def _coerce_costume(value: Any) -> dict[str, str]:
    """服装字段归一成 {角色: 服装id}。支持对象 / "孙悟空=armor,唐僧=default" / 数组。"""
    if value is None or value == "":
        return {}
    out: dict[str, str] = {}
    if isinstance(value, dict):
        for k, v in value.items():
            if v is None or v == "":
                continue
            out[str(k)] = str(v)
        return out
    if isinstance(value, str):
        parts: list[Any] = re.split(r"[,，;；]+", value)
    elif isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        raise TaskError(f"costume 必须是对象、'角色=服装' 字符串或数组，收到 {type(value).__name__}")
    for part in parts:
        if not isinstance(part, str):
            raise TaskError("costume 数组里只能放 '角色=服装' 字符串")
        k, _, v = part.partition("=")
        k = k.strip()
        if k:
            out[k] = (v.strip() or "default")
    return out


def _coerce_field(key: str, value: Any) -> Any:
    """patch 里单个字段的类型归一化。类型不对就报人话错误，绝不猜。"""
    if key in ("sec", "seed"):
        n = _as_int_strict(value)
        if n is None:
            raise TaskError(f"{key} 必须是整数，收到 {value!r}")
        return n
    if key == "chars":
        if value is None:
            return []
        if isinstance(value, str):
            return [c for c in re.split(r"[,，、\s]+", value) if c]
        if isinstance(value, (list, tuple)):
            out = []
            for c in value:
                if not isinstance(c, str):
                    raise TaskError(f"chars 里含非字符串项 {c!r}")
                if c.strip():
                    out.append(c.strip())
            return out
        raise TaskError(f"chars 必须是字符串数组或逗号分隔字符串，收到 {type(value).__name__}")
    if key == "costume":
        return _coerce_costume(value)
    if key in SHOT_PATCH_FIELDS:
        if value is None:
            return ""
        if not isinstance(value, str):
            raise TaskError(f"{key} 必须是字符串，收到 {type(value).__name__}")
        return value
    raise TaskError(f"不支持改字段 {key}")


# ---- 读取 / 写回原始镜头表 ------------------------------------------------

def _load_shot_docs(proj: Project) -> list[dict]:
    """
    读 shots/*.json，保留**原始形状**：顶层是数组还是 {"shots": [...]}、
    以及对象里的其它键（例如 plan 之后可能加的元数据）都要能原样写回。
    """
    docs: list[dict] = []
    d = proj.shots_dir
    if not d.is_dir():
        return docs
    for p in sorted(d.glob("*.json")):
        if p.name.startswith(".") or p.name.endswith(".tmp"):
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            # 不静默跳过：跳过会让用户"改着改着发现有的镜头不在表里"
            raise TaskError(f"镜头表 {p.name} 读不进来：{e}") from e
        wrap: str | None = None
        prefix: dict = {}
        if isinstance(data, dict):
            if not isinstance(data.get("shots"), list):
                raise TaskError(f"镜头表 {p.name} 顶层是对象但没有 shots 数组")
            wrap = "shots"
            prefix = {k: v for k, v in data.items() if k != "shots"}
            items = data["shots"]
        elif isinstance(data, list):
            items = data
        else:
            raise TaskError(f"镜头表 {p.name} 顶层必须是数组或 {{\"shots\": [...]}}")
        for i, it in enumerate(items):
            if not isinstance(it, dict):
                raise TaskError(f"镜头表 {p.name} 第 {i + 1} 项不是 JSON 对象，先手动修好再编辑")
        docs.append({"path": p, "wrap": wrap, "prefix": prefix, "items": items, "dirty": False})
    return docs


def _shot_refs(docs: list[dict]) -> list[tuple[int, int]]:
    """按**分镜顺序**（与镜头表 UI 一致）返回 [(文件下标, 项下标)]。"""
    refs = [(di, ii) for di, doc in enumerate(docs) for ii in range(len(doc["items"]))]
    refs.sort(key=lambda t: _shot_order(str(docs[t[0]]["items"][t[1]].get("id") or "")))
    return refs


def _find_in_docs(docs: list[dict], shot_id: str) -> tuple[dict, int]:
    hits = [
        (docs[di], ii)
        for di, ii in _shot_refs(docs)
        if str(docs[di]["items"][ii].get("id") or "") == str(shot_id)
    ]
    if not hits:
        raise TaskError(f"镜头 {shot_id} 不在镜头表里")
    if len(hits) > 1:
        raise TaskError(f"镜头 {shot_id} 在表里出现了 {len(hits)} 次（重号），先手动去重再编辑")
    return hits[0]


def _neighbor_in_docs(docs: list[dict], shot_id: str, delta: int) -> dict | None:
    refs = _shot_refs(docs)
    for i, (di, ii) in enumerate(refs):
        if str(docs[di]["items"][ii].get("id") or "") == str(shot_id):
            j = i + delta
            if 0 <= j < len(refs):
                return docs[refs[j][0]]["items"][refs[j][1]]
            return None
    raise TaskError(f"镜头 {shot_id} 不在镜头表里")


def _unique_shot_id(existing: Iterable[Any], base: str) -> str:
    """
    生成不与现有镜头重号的 id。

    为什么不自动"把后面的镜头整体+1"：那会让一批镜头的产物/指纹记录全部错位
    （在渲染进行中尤其危险）。这里用 `1-4-01_2` 这样的后缀，**不动任何已有镜头号**；
    需要连续编号时用户显式点「重排编号」。
    """
    taken = {str(x) for x in existing}
    if base not in taken:
        return base
    n = 2
    while f"{base}_{n}" in taken:
        n += 1
    return f"{base}_{n}"


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _retarget_entry_file(entry: Any, src: Path, dst: Path) -> None:
    """
    清单记录的 file 字段跟着产物走。

    比较时必须容忍"相对路径 vs 绝对路径"：清单里的路径可能是当初相对 cwd 记下的，
    而这里的 src 是绝对路径 —— 只比字符串会静默不更新，留下一个指向旧编号的记录。
    """
    try:
        old = str(getattr(entry, "file", "") or "")
        if old == str(src) or Path(old).name == src.name:
            entry.file = str(dst)
    except Exception:  # noqa: BLE001 - 清单字段只是备注，改不动也不能让编辑失败
        pass


def _backup_file(path: Path) -> Path:
    """把当前内容复制到 <名>.json.bak（一步撤销用）。复制走 .tmp+replace，避免半截备份。"""
    bak = path.with_suffix(path.suffix + ".bak")
    _atomic_write_bytes(bak, path.read_bytes())
    return bak


def _write_shot_doc(doc: dict) -> None:
    if doc["wrap"] == "shots":
        payload: Any = {**doc["prefix"], "shots": doc["items"]}
    else:
        payload = doc["items"]
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    _atomic_write_bytes(doc["path"], text.encode("utf-8"))


def _item_to_shot(mod: Any, item: dict) -> Any:
    """原始 dict → vm/shots.Shot（只用于校验；类型不对的值原样传进去让校验报出来）。"""
    chars = item.get("chars")
    if isinstance(chars, str):
        chars = [c for c in re.split(r"[,，、\s]+", chars) if c]
    elif isinstance(chars, (list, tuple)):
        chars = [str(c) for c in chars]
    elif chars is None:
        chars = []
    else:
        chars = [str(chars)]
    return mod.Shot(
        id=str(item.get("id") or ""),
        sec=item.get("sec"),
        chars=chars,
        seed=item.get("seed"),
        prompt=str(item.get("prompt") or ""),
        shot_size=str(item.get("shot_size") or ""),
        camera=str(item.get("camera") or ""),
        dialogue=str(item.get("dialogue") or ""),
        narration=str(item.get("narration") or ""),
    )


def _validate_items(mod: Any, items: list[dict], refs_dir: Path) -> list[str]:
    """对一份完整的镜头表跑权威校验（vm/shots.validate_shots），返回问题清单。"""
    try:
        shots = [_item_to_shot(mod, it) for it in items]
        return [str(x) for x in mod.validate_shots(shots, refs_dir)]
    except Exception as e:  # noqa: BLE001 - 队友的校验器炸了也要给人话
        return [f"validate_shots 执行失败：{type(e).__name__}: {e}"]


_EDIT_LOCKS: dict[str, threading.Lock] = {}


@contextmanager
def _shot_edit_lock(pdir: Path):
    """
    编辑专用锁（**不是** task.lock 项目锁）。

    为什么不复用项目锁：任务锁的语义是"同项目只能跑一个任务"，
    编辑接口一旦去抢它，用户在渲染进行时就改不了镜头 —— 而那正是最需要改的时候。
    这里用独立的锁文件，只保证"两次编辑不互相覆盖"，与任务并发完全无关。
    """
    key = str(pdir)
    with _LOCKS_GUARD:
        tlock = _EDIT_LOCKS.setdefault(key, threading.Lock())
    with tlock:
        state = pdir / "state"
        state.mkdir(parents=True, exist_ok=True)
        lf = open(state / "shot_edit.lock", "a+")
        try:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
            finally:
                lf.close()


def _record_edit(pdir: Path, info: dict) -> dict:
    """把"上一步改了什么"落盘，供 UI 显示/撤销；并追加一条文本日志。"""
    rec = {"at": int(time.time()), "time": time.strftime("%Y-%m-%d %H:%M:%S"), **info}
    _atomic_write_json(pdir / "state" / "shot_edit.json", rec)
    try:
        with open(pdir / "state" / "shot_edit.log", "a", encoding="utf-8") as f:
            f.write(
                f"[{rec['time']}] {rec.get('action', '?')} {rec.get('shot', '') or ''} "
                f"{rec.get('message', '')}\n"
            )
    except OSError:
        pass
    return rec


def last_edit(project: str | os.PathLike[str], root: Path | None = None) -> dict | None:
    pdir = resolve_project(project, root)
    d = read_json(pdir / "state" / "shot_edit.json", default=None)
    return d if isinstance(d, dict) else None


def _apply_edit(project, root, mutate, *, action: str, shot: str = "") -> dict:
    """
    编辑的统一闸门：读 → 改（内存）→ 校验 → 备份 → 原子写 → 记账。

    任何一步失败都在写盘之前抛出，磁盘上不会留下"改了一半"的表。
    """
    pdir = resolve_project(project, root)
    proj = Project(pdir)
    proj.ensure()
    mod = import_module("shots")  # 权威校验在 vm/shots.py；未就绪时由 web 层转 503
    with _shot_edit_lock(pdir):
        docs = _load_shot_docs(proj)
        if not docs:
            raise TaskError(f"{proj.shots_dir} 下没有镜头表（*.json）")
        before_items = [dict(it) for doc in docs for it in doc["items"]]
        before_problems = _validate_items(mod, before_items, proj.refs_dir)

        work = [
            {
                "path": doc["path"],
                "wrap": doc["wrap"],
                "prefix": doc["prefix"],
                "items": [dict(it) for it in doc["items"]],
                "dirty": False,
            }
            for doc in docs
        ]
        info = dict(mutate(work) or {})
        after_items = [it for doc in work for it in doc["items"]]

        after_problems = _validate_items(mod, after_items, proj.refs_dir)
        new_problems = list((Counter(after_problems) - Counter(before_problems)).elements())
        if new_problems:
            raise ShotInvalid(
                "已拒绝保存：这次改动会让镜头表出现新问题（磁盘没有改动）：\n- "
                + "\n- ".join(new_problems),
                issues=new_problems,
            )

        files: list[str] = []
        for doc in work:
            if not doc["dirty"]:
                continue
            _backup_file(doc["path"])  # 先备份再写：UI 的「撤销上一步」就靠它
            _write_shot_doc(doc)
            files.append(doc["path"].name)
        rec = _record_edit(
            pdir,
            {
                "action": action,
                "shot": shot,
                "files": files,
                "changed": info.get("changed") or [],
                "message": info.get("message") or "",
            },
        )
    return {
        "action": action,
        "shot": shot,
        "files": files,
        "changed": info.get("changed") or [],
        "warnings": info.get("warnings") or [],
        "issues": after_problems,
        "new_issues": [],
        "last_edit": rec,
        "undo_available": bool(files),
        **{k: v for k, v in info.items() if k not in ("changed", "warnings", "message")},
        "message": info.get("message") or "",
    }


# ---- 六段式拼装与"提示词 ↔ 字段"一致性 ------------------------------------

def compose_prompt_from_sections(sections: dict) -> str:
    """
    按**六段式固定顺序**把 sections 重新拼成 prompt。

    - 缺段、段内容为空（non_diegetic_music 除外）→ 报错，绝不静默补空段
      （少一段模型不报错，只是参考图不生效 —— 本项目最想消灭的一类静默劣化）
    - 段名不在白名单 → 报错，不静默丢弃
    """
    if not isinstance(sections, dict) or not sections:
        raise TaskError("sections 必须是非空对象")
    clean: dict[str, str] = {}
    for k, v in sections.items():
        name = str(k).strip()
        if not isinstance(v, str):
            raise TaskError(f"sections.{name} 必须是字符串（收到 {type(v).__name__}）")
        clean[name] = v
    six, fl2 = _sections_constants()
    known = set(six) | set(fl2)
    unknown = sorted(set(clean) - known)
    if unknown:
        raise TaskError("不认识的段名：" + "、".join(unknown) + "（六段式只允许：" + "、".join(six) + "）")

    if set(six) <= set(clean):
        order = six
    elif set(fl2) <= set(clean):
        order = fl2
    else:
        missing = [s for s in six if s not in clean]
        raise TaskError("六段式缺段：" + "、".join(missing) + "（缺段一律拒绝，不静默补空）")

    parts: list[str] = []
    for name in order:
        body = clean[name].strip()
        if not body and name != "non_diegetic_music":
            raise TaskError(f"段内容为空：{name}（空段会让模型静默丢内容，请填写或保留原值）")
        if name == "non_diegetic_music" and not body:
            body = "none"
        parts.append(f"{name}: {body}")
    return "\n".join(parts) + "\n"


_D_TAG_RE = re.compile(r"<d>(.*?)</d>", re.S)
_NORM_STRIP_RE = re.compile(
    r"[\s\u3000，。！？、；：“”‘’（）《》〈〉【】…—～·,.!?;:'\"()\[\]{}<>|/\\*#`~^&+=@$-]+"
)


def _replace_section_body(prompt: str, name: str, new_body: str) -> str:
    """
    只替换某一个段的内容，其它段原样保留。

    为什么不用 compose_prompt_from_sections 重拼：那会规范化整份提示词
    （丢注释、丢原文里的小差异），而"改一句台词"不该顺手改动别的段。
    """
    try:
        mod = import_module("shots")
        marks = list(mod.prompt_headers(prompt))
    except Exception:  # noqa: BLE001
        return prompt
    for i, (pos, nm) in enumerate(marks):
        if nm != name:
            continue
        colon = prompt.find(":", pos)
        if colon < 0:
            return prompt
        end = marks[i + 1][0] if i + 1 < len(marks) else len(prompt)
        tail = prompt[end:]
        if tail and not tail.endswith("\n"):
            tail += "\n"
        return prompt[: colon + 1] + " " + new_body.strip() + "\n" + tail
    return prompt


def _replace_speech_in_prompt(prompt: str, old: str, new: str) -> tuple[str, bool]:
    """把某个 <d> 里的旧台词逐字换成新台词（保留 <d> 里的 [语言] 等前缀）。"""
    if not old or old == new:
        return prompt, False
    state = {"done": False}

    def _sub(m: re.Match) -> str:
        if state["done"] or old not in m.group(1):
            return m.group(0)
        state["done"] = True
        return "<d>" + m.group(1).replace(old, new) + "</d>"

    out = _D_TAG_RE.sub(_sub, prompt)
    return out, state["done"]


def _sync_speech_into_prompt(item: dict, changed: list[str], old: dict) -> tuple[str, list[str], list[str]]:
    """
    把改过的台词/旁白同步进提示词的 <d>。返回 (新 prompt, 已同步说明, 告警)。

    ★ 为什么必须做（P1 实测发现的坑）：
      ① 渲染时 H3 念的是提示词 <d> 里的原文，不是表里的 dialogue 字段；
      ② 镜头指纹 `shot_fingerprint(prompt, chars, refs, params, frames, seed)` **不含 dialogue**，
         所以"只改字段不同步提示词"既不会生效、也不会变待重渲 ——
         用户会以为改了、其实永远渲的是旧台词。这属于本项目最想消灭的静默不一致。
      同步是确定性的：旧句在 <d> 里 → 逐字替换；旧句本来就不在 → 按 plan 的说话人推断追加一句。
    """
    prompt = str(item.get("prompt") or "")
    synced: list[str] = []
    warns: list[str] = []
    row = {
        "chars": [str(c) for c in _as_list(item.get("chars"))],
        "action": str(item.get("action") or ""),
        "dialogue": "",
        "narration": "",
    }
    append_needed = False
    for label, key in (("台词", "dialogue"), ("旁白", "narration")):
        if key not in changed:
            continue
        o = str(old.get(key) or "")
        n = str(item.get(key) or "")
        if o == n:
            continue
        if o and n:
            prompt, done = _replace_speech_in_prompt(prompt, o, n)
            if done:
                synced.append(f"{label}已同步进提示词 <d>")
                continue
            warns.append(f"{label}的旧句没出现在提示词里（本来就与字段不一致），改为追加新句")
        if n:
            row[key] = n
            append_needed = True
        else:
            warns.append(f"{label}已清空，但提示词里还留着旧句（渲染时仍会念）；请点『重写提示词』")
    if append_needed:
        try:
            pl = import_module("plan")
            payload = {"detailed_description": _section_body_of(prompt, "detailed_description")}
            reps = pl._repair_missing_lines(row, payload)
            new_dd = str(payload.get("detailed_description") or "")
            if reps and new_dd.strip():
                prompt = _replace_section_body(prompt, "detailed_description", new_dd)
                synced.append("；".join(str(r) for r in reps))
            else:
                warns.append("新台词没能自动写进提示词，请点『重写提示词』")
        except Exception as e:  # noqa: BLE001 - 同步失败不能阻塞保存，但要如实告警
            warns.append(f"自动同步台词失败（{type(e).__name__}: {e}），请点『重写提示词』")
    return prompt, synced, warns


def _section_body_of(prompt: str, name: str) -> str:
    try:
        mod = import_module("shots")
        return str(mod.section_bodies(prompt).get(name) or "")
    except Exception:  # noqa: BLE001
        return ""


# ---- P2 服装：costume 变更时确定性重建提示词（契约 ★R3.2） --------------------

def _costume_shot_proxy(item: dict) -> SimpleNamespace:
    """
    `costumes.rebuild_prompt(shot, cards, data)` 只按属性取值，而本层编辑的是**原始 dict**
    （为了不丢 action/costume 等未知键）。所以这里做一个只读代理对象。

    为什么不直接把 dict 传进去：它的实现是 `getattr(shot, "prompt")`，
    dict 会全部落到默认值 → 重建出一份空提示词（静默劣化，比报错更糟）。
    """
    return SimpleNamespace(
        id=str(item.get("id") or ""),
        prompt=str(item.get("prompt") or ""),
        chars=[str(c) for c in _as_list(item.get("chars"))],
        camera=str(item.get("camera") or ""),
        costume=dict(item.get("costume")) if isinstance(item.get("costume"), dict) else {},
    )


def _costume_config_warnings(data: dict, item: dict) -> list[str]:
    """
    服装配置的"会不会白改"检查（纯告警，不阻塞）。

    为什么要有：`costumes.resolve()` 在"变体不存在"或"角色没配置"时会静默回落默认变体
    —— 用户点了换装、保存成功、什么都没变，正是本项目最想消灭的静默失效。
    """
    cost = item.get("costume") if isinstance(item.get("costume"), dict) else {}
    if not cost:
        return []
    try:
        cs = import_module("costumes")
    except ModuleNotReady:
        return [f"vm/costumes.py 未就绪，服装字段暂时不生效（已原样保存：{cost}）"]
    chars = {str(c) for c in _as_list(item.get("chars"))}
    out: list[str] = []
    for name, want in cost.items():
        name = str(name)
        want = str(want)
        if chars and name not in chars:
            out.append(f"服装里的「{name}」不在本镜 chars（{'、'.join(sorted(chars))}）中，这一条会被忽略")
            continue
        vs = cs.variants_of(data, name)
        if not vs:
            out.append(
                f"「{name}」在 costumes.json 里还没有服装配置，{name}={want} 不会写进提示词"
                "（先给它建一个 default 变体，再加别的变体）"
            )
            continue
        ids = [str(v.get("id") or "") for v in vs]
        if want not in ids:
            out.append(
                f"「{name}」没有变体 {want!r}（现有：{'、'.join(ids)}）→ 已回落到默认变体，换装不会生效"
            )
    return out


def _rebuild_prompt_for_costume(item: dict, proj: Project) -> tuple[str | None, list[str]]:
    """
    按 `shot.costume` **确定性重建**整条提示词（契约 ★R3.2）。返回 (新 prompt 或 None, 告警)。

    ★ 为什么必须做：`shot_fingerprint` 只含 prompt/chars/refs/渲染参数/frames/seed。
    只改 `costume` 字段不动 prompt → 指纹不变 → 状态仍是 current → 不会重渲 →
    `subject_definitions` 里仍是旧衣服、`retention_analysis` 仍写"服装必须与图一致" →
    **换装永远不生效**。（与 R3.1 的台词/运镜是同一类静默失效。）

    重建只改 `subject_definitions` 与 `retention_analysis` 两段（其余四段一字不动），
    走 `plan._assemble_prompt()`，**不调 LLM**。

    失败一律**降级**：返回 (None, 告警)，调用方保留原 prompt、照常保存字段 ——
    不能因为"重建不了"就把用户的整个保存拒掉（那会让人连改台词都做不了）。
    """
    try:
        cs = import_module("costumes")
        pl = import_module("plan")
        cards = pl.load_char_cards(proj)  # 注意：必须用它（它带 portrait_prompt 原文）
        data = cs.load(proj)
    except ModuleNotReady as e:
        return None, [f"按服装重建提示词需要 vm/costumes.py：{e}"]
    except Exception as e:  # noqa: BLE001
        return None, [f"载入角色卡/服装配置失败（{type(e).__name__}: {e}），已保留原提示词"]

    warns = _costume_config_warnings(data, item)
    try:
        new_prompt = cs.rebuild_prompt(_costume_shot_proxy(item), cards, data)
    except ValueError as e:
        # 提示词缺段（例如旧表/手改过）→ 契约要求降级，不拒绝保存
        warns.append(f"提示词结构不完整，服装没能写进去（{e}）；已保留原提示词，请点『重写提示词』")
        return None, warns
    except Exception as e:  # noqa: BLE001
        warns.append(f"按服装重建提示词失败（{type(e).__name__}: {e}）；已保留原提示词")
        return None, warns
    return str(new_prompt), warns


def _sync_camera_into_prompt(prompt: str, camera: str) -> tuple[str, bool, list[str]]:
    """
    把 camera 同步进提示词的 `CAMERA DISCIPLINE:` 行（措辞取自 plan._assemble_prompt，唯一权威）。

    同样是因为指纹只认 prompt：不同步的话"改了运镜"既不会生效也不会变待重渲。
    没有纪律行时按 plan 的"纪律前移"规则插在 detailed_description 段标题紧前面。

    两种不同步的情况都**只告警、不改提示词**（绝不静默删掉运镜纪律）：
      · plan 认不出这个运镜写法（没有英文括号短语、也没有 推/拉/摇/移/固定 这类关键词）
      · 提示词里没有 detailed_description 段（非标准结构）
    """
    try:
        pl = import_module("plan")
        ref = pl._assemble_prompt(
            chars=[], cards={}, camera=str(camera or ""),
            detailed_description="x", overall_soundscape="x", non_diegetic_music="none",
        )
    except Exception as e:  # noqa: BLE001
        return prompt, False, [f"运镜没能自动同步进提示词（{type(e).__name__}），请点『重写提示词』"]
    # 注意：chars=[] 时 _assemble_prompt 的 retention_analysis 只有一行，
    # 纪律句会跟在 "retention_analysis: " 后面**同一行**里 —— 按行首前缀匹配会漏掉它，
    # 所以这里用子串定位（真实提示词里它通常独占一行）。
    m = re.search(r"CAMERA DISCIPLINE:[^\n]*", ref)
    newline = m.group(0).strip() if m else ""
    if not newline:
        return prompt, False, [
            f"运镜「{camera}」plan 认不出英文运镜短语（既不认括号里的英文，也没有 推/拉/摇/移/跟/固定 这类关键词），"
            "提示词没有改 → 这一镜不会变待重渲。请写成「横移（Truck, medium amplitude, slow speed）」这种形式，"
            "或点『重写提示词』"
        ]
    lines = prompt.split("\n")
    idx = next((i for i, l in enumerate(lines) if l.startswith("CAMERA DISCIPLINE:")), None)
    if idx is not None and lines[idx].strip() != newline:
        lines[idx] = newline
        return "\n".join(lines), True, []
    if idx is not None:
        return prompt, False, []
    j = next((i for i, l in enumerate(lines) if l.startswith("detailed_description:")), None)
    if j is None:
        return prompt, False, ["提示词里没有 detailed_description 段，运镜纪律没能同步（请点『重写提示词』）"]
    lines.insert(j, newline)
    return "\n".join(lines), True, []


def _speech_norm(text: str) -> str:
    """去掉标点/空白/说话人前缀，只留字（与 plan._norm_speech 同口径）。"""
    try:
        pl = import_module("plan")
        return str(pl._norm_speech(text))
    except Exception:  # noqa: BLE001 - plan 未就绪时用本地等价实现
        return _NORM_STRIP_RE.sub("", str(text or ""))


def prompt_mismatch(item: dict) -> list[str]:
    """
    提示词里的台词/旁白是否与字段逐字一致（人话告警，不阻塞）。

    为什么要有：拆镜/合并/插入都会让字段与提示词正文<b>劈叉</b>
    （正文还是旧台词）。渲染时 H3 念的是正文里的 <d>，不是表里的字段 ——
    不提示的话用户会以为改了台词就生效了。

    两档检查：
      ① 字段没出现在任何 <d> 里 → 直接告警（旧词/不念）
      ② 字段所在的 <d> 里还有**额外**的字（典型：拆镜后两个镜头都还挂着原句）
         → 只做子串匹配会漏掉这种，必须比"归一化后的整段"，否则用户以为拆成功了两镜会各念一整句
    """
    prompt = str(item.get("prompt") or "")
    if not prompt.strip():
        return ["提示词为空（该镜渲染不出来，请点『重写提示词』）"]
    tags = _D_TAG_RE.findall(prompt)
    out: list[str] = []
    for label, text in (("台词", item.get("dialogue") or ""), ("旁白", item.get("narration") or "")):
        t = str(text or "")
        if not t:
            continue
        if not any(t in tag for tag in tags):
            out.append(f"{label}「{t}」没有逐字出现在提示词的 <d> 标签里（渲染时会念旧词或不念）")
            continue
        tn = _speech_norm(t)
        extra = ""
        for tag in tags:
            if t not in tag:
                continue
            gn = _speech_norm(tag)
            if tn and tn in gn and gn != tn:
                extra = gn.replace(tn, "", 1)
                break
        if extra.strip():
            out.append(
                f"{label}所在的 <d> 里还有额外的字（「{extra[:30]}」），渲染时会被一起念出来；"
                "拆镜/改台词后请点『重写提示词』"
            )
    return out


# ---- 单镜详情 -------------------------------------------------------------

def _entry_flag(proj, shot_id: str, flag: str):
    """读 manifest 里某镜头的标记位。读不到返回 False / 0 / ""（按字段类型）。"""
    try:
        e = proj.manifest.shots.get(str(shot_id))
        if e is None:
            return 0 if flag == "locked_at" else ("" if flag == "locked_by" else False)
        return getattr(e, flag, 0 if flag == "locked_at" else ("" if flag == "locked_by" else False))
    except Exception:
        return 0 if flag == "locked_at" else ("" if flag == "locked_by" else False)


def set_shot_flag(
    project: str | os.PathLike[str],
    shot_id: str,
    flag: str,
    value: bool,
    root: Path | None = None,
    by: str = "user",
) -> dict:
    """
    E1：置位 / 清除 locked | selected | favorite。

    为什么需要锁定：在此之前**任何产物都能被覆盖** —— 改定妆照、批量重渲、重跑渲染，
    都可能把一个你已经满意的镜头盖掉，而且没有任何提示。
    锁定的镜头在渲染层被跳过（`gen.render_shot` 检查 `manifest.is_locked`），
    只有显式 force 才能动它。

    **锁定同时记录时间与操作者**：那是排查"为什么这个镜头改不动"的唯一线索。

    ★ 无产物镜头 = **优雅跳过**，不是 400（lead 裁决，2026-09-25）：
    单镜锁定 UI 本来就禁用没渲的镜头，真正踩到这坑的是**批量**（勾选里混着没渲的）——
    抛错会把整批炸掉。现在单镜返回 200 + locked:false + 人话 note，
    批量调用方按响应里的 `skipped` 列表汇总，个别项永远不拖垮整批。
    """
    if flag not in ("locked", "selected", "favorite"):
        raise TaskError(f"未知标记：{flag}（只支持 locked / selected / favorite）")
    if not SHOT_ID_RE.match(str(shot_id or "")):
        raise TaskError(f"非法镜头号：{shot_id!r}")
    pdir = resolve_project(project, root)
    proj = Project(pdir)
    manifest = proj.manifest
    if str(shot_id) not in manifest.shots:
        return {
            "shot": str(shot_id),
            "locked": False, "locked_at": 0, "locked_by": "",
            "selected": False, "favorite": False,
            "skipped": [str(shot_id)],
            "note": "无产物记录，已跳过",
            "message": f"{shot_id} 无产物记录，已跳过（先渲染一次才有可锁的版本）",
        }
    manifest.set_flag(str(shot_id), flag, bool(value), by=by)
    e = manifest.shots[str(shot_id)]
    return {
        "shot": str(shot_id),
        "locked": e.locked,
        "locked_at": e.locked_at,
        "locked_by": e.locked_by,
        "selected": e.selected,
        "favorite": e.favorite,
        "skipped": [],
        "message": (
            f"已{'锁定' if e.locked else '解锁'} {shot_id}"
            + (f"（{e.locked_by}）" if e.locked else "")
            if flag == "locked"
            else f"{shot_id} 的 {flag} = {bool(value)}"
        ),
    }


def _backup_before_edit(proj, filename: str, why: str) -> str:
    """
    编辑类接口的覆盖前备份（与 plan 覆盖镜头表同一策略）。

    为什么必须做：我**刚刚亲手踩过** —— 用真实项目测编辑接口时把 `S1.description`
    写成了测试串，而 `scenes.json` **没有备份**，只能靠记忆里的原文手工恢复。
    `plan` 覆盖镜头表时我已经加了备份，编辑接口是后来加的，漏了。
    """
    src = Path(proj.root) / filename
    if not src.is_file():
        return ""
    try:
        bak_dir = Path(proj.root) / "_backup"
        bak_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        bak = bak_dir / f"{Path(filename).stem}.{stamp}.json"
        shutil.copy2(src, bak)
        return str(bak.relative_to(Path(proj.root)))
    except OSError:
        return ""


def update_scene(project: str | os.PathLike[str], sid: str, patch: dict,
                 root: Path | None = None) -> dict:
    """
    改场景实体（name / location / time_of_day / lighting / atmosphere / description）。

    ⚠️ **说清楚影响面**（改完 UI 要提示，不能让用户以为改了就生效）：
      · `description` 是**拆镜时**逐字注入每镜提示词的 —— 改它**不会**动已有的
        `shots/*.json`（那些是当时写好的）。要让改动生效得**重跑 plan**，而重跑会让
        相关镜头的指纹失配 → 变「需重渲」。
      · 但它**立即**影响**抽卡出的概念图**（出图提示词就是从 description 来的）。
    """
    from vm import plan as P
    from vm.state import Project
    pdir = resolve_project(project, root)
    proj = Project(pdir)
    scenes = P.load_scenes(proj)
    sc = scenes.get(sid)
    if sc is None:
        raise TaskError(f"场景 {sid} 不在 scenes.json 里")
    allowed = ("name", "location", "time_of_day", "lighting", "atmosphere", "description")
    changed = []
    for k in allowed:
        if k in patch:
            v = str(patch[k] or "").strip()
            if getattr(sc, k) != v:
                setattr(sc, k, v)
                changed.append(k)
    if not changed:
        return {"scene": sid, "changed": [], "message": "没有变化"}
    bak = _backup_before_edit(proj, "scenes.json", "编辑场景前")
    P.save_scenes(proj, list(scenes.values()))
    return {
        "scene": sid, "changed": changed, "backup": bak,
        "message": f"已保存场景 {sid}（改了 {'、'.join(changed)}）",
        "hint": "description 影响后续拆镜与出图；已生成的镜头提示词不受影响，"
                "要让锚定生效需重跑「拆镜」",
    }


def update_prop(project: str | os.PathLike[str], pid: str, patch: dict,
                root: Path | None = None) -> dict:
    """改道具实体（name / owner / description）。影响面同 update_scene。"""
    from vm import plan as P
    from vm.state import Project
    pdir = resolve_project(project, root)
    proj = Project(pdir)
    props = P.load_props(proj)
    pr = props.get(pid)
    if pr is None:
        raise TaskError(f"道具 {pid} 不在 props.json 里")
    changed = []
    for k in ("name", "owner", "description"):
        if k in patch:
            v = str(patch[k] or "").strip()
            if getattr(pr, k) != v:
                setattr(pr, k, v)
                changed.append(k)
    if "inferred" in patch:
        pr.inferred = bool(patch["inferred"])
        changed.append("inferred")
    if not changed:
        return {"prop": pid, "changed": [], "message": "没有变化"}
    bak = _backup_before_edit(proj, "props.json", "编辑道具前")
    P.save_props(proj, list(props.values()))
    return {
        "prop": pid, "changed": changed, "backup": bak,
        "message": f"已保存道具 {pid}（改了 {'、'.join(changed)}）",
        "hint": "description 影响后续拆镜与出图；已生成的镜头提示词不受影响",
    }


# ── 清空 / 重置 ─────────────────────────────────────────────────────────────
# 三档作用域，命名按"重置到哪一步"来 —— 用户想的是"我要重来哪一段"，不是目录名。
RESET_SCOPES: dict[str, dict] = {
    # 只删成片与视频片段 → 重渲
    "clips": {
        "label": "只清视频片段（保留镜头表，重渲即可）",
        "dirs": ("clips", "final"),
        "files": (),
    },
    # 删镜头之后的全部产物 → 重拆镜
    "shots": {
        "label": "清镜头表之后的一切（角色/场景/道具/分镜图都保留）",
        "dirs": ("clips", "final", "shots", "storyboard"),
        "files": ("costumes.json",),
    },
    # 除了小说正文与配置，全清 → 从零开始
    "all": {
        "label": "全部清空（只保留 novel/ 正文与 project.json 配置）",
        # ⚠️ **绝不能把 _backup 写进这里**。我犯过这个错（2026-09-24）：
        #    备份被写进 `_backup/reset-…/`，而 `_backup` 又在删除列表里 ——
        #    备份创建完立刻被同一次操作删掉，"安全网"把自己拆了，
        #    用户的西游记/雨夜地铁元数据因此丢失。见下方 RESET_BACKUP_ROOT。
        "dirs": ("clips", "final", "shots", "storyboard", "refs", "assets",
                 "scripts", "state", "prompts"),
        "files": ("scenes.json", "props.json", "costumes.json"),
    },
}

# **永不删除**的东西。放在这里不是"顺便"，而是**安全边界**：
# `novel/` 是用户唯一不可再生的输入（小说原文），`project.json` 是配置。
# 任何 reset 都不许碰它们 —— 删了就是真的没了。
RESET_KEEP = ("novel", "project.json")

# 备份根目录在 `projects/` 下、**各项目之外**。
# 为什么必须在外面：只要它在项目目录内，就总有一天会被某个删除作用域扫到
# （我第一版就是这么栽的）。放外面 = 结构上不可能被自己删掉。
RESET_BACKUP_ROOT = "_backups"


def reset_preview(project: str | os.PathLike[str], scope: str,
                  root: Path | None = None) -> dict:
    """**先算清要删什么、多大、备份什么**。UI 拿它把清单摆给用户看，而不是笼统问"确定吗"。"""
    pdir = Path(resolve_project(project, root))
    if scope not in RESET_SCOPES:
        raise TaskError(f"未知的清理范围：{scope}（可选 {', '.join(RESET_SCOPES)}）")
    spec = RESET_SCOPES[scope]

    targets: list[dict] = []
    total = 0
    for name in spec["dirs"]:
        d = pdir / name
        if not d.is_dir():
            continue
        n = sum(1 for f in d.rglob("*") if f.is_file())
        sz = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
        targets.append({"path": name + "/", "kind": "dir", "files": n, "bytes": sz})
        total += sz
    for name in spec["files"]:
        f = pdir / name
        if f.is_file():
            sz = f.stat().st_size
            targets.append({"path": name, "kind": "file", "files": 1, "bytes": sz})
            total += sz

    keeps = []
    for name in RESET_KEEP:
        f = pdir / name
        if f.exists():
            sz = sum(x.stat().st_size for x in f.rglob("*") if x.is_file()) if f.is_dir() else f.stat().st_size
            keeps.append({"path": name, "bytes": sz})

    return {
        "project": pdir.name, "scope": scope,
        "label": spec["label"],
        "targets": targets,
        "total_bytes": total,
        "keep": keeps,
        "requires_confirm": pdir.name,   # UI 要求用户**手输这个名字**
    }


def reset_project(project: str | os.PathLike[str], scope: str, *,
                  confirm: str = "", root: Path | None = None,
                  backup: bool = True) -> dict:
    """
    清空项目产物。**不可逆**，所以：

      1. 必须 `confirm == 项目名`（防止误点 —— 手输名字这个动作本身就是确认）
      2. 删之前**自动备份元数据**（shots/ scenes/ props/ costumes/ prompts/scripts）
         —— 视频文件太大不备份，但"拆镜结果"是最贵的（LLM 跑出来的），必须留
      3. **绝不碰 `novel/` 与 `project.json`**（见 RESET_KEEP）
      4. 如果有任务在跑，直接拒绝 —— 边跑边删会产出半个状态
    """
    import shutil as _sh

    pdir = Path(resolve_project(project, root))
    if scope not in RESET_SCOPES:
        raise TaskError(f"未知的清理范围：{scope}（可选 {', '.join(RESET_SCOPES)}）")
    if confirm.strip() != pdir.name:
        raise TaskError(
            f"确认名不匹配：需要手输「{pdir.name}」，收到「{confirm}」。"
            "这是不可逆操作，所以要求手输项目名。"
        )
    cur = read_task(pdir)
    if task_running(cur):
        raise TaskError(
            f"项目「{pdir.name}」有任务在跑（stage={cur.get('stage')}），"
            "请先停止再清空 —— 边跑边删会留下半个状态。"
        )

    spec = RESET_SCOPES[scope]
    pre = reset_preview(pdir, scope, root)
    removed: list[str] = []
    freed = 0

    # ① 元数据备份（只备份"重跑要花钱"的东西）
    bak_dir = ""
    if backup:
        meta = [("shots", "*.json"), ("prompts", "*.txt"), ("scripts", "*.json")]
        top = ["scenes.json", "props.json", "costumes.json"]
        bd = pdir.parent / RESET_BACKUP_ROOT / pdir.name / f"reset-{time.strftime('%Y%m%d-%H%M%S')}-{scope}"
        try:
            bd.mkdir(parents=True, exist_ok=True)
            for dname, pat in meta:
                d = pdir / dname
                if d.is_dir():
                    for f in d.glob(pat):
                        if f.is_file():
                            _sh.copy2(f, bd / f"{dname}__{f.name}")
            for fname in top:
                f = pdir / fname
                if f.is_file():
                    _sh.copy2(f, bd / fname)
            bak_dir = str(bd)
        except OSError as e:
            raise TaskError(f"备份失败，已中止清空（宁可不删也不裸删）：{e}") from e

    # ② 删
    for name in list(spec["dirs"]):
        d = pdir / name
        if d.is_dir():
            freed += sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            _sh.rmtree(d, ignore_errors=True)
            removed.append(name + "/")
    for name in list(spec["files"]):
        f = pdir / name
        if f.is_file():
            freed += f.stat().st_size
            f.unlink()
            removed.append(name)

    # ③ 清空后把 task.json / queue.json 也归零（否则 UI 还显示旧任务）
    try:
        tj = pdir / "state" / "task.json"
        if tj.is_file():
            tj.unlink()
    except OSError:
        pass

    # ★ 自检：备份必须**真的还在**。这条检查是因为我漏过一次 ——
    #   备份写在自己要删的目录里，删完就没了，而返回值照样报"已备份"。
    #   一道通向不了人的告警等于没有告警；一个自己会消失的备份等于没有备份。
    if backup:
        if not bak_dir or not Path(bak_dir).is_dir() or not any(Path(bak_dir).iterdir()):
            raise TaskError(
                f"备份校验失败：{bak_dir!r} 在清空后不存在或为空。"
                "数据已被删除，请**立即停止操作**并检查 RESET_BACKUP_ROOT 配置。"
            )

    return {
        "project": pdir.name, "scope": scope, "label": spec["label"],
        "removed": removed, "freed_bytes": freed,
        "backup": bak_dir,
        "kept": [k["path"] for k in pre["keep"]],
        "message": f"已清空「{pdir.name}」的 {len(removed)} 项（{freed / 1048576:.1f} MB）"
                   + (f"；元数据已备份到 {bak_dir}" if bak_dir else ""),
    }


def episodes(proj) -> list[dict]:
    """
    列出 final/ 下**全部**成片（EP01、EP02…）。

    ★ 为什么需要：我做了"逐章出集"（chapter01→EP01、chapter02→EP02），
    但**忘了改读它的人** —— 三处代码都写死 `final_dir / "EP01.mp4"`，
    于是两章的项目在界面上**只显示一集**，EP02 明明在磁盘上却看不见。
    "改了写的人、没改读的人" = 产物存在但通向不了人。
    """
    out: list[dict] = []
    d = Path(proj.final_dir)
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.mp4")):
        try:
            st = f.stat()
        except OSError:
            continue
        srt = f.with_suffix(".srt")
        out.append({
            "name": f.name,
            "stem": f.stem,
            "size": st.st_size,
            "mtime": int(st.st_mtime),
            "srt": srt.name if srt.is_file() else "",
            "url": f"/view?project={Path(proj.root).name}&kind=final&file={f.name}",
        })
    return out


def assets_table(project: str | os.PathLike[str], root: Path | None = None) -> dict:
    """场景/道具的概念图候选总览（给控制台用）。"""
    from vm import assets as A
    return A.list_assets(project, root)


def asset_gen(project: str | os.PathLike[str], kind: str, aid: str, n: int = 2,
              root: Path | None = None) -> dict:
    """为场景/道具出 n 张候选概念图。"""
    from vm import assets as A
    from vm.state import Project
    pdir = resolve_project(project, root)
    made = A.gen_candidates(Project(pdir), kind, aid, n=int(n))
    return {"kind": kind, "id": aid, "made": len(made),
            "files": [c.file for c in made],
            "message": f"{kind} {aid} 新出 {len(made)} 张候选"}


def asset_upload(project: str | os.PathLike[str], kind: str, aid: str,
                 filename: str, raw: bytes, root: Path | None = None) -> dict:
    """上传自有图作为场景/道具的候选。"""
    from vm import assets as A
    from vm.state import Project
    pdir = resolve_project(project, root)
    c = A.add_upload(Project(pdir), kind, aid, filename, raw)
    return {"kind": kind, "id": aid, "file": c.file, "size": c.size,
            "message": f"已上传 {kind} {aid} → {c.file}（{c.size // 1024} KB，点图采纳）"}


def asset_adopt(project: str | os.PathLike[str], kind: str, aid: str, file: str,
                root: Path | None = None) -> dict:
    """采纳一张候选作为定稿。"""
    from vm import assets as A
    from vm.state import Project
    pdir = resolve_project(project, root)
    rec = A.adopt(Project(pdir), kind, aid, file)
    return {"kind": kind, "id": aid, "adopted": rec.adopted,
            "message": f"已采纳 {kind} {aid} → {rec.adopted}"}


def props_table(project: str | os.PathLike[str], root: Path | None = None) -> dict:
    """
    关键道具实体 + 每镜归属（给控制台「道具」tab 用）。

    和场景同理：道具外观若每镜各写各的，就会出现"金箍棒每镜长得不一样"。
    `plan.py` 的 `_extract_props()` 抽出关键道具 → 一段权威英文外观，
    用到该道具的镜头注入**同一段文字**。
    """
    pdir = resolve_project(project, root)
    proj = Project(pdir)
    out: list[dict] = []
    pp = Path(pdir) / "props.json"
    if pp.is_file():
        try:
            d = json.loads(pp.read_text(encoding="utf-8"))
            out = [x for x in (d.get("props") or []) if isinstance(x, dict)]
        except (json.JSONDecodeError, OSError):
            out = []
    try:
        rows = _raw_shots(proj.shots_dir)[0]
    except Exception:
        rows = []
    counts: dict[str, int] = {}
    for r in rows if isinstance(rows, list) else []:
        for pid in ((r or {}).get("prop_ids") or []):
            counts[str(pid)] = counts.get(str(pid), 0) + 1
    for x in out:
        x["shot_count"] = counts.get(str(x.get("id")), 0)
    return {
        "project": str(pdir.name),
        "props": out,
        "assigned": sum(counts.values()),
        "total_shots": len(rows) if isinstance(rows, list) else 0,
        "shots_with_props": sum(1 for r in (rows if isinstance(rows, list) else []) if (r or {}).get("prop_ids")),
        "file": str(pp),
    }


def storyboard_table(project: str | os.PathLike[str], root: Path | None = None) -> dict:
    """
    分镜图（渲染前审片用的静态关键帧）+ 每镜状态。

    为什么单独一层：52 镜渲一遍视频是 **35 分钟 GPU**；先出 52 张静态图只要 **约 9 分钟**
    （实测单图 10s vs 单段视频 40s）。**在花钱渲视频前先看画面**，直接压废片率 ——
    这也是行业数据里"制作只占 7.5%、废片率才是杀手"的对应抓手。

    状态来自 `vm/storyboard` 的指纹索引：产物在 + 指纹一致 = current，
    提示词改了 = stale（需重出），没有 = missing。
    """
    pdir = resolve_project(project, root)
    proj = Project(pdir)
    out: dict = {"project": str(pdir.name), "shots": [], "counts": {}}
    try:
        from vm import storyboard as SB
        from vm.shots import load_shots_dir as _load_dir
        cfg = SB.QIConfig()
        index = SB.SBIndex.load(Path(proj.state_dir) / SB.INDEX_FILENAME)
        shots = _load_dir(proj.shots_dir)
        rows = SB.list_status(shots, cfg, index, proj, negative="", seed=None,
                              with_subject_defs=False)
        for r in rows:
            out["shots"].append(r if isinstance(r, dict) else getattr(r, "__dict__", {}))
        out["model"] = {"unet": cfg.unet_name, "clip": cfg.clip_name, "vae": cfg.vae_name,
                        "width": cfg.width, "height": cfg.height, "steps": cfg.steps, "cfg": cfg.cfg}
        st = [str(x.get("status") or "") for x in out["shots"]]
        out["counts"] = {k: st.count(k) for k in sorted(set(st)) if k}
    except Exception as e:  # 索引损坏/模块未就绪都不该让控制台挂掉
        out["error"] = f"{type(e).__name__}: {e}"
    out["dir"] = str(Path(pdir) / "storyboard")
    return out


def completeness_table(project: str | os.PathLike[str], root: Path | None = None) -> dict:
    """
    B3 逐镜 7 维完备性矩阵（零 LLM、纯 CPU、只读）。

    比"第 5 镜有问题"信息密度高得多 —— 能说「第 5 镜的**角色参考图还只是 draft**」。
    维度按**我们的真实管线**定义（不是照抄调研原文，那样会有 3 个恒定空的列）。
    """
    pdir = resolve_project(project, root)
    from vm.audit import completeness as C
    m = C.build(pdir)
    m["text"] = C.render(m)
    return m


def script_table(project: str | os.PathLike[str], root: Path | None = None) -> dict:
    """
    剧本层（无损结构化视图）+ 逐字校验结果。

    **铁律：无损。** 调研负面清单明确写着「storyforge 的摘要式链路」是架构级错误
    （原文第 1 步就丢，对白必丢）。所以剧本层**不替代小说作为对白来源**，
    只做旁路视图：每句对白带 `src_span` 指回原文，`verify()` 做确定性逐字校验。
    拆镜仍直接从小说来（B5 那条路径不变）。丢了剧本层，流水线照跑。
    """
    pdir = resolve_project(project, root)
    proj = Project(pdir)
    from vm import script as SB
    sdir = Path(pdir) / SB.SCRIPT_DIRNAME
    out: list[dict] = []
    novels = [p for p in sorted(proj.novel_dir.glob("*.md")) + sorted(proj.novel_dir.glob("*.txt"))
              if not p.name.startswith(".")]
    for i, n in enumerate(novels, 1):
        try:
            from vm import plan as P
            cfg = json.loads((Path(pdir) / "project.json").read_text(encoding="utf-8"))
            no = P.chapter_number(n, cfg)
        except Exception:
            no = i
        sc = SB.load_script(proj, no)
        if sc is None:
            out.append({"no": no, "title": n.stem, "has_script": False, "beats": 0, "lines": 0,
                        "checks": {}, "text": ""})
            continue
        out.append({
            "no": no, "title": sc.title, "has_script": True,
            "beats": sc.n_beats, "lines": sc.n_lines,
            "checks": sc.span_checks or {},
            "text": SB.render(sc),
            "file": str(SB.script_path(proj, no)),
        })
    return {"project": str(pdir.name), "chapters": out, "dir": str(sdir),
            "gate_passed": all((c.get("checks") or {}).get("gate_passed", True)
                               for c in out if c["has_script"])}


def chapter_table(project: str | os.PathLike[str], root: Path | None = None) -> dict:
    """
    集/章清单 + 每章状态（多集管理，2026-09-24）。

    背景：原来 `stage_plan` 写死 `chapters[0]` 并提示"其余章节请分项目或手改镜头表"，
    也就是**多集从来不支持**。现在改成遍历 novel/ 下全部章节，每章一个镜头表文件
    （`shots/chapterNN.json`），`load_shots_dir` 按文件名顺序合并，
    所以逐镜渲染/质检/合成不需要改动就能跑多集。

    **章号从镜头 id 的首段来**（`1-2-03` → 第 1 章），这样即使镜头表被手工改名也对得上。
    """
    pdir = resolve_project(project, root)
    proj = Project(pdir)
    # novel/ 下的章节文件（按文件名排序 = 拆镜顺序）
    novels = [p for p in sorted(proj.novel_dir.glob("*.md")) + sorted(proj.novel_dir.glob("*.txt"))
              if not p.name.startswith(".")]
    # shots/ 下的镜头表
    shot_files = [p for p in sorted(proj.shots_dir.glob("*.json"))
                  if not p.name.startswith(".") and not p.name.endswith(".tmp")]
    # 每章镜数从**镜头 id 首段**统计（比文件名更可信）
    per_ch: dict[str, int] = {}
    try:
        rows = _raw_shots(proj.shots_dir)[0]
    except Exception:
        rows = []
    for r in rows if isinstance(rows, list) else []:
        sid = str((r or {}).get("id") or "")
        seg = sid.split("-")[0] if "-" in sid else ""
        if seg.isdigit():
            per_ch[seg] = per_ch.get(seg, 0) + 1

    out: list[dict] = []
    for i, np_ in enumerate(novels, 1):
        num = None
        m = re.search(r"第([零一二两三四五六七八九十\d]+)[章回]", np_.stem)
        if m:
            try:
                num = _cn_num(m.group(1))
            except Exception:
                num = None
        if num is None:
            num = i
        key = str(num)
        sf = proj.shots_dir / f"chapter{num:02d}.json"
        out.append({
            "index": i,
            "no": num,
            "title": np_.stem,
            "novel": str(np_),
            "novel_chars": np_.stat().st_size,
            "shots_file": str(sf) if sf.is_file() else "",
            "has_shots": sf.is_file(),
            "shots": per_ch.get(key, 0),
        })
    # 镜头表里出现了但 novel/ 里没有的章（手工加的）
    known = {str(x["no"]) for x in out}
    for k in sorted(per_ch, key=lambda x: int(x)):
        if k not in known:
            out.append({"index": len(out) + 1, "no": int(k), "title": f"第 {k} 章（无对应正文章节）",
                        "novel": "", "novel_chars": 0,
                        "shots_file": str(proj.shots_dir / f"chapter{int(k):02d}.json"),
                        "has_shots": True, "shots": per_ch[k]})
    return {
        "project": str(pdir.name),
        "chapters": out,
        "total_chapters": len(out),
        "total_shots": sum(x["shots"] for x in out),
        "shot_files": [p.name for p in shot_files],
    }


def _cn_num(s: str) -> int:
    """中文数字 → int（「一」→1、「十二」→12）。解析不出抛 ValueError。"""
    s = s.strip()
    if s.isdigit():
        return int(s)
    d = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
         "六": 6, "七": 7, "八": 8, "九": 9}
    if s in d:
        return d[s]
    if s.startswith("十"):
        return 10 + (d.get(s[1:], 0) if len(s) > 1 else 0)
    if "十" in s:
        a, _, b = s.partition("十")
        return d.get(a, 0) * 10 + (d.get(b, 0) if b else 0)
    raise ValueError(s)


def scene_table(project: str | os.PathLike[str], root: Path | None = None) -> dict:
    """
    场景实体 + 每镜归属（给控制台「场景」tab 用）。

    为什么需要：数据里的 `scene` 只是**场次号** —— 实测 52 镜被分成 47 个场次，
    "场景"这一层从来没真正建立过。2026-09-23 起由 `plan.py` 的 `_extract_scenes()`
    抽出真正的地点，同 `scene_id` 的镜头注入**同一段**英文场景描述做一致性锚定。
    """
    pdir = resolve_project(project, root)
    proj = Project(pdir)
    out: list[dict] = []
    sp = Path(pdir) / "scenes.json"
    if sp.is_file():
        try:
            d = json.loads(sp.read_text(encoding="utf-8"))
            out = [x for x in (d.get("scenes") or []) if isinstance(x, dict)]
        except (json.JSONDecodeError, OSError):
            out = []
    # 每镜归属：读**原始 dict**（scene_id 不在 Shot 的公开视图里也要能读到）
    try:
        rows = _raw_shots(proj.shots_dir)[0]
    except Exception:
        rows = []
    counts: dict[str, int] = {}
    for r in rows if isinstance(rows, list) else []:
        sid = str((r or {}).get("scene_id") or "")
        if sid:
            counts[sid] = counts.get(sid, 0) + 1
    for x in out:
        x["shot_count"] = counts.get(str(x.get("id")), 0)
    return {
        "project": str(pdir.name),
        "scenes": out,
        "assigned": sum(counts.values()),
        "total_shots": len(rows) if isinstance(rows, list) else 0,
        "file": str(sp),
    }


def shot_detail(project: str | os.PathLike[str], shot_id: str, root: Path | None = None) -> dict:
    """GET /api/shot 的全部数据：六段式全文 + 元信息 + 三态 + 质检 + 语音预算。"""
    if not SHOT_ID_RE.match(str(shot_id or "")):
        raise TaskError(f"非法镜头号：{shot_id!r}")
    pdir = resolve_project(project, root)
    proj = Project(pdir)
    docs = _load_shot_docs(proj)
    if not docs:
        raise TaskError(f"{proj.shots_dir} 下没有镜头表（*.json）")
    doc, idx = _find_in_docs(docs, shot_id)
    item = doc["items"][idx]
    refs = _shot_refs(docs)
    order_ids = [str(docs[di]["items"][ii].get("id") or "") for di, ii in refs]

    lim = _limits()
    six, fl2 = _sections_constants()
    try:
        mod = import_module("shots")
    except ModuleNotReady:
        mod = None
    prompt = str(item.get("prompt") or "")
    sections: dict[str, str] = {}
    section_order: list[str] = []
    if mod is not None and hasattr(mod, "section_bodies"):
        sections = {str(k): str(v) for k, v in mod.section_bodies(prompt).items()}
        section_order = list(sections.keys())
    if mod is not None and hasattr(mod, "prompt_headers"):
        # 段序一律以"文中出现顺序"为准（section_bodies 是 dict，顺序虽然也保序，
        # 但显式取 header 顺序更不容易被将来的实现改动带偏）
        section_order = [str(n) for _, n in mod.prompt_headers(prompt)]
    missing_sections = [s for s in six if s not in sections]
    row = None
    try:
        table = shot_table(project, root)
        row = next((r for r in table["shots"] if r["id"] == str(shot_id)), None)
    except Exception:  # noqa: BLE001 - 详情页不能因为算三态失败就打不开
        row = None
    sec = int(item.get("sec") or 0)
    speech = 0
    if mod is not None and hasattr(mod, "speech_chars"):
        try:
            speech = int(mod.speech_chars(str(item.get("dialogue") or ""), str(item.get("narration") or "")))
        except Exception:  # noqa: BLE001
            speech = 0
    cps = float(lim.get("chars_per_sec") or 4.0)
    costume = item.get("costume")
    if not isinstance(costume, dict):
        costume = {"__raw__": str(costume)} if costume else {}

    # P2：把该镜角色**可选的服装变体**一起交给 UI。
    # 为什么不只给个文本框：变体 id 写错时 `costumes.resolve()` 会静默回落默认变体
    # （换装不生效、用户却看到"保存成功"）—— 把可选项摆出来能挡掉这类静默失效。
    costume_options: dict[str, list[dict]] = {}
    costume_configured = False
    try:
        cs = import_module("costumes")
        cdata = cs.load(proj)
        for name in [str(c) for c in _as_list(item.get("chars"))]:
            vs = cs.variants_of(cdata, name)
            if vs:
                costume_configured = True
                costume_options[name] = [
                    {"id": str(v.get("id") or ""), "label": str(v.get("label") or "")} for v in vs
                ]
    except Exception:  # noqa: BLE001 - 服装模块未就绪时 UI 只是没有可选项，不影响编辑
        pass

    shot = {
        "id": str(item.get("id") or ""),
        "sec": sec,
        "chars": [str(c) for c in _as_list(item.get("chars"))],
        "seed": item.get("seed"),
        "shot_size": str(item.get("shot_size") or ""),
        "camera": str(item.get("camera") or ""),
        "dialogue": str(item.get("dialogue") or ""),
        "narration": str(item.get("narration") or ""),
        "action": str(item.get("action") or ""),
        "costume": costume,
        # E1 资产锁定/标记：三个独立布尔位（locked / selected / favorite），不是互斥状态。
        # locked 的镜头渲染层拒绝覆盖、编辑层应当拒绝改字段。
        "locked": _entry_flag(proj, shot_id, "locked"),
        "locked_at": _entry_flag(proj, shot_id, "locked_at"),
        "locked_by": _entry_flag(proj, shot_id, "locked_by"),
        "selected": _entry_flag(proj, shot_id, "selected"),
        "favorite": _entry_flag(proj, shot_id, "favorite"),
        "costume_options": costume_options,
        "costume_configured": costume_configured,
        "prompt": prompt,
        "sections": sections,
        "section_order": section_order,
        "missing_sections": missing_sections,
        "frames": (row or {}).get("frames"),
        "status": (row or {}).get("status", MISSING),
        "fp": (row or {}).get("fp", ""),
        "fp_source": (row or {}).get("fp_source", ""),
        "clip": (row or {}).get("clip", {"exists": False, "size": 0, "mtime": 0}),
        "qc": (row or {}).get("qc"),
        "file": doc["path"].name,
        "index": order_ids.index(str(shot_id)) + 1 if str(shot_id) in order_ids else 0,
        "index_in_file": idx,
        "prev_id": order_ids[order_ids.index(str(shot_id)) - 1] if str(shot_id) in order_ids and order_ids.index(str(shot_id)) > 0 else "",
        "next_id": order_ids[order_ids.index(str(shot_id)) + 1] if str(shot_id) in order_ids and order_ids.index(str(shot_id)) + 1 < len(order_ids) else "",
        "speech_chars": speech,
        "seconds_needed": round(speech / cps, 2) if cps else 0,
        "limits": {
            "dialogue_max": int(lim["dialogue_max"]),
            "narration_max": int(lim["narration_max"]),
            "sec_min": int(lim["sec_min"]),
            "sec_max": int(lim["sec_max"]),
            "chars_per_sec": cps,
        },
        "prompt_len": len(prompt),
        "prompt_mismatch": prompt_mismatch(item),
        "extra_keys": sorted(k for k in item.keys() if k not in SHOT_PATCH_FIELDS and k != "id"),
    }
    return {"project": pdir.name, "shot": shot, "total": len(order_ids), "last_edit": last_edit(project, root)}


def _shot_after(project, root, shot_id: str) -> dict:
    try:
        return shot_detail(project, shot_id, root)["shot"]
    except TaskError:
        return {}


# ---- 改字段 ---------------------------------------------------------------

def update_shot(project, shot_id: str, patch: dict, root: Path | None = None) -> dict:
    """POST /api/shot/update：改单镜字段（只写盘 + 标 stale，不触发渲染）。"""
    if not isinstance(patch, dict) or not patch:
        raise TaskError("patch 不能为空（没有要改的字段）")
    if "id" in patch:
        raise TaskError("不支持改 id（会让已有产物与指纹记录错位）；要连续编号请用「重排编号」")
    sections = patch.get("sections")
    if sections is not None and "prompt" in patch:
        raise TaskError("sections 与 prompt 不能同时给：sections 会重新拼出 prompt，同时给会有歧义")
    allowed = set(SHOT_PATCH_FIELDS) | {"sections"}
    unknown = sorted(set(patch) - allowed)
    if unknown:
        raise TaskError("不认识的字段：" + "、".join(unknown) + "（可改：" + "、".join(sorted(allowed)) + "）")

    coerced = {k: _coerce_field(k, v) for k, v in patch.items() if k != "sections"}
    new_prompt = compose_prompt_from_sections(sections) if sections is not None else None
    proj = Project(resolve_project(project, root))  # 服装重建要用它读 prompts/ 与 costumes.json

    def mutate(docs: list[dict]) -> dict:
        doc, idx = _find_in_docs(docs, shot_id)
        item = doc["items"][idx]
        old = {k: item.get(k) for k in ("dialogue", "narration", "camera")}
        changed: list[str] = []
        for k, v in coerced.items():
            # 清空服装（{}）+ 本来就没这个键 → 不要往表里塞一个空对象；
            # 重建仍然会跑（见下），这样"反复保存"不会把表越写越脏。
            if k == "costume" and not v and "costume" not in item:
                continue
            if item.get(k) != v:
                changed.append(k)
            item[k] = v
        # 单句上限（plan.DIALOGUE_MAX/NARRATION_MAX：字幕一行放不下、4 字/秒念不完）。
        # 只拦"这次改出来的超限值"：表里本来就超限的旧数据不该因为改别的字段被连坐。
        lim = _limits()
        for label, key in (("台词", "dialogue"), ("旁白", "narration")):
            if key not in changed:
                continue
            nv = str(item.get(key) or "")
            mx = int(lim["dialogue_max"] if key == "dialogue" else lim["narration_max"])
            if len(nv) > mx:
                raise TaskError(
                    f"{label} {len(nv)} 字超过单句上限 {mx} 字（字幕一行放不下、{lim['chars_per_sec']:g} 字/秒念不完）；"
                    "请用「拆分」把它拆到两镜，或改短。"
                )
        if new_prompt is not None and item.get("prompt") != new_prompt:
            changed.append("prompt（六段式重拼）")
            item["prompt"] = new_prompt
        warnings: list[str] = []
        synced: list[str] = []
        # ★ 台词/旁白/运镜不在镜头指纹里（指纹只含 prompt），必须同步进提示词，
        #   否则"改了却不变待重渲、渲染时也不生效"。详见 _sync_speech_into_prompt 的注释。
        if new_prompt is None:
            if any(k in changed for k in ("dialogue", "narration")):
                p2, sy, wn = _sync_speech_into_prompt(item, changed, old)
                if p2 != item.get("prompt"):
                    item["prompt"] = p2
                    changed.append("prompt（台词同步）")
                synced.extend(sy)
                warnings.extend(wn)
            if "camera" in changed:
                p3, did, wn = _sync_camera_into_prompt(str(item.get("prompt") or ""), str(item.get("camera") or ""))
                if did:
                    item["prompt"] = p3
                    changed.append("prompt（运镜纪律同步）")
                synced.extend(["运镜已同步进提示词的 CAMERA DISCIPLINE 行"] if did else [])
                warnings.extend(wn)
            # ★R3.2 服装：costume 变了（或 chars 变了，<Subject N> 要重排）→ 确定性重建提示词。
            #   只要 patch 里点了 costume 就重建（哪怕值没变）：这样"清空服装"也能把
            #   subject_definitions/retention_analysis 恢复成默认措辞。
            if ("costume" in coerced) or ("chars" in changed):
                np2, wn2 = _rebuild_prompt_for_costume(item, proj)
                warnings.extend(wn2)
                if np2 is not None and np2 != item.get("prompt"):
                    item["prompt"] = np2
                    changed.append("prompt（按服装重建）")
                    synced.append("已按服装重建 subject_definitions / retention_analysis")
        if "shot_size" in changed:
            warnings.append(
                "景别（shot_size）只是分镜表里的记录字段，不参与镜头指纹；"
                "要让画面景别真的变，请点『重写提示词』"
            )
        if "action" in changed:
            warnings.append(
                "action 只作为『重写提示词』的语境（谁在做什么/是否画外音），本身不进提示词、也不参与指纹；"
                "要让动作真的变，请点『重写提示词』"
            )
        doc["dirty"] = bool(changed)
        if "dialogue" in changed or "narration" in changed or new_prompt is not None:
            warnings.extend(prompt_mismatch(item))
        return {
            "changed": changed,
            "warnings": warnings,
            "synced": synced,
            "message": (
                f"已保存 {shot_id}：改了 {'、'.join(changed)}" if changed else f"{shot_id} 内容没有变化"
            ),
        }

    res = _apply_edit(project, root, mutate, action="update", shot=shot_id)
    after = _shot_after(project, root, shot_id)
    res["shot"] = after
    res["message"] += (
        f"；该镜现在是「{_status_cn(after.get('status'))}」"
        "（编辑只写盘、不自动渲染，何时重渲由你决定）"
        if after else ""
    )
    return res


def _status_cn(st: Any) -> str:
    return {CURRENT: "就绪", STALE: "待重渲", MISSING: "缺失"}.get(str(st), str(st))


# ---- 拆分 -----------------------------------------------------------------

def _auto_split_pos(text: str, limit: int) -> int | None:
    """
    自动挑拆分点：先复用 plan._split_by_punct（超长文本按标点切，保证不丢字），
    短文本（≤limit，正常台词都是这种）则取**最靠近中点**的标点边界。
    找不到标点边界返回 None（调用方会给出人话拒绝）。
    """
    if not text:
        return None
    try:
        pl = import_module("plan")
        segs = [str(s) for s in pl._split_by_punct(text, max(1, int(limit)))]
        if len(segs) >= 2 and segs[0]:
            return len(segs[0])
    except Exception:  # noqa: BLE001 - plan 未就绪时退到中点法，拆分仍然安全
        pass
    n = len(text)
    mid = n / 2.0
    best: tuple[float, int] | None = None
    for i, ch in enumerate(text):
        pos = i + 1
        if ch in _SPLIT_BOUNDARY and 0 < pos < n:
            d = abs(pos - mid)
            if best is None or d < best[0]:
                best = (d, pos)
    return best[1] if best else None


def _sec_for_speech(mod: Any, dialogue: str, narration: str, lim: dict) -> int:
    n = 0
    if mod is not None and hasattr(mod, "speech_chars"):
        try:
            n = int(mod.speech_chars(dialogue, narration))
        except Exception:  # noqa: BLE001
            n = 0
    cps = float(lim.get("chars_per_sec") or 4.0)
    need = (n / cps) if cps else 0.0
    return max(int(lim["sec_min"]), min(int(lim["sec_max"]), int(round(need + 1))))


def split_shot(
    project,
    shot_id: str,
    *,
    at: Any = None,
    field: str = "",
    root: Path | None = None,
) -> dict:
    """
    POST /api/shot/split：把一镜拆成两镜（**一个字都不丢**）。

    at: 字符位置（int）；0<at<1 视为比例；不给则自动挑标点边界（先台词，再旁白）。
    field: dialogue / narration，不给则自动挑有内容的那个。
    返回里带 head/tail/concat_ok，供人一眼核对"拼接是否精确等于原文"。
    """
    lim = _limits()
    ev: dict = {}

    def mutate(docs: list[dict]) -> dict:
        doc, idx = _find_in_docs(docs, shot_id)
        item = doc["items"][idx]
        d0 = str(item.get("dialogue") or "")
        n0 = str(item.get("narration") or "")
        fld = (field or "").strip()
        if fld and fld not in ("dialogue", "narration"):
            raise TaskError("field 只能是 dialogue 或 narration")
        if at is not None and fld:
            cands = [(fld, d0 if fld == "dialogue" else n0)]
        else:
            # 优先台词（观众感知最强），台词为空/无边界时退到旁白
            cands = [("dialogue", d0), ("narration", n0)] if not fld else [(fld, d0 if fld == "dialogue" else n0)]

        chosen_field, text, pos = "", "", 0
        for name, txt in cands:
            if not txt.strip():
                continue
            limit = int(lim["dialogue_max"] if name == "dialogue" else lim["narration_max"])
            p = _split_pos(txt, at, limit, name)
            if p is None:
                continue
            chosen_field, text, pos = name, txt, p
            break
        if not chosen_field:
            raise TaskError(
                "这一镜没有可用的拆分点：台词与旁白都是空的，或文本里没有任何标点边界。"
                "请用 at 显式指定字符位置（0<at<1 视为比例），或先用「重写提示词」补齐内容。"
            )

        head, tail = text[:pos], text[pos:]
        if head + tail != text:  # noqa: SIM108 - 显式断言，别让"丢字"有机会溜过去
            raise TaskError("内部校验失败：拆分点会丢字，已拒绝（请把这个镜头号报给开发者）")
        if not head.strip() or not tail.strip():
            raise TaskError(f"在 {pos} 处拆会把内容拆出空段（{chosen_field} 共 {len(text)} 字），请换拆分点")
        if not _NORM_STRIP_RE.sub("", head) or not _NORM_STRIP_RE.sub("", tail):
            # 只靠标点撑起来的一段（例如 "！"）没有任何内容，渲染出来是废镜
            raise TaskError(f"在 {pos} 处会拆出『只剩标点』的一段（「{head}」/「{tail}」），请换拆分点")

        second = dict(item)  # 继承景别/运镜/角色/seed/服装/action 等全部字段
        if chosen_field == "dialogue":
            item["dialogue"], second["dialogue"] = head, tail
        else:
            item["narration"], second["narration"] = head, tail
        second["id"] = _unique_shot_id(
            [it.get("id") for dc in docs for it in dc["items"]], str(item.get("id") or shot_id)
        )
        try:
            smod = import_module("shots")
        except ModuleNotReady:
            smod = None
        s1 = _sec_for_speech(smod, item.get("dialogue") or "", item.get("narration") or "", lim)
        s2 = _sec_for_speech(smod, second.get("dialogue") or "", second.get("narration") or "", lim)
        item["sec"], second["sec"] = s1, s2
        doc["items"].insert(idx + 1, second)
        doc["dirty"] = True
        hints = [
            f"两镜的提示词暂时都还是原镜的原文（提示词不能按标点自动切）；"
            f"请对 {second['id']} 点『重写提示词』，否则两镜画面会一样",
            f"时长按语音预算重算：{s1}s + {s2}s（单镜下限 {lim['sec_min']}s，两镜合计可能比原来长）",
        ]
        ev.update(
            {
                "split_field": chosen_field,
                "split_at": pos,
                "original": text,
                "head": head,
                "tail": tail,
                "concat_ok": (head + tail) == text,
                "new_id": second["id"],
                "sec": [s1, s2],
            }
        )
        return {
            "changed": [chosen_field, "prompt(复制)", "sec"],
            "warnings": hints,
            "message": f"已把 {shot_id} 拆成 {shot_id} + {second['id']}（{chosen_field} {len(text)} 字 → "
                       f"{len(head)} + {len(tail)} 字，一字未丢）",
        }

    res = _apply_edit(project, root, mutate, action="split", shot=shot_id)
    res.update(ev)
    res["shot"] = _shot_after(project, root, shot_id)
    res["next_shot"] = _shot_after(project, root, str(ev.get("new_id") or ""))
    warn = list(res.get("warnings") or [])
    for s in (res.get("shot"), res.get("next_shot")):
        if s:
            warn.extend(s.get("prompt_mismatch") or [])
    res["warnings"] = warn
    return res


def _split_pos(text: str, at: Any, limit: int, field: str) -> int | None:
    """求拆分点。at 为 None 时自动挑；显式给 at 时越界直接报人话错误。"""
    n = len(text)
    if at is None:
        return _auto_split_pos(text, limit)
    if isinstance(at, bool):
        raise TaskError("at 不能是布尔值")
    if isinstance(at, str):
        s = at.strip()
        if not re.fullmatch(r"-?\d+(\.\d+)?", s):
            raise TaskError(f"at 必须是字符位置或 0~1 的比例，收到 {at!r}")
        f = float(s)
    elif isinstance(at, (int, float)):
        f = float(at)
    else:
        raise TaskError(f"at 必须是字符位置或 0~1 的比例，收到 {type(at).__name__}")
    pos = int(round(f * n)) if 0 < f < 1 else int(f)
    if not (0 < pos < n):
        raise TaskError(f"at={at} 越界：{field} 共 {n} 字，拆分点必须落在 1..{max(0, n - 1)}")
    return pos


# ---- 合并 -----------------------------------------------------------------

def _merge_prompts(mod: Any, p1: str, p2: str, chars_same: bool) -> tuple[str, list[str]]:
    """
    两镜六段式 → 一镜。

    只在"角色顺序完全一致"时才做确定性拼接（否则 <Subject N> 编号会错位）；
    拼接方式是把两段 detailed_description 用 " Then: " 接起来（两句 <d> 都保住），
    其余段落沿用前一镜。任何情况下都不静默产出一个角色错位的提示词。
    """
    fallback = ["合并沿用了前一镜的提示词（不能自动合并两镜正文），请点『重写提示词』"]
    if not chars_same:
        return p1, ["两镜角色不同，<Subject N> 编号无法自动对齐；" + fallback[0]]
    if mod is None or not hasattr(mod, "section_bodies"):
        return p1, fallback
    six, _ = _sections_constants()
    s1 = {str(k): str(v) for k, v in mod.section_bodies(p1).items()}
    s2 = {str(k): str(v) for k, v in mod.section_bodies(p2).items()}
    if not all(k in s1 for k in six) or not all(k in s2 for k in six):
        return p1, ["提示词不是标准六段式，" + fallback[0]]
    merged = dict(s1)
    d1 = (s1.get("detailed_description") or "").rstrip()
    d2 = (s2.get("detailed_description") or "").strip()
    if d2:
        merged["detailed_description"] = f"{d1} Then: {d2}".strip()
    a1 = (s1.get("overall_soundscape") or "").rstrip()
    a2 = (s2.get("overall_soundscape") or "").strip()
    if a2:
        merged["overall_soundscape"] = f"{a1} {a2}".strip()
    m1 = (s1.get("non_diegetic_music") or "").strip()
    m2 = (s2.get("non_diegetic_music") or "").strip()
    if (not m1 or m1.lower() == "none") and m2:
        merged["non_diegetic_music"] = m2
    try:
        return compose_prompt_from_sections(merged), ["两镜正文已按 'Then:' 拼接，建议人工确认"]
    except TaskError as e:
        return p1, [f"自动拼接失败（{e}），" + fallback[0]]


def merge_shot(project, shot_id: str, root: Path | None = None) -> dict:
    """POST /api/shot/merge：与**下一镜**合并。台词/旁白/时长超上限就拒绝并说明原因。"""
    lim = _limits()
    ev: dict = {}

    def mutate(docs: list[dict]) -> dict:
        doc, idx = _find_in_docs(docs, shot_id)
        nxt = _neighbor_in_docs(docs, shot_id, +1)
        if nxt is None:
            raise TaskError(f"{shot_id} 是最后一镜，后面没有可合并的镜头")
        d1 = str(doc["items"][idx].get("dialogue") or "")
        n1 = str(doc["items"][idx].get("narration") or "")
        d2 = str(nxt.get("dialogue") or "")
        n2 = str(nxt.get("narration") or "")
        s1 = int(doc["items"][idx].get("sec") or 0)
        s2 = int(nxt.get("sec") or 0)
        reasons: list[str] = []
        if len(d1 + d2) > lim["dialogue_max"]:
            reasons.append(
                f"台词合并后 {len(d1 + d2)} 字 > 上限 {lim['dialogue_max']} 字"
                f"（「{d1}」+「{d2}」，字幕一行放不下、语速也念不完）"
            )
        if len(n1 + n2) > lim["narration_max"]:
            reasons.append(f"旁白合并后 {len(n1 + n2)} 字 > 上限 {lim['narration_max']} 字")
        if s1 + s2 > lim["sec_max"]:
            reasons.append(f"时长合并后 {s1 + s2}s > 单镜上限 {lim['sec_max']}s")
        if reasons:
            raise TaskError("已拒绝合并 " + shot_id + " 与 " + str(nxt.get("id")) + "：\n- " + "\n- ".join(reasons))

        chars1 = [str(c) for c in _as_list(doc["items"][idx].get("chars"))]
        chars2 = [str(c) for c in _as_list(nxt.get("chars"))]
        union = chars1 + [c for c in chars2 if c not in chars1]
        item = doc["items"][idx]
        item["dialogue"] = d1 + d2
        item["narration"] = n1 + n2
        item["chars"] = union
        item["sec"] = s1 + s2
        a1 = str(item.get("action") or "").rstrip()
        a2 = str(nxt.get("action") or "").strip()
        if a1 or a2:
            if a1 and not a1.endswith((".", "!", "?", "。")):
                a1 += "."
            item["action"] = f"{a1} Then: {a2}".strip()
        chars_same = chars1 == chars2
        mod = None
        try:
            mod = import_module("shots")
        except ModuleNotReady:
            mod = None
        new_prompt, warns = _merge_prompts(mod, str(item.get("prompt") or ""), str(nxt.get("prompt") or ""), chars_same)
        item["prompt"] = new_prompt
        removed = str(nxt.get("id") or "")
        doc["items"].remove(nxt)
        doc["dirty"] = True
        ev.update({"merged": [shot_id, removed], "removed_id": removed, "sec": item["sec"]})
        return {
            "changed": ["dialogue", "narration", "chars", "sec", "action", "prompt", f"删除 {removed}"],
            "warnings": warns,
            "message": f"已把 {removed} 合并进 {shot_id}（时长 {s1}s + {s2}s = {item['sec']}s）；"
                       f"{removed} 的产物文件没有删除，但成片按镜头表组装，不会再包含它",
        }

    res = _apply_edit(project, root, mutate, action="merge", shot=shot_id)
    res.update(ev)
    res["shot"] = _shot_after(project, root, shot_id)
    if res["shot"]:
        res["warnings"] = list(res.get("warnings") or []) + (res["shot"].get("prompt_mismatch") or [])
    return res


# ---- 插入 / 删除 ----------------------------------------------------------

def insert_shot(project, after: str, shot: dict | None = None, root: Path | None = None) -> dict:
    """
    POST /api/shot/insert：在 after 之后插入一镜。

    新镜默认**继承 after 那一镜**（角色/景别/运镜/提示词），只把时长压到下限、
    台词旁白清空 —— 因为空 chars 或空 prompt 会被 validate_shots 判坏表，
    而"空提示词"是用户最难自己修的状态（他会看到一个死镜头）。
    提示词是复制的，所以返回里带醒目告警：请点『重写提示词』。
    """
    lim = _limits()
    shot = shot if isinstance(shot, dict) else {}
    allowed = set(SHOT_PATCH_FIELDS) | {"id"}
    unknown = sorted(set(shot) - allowed)
    if unknown:
        raise TaskError("新镜头里不认识的字段：" + "、".join(unknown))
    ev: dict = {}

    def mutate(docs: list[dict]) -> dict:
        doc, idx = _find_in_docs(docs, after)
        base = doc["items"][idx]
        new = {
            "id": "",
            "sec": int(lim["sec_min"]),
            "chars": [str(c) for c in _as_list(base.get("chars"))],
            "seed": base.get("seed"),
            "prompt": str(base.get("prompt") or ""),
            "shot_size": str(base.get("shot_size") or ""),
            "camera": str(base.get("camera") or ""),
            "dialogue": "",
            "narration": "",
            "action": str(base.get("action") or ""),
        }
        if isinstance(base.get("costume"), dict):
            new["costume"] = dict(base["costume"])
        for k, v in shot.items():
            new[k] = _coerce_field(k, v) if k != "id" else str(v)
        if not new["id"]:
            new["id"] = _unique_shot_id([it.get("id") for dc in docs for it in dc["items"]], str(base.get("id") or after))
        taken = [str(it.get("id") or "") for dc in docs for it in dc["items"]]
        if new["id"] in taken:
            raise TaskError(f"新镜头号 {new['id']} 已存在，换一个（或留空让系统自动生成）")
        for label, key, mx in (("台词", "dialogue", int(lim["dialogue_max"])),
                               ("旁白", "narration", int(lim["narration_max"]))):
            if len(str(new.get(key) or "")) > mx:
                raise TaskError(f"新镜的{label} {len(str(new.get(key)))} 字超过单句上限 {mx} 字，请改短或插入后再拆分")
        if not str(new.get("seed") or "").isdigit():
            # seed 是"可复现性"的根，默认给一个没被用过的值（同 seed 会让两镜画面高度相似）
            seeds = [int(it.get("seed") or 0) for dc in docs for it in dc["items"]]
            new["seed"] = (max(seeds) + 1) if seeds else 3100
        doc["items"].insert(idx + 1, new)
        doc["dirty"] = True
        ev.update({"new_id": new["id"]})
        return {
            "changed": [f"插入 {new['id']}（继承 {after} 的提示词/角色/景别）"],
            "warnings": [f"{new['id']} 的提示词是复制 {after} 的，请点『重写提示词』生成它自己的内容"],
            "message": f"已在 {after} 之后插入 {new['id']}（{new['sec']}s，角色 {'、'.join(new['chars']) or '空'}）",
        }

    res = _apply_edit(project, root, mutate, action="insert", shot=after)
    res.update(ev)
    res["shot"] = _shot_after(project, root, str(ev.get("new_id") or ""))
    return res


def delete_shot(project, shot_id: str, root: Path | None = None) -> dict:
    """POST /api/shot/delete：删除该镜（只从镜头表移除；不动 clips/ 产物与清单记录）。"""
    ev: dict = {}

    def mutate(docs: list[dict]) -> dict:
        doc, idx = _find_in_docs(docs, shot_id)
        doc["items"].pop(idx)
        doc["dirty"] = True
        ev.update({"removed_id": shot_id})
        return {
            "changed": [f"删除 {shot_id}"],
            "warnings": [
                f"{shot_id} 的产物 clips/{shot_id}.mp4 没有删除（成片按镜头表组装，不会再包含它）；"
                "如果要连产物一起清掉，请手动处理"
            ],
            "message": f"已删除 {shot_id}",
        }

    res = _apply_edit(project, root, mutate, action="delete", shot=shot_id)
    res.update(ev)
    return res


def undo_shot_edit(project, root: Path | None = None) -> dict:
    """
    POST /api/shot/undo：把 shots/*.json.bak 恢复回 shots/*.json（一步撤销）。

    若上一步是「重排编号」，还要把搬走的产物/清单记录**搬回来**：
    否则表恢复了旧编号、产物却留在新编号下，所有相关镜头会突然集体"缺失"。
    """
    pdir = resolve_project(project, root)
    proj = Project(pdir)
    with _shot_edit_lock(pdir):
        if not proj.shots_dir.is_dir():
            raise TaskError(f"{proj.shots_dir} 不存在")
        prev = read_json(pdir / "state" / "shot_edit.json", default=None) or {}
        restored: list[str] = []
        for p in sorted(proj.shots_dir.glob("*.json")):
            if p.name.startswith(".") or p.name.endswith(".tmp"):
                continue
            bak = p.with_suffix(p.suffix + ".bak")
            if not bak.is_file():
                continue
            _atomic_write_bytes(p, bak.read_bytes())
            restored.append(p.name)
        if not restored:
            raise TaskError("没有可撤销的备份（shots/*.json.bak 不存在）；只有编辑过的项目才会有备份")
        moved_back: list[str] = []
        moves = prev.get("artifact_moves") if isinstance(prev, dict) else None
        if isinstance(moves, dict) and moves:
            manifest = proj.manifest
            ck = proj.checkpoint
            for new, old in moves.items():
                src, dst = proj.clip(str(new)), proj.clip(str(old))
                if src.is_file() and not dst.exists():
                    os.replace(src, dst)
                    moved_back.append(f"{new}.mp4 → {old}.mp4")
                entry = manifest.shots.pop(str(new), None)
                if entry is not None:
                    _retarget_entry_file(entry, src, dst)
                    manifest.shots[str(old)] = entry
                pid = ck.shots.pop(str(new), None)
                if pid is not None:
                    ck.shots[str(old)] = pid
            manifest.save()
            ck.save()
        mod = None
        try:
            mod = import_module("shots")
        except ModuleNotReady:
            mod = None
        docs = _load_shot_docs(proj)
        issues = _validate_items(mod, [it for d in docs for it in d["items"]], proj.refs_dir) if mod else []
    msg = "已撤销上一步编辑：从 " + "、".join(f"{n}.bak" for n in restored) + " 恢复"
    if moved_back:
        msg += "；产物也搬回来了（" + "、".join(moved_back) + "）"
    rec = _record_edit(pdir, {"action": "undo", "shot": "", "files": restored, "changed": restored, "message": msg})
    return {
        "action": "undo",
        "message": msg,
        "files": restored,
        "moved_back": moved_back,
        "issues": issues,
        "last_edit": rec,
        "undo_available": False,
    }


# ---- 批量 ----------------------------------------------------------------

def _selector_ids(project, root, selector: dict) -> list[str]:
    """把选择器（章/场/选中集合/状态）解析成镜头号列表。"""
    if not isinstance(selector, dict) or not selector:
        raise TaskError("selector 不能为空（支持 chapter / scene / ids / status / qc_fail / all）")
    table = shot_table(project, root)
    rows = table["shots"]
    ids = [r["id"] for r in rows]
    status_of = {r["id"]: r.get("status") for r in rows}

    if selector.get("ids") is not None:
        want = {str(x) for x in _as_list(selector["ids"])}
        return [i for i in ids if i in want]
    if selector.get("all"):
        return ids
    if selector.get("qc_fail"):
        return [r["id"] for r in rows if r.get("qc") and not r["qc"].get("ok")]
    if selector.get("status"):
        want = str(selector["status"])
        if want == "qc_fail":
            return [r["id"] for r in rows if r.get("qc") and not r["qc"].get("ok")]
        if want not in (MISSING, STALE, CURRENT):
            raise TaskError(f"status 只能是 {MISSING}/{STALE}/{CURRENT}/qc_fail，收到 {want!r}")
        return [i for i in ids if status_of.get(i) == want]

    chapter = selector.get("chapter")
    scene = selector.get("scene")
    if chapter is None and scene is None and selector.get("prefix") is not None:
        prefix = str(selector["prefix"]).strip()
        return [i for i in ids if i == prefix or i.startswith(prefix + "-")]
    if chapter is None and scene is None:
        raise TaskError("selector 至少要给一个条件：chapter / scene / ids / status / qc_fail / all")
    ch = sc = None
    if chapter is not None:
        ch = _as_int_strict(chapter)
        if ch is None:
            raise TaskError(f"chapter 必须是整数，收到 {chapter!r}")
    if scene is not None:
        sc = _as_int_strict(scene)
        if sc is None:
            raise TaskError(f"scene 必须是整数，收到 {scene!r}")
    out: list[str] = []
    for i in ids:
        nums = re.findall(r"\d+", i)
        if ch is not None and (not nums or int(nums[0]) != ch):
            continue
        if sc is not None and (len(nums) < 2 or int(nums[1]) != sc):
            continue
        out.append(i)
    return out


def bulk_update(project, selector: dict, patch: dict, root: Path | None = None) -> dict:
    """POST /api/shots/bulk：按章/场/选中集合批量改（同样只写盘 + 标 stale）。"""
    if not isinstance(patch, dict) or not patch:
        raise TaskError("patch 不能为空")
    unknown = sorted(set(patch) - set(BULK_PATCH_FIELDS))
    if unknown:
        raise TaskError(
            "批量只能改：" + "、".join(BULK_PATCH_FIELDS) + f"；收到不支持的字段 {'、'.join(unknown)}"
            "（批量改台词/提示词几乎一定是误操作，请逐镜改）"
        )
    ids = _selector_ids(project, root, selector)
    if not ids:
        raise TaskError("选择器没有匹配到任何镜头，什么都没改")
    coerced = {k: _coerce_field(k, v) for k, v in patch.items()}
    want = set(ids)
    ev: dict = {}
    proj = Project(resolve_project(project, root))  # 服装重建要用它读 prompts/ 与 costumes.json

    def mutate(docs: list[dict]) -> dict:
        changed_ids: list[str] = []
        fields: set[str] = set()
        warns: list[str] = []
        rebuilt = 0
        skipped_cost = 0
        for doc in docs:
            for item in doc["items"]:
                sid = str(item.get("id") or "")
                if sid not in want:
                    continue
                touched = False
                item_chars = {str(c) for c in _as_list(item.get("chars"))}
                for k, v in coerced.items():
                    if k == "costume":
                        # 只把"本镜真的登场"的角色写进去：否则整章换装会把
                        # {"孙悟空":"armor"} 塞进几十个没有孙悟空的镜头里，表越改越脏
                        applicable = {n: val for n, val in v.items() if not item_chars or str(n) in item_chars}
                        skipped_cost += len(v) - len(applicable)
                        if not applicable:
                            continue
                        merged = dict(item.get("costume")) if isinstance(item.get("costume"), dict) else {}
                        merged.update(applicable)
                        if item.get("costume") != merged:
                            item["costume"] = merged
                            touched = True
                            fields.add("costume")
                        continue
                    if item.get(k) != v:
                        item[k] = v
                        touched = True
                        fields.add(k)
                # ★ 重建的触发条件**不能**是"字段变了"：上一次重建失败（例如提示词缺段）时
                #   字段已经落盘了，用户再点一次保存时字段值相同 → 不重建 → 那个"字段说战甲、
                #   提示词还是常服"的不一致就永远修不回来。所以只要 patch 里点了 costume 就重建。
                if "costume" in coerced:
                    np2, wn2 = _rebuild_prompt_for_costume(item, proj)
                    warns.extend(wn2)
                    if np2 is not None and np2 != item.get("prompt"):
                        item["prompt"] = np2
                        rebuilt += 1
                        fields.add("prompt（按服装重建）")
                        touched = True
                if touched:
                    changed_ids.append(sid)
                    doc["dirty"] = True
                    # 批量改运镜同样要同步纪律行，否则整批都"改了不生效"
                    if "camera" in coerced:
                        p3, did, wn = _sync_camera_into_prompt(
                            str(item.get("prompt") or ""), str(item.get("camera") or "")
                        )
                        if did:
                            item["prompt"] = p3
                            fields.add("prompt（运镜纪律同步）")
                        warns.extend(wn)
        if skipped_cost:
            warns.insert(0, f"有 {skipped_cost} 条服装指定对应的角色不在所选镜头里，已跳过（不是错误）")
        ev.update({"updated": changed_ids, "rebuilt": rebuilt})
        if not changed_ids:
            return {"changed": [], "message": f"选中 {len(ids)} 个镜头，但字段值本来就一样，没有改动"}
        return {
            "changed": sorted(fields),
            "warnings": sorted(set(warns))[:5],
            "message": f"已批量改 {len(changed_ids)} 个镜头（{'、'.join(sorted(fields))}）；"
                       + (f"其中 {rebuilt} 镜已按服装重建提示词；" if rebuilt else "")
                       + "只标待重渲，不自动渲染",
        }

    res = _apply_edit(project, root, mutate, action="bulk")
    res.update(ev)
    if ev.get("updated"):
        try:
            table = shot_table(project, root)
            by_id = {r["id"]: r["status"] for r in table["shots"]}
            res["stale"] = [i for i in ev["updated"] if by_id.get(i) != CURRENT]
        except Exception:  # noqa: BLE001
            res["stale"] = []
    return res


# ---- 重排编号 -------------------------------------------------------------

def renumber_shots(project, root: Path | None = None, *, force: bool = False) -> dict:
    """
    POST /api/shots/renumber：按分镜顺序把镜头号重排成 `<章>-<场>-<序号>`。

    "拆分/插入"故意用 `1-4-01_2` 这种后缀，不动任何已有镜头号（渲染进行中尤其重要）。
    需要连号时用这个接口：它会把产物文件与清单记录**一起搬过去**，产物不会变成孤儿。

    有任务在跑时默认拒绝 —— 在跑的 worker 内存里握着旧编号的 manifest，
    它一保存就会把搬好的记录覆盖回去，产物与编号会错位。
    """
    pdir = resolve_project(project, root)
    proj = Project(pdir)
    task = read_task(pdir)
    if task_running(task) and not force:
        raise TaskError(
            f"有任务正在跑（stage={task.get('stage')} pid={task.get('pid')}），"
            "改编号会让在跑的任务把产物写到旧编号下；等它结束再改，或显式 force=true（不建议）"
        )
    with _shot_edit_lock(pdir):
        docs = _load_shot_docs(proj)
        if not docs:
            raise TaskError(f"{proj.shots_dir} 下没有镜头表（*.json）")
        refs = _shot_refs(docs)
        groups: dict[tuple, list[str]] = {}
        for di, ii in refs:
            sid = str(docs[di]["items"][ii].get("id") or "")
            # 去掉"拆分/插入"生成的 `_2` 后缀再取数字：
            # 否则 `1-12-01_2` 会被算成 (1,12,1) 这一组，重排出 `1-12-01-01` 这种怪号。
            base_id = re.sub(r"_\d+$", "", sid)
            nums = re.findall(r"\d+", base_id)
            key = tuple(nums[:-1]) if len(nums) >= 2 else ()
            groups.setdefault(key, []).append(sid)
        mapping: dict[str, str] = {}
        for key, members in groups.items():
            if not key:
                continue  # 认不出章-场的镜头号不动它（宁可少改，也不猜）
            for n, sid in enumerate(members, 1):
                mapping[sid] = "-".join(list(key) + [f"{n:02d}"])
        changed = {k: v for k, v in mapping.items() if k != v}
        if not changed:
            return {"action": "renumber", "message": "编号已经是连续的，没有改动", "mapping": {}, "changed": [], "issues": []}

        # 产物与清单一起搬：不然重排后所有镜头都会显示"缺失"而白烧一遍 GPU
        moved: list[str] = []
        artifact_moves: dict[str, str] = {}
        manifest = proj.manifest
        ck = proj.checkpoint
        for old, new in changed.items():
            src = proj.clip(old)
            dst = proj.clip(new)
            if src.is_file() and not dst.exists():
                os.replace(src, dst)
                moved.append(f"{old}.mp4 → {new}.mp4")
                artifact_moves[new] = old
            entry = manifest.shots.pop(old, None)
            if entry is not None:
                _retarget_entry_file(entry, src, dst)
                manifest.shots[new] = entry
            pid = ck.shots.pop(old, None)
            if pid is not None:
                ck.shots[new] = pid
        for doc in docs:
            for item in doc["items"]:
                sid = str(item.get("id") or "")
                if sid in changed:
                    item["id"] = changed[sid]
                    doc["dirty"] = True
        before = [it for d in docs for it in d["items"]]
        mod = import_module("shots")
        problems = _validate_items(mod, before, proj.refs_dir)
        for doc in docs:
            if not doc["dirty"]:
                continue
            _backup_file(doc["path"])
            _write_shot_doc(doc)
        manifest.save()
        ck.save()
    rec = _record_edit(
        pdir,
        {"action": "renumber", "shot": "", "files": ["（全部镜头表）"], "changed": list(changed.values()),
         "artifact_moves": artifact_moves,
         "message": f"重排 {len(changed)} 个镜头号"},
    )
    return {
        "action": "renumber",
        "mapping": changed,
        "changed": sorted(changed.values()),
        "moved": moved,
        "issues": problems,
        "message": f"已重排 {len(changed)} 个镜头号，并搬走 {len(moved)} 个产物（清单记录同步更新）",
        "last_edit": rec,
    }


# ---- 单镜重写提示词（只调 1 次 LLM） --------------------------------------

def _llm_ledger_path(pdir: Path) -> Path:
    return pdir / "state" / "shot_llm.json"


def _llm_ledger_add(pdir: Path, shot_id: str, meta: dict, note: str = "") -> dict:
    """每次重写记一笔（调用次数 + token）。验收要求"只增加 1 次调用"要靠它自证。"""
    p = _llm_ledger_path(pdir)
    d = read_json(p, default=None)
    if not isinstance(d, dict):
        d = {}
    usage = (meta or {}).get("usage") or {}
    d["calls"] = int(d.get("calls") or 0) + 1
    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
        try:
            d[k] = int(d.get(k) or 0) + int(usage.get(k) or 0)
        except (TypeError, ValueError):
            d[k] = int(d.get(k) or 0)
    entries = d.get("entries")
    if not isinstance(entries, list):
        entries = []
    entries.append(
        {
            "at": int(time.time()),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "shot": shot_id,
            "model": (meta or {}).get("model", ""),
            "usage": {k: usage.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens")},
            "note": note,
        }
    )
    d["entries"] = entries[-200:]
    _atomic_write_json(p, d)
    return d


def llm_ledger(project, root: Path | None = None) -> dict:
    pdir = resolve_project(project, root)
    d = read_json(_llm_ledger_path(pdir), default=None)
    if not isinstance(d, dict):
        return {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "entries": []}
    return d


def rewrite_shot(project, shot_id: str, *, feedback: str = "", root: Path | None = None) -> dict:
    """
    POST /api/shot/rewrite：只重写这一镜的六段式（**1 次 LLM 调用**，不是 53 次）。

    复用 plan.py 的单镜生成口径（SHOT_SYSTEM + _shot_user_prompt + _shot_prompt_issues），
    但**不跑** plan 的整章流程：不拆章节、不重写其它镜头、不碰别的镜头表。
    校验不过就拒绝写盘（返回 400 + 问题清单），磁盘保持原状。
    """
    pdir = resolve_project(project, root)
    proj = Project(pdir)
    if not SHOT_ID_RE.match(str(shot_id or "")):
        raise TaskError(f"非法镜头号：{shot_id!r}")
    pl = import_module("plan")
    mod = import_module("shots")
    docs = _load_shot_docs(proj)
    if not docs:
        raise TaskError(f"{proj.shots_dir} 下没有镜头表（*.json）")
    doc, idx = _find_in_docs(docs, shot_id)
    item = doc["items"][idx]
    prev = _neighbor_in_docs(docs, shot_id, -1)

    cards = pl.load_char_cards(proj)
    style = pl._card_style(cards.values()) or getattr(pl, "DEFAULT_STYLE", "")
    chars = [str(c) for c in _as_list(item.get("chars"))]
    dd_cur = ""
    try:
        dd_cur = str(mod.section_bodies(str(item.get("prompt") or "")).get("detailed_description") or "")
    except Exception:  # noqa: BLE001
        dd_cur = ""
    nums = re.findall(r"\d+", str(shot_id))
    scene = "-".join(nums[:2]) if len(nums) >= 2 else str(shot_id)
    # action 缺失时用现有正文开头兜底：否则 LLM 拿不到"这镜在演什么"，
    # 只能凭空编一个动作（比"承接上一镜"更差）。
    action = str(item.get("action") or "").strip()
    if not action:
        head = f"{item.get('shot_size') or ''}, {item.get('camera') or ''}".strip(", ")
        action = (head + "；" + dd_cur[:400]).strip("；") or "(沿用当前镜头画面)"
    row = {
        "_id": str(shot_id),
        "scene": scene,
        "shot_size": str(item.get("shot_size") or ""),
        "camera": str(item.get("camera") or ""),
        "sec": int(item.get("sec") or 0),
        "chars": chars,
        "dialogue": str(item.get("dialogue") or ""),
        "narration": str(item.get("narration") or ""),
        "action": action,
    }
    prev_row = None
    if prev is not None:
        try:
            tail = str(mod.section_bodies(str(prev.get("prompt") or "")).get("detailed_description") or "")[-220:]
        except Exception:  # noqa: BLE001
            tail = ""
        prev_row = {"action": str(prev.get("action") or ""), "_tail": tail}

    cfg = load_params(pdir)
    temperature = 0.3
    try:
        obj, meta = pl._llm_json(
            cfg,
            pl.SHOT_SYSTEM,
            pl._shot_user_prompt(row=row, cards=cards, style=style, prev_row=prev_row, feedback=feedback or ""),
            temperature=temperature,
            log=None,
            tag=f"重写 {shot_id}（单镜，1 次调用）",
        )
    except pl.PlanError as e:
        raise ShotLLMError(f"重写 {shot_id} 时 LLM 调用失败：{e}") from e
    except Exception as e:  # noqa: BLE001 - 网络/解析异常都转成人话，且绝不写盘
        raise ShotLLMError(f"重写 {shot_id} 时 LLM 调用异常：{type(e).__name__}: {e}") from e

    payload = {
        "detailed_description": str(obj.get("detailed_description") or "").strip(),
        "overall_soundscape": str(obj.get("overall_soundscape") or "").strip(),
        "non_diegetic_music": str(obj.get("non_diegetic_music") or "").strip(),
    }
    issues = [str(x) for x in pl._shot_prompt_issues(row, payload, cards)]
    hard = [i for i in issues if not i.startswith("__soft__")]
    repaired: list[str] = []
    if hard and all(i.startswith(pl._REPAIRABLE_PREFIXES) for i in hard):
        # 与 plan 阶段同一套机械兜底：只修"台词没进 <d>"这类可机械修复的问题
        repaired = pl._repair_missing_lines(row, payload) + pl._repair_missing_subjects(row, payload)
        hard = [i for i in pl._shot_prompt_issues(row, payload, cards) if not i.startswith("__soft__")]
    if hard:
        ledger = _llm_ledger_add(pdir, str(shot_id), meta, note="校验未通过，已拒绝写盘（这次调用已计费）")
        raise ShotLLMError(
            f"重写后的提示词没通过校验，已拒绝写盘（{shot_id} 的内容没有变化）：\n- " + "\n- ".join(hard),
            issues=hard,
            payload=payload,
        )

    prompt = pl._assemble_prompt(
        chars=chars,
        cards=cards,
        camera=row["camera"],
        detailed_description=payload["detailed_description"],
        overall_soundscape=payload["overall_soundscape"],
        non_diegetic_music=payload["non_diegetic_music"],
    )
    ledger = _llm_ledger_add(pdir, str(shot_id), meta, note="单镜重写成功")

    def mutate(docs2: list[dict]) -> dict:
        d2, i2 = _find_in_docs(docs2, shot_id)
        it = d2["items"][i2]
        it["prompt"] = prompt
        d2["dirty"] = True
        return {
            "changed": ["prompt（六段式全文重写）"],
            "warnings": (["机械修复：" + "；".join(repaired)] if repaired else []),
            "message": f"已重写 {shot_id} 的六段式提示词（1 次 LLM 调用，{len(prompt)} 字）",
        }

    res = _apply_edit(project, root, mutate, action="rewrite", shot=str(shot_id))
    res["llm"] = {
        "calls": 1,
        "model": meta.get("model", ""),
        "usage": meta.get("usage") or {},
        "repaired": repaired,
        "ledger": {k: ledger.get(k) for k in ("calls", "prompt_tokens", "completion_tokens", "total_tokens")},
    }
    res["shot"] = _shot_after(project, root, shot_id)
    return res


# ---------------------------------------------------------------- 角色定妆视图（R2 抽卡/采纳）

# 角色名：中文/英文/数字/下划线/连字符，最长 32；用于挡目录穿越
CHAR_NAME_RE = re.compile(r"^[\w\u4e00-\u9fff][\w\u4e00-\u9fff\-]{0,31}$")


def char_names(project: str | os.PathLike[str], root: Path | None = None) -> list[str]:
    """角色列表：以 prompts/char_<名>.txt 为准（提示词是角色存在的唯一依据）。"""
    proj = Project(resolve_project(project, root))
    return char_names_of(proj)


def char_names_of(proj: Project) -> list[str]:
    return sorted(f.stem[len("char_"):] for f in proj.prompts_dir.glob("char_*.txt"))


def _candidate_files(proj: Project, name: str) -> list[Path]:
    """某角色的抽卡候选（按 seed 数值排序，与对比图 _sheet.jpg 的网格顺序一致）。"""
    mod = None
    try:
        mod = import_module("chars")
    except ModuleNotReady:
        pass
    if mod is not None and hasattr(mod, "list_candidates"):
        try:
            return list(mod.list_candidates(proj, name))
        except Exception:
            pass
    d = proj.refs_dir / "_gacha" / name
    if not d.is_dir():
        return []

    def _seed(p: Path) -> int:
        try:
            return int(p.stem.replace("seed", ""))
        except ValueError:
            return 1 << 30

    return sorted(d.glob("seed*.png"), key=_seed)


_DIGEST_CACHE: dict[tuple[str, int, int], str] = {}


def file_digest16(path: Path) -> str:
    """
    内容指纹：size + sha256 前 16 位（用于判断"这张候选就是当前 ref"）。

    之所以按内容而不是文件名：采纳时是 shutil.copy2，文件名会变（schema 不同），
    但内容一模一样；反过来用户也可能把同一张图换个名字放进来。
    缓存键含 mtime+size，文件被覆盖后自然失效。
    """
    try:
        st = path.stat()
    except OSError:
        return ""
    key = (str(path), st.st_mtime_ns, st.st_size)
    hit = _DIGEST_CACHE.get(key)
    if hit:
        return hit
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return ""
    digest = f"{st.st_size}:{h.hexdigest()[:16]}"
    if len(_DIGEST_CACHE) > 512:  # 简单封顶，别让长跑的服务无限涨
        _DIGEST_CACHE.clear()
    _DIGEST_CACHE[key] = digest
    return digest


def char_prompt(project: str | os.PathLike[str], name: str, root: Path | None = None) -> str:
    proj = Project(resolve_project(project, root))
    f = proj.prompts_dir / f"char_{name}.txt"
    try:
        return f.read_text(encoding="utf-8")
    except OSError:
        return ""


def char_prompt_warnings(prompt: str) -> list[str]:
    """风格锚告警。chars.py 还没就绪时返回空表（UI 就不会误报）。"""
    try:
        mod = import_module("chars")
    except ModuleNotReady:
        return []
    fn = getattr(mod, "check_prompt_style", None)
    if fn is None:
        return []
    try:
        return [str(x) for x in fn(prompt or "")]
    except Exception:
        return []


def char_table(project: str | os.PathLike[str], root: Path | None = None) -> dict:
    """
    角色定妆视图：/api/chars 的全部数据。

    返回里给的是**磁盘路径**；URL 由 web.py 拼（它才知道自己的路由长什么样）。
    """
    pdir = resolve_project(project, root)
    proj = Project(pdir)
    params = load_params(pdir)
    # gen.py 把参考图名交给 ComfyUI 的 LoadImage，而 LoadImage 只认 ComfyUI 自己的 input/。
    # 定妆照没同步过去的话，渲染会在提交时才失败（甚至更晚），所以这里先查、先显示。
    comfy_input = Path(str(params.get("comfy_input") or "/home/max/ComfyUI/input"))
    rows: list[dict] = []
    for name in char_names_of(proj):
        prompt = char_prompt(pdir, name)
        ref = proj.refs_dir / f"char_{name}.png"
        ref_digest = file_digest16(ref) if ref.is_file() else ""
        synced_ref = comfy_input / f"char_{name}.png"
        ref_synced = bool(ref_digest) and file_digest16(synced_ref) == ref_digest
        cands = []
        for p in _candidate_files(proj, name):
            try:
                st = p.stat()
                mt, size = int(st.st_mtime), st.st_size
            except OSError:
                continue
            try:
                seed = int(p.stem.replace("seed", ""))
            except ValueError:
                seed = 0
            cands.append(
                {
                    "file": p.name,
                    "seed": seed,
                    "path": str(p),
                    "size": size,
                    "mtime": mt,
                    # 内容比对（不是文件名猜测）：与当前 ref 完全一致 → 这就是"当前那张"
                    "adopted": bool(ref_digest) and file_digest16(p) == ref_digest,
                }
            )
        sheet = proj.refs_dir / "_gacha" / name / "_sheet.jpg"
        rows.append(
            {
                "name": name,
                "prompt": prompt,
                "prompt_warnings": char_prompt_warnings(prompt),
                "ref_path": str(ref) if ref.is_file() else None,
                "ref_mtime": int(ref.stat().st_mtime) if ref.is_file() else 0,
                "ref_size": ref.stat().st_size if ref.is_file() else 0,
                # 没有定妆照时给 None（"不适用"），而不是 False ——
                # 否则新角色会被 UI 误标成"未同步到 ComfyUI"，其实它只是还没生成
                "ref_synced": ref_synced if ref_digest else None,
                "candidates": cands,
                "sheet_path": str(sheet) if sheet.is_file() else None,
                "gacha_raw": _count(proj.state_dir / "chars_raw", f"{name}_*.mp4"),
            }
        )
    return {"project": pdir.name, "chars": rows}


_SEED_RE = re.compile(r"^seed\d+\.png$")


def resolve_candidate(project: str | os.PathLike[str], name: str, file: str, root: Path | None = None) -> Path:
    """把 {name, file} 解析成候选图路径，并挡住目录穿越与乱文件名。"""
    if not CHAR_NAME_RE.match(name or ""):
        raise TaskError(f"非法角色名：{name!r}")
    if not _SEED_RE.match(file or ""):
        raise TaskError(f"非法候选文件名：{file!r}（应形如 seed2741.png）")
    proj = Project(resolve_project(project, root))
    d = (proj.refs_dir / "_gacha").resolve()
    p = (proj.refs_dir / "_gacha" / name / file).resolve()
    if d not in p.parents:
        raise TaskError("候选图路径越界")
    if not p.is_file():
        raise TaskError(f"候选图不存在：{name}/{file}")
    return p


def save_char_prompt(project: str | os.PathLike[str], name: str, text: str, root: Path | None = None) -> dict:
    """保存 prompts/char_<名>.txt（原子写，UTF-8 不转义中文）。"""
    if not CHAR_NAME_RE.match(name or ""):
        raise TaskError(f"非法角色名：{name!r}")
    proj = Project(resolve_project(project, root))
    proj.prompts_dir.mkdir(parents=True, exist_ok=True)
    f = proj.prompts_dir / f"char_{name}.txt"
    tmp = f.with_suffix(".txt.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text if text is not None else "")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, f)
    prompt = char_prompt(project, name, root)
    return {"name": name, "path": str(f), "prompt": prompt, "prompt_warnings": char_prompt_warnings(prompt)}


# ---------------------------------------------------------------- 项目摘要


def project_summary(project: str | os.PathLike[str], root: Path | None = None) -> dict:
    pdir = resolve_project(project, root)
    proj = Project(pdir)
    task = read_task(pdir)
    _eps = episodes(proj)
    # `final` 保留单个（取最新一集）兼容旧调用方；`finals` 给**完整列表**
    fin = {"name": _eps[-1]["name"], "size": _eps[-1]["size"], "mtime": _eps[-1]["mtime"]} if _eps else None
    return {
        "name": pdir.name,
        "path": str(pdir),
        "has_project_json": (pdir / "project.json").is_file(),
        "novel_chapters": _count(proj.novel_dir, "*.md") + _count(proj.novel_dir, "*.txt"),
        "shot_files": _count(proj.shots_dir, "*.json"),
        "clips": _count(proj.clips_dir, "*.mp4"),
        "refs": _count(proj.refs_dir, "*.png"),
        "char_prompts": _count(proj.prompts_dir, "char_*.txt"),
        "final": fin,
        "finals": _eps,          # ★ 逐章出集后可能有多集，UI 要能列全
        "episode_count": len(_eps),
        "running": task_running(task),
        "task_stage": (task or {}).get("stage", ""),
    }


# ---------------------------------------------------------------- 日志


def read_log(project: str | os.PathLike[str], offset: int = 0, limit: int = 262144, root: Path | None = None) -> dict:
    """
    增量读 run.log。按**字节**偏移，并保证不切在多字节 UTF-8 字符中间
    （否则中文日志会出现半个字的乱码，且下次偏移错位）。
    """
    p = log_path(project, root)
    if not p.is_file():
        return {"offset": int(offset), "next": 0, "text": "", "size": 0, "reset": bool(offset)}
    size = p.stat().st_size
    reset = False
    if offset < 0 or offset > size:  # 日志被新任务截断过
        offset = 0
        reset = True
    with open(p, "rb") as f:
        f.seek(offset)
        raw = f.read(limit)
    cut = 0
    text = ""
    while cut <= 3:
        try:
            text = raw[: len(raw) - cut].decode("utf-8") if cut else raw.decode("utf-8")
            break
        except UnicodeDecodeError:
            cut += 1
    else:  # 兜底：整段替换非法字节，保证 UI 永远拿得到字符串
        text, cut = raw.decode("utf-8", "replace"), 0
    return {
        "offset": int(offset),
        "next": int(offset + len(raw) - cut),
        "text": text,
        "size": int(size),
        "reset": reset,
    }


def log_tail(project: str | os.PathLike[str], lines: int = 60, root: Path | None = None) -> list[str]:
    p = log_path(project, root)
    if not p.is_file():
        return []
    try:
        with open(p, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            back = min(size, 32768)
            f.seek(size - back)
            raw = f.read()
        text = raw.decode("utf-8", "replace")
        return text.splitlines()[-lines:]
    except OSError:
        return []


# ---------------------------------------------------------------- 启动 / 停止


@dataclass
class TaskHandle:
    """一次已派生任务的句柄（Web 只要 pid；CLI 还要前台等待）。"""

    project: str
    pdir: Path
    stage: str
    proc: subprocess.Popen
    task: dict
    echo: bool = False
    rc: int | None = None
    _done: threading.Event = field(default_factory=threading.Event)

    @property
    def pid(self) -> int:
        return int(self.proc.pid)

    def public(self) -> dict:
        return task_public(self.task, self.project)

    # ---- 监督线程：收日志 + 收尾写 task.json

    def _supervise(self) -> None:
        lp = self.pdir / "state" / "run.log"
        try:
            with open(lp, "a", encoding="utf-8", buffering=1) as f:
                assert self.proc.stdout is not None
                for line in self.proc.stdout:
                    f.write(line)
                    if self.echo:
                        sys.stdout.write(line)
                        sys.stdout.flush()
            rc = self.proc.wait()
        except Exception as e:  # 监督线程自己绝不能把整个服务带走
            rc = -999
            print(f"[taskctl] 监督线程异常：{e}", flush=True)
        self.rc = rc
        self._finalize(rc)
        self._done.set()

    def _finalize(self, rc: int) -> None:
        """
        任务收尾：把 exit_code / finished_at / stopped 补进 task.json。
        用项目锁做读-改-写，避免和 stop() 的写入互相覆盖。
        """
        try:
            with _project_lock(self.pdir):
                cur = read_task(self.pdir) or {}
                if int(cur.get("pid") or 0) != self.pid:
                    return  # 已经被新任务顶掉，不要污染新记录
                stopped = bool(cur.get("stopped")) or rc in (-15, -2, 143, 130) or rc < 0
                cur["exit_code"] = rc
                cur["finished_at"] = int(time.time())
                cur["stopped"] = stopped
                if rc == 3:
                    cur["error"] = cur.get("error") or "依赖模块未就绪"
                write_task(self.pdir, cur)
        except Exception as e:
            print(f"[taskctl] 收尾写 task.json 失败：{e}", flush=True)

    def start(self) -> "TaskHandle":
        threading.Thread(target=self._supervise, name=f"vm-task-{self.pid}", daemon=True).start()
        return self

    def wait(self, timeout: float | None = None) -> int | None:
        self._done.wait(timeout)
        return self.rc

    def wait_foreground(self) -> int:
        """
        CLI 前台等待：把终端的 SIGINT/SIGTERM 转成"停 worker"，
        而不是让 Ctrl-C 直接杀掉 CLI、把 worker 丢成孤儿。
        """
        prev_int = signal.getsignal(signal.SIGINT)
        prev_term = signal.getsignal(signal.SIGTERM)
        stopping = threading.Event()

        def _forward(signum, frame):  # noqa: ARG001
            if stopping.is_set():
                print("\n[taskctl] 再收到一次中断，直接退出（worker 可能仍在跑）", flush=True)
                raise KeyboardInterrupt
            stopping.set()
            print(f"\n[taskctl] 收到信号 {signum}，停止任务 pid={self.pid} …", flush=True)
            try:
                stop(self.project, root=self.pdir.parent)
            except Exception as e:
                print(f"[taskctl] 停止失败：{e}", flush=True)

        try:
            signal.signal(signal.SIGINT, _forward)
            signal.signal(signal.SIGTERM, _forward)
            while not self._done.wait(0.5):
                pass
        except KeyboardInterrupt:
            self.rc = 130
        finally:
            signal.signal(signal.SIGINT, prev_int)
            signal.signal(signal.SIGTERM, prev_term)
        return int(self.rc if self.rc is not None else 0)


def _build_cmd(
    project: str,
    stage: str,
    *,
    only: list[str] | None,
    force: bool,
    dry: bool,
    fake_sleep: float,
    count: int = 0,
) -> list[str]:
    # -u：worker 的 stdout 是管道，不加 -u 会块缓冲，"实时日志"就变成"结束才刷一堆"
    cmd = [sys.executable, "-u", str(PIPELINE), project, "--stage", stage, "--_worker"]
    if only:
        cmd += ["--only", ",".join(only)]
    if count:
        cmd += ["--count", str(int(count))]
    if force:
        cmd += ["--force"]
    if dry:
        cmd += ["--dry-run"]
    if fake_sleep:
        cmd += ["--_fake-sleep", str(fake_sleep)]
    return cmd


def start_async(
    project: str,
    stage: str,
    *,
    only: list[str] | None = None,
    force: bool = False,
    dry: bool = False,
    fake_sleep: float = 0.0,
    count: int = 0,
    root: Path | None = None,
    echo: bool = False,
) -> TaskHandle:
    """
    启动一个任务（Web 与 CLI 共用）。同项目已有任务在跑 → TaskBusy。

    fake_sleep：自测/演示用的替身 —— 派生一个只 sleep N 秒的 worker，
    用来验证「并发保护」「停止真的停掉」。不参与任何真实渲染。
    count：抽卡张数（仅 gacha 阶段用；0 = 用默认值 6）
    """
    if stage not in STAGES:
        raise TaskError(f"未知阶段：{stage}（可选 {', '.join(STAGES)}）")
    pdir = resolve_project(project, root)
    Project(pdir).ensure()
    if fake_sleep <= 0:
        ready = stage_readiness(stage)
        if not ready["ready"]:
            raise ModuleNotReady(ready["message"])

    # ── E3 预算护栏（2026-09-24）────────────────────────────────────────────
    # 位置选在这里是因为 `start_async` 是 **Web 与 CLI 的唯一咽喉点** ——
    # 放在 UI 层会被绕过，放在 worker 里则已经开跑了（那就不是"刹车点"）。
    # 三条不拦：dry-run 不花钱；fake_sleep 是自测替身；没配 budget 的项目保持向后兼容。
    if not dry and fake_sleep <= 0:
        from vm import budget as _budget
        est = _budget.estimate(pdir, stage, only=only, count=count)
        chk = _budget.check(pdir, est)
        if chk.get("needs_approval"):
            ap_ = _budget.consume_approval(pdir)   # 一次性放行
            if not ap_:
                raise NeedsApproval({**chk, "stage": stage, "project": pdir.name})
        # 记一笔"已放行并开始"（成本台账，按日轮转）。
        # queue 阶段**不在这儿记**：它的 est 是整个队列的预估，真正跑多少取决于
        # 之后逐个消费的作业 —— queue.drain 会按作业实际张数逐笔记账，
        # 这里再记一笔整队列预估就是重复计费（今日已用会翻倍）。
        if stage != "queue":
            _budget.record(pdir, stage, est, ok=True, note="started")

    with _project_lock(pdir):
        cur = read_task(pdir)
        if task_running(cur):
            age = int(time.time()) - int(cur.get("started_at") or time.time())
            raise TaskBusy(
                f"项目「{pdir.name}」已有任务在跑：stage={cur.get('stage')} pid={cur.get('pid')} "
                f"已运行 {age}s。同一项目同时只允许一个任务，请先停止或等它结束。"
            )
        # 开新 run.log：UI 按 offset 增量拉，日志被截断时它会收到 reset 标记
        lp = pdir / "state" / "run.log"
        started = int(time.time())
        task = {
            "project": pdir.name,
            "stage": stage,
            "shot": "",
            "pid": 0,
            "started_at": started,
            "stopped": False,
            "finished_at": None,
            "exit_code": None,
            "only": list(only or []),
            "count": int(count or 0),
            "force": bool(force),
            "dry": bool(dry),
            "fake_sleep": float(fake_sleep or 0),
            "error": "",
        }
        cmd = _build_cmd(pdir.name, stage, only=only, force=force, dry=dry,
                         fake_sleep=fake_sleep, count=count)
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        env["VM_TASK_MODE"] = "1"
        # ── 日志改为**追加**而不是截断（2026-09-24）─────────────────────────
        # 原来这里是 `open(lp, "w")` —— 每开一个任务就清掉上一次的日志。
        # 抽卡这类**高频小任务**（点一下起一个 queue 任务）下，后果是：
        # 记录确实写了，但**下一次点击就把它清掉**，用户看到的是"点了没日志"。
        # 轮转上限 2MB —— 保留历史，又不让文件无限长。
        MAX_LOG_BYTES = 2 << 20
        try:
            if lp.exists() and lp.stat().st_size > MAX_LOG_BYTES:
                lp.replace(lp.with_suffix(".log.1"))
        except OSError:
            pass
        logf = open(lp, "a", encoding="utf-8", buffering=1)
        try:
            logf.write(f"\n===== 任务开始 {time.strftime('%Y-%m-%d %H:%M:%S')} stage={stage} "
                       f"only={only or '-'} force={force} dry={dry} =====\n")
            logf.flush()
            proc = subprocess.Popen(
                cmd,
                cwd=str(WORKSPACE),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                env=env,
                start_new_session=True,  # 自己建会话：既不被终端信号误伤，也便于精确整组停止
            )
        except Exception:
            logf.close()
            raise
        finally:
            logf.close()
        task["pid"] = proc.pid
        write_task(pdir, task)
        handle = TaskHandle(
            project=pdir.name, pdir=pdir, stage=stage, proc=proc, task=task, echo=echo
        )
    # 临界区外再起监督线程：避免线程抢锁造成自我等待
    return handle.start()


def run_foreground(
    project: str,
    stage: str,
    *,
    only: list[str] | None = None,
    force: bool = False,
    dry: bool = False,
    fake_sleep: float = 0.0,
    count: int = 0,
    root: Path | None = None,
) -> int:
    """CLI 路径：派生同一个 worker，然后前台等待并转发信号。"""
    handle = start_async(
        project, stage, only=only, force=force, dry=dry, fake_sleep=fake_sleep,
        count=count, root=root, echo=True,
    )
    print(f"[taskctl] 任务已启动：pid={handle.pid} stage={stage} 日志={log_path(handle.pdir)}", flush=True)
    print("[taskctl] 停止：Ctrl-C，或 Web UI 的「停止」按钮", flush=True)
    return handle.wait_foreground()


def stop(project: str, *, root: Path | None = None, wait: float = STOP_WAIT) -> dict:
    """
    停止当前任务：只对 task.json 里记录的精确 pid 发 SIGTERM。

    绝不做的事（用户踩过坑）：
      · pkill -f  —— 会匹配到自己的命令行，把自己杀掉
      · 按端口杀  —— 会误杀用户常驻服务
    """
    pdir = resolve_project(project, root)
    with _project_lock(pdir):
        cur = read_task(pdir)
        if not cur or not cur.get("pid"):
            return {"ok": False, "reason": "no_task", "message": f"项目「{pdir.name}」没有任务记录"}
        pid = int(cur.get("pid") or 0)
        if not pid_alive(pid):
            cur["stopped"] = True
            cur["finished_at"] = cur.get("finished_at") or int(time.time())
            write_task(pdir, cur)
            return {"ok": False, "reason": "already_done", "pid": pid, "message": f"任务已结束（pid={pid} 不存在）"}
        if not is_our_worker(pid, pdir.name):
            # pid 被系统回收给了别的进程 —— 绝不发信号
            return {
                "ok": False,
                "reason": "pid_reused",
                "pid": pid,
                "message": f"拒绝停止：pid={pid} 的命令行不像本项目的 worker（可能已被系统复用），"
                           f"为避免误杀其他进程不发送任何信号。可手动核对该 pid。",
            }
        cur["stopped"] = True
        cur["stop_pid"] = pid
        write_task(pdir, cur)

    how = _signal_worker(pid)
    deadline = time.time() + max(0.0, wait)
    while time.time() < deadline and pid_alive(pid):
        time.sleep(0.2)
    alive = pid_alive(pid)
    with _project_lock(pdir):
        cur = read_task(pdir) or {}
        if int(cur.get("pid") or 0) == pid:
            cur["stopped"] = True
            write_task(pdir, cur)
    return {
        "ok": True,
        "pid": pid,
        "signal": "SIGTERM",
        "target": how,
        "alive": alive,
        "message": (f"已向 pid={pid} 发送 SIGTERM（精确 pid，非 pkill/非按端口），进程已退出"
                    if not alive else
                    f"已向 pid={pid} 发送 SIGTERM，进程仍在退出中（{wait:.0f}s 内未消失）"),
    }


# ---------------------------------------------------------------- 状态聚合


def status(project: str, root: Path | None = None, tail_lines: int = 60) -> dict:
    """/api/status 的全部内容都在这里算，web.py 只做序列化。"""
    pdir = resolve_project(project, root)
    proj = Project(pdir)
    task = read_task(pdir)
    pub = task_public(task, pdir.name)
    prog = read_progress(pdir)
    stage = pub["stage"] or "all"
    derived = derive_progress(pdir, stage)
    # 若 worker 自报了当前镜头（可选），以它为准
    current_shot = (prog or {}).get("current") or pub.get("shot") or derived.get("current", "")
    _eps = episodes(proj)
    # `final` 保留单个（取最新一集）兼容旧调用方；`finals` 给**完整列表**
    final_info = {"name": _eps[-1]["name"], "size": _eps[-1]["size"],
                  "mtime": _eps[-1]["mtime"]} if _eps else None
    readiness = {s: stage_readiness(s) for s in STAGES}
    return {
        "project": pdir.name,
        "path": str(pdir),
        "task": pub,
        "running": pub["running"],
        "stage": stage if pub["running"] else (stage if pub["started_at"] else ""),
        "shot": current_shot,
        "progress": derived,
        "progress_reported": prog,
        "readiness": readiness,
        "final": final_info,
        "finals": _eps,          # ★ 逐章出集后可能有多集，UI 要能列全
        "episode_count": len(_eps),
        "log_size": (log_path(pdir).stat().st_size if log_path(pdir).exists() else 0),
        "log_tail": log_tail(pdir, tail_lines),
    }
