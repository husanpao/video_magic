"""
qc.py —— 机械质检三防线（确定性、零 GPU、纯 ffmpeg + 标准库）。

为什么要有这个模块：
  H3 生成的片段里有几类"看着像成品、其实不能进成片"的毛病，靠人逐条看 118 镜太贵：
    ① 纯色/故障片   —— 模型崩了，整段没有画面内容
    ② 近黑夜戏      —— 正常画面，**绝不能误杀**（用户真实语料里有亮度 10 的正常夜戏）
    ③ 段尾冻结      —— H3 典型病：提前到达末帧后静止到片尾
    ④ 静音 / 无音轨 / 音画不同步 / 时长不对
  机械质检负责把这些挑出来，且必须区分「检查通过」与「检查未执行」——
  静默判过比不检更危险（PIPELINE-DESIGN §5.2 的血泪教训）。

判据来自 vm/CONTRACTS.md（冻结版，改判据要先改契约）：
  | 检查       | 判据                                                        |
  | 纯色/故障  | 采样帧灰度 std < 3 **且** 色数 < 10                          |
  | 近黑可疑   | std >= 3 **且** 平均亮度 < 20                                |
  | 静音       | 音轨 RMS < 0.02 且峰值 < 0.06                                |
  | 段尾冻结   | 末尾 25% 窗口内相邻帧**逐像素绝对差**均值 < 0.5（v2 判据，见下）  |
  | 时长       | 与目标差 > 1s                                                |
  | 音轨/同步  | 无 audio stream；video 与 audio 时长差 > 0.5s                 |
  | 未执行     | 采样成功帧数为 0 → **必须显式报错**，不可静默判过             |

★ 最容易写错的一条：不要用 `std < 8 → 故障`。
  纯色故障必须"std 低"与"色数少"**同时**成立；近黑只能判"可疑"。
  只按 std 判故障，会把正常夜戏（实测亮度 10 / std 15.3 / 色数 103）全部打回重渲。

★ 第二条同样踩过坑：段尾冻结**不能**用"相邻帧灰度均值差"。
  画面有运动时平均灰度几乎不变（运动改变的是像素分布，不是均值）——实测 52 个真实镜头
  100% 被判可疑，而人工标定的 0s/0.6s/1.2s/1.8s 冻结尾巴该指标是 0.971/0.974/0.976/1.000，
  从 0.971 到 1.000 几乎不动，**对冻结零敏感**。v2 改为逐像素绝对差后单调可辨
  （同源标定 1.393/0.887/0.448/0.068）。旧的 freeze_ratio 仍保留在 metrics 里，仅作对比。

严重级别（决定 `ok` 与 `metrics["verdict"]`）：
  fail        硬故障，需要重渲/处理：纯色故障帧、静音、无音轨、时长异常、音画不同步
  error       检查根本没跑成：文件缺失/损坏、抽帧失败、samples=0  →  ok=False
  suspicious  可疑/告警，交人工确认，**不阻塞**：近黑、段尾冻结      →  ok=True
  pass        全绿

  `ok=True` 只代表"没有硬故障"，不代表"没毛病"；调用方要看 issues / verdict。
  这样设计的理由：可疑项（尤其夜戏）绝不能触发自动重渲，否则就是用户最怕的误杀。

实现方式（不用 numpy/PIL，避免 PIPELINE-DESIGN §5.2 那种"回退分支静默失效"）：
  · 逐帧灰度整帧：ffmpeg 缩到 160×160 输出 rawvideo gray，Python 算
                  （段尾冻结要逐像素差，所以必须留整帧，不能只留均值）
  · 采样帧统计  ：ffmpeg 缩到 160×120 输出 rawvideo rgb24，Python 算 std / 色数
  · 音轨统计    ：解码成 8kHz 单声道 s16le，Python 算 RMS / 峰值（不依赖 signalstats 的
                  metadata 输出，那条路一改日志级别就会被吞掉）
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 只为类型注解，运行时不 import
    from .state import Project

# ---------------------------------------------------------------- 阈值（契约冻结）

STD_FAULT = 3.0          # 纯色/故障：灰度 std 上限
COLORS_FAULT = 10        # 纯色/故障：帧内不同颜色数上限
DARK_SUSPECT = 20.0      # 近黑可疑：平均亮度上限（必须配合 std >= STD_FAULT）
SILENCE_RMS = 0.02       # 静音：RMS 上限
SILENCE_PEAK = 0.06      # 静音：峰值上限
FREEZE_TAIL = 0.25       # 段尾冻结：只看末尾 25% 窗口
FREEZE_PIXEL_DIFF = 0.5  # ★ 段尾冻结主判据：相邻帧**逐像素绝对差**均值的下限
FREEZE_DIFF = 0.8        # 【已废弃】旧判据阈值：相邻帧灰度均值差
FREEZE_RATIO = 0.6       # 【已废弃】旧判据阈值：静止差占比
FREEZE_SIDE = 160        # 冻结判据的灰度分辨率（160×160，与标定表同尺度）
DUR_TOL = 1.0            # 时长容差（秒）
AV_TOL = 0.5             # 音画时长差容差（秒）
MIN_FREEZE_FRAMES = 8    # 帧数太少时冻结检测无意义，显式标注"未执行"

SEV_FAULT = "故障"
SEV_WARN = "可疑"
SEV_SKIP = "未执行"

FFMPEG = os.environ.get("VM_FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("VM_FFPROBE", "ffprobe")
RUN_TIMEOUT = 300        # 单次 ffmpeg/ffprobe 超时（秒）；15s 片源远用不到

# 画面统计用的降采样尺寸：够算出 std/色数，又不至于产生几十 MB 的管道数据
GRAY_SIDE = 64
SAMPLE_W, SAMPLE_H = 160, 120


@dataclass
class QCResult:
    """单个镜头的质检结论。

    ok 的语义（重要）：True = 没有硬故障，可进成片；False = 有硬故障或检查未执行。
    "可疑"（夜戏、段尾冻结）算 ok=True，但会出现在 issues 里、verdict == "suspicious"。
    """

    shot: str
    ok: bool
    issues: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    @property
    def verdict(self) -> str:
        return str(self.metrics.get("verdict", "pass"))

    def to_dict(self) -> dict:
        return {
            "shot": self.shot,
            "ok": self.ok,
            "verdict": self.verdict,
            "issues": list(self.issues),
            "metrics": dict(self.metrics),
        }


# ---------------------------------------------------------------- 小工具


def _atomic_write_json(path: Path, data) -> None:
    """先写 .tmp 再 os.replace，避免掉电/被杀留下半截 JSON。

    这里刻意复制 state.py 的同名私有函数（而不是 import 下划线名字）：
    私有函数跨模块引用会让重构互相牵连，10 行代码不值得那个耦合。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _which(tool: str) -> str:
    exe = shutil.which(tool)
    if not exe:
        raise RuntimeError(f"找不到 {tool}，请先安装 ffmpeg（sudo apt install ffmpeg）")
    return exe


def _run(cmd: list[str], *, binary: bool = False, cwd: Path | None = None):
    """跑子进程。binary=True 时拿 bytes（rawvideo 管道），否则拿 text。"""
    p = subprocess.run(
        cmd,
        capture_output=True,
        timeout=RUN_TIMEOUT,
        cwd=str(cwd) if cwd else None,
        **({} if binary else {"text": True}),
    )
    return p


def _fail_msg(p) -> str:
    err = p.stderr.decode("utf-8", "replace") if isinstance(p.stderr, bytes) else (p.stderr or "")
    err = " ".join(err.strip().split())
    return err[-300:] if err else f"exit={p.returncode}"


def _result(shot: str, issues: list[str], metrics: dict) -> QCResult:
    """统一出口：由 issues 的严重级别前缀推导 verdict / ok（单一事实来源）。"""
    if any(i.startswith(SEV_SKIP) for i in issues):
        verdict = "error"
    elif any(i.startswith(SEV_FAULT) for i in issues):
        verdict = "fail"
    elif issues:
        verdict = "suspicious"
    else:
        verdict = "pass"
    m = dict(metrics)
    m["verdict"] = verdict
    return QCResult(shot=shot, ok=verdict in ("pass", "suspicious"), issues=issues, metrics=m)


# ---------------------------------------------------------------- ffprobe


def _ffprobe(path: Path) -> dict:
    p = _run(
        [
            _which(FFPROBE), "-v", "error",
            "-print_format", "json",
            "-show_streams", "-show_format",
            str(path),
        ]
    )
    if p.returncode != 0:
        raise RuntimeError(f"ffprobe 读不了这个文件：{_fail_msg(p)}")
    try:
        return json.loads(p.stdout or "{}")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"ffprobe 输出不是合法 JSON：{e}")


def _pick_streams(info: dict):
    streams = info.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    return v, a


def _dur_of(stream: dict | None, info: dict) -> float | None:
    """流时长优先，缺了退到容器时长（mp4 里 AAC 流常常没有 duration 字段）。"""
    if stream:
        try:
            d = float(stream.get("duration"))
            if d > 0:
                return d
        except (TypeError, ValueError):
            pass
    try:
        d = float((info.get("format") or {}).get("duration"))
        return d if d > 0 else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 画面统计


def _gray_frames(path: Path, side: int = FREEZE_SIDE) -> list[bytes]:
    """逐帧灰度原始数据（side×side，每帧 side*side 字节）。

    **必须保留整帧、不能只留均值** —— 见 `_freeze_metrics` 的说明：
    段尾冻结要看"像素有没有变"，而平均灰度对运动几乎不敏感。
    注意用 `-v error` 但走 **rawvideo stdout**，不解析任何日志文本，天然不会踩
    PIPELINE-DESIGN §5.2 那个"日志级别吞掉 metadata"的坑。
    """
    p = _run(
        [
            _which(FFMPEG), "-v", "error", "-nostdin",
            "-i", str(path),
            "-map", "0:v:0", "-an",
            "-vf", f"scale={side}:{side}:flags=area,format=gray",
            "-f", "rawvideo", "-pix_fmt", "gray", "-",
        ],
        binary=True,
    )
    if p.returncode != 0:
        raise RuntimeError(f"逐帧灰度提取失败：{_fail_msg(p)}")
    buf = p.stdout or b""
    fs = side * side
    return [buf[i * fs:(i + 1) * fs] for i in range(len(buf) // fs)]


def _sample_rgb_frames(path: Path, samples: int, duration: float | None) -> list[bytes]:
    """均匀抽 samples 帧，返回 rgb24 原始字节。

    用 `fps=<samples/时长>` 让 ffmpeg 自己均匀取帧：不用逐帧 seek（5 个进程），
    也不用 select 表达式拼转义（易错）。
    """
    dur = duration if (duration and duration > 0) else 1.0
    rate = samples / dur
    rate = min(max(rate, 0.05), 60.0)  # 极短片/极长片都要夹住，fps 过滤器的参数得有界
    p = _run(
        [
            _which(FFMPEG), "-v", "error", "-nostdin",
            "-i", str(path),
            "-map", "0:v:0", "-an",
            "-vf", f"fps={rate:.6f},scale={SAMPLE_W}:{SAMPLE_H}:flags=area,format=rgb24",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ],
        binary=True,
    )
    if p.returncode != 0:
        raise RuntimeError(f"抽帧失败：{_fail_msg(p)}")
    buf = p.stdout or b""
    fs = SAMPLE_W * SAMPLE_H * 3
    n = len(buf) // fs
    return [buf[i * fs:(i + 1) * fs] for i in range(n)]


def _stats_of_rgb(frame: bytes) -> tuple[float, float, int]:
    """一帧 RGB 的 (灰度均值, 灰度标准差, 不同颜色数)。

    灰度用整数加权 0.299/0.587/0.114 —— 和用户现有脚本、以及 PIPELINE-DESIGN
    里说的"全范围 0-255"口径一致（不用 signalstats 的有限范围 YAVG，省得换算）。
    """
    n = SAMPLE_W * SAMPLE_H
    tot = 0
    tot2 = 0
    colors = set()
    for i in range(0, n * 3, 3):
        r = frame[i]
        g = frame[i + 1]
        b = frame[i + 2]
        y = (r * 299 + g * 587 + b * 114) // 1000
        tot += y
        tot2 += y * y
        colors.add(frame[i:i + 3])
    mean = tot / n
    var = tot2 / n - mean * mean
    std = math.sqrt(var) if var > 0 else 0.0
    return mean, std, len(colors)


def _freeze_metrics(frames: list[bytes], side: int = FREEZE_SIDE) -> dict:
    """段尾冻结：**主判据 = 末尾 25% 窗口内相邻帧「逐像素绝对差」的均值**。

    ★ 为什么必须换掉旧判据（真事故，不是理论问题）
      旧判据用的是"相邻帧**灰度均值**差 < 0.8 的占比"。但**画面里有运动时平均灰度几乎不变**
      —— 运动改变的是像素分布，不是均值。实测 52 个真实镜头全部被判可疑
      （ratio 0.97~1.00，全中等于没判），而人工标定的 0s/0.6s/1.2s/1.8s 冻结尾巴
      ratio 分别是 0.971/0.974/0.976/1.000 —— 从 0.971 到 1.000 几乎不动，对冻结零敏感。

      换成逐像素绝对差后（同源标定，160×160 灰度）：

        freeze    旧 ratio    新 pixel_diff
        0s        0.971       1.393   ← 有运动
        0.6s      0.974       0.887
        1.2s      0.976       0.448   ← 低于 0.5，可检出
        1.8s      1.000       0.068   ← 稳定检出

    返回 dict：
      freeze_pixel_diff  主判据值（< FREEZE_PIXEL_DIFF 判冻结）
      freeze_ratio       【已废弃】旧判据值，仅保留供对比，不参与判定
      freeze_body_diff   非尾部窗口的逐像素差（诊断用，便于看出"全片都慢"还是"只有尾巴慢"）
      freeze_frames      参与统计的窗口内相邻帧对数
    帧数不足 MIN_FREEZE_FRAMES 时各项为 None（调用方标记"未执行"）。
    """
    n = len(frames)
    blank = {"freeze_pixel_diff": None, "freeze_ratio": None,
             "freeze_body_diff": None, "freeze_frames": 0}
    if n < MIN_FREEZE_FRAMES:
        return blank
    fs = side * side
    means = [sum(f) / fs for f in frames]  # 只为算已废弃的旧指标
    start = int(n * (1.0 - FREEZE_TAIL))   # 窗口起点：只算窗口内的相邻对
    pairs = list(range(start + 1, n))
    if not pairs:
        return blank

    def pixdiff(i: int) -> float:
        a, b = frames[i - 1], frames[i]
        return sum(abs(x - y) for x, y in zip(a, b)) / fs

    tail = [pixdiff(i) for i in pairs]
    body_pairs = list(range(1, max(1, start)))
    return {
        "freeze_pixel_diff": sum(tail) / len(tail),
        "freeze_ratio": sum(1 for i in pairs if abs(means[i] - means[i - 1]) < FREEZE_DIFF) / len(pairs),
        "freeze_body_diff": (sum(pixdiff(i) for i in body_pairs) / len(body_pairs)) if body_pairs else None,
        "freeze_frames": len(pairs),
    }


# ---------------------------------------------------------------- 音频统计


def _audio_stats(path: Path) -> tuple[float, float, int]:
    """解码成 8kHz 单声道 s16le，返回 (RMS, 峰值, 样本数)，均归一到 [0,1]。"""
    p = _run(
        [
            _which(FFMPEG), "-v", "error", "-nostdin",
            "-i", str(path),
            "-map", "0:a:0", "-vn",
            "-ac", "1", "-ar", "8000",
            "-f", "s16le", "-",
        ],
        binary=True,
    )
    if p.returncode != 0:
        raise RuntimeError(f"音频解码失败：{_fail_msg(p)}")
    import array

    a = array.array("h")
    raw = p.stdout or b""
    a.frombytes(raw[: len(raw) // 2 * 2])  # 丢掉可能的半个样本
    n = len(a)
    if n == 0:
        return 0.0, 0.0, 0
    ss = 0
    peak = 0
    for s in a:
        ss += s * s
        if s < 0:
            s = -s
        if s > peak:
            peak = s
    return math.sqrt(ss / n) / 32768.0, peak / 32768.0, n


# ---------------------------------------------------------------- 单片段质检


def check_clip(path: Path, *, samples: int = 5, target_sec: float | None = None) -> QCResult:
    """对单个片段跑完三防线。返回 QCResult（任何异常都落成 error 结论，不抛出）。

    签名说明：契约是 `check_clip(path, *, samples=5)`。
    `target_sec` 是**向后兼容的可选追加参数**（能让"时长检查"拿到目标秒数）：
    不传就跳过时长比对，既有调用方式完全不受影响。

    为什么不让异常冒出去：质检的价值在于"每条片子都有结论"。
    如果坏文件让 check_clip 抛异常，调用方一 try 一 continue，就又回到静默跳过；
    所以这里把所有失败都变成 verdict="error" 的显式结论。
    """
    p = Path(path)
    shot = p.stem
    m: dict = {
        "mean": None, "std": None, "rms": None, "freeze_ratio": None,
        "v_dur": None, "a_dur": None,
        "sampled": 0, "frames": 0, "fault_frames": 0, "dark_frames": 0,
    }

    if not p.exists() or p.stat().st_size == 0:
        return _result(shot, [f"{SEV_SKIP}: 文件不存在或为空 {p}"], m)

    # samples=0 是最隐蔽的"假质检"：一个采样点都不采，却可能判通过
    if samples <= 0:
        return _result(shot, [f"{SEV_SKIP}: 采样数为 {samples}，画面质检未执行"], m)

    # 1) 元数据
    try:
        info = _ffprobe(p)
    except Exception as e:
        return _result(shot, [f"{SEV_SKIP}: {e}"], m)
    v, a = _pick_streams(info)
    if v is None:
        return _result(shot, [f"{SEV_SKIP}: 没有视频流（文件损坏？）"], m)
    v_dur = _dur_of(v, info)
    a_dur = _dur_of(a, info) if a else None
    m["v_dur"] = round(v_dur, 3) if v_dur is not None else None
    m["a_dur"] = round(a_dur, 3) if a_dur is not None else None

    issues: list[str] = []

    # 2) 抽帧画面统计（std / 色数 / 亮度）
    try:
        frames = _sample_rgb_frames(p, samples, v_dur)
    except Exception as e:
        return _result(shot, [f"{SEV_SKIP}: {e}"], m)
    if not frames:
        # ★ 契约里的"未执行"红线：采样成功帧数为 0 必须显式报错
        return _result(shot, [f"{SEV_SKIP}: 抽帧成功 0 帧，画面质检未执行"], m)

    stats = [_stats_of_rgb(f) for f in frames]
    m["sampled"] = len(stats)
    dark_f = [s for s in stats if s[1] < STD_FAULT and s[2] < COLORS_FAULT]
    # 近黑可疑：std 正常但太暗 —— 必须配合 std，否则正常夜戏会被误杀
    suspect_dark = [s for s in stats if s[1] >= STD_FAULT and s[0] < DARK_SUSPECT]
    m["fault_frames"] = len(dark_f)
    m["dark_frames"] = len(suspect_dark)
    # metrics 里的 mean/std 取"最暗那一帧"，这样和用户描述的口径一致
    # （例如夜戏：亮度 10 / std 15.3），而不是被亮帧平均掉
    darkest = min(stats, key=lambda s: s[0])
    m["mean"] = round(darkest[0], 2)
    m["std"] = round(darkest[1], 2)
    m["colors"] = darkest[2]
    m["mean_avg"] = round(sum(s[0] for s in stats) / len(stats), 2)
    m["std_avg"] = round(sum(s[1] for s in stats) / len(stats), 2)

    if dark_f:
        f0 = dark_f[0]
        issues.append(
            f"{SEV_FAULT}: {len(dark_f)}/{len(stats)} 个采样帧近似纯色"
            f"（std {f0[1]:.2f} < {STD_FAULT} 且色数 {f0[2]} < {COLORS_FAULT}）"
        )
    if suspect_dark:
        issues.append(
            f"{SEV_WARN}: {len(suspect_dark)}/{len(stats)} 个采样帧偏暗"
            f"（亮度 {min(s[0] for s in suspect_dark):.1f} < {DARK_SUSPECT}，"
            f"但 std {min(s[1] for s in suspect_dark):.2f} 正常，需人工确认是否夜戏）"
        )

    # 3) 段尾冻结：主判据 = 相邻帧逐像素绝对差（旧判据只看灰度均值，测不出冻结）
    try:
        gray = _gray_frames(p)
    except Exception as e:
        gray = []
        issues.append(f"{SEV_SKIP}: 段尾冻结检测未执行（{e}）")
    m["frames"] = len(gray)
    if gray:
        fm = _freeze_metrics(gray)
        m.update({k: (round(v, 4) if isinstance(v, float) else v) for k, v in fm.items()})
        if fm["freeze_pixel_diff"] is None:
            issues.append(
                f"{SEV_SKIP}: 帧数过少（{len(gray)} < {MIN_FREEZE_FRAMES}），段尾冻结未检测"
            )
        elif fm["freeze_pixel_diff"] < FREEZE_PIXEL_DIFF:
            issues.append(
                f"{SEV_WARN}: 段尾冻结，末尾 {int(FREEZE_TAIL * 100)}% 窗口内"
                f"相邻帧逐像素绝对差均值 {fm['freeze_pixel_diff']:.3f} < {FREEZE_PIXEL_DIFF}"
                f"（旧判据 freeze_ratio={fm['freeze_ratio']:.3f}，已废弃，仅供参考）"
            )

    # 4) 音轨
    if a is None:
        issues.append(f"{SEV_FAULT}: 没有音频流")
    else:
        try:
            rms, peak, n = _audio_stats(p)
        except Exception as e:
            issues.append(f"{SEV_SKIP}: 音频质检未执行（{e}）")
            n = 0
        if n == 0 and not any(i.startswith(SEV_SKIP) for i in issues):
            issues.append(f"{SEV_SKIP}: 音频解码得到 0 个样本，静音检查未执行")
        if n > 0:
            m["rms"] = round(rms, 4)
            m["peak"] = round(peak, 4)
            if rms < SILENCE_RMS and peak < SILENCE_PEAK:
                issues.append(
                    f"{SEV_FAULT}: 音轨静音（RMS {rms:.4f} < {SILENCE_RMS} "
                    f"且峰值 {peak:.4f} < {SILENCE_PEAK}）"
                )
        # 音画同步
        if v_dur is not None and a_dur is not None:
            d = abs(v_dur - a_dur)
            if d > AV_TOL:
                issues.append(f"{SEV_FAULT}: 音画时长差 {d:.3f}s > {AV_TOL}s")

    # 5) 时长（只有拿到目标秒数才比对）
    if target_sec is not None and v_dur is not None:
        d = abs(v_dur - float(target_sec))
        if d > DUR_TOL:
            issues.append(
                f"{SEV_FAULT}: 时长 {v_dur:.2f}s 与目标 {float(target_sec):.2f}s 差 {d:.2f}s > {DUR_TOL}s"
            )

    return _result(shot, issues, m)


# ---------------------------------------------------------------- 批量质检


def _load_shots_dir(d: Path):
    """延迟 import shots.py：它由 planner 并行开发，且本模块要能独立跑测试。"""
    try:
        from .shots import load_shots_dir  # type: ignore
    except ImportError:
        from shots import load_shots_dir  # type: ignore
    return load_shots_dir(d)


def check_all(proj: "Project", log=None) -> dict[str, QCResult]:
    """对项目里所有镜头跑质检，写 state/qc.json，返回 {镜头号: QCResult}。

    写盘内容（qc.json）：
      results   每个镜头的 ok/verdict/issues/metrics —— Web UI 直接读它
      rerender  硬故障镜头号列表（ok=False），给"重渲队列"
      review    可疑镜头号列表（verdict=suspicious），给人看，**不自动重渲**
    """
    log = log or (lambda msg: None)
    proj.ensure()

    # 优先按镜头表（能拿到目标时长）；shots.py 还没就绪就退回扫 clips 目录
    shots = []
    targets: dict[str, float] = {}
    try:
        shots = _load_shots_dir(proj.shots_dir)
    except Exception as e:
        log(f"[qc] 读不到镜头表（{e}），改为扫描 clips 目录")
    if shots:
        items = []
        for s in shots:
            items.append((s.id, proj.clip(s.id)))
            try:
                targets[s.id] = float(s.sec)
            except (TypeError, ValueError):
                pass
    else:
        items = [(p.stem, p) for p in sorted(proj.clips_dir.glob("*.mp4"))]

    if not items:
        log("[qc] 没有可质检的片段")
    results: dict[str, QCResult] = {}
    for sid, path in items:
        r = check_clip(path, target_sec=targets.get(sid))
        results[sid] = r
        mark = {"pass": "✓", "suspicious": "!", "fail": "✗", "error": "✗"}.get(r.verdict, "?")
        log(f"[qc] {mark} {sid} {r.verdict}" + ("：" + "；".join(r.issues) if r.issues else ""))

    rerender = [sid for sid, r in results.items() if not r.ok]
    review = [sid for sid, r in results.items() if r.verdict == "suspicious"]
    _atomic_write_json(
        proj.state_dir / "qc.json",
        {
            "generated_at": int(time.time()),
            "results": {sid: r.to_dict() for sid, r in results.items()},
            "rerender": rerender,
            "review": review,
        },
    )
    log(
        f"[qc] 共 {len(results)} 镜：通过 {sum(1 for r in results.values() if r.verdict == 'pass')}，"
        f"可疑 {len(review)}，故障/未执行 {len(rerender)}"
    )
    return results


# ---------------------------------------------------------------- CLI


def _cli(argv: list[str]) -> int:
    """`python vm/qc.py <片段或目录> [--samples N] [--target-sec S] [--json]`

    退出码：有硬故障/未执行 → 1，可用于 CI 卡点（沿用 PIPELINE-DESIGN §5.3 的约定）。
    """
    import argparse

    ap = argparse.ArgumentParser(description="机械质检三防线（纯 ffmpeg + 标准库）")
    ap.add_argument("target", help="片段文件或目录")
    ap.add_argument("--samples", type=int, default=5, help="抽帧数，默认 5")
    ap.add_argument("--target-sec", type=float, default=None, help="目标时长（秒），用于时长比对")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    a = ap.parse_args(argv)

    t = Path(a.target)
    paths = sorted(t.glob("*.mp4")) if t.is_dir() else [t]
    if not paths:
        print(f"没有找到 mp4：{t}")
        return 1
    results = {}
    for q in paths:
        r = check_clip(q, samples=a.samples, target_sec=a.target_sec)
        results[q.stem] = r
    if a.json:
        print(json.dumps({k: v.to_dict() for k, v in results.items()}, ensure_ascii=False, indent=2))
    else:
        for sid, r in results.items():
            mark = {"pass": "✓", "suspicious": "!", "fail": "✗", "error": "✗"}.get(r.verdict, "?")
            print(f"{mark} {sid:20s} {r.verdict:10s} " + "；".join(r.issues))
    return 0 if all(r.ok for r in results.values()) else 1


if __name__ == "__main__":
    import sys

    raise SystemExit(_cli(sys.argv[1:]))
