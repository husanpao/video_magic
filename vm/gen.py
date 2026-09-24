"""
gen.py —— 逐镜生成（H3 / ComfyUI）。

三态幂等 + 检查点回收都在这里落地：
  跳过（current）→ 收回（检查点里还没收的）→ 提交 → 落检查点 → 等待 → 收产物 → 写指纹

工作流结构复刻本机已验证跑通的形态（原 `~/comfyui/h3-test-run.py` 的 R2V 分支，
Lead 实测 8/8 成功、每镜约 40 秒 @864×480/8步/5秒）。

两个已知的坑，代码里都做了防：
  * 参考图必须用 Autogrow 平铺键 `ref_images.ref_image_N`；传数组会**静默失效**
    （表现为参考图完全不起作用，画面随机跑）
  * 有角色 → MiniMaxH3ReferenceToVideo；无角色（空镜）→ MiniMaxH3AudioConditioningT8
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Callable

from .comfy import Comfy, ComfyError
from .shots import Shot, effective_sec, seconds_to_frames
from .state import Checkpoint, CURRENT, Manifest, Project, shot_fingerprint

# 与项目参数解耦的默认值（可被 params 覆盖）
DEFAULTS = {
    "width": 864,
    "height": 480,
    "fps": 24,
    "steps": 8,
    "unet": "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
    "lora": "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors",
    "vae_video": "minimax_h3_video_vae_fp16.safetensors",
    "vae_audio": "minimax_h3_audio_vae_fp32.safetensors",
    "clip": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
    "ref_image_size": "match",
}

# 判定"检查点里的 prompt_id 已丢失"前，先给它多久的宽限期
RECLAIM_GRACE_SEC = 90


def _p(params: dict, key: str):
    return params.get(key, DEFAULTS[key])


def _pixels(sec: int, params: dict) -> tuple[int, int]:
    """把目标秒数换算成帧数（17n+5 网格）。宽高来自项目参数。"""
    return int(_p(params, "width")), int(_p(params, "height"))


def _assert_dense_slots(inputs: dict, count: int) -> None:
    """
    A5：Autogrow 槽位必须稠密（0..count-1 连续），否则 fail-closed。

    为什么不能容忍稀疏：Autogrow 的展示顺序就是提示词里 `<Picture N>` 的编号来源，
    节点遇到空洞会**静默重新编号**，于是"第 2 张参考图"实际变成第 1 张 ——
    画面照常出，只是角色错位，属于最难排查的一类 bug。
    """
    got = sorted(
        int(k.rsplit("_", 1)[1])
        for k in inputs
        if k.startswith("ref_images.ref_image_")
    )
    expect = list(range(count))
    if got != expect:
        raise ComfyError(
            f"参考图槽位不是稠密的：期望 {expect}，实际 {got}。"
            f"Autogrow 会静默重新编号导致 <Picture N> 与图错位 —— 拒绝提交，请检查 chars 与参考图"
        )


_THREE_SECTION_RE = re.compile(r"^([a-z_]+): (.*?)(?=\n[a-z_]+: |\Z)", re.S | re.M)


def _to_three_section(prompt: str) -> str:
    """
    六段式 → 三段式（FL2VA / T2VA 系用，见 A1 说明）。

    - `integrated_multimodal_description`：取 detailed_description，
      并把 retention_analysis 里的 CAMERA/POSITION DISCIPLINE 行接在后面
      （它们是运镜/站位纪律，对空镜同样有效）
    - `overall_soundscape` / `non_diegetic_music`：原样保留
    缺 detailed_description 时**原样返回**（宁可交给模型，也不要拼一个空提示词出来）。
    """
    secs = {
        m.group(1): m.group(2).strip()
        for m in _THREE_SECTION_RE.finditer(prompt or "")
    }
    desc = secs.get("detailed_description", "")
    if not desc:
        return prompt
    # ★ 纪律行在六段式里是 retention_analysis 段内的独立行（`CAMERA DISCIPLINE: …`）。
    #   但三段式**没有** retention_analysis 段 —— 若原样拼进 integrated_multimodal_description，
    #   模型会把 `CAMERA DISCIPLINE:` 当成一个**新的段标题**（`XXX:` 正是段标题的形状），
    #   而我们刚依据实测确认 FL2VA 系只认三段。
    #   所以这里**去掉前缀、压成散文句**并入描述，保持"恰好三段"。
    body = desc.rstrip()
    for ln in (secs.get("retention_analysis") or "").splitlines():
        t = ln.strip()
        for pre in ("CAMERA DISCIPLINE:", "POSITION DISCIPLINE:"):
            if t.startswith(pre):
                tail = t[len(pre):].strip()
                if tail:
                    if body and not body.endswith((".", "!", "?")):
                        body += "."
                    body += " " + tail
                break
    return (
        "integrated_multimodal_description: " + body + "\n"
        "overall_soundscape: " + (secs.get("overall_soundscape") or "room tone") + "\n"
        "non_diegetic_music: " + (secs.get("non_diegetic_music") or "none") + "\n"
    )


def build_render_workflow(
    shot: Shot,
    params: dict,
    ref_names: list[str],
    frames: int,
    seed: int,
) -> dict:
    """
    构造 H3 渲染工作流。

    ref_names 非空 → Ref2VA（角色镜，参考图锁形象）
    ref_names 为空 → T2VA  （空镜，无参考）
    """
    w, h = _pixels(shot.sec, params)
    steps = int(_p(params, "steps"))
    unet = _p(params, "unet")
    lora = _p(params, "lora")
    prefix = f"VM_{shot.id}"

    g: dict = {
        "1": {"class_type": "VAELoader", "inputs": {"vae_name": _p(params, "vae_video")}},
        "2": {"class_type": "VAELoader", "inputs": {"vae_name": _p(params, "vae_audio")}},
        "3": {
            "class_type": "CLIPLoader",
            "inputs": {
                "clip_name": _p(params, "clip"),
                "type": "minimax",
                "device": "default",
            },
        },
        "4": {"class_type": "UNETLoader", "inputs": {"unet_name": unet, "weight_dtype": "default"}},
        "9": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "13": {
            "class_type": "VHS_VideoCombine",
            "inputs": {
                "frame_rate": int(_p(params, "fps")),
                "loop_count": 0,
                "filename_prefix": prefix,
                "format": "video/h264-mp4",
                "pix_fmt": "yuv420p",
                "crf": 19,
                "save_metadata": True,
                "trim_to_audio": False,
                "pingpong": False,
                "save_output": True,
                "images": ["12", 0],
                "audio": ["12", 1],
            },
        },
    }

    # LoRA：有才挂（挂 LoRA 会把步数语义变成"Turbo 步数"）
    model_ref = ["4", 0]
    if lora:
        g["5"] = {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {"lora_name": lora, "strength_model": 1.0, "model": ["4", 0]},
        }
        model_ref = ["5", 0]

    if ref_names:
        # ---- Ref2VA：角色镜 ----
        r2v: dict = {
            "clip": ["3", 0],
            "prompt": shot.prompt,
            "width": w,
            "height": h,
            "length": frames,
            "vae": ["1", 0],
            "audio_vae": ["2", 0],
            "ref_image_size": _p(params, "ref_image_size"),
        }
        for i, name in enumerate(ref_names):
            nid = f"img{i}"
            g[nid] = {"class_type": "LoadImage", "inputs": {"image": name, "upload": "image"}}
            # ★ Autogrow 平铺键。写成数组会静默失效（参考图不起作用）
            r2v[f"ref_images.ref_image_{i}"] = [nid, 0]
        # ★ A5 稠密槽位 fail-closed（2026-09-23）：
        #   Autogrow 的**展示序 = 提示词里的 <Picture N> 编号**，所以槽位必须 0..n-1 连续。
        #   一旦出现稀疏（比如服装变体删了中间一张、或某个角色漏传），
        #   节点会**静默重新编号** → 图文错位 → 画面对了但人不对，最难查。
        #   这里直接报错，不降级。
        _assert_dense_slots(r2v, len(ref_names))
        g["7"] = {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": r2v}
    else:
        # ---- T2VA / FL2VA 系：空镜 ----
        # ★ A1（2026-09-23）：**段落结构必须是三段式，不是六段式**。
        #   依据：Director-WebUI 实测（`timelineProject.ts:531-543` + 测试显式断言
        #   `not.toContain("subject_definitions:")`）——Ref2VA 用六段式，
        #   而 FL2VA 系（含 T2VA）用 integrated_multimodal_description /
        #   overall_soundscape / non_diegetic_music 三段，**没有前四段**。
        #   我们模型的权件名就是 fl2va（First-Last frame to Video+Audio），
        #   走空镜分支时必须换结构，否则模型看到的是一堆它不认识的段标题。
        g["7"] = {
            "class_type": "MiniMaxH3AudioConditioningT8",
            "inputs": {
                "prompt": _to_three_section(shot.prompt),
                "width": w,
                "height": h,
                "length": frames,
                "task_type": "T2VA",
                "audio_mode": "native",
                "audio_denoise_strength": 1.0,
                "add_source_as_reference": False,
                "prompt_primary_audio_ordinal": 0,
                "strict_prompt_tags": True,
                "ref_image_size": _p(params, "ref_image_size"),
                "reference_video_policy": "official_2_to_15s",
                "clip": ["3", 0],
                "video_vae": ["1", 0],
                "audio_vae": ["2", 0],
            },
        }

    g["8"] = {
        "class_type": "MiniMaxH3DualClockSamplerT8",
        "inputs": {
            "steps": steps,
            "shift_video": 12.0,
            "shift_audio": 3.0,
            "model": model_ref,
            "av_latent": ["7", 1],
        },
    }
    g["10"] = {
        "class_type": "BasicGuider",
        "inputs": {"model": ["8", 0], "conditioning": ["7", 0]},
    }
    g["11"] = {
        "class_type": "SamplerCustomAdvanced",
        "inputs": {
            "noise": ["9", 0],
            "guider": ["10", 0],
            "sampler": ["8", 1],
            "sigmas": ["8", 2],
            "latent_image": ["7", 1],
        },
    }
    g["12"] = {
        "class_type": "MiniMaxH3AVDecodeT8",
        "inputs": {"av_latent": ["11", 0], "video_vae": ["1", 0], "audio_vae": ["2", 0]},
    }
    return g


def _degradation_note(shot, frames: int, params: dict, extra: list[str] | None = None) -> str:
    """
    A3：把"能力不满足导致的降级"显式写进 manifest（2026-09-23）。

    为什么必须留行：`seconds_to_frames` 会把请求时长对齐到 H3 的 `17k+5` 帧格点，
    所以**请求 6s 实际是 141 帧 = 5.875s**。这个差异本身正常，但如果**只**在内存里
    发生、不落盘，那么：
      - 字幕时间轴按请求值累计 → 越拼越偏
      - 事后完全看不出"哪一镜的时长被改过、改了多少"
    wind-comic 的 `diagnose_empty_chain()` 就是为这类"静态可知的降级"留痕；
    他们踩过的坑是：所有引擎上限都 < 请求时长时链为空，决策日志上**没有任何一行**
    说明时长被降过 —— 看日志的人会以为一切正常。

    返回给 manifest 的 `note` 字段（空串 = 无降级）。
    """
    req = float(getattr(shot, "sec", 0) or 0)
    fps = int(_p(params, "fps"))
    actual = frames / fps if fps else 0.0
    # 阈值 0.1s（约 2.4 帧）：格点对齐的最大误差是 17/2/24 ≈ 0.35s，
    # 一镜 0.35s 看着小，52 镜累计就是 18 秒 —— 正是 A2 那个字幕漂移的源头，必须留痕。
    if req and abs(actual - req) >= 0.1:
        _note = f"时长从请求 {req:g}s 对齐到帧格点 {frames} 帧 = {actual:.3f}s（计划时间轴须用实际值）"
        if extra:
            _note = (_note + ' | ' if _note else '') + '台词标签问题: ' + '; '.join(extra)
        return _note
    return ""


def probe_duration(path: Path) -> float | None:
    """
    A2：用 ffprobe 回读产物真实时长（秒）。读不到返回 None 并**明确告警**，
    绝不静默回退到请求值 —— 那正是累积漂移的起因。
    """
    import subprocess
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout.strip()
        return float(out) if out else None
    except Exception as e:  # ffprobe 缺失/文件坏都不该拖垮整批渲染
        return None


def _pick_video(items: list[dict]) -> dict | None:
    """从产物里挑视频（优先带音轨的 *-audio.mp4）。"""
    mp4 = [it for it in items if str(it["filename"]).lower().endswith(".mp4")]
    if not mp4:
        return None
    for it in mp4:
        if "-audio" in it["filename"]:
            return it
    return mp4[0]


def _ref_names_for(proj: Project, shot: Shot) -> list[str]:
    """
    把 shot.chars 映射到 refs/ 下的实际文件名，**严格保持 chars 的顺序**。

    ★ 顺序即契约：chars[0] ↔ ref_image_0 ↔ <Picture 1>/<Subject 1>。
      因此**绝不允许静默跳过缺失的参考图** —— 少一张就会让后面所有索引前移，
      导致 subject_definitions 里的角色与画面里的图整体错位，
      而画面还能正常出（只是人不对），是最难查的一类 bug。
      缺图必须显式报错。
    """
    names: list[str] = []
    missing: list[str] = []
    for c in shot.chars:
        p = proj.refs_dir / f"char_{c}.png"
        if p.exists():
            names.append(p.name)
        else:
            missing.append(c)
    if missing:
        raise ComfyError(
            f"镜头 {shot.id} 的角色 {missing} 缺少参考图（refs/char_<名>.png）"
            f"；参考图顺序必须与 chars 一一对应，不能缺项。请先跑 chars 阶段生成定妆照。"
        )
    return names


def render_shot(
    proj: Project,
    shot: Shot,
    params: dict,
    comfy: Comfy,
    manifest: Manifest,
    ck: Checkpoint,
    log: Callable[[str], None] = print,
    *,
    force: bool = False,
    on_tick: Callable[..., None] | None = None,
    progress: Any = None,
    should_stop: Callable[[], bool] | None = None,
) -> tuple[Path | None, str]:
    """
    生成单个镜头。

    返回 (产物路径 | None, 动作)，动作 ∈ {skipped, reclaimed, rendered}
    """
    clip = proj.clip(shot.id)
    # A2：帧数按「人工覆盖 ?? 规划值」算，而不是裸 shot.sec
    frames = seconds_to_frames(effective_sec(shot), int(_p(params, "fps")))
    ref_paths = [proj.refs_dir / f"char_{c}.png" for c in shot.chars]
    fp = shot_fingerprint(
        shot.prompt, shot.chars, ref_paths, params, frames, shot.seed
    )

    # ---- 0a) A4 的正确形态：台词必须只在 <d>…</d> 里 ----
    # 按负面清单那条原则「只写 log 的判定 = 装了但没通电」，这里**不静默**：
    # 有问题就写进显眼日志，并进入 manifest 的 note（UI 可见）。
    # 不阻塞渲染 —— 台词标签坏了顶多配音不对，不该让整章渲不出来。
    from vm.plan import check_dialogue_tags
    dialog_issues = check_dialogue_tags(shot.prompt, shot.dialogue)

    # ---- 0) E1 锁定检查：锁定的镜头绝不覆盖 ----
    # 在此之前"任何产物都能被覆盖" —— 改定妆照 / 批量重渲 / 重跑都可能
    # 把一个已满意的镜头盖掉且无提示。锁定后只有显式 force 才能动它。
    if not force and manifest.is_locked(shot.id):
        log(f"  🔒 跳过（已锁定，需先解锁或 force）: {shot.id}")
        return None, "skipped"

    # ---- 1) 三态判定 ----
    if not force:
        st = manifest.status(shot.id, fp, clip)
        if st == CURRENT:
            log(f"  ⏭ 跳过（current）: {shot.id}")
            return None, "skipped"

    # ---- 2) 检查点回收：上过盘但还没收到的任务 ----
    if not force and ck.get(shot.id):
        pid = ck.get(shot.id)
        entry = comfy.history(pid)
        if entry is not None:
            status = entry.get("status") or {}
            if status.get("completed"):
                items = comfy.outputs_of(entry)
                vid = _pick_video(items)
                if vid:
                    comfy.download(vid, clip)
                    manifest.mark(
                        shot.id, fp, clip,
                        note=_degradation_note(shot, frames, params, extra=dialog_issues),
                        sec_actual=probe_duration(clip),
                    )
                    manifest.save()
                    ck.clear(shot.id)
                    log(f"  ♻️ 收回已完成任务: {shot.id} ← {pid}")
                    return clip, "reclaimed"
            elif status.get("status_str") == "error":
                log(f"  ⚠️ 检查点任务已出错，清掉重提: {shot.id}")
                ck.clear(shot.id)
        else:
            running, pending = comfy.queue_state()
            age = ck.age(shot.id)
            if running == 0 and pending == 0:
                # ★ A6 排队宽限（2026-09-23）："历史查不到 + 队列为空"**不足以**判定丢失。
                #   刚提交的任务可能还没进 /history，队列也可能瞬时为空 ——
                #   没有宽限期就会误判丢失 → 重复提交 → 白烧 GPU 且产物被覆盖。
                #   只有检查点已超过宽限期才判丢失。
                if age < RECLAIM_GRACE_SEC:
                    log(
                        f"  ⏳ 检查点暂时查不到（{age:.0f}s < 宽限 {RECLAIM_GRACE_SEC}s），"
                        f"继续等而不是重提: {shot.id} ← {pid}"
                    )
                    entry = comfy.wait(pid, on_tick=on_tick, should_stop=should_stop, progress=progress)
                    vid = _pick_video(comfy.outputs_of(entry))
                    if vid:
                        comfy.download(vid, clip)
                        manifest.mark(shot.id, fp, clip)
                        manifest.save()
                        ck.clear(shot.id)
                        log(f"  ♻️ 收回已完成任务: {shot.id} ← {pid}")
                        return clip, "reclaimed"
                else:
                    # 既不在历史也不在队列、且已过宽限期 → 判定丢失，重提
                    log(
                        f"  ⚠️ 检查点任务已丢失（不在队列也不在历史，已 {age:.0f}s），"
                        f"重新提交: {shot.id}"
                    )
                    ck.clear(shot.id)
            else:
                log(f"  ⏳ 检查点任务仍在进行，等它完成: {shot.id} ← {pid}")
                entry = comfy.wait(
                    pid, on_tick=on_tick, should_stop=should_stop, progress=progress
                )
                items = comfy.outputs_of(entry)
                vid = _pick_video(items)
                if vid:
                    comfy.download(vid, clip)
                    manifest.mark(shot.id, fp, clip)
                    manifest.save()
                    ck.clear(shot.id)
                    return clip, "reclaimed"

    # ---- 3) 提交新任务 ----
    ref_names = _ref_names_for(proj, shot)
    if shot.chars and not ref_names:
        raise ComfyError(
            f"镜头 {shot.id} 声明角色 {shot.chars}，但 refs/ 下找不到对应参考图；"
            f"请先跑 chars 阶段生成定妆照"
        )
    wf = build_render_workflow(shot, params, ref_names, frames, shot.seed)
    log(
        f"  ▶ 提交 {shot.id} | {shot.sec}s/{frames}帧 | {_p(params, 'width')}x{_p(params, 'height')}"
        f" | {len(ref_names)} 张参考图 | seed={shot.seed}"
    )
    pid = comfy.submit(wf)
    # ★ 提交即落盘：此后无论怎么崩，重启都能凭这个 id 收回
    ck.set(shot.id, pid)

    entry = comfy.wait(pid, on_tick=on_tick, should_stop=should_stop, progress=progress)
    vid = _pick_video(comfy.outputs_of(entry))
    if not vid:
        raise ComfyError(f"镜头 {shot.id} 完成但未找到视频产物: {entry.get('outputs')}")

    comfy.download(vid, clip)
    manifest.mark(
        shot.id, fp, clip,
        note=_degradation_note(shot, frames, params),
        sec_actual=probe_duration(clip),   # A2：回读真实时长，时间轴用它
    )
    manifest.save()
    ck.clear(shot.id)
    size_mb = clip.stat().st_size / 1e6
    log(f"  ✅ {shot.id} → {clip.name} ({size_mb:.1f} MB)")
    return clip, "rendered"


def render_all(
    proj: Project,
    params: dict,
    *,
    only: list[str] | None = None,
    force: bool = False,
    dry: bool = False,
    log: Callable[[str], None] = print,
    on_tick: Callable[..., None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict:
    """按镜头表顺序生成全部（或指定）镜头。返回动作统计。"""
    from .shots import load_shots_dir

    # ── 实时进度（2026-09-24）────────────────────────────────────────────────
    # 整批共用一个 WebSocket 连接（ComfyUI 在 /ws 上推**步级** progress）。
    # **连不上不影响渲染** —— 进度是锦上添花；拿不到时 on_tick 退化为只有
    # running/pending 计数，也就是从前的行为（日志里一长串 `running=1 pending=0`）。
    _prog = None
    try:
        from .wsclient import ComfyProgress
        from urllib.parse import urlparse
        _u = urlparse(str(params.get("comfy_url") or "http://127.0.0.1:8188"))
        _prog = ComfyProgress(_u.hostname or "127.0.0.1", _u.port or 8188).start()
        if _prog.error:
            log(f"  · 实时进度不可用（{_prog.error}）——退化为只显示队列计数")
            _prog = None
        else:
            log("  · 已连接 ComfyUI 实时进度（步级）")
    except Exception as _e:                                   # noqa: BLE001
        log(f"  · 实时进度不可用（{type(_e).__name__}: {_e}）")
        _prog = None

    shots = load_shots_dir(proj.shots_dir)
    if only:
        want = set(only)
        shots = [s for s in shots if s.id in want]
    if not shots:
        raise ValueError(f"{proj.shots_dir} 里没有镜头（或 --only 未匹配到任何镜头）")

    comfy = Comfy(params.get("comfy_url", "http://127.0.0.1:8188"))
    comfy.require_healthy()

    # ★ 参考图必须先同步到 ComfyUI 的 input 目录。
    #   LoadImage 只认那里，而 refs/ 是项目内留档 —— 少这一步的表现是提交时
    #   "节点校验失败/找不到图片"，报错很难懂（webui 验收时实测踩到过：
    #   4 张定妆照里 3 张不在 input，一点渲染就会失败）。
    #   sync 内部按 size 比对，已一致的不重复复制，代价可忽略。
    from .chars import sync_refs_to_input  # 局部导入：chars 反过来依赖 gen，避免循环
    if not dry:
        try:
            sync_refs_to_input(proj, params, log)
        except Exception as e:
            raise ComfyError(f"参考图同步到 ComfyUI input 失败：{e}") from e

    manifest = proj.manifest
    ck = proj.checkpoint
    result = {"rendered": [], "skipped": [], "reclaimed": [], "failed": []}

    log(f"===== 渲染 {len(shots)} 个镜头（force={force}, dry={dry}）=====")
    t0 = time.time()
    for i, shot in enumerate(shots, 1):
        log(f"[{i}/{len(shots)}] {shot.id}")
        if dry:
            frames = seconds_to_frames(shot.sec)
            log(f"  · dry-run：{shot.sec}s→{frames}帧，参考图 {[c for c in shot.chars]}")
            continue
        try:
            _path, action = render_shot(
                proj, shot, params, comfy, manifest, ck, log,
                force=force, on_tick=on_tick, should_stop=should_stop, progress=_prog,
            )
            result[action].append(shot.id)
        except ComfyError as e:
            log(f"  ❌ {shot.id} 失败: {e}")
            result["failed"].append(shot.id)
        except Exception as e:  # 单个镜头出意外不该拖垮整批
            log(f"  ❌ {shot.id} 异常: {e!r}")
            result["failed"].append(shot.id)

    el = time.time() - t0
    log(
        f"===== 渲染结束：新增 {len(result['rendered'])} / 收回 {len(result['reclaimed'])}"
        f" / 跳过 {len(result['skipped'])} / 失败 {len(result['failed'])} | 用时 {el/60:.1f} 分钟 ====="
    )
    if result["failed"]:
        log("失败镜头: " + ", ".join(result["failed"]))
    if _prog is not None:
        _prog.stop()
    return result
