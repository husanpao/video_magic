"""
web.py —— 极简 Web UI（零框架、零构建、纯标准库）。

设计取向：**能看 + 能点**。
    能看：阶段进度条 / 实时日志（增量拉取）/ 镜头网格（缩略图 + 三态 + 质检）/ 成片播放下载
    能点：启动各阶段 / 停止 / 单镜重渲

但它**自己不跑任务**：所有启动/停止都调 vm/taskctl.py，
所以 Web 和 CLI 用的永远是同一份任务状态（`<项目>/state/task.json`），
不会出现"两边各跑一份、互相覆盖产物"。

依赖并行开发中的模块（shots/gen/qc/…）时一律走 taskctl 的延迟导入：
缺模块 → /api/run 返回 503 + 人话原因，UI 照常打开。
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from vm import taskctl
from vm import config as vm_config  # T3 配置中心：/api/config 委托给它，本层只做参数校验+错误映射
from vm.state import Project

STATIC_DIR = Path(__file__).resolve().parent / "static"
INDEX_HTML = STATIC_DIR / "index.html"

# ── 前端构建产物（Vue 3 + Vite + Element Plus）──────────────────────────────
# 2026-09-23 起 UI 重构成 Vue 工程（源码 web/，`npm run build` 输出到 static/dist）。
# 迁移期策略：**dist 存在就用它，否则回退旧的单文件 index.html** ——
# 这样构建失败也不会让控制台变成白屏。
DIST_DIR = STATIC_DIR / "dist"
DIST_INDEX = DIST_DIR / "index.html"
# Vite 产物里的静态资源路径，白名单防止目录穿越
ASSET_RE = re.compile(r"^/assets/([A-Za-z0-9_.\-]+)$")

MAX_BODY = 1 << 20        # 1MB：普通 JSON（/api/run 之类）足够
MAX_UPLOAD_BODY = 24 << 20  # 24MB：上传自有定妆图走 base64，会膨胀 4/3
SHOT_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")

_FALLBACK_HTML = (
    "<!doctype html><meta charset='utf-8'><title>漫剧流水线</title>"
    "<h1>漫剧流水线</h1><p>vm/static/index.html 缺失，UI 不可用；API 仍可访问："
    "/api/projects /api/status /api/shots /api/log</p>"
)


# ---------------------------------------------------------------- 响应辅助


class _Abort(Exception):
    """带 HTTP 状态码的提前返回。issues 用于把"为什么拒绝"的问题清单带给 UI。"""

    def __init__(self, code: int, error: str, message: str, issues: list | None = None,
                 extra: dict | None = None):
        super().__init__(message)
        self.code = code
        self.error = error
        self.message = message
        self.issues = list(issues or [])
        # extra：把结构化载荷一并带给 UI（E3 的"暂停等人批"要它 —— 载荷里带着
        # 已花多少 / 卡在哪步 / 再放行多少，UI 才能直接弹对话框而不用去翻日志）。
        self.extra = dict(extra or {})


def _log_exc(where: str) -> None:
    """
    服务端异常必须留栈到 stderr —— 不留栈就没法排障。

    为什么不走 logging：本文件其余输出（log_message 等）都是 print 直写，
    保持同一出口（stderr）保证「制造服务端异常 → 500 且日志有栈」可以被验证。
    """
    print(f"[web] 服务端异常 @ {where}", file=sys.stderr, flush=True)
    traceback.print_exc(file=sys.stderr)


# 任务层抛出的「已知输入错误」：请求本身有问题，映射成 4xx + 人话。
# 为什么单列一份：以前 upload/adopt 把**一切**异常折叠成 400，服务端 bug
# （磁盘满、代码缺陷）伪装成「请求错误」，排障失真。现在的纪律是：
#   已知输入错误（TaskError / ValueError / FileNotFoundError）→ 4xx；
#   其余异常 → 不在 handler 里折叠，冒到 _dispatch 兜底 → 500 server_error + 留栈。
_CLIENT_ERRORS = (taskctl.TaskError, ValueError, FileNotFoundError)


def _thumb_path(proj: Project, key: str) -> Path:
    return proj.state_dir / "thumbs" / f"{key}.jpg"


def _thumb_safe_key(key: str) -> str:
    # key 里可能带 `/`（例如 gacha/孙悟空/seed2741），换成 `__` 保证是单个文件名
    return re.sub(r"[^A-Za-z0-9_.\-]", "_", key.replace("/", "__"))


def _thumb_cached(proj: Project, key: str, src: Path) -> Path | None:
    """缓存命中（存在 + 不比源文件旧 + 非空）就返回缩略图路径，否则 None。"""
    dst = _thumb_path(proj, _thumb_safe_key(key))
    try:
        if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime and dst.stat().st_size > 0:
            return dst
    except OSError:
        pass
    return None


def _make_thumb(proj: Project, key: str, src: Path) -> Path | None:
    """
    抽一帧做缩略图，缓存在 state/thumbs/<key>.jpg。

    既能处理片段（clips/*.mp4）也能处理静图（定妆照/抽卡候选），
    因为 ffmpeg 对单张 PNG 也会输出一帧。

    注意：只读项目产物，只往本项目 state/ 写 —— 绝不碰 ComfyUI 的 output/。
    ⚠️ 里面有 ffmpeg（最坏 25s 超时）——**不要在请求线程直接调**，
    走 `_thumb_request()` 的后台线程池（S9：缩略图异步化）。
    """
    hit = _thumb_cached(proj, key, src)
    if hit is not None:
        return hit
    dst = _thumb_path(proj, _thumb_safe_key(key))
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.stem + ".tmp.jpg")
    # 只有视频才需要 -ss 跳开头：对单张静图 seek 0.5s 会"跳过唯一一帧"，
    # ffmpeg 退出码仍是 0 但一个文件都不产出（实测踩过），所以按扩展名分流。
    is_video = src.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm", ".avi")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if is_video:
        cmd += ["-ss", "0.5"]
    cmd += ["-i", str(src), "-frames:v", "1", "-vf", "scale=360:-2", "-q:v", "4", str(tmp)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=25, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0 or not tmp.exists():
        try:
            tmp.unlink()
        except OSError:
            pass
        return None
    os.replace(tmp, dst)
    return dst


# ── 缩略图异步化（S9）────────────────────────────────────────────────────────
# 为什么：_make_thumb 里的 ffmpeg 最坏 25s —— 同步跑在请求线程上时，首屏 52 张
# 缩略图能把整个 Web 拖死（诊断 2.7）。现在的策略：
#   · 缓存命中 → 直接给（绝大多数二次加载）；
#   · 未命中 → 丢给小线程池后台生成（single-flight 去重，同 key 不重复跑 ffmpeg；
#     2 并发上限，52 张也不会把机器轰爆），请求**最多等 _THUMB_WAIT 秒**；
#   · 等到了 → 给真图（静图普遍 <1s，正常体验不变）；
#   · 等不到 → 给占位图 + `X-Thumb-Pending: 1` + no-store（前端可延迟换 URL 重试），
#     绝不再让请求线程扛满 25s。
_THUMB_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="vm-thumb")
_THUMB_FUTURES: dict[str, Future] = {}
_THUMB_FUTURES_LOCK = threading.Lock()
_THUMB_WAIT = 2.5

# 占位图：360×240 深灰 JPEG（740 字节，ffmpeg color 源生成后内嵌 —— 纯标准库
# 没有编码器可用，内嵌字节是唯一不引入依赖的做法）
_PLACEHOLDER_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAgAAAQABAAD//gARTGF2YzU4LjEzNC4xMDAA/9sAQwAIDAwODA4QEBAQEBAT"
    "EhMUFBQTExMTFBQUFRUVGRkZFRUVFBQVFRgYGRkbHBsaGhkaHBweHh4kJCIiKiorMzM+/8QATAAB"
    "AQAAAAAAAAAAAAAAAAAAAAcBAQEAAAAAAAAAAAAAAAAAAAACEAEAAAAAAAAAAAAAAAAAAAAAEQEA"
    "AAAAAAAAAAAAAAAAAAAA/8AAEQgA8AFoAwEiAAIRAAMRAP/aAAwDAQACEQMRAD8AnICwAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB/9k="
)


def _thumb_request(proj: Project, key: str, src: Path) -> tuple[Path | None, bool]:
    """
    拿缩略图（异步化入口）。返回 (文件, pending)：
      (Path, False)  → 真图就绪，直接发；
      (None, True)   → 后台还在生成，调用方发占位图 + X-Thumb-Pending: 1；
      (None, False)  → 生成失败（ffmpeg 不可用/文件损坏），调用方报 thumb_failed。
    """
    hit = _thumb_cached(proj, key, src)
    if hit is not None:
        return hit, False
    fkey = f"{proj.root}|{key}|{src}"
    with _THUMB_FUTURES_LOCK:
        fut = _THUMB_FUTURES.get(fkey)
        if fut is None or fut.done():
            # single-flight：同一个 key 只跑一个 ffmpeg；做完了下次要图再重试失败
            fut = _THUMB_POOL.submit(_make_thumb, proj, key, src)
            _THUMB_FUTURES[fkey] = fut
            if len(_THUMB_FUTURES) > 256:   # 顺手清掉已完成的，别让表无限长
                for k in [k for k, v in _THUMB_FUTURES.items() if v.done() and k != fkey]:
                    _THUMB_FUTURES.pop(k, None)
    try:
        return fut.result(timeout=_THUMB_WAIT), False
    except FutTimeout:
        return None, True
    except Exception:  # noqa: BLE001 —— 后台炸了不许把请求线程带走
        _log_exc(f"thumb {key}")
        return None, False


def _resolve_shot_clip(proj: Project, shot: str) -> Path:
    if not SHOT_RE.match(shot or ""):
        raise _Abort(400, "bad_shot", f"非法镜头号：{shot!r}")
    clip = (proj.clips_dir / f"{shot}.mp4").resolve()
    if proj.clips_dir.resolve() not in clip.parents:
        raise _Abort(400, "bad_shot", "镜头号越界")
    if not clip.is_file():
        raise _Abort(404, "no_clip", f"还没有这个镜头的产物：{clip.name}")
    return clip


_RAW_FILE_RE = re.compile(r"^[^/\\\x00]{1,120}\.(mp4|png|jpg|jpeg|webp)$", re.I)

# 图片魔数：不接受"随便什么文件改个名"，免得把非图片塞进 refs/ 之后渲染才炸
_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"RIFF", ".webp"),  # 还需第 8-12 字节是 WEBP
)


def _decode_image_b64(b64: object) -> bytes:
    """
    解 base64（容忍 data:image/png;base64, 前缀），并做魔数校验。

    上传的是用户自己的图，宁可在入口严格一点：非图片直接 400，
    不要等到渲染时才因为 LoadImage 读不了而失败。
    """
    if not isinstance(b64, str) or not b64.strip():
        raise _Abort(400, "bad_upload", "缺少 b64（图片内容）")
    s = b64.strip()
    if s.startswith("data:"):
        _, _, s = s.partition(",")
    try:
        raw = base64.b64decode(re.sub(r"\s+", "", s), validate=True)
    except (binascii.Error, ValueError) as e:
        raise _Abort(400, "bad_upload", f"base64 解码失败：{e}") from e
    if not raw:
        raise _Abort(400, "bad_upload", "图片内容为空")
    if len(raw) > 16 << 20:
        raise _Abort(413, "too_large", f"图片太大：{len(raw)/1048576:.1f}MB（上限 16MB）")
    ok = any(raw.startswith(m) for m, _ in _IMAGE_MAGIC) and not (raw.startswith(b"RIFF") and raw[8:12] != b"WEBP")
    if not ok:
        raise _Abort(400, "bad_upload", "不是 PNG/JPEG/WebP 图片（按文件头判断）")
    return raw


def _resolve_view_target(proj: Project, kind: str, name: str, file: str) -> tuple[Path, str, str]:
    """
    `/view?kind=…` 的路径解析 + 目录穿越防护。

    返回 (实际文件, Content-Type, 缩略图 key)。
    """
    kind = (kind or "").strip()

    def _inside(base: Path, p: Path) -> Path:
        base = base.resolve()
        p = p.resolve()
        if base != p and base not in p.parents:
            raise _Abort(400, "bad_path", "路径越界")
        if not p.is_file():
            raise _Abort(404, "not_found", f"文件不存在：{p.name}")
        return p

    if kind == "ref":
        if not taskctl.CHAR_NAME_RE.match(name or ""):
            raise _Abort(400, "bad_name", f"非法角色名：{name!r}")
        p = _inside(proj.refs_dir, proj.refs_dir / f"char_{name}.png")
        return p, "image/png", f"ref__{name}"
    if kind == "sheet":
        if not taskctl.CHAR_NAME_RE.match(name or ""):
            raise _Abort(400, "bad_name", f"非法角色名：{name!r}")
        p = _inside(proj.refs_dir / "_gacha" / name, proj.refs_dir / "_gacha" / name / "_sheet.jpg")
        return p, "image/jpeg", f"sheet__{name}"
    if kind == "gacha":
        if not taskctl.CHAR_NAME_RE.match(name or "") or not re.match(r"^seed\d+\.png$", file or ""):
            raise _Abort(400, "bad_file", f"非法候选图：{name!r}/{file!r}")
        base = proj.refs_dir / "_gacha" / name
        p = _inside(base, base / file)
        return p, "image/png", f"gacha__{name}__{file[:-4]}"
    if kind == "storyboard":
        # 分镜图（Qwen-Image 预演）。shot id 复用 SHOT_RE 校验，且必须落在 storyboard/ 内。
        if not SHOT_RE.match(name or ""):
            raise _Abort(400, "bad_shot", f"非法镜头号：{name!r}")
        base = Path(proj.root) / "storyboard"
        p = _inside(base, base / f"{name}.png")
        return p, "image/png", f"sb__{name}"
    if kind == "asset":
        # 场景/道具概念图：name 形如 scene_S1 / prop_P1，file 形如 seed1234.png
        if not re.match(r"^(scene|prop)_[A-Za-z0-9_\-]{1,32}$", name or ""):
            raise _Abort(400, "bad_name", f"非法素材名：{name!r}")
        # 候选文件名有两类：抽卡出的 `seed<数字>.png`、上传的 `upload_<时间>_<原名>`
        if not re.match(r"^(seed\d+|upload_\d+_[A-Za-z0-9_.\-]{1,60})\.(png|jpg|jpeg|webp)$",
                        file or "", re.I):
            raise _Abort(400, "bad_file", f"非法候选文件名：{file!r}")
        base = Path(proj.root) / "assets" / name
        p = _inside(base, base / file)
        return p, "image/png", f"asset__{name}__{file[:-4]}"
    if kind == "char_raw":
        if not _RAW_FILE_RE.match(file or ""):
            raise _Abort(400, "bad_file", f"非法文件名：{file!r}")
        p = _inside(proj.state_dir / "chars_raw", proj.state_dir / "chars_raw" / file)
        ctype = "video/mp4" if p.suffix.lower() == ".mp4" else "image/" + p.suffix.lstrip(".").lower()
        return p, ctype, f"charraw__{p.stem}"
    raise _Abort(400, "bad_kind", f"未知 kind：{kind!r}（应为 ref/gacha/sheet/char_raw/storyboard）")


# ---------------------------------------------------------------- 路由表
#
# 为什么表化：原来 do_GET/do_POST 是 48 个 if 手工配对 —— `/api/budget` 写好了
# handler 却忘了接线，前端调用恒 404（B1 死路由）。现在「路径 → handler 方法名」
# 一张表、分发只有一处；再配 `_verify_routes` 启动自检（每个 api_* 必须已注册、
# 每个表项必须真有方法），"写 handler 忘接线"在**启动时**就报错，不用等用户点到。
#
# 值用「方法名字符串」而不是函数对象：handler 类在 make_handler() 里才成形
# （闭包绑 projects_root），模块加载时拿不到函数对象；字符串查表照样能自检。

ROUTES_GET = {
    "/": "page_index",
    "/index.html": "page_index",
    "/favicon.ico": "page_favicon",
    "/api/projects": "api_projects",
    "/api/status": "api_status",
    "/api/completeness": "api_completeness",
    "/api/script": "api_script",
    "/api/chapters": "api_chapters",
    "/api/storyboard": "api_storyboard",
    "/api/scenes": "api_scenes",
    "/api/reset/preview": "api_reset_preview",
    "/api/queue": "api_queue",
    "/api/assets": "api_assets",
    "/api/props": "api_props",
    "/api/budget": "api_budget",
    "/api/config": "api_config",
    "/api/shots": "api_shots",
    "/api/log": "api_log",
    "/api/chars": "api_chars",
    "/api/shot": "api_shot",
    "/api/qc": "api_qc",
    "/api/audit": "api_audit",
    "/view": "view",
}

ROUTES_POST = {
    "/api/reset": "api_reset",
    "/api/scene/update": "api_scene_update",
    "/api/prop/update": "api_prop_update",
    "/api/queue/add": "api_queue_add",
    "/api/queue/cancel": "api_queue_cancel",
    "/api/queue/clear": "api_queue_clear",
    "/api/asset/gen": "api_asset_gen",
    "/api/asset/upload": "api_asset_upload",
    "/api/asset/adopt": "api_asset_adopt",
    "/api/approve": "api_approve",
    "/api/config": "api_config_save",
    "/api/config/validate": "api_config_validate",
    "/api/run": "api_run",
    "/api/stop": "api_stop",
    "/api/rerender": "api_rerender",
    "/api/chars/gacha": "api_chars_gacha",
    "/api/chars/adopt": "api_chars_adopt",
    "/api/chars/upload": "api_chars_upload",
    "/api/chars/prompt": "api_chars_prompt",
    # P1：镜头编辑（全部走 taskctl，Web 自己不做业务逻辑）
    "/api/shot/lock": "api_shot_lock",
    "/api/shot/update": "api_shot_update",
    "/api/shot/rewrite": "api_shot_rewrite",
    "/api/shot/split": "api_shot_split",
    "/api/shot/merge": "api_shot_merge",
    "/api/shot/insert": "api_shot_insert",
    "/api/shot/delete": "api_shot_delete",
    "/api/shot/undo": "api_shot_undo",
    "/api/shots/bulk": "api_shots_bulk",
    "/api/shots/renumber": "api_shots_renumber",
}

# 唯一的「前缀路由」：Vite 产物 /assets/<file>（文件名带 hash，无法逐个精确登记）。
# 除此之外一律精确匹配 —— 前缀路由是目录穿越的高发区，只留这一个口子。
PREFIX_ROUTES_GET = {"/assets/": "page_asset"}

# 需要放宽请求体上限的路由（上传走 base64，会膨胀 4/3）
BODY_LIMITS = {"/api/chars/upload": MAX_UPLOAD_BODY}


def _verify_routes(cls) -> None:
    """
    启动自检：路由表 ↔ handler 方法必须一一对应，漏一个都当场报错。

    两个方向都查（B1 的根因是"只查了一个方向"甚至没查）：
      · 表项指向不存在的方法 —— 改名/删方法忘更新表；
      · api_* 方法没进任何表 —— 新写 handler 忘接线。
    """
    problems: list[str] = []
    registered: set[str] = set()
    for table in (ROUTES_GET, ROUTES_POST, PREFIX_ROUTES_GET):
        for path, name in table.items():
            registered.add(name)
            if not callable(getattr(cls, name, None)):
                problems.append(f"路由 {path} → {name}：Handler 上没有这个方法")
    for name in sorted(dir(cls)):
        if name.startswith("api_") and name not in registered:
            problems.append(f"handler {name} 没有注册进路由表（写 handler 忘接线？）")
    if problems:
        raise RuntimeError("路由自检失败：\n  " + "\n  ".join(problems))


# ---------------------------------------------------------------- Handler


def make_handler(projects_root: Path, default_project: str | None = None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "vm-web/1.0"
        protocol_version = "HTTP/1.1"
        timeout = 60

        # -- 基础设施

        def log_message(self, fmt, *args):  # noqa: A003
            if os.environ.get("VM_WEB_DEBUG"):
                print(f"[web] {self.address_string()} {fmt % args}", flush=True)

        def _json(self, obj, code: int = 200) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _fail(self, e: _Abort) -> None:
            """_Abort → JSON。带问题清单（拒绝保存的原因）时一并返回，UI 直接列给用户看。"""
            payload = {"ok": False, "error": e.error, "message": e.message}
            if e.issues:
                payload["issues"] = e.issues
            if getattr(e, "extra", None):
                payload.update(e.extra)   # 结构化载荷（E3 的待批准信息）一并带给 UI
            self._json(payload, e.code)

        def _static_asset(self, path: str) -> None:
            """托管 Vite 构建产物（/assets/*.js|css|...）。

            安全：只接受 `[A-Za-z0-9_.-]+` 的单段文件名，且**必须**解析后仍在
            DIST_DIR/assets 内 —— 否则 `..%2f` 之类能读到任意文件。
            """
            m = ASSET_RE.match(path)
            if not m:
                return self._fail_json(404, "not_found", "非法资源路径")
            base = (DIST_DIR / "assets").resolve()
            f = (base / m.group(1)).resolve()
            if base not in f.parents or not f.is_file():
                return self._fail_json(404, "not_found", f"资源不存在：{path}")
            ctype = {
                ".js": "text/javascript; charset=utf-8",
                ".mjs": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".map": "application/json; charset=utf-8",
                ".svg": "image/svg+xml",
                ".woff2": "font/woff2",
                ".woff": "font/woff",
                ".png": "image/png",
            }.get(f.suffix, "application/octet-stream")
            data = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            # Vite 产物文件名带内容 hash → 可长缓存
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self.end_headers()
            self.wfile.write(data)

        def _fail_json(self, code: int, err: str, msg: str) -> None:
            self._json({"ok": False, "error": err, "message": msg}, code)

        def _html(self, text: str, code: int = 200) -> None:
            body = text.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _file(self, path: Path, ctype: str, *, download: bool = False) -> None:
            """
            带 Range 支持的静态发送 —— <video> 拖进度条、断点续传都要它。
            """
            size = path.stat().st_size
            start, end = 0, size - 1
            code = 200
            rng = self.headers.get("Range", "")
            if rng.startswith("bytes="):
                spec = rng[6:].split(",")[0].strip()
                try:
                    a, _, b = spec.partition("-")
                    if a == "":  # bytes=-N 取末尾 N 字节
                        n = int(b)
                        start, end = max(0, size - n), size - 1
                    else:
                        start = int(a)
                        end = int(b) if b else size - 1
                    if start > end or start >= size:
                        raise ValueError
                    code = 206
                except ValueError:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
            length = end - start + 1
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-cache")
            if code == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            if download:
                self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
            self.end_headers()
            with open(path, "rb") as f:
                f.seek(start)
                remain = length
                while remain > 0:
                    chunk = f.read(min(262144, remain))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    remain -= len(chunk)

        # -- 参数解析

        def _query(self) -> dict:
            q = urllib.parse.urlparse(self.path).query
            return {k: v[-1] for k, v in urllib.parse.parse_qs(q).items()}

        def _project_arg(self, q: dict) -> str:
            name = q.get("project") or default_project or ""
            if not name:
                names = taskctl.discover_projects(projects_root)
                if not names:
                    raise _Abort(400, "no_project", f"projects/ 下没有项目：{projects_root}")
                name = names[0]
            return name

        def _body(self, limit: int | None = None) -> dict:
            max_len = MAX_BODY if limit is None else limit
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            if n > max_len:
                raise _Abort(413, "too_large", f"请求体过大（> {max_len // 1024 // 1024}MB）")
            raw = self.rfile.read(n)
            try:
                d = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                raise _Abort(400, "bad_json", f"请求体不是合法 JSON：{e}") from e
            if not isinstance(d, dict):
                raise _Abort(400, "bad_json", "请求体必须是 JSON 对象")
            return d

        # -- 路由（查表分发，见文件头 ROUTES_*）

        def do_GET(self):  # noqa: N802
            self._dispatch("GET", ROUTES_GET, PREFIX_ROUTES_GET)

        def do_POST(self):  # noqa: N802
            self._dispatch("POST", ROUTES_POST)

        def _dispatch(self, method: str, table: dict, prefixes: dict | None = None):
            """
            查表分发 + 唯一的错误边界。

            错误分流的纪律（S6）：
              · _Abort 带着明确类别与状态码直接走（4xx / 409 / 502 / 503）；
              · 任务层「已知输入错误」在各 handler 里转成 _Abort(4xx)；
              · 其余异常 = 服务端 bug → 500 server_error，且**必须**留栈到 stderr
                （`_log_exc`）—— 否则排障时只剩一行 message，失真。
            """
            try:
                path = urllib.parse.urlparse(self.path).path
                name = table.get(path)
                if name is None and prefixes:
                    for pre, pname in prefixes.items():
                        if path.startswith(pre):
                            name = pname
                            break
                if name is None:
                    raise _Abort(404, "not_found", f"没有这个路径：{path}")
                if method == "POST":
                    return getattr(self, name)(self._body(BODY_LIMITS.get(path)))
                return getattr(self, name)()
            except _Abort as e:
                self._fail(e)
            except BrokenPipeError:
                pass
            except _CLIENT_ERRORS as e:
                # 已知输入错误的漏网之鱼（handler 没显式映射的 TaskError/ValueError
                # 等）仍然是「请求不对」→ 4xx 人话，不能混进 500 冒充服务端故障
                self._fail(_Abort(400, "bad_request", str(e)))
            except Exception as e:  # 任何异常都要变成 JSON，别让 UI 拿到半截响应
                _log_exc(f"{method} {self.path}")
                self._json({"ok": False, "error": "server_error", "message": f"{type(e).__name__}: {e}"}, 500)

        # -- 页面/静态

        def page_index(self):
            # Vue 产物优先；没有就回退旧单文件
            page = DIST_INDEX if DIST_INDEX.is_file() else INDEX_HTML
            text = page.read_text(encoding="utf-8") if page.is_file() else _FALLBACK_HTML
            return self._html(text)

        def page_favicon(self):
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def page_asset(self):
            return self._static_asset(urllib.parse.urlparse(self.path).path)

        # -- API 实现

        def api_projects(self):
            names = taskctl.discover_projects(projects_root)
            items = []
            for n in names:
                s = taskctl.project_summary(n, projects_root)
                try:
                    s["progress"] = taskctl.derive_progress(n, s.get("task_stage") or "all", projects_root)
                except Exception:
                    # 单个项目进度推导失败不该拖垮整个列表，但要留栈 —— 静默给 0 会骗人
                    _log_exc(f"derive_progress({n})")
                    s["progress"] = {"pct": 0, "done": 0, "total": 0, "label": ""}
                items.append(s)
            self._json({"ok": True, "root": str(projects_root), "default": default_project or (names[0] if names else ""), "projects": items})

        def api_status(self):
            q = self._query()
            name = self._project_arg(q)
            st = taskctl.status(name, projects_root, tail_lines=60)
            st["ok"] = True
            st["now"] = int(time.time())
            self._json(st)

        def api_scene_update(self, body: dict):
            """POST /api/scene/update：改场景实体（含 description —— 它决定出图提示词）。"""
            name = str(body.get("project") or default_project or "")
            if not name:
                raise _Abort(400, "no_project", "请求里必须带 project")
            sid = str(body.get("id") or "")
            patch = body.get("patch") if isinstance(body.get("patch"), dict) else {
                k: v for k, v in body.items() if k not in ("project", "id")
            }
            try:
                self._json({"ok": True, **taskctl.update_scene(name, sid, patch, projects_root)})
            except taskctl.TaskError as e:
                raise _Abort(400, "bad_request", str(e)) from e

        def api_prop_update(self, body: dict):
            """POST /api/prop/update：改道具实体（含 description）。"""
            name = str(body.get("project") or default_project or "")
            if not name:
                raise _Abort(400, "no_project", "请求里必须带 project")
            pid = str(body.get("id") or "")
            patch = body.get("patch") if isinstance(body.get("patch"), dict) else {
                k: v for k, v in body.items() if k not in ("project", "id")
            }
            try:
                self._json({"ok": True, **taskctl.update_prop(name, pid, patch, projects_root)})
            except taskctl.TaskError as e:
                raise _Abort(400, "bad_request", str(e)) from e

        def api_reset_preview(self):
            """
            GET /api/reset/preview?project=X&scope=Y

            **先摆清单再问"确定吗"** —— 直接弹"确定清空吗"等于没问：
            用户不知道会删掉多少、什么会被保留、能不能恢复。
            """
            q = self._query()
            name = self._project_arg(q)
            scope = str(q.get("scope") or "shots")
            try:
                self._json({"ok": True, **taskctl.reset_preview(name, scope, projects_root)})
            except taskctl.TaskError as e:
                raise _Abort(400, "bad_request", str(e)) from e

        def api_reset(self, body: dict):
            """POST /api/reset：清空项目产物（不可逆 → 要手输项目名 + 自动备份元数据）。"""
            name = str(body.get("project") or default_project or "")
            if not name:
                raise _Abort(400, "no_project", "请求里必须带 project")
            scope = str(body.get("scope") or "shots")
            confirm = str(body.get("confirm") or "")
            try:
                r = taskctl.reset_project(name, scope, confirm=confirm, root=projects_root)
            except taskctl.TaskError as e:
                raise _Abort(400, "bad_request", str(e)) from e
            self._json({"ok": True, **r})

        def api_queue(self):
            """GET /api/queue：作业队列状态（待处理/进行中/已完成/失败 + 最近作业）。"""
            q = self._query()
            name = self._project_arg(q)
            from vm import queue as Q
            from vm.state import Project
            proj = Project(Path(projects_root) / name)
            self._json({"ok": True, "project": name, **Q.stats(proj)})

        def api_queue_add(self, body: dict):
            """
            POST /api/queue/add：**入队并立即返回**，然后按需唤醒 drainer。

            ★ 这是用户明确要求的行为：「我点完现在就没法点了，我也不能一直看着啊」。
            原来 `/api/asset/gen` 是**同步**的（浏览器等 20 秒、期间按钮禁用），
            现在点击 = 入队，可以连点十几个然后走开。
            """
            name = str(body.get("project") or default_project or "")
            if not name:
                raise _Abort(400, "no_project", "请求里必须带 project")
            kind = str(body.get("kind") or "")
            from vm import queue as Q
            from vm.state import Project
            proj = Project(Path(projects_root) / name)
            start_err = ""
            args = dict(body.get("args") or {})
            # 也接受把参数平铺在 body 上（前端更顺手）
            for k in ("id", "name", "n", "asset_kind"):
                if k in body and k not in args:
                    args[k] = body[k]
            if kind == "asset_gen" and "asset_kind" in args and "kind" not in args:
                args["kind"] = args.pop("asset_kind")
            try:
                job = Q.add(proj, kind, args)
            except ValueError as e:
                raise _Abort(400, "bad_kind", str(e)) from e

            # 队列是"入队即完事"，但 drainer 得有人跑 —— 空闲就起一个。
            # ★ _start 只干活不发响应（发响应是本 handler 的事），这里只取结果。
            started = False
            approval = None
            try:
                cur = taskctl.read_task(Path(projects_root) / name)
                if not taskctl.task_running(cur):
                    self._start({"project": name, "stage": "queue"}, only=[])
                    started = True
            except _Abort as e:
                # 唤醒失败不阻断入队（作业已落盘不会丢）。**但预算拦截要透传**：
                # NeedsApproval 的结构化载荷必须带给 UI 弹审批框 —— 折成一句
                # start_error 的话用户看不到审批框，队列就永远停在那（S4 闭环缺口）。
                started = False
                start_err = f"{e.error}: {e.message}"
                if e.code == 409 and (e.extra or {}).get("needs_approval"):
                    approval = e.extra
            except Exception as e:                              # noqa: BLE001
                # 唤醒 drainer 失败不阻断入队（作业已落盘不会丢），但必须留栈
                _log_exc("queue/add 唤醒 drainer")
                started = False
                start_err = f"{type(e).__name__}: {e}"
            st = Q.stats(proj)
            out = {"ok": True, "job": job.to_dict(), "started_worker": started,
                   "start_error": start_err, "pending": st["pending"],
                   "message": f"已入队：{job.label}（队列 {st['pending']} 项）"
                              + ("" if started else "，worker 已在跑")}
            if approval:
                # 结构化透传（**只加字段不改既有字段**）：UI 拿 needs_approval 载荷
                # 直接弹审批框（载荷自带 已花/卡在哪/再放行多少 三问的答案）
                out["needs_approval"] = approval
                out["message"] += "；预算超出，「放行」一次后队列继续"
            self._json(out)

        def api_queue_cancel(self, body: dict):
            """POST /api/queue/cancel：取消待处理作业（正在跑的那个要按「停止」）。"""
            name = str(body.get("project") or default_project or "")
            if not name:
                raise _Abort(400, "no_project", "请求里必须带 project")
            from vm import queue as Q
            from vm.state import Project
            proj = Project(Path(projects_root) / name)
            if body.get("id"):
                n = 1 if Q.cancel(proj, str(body["id"])) else 0
            else:
                n = Q.cancel_pending(proj)
            self._json({"ok": True, "canceled": n,
                        "message": f"已取消 {n} 项待处理作业"})

        def api_queue_clear(self, body: dict):
            """POST /api/queue/clear：清掉已完成/失败/取消的记录。"""
            name = str(body.get("project") or default_project or "")
            if not name:
                raise _Abort(400, "no_project", "请求里必须带 project")
            from vm import queue as Q
            from vm.state import Project
            proj = Project(Path(projects_root) / name)
            n = Q.clear_finished(proj)
            self._json({"ok": True, "cleared": n, "message": f"已清理 {n} 条记录"})

        def api_assets(self):
            """GET /api/assets：场景/道具的概念图候选（用于渲染前确认景与道具）。"""
            q = self._query()
            name = self._project_arg(q)
            t = taskctl.assets_table(name, projects_root)
            t["ok"] = True
            self._json(t)

        def api_asset_gen(self, body: dict):
            """POST /api/asset/gen：给场景/道具出候选概念图（默认 2 张）。"""
            name = str(body.get("project") or default_project or "")
            if not name:
                raise _Abort(400, "no_project", "请求里必须带 project")
            kind = str(body.get("kind") or "")
            aid = str(body.get("id") or "")
            if kind not in ("scene", "prop"):
                raise _Abort(400, "bad_kind", "kind 必须是 scene 或 prop")
            if not aid:
                raise _Abort(400, "bad_id", "必须给 id（S1 / P1）")
            # ★ 不再同步出图（那会让浏览器等 20 秒且按钮禁用）。
            #   改成入队 + 返回 —— 前端可以连点，用户也能走开。
            from vm import queue as Q
            from vm.state import Project
            proj = Project(Path(projects_root) / name)
            job = Q.add(proj, "asset_gen", {"kind": kind, "id": aid, "n": int(body.get("n") or 2)})
            started = False
            try:
                cur = taskctl.read_task(Path(projects_root) / name)
                if not taskctl.task_running(cur):
                    self._start({"project": name, "stage": "queue"}, only=[])
                    started = True
            except Exception:                               # noqa: BLE001
                # 唤醒 drainer 失败不能整个吞掉 —— 留栈，作业还在队列里不会丢
                _log_exc("asset/gen 唤醒 drainer")
            st = Q.stats(proj)
            self._json({"ok": True, "job": job.to_dict(), "started_worker": started,
                        "pending": st["pending"],
                        "message": f"已入队：{kind} {aid}（队列 {st['pending']} 项）"})

        def api_asset_upload(self, body: dict):
            """POST /api/asset/upload：上传自有图作为场景/道具候选（base64）。"""
            name = str(body.get("project") or default_project or "")
            if not name:
                raise _Abort(400, "no_project", "请求里必须带 project")
            kind = str(body.get("kind") or "")
            aid = str(body.get("id") or "")
            if kind not in ("scene", "prop"):
                raise _Abort(400, "bad_kind", "kind 必须是 scene 或 prop")
            if not aid:
                raise _Abort(400, "bad_id", "必须给 id（S1 / P1）")
            raw = _decode_image_b64(body.get("b64") or "")
            try:
                r = taskctl.asset_upload(name, kind, aid,
                                         str(body.get("filename") or "upload.png"),
                                         raw, projects_root)
            except _CLIENT_ERRORS as e:
                # 已知输入错误（参数/内容不合法）→ 4xx 人话；
                # 其余异常（磁盘、代码缺陷）**不在此折叠** —— 冒到 _dispatch 兜底
                # 变 500 + 留栈，服务端 bug 不再伪装成「请求错误」。
                raise _Abort(400, "upload_failed", str(e)) from e
            self._json({"ok": True, **r})

        def api_asset_adopt(self, body: dict):
            """POST /api/asset/adopt：采纳一张候选作为定稿。"""
            name = str(body.get("project") or default_project or "")
            if not name:
                raise _Abort(400, "no_project", "请求里必须带 project")
            try:
                r = taskctl.asset_adopt(name, str(body.get("kind") or ""),
                                        str(body.get("id") or ""),
                                        str(body.get("file") or ""), projects_root)
            except _CLIENT_ERRORS as e:
                # 同 upload：已知输入错误 → 4xx；服务端故障不折叠，走 500 + 留栈
                raise _Abort(400, "asset_adopt_failed", str(e)) from e
            self._json({"ok": True, **r})

        def api_props(self):
            """GET /api/props：关键道具实体 + 每镜归属（右栏「道具」tab 的数据源）。"""
            q = self._query()
            name = self._project_arg(q)
            t = taskctl.props_table(name, projects_root)
            t["ok"] = True
            self._json(t)

        def api_budget(self):
            """GET /api/budget：成本估算 / 今日已花 / 是否超预算（E3 预算护栏）。"""
            q = self._query()
            name = self._project_arg(q)
            stage = str(q.get("stage") or "all")
            from vm import budget as B
            # 预算模块按「项目目录」定位（它自己 resolve_project 用默认根目录，
            # 不知道 web 的 projects_root 口径）—— 先解析成绝对路径再交给它，
            # 免得 serve 挂在非默认根目录时预算读到别的项目去。
            pdir = taskctl.resolve_project(name, projects_root)
            est = B.estimate(pdir, stage)
            chk = B.check(pdir, est)
            self._json({"ok": True, "est": est, "check": chk,
                        "budget": B.budget_of(pdir),
                        "spent": B.spent(pdir)})

        # -- 配置中心（T3）：handler 只做参数校验 + 错误映射，业务全部在 vm/config.py

        def api_config(self):
            """GET /api/config?project=X：全部配置项 + 各层值（继承/覆盖标记）+ 说明文案。

            project 可省略（只看全局层）。**永远不含 API Key** —— 只报 api_key_present。
            """
            q = self._query()
            name = str(q.get("project") or default_project or "")
            self._json({"ok": True, **vm_config.get_config(name or None, projects_root)})

        def api_config_save(self, body: dict):
            """POST /api/config：保存配置改动。

            body: {project, changes: [{key, layer, value}], confirm_impact?}
            value=null = 从该层删除（一键恢复继承）；危险项（进镜头指纹）改动
            未 confirm_impact 时不落盘，返回 needs_confirm=true + 影响清单让人确认。
            """
            name = str(body.get("project") or default_project or "")
            changes = body.get("changes")
            if not isinstance(changes, list):
                raise _Abort(400, "bad_request",
                             "changes 必须是数组：[{key, layer, value}]（value=null 表示恢复继承）")
            try:
                out = vm_config.save_config(
                    name or None, changes, root=projects_root,
                    confirm_impact=bool(body.get("confirm_impact")),
                )
            except vm_config.ConfigError as e:
                raise _Abort(400, "config_invalid", str(e), issues=e.errors) from e
            self._json({"ok": True, **out})

        def api_config_validate(self, body: dict):
            """POST /api/config/validate：保存前预检 —— schema 校验 + ComfyUI 连通 /
            模型文件 / 字体 / ffmpeg / API Key 是否就位 + 危险项影响清单。"""
            name = str(body.get("project") or default_project or "")
            values = body.get("values") if isinstance(body.get("values"), dict) else {}
            try:
                self._json({"ok": True,
                            **vm_config.validate_bundle(name or None, values, root=projects_root)})
            except vm_config.ConfigError as e:
                raise _Abort(400, "config_invalid", str(e), issues=e.errors) from e

        def api_approve(self, body: dict):
            """POST /api/approve：放行**一次**（下一个任务消费掉即失效，防永久放行）。"""
            name = str(body.get("project") or default_project or "")
            if not name:
                raise _Abort(400, "no_project", "请求里必须带 project")
            from vm import budget as B
            # ★ 必须传「被服务的项目」的绝对路径（BUG-2 尾巴）：只传裸项目名时
            #   budget._pdir 会拿**默认 projects 根**去解析 —— serve 挂自定义根
            #   （测试/多根）时放行记录写到别的项目去，审批永远不生效。
            pdir = taskctl.resolve_project(name, projects_root)
            r = B.grant_approval(pdir, by="user", stage=str(body.get("stage") or ""),
                                 note=str(body.get("note") or ""))
            self._json({"ok": True, **r,
                        "message": "已放行一次；下一个任务消费后即失效"})

        def api_completeness(self):
            """GET /api/completeness：逐镜 7 维完备性矩阵（B3）。"""
            q = self._query()
            name = self._project_arg(q)
            t = taskctl.completeness_table(name, projects_root)
            t["ok"] = True
            self._json(t)

        def api_script(self):
            """GET /api/script：剧本层（无损视图）+ 逐字校验（右栏「剧本」tab 的数据源）。"""
            q = self._query()
            name = self._project_arg(q)
            t = taskctl.script_table(name, projects_root)
            t["ok"] = True
            self._json(t)

        def api_chapters(self):
            """GET /api/chapters：集/章清单 + 每章镜数（多集管理的数据源）。"""
            q = self._query()
            name = self._project_arg(q)
            t = taskctl.chapter_table(name, projects_root)
            t["ok"] = True
            self._json(t)

        def api_storyboard(self):
            """GET /api/storyboard：分镜图（渲染前审片）逐镜状态。"""
            q = self._query()
            name = self._project_arg(q)
            t = taskctl.storyboard_table(name, projects_root)
            t["ok"] = True
            # 每镜的出图 URL（有产物才有意义，但一律给，前端按 status 决定是否加载）
            t["url_base"] = "/view?project=" + urllib.parse.quote(t["project"]) + "&kind=storyboard&name="
            self._json(t)

        def api_scenes(self):
            """GET /api/scenes：场景实体 + 每镜归属（右栏「场景」tab 的数据源）。"""
            q = self._query()
            name = self._project_arg(q)
            t = taskctl.scene_table(name, projects_root)
            t["ok"] = True
            self._json(t)

        def api_shots(self):
            q = self._query()
            name = self._project_arg(q)
            t = taskctl.shot_table(name, projects_root)
            t["ok"] = True
            self._json(t)

        def api_qc(self):
            """GET /api/qc：逐镜质检判定 + 汇总（右栏「质检」tab 的数据源）。"""
            q = self._query()
            name = self._project_arg(q)
            t = taskctl.qc_table(name, projects_root)
            t["ok"] = True
            base = "/view?project=" + urllib.parse.quote(t["project"]) + "&shot="
            t["play_url_base"] = base
            self._json(t)

        def api_audit(self):
            """GET /api/audit：state/audit.json + 序号→镜头号映射（右栏「审计」tab）。"""
            q = self._query()
            name = self._project_arg(q)
            t = taskctl.audit_table(name, projects_root)
            t["ok"] = True
            self._json(t)

        def api_log(self):
            q = self._query()
            name = self._project_arg(q)
            try:
                offset = int(q.get("offset") or 0)
            except ValueError:
                offset = 0
            self._json({"ok": True, **taskctl.read_log(name, offset, root=projects_root)})

        def api_shot(self):
            """GET /api/shot：单镜完整详情（六段式全文 + 台词旁白 + 三态 + 预览地址）。"""
            q = self._query()
            name = self._project_arg(q)
            sid = (q.get("id") or q.get("shot") or "").strip()
            if not sid:
                raise _Abort(400, "bad_shot", "必须给 id（镜头号），例如 ?project=西游记&id=1-1-01")
            try:
                d = taskctl.shot_detail(name, sid, projects_root)
            except taskctl.TaskError as e:
                raise _Abort(404, "no_shot", str(e)) from e
            shot = d["shot"]
            base = "/view?project=" + urllib.parse.quote(d["project"]) + "&shot=" + urllib.parse.quote(sid)
            exists = bool((shot.get("clip") or {}).get("exists"))
            shot["thumb_url"] = base + "&thumb=1" if exists else None
            shot["play_url"] = base if exists else None
            self._json({"ok": True, **d})

        # -- P1：镜头编辑 -------------------------------------------------
        #
        # 统一错误映射：任务层抛的都是"人话 + 明确类别"，这里只负责转成合适的状态码。
        # 特别地 ShotInvalid（校验拒绝）要把 issues 带出去 —— 用户要能看到"为什么不让存"。

        def _shot_body_project(self, body: dict) -> str:
            name = str(body.get("project") or default_project or "").strip()
            if not name:
                raise _Abort(400, "no_project", "请求里必须带 project")
            return name

        def _shot_call(self, fn):
            try:
                r = fn()
            except taskctl.ShotInvalid as e:
                raise _Abort(400, "shot_invalid", str(e), issues=e.issues) from e
            except taskctl.ShotLLMError as e:
                raise _Abort(502, "llm_failed", str(e), issues=e.issues) from e
            except taskctl.ModuleNotReady as e:
                raise _Abort(503, "module_not_ready", str(e)) from e
            except taskctl.TaskError as e:
                raise _Abort(400, "bad_request", str(e)) from e
            r.pop("ok", None)
            self._json({"ok": True, **r})

        def _shot_id_arg(self, body: dict) -> str:
            sid = str(body.get("id") or body.get("shot_id") or "").strip()
            if not sid:
                raise _Abort(400, "bad_shot", "必须给 id（镜头号）")
            return sid

        def api_shot_lock(self, body: dict):
            """
            E1：锁定 / 解锁 / 选中 / 收藏。单镜传 `id`，批量传 `ids`（数组）。

            为什么要有这个：在此之前**任何产物都能被覆盖** —— 改定妆照、批量重渲、
            重跑渲染，都可能把一个你已经满意的镜头盖掉且无任何提示。
            锁定后渲染层会跳过它（`gen.render_shot` 里检查 `manifest.is_locked`），
            只有显式 force 才能动。

            ★ 无产物镜头**优雅跳过**（lead 裁决）：单镜 200 + locked:false + 人话 note；
            批量调用里同类镜头进响应 `skipped` 列表 —— 批量操作不因个别项炸掉。
            """
            name = self._shot_body_project(body)
            flag = str(body.get("flag") or "locked")
            value = bool(body.get("value", True))
            by = str(body.get("by") or "user")
            ids_raw = body.get("ids")

            def _one(sid: str) -> dict:
                return taskctl.set_shot_flag(name, sid, flag, value, projects_root, by=by)

            if ids_raw is None:
                # 单镜：原样透出（含 skipped/note —— 无产物不再 400）
                self._shot_call(lambda: _one(self._shot_id_arg(body)))
                return
            if not isinstance(ids_raw, list) or not ids_raw:
                raise _Abort(400, "bad_shot", "ids 必须是非空的镜头号数组")

            def _bulk() -> dict:
                updated: list[str] = []
                skipped: list[str] = []
                notes: list[str] = []
                for x in ids_raw:
                    r = _one(str(x))
                    (skipped if r.get("skipped") else updated).append(r["shot"])
                    if r.get("note"):
                        notes.append(f"{r['shot']}：{r['note']}")
                return {
                    "updated": updated,
                    "skipped": skipped,
                    "notes": notes,
                    "message": f"已处理 {len(ids_raw)} 镜：成功 {len(updated)} 镜"
                               + (f"，跳过 {len(skipped)} 镜（无产物记录）" if skipped else ""),
                }

            self._shot_call(_bulk)

        def api_shot_update(self, body: dict):
            name = self._shot_body_project(body)
            sid = self._shot_id_arg(body)
            patch = body.get("patch")
            if patch is None:
                # 容忍"字段平铺"的写法：{project,id,dialogue:"…"} 也当 patch 用
                skip = {"project", "id", "shot_id"}
                patch = {k: v for k, v in body.items() if k not in skip}
            if not isinstance(patch, dict):
                raise _Abort(400, "bad_patch", "patch 必须是 JSON 对象")
            self._shot_call(lambda: taskctl.update_shot(name, sid, patch, projects_root))

        def api_shot_rewrite(self, body: dict):
            name = self._shot_body_project(body)
            sid = self._shot_id_arg(body)
            fb = body.get("feedback")
            if fb is not None and not isinstance(fb, str):
                raise _Abort(400, "bad_feedback", "feedback 必须是字符串")
            self._shot_call(lambda: taskctl.rewrite_shot(name, sid, feedback=fb or "", root=projects_root))

        def api_shot_split(self, body: dict):
            name = self._shot_body_project(body)
            sid = self._shot_id_arg(body)
            field = str(body.get("field") or "")
            at = body.get("at")
            self._shot_call(lambda: taskctl.split_shot(name, sid, at=at, field=field, root=projects_root))

        def api_shot_merge(self, body: dict):
            name = self._shot_body_project(body)
            sid = self._shot_id_arg(body)
            self._shot_call(lambda: taskctl.merge_shot(name, sid, projects_root))

        def api_shot_insert(self, body: dict):
            name = self._shot_body_project(body)
            after = str(body.get("after") or "").strip()
            if not after:
                raise _Abort(400, "bad_after", "必须给 after（在哪一镜之后插入）")
            shot = body.get("shot")
            if shot is not None and not isinstance(shot, dict):
                raise _Abort(400, "bad_shot_obj", "shot 必须是 JSON 对象")
            self._shot_call(lambda: taskctl.insert_shot(name, after, shot, projects_root))

        def api_shot_delete(self, body: dict):
            name = self._shot_body_project(body)
            sid = self._shot_id_arg(body)
            self._shot_call(lambda: taskctl.delete_shot(name, sid, projects_root))

        def api_shot_undo(self, body: dict):
            name = self._shot_body_project(body)
            self._shot_call(lambda: taskctl.undo_shot_edit(name, projects_root))

        def api_shots_bulk(self, body: dict):
            name = self._shot_body_project(body)
            selector = body.get("selector")
            patch = body.get("patch")
            if not isinstance(selector, dict):
                raise _Abort(400, "bad_selector", "selector 必须是 JSON 对象（chapter/scene/ids/status/all）")
            if not isinstance(patch, dict):
                raise _Abort(400, "bad_patch", "patch 必须是 JSON 对象")
            self._shot_call(lambda: taskctl.bulk_update(name, selector, patch, projects_root))

        def api_shots_renumber(self, body: dict):
            name = self._shot_body_project(body)
            force = bool(body.get("force"))
            self._shot_call(lambda: taskctl.renumber_shots(name, projects_root, force=force))

        def view(self):
            q = self._query()
            name = self._project_arg(q)
            pdir = taskctl.resolve_project(name, projects_root)
            proj = Project(pdir)
            if q.get("final"):
                episode = (q.get("episode") or "EP01").strip()
                if not SHOT_RE.match(episode):
                    raise _Abort(400, "bad_episode", f"非法集名：{episode!r}")
                f = proj.final_dir / f"{episode}.mp4"
                if not f.is_file():
                    raise _Abort(404, "no_final", f"还没有成片：{f.name}（先跑 assemble）")
                return self._file(f, "video/mp4", download=bool(q.get("download")))

            # R2：kind=ref|gacha|sheet|char_raw（定妆照 / 抽卡候选 / 对比图 / 原始定妆视频）
            if q.get("kind"):
                src, ctype, thumb_key = _resolve_view_target(proj, q.get("kind", ""), q.get("name", ""), q.get("file", ""))
                if q.get("thumb"):
                    tp, pending = _thumb_request(proj, thumb_key, src)
                    if tp is None:
                        if pending:
                            return self._thumb_placeholder()
                        raise _Abort(500, "thumb_failed", "缩略图生成失败（ffmpeg 不可用或文件损坏）")
                    return self._file(tp, "image/jpeg")
                return self._file(src, ctype, download=bool(q.get("download")))

            shot = q.get("shot") or ""
            clip = _resolve_shot_clip(proj, shot)
            if q.get("thumb"):
                tp, pending = _thumb_request(proj, shot, clip)
                if tp is None:
                    if pending:
                        return self._thumb_placeholder()
                    raise _Abort(500, "thumb_failed", "缩略图生成失败（ffmpeg 不可用或该片段无法解码）")
                return self._file(tp, "image/jpeg")
            return self._file(clip, "video/mp4", download=bool(q.get("download")))

        def _thumb_placeholder(self) -> None:
            """
            缩略图还在后台生成 → 先回占位图 + `X-Thumb-Pending: 1`。
            no-store 是硬要求：占位图绝不能进缓存，否则前端重试拿到的还是它。
            前端约定（可选优化）：看到 X-Thumb-Pending 就延迟 2~3s 换 URL（&t=时间戳）重试。
            """
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(_PLACEHOLDER_JPEG)))
            self.send_header("X-Thumb-Pending", "1")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(_PLACEHOLDER_JPEG)

        # -- R2：角色定妆 / 抽卡 / 采纳 ------------------------------------

        def api_chars(self):
            q = self._query()
            name = self._project_arg(q)
            t = taskctl.char_table(name, projects_root)
            only = (q.get("name") or "").strip()
            rows = t["chars"]
            if only:
                rows = [c for c in rows if c["name"] == only]
            base = "/view?project=" + urllib.parse.quote(t["project"]) + "&kind="
            for c in rows:
                cn = urllib.parse.quote(c["name"])
                c["ref_url"] = f"{base}ref&name={cn}" if c.get("ref_path") else None
                c["sheet_url"] = f"{base}sheet&name={cn}" if c.get("sheet_path") else None
                for cand in c["candidates"]:
                    cand["url"] = f"{base}gacha&name={cn}&file={urllib.parse.quote(cand['file'])}"
                    cand["thumb_url"] = cand["url"] + "&thumb=1"
                for k in ("ref_path", "sheet_path"):
                    c.pop(k, None)
                for cand in c["candidates"]:
                    cand.pop("path", None)
            self._json({"ok": True, "project": t["project"], "chars": rows})

        def api_chars_gacha(self, body: dict):
            name = str(body.get("project") or default_project or "")
            char = str(body.get("name") or "").strip()
            if not char:
                raise _Abort(400, "bad_name", "必须给角色名 name")
            if not taskctl.CHAR_NAME_RE.match(char):
                raise _Abort(400, "bad_name", f"非法角色名：{char!r}")
            try:
                n = int(body.get("n") or taskctl.DEFAULT_GACHA_COUNT)
            except (TypeError, ValueError):
                raise _Abort(400, "bad_count", "张数 n 必须是整数") from None
            if not 1 <= n <= 24:
                raise _Abort(400, "bad_count", f"抽卡张数要在 1~24 之间，收到 {n}")
            # 走统一任务模型：进度/日志/停止/并发锁与 render 完全一样
            self._json(self._start({"project": name, "stage": "gacha", "count": n}, only=[char], force=False))

        def api_chars_adopt(self, body: dict):
            name = str(body.get("project") or default_project or "")
            char = str(body.get("name") or "").strip()
            file = str(body.get("file") or "").strip()
            if not name:
                raise _Abort(400, "no_project", "请求里必须带 project")
            try:
                src = taskctl.resolve_candidate(name, char, file, projects_root)
            except taskctl.TaskError as e:
                raise _Abort(400, "bad_candidate", str(e)) from e
            self._json(self._apply_ref(name, char, src, f"采纳候选 {file}"))

        def api_chars_upload(self, body: dict):
            name = str(body.get("project") or default_project or "")
            char = str(body.get("name") or "").strip()
            filename = str(body.get("filename") or "upload.png")
            b64 = body.get("b64") or ""
            if not name:
                raise _Abort(400, "no_project", "请求里必须带 project")
            if not taskctl.CHAR_NAME_RE.match(char):
                raise _Abort(400, "bad_name", f"非法角色名：{char!r}")
            raw = _decode_image_b64(b64)
            pdir = taskctl.resolve_project(name, projects_root)
            safe = re.sub(r"[^A-Za-z0-9_.\-]", "_", Path(filename).name)[-60:] or "upload"
            dst = Project(pdir).state_dir / "uploads" / f"{char}_{int(time.time())}_{safe}"
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_suffix(dst.suffix + ".tmp")
            with open(tmp, "wb") as f:
                f.write(raw)
            os.replace(tmp, dst)
            self._json(self._apply_ref(name, char, dst, f"上传自有图 {safe}（{len(raw)/1024:.0f} KB）"))

        def api_chars_prompt(self, body: dict):
            name = str(body.get("project") or default_project or "")
            char = str(body.get("name") or "").strip()
            text = body.get("text")
            if not name:
                raise _Abort(400, "no_project", "请求里必须带 project")
            if not isinstance(text, str):
                raise _Abort(400, "bad_text", "text 必须是字符串")
            try:
                r = taskctl.save_char_prompt(name, char, text, projects_root)
            except taskctl.TaskError as e:
                raise _Abort(400, "bad_request", str(e)) from e
            msg = f"已保存 prompts/char_{char}.txt（{len(text)} 字符）"
            if r["prompt_warnings"]:
                msg += f"；⚠ 提示词含风格锚：{'、'.join(r['prompt_warnings'])}"
            self._json({"ok": True, "message": msg, **{k: r[k] for k in ("name", "prompt", "prompt_warnings")}})

        def _apply_ref(self, project: str, char: str, src: Path, what: str) -> dict:
            """
            采纳/上传的统一收尾：覆盖 refs/char_<名>.png + 同步 ComfyUI input，
            然后回报"哪些镜头因此变成待重渲" —— 让用户看得见指纹机制的联动。
            """
            try:
                chars_mod = taskctl.import_module("chars")
            except taskctl.ModuleNotReady as e:
                raise _Abort(503, "module_not_ready", str(e)) from e
            params = taskctl.load_params(project, projects_root)
            pdir = taskctl.resolve_project(project, projects_root)
            ref = Project(pdir).refs_dir / f"char_{char}.png"
            before = taskctl.file_digest16(ref) if ref.is_file() else ""
            lines: list[str] = []
            try:
                chars_mod.adopt_candidate(Project(pdir), char, src, params, lines.append)
            except _CLIENT_ERRORS as e:
                # 已知输入错误（候选图不存在等）→ 4xx；
                # 其余（磁盘故障、ComfyUI input 同步炸）不折叠成 400，走 500 + 留栈
                raise _Abort(400, "adopt_failed", f"采纳失败：{e}") from e
            after = taskctl.file_digest16(ref)
            # 采纳后立刻重算三态：参考图 mtime 变了 → 用到该角色的镜头自动变 stale/缺失
            stale: list[str] = []
            try:
                t = taskctl.shot_table(project, projects_root)
                stale = [s["id"] for s in t["shots"]
                         if char in (s.get("chars") or []) and s["status"] != taskctl.CURRENT]
            except Exception:
                # 这里吞异常是刻意的（采纳已成功，不能因重算 stale 而回滚响应），
                # 但必须留栈 —— 否则"stale 列表为空"会静默骗人
                _log_exc("shot_table（采纳后重算 stale）")
                stale = []
            running = taskctl.task_running(taskctl.read_task(project, projects_root))
            res = {
                "ok": True,
                "message": f"{what} → refs/char_{char}.png 已更新"
                           + (f"，{len(stale)} 个相关镜头待重渲" if stale else ""),
                "name": char,
                "ref_url": f"/view?project={urllib.parse.quote(pdir.name)}&kind=ref&name={urllib.parse.quote(char)}&t={int(time.time())}",
                "ref_digest": after,
                "changed": before != after,
                "stale_shots": stale,
                "log": lines,
            }
            if running:
                res["warning"] = "有任务正在运行：新参考图会影响之后的提交，但已经在跑的那一镜仍用旧图"
            return res

        def _start(self, body: dict, *, stage_default: str | None = None, only=None, force=None) -> dict:
            """
            启动任务，**只干活、只返回载荷，不发响应** —— 发响应是调用方的统一职责。

            （原来是 quiet 开关的双响应 hack：漏传 quiet 就一个请求发两次响应，
             第二个响应在连接复用时被当响应体读到 → 前端表现为卡死。现在结构上
             不存在"发一次还是发两次"的选择题。）
            """
            name = str(body.get("project") or default_project or "")
            if not name:
                raise _Abort(400, "no_project", "请求里必须带 project")
            stage = str(body.get("stage") or stage_default or "")
            if stage not in taskctl.STAGES:
                raise _Abort(400, "bad_stage", f"stage 必须是 {', '.join(taskctl.STAGES)} 之一，收到 {stage!r}")
            only_v = only if only is not None else body.get("only")
            if isinstance(only_v, str):
                only_v = [s.strip() for s in only_v.split(",") if s.strip()]
            if only_v is not None and not isinstance(only_v, list):
                raise _Abort(400, "bad_only", "only 必须是镜头号数组或逗号分隔字符串")
            try:
                fake_sleep = float(body.get("fake_sleep") or 0)
            except (TypeError, ValueError):
                raise _Abort(400, "bad_fake_sleep", "fake_sleep 必须是秒数") from None
            try:
                count = int(body.get("count") or 0)
            except (TypeError, ValueError):
                raise _Abort(400, "bad_count", "count 必须是整数") from None
            try:
                h = taskctl.start_async(
                    name,
                    stage,
                    only=[str(x) for x in (only_v or [])] or None,
                    force=bool(body.get("force")) if force is None else force,
                    dry=bool(body.get("dry")),
                    fake_sleep=fake_sleep,
                    count=count,
                    root=projects_root,
                )
            except taskctl.NeedsApproval as e:
                # ★ E3：**409 + 结构化载荷**，不是普通报错。
                # UI 拿 payload 弹"暂停等人批"对话框；载荷里带着三问的答案
                # （已花多少 / 卡在哪步 / 再放行多少），所以人不用去翻日志。
                raise _Abort(409, "needs_approval", str(e.payload.get("message") or str(e)),
                             extra={"needs_approval": True, **e.payload}) from e
            except taskctl.TaskBusy as e:
                raise _Abort(409, "busy", str(e)) from e
            except taskctl.ModuleNotReady as e:
                raise _Abort(503, "module_not_ready", str(e)) from e
            except taskctl.TaskError as e:
                raise _Abort(400, "bad_request", str(e)) from e
            task = h.public()
            return {
                "ok": True,
                "message": f"已启动：{name} / {stage} / pid={h.pid}",
                "pid": h.pid,
                "project": name,
                "stage": stage,
                "only": task.get("only") or [],
                "started_at": task.get("started_at"),
            }

        def api_run(self, body: dict):
            self._json(self._start(body))

        def api_rerender(self, body: dict):
            shot = str(body.get("shot_id") or body.get("shot") or "").strip()
            if not SHOT_RE.match(shot):
                raise _Abort(400, "bad_shot", f"非法镜头号：{shot!r}")
            self._json(self._start({**body, "stage": "render"}, only=[shot], force=True))

        def api_stop(self, body: dict):
            name = str(body.get("project") or default_project or "")
            if not name:
                raise _Abort(400, "no_project", "请求里必须带 project")
            try:
                r = taskctl.stop(name, root=projects_root)
            except taskctl.TaskError as e:
                raise _Abort(400, "bad_request", str(e)) from e
            code = 200 if r.get("ok") else 409
            self._json({"ok": bool(r.get("ok")), "error": r.get("reason", ""), "message": r.get("message", ""),
                        "pid": r.get("pid", 0), "alive": r.get("alive"),
                        "signal": r.get("signal", ""), "target": r.get("target", "")}, code)

    # 启动自检：漏注册/表项悬空直接在这里炸掉 —— 服务起不来，胜过线上 404
    _verify_routes(Handler)
    return Handler


# ---------------------------------------------------------------- 入口


def serve(proj_root: Path, port: int = 8801, *, default_project: str | None = None, host: str = "127.0.0.1") -> None:
    """
    proj_root 传 projects/ 目录（含各项目子目录）。
    也容忍传单个项目目录 —— 会自动退到它的上一层，省得用户记错。
    """
    root = Path(proj_root).expanduser().resolve()
    if not root.is_dir():
        raise taskctl.TaskError(f"项目根目录不存在：{root}")
    if (root / "project.json").is_file() or (root / "novel").is_dir() or (root / "shots").is_dir():
        root = root.parent
    handler = make_handler(root, default_project)
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    names = taskctl.discover_projects(root)
    print(f"[web] 漫剧流水线 Web UI：http://{host}:{port}/", flush=True)
    print(f"[web] 项目根目录：{root}", flush=True)
    print(f"[web] 项目：{', '.join(names) if names else '（无）'}", flush=True)
    print("[web] 只读+提交：不会启停 ComfyUI，也不会动 /home/max/ComfyUI/output/", flush=True)
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n[web] Ctrl-C，退出", flush=True)
    finally:
        httpd.server_close()
    return None
