"""
chain.py —— 连贯链式渲染（社区成熟工作流 MiniMaxH3-TimelineDirector 的生产封装）。

为什么要有它（2026-09-28 用户反馈"分镜衔接对不上"）：
    原来每镜独立生成：参考图锁住了"还是同一个人"，但**镜头之间是硬切** ——
    景别/机位/光位/姿态各写各的，观众看得出"不是一场戏"；末段还常伴随冻结帧。

社区成熟做法（本项目实测 2026-09-28）：
    把同场景的连续镜头编成一条**链**，一次 ComfyUI 执行生成：
      · 相邻段之间固定重叠 `overlap` 帧（默认 22 ≈ 0.92s）
      · **原生 AV latent 续接**（不经过 RGB 解码→重编码，所以接缝无跳变）
      · 出片后按去重边界**切回逐镜 clips** —— 指纹/manifest/质检/胶片条/合成都照旧
    实测：3 镜一条链 110 秒出 13.67s 连贯成片，接缝帧差 1.3–2.3（全片中位 1.01）。

音频（**重要事实，别被社区文档误导**）：
    TimelineDirector 的 `audios[].audioMode="locked"` 在本机实测**并不保留原音**
    （成片音轨与输入配音的包络相关只有 0.378 —— 是模型重新生成的、时间大致对齐）。
    所以这里走**确定性混音**：逐镜 TTS 摆在"本段新内容起点"上合成一条轨，
    渲完用 ffmpeg 直接混进成片 → 实测包络相关 **1.0000**（听到的就是写的台词）。

帧格点（踩过的坑，写死在这里）：
    H3 要求每段帧数 = `5 + 17n`，重叠 = `5 + 17k`。所以
        段长 = 计划帧 + 重叠 − 5
    这样去重后每镜只损失 **5 帧（0.21s）**；若照直觉写 `计划帧 + 重叠`，段长不合规直接报错。

能力边界：H3 不是确定性口型求解器 —— 本模块保证"说什么/字幕/时序"一致，
    嘴型只是逼近；要广播级口型仍需出片后加专门唇形后处理。
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from .comfy import Comfy, ComfyError

# 必需节点（缺任一 → 链渲染不可用，调用方回退单镜路径；不静默降级）
REQUIRED_NODES = (
    "MiniMaxH3TimelinePlanner",
    "MiniMaxH3FiniteSegmentSampler",
    "BasicScheduler",
    "KSamplerSelect",
    "CreateVideo",
    "SaveVideo",
)

DEFAULT_OVERLAP_FRAMES = 22     # 社区默认；= 5 + 17*1
DEFAULT_MAX_SHOTS = 3
CHAIN_DIR = "chain_test"        # 链渲染的原始产物（调试/复看用，不参与成片）
FPS = 24


# ---------------------------------------------------------------- 可用性 / 分组


def missing_nodes(comfy: Comfy) -> list[str]:
    """返回缺失的必需节点（空 = 可用）。给前置检查与回退判断用。"""
    return [n for n in REQUIRED_NODES if not comfy.has_node(n)]


def _refs(proj: Any, shot: Any) -> list[tuple[Path, str]]:
    """
    这一镜的参考图（(路径, 已同步到 ComfyUI input 的名字)）。

    与 `vm/refs.py` 同源 —— **提交什么就算什么**，指纹与画面用的是同一份清单。
    缺图会抛 ComfyError（fail-closed），不会静默跳过导致角色错位。
    """
    from . import refs as R
    return list(R.require_refs(proj, list(getattr(shot, "chars", []) or []), shot.id))


def _ref_names(proj: Any, shot: Any) -> list[str]:
    return [p.name for p, _src in _refs(proj, shot)]


def _ref_paths(proj: Any, shot: Any) -> list[Path]:
    return [p for p, _src in _refs(proj, shot)]


def plan_chains(
    shots: list[Any],
    *,
    is_locked: Callable[[str], bool] | None = None,
    max_shots: int = DEFAULT_MAX_SHOTS,
    log: Callable[[str], None] = print,
) -> list[list[Any]]:
    """
    把镜头编成链。成链条件（全部满足才连）：
      · 相邻（表中相邻）
      · 同一个 `scene_id`（同一个地点）
      · **chars 完全相同且顺序相同** —— 链只有一份参考图清单，而 `<Picture N>` 的编号
        是全局的；不同角色集合会让编号错位（画面正常但人不对，最难查）。所以宁可断开。
      · 都没有被锁定（锁定 = 用户已认可的那一版，不能被链重渲覆盖）
      · 每链 ≤ max_shots 镜
    返回只含**长度 ≥ 2** 的链；长度 1 的交回单镜路径。
    """
    is_locked = is_locked or (lambda _sid: False)
    chains: list[list[Any]] = []
    cur: list[Any] = []
    for s in shots:
        joinable = (
            cur
            and len(cur) < max_shots
            and not is_locked(getattr(s, "id", ""))
            and not is_locked(getattr(cur[-1], "id", ""))
            and getattr(s, "scene_id", "") == getattr(cur[-1], "scene_id", "")
            and list(getattr(s, "chars", []) or []) == list(getattr(cur[-1], "chars", []) or [])
            and bool(getattr(s, "scene_id", ""))
        )
        if joinable:
            cur.append(s)
            continue
        if len(cur) >= 2:
            chains.append(cur)
        cur = [s]
    if len(cur) >= 2:
        chains.append(cur)
    for c in chains:
        log(f"  🔗 成链 {len(c)} 镜（{c[0].scene_id} / {'、'.join(c[0].chars)}）: "
            + " → ".join(s.id for s in c))
    return chains


def geometry(chain: list[Any], overlap: int, *, fps: int = FPS) -> list[dict]:
    """
    链的帧几何。返回每段 {start, end, gen_frames, new_frames}（全局帧坐标）。

    校验：段长与重叠都必须落在 H3 格点上（5+17n / 5+17k），否则就地报错
    —— 让节点包报"Segment 2 length must be 5+17*n"更难懂。
    """
    if (overlap - 5) % 17 != 0:
        raise ValueError(f"重叠帧数必须是 5+17k（5/22/39…），收到 {overlap}")
    from .shots import effective_sec, seconds_to_frames

    segs: list[dict] = []
    prev_end = 0
    for i, s in enumerate(chain):
        plan_frames = seconds_to_frames(effective_sec(s), fps)
        gen = plan_frames if i == 0 else plan_frames + overlap - 5
        if (gen - 5) % 17 != 0:
            raise ValueError(f"段长不合规（需 5+17n）：{s.id} → {gen} 帧")
        start = 0 if i == 0 else prev_end - overlap
        end = start + gen
        segs.append({
            "start": start, "end": end, "gen_frames": gen,
            "new_frames": gen if i == 0 else gen - overlap,
            "plan_frames": plan_frames,
        })
        prev_end = end
    return segs


def total_frames(segs: Iterable[dict]) -> int:
    return max((s["end"] for s in segs), default=0)


def new_content_starts(segs: list[dict]) -> list[int]:
    """各段"新内容"在全局帧坐标上的起点 —— 配音就摆在这些位置上。"""
    return [0 if i == 0 else segs[i - 1]["end"] for i in range(len(segs))]


# ---------------------------------------------------------------- 图与时间线


def build_timeline(
    images: list[dict], segs: list[dict], prompts: list[str], *, fps: int = FPS,
) -> dict:
    """构造 TimelineDirector 的 timeline_data（version 4 结构，实测可跑）。"""
    total = total_frames(segs)
    return {
        "version": 4,
        "fps": fps,
        "globalPrompt": "",
        "selection": {"start": 0, "duration": total / fps},
        "videoAudioEnabled": False,      # 音轨我们自己混（见模块头"音频"一节）
        "videoClips": [],
        "images": images,
        "audios": [],
        "secondPass": False,
        "segmentConfig": {
            "count": len(segs),
            "mode": "timeline",
            "segments": [
                {"startFrame": s["start"], "endFrame": s["end"], "prompt": p}
                for s, p in zip(segs, prompts)
            ],
        },
    }


def build_chain_workflow(
    params: dict, timeline: dict, segs: list[dict], tick_seed: int, *, fps: int = FPS,
) -> dict:
    """社区工作流的图：Planner → FiniteSegmentSampler → CreateVideo → SaveVideo。"""
    def _p(key: str) -> Any:
        from . import taskctl
        return params.get(key, taskctl.DEFAULT_PARAMS.get(key))

    first_len = segs[0]["gen_frames"]
    g: dict = {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": _p("unet"), "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": _p("clip"), "type": "minimax", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": _p("vae_video")}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": _p("vae_audio")}},
        "6": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "8": {"class_type": "MiniMaxH3TimelinePlanner", "inputs": {
            "width": int(_p("width")), "height": int(_p("height")),
            "generation_seconds": first_len / fps,
            "timeline_data": json.dumps(timeline, ensure_ascii=False)}},
        "10": {"class_type": "CreateVideo",
               "inputs": {"images": ["9", 1], "audio": ["9", 2], "fps": float(fps),
                          "bit_depth": 8, "color_space": "sRGB"}},
        "11": {"class_type": "SaveVideo",
               "inputs": {"video": ["10", 0], "filename_prefix": "video/VM_CHAIN",
                          "format": "mp4", "codec": "h264"}},
    }
    model_ref: list = ["1", 0]
    if _p("lora"):
        g["5"] = {"class_type": "LoraLoaderModelOnly",
                  "inputs": {"model": ["1", 0], "lora_name": _p("lora"), "strength_model": 1.0}}
        model_ref = ["5", 0]
    g["7"] = {"class_type": "BasicScheduler",
              "inputs": {"model": model_ref, "scheduler": "simple",
                         "steps": int(_p("steps")), "denoise": 1.0}}
    g["9"] = {"class_type": "MiniMaxH3FiniteSegmentSampler", "inputs": {
        "model": model_ref, "clip": ["2", 0], "vae": ["3", 0], "audio_vae": ["4", 0],
        "finite_plan": ["8", 2], "sampler": ["6", 0], "sigmas": ["7", 0],
        "seed": int(tick_seed), "continue_audio_latent": False,
        "ref_image_size": _p("ref_image_size") or "match"}}
    return g


# ---------------------------------------------------------------- 音频轨


def build_audio_track(
    wavs: list[Path | None], starts: list[int], total: int, out: Path,
    *, fps: int = FPS, log: Callable[[str], None] = print,
) -> Path | None:
    """
    把逐镜配音摆到各自"新内容起点"，合成一条与全局时间轴对齐的音轨。

    没有台词时返回 None → 调用方用静音轨（**绝不让模型自己编台词**：
    那正是"嘴说话对不上"的根源之一）。
    """
    have = [(w, s) for w, s in zip(wavs, starts) if w is not None and Path(w).is_file()]
    dur = total / fps
    out.parent.mkdir(parents=True, exist_ok=True)
    if not have:
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                        "-i", f"anullsrc=r=32000:cl=stereo", "-t", f"{dur:.3f}",
                        "-c:a", "pcm_s16le", str(out)], check=True)
        log("    🎧 无台词 → 静音轨（避免模型自编台词）")
        return out
    args: list[str] = ["ffmpeg", "-y", "-v", "error"]
    for w, _s in have:
        args += ["-i", str(w)]
    filters, mix = [], []
    for idx, (_w, start) in enumerate(have):
        ms = int(round(start / fps * 1000))
        filters.append(f"[{idx}:a]adelay={ms}|{ms}[a{idx}]")
        mix.append(f"[a{idx}]")
    fc = ";".join(filters + [f"{''.join(mix)}amix=inputs={len(mix)}:normalize=0[mixed];"
                             f"[mixed]apad[out]"])
    args += ["-filter_complex", fc, "-map", "[out]", "-t", f"{dur:.3f}",
             "-ar", "32000", "-ac", "2", "-c:a", "pcm_s16le", str(out)]
    subprocess.run(args, check=True)
    log(f"    🎧 配音轨 {out.name}（{len(have)} 句，{dur:.2f}s）")
    return out


def mux_audio(video: Path, track: Path | None, out: Path) -> Path:
    """把我们的音轨混进链渲染的视频（视频流直接 copy，不重编）。"""
    if track is None:
        return video
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-i", str(track),
                    "-map", "0:v", "-map", "1:a", "-c:v", "copy",
                    "-c:a", "aac", "-b:a", "192k", "-shortest", str(out)], check=True)
    return out


def slice_shot(video: Path, start: int, frames: int, out: Path, *, fps: int = FPS) -> Path:
    """
    按帧边界切出一镜（-ss 用时间、-frames:v 精确帧数）。

    ★ 音频必须**同时**裁到同一时长（`atrim`）：只限 `-frames:v` 时视频会在 n 帧处停，
    但 AAC 音频会继续写到下一个音频帧边界 —— 实测每镜音频比视频长 0.57~0.59s，
    `format=duration` 于是读成偏大的值，拼片/字幕会逐镜漂移（最难查的那类错）。
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    dur = frames / fps
    subprocess.run(["ffmpeg", "-y", "-v", "error",
                    "-ss", f"{start / fps:.4f}", "-i", str(video),
                    "-frames:v", str(frames),
                    "-af", f"atrim=0:{dur:.6f},asetpts=N/SR/TB",
                    "-c:v", "libx264", "-crf", "19", "-preset", "veryfast",
                    "-c:a", "aac", "-b:a", "192k", str(out)],
                   check=True)
    return out


def probe_duration(path: Path) -> float:
    """容器时长（= 各流最大值）。记账**不要**用它，见 `video_duration`。"""
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(path)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def video_duration(path: Path) -> float:
    """
    **视频流**时长 —— `sec_actual` 用它。

    容器时长会被音频流拉长（AAC 帧粒度），拿它当"这镜放映多久"会偏大；
    拼片与字幕都以画面为准，所以这里明确只看 v:0，读不到再退回容器时长。
    """
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=duration", "-of", "default=nw=1:nk=1", str(path)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return probe_duration(path)


# ---------------------------------------------------------------- 指纹


def chain_extra_fp(chain: list[Any], index: int, overlap: int) -> str:
    """
    这一镜的链上下文摘要（供 `shot_fingerprint(extra=...)`）。

    第 0 镜不依赖前序 → 返回 ""（**保持老指纹不变**，不会让已有产物无辜变 stale）。
    第 i 镜依赖前 i 镜的内容与重叠帧数 → 只要前序任何一镜的提示词/时长/种子变了，
    本镜必须变 stale，否则会"上一镜重渲了、这一镜还用旧的接"。
    """
    if index <= 0:
        return ""
    import hashlib
    from .shots import effective_sec, seconds_to_frames
    prev = chain[index - 1]
    raw = "|".join([
        f"overlap={overlap}",
        f"prev_id={getattr(prev, 'id', '')}",
        f"prev_prompt={getattr(prev, 'prompt', '')}",
        f"prev_frames={seconds_to_frames(effective_sec(prev), FPS)}",
        f"prev_seed={getattr(prev, 'seed', 0)}",
    ])
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


def continuity_note(overlap: int, *, fps: int = FPS) -> str:
    """官方 `inject_continuity_instruction` 的原话 —— 让模型把开头当作上一段的延续。"""
    d = overlap / fps
    return (f" The opening 00:00.000-00:{d:06.3f} is a carried latent continuation "
            "from the preceding segment. Describe this opening as the preceding segment's "
            "final shot, preserving character positions, environment, motion, camera path, "
            "lighting, color, and sound before introducing new action.")


def render_chain(
    proj: Any,
    chain: list[Any],
    params: dict,
    comfy: Comfy,
    manifest: Any,
    ck: Any,
    log: Callable[[str], None] = print,
    *,
    force: bool = False,
    overlap: int = DEFAULT_OVERLAP_FRAMES,
    on_tick: Callable[..., None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict:
    """
    渲染一条链，并把结果**切回逐镜** clips（每镜仍是一个文件，下游全部照旧）。

    返回 {"rendered": [...], "skipped": [...], "failed": [...]}。
    失败时抛 ComfyError（调用方可回退单镜路径）。
    """
    from . import voice as V
    from .state import shot_fingerprint

    if len(chain) < 2:
        raise ValueError("链至少要有 2 镜")
    fps = int(params.get("fps") or FPS)
    segs = geometry(chain, overlap, fps=fps)
    total = total_frames(segs)
    starts = new_content_starts(segs)
    first, last = chain[0].id, chain[-1].id
    work = Path(proj.root) / CHAIN_DIR
    work.mkdir(parents=True, exist_ok=True)

    # ---- 0) 需不需要渲？（三态判定，按链里任一镜为准）----
    ref_paths = _ref_paths(proj, chain[0])
    render = params
    pending: list[int] = []
    fps_map: dict[str, int] = {}
    for i, s in enumerate(chain):
        fr = segs[i]["plan_frames"]
        fps_map[s.id] = fr
        fp = shot_fingerprint(s.prompt, s.chars, ref_paths, render, fr, s.seed,
                              extra=chain_extra_fp(chain, i, overlap))
        st = manifest.status(s.id, fp, proj.clip(s.id))
        if force or st != "current":
            pending.append(i)
    if not pending:
        log(f"  ⏭ 整链跳过（current）：{first} → {last}")
        return {"rendered": [], "skipped": [s.id for s in chain], "failed": []}

    # ---- 1) 逐镜配音（台词 → 32k wav，带指纹缓存）----
    log(f"  🔗 链渲染 {len(chain)} 镜（重叠 {overlap} 帧 ≈ {overlap/fps:.2f}s）："
        f"{' → '.join(s.id for s in chain)}")
    wavs: list[Path | None] = []
    for s in chain:
        wavs.append(V.synthesize(comfy, params, proj, s, log=log))
    track = build_audio_track(wavs, starts, total, work / f"audio_{first}_{last}.wav", fps=fps, log=log)

    # ---- 2) 图 ----
    ref_names = _ref_names(proj, chain[0])
    images = [{"id": f"p{i}", "file": n, "name": n} for i, n in enumerate(ref_names)]
    prompts = [s.prompt + (continuity_note(overlap, fps=fps) if i else "")
               for i, s in enumerate(chain)]
    timeline = build_timeline(images, segs, prompts, fps=fps)
    wf = build_chain_workflow(params, timeline, segs, chain[0].seed, fps=fps)

    # ---- 3) 提交并等（提交即落盘：崩了至少能看到"链在跑"）----
    pid = comfy.submit(wf)
    for s in chain:
        ck.set(s.id, pid)
    log(f"  ▶ 提交链 prompt_id={pid}（{total} 帧 = {total/fps:.2f}s）")
    t0 = time.time()
    entry = comfy.wait(pid, timeout=3600, on_tick=on_tick, should_stop=should_stop)
    raw_items = comfy.outputs_of(entry)
    vid = next((it for it in raw_items
                if str(it.get("filename", "")).lower().endswith((".mp4", ".webm", ".mkv"))), None)
    if vid is None:
        raise ComfyError(f"链渲染完成但没找到视频产物：{raw_items}")
    raw = work / f"raw_{first}_{last}.mp4"
    comfy.download(vid, raw)
    log(f"  ✅ 链渲染完成（{time.time()-t0:.0f}s）→ {raw.name}")

    # ---- 4) 混我们的配音轨（社区路径不保留原音，见模块头）----
    final = work / f"final_{first}_{last}.mp4"
    mux_audio(raw, track, final)

    # ---- 5) 切回逐镜 + 记账 ----
    out = {"rendered": [], "skipped": [], "failed": []}
    for i, s in enumerate(chain):
        clip = proj.clip(s.id)
        slice_shot(final, starts[i], segs[i]["new_frames"], clip, fps=fps)
        fp = shot_fingerprint(s.prompt, s.chars, ref_paths, render, fps_map[s.id], s.seed,
                              extra=chain_extra_fp(chain, i, overlap))
        note = (f"链渲染（{first}→{last}，重叠 {overlap} 帧）；"
                f"本镜损失 {segs[i]['plan_frames'] - segs[i]['new_frames']} 帧" if i else
                f"链渲染首镜（{first}→{last}）")
        manifest.mark(s.id, fp, clip, note=note, sec_actual=video_duration(clip))
        ck.clear(s.id)
        out["rendered"].append(s.id)
        log(f"    ✂️ {s.id} → {clip.name}（{video_duration(clip):.2f}s）")
    manifest.save()
    log(f"  🔗 链完成：{' / '.join(out['rendered'])}（整链 {total/fps:.2f}s）")
    return out

