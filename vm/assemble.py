"""
assemble.py —— 合成：镜头表 + clips/*.mp4 → final/<episode>.mp4（含中文字幕）。

为什么要有这个模块：
  逐镜产物再漂亮，也得按镜头表顺序拼成一集才算成片。这里做三件事：
    ① 拼接   —— 优先 concat 流复制（快且无损），参数不一致就 fallback 重编码
    ② 字幕   —— 导出 .srt + 烧录进画面（契约要求两者都要）
    ③ 规格统一 —— libx264 crf18 / yuv420p / aac 32kHz 128k / +faststart

踩过的坑（都在代码里留了注释）：
  · concat 清单**必须绝对路径**，否则 ffmpeg 会在清单所在目录找文件（PIPELINE-DESIGN 阶段6）
  · 不能只靠 `-c copy`：片段分辨率/帧率/采样率只要有一项不同，流复制就会错帧或直接失败
  · `subtitles` 滤镜的文件名要过 filtergraph 解析，路径里的 `:`、`\`、`'` 全是雷。
    这里的解法是把 srt 复制成临时目录里的 `sub.srt`（纯 ASCII 相对名），
    并让 ffmpeg 以该目录为 cwd —— 一个转义都不用写。
  · 中文字体必须先探测：缺字体时 libass 不报错，只烧出一屏方块，
    所以探测失败要**直接抛错并给 apt 安装提示**，而不是静默出一版废片。
  · `eq=brightness=` 这类表达式滤镜默认 `eval=init`（只在起始求值一次），
    做"随时间变化"的测试夹具时必须显式 `eval=frame`。

转场：
  cut      硬切（一期主用）：concat 流复制 → 一次编码落规格；失败则单趟 filter 重编码
  fade     闪黑：每片首尾各 0.25s 淡入淡出（时长不变，时间轴与 cut 一致）
  dissolve 叠化：xfade + acrossfade，每处重叠 0.3s，**总时长会缩短**，字幕时间轴同步前移
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 只为类型注解，运行时不 import（避免和并行开发的模块互相拖累）
    from .shots import Shot
    from .state import Project

FPS = 24
CRF = "18"
PRESET = "medium"
AUDIO_RATE = "32000"
AUDIO_BITRATE = "128k"
FADE_DUR = 0.25     # fade：每片首尾淡入淡出时长
XFADE_DUR = 0.3     # dissolve：相邻片重叠时长

# 中文字体候选（本机实测存在：Noto Sans CJK SC / 文泉驿）
FONT_CANDIDATES = [
    "Noto Sans CJK SC",
    "Noto Sans CJK TC",
    "Noto Sans SC",
    "Source Han Sans SC",
    "WenQuanYi Zen Hei",
    "WenQuanYi Micro Hei",
    "AR PL UMing CN",
]
# fc-match 找不到时兜底看这些文件（有的机器没装 fontconfig 的匹配数据）
FONT_FILE_HINTS = [
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "Noto Sans CJK SC"),
    ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", "WenQuanYi Zen Hei"),
    ("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", "WenQuanYi Micro Hei"),
]

FFMPEG = os.environ.get("VM_FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("VM_FFPROBE", "ffprobe")
RUN_TIMEOUT = 7200  # 整集编码可能很久；这里给足，避免长片被误杀


# ---------------------------------------------------------------- 小工具


def _which(tool: str) -> str:
    exe = shutil.which(tool)
    if not exe:
        raise RuntimeError(f"找不到 {tool}，请先安装 ffmpeg（sudo apt install ffmpeg）")
    return exe


def _atomic_write_text(path: Path, text: str) -> None:
    """先写 .tmp 再 os.replace：字幕文件也不留半截（UTF-8、不转义中文、无 BOM）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _run(cmd: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=RUN_TIMEOUT,
        cwd=str(cwd) if cwd else None,
    )


def _err(p: subprocess.CompletedProcess) -> str:
    return " ".join((p.stderr or "").strip().split())[-400:]


def _load_shots_dir(d: Path):
    """延迟 import shots.py：让本模块能被单测、也能在 shots.py 未就绪时给出清楚报错。"""
    try:
        from .shots import load_shots_dir  # type: ignore
    except ImportError:
        from shots import load_shots_dir  # type: ignore
    return load_shots_dir(d)


def _shots_api():
    """延迟 import vm/shots.py 模块本身 —— 复用它定义的 A2 时长口径，避免两处各写一份。"""
    try:
        from . import shots as mod  # type: ignore
    except ImportError:
        import shots as mod  # type: ignore
    return mod


# ---------------------------------------------------------------- 字体 / 滤镜探测


def _has_filter(name: str) -> bool:
    """`ffmpeg -filters` 里有没有这个滤镜（subtitles 需要 libass 支持）。"""
    p = _run([_which(FFMPEG), "-hide_banner", "-filters"])
    if p.returncode != 0:
        return False
    return any(line.split()[1] == name for line in (p.stdout or "").splitlines() if len(line.split()) > 2)


def detect_cjk_font() -> str:
    """探测可用的中文字体族名；找不到就抛错并给安装提示。

    注意 fc-match 的坑：**它永远会返回一个字体**（找不到就给默认字体 DejaVu 之类），
    所以不能只看 returncode，必须检查返回的族名里真的包含请求的名字；
    否则字体会静默退化成无中文字形 → 满屏方块。
    """
    fc = shutil.which("fc-match")
    if fc:
        for fam in FONT_CANDIDATES:
            p = subprocess.run([fc, "-f", "%{family}", fam], capture_output=True, text=True)
            got = (p.stdout or "").strip()
            if p.returncode == 0 and got and fam.lower() in got.lower():
                return fam
    for path, fam in FONT_FILE_HINTS:
        if Path(path).exists():
            return fam
    raise RuntimeError(
        "找不到中文字体，无法烧录字幕（libass 缺字体会烧出满屏方块）。\n"
        "  请安装其一：\n"
        "    sudo apt install fonts-noto-cjk      # 推荐\n"
        "    sudo apt install fonts-wqy-zenhei\n"
        "  或设置环境变量 VM_SUBTITLE_FONT 指定已安装的中文字体族名。"
    )


def _resolve_font() -> str:
    forced = os.environ.get("VM_SUBTITLE_FONT", "").strip()
    return forced or detect_cjk_font()


def _subtitle_style(font: str, height: int) -> str:
    """字幕样式：白字 + 黑描边 + 底部居中。

    FontSize 按画面高度自适应：libass 处理 SRT 时的基准分辨率是视频自身，
    所以同一套字号在 864×480 和 320×240 上观感一致。
    """
    fs = max(12, round(height / 22))
    mv = max(8, round(height / 18))
    return (
        f"FontName={font},FontSize={fs},"
        "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
        "BorderStyle=1,Outline=2,Shadow=0,Alignment=2,MarginV=" + str(mv)
    )


# ---------------------------------------------------------------- 三层时长（A2）

# 三层时长：sec(规划) / sec_override(人工覆盖) / sec_actual(ffprobe 回读)
#   实际提交 = override ?? planned        （gen.py 侧，Lead 负责）
#   concat/字幕时间轴 = sec_actual        （本模块，A2）
#
# 为什么必须回读：H3 帧数有 `frames % 17 == 5` 步进（17n+5）。
# 实测真实项目 52 镜：planned 264.000s vs actual 265.844s，每镜差 -0.167 ~ +0.250s，
# 旧代码按请求值算时间轴 → 成片尾部字幕比画面早 1.875s。所以这里**绝不**用请求值兜底。


def _project_cfg(proj) -> dict:
    """读 project.json（读不到就空 dict，不阻断合成）。"""
    path = Path(getattr(proj, "root", "")) / "project.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError):
        return {}


def read_clip_duration(path: Path) -> float:
    """ffprobe 回读片段实际时长 —— 也就是 `sec_actual` 的来源。

    命令按 Lead 指定：`ffprobe -v error -show_entries format=duration -of csv=p=0 <file>`。
    实测 52 镜 format.duration 与 nb_frames/24 完全一致（误差仅 ms 级四舍五入）。

    失败时**不静默回退**到镜头表里请求的 sec：静默回退正是 A2 要消灭的漂移根因，
    所以这里直接抛错，并把 ffprobe 的原始输出带出来，便于判断是"文件损坏"还是"还没写完"。
    """
    p = _run([_which(FFPROBE), "-v", "error", "-show_entries", "format=duration",
              "-of", "csv=p=0", str(path)])
    raw = (p.stdout or "").strip()
    if p.returncode != 0 or not raw:
        raise RuntimeError(
            f"无法回读片段实际时长（sec_actual）：{path}\n"
            f"  ffprobe 退出码={p.returncode}，输出={raw!r}，错误={_err(p)}\n"
            f"  提示：片段可能损坏或尚未写完。A2 要求 concat/字幕时间轴必须用实际时长，"
            f"因此这里不会回退到镜头表里请求的 sec —— 那正是字幕累积漂移的根因。"
        )
    try:
        dur = float(raw.splitlines()[0].split(",")[0])
    except (ValueError, IndexError):
        raise RuntimeError(f"无法解析 {path.name} 的时长：ffprobe 输出 {raw!r}")
    if not (dur > 0):
        raise RuntimeError(f"{path.name} 回读到的时长是 {dur}，不可用")
    return dur


def planned_sec(shot) -> float:
    """规划时长（提交层）= `sec_override ?? sec`。

    优先复用 `shots.effective_sec` —— 那是这个口径的唯一事实来源；
    shots.py 较早的版本没有它时退回本地等价实现，不因为队友文件版本差异把合成卡死。
    这里只用来算"计划 vs 实际"的偏差给日志/审计用。
    """
    fn = getattr(_shots_api(), "effective_sec", None)
    if callable(fn):
        try:
            return float(fn(shot))
        except (TypeError, ValueError):
            pass
    for key in ("sec_override", "sec"):
        v = getattr(shot, key, None)
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f > 0:
            return f
    return 0.0


def resolve_sec_actual(shot, clip: Path) -> tuple[float, str]:
    """返回 (实际秒数, 来源)。来源 ∈ {"sec_actual", "probe"}，会记进 assemble.json 便于审计。

    · 镜头表已带 `sec_actual`（Lead 加字段后由 plan/gen 回填）→ 直接用，省一次 ffprobe
    · 没有该字段（当前真实项目 52 镜都还没有，shots.py 尚未加）→ ffprobe 现读
    """
    v = getattr(shot, "sec_actual", None)
    if v is not None:
        try:
            f = float(v)
        except (TypeError, ValueError):
            f = 0.0
        if f > 0:
            return f, "sec_actual"
    return read_clip_duration(clip), "probe"


def _table_sec(shot) -> float:
    """镜头表能给出的时间轴时长：优先 `shots.timeline_sec`（sec_actual→sec_override→sec）。

    仅用于 `build_srt()` 这种拿不到片段文件路径的场景；assemble() 那条真实链路
    走 `resolve_sec_actual()`，拿不到 sec_actual 就 ffprobe 现读。
    """
    fn = getattr(_shots_api(), "timeline_sec", None)
    if callable(fn):
        try:
            v = float(fn(shot))
        except (TypeError, ValueError):
            v = 0.0
        if v > 0:
            return v
    v = getattr(shot, "sec_actual", None)
    if v is not None:
        try:
            f = float(v)
        except (TypeError, ValueError):
            f = 0.0
        if f > 0:
            return f
    return planned_sec(shot)


def resolve_trim_start(shot, default_trim: float) -> float:
    """起始裁剪秒数：镜头级 `trim_start` 字段优先，其次全局配置，默认 0 = 不裁。"""
    v = getattr(shot, "trim_start", None)
    if v is not None:
        try:
            f = float(v)
        except (TypeError, ValueError):
            f = None
        if f is not None and f >= 0:
            return f
    return max(0.0, float(default_trim or 0.0))


def trim_config(proj) -> float:
    """起始裁剪（C5）配置：环境变量 VM_TRIM_START_SEC → project.json → 0.0（关闭）。

    **默认必须是 0**，依据是实测证据（/tmp/vm-qc-test/start_audio_report.txt）：
    我们 52 个真实片段里**没有**统一的 0.12-0.16s 起始杂音 —— 0-160ms 的过零率中位
    0.072 反而低于全片 0.095（更像语音而非噪声），DC 偏置 |≤0.0028|，起始跳变中位
    0.014 也小于尾段参照 0.059。而固定裁 0.12s 会切进 30/52 个镜头的真实语音。
    所以默认关闭，要开必须显式配置，并且逐镜记录到 manifest。
    """
    env = os.environ.get("VM_TRIM_START_SEC", "").strip()
    if env:
        try:
            return max(0.0, float(env))
        except ValueError:
            raise RuntimeError(f"环境变量 VM_TRIM_START_SEC 不是数字：{env!r}")
    cfg = _project_cfg(proj)
    node = cfg.get("assemble") if isinstance(cfg.get("assemble"), dict) else {}
    for src in (node.get("trim_start_sec"), cfg.get("trim_start_sec")):
        if src is None:
            continue
        try:
            return max(0.0, float(src))
        except (TypeError, ValueError):
            raise RuntimeError(f"project.json 里的 trim_start_sec 不是数字：{src!r}")
    return 0.0


# ---------------------------------------------------------------- SRT


def _srt_time(sec: float) -> str:
    ms = int(round(max(0.0, sec) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _cue_text(shot) -> str:
    """一个镜头的字幕文本：台词优先，旁白另起一行；两者都没有就没有字幕。"""
    parts = [(getattr(shot, "dialogue", "") or "").strip(), (getattr(shot, "narration", "") or "").strip()]
    return "\n".join(p for p in parts if p)


def _build_cues(shots, fps: int, overlap: float = 0.0, *, durations=None, trims=None):
    """生成 (start_sec, end_sec, text) 字幕时间轴。

    **时间轴一律按实际时长累计（A2）**：`durations` 由 assemble() 解析好传进来
    （镜头表 `sec_actual` 或 ffprobe 回读），不再用请求的 `sec`。
    不传 `durations` 时退回镜头表里的 `sec_actual`/`sec` —— 独立调用 `build_srt()`
    拿不到文件路径，只能这样；assemble() 那条真实链路永远是回读值。

    · overlap：dissolve 每处转场让时间轴前移一个重叠时长
    · trims：C5 起始裁剪。裁掉开头让该镜在时间轴上变短，**后面的镜头必须跟着前移**，
      否则裁剪本身就成了新的漂移源
    · 用秒累加而不是"帧数×fps"：请求 6s 实际 5.96s 这种非整帧场景下，
      帧量化会把每镜误差再滚一遍，秒累加才是对的
    """
    cues: list[tuple[float, float, str]] = []
    t = 0.0
    for i, s in enumerate(shots):
        dur = float(durations[i]) if durations is not None else _table_sec(s)
        trim = float(trims[i]) if trims is not None else 0.0
        if i > 0:
            t -= overlap  # 上一处转场造成的时长收缩
        eff = max(0.0, dur - max(0.0, trim))
        text = _cue_text(s)
        if text and eff > 0:
            cues.append((t, t + eff, text))
        t += eff
    return cues


def _write_srt(cues, out: Path) -> Path:
    """把 (start, end, text) 列表写成 SRT 文件（UTF-8 无 BOM，原子写）。"""
    out = Path(out)
    lines = []
    for i, (a, b, text) in enumerate(cues, 1):
        lines.append(str(i))
        lines.append(f"{_srt_time(a)} --> {_srt_time(b)}")
        lines.append(text)
        lines.append("")
    _atomic_write_text(out, "\n".join(lines))
    return out


def build_srt(shots: list["Shot"], out: Path, *, fps: int = FPS) -> Path:
    """镜头表(dialogue/narration) → .srt（UTF-8 无 BOM，原子写）。

    时间轴 = 各镜时长顺序累加，cut 模式即最终成片时间轴。
    **时长优先取 `sec_actual`**（A2）；镜头表里还没有该字段时才退回 planned `sec`。
    需要"拿不到 sec_actual 就用 ffprobe 现读"时请走 assemble()，这个函数没有文件路径。
    """
    return _write_srt(_build_cues(shots, fps), out)


# ---------------------------------------------------------------- 探测


def _probe(path: Path) -> dict:
    p = _run([
        _which(FFPROBE), "-v", "error", "-print_format", "json",
        "-show_streams", "-show_format", str(path),
    ])
    if p.returncode != 0:
        raise RuntimeError(f"ffprobe 读不了 {path.name}：{_err(p)}")
    info = json.loads(p.stdout or "{}")
    streams = info.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if v is None:
        raise RuntimeError(f"{path.name} 没有视频流")
    try:
        fps = float(Fraction(v.get("avg_frame_rate") or "0/1"))
    except (ZeroDivisionError, ValueError):
        fps = 0.0
    dur = None
    for src in (v, a, info.get("format") or {}):
        try:
            d = float((src or {}).get("duration"))
            if d > 0:
                dur = d
                break
        except (TypeError, ValueError):
            continue
    return {
        "width": int(v.get("width") or 0),
        "height": int(v.get("height") or 0),
        "fps": round(fps, 3),
        "vcodec": v.get("codec_name") or "",
        "pix_fmt": v.get("pix_fmt") or "",
        "has_audio": a is not None,
        "acodec": (a or {}).get("codec_name") or "",
        "ar": int((a or {}).get("sample_rate") or 0),
        "ch": int((a or {}).get("channels") or 0),
        "v_dur": dur,
    }


def _params_consistent(probes: list[dict]) -> bool:
    """所有片段的关键参数是否一致 —— 一致才敢用流复制拼接。"""
    if not probes:
        return False
    keys = ("width", "height", "fps", "vcodec", "pix_fmt", "has_audio", "acodec", "ar", "ch")
    first = {k: probes[0][k] for k in keys}
    return all({k: p[k] for k in keys} == first for p in probes)


def _target_spec(proj, probes: list[dict]) -> tuple[int, int, int]:
    """优先用项目参数（project.json 的 width/height/fps），其次用多数片段的实际值。"""
    cfg = _project_cfg(proj)
    try:
        w = int(cfg.get("width") or 0)
        h = int(cfg.get("height") or 0)
        fps = int(cfg.get("fps") or 0)
    except (TypeError, ValueError):
        w = h = fps = 0
    if not w or not h:
        from collections import Counter

        common = Counter((p["width"], p["height"]) for p in probes).most_common(1)
        if common:
            w = w or common[0][0][0]
            h = h or common[0][0][1]
    return (w or 864), (h or 480), (fps or FPS)


# ---------------------------------------------------------------- 拼接 / 编码


def _write_concat_list(clips: list[Path], listfile: Path) -> Path:
    """concat 清单：**绝对路径**（相对路径会在清单目录里找文件，踩过）。"""
    lines = []
    for c in clips:
        # concat 清单是 file '<路径>' 语法，路径里的单引号要转义，否则清单被解析错
        lines.append("file '" + str(Path(c).resolve()).replace("'", "'\\''") + "'")
    _atomic_write_text(listfile, "\n".join(lines) + "\n")
    return listfile


def _concat_copy(clips: list[Path], dst: Path, tmpdir: Path) -> bool:
    """流复制拼接（同源同参数时无损且极快）。失败返回 False，由调用方 fallback。"""
    listfile = _write_concat_list(clips, tmpdir / "concat.txt")
    p = _run([
        _which(FFMPEG), "-y", "-v", "error", "-nostdin",
        "-f", "concat", "-safe", "0", "-i", str(listfile),
        "-c", "copy", "-movflags", "+faststart", str(dst),
    ])
    if p.returncode != 0:
        print(f"[asm] 流复制拼接失败，回退重编码：{_err(p)}")
        return False
    return True


def _finalize(
    src: Path, out: Path, *, srt: Path | None, font: str, height: int,
    width: int, fps: int, tmpdir: Path,
) -> None:
    """一次编码把 intermediate 落成最终规格（顺带烧字幕）。"""
    # 本函数用 cwd=tmpdir 跑 ffmpeg（为了 sub.srt 的相对名，躲开 filtergraph 转义），
    # 所以所有路径必须绝对化 —— 相对路径会在那个 cwd 下"找不到文件"。
    src = Path(src).resolve()
    out = Path(out).resolve()
    cmd = [_which(FFMPEG), "-y", "-v", "error", "-nostdin", "-i", str(src)]
    has_audio = _probe(src)["has_audio"] if src.exists() else False
    if not has_audio:
        cmd += ["-f", "lavfi", "-i", f"anullsrc=r={AUDIO_RATE}:cl=stereo"]
    vf = None
    if srt is not None:
        # srt 复制成纯 ASCII 相对名 + cwd=临时目录：彻底躲开 filtergraph 路径转义
        shutil.copyfile(srt, tmpdir / "sub.srt")
        vf = f"subtitles=sub.srt:force_style='{_subtitle_style(font, height)}'"
    if vf:
        cmd += ["-vf", vf]
    cmd += ["-map", "0:v:0", "-map", "0:a:0" if has_audio else "1:a:0"]
    cmd += [
        "-c:v", "libx264", "-preset", PRESET, "-crf", CRF, "-pix_fmt", "yuv420p",
        "-r", str(fps),
        "-c:a", "aac", "-ar", AUDIO_RATE, "-b:a", AUDIO_BITRATE, "-ac", "2",
        "-movflags", "+faststart", "-f", "mp4", str(out),
    ]
    p = _run(cmd, cwd=tmpdir)
    if p.returncode != 0:
        raise RuntimeError(f"最终编码失败：{_err(p)}")


def _build_filter_graph(
    clips: list[Path], probes: list[dict], *, width: int, height: int, fps: int,
    transition: str, srt_name: str | None, font: str, trims: list[float] | None = None,
) -> tuple[list[str], str, str, str]:
    """构造单趟 filter_complex 的输入参数与滤镜图。

    返回 (输入参数, filter_complex 字符串, 视频标签, 音频标签)。
    · 没有音轨的片段用 anullsrc 补静音，保证 concat 的 a=1 通道数对齐
    · trims[i] > 0 时用 trim/atrim 剪掉开头（C5）：音频视频一起剪，
      并 setpts/asetpts 重置时基，否则 concat 会带上被剪掉的时间戳
    """
    args: list[str] = []
    chains: list[str] = []
    vlabs: list[str] = []
    alabs: list[str] = []
    idx = 0
    for i, clip in enumerate(clips):
        args += ["-i", str(clip)]
        vi = idx
        idx += 1
        p = probes[i]
        if p["has_audio"]:
            ai = vi
        else:
            dur = p["v_dur"] or 1.0
            args += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", f"anullsrc=r={AUDIO_RATE}:cl=stereo"]
            ai = idx
            idx += 1

        dur = p["v_dur"] or 0.0
        trim = float(trims[i]) if trims else 0.0
        trim = max(0.0, min(trim, max(0.0, dur - 0.001)))  # 不许裁到 <=0
        eff = max(0.0, dur - trim)
        pre = f"trim=start={trim:.3f},setpts=PTS-STARTPTS," if trim > 0 else ""
        vf = (
            f"{pre}scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}"
        )
        if transition == "fade" and eff > 2 * FADE_DUR:
            vf += f",fade=t=in:st=0:d={FADE_DUR},fade=t=out:st={eff - FADE_DUR:.3f}:d={FADE_DUR}"
        chains.append(f"[{vi}:v]{vf}[v{i}]")
        vlabs.append(f"v{i}")

        apre = f"atrim=start={trim:.3f},asetpts=PTS-STARTPTS," if trim > 0 else ""
        af = f"{apre}aformat=sample_fmts=fltp:sample_rates={AUDIO_RATE}:channel_layouts=stereo"
        if transition == "fade" and eff > 2 * FADE_DUR:
            af += f",afade=t=in:st=0:d={FADE_DUR},afade=t=out:st={eff - FADE_DUR:.3f}:d={FADE_DUR}"
        chains.append(f"[{ai}:a]{af}[a{i}]")
        alabs.append(f"a{i}")

    n = len(clips)
    if transition == "dissolve" and n >= 2:
        # xfade 需要每处的 offset = 之前所有片段**有效时长**之和 - 已消耗的重叠
        cur_v, cur_a = vlabs[0], alabs[0]
        offset = (probes[0]["v_dur"] or 0.0) - (float(trims[0]) if trims else 0.0) - XFADE_DUR
        for i in range(1, n):
            chains.append(
                f"[{cur_v}][{vlabs[i]}]xfade=transition=fade:duration={XFADE_DUR}:offset={offset:.3f}[vx{i}]"
            )
            chains.append(f"[{cur_a}][{alabs[i]}]acrossfade=d={XFADE_DUR}:c1=tri:c2=tri[ax{i}]")
            cur_v, cur_a = f"vx{i}", f"ax{i}"
            offset += (probes[i]["v_dur"] or 0.0) - (float(trims[i]) if trims else 0.0) - XFADE_DUR
        vcat, acat = cur_v, cur_a
    else:
        ins = "".join(f"[{vlabs[i]}][{alabs[i]}]" for i in range(n))
        chains.append(f"{ins}concat=n={n}:v=1:a=1[vcat][acat]")
        vcat, acat = "vcat", "acat"

    vout = vcat
    if srt_name:
        chains.append(f"[{vcat}]subtitles={srt_name}:force_style='{_subtitle_style(font, height)}'[vsub]")
        vout = "vsub"
    return args, ";".join(chains), vout, acat


def _encode_filtered(
    clips: list[Path], probes: list[dict], out: Path, *, width: int, height: int, fps: int,
    transition: str, srt: Path | None, font: str, tmpdir: Path, trims: list[float] | None = None,
) -> None:
    """单趟 filter_complex 编码落成片（转场 / 参数不一致时 / 有起始裁剪时的 fallback）。"""
    # 同 _finalize：cwd=tmpdir 跑 ffmpeg，路径必须绝对化
    clips = [Path(c).resolve() for c in clips]
    out = Path(out).resolve()
    srt_name = None
    if srt is not None:
        shutil.copyfile(srt, tmpdir / "sub.srt")
        srt_name = "sub.srt"
    args, graph, vout, aout = _build_filter_graph(
        clips, probes, width=width, height=height, fps=fps,
        transition=transition, srt_name=srt_name, font=font, trims=trims,
    )
    cmd = [_which(FFMPEG), "-y", "-v", "error", "-nostdin"] + args + [
        "-filter_complex", graph,
        "-map", f"[{vout}]", "-map", f"[{aout}]",
        "-c:v", "libx264", "-preset", PRESET, "-crf", CRF, "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", AUDIO_RATE, "-b:a", AUDIO_BITRATE, "-ac", "2",
        "-movflags", "+faststart", "-f", "mp4", str(out),
    ]
    p = _run(cmd, cwd=tmpdir)
    if p.returncode != 0:
        raise RuntimeError(f"filter_complex 编码失败：{_err(p)}")


# ---------------------------------------------------------------- 时长账本


def _record_assemble_state(
    proj, rows: list[dict], *, episode: str, out: Path, srt_path: Path,
    transition: str, trim_default: float, planned_total: float,
    actual_total: float, effective_total: float, log,
) -> None:
    """把 A2/C5 的时长账本落盘。

    ① `state/assemble.json` —— 完整明细（planned / sec_actual / 来源 / trim / effective + 总账），
       给审计、Web UI 和 Lead 复核用。这是本模块自己的产物。
    ② `state/manifest.json` —— 被裁过的镜头在 note 里打 `trim_start=0.140s` 标签
       （Lead 要求"记录到 manifest：哪个镜头裁了、裁了多少"）。
       走 state.py 现成的 `Manifest` API（load → 改 note → save），**不新增字段、不改 state.py**，
       这样 gen.py 记录的 fp/size/qc 都不会被动到。

    两者失败都只告警不阻断：账本问题不该让成片出不来。
    """
    payload = {
        "generated_at": int(__import__("time").time()),
        "episode": episode,
        "out": str(out),
        "srt": str(srt_path),
        "transition": transition,
        "trim_start_default": round(trim_default, 3),
        "planned_total": round(planned_total, 3),
        "actual_total": round(actual_total, 3),
        "effective_total": round(effective_total, 3),
        "planned_vs_actual": round(planned_total - actual_total, 3),
        "trimmed_shots": [r["id"] for r in rows if r["trim"] > 0],
        "shots": {r["id"]: r for r in rows},
    }
    try:
        _atomic_write_text(
            proj.state_dir / "assemble.json",
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
    except OSError as e:
        log(f"[asm] 警告：写 state/assemble.json 失败（{e}）")

    trimmed = [r for r in rows if r["trim"] > 0]
    if not trimmed:
        return
    try:
        import re as _re

        try:
            from .state import Manifest  # type: ignore
        except ImportError:
            from state import Manifest  # type: ignore

        mf = Manifest.load(proj.state_dir / "manifest.json")
        touched = 0
        for r in trimmed:
            e = mf.shots.get(r["id"])
            if e is None:
                continue
            tag = f"trim_start={r['trim']:.3f}s"
            e.note = (
                _re.sub(r"trim_start=[\d.]+s", tag, e.note)
                if "trim_start=" in e.note
                else ((e.note + "; " if e.note else "") + tag)
            )
            touched += 1
        mf.save()
        log(f"[asm] 起始裁剪已记入 manifest：{touched} 镜（明细见 state/assemble.json）")
    except Exception as e:  # noqa: BLE001 —— 账本失败不该炸掉成片
        log(f"[asm] 警告：起始裁剪未能写进 manifest（{e}）")


# ---------------------------------------------------------------- 主流程


def assemble(
    proj: "Project", *, episode: str = "EP01", transition: str = "cut",
    burn_subtitle: bool = True, skip_shots: list[str] | None = None, log=None,
    chapters: list[int] | None = None, include_qc_fail: bool = False,
) -> Path:
    """按镜头表顺序把 clips/*.mp4 拼成 final/<episode>.mp4。

    · 返回成片路径；失败抛异常（不产出半成品：先写 .tmp 再原子替换）
    · 字幕：总是导出 final/<episode>.srt；burn_subtitle=True 时再烧进画面
    · transition: cut / fade / dissolve
    """
    log = log or (lambda m: print(m))
    if transition not in ("cut", "fade", "dissolve"):
        raise ValueError(f"不支持的转场 {transition!r}（可选 cut/fade/dissolve）")
    proj.ensure()

    shots = _load_shots_dir(proj.shots_dir)
    if not shots:
        raise RuntimeError(f"镜头表为空：{proj.shots_dir}（先跑 plan）")

    # ── 质检结论必须参与合成决策（2026-09-24 修）────────────────────────────
    # 原来 `assemble.py` **零处提到质检**：`qc.json` 里的 rerender（硬故障）/
    # review（可疑）只被 `/api/qc` 读去给 UI 看，**没有任何代码消费它** ——
    # 于是一个被判"冻结/损坏"的镜头会**静默出现在成片里**，用户看到才发现。
    # 一道到不了决策的判定，等于没有判定。
    qc_fail: list[str] = []
    qc_suspicious: list[str] = []
    try:
        import json as _json
        _qp = Path(proj.state_dir) / "qc.json"
        if _qp.is_file():
            _raw = _json.loads(_qp.read_text(encoding="utf-8")) or {}
            qc_fail = [str(x) for x in (_raw.get("rerender") or [])]
            qc_suspicious = [str(x) for x in (_raw.get("review") or [])]
    except (OSError, ValueError):
        qc_fail, qc_suspicious = [], []

    if qc_fail and not include_qc_fail:
        # 默认**跳过硬故障镜头**并大声说清 —— 宁可成片里少一段，
        # 也不要把明显坏掉的画面悄悄播给观众。
        dropped = [s for s in shots if s.id in set(qc_fail)]
        if len(dropped) == len(shots):
            raise RuntimeError(
                f"全部 {len(shots)} 镜都被质检判为硬故障 —— 拒绝产出一个空成片。"
                f"先重渲：{', '.join(qc_fail[:10])}"
            )
        shots = [s for s in shots if s.id not in set(qc_fail)]
        log(f"⚠ 质检硬故障 {len(qc_fail)} 镜，**已从成片剔除**：{', '.join(qc_fail[:10])}"
            + (" …" if len(qc_fail) > 10 else ""))
        log(f"  （要强行包含就传 include_qc_fail=True；重渲这些镜头后可恢复）")
    elif qc_fail and include_qc_fail:
        log(f"⚠ 质检硬故障 {len(qc_fail)} 镜，按你的要求**强行包含**进成片："
            f"{', '.join(qc_fail[:10])}")

    if qc_suspicious:
        log(f"· 质检可疑 {len(qc_suspicious)} 镜（不剔除，仅提示）："
            f"{', '.join(qc_suspicious[:10])}")

    # ── 集边界（2026-09-24）──────────────────────────────────────────────────
    # 原来 `assemble()` 会把**全部**镜头表拼成一个 EP01，而 `plan` 是按章产出
    # `chapter01.json / chapter02.json` 的 —— 也就是"规划是多集的、合成不是"，
    # 两章会被**静默连成一部**。这里加 `chapters` 过滤，让每一章能单独成集。
    # 章号从**镜头 id 首段**取（`2-1-03` → 第 2 章），比读文件名稳。
    if chapters is not None:
        want = {int(c) for c in chapters}

        def _ch(sid: str) -> int:
            seg = str(sid).split("-")[0]
            try:
                return int(seg)
            except ValueError:
                return 0

        shots = [s for s in shots if _ch(s.id) in want]
        if not shots:
            raise RuntimeError(f"按 chapters={sorted(want)} 过滤后没有可合成的镜头")

    skip = set(skip_shots or [])
    if skip:
        shots = [s for s in shots if s.id not in skip]
    if not shots:
        raise RuntimeError("过滤 skip_shots 后没有可合成的镜头")

    missing = [s.id for s in shots if not proj.clip(s.id).exists()]
    if missing:
        raise FileNotFoundError(
            f"缺少 {len(missing)} 个片段，先跑 render：{', '.join(missing[:10])}"
            + (" …" if len(missing) > 10 else "")
        )
    # 一律解析成绝对路径：_finalize/_encode_filtered 会用 cwd=临时目录跑 ffmpeg
    # （为了躲开 subtitles 滤镜的路径转义），相对路径在那个 cwd 下会"找不到文件"。
    clips = [proj.clip(s.id).resolve() for s in shots]
    probes = [_probe(c) for c in clips]
    width, height, fps = _target_spec(proj, probes)

    # ---- A2：解析三层时长，时间轴一律用 sec_actual（拿不到就 ffprobe 现读，失败即报错）----
    trim_default = trim_config(proj)
    rows: list[dict] = []
    for s, c, pr in zip(shots, clips, probes):
        actual, src = resolve_sec_actual(s, c)
        trim = resolve_trim_start(s, trim_default)
        actual = max(0.0, actual)
        trim = max(0.0, min(trim, max(0.0, actual - 0.001)))  # 不许裁到 <=0
        rows.append({
            "id": s.id,
            "planned": round(planned_sec(s), 3),
            "sec_actual": round(actual, 3),
            "sec_source": src,
            "probe_v_dur": round(pr["v_dur"] or 0.0, 3),
            "trim": round(trim, 3),
            "effective": round(max(0.0, actual - trim), 3),
        })
    durations = [r["effective"] for r in rows]
    planned_total = sum(r["planned"] for r in rows)
    actual_total = sum(r["sec_actual"] for r in rows)
    effective_total = sum(durations)
    trimmed_n = sum(1 for r in rows if r["trim"] > 0)
    max_diff = max((abs(r["sec_actual"] - r["planned"]) for r in rows), default=0.0)
    n_probe = sum(1 for r in rows if r["sec_source"] == "probe")
    log(f"[asm] A2 三层时长：planned {planned_total:.3f}s → actual {actual_total:.3f}s"
        f"（净差 {planned_total - actual_total:+.3f}s，单镜最大偏差 {max_diff:.3f}s；"
        f"来源：ffprobe 现读 {n_probe} 镜 / 镜头表 sec_actual {len(rows) - n_probe} 镜）")
    if max_diff > 0.5:
        log(f"[asm] 注意：有镜头实际时长与规划差 > 0.5s（最大 {max_diff:.3f}s），"
            f"可能是 H3 时长降级或人工 override，明细见 state/assemble.json")
    if trimmed_n:
        log(f"[asm] C5 起始裁剪：{trimmed_n} 镜 × {trim_default:.3f}s，"
            f"有效总时长 {effective_total:.3f}s（裁剪已从时间轴扣掉，不是新的漂移）")
    else:
        log("[asm] C5 起始裁剪：关闭（trim_start_sec=0.0）"
            "—— 实测本项目 52 镜无统一的 0.12-0.16s 起始杂音")

    # 字幕：.srt 总是导出；是否烧录由 burn_subtitle 决定
    font = ""
    if burn_subtitle:
        font = _resolve_font()
        if not _has_filter("subtitles"):
            raise RuntimeError(
                "ffmpeg 没有 subtitles 滤镜（缺 libass），无法烧录字幕。\n"
                "  请安装带 libass 的 ffmpeg：sudo apt install ffmpeg libass9"
            )
    overlap = XFADE_DUR if transition == "dissolve" else 0.0
    srt_path = (proj.final_dir / f"{episode}.srt").resolve()
    tmp_srt = (proj.final_dir / f".{episode}.tmp.srt").resolve()
    # ★ A2：字幕时间轴用 actual（再减去 C5 裁剪）累计
    cues = _build_cues(shots, fps, overlap, durations=durations)
    # 先写临时 srt，编码成功后再和成片一起原子生效：否则编码失败会留下
    # "新字幕 + 旧成片"的混装状态（这个坑我第一版真踩了）
    _write_srt(cues, tmp_srt)

    has_cue = bool(cues)
    burn = bool(burn_subtitle and has_cue)
    if burn_subtitle and not has_cue:
        log("[asm] 镜头表里没有 dialogue/narration，跳过字幕烧录（.srt 已导出为空）")
    if cues:
        last_end = cues[-1][1]
        log(f"[asm] 字幕时间轴按实际时长累计：末条结束 {last_end:.3f}s"
            + (f"（若按请求的 sec 累计会是 {planned_total:.3f}s，相差 {last_end - planned_total:+.3f}s）"
               if abs(last_end - planned_total) > 0.01 and overlap == 0.0 else ""))

    out = (proj.final_dir / f"{episode}.mp4").resolve()
    log(
        f"[asm] {len(clips)} 镜 / 实际 {effective_total:.2f}s / {width}×{height}@{fps} / 转场={transition} / "
        f"字幕={'烧录+' if burn else ''}.srt"
        + (f" / 字体={font}" if burn else "")
    )

    trims = [r["trim"] for r in rows]
    tmpdir = Path(tempfile.mkdtemp(prefix="vm-asm-"))
    tmp_out = (proj.final_dir / f".{episode}.tmp.mp4").resolve()
    try:
        if transition == "cut" and trimmed_n == 0:
            joined = tmpdir / "joined.mp4"
            if _params_consistent(probes) and _concat_copy(clips, joined, tmpdir):
                log("[asm] concat 流复制成功（参数一致），单次编码落规格")
                _finalize(
                    joined, tmp_out, srt=tmp_srt if burn else None, font=font,
                    height=height, width=width, fps=fps, tmpdir=tmpdir,
                )
            else:
                log("[asm] 参数不一致，改用 filter_complex 重编码拼接")
                _encode_filtered(
                    clips, probes, tmp_out, width=width, height=height, fps=fps,
                    transition="cut", srt=tmp_srt if burn else None, font=font, tmpdir=tmpdir,
                )
        else:
            if trimmed_n:
                log(f"[asm] 有 {trimmed_n} 镜要裁开头，走 filter_complex（流复制不能裁剪）")
            _encode_filtered(
                clips, probes, tmp_out, width=width, height=height, fps=fps,
                transition=transition, srt=tmp_srt if burn else None, font=font,
                tmpdir=tmpdir, trims=trims,
            )
        os.replace(tmp_out, out)      # 成片先原子生效
        os.replace(tmp_srt, srt_path)  # 字幕紧随其后，两者始终成对
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        for leftover in (tmp_out, tmp_srt):
            if leftover.exists():
                leftover.unlink()

    # 时长账本：manifest 打 trim 标签 + state/assemble.json 明细（审计用）
    _record_assemble_state(
        proj, rows, episode=episode, out=out, srt_path=srt_path,
        transition=transition, trim_default=trim_default,
        planned_total=planned_total, actual_total=actual_total,
        effective_total=effective_total, log=log,
    )

    size_mb = out.stat().st_size / 1024 / 1024
    log(f"[asm] 完成：{out}（{size_mb:.1f} MB），字幕：{srt_path}")
    return out


# ---------------------------------------------------------------- CLI


def _cli(argv: list[str]) -> int:
    """`python vm/assemble.py <项目目录> [--episode EP01] [--transition cut|fade|dissolve] [--no-burn] [--skip ...]`"""
    import argparse

    try:
        from .state import Project  # type: ignore
    except ImportError:
        from state import Project  # type: ignore

    ap = argparse.ArgumentParser(description="合成成片（concat + 中文字幕）")
    ap.add_argument("project", help="项目目录，如 projects/西游记")
    ap.add_argument("--episode", default="EP01")
    ap.add_argument("--transition", default="cut", choices=["cut", "fade", "dissolve"])
    ap.add_argument("--no-burn", action="store_true", help="只导出 .srt，不烧进画面")
    ap.add_argument("--skip", default="", help="跳过的镜头号，逗号分隔")
    ap.add_argument("--trim-start", type=float, default=None,
                    help="C5 起始裁剪秒数（默认 0=关闭；实测本项目 52 镜无常统一 0.12-0.16s 杂音，"
                         "开了会切进 30/52 镜的语音，谨慎使用）")
    a = ap.parse_args(argv)

    if a.trim_start is not None:
        # 复用统一的配置读取顺序（env 优先级最高），避免再给 assemble() 加参数
        os.environ["VM_TRIM_START_SEC"] = str(a.trim_start)

    proj = Project(Path(a.project))
    skip = [x.strip() for x in a.skip.split(",") if x.strip()]
    assemble(
        proj, episode=a.episode, transition=a.transition,
        burn_subtitle=not a.no_burn, skip_shots=skip, log=print,
    )
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_cli(sys.argv[1:]))
