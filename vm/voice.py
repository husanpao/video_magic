"""
voice.py —— 台词 → 配音音频（H3 原生语音栈；复用现有权重，**零额外下载**）。

为什么需要它（2026-09-28）：
    角色镜原来走 `MiniMaxH3ReferenceToVideo`，而那个节点**没有 AUDIO 输入**
    （/object_info 实测：只有 clip/prompt/宽高/参考图）→ 角色镜物理上无法被台词驱动，
    只能让 H3 自己编声。表现就是"嘴说话对不上"、部分镜头音轨静音（实测 3/37 被判不合格）。

链路（全部是节点包自带、本机已实测）：
    MiniMaxH3VoiceProfileT8 → MiniMaxH3SpeechPlanT8 → MiniMaxH3SpeechStudioT8 → SaveAudio
    本机实测：一句中文台词约 5 秒出 32kHz 立体声，与 `drive_audio` 要求的格式一致。

产物约定：`<项目>/voice/<镜头号>.wav`（32kHz 立体声 s16）。
    连同 `<镜头号>.json` 记指纹 —— 台词/音色/参数/种子任一变化都要重出，
    否则会出现"改了台词但配音还是旧的"这种最难查的错。

能力边界（节点包文档原话，别误当成口型方案）：
    H3 **不是确定性口型求解器**；即使最终音轨与输入逐样本一致，嘴型仍可能说别的。
    要广播级口型，必须在出片之后加专门的唇形后处理。

不做什么：不提交渲染、不碰 clips/、不启停 ComfyUI（与 comfy.py / qi.py 同一纪律）。
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .comfy import Comfy, ComfyError

# 必需节点（缺一个就 fail-closed，不降级）
REQUIRED_NODES = (
    "MiniMaxH3VoiceProfileT8",
    "MiniMaxH3SpeechPlanT8",
    "MiniMaxH3SpeechStudioT8",
    "SaveAudio",
    "UNETLoader",
    "CLIPLoader",
    "VAELoader",
)

VOICE_DIR = "voice"
VOICE_OVERRIDE_FILE = "voice.json"

# SpeechStudio 的 render_seconds 下限（H3 帧格点），实测 5.17
MIN_RENDER_SECONDS = 5.17

_SAFE_ID = re.compile(r"[^A-Za-z0-9_\u4e00-\u9fff]")


@dataclass
class VoiceConfig:
    """一次配音所需参数。指纹由它派生，字段要克制。"""

    language: str = "Chinese"
    steps: int = 20
    sampler_name: str = "res_multistep"
    scheduler: str = "simple"
    resolution: int = 32
    shift_video: float = 12.0
    shift_audio: float = 3.0
    acting_direction: str = "natural dramatic delivery, clear diction"
    emotion: str = "neutral"
    emotion_intensity: float = 0.4
    space: str = "close"
    target_units: int = 18
    max_units: int = 24
    seed: int = 7
    render_pad: float = 0.4          # 给 render_seconds 留的余量
    voice_overrides: dict[str, str] = field(default_factory=dict)

    def render_dict(self) -> dict:
        return {
            "language": self.language,
            "steps": self.steps,
            "sampler": self.sampler_name,
            "scheduler": self.scheduler,
            "resolution": self.resolution,
            "shift_video": self.shift_video,
            "shift_audio": self.shift_audio,
            "acting": self.acting_direction,
            "emotion": self.emotion,
            "space": self.space,
            "seed": self.seed,
        }


DEFAULT_VOICE_FALLBACK = (
    "A Chinese adult speaker with a natural warm voice, clear diction, "
    "human micro-pauses, close conversational delivery"
)


def load_voice_overrides(proj: Any) -> dict[str, str]:
    """项目级音色覆盖（可选文件 `<项目>/voice.json`：{"角色名": "英文音色描述"}）。"""
    p = Path(proj.root) / VOICE_OVERRIDE_FILE
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {str(k): str(v) for k, v in data.items() if str(v).strip()} if isinstance(data, dict) else {}


def voice_description(speaker: str, appearance: str, cfg: VoiceConfig) -> str:
    """
    这位角色用什么音色。

    优先级：项目 voice.json 覆盖 → 角色卡外貌派生（至少让不同角色音色有区别）→ 通用兜底。
    """
    if speaker in cfg.voice_overrides:
        return cfg.voice_overrides[speaker]
    look = (appearance or "").strip().rstrip(".")
    if look:
        return (f"Chinese speaker matching this character: {look[:140]}. "
                "Natural warm voice, clear diction, close conversational delivery.")
    return DEFAULT_VOICE_FALLBACK


def _safe_id(name: str) -> str:
    return _SAFE_ID.sub("_", name or "") or "S1"


def speaker_of(shot: Any) -> str:
    """这一镜谁在说 —— 有角色就取第一个，没有就画外音。"""
    chars = list(getattr(shot, "chars", None) or [])
    return chars[0] if chars else "旁白"


def voice_fingerprint(text: str, speaker: str, desc: str, cfg: VoiceConfig) -> str:
    parts = [
        "voice-v1",
        "text=" + (text or ""),
        "speaker=" + (speaker or ""),
        "desc=" + (desc or ""),
        "cfg=" + json.dumps(cfg.render_dict(), sort_keys=True, ensure_ascii=False),
    ]
    return hashlib.md5("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def voice_path(proj: Any, shot_id: str) -> Path:
    return Path(proj.root) / VOICE_DIR / f"{shot_id}.wav"


def _meta_path(proj: Any, shot_id: str) -> Path:
    return Path(proj.root) / VOICE_DIR / f"{shot_id}.json"


def cached_voice(proj: Any, shot_id: str, fp: str) -> Path | None:
    """指纹一致且文件在 → 直接复用（配音很贵，能省就省）。"""
    wav = voice_path(proj, shot_id)
    if not wav.is_file() or wav.stat().st_size == 0:
        return None
    try:
        meta = json.loads(_meta_path(proj, shot_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return wav if meta.get("fp") == fp else None


def build_voice_workflow(
    text: str, speaker: str, desc: str, cfg: VoiceConfig, params: dict, seconds: float,
) -> dict:
    """文本 → 音频的工作流（只提交，不落盘）。"""
    sid = _safe_id(speaker)
    render_seconds = max(MIN_RENDER_SECONDS, float(seconds) + float(cfg.render_pad))
    return {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": _p(params, "unet"), "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": _p(params, "clip"), "type": "minimax", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": _p(params, "vae_video")}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": _p(params, "vae_audio")}},
        "5": {"class_type": "MiniMaxH3VoiceProfileT8", "inputs": {
            "voice_mode": "described_voice", "speaker_id": sid, "language": cfg.language,
            "voice_description": desc, "rights_confirmed": True,
            "reference_start_seconds": 0.0, "reference_duration_seconds": 0.0,
            "highpass_60hz": True, "peak_limit_minus_3_dbfs": True}},
        "6": {"class_type": "MiniMaxH3SpeechPlanT8", "inputs": {
            "voice_profile": ["5", 0], "text": text, "language": cfg.language,
            "acting_direction": cfg.acting_direction, "emotion": cfg.emotion,
            "emotion_intensity": float(cfg.emotion_intensity), "space": cfg.space,
            "chunking": "single_segment", "target_units": int(cfg.target_units),
            "max_units": int(cfg.max_units)}},
        "7": {"class_type": "MiniMaxH3SpeechStudioT8", "inputs": {
            "model": ["1", 0], "clip": ["2", 0], "video_vae": ["3", 0], "audio_vae": ["4", 0],
            "voice_profile": ["5", 0], "speech_plan": ["6", 0],
            "segment_index": 0, "seed": int(cfg.seed), "render_seconds": render_seconds,
            "resolution": int(cfg.resolution), "steps": int(cfg.steps),
            "sampler_name": cfg.sampler_name, "scheduler": cfg.scheduler,
            "shift_video": float(cfg.shift_video), "shift_audio": float(cfg.shift_audio),
            "trim_mode": "auto_reference_voice", "verify_mode": "off",
            "asr_model_directory": "", "asr_language": "auto", "min_similarity": 0.85,
            "unload_asr_after_verify": True, "speaker_check_mode": "off",
            "speaker_model_directory": "", "min_speaker_similarity": 0.86,
            "unload_speaker_after_verify": True, "peak_limit_dbfs": -1.0,
            "release_policy": "clear_execution_cache"}},
        "8": {"class_type": "SaveAudio",
              "inputs": {"audio": ["7", 0], "filename_prefix": "VM_VOICE"}},
    }


def _p(params: dict, key: str) -> Any:
    from . import taskctl
    if key in params:
        return params[key]
    return taskctl.DEFAULT_PARAMS.get(key)


def synthesize(
    comfy: Comfy, params: dict, proj: Any, shot: Any, *,
    cfg: VoiceConfig | None = None, log: Callable[[str], None] = print,
) -> Path | None:
    """
    给这一镜的台词配音，落盘到 `<项目>/voice/<镜头号>.wav`。

    没有台词（dialogue 与 narration 都空）→ 返回 None（不需要配音）。
    指纹一致且文件在 → 直接复用，不重复烧 GPU。
    """
    text = ((getattr(shot, "dialogue", "") or "").strip()
            or (getattr(shot, "narration", "") or "").strip())
    if not text:
        return None
    cfg = cfg or VoiceConfig(voice_overrides=load_voice_overrides(proj))
    speaker = speaker_of(shot)
    appearance = getattr(shot, "char_appearance", "") or ""
    desc = voice_description(speaker, appearance, cfg)
    fp = voice_fingerprint(text, speaker, desc, cfg)

    hit = cached_voice(proj, shot.id, fp)
    if hit is not None:
        log(f"    🗣 复用配音 {hit.name}")
        return hit

    from .shots import effective_sec
    seconds = float(effective_sec(shot))
    wf = build_voice_workflow(text, speaker, desc, cfg, params, seconds)
    pid = comfy.submit(wf)
    entry = comfy.wait(pid, timeout=1800)
    items = comfy.outputs_of(entry)
    audio = next((it for it in items
                  if str(it.get("filename", "")).lower().endswith((".flac", ".wav", ".mp3"))), None)
    if audio is None:
        raise ComfyError(f"配音失败：prompt_id={pid} 没有音频产物 outputs={entry.get('outputs')}")

    out = voice_path(proj, shot.id)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".raw" + Path(str(audio["filename"])).suffix)
    comfy.download(audio, tmp)
    try:
        _to_wav32k(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)
    _meta_path(proj, shot.id).write_text(
        json.dumps({"fp": fp, "text": text, "speaker": speaker, "desc": desc},
                   ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    log(f"    🗣 配音完成 {out.name}（{speaker}「{text[:16]}」）")
    return out


def _to_wav32k(src: Path, dst: Path) -> None:
    """统一成 32kHz 立体声 s16 —— drive_audio / 混音都按这个口径。"""
    r = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-ar", "32000", "-ac", "2",
         "-c:a", "pcm_s16le", str(dst)],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not dst.is_file():
        raise ComfyError(f"配音转码失败：{r.stderr.strip()[:200]}")


def probe(comfy: Comfy) -> list[str]:
    """返回缺失的必需节点（空 = 可用）。给前置检查用。"""
    return [n for n in REQUIRED_NODES if not comfy.has_node(n)]
