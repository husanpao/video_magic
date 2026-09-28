"""
chars.py —— 角色定妆照生成 + 抽卡。

## 逻辑（三步）
    prompts/char_<角色>.txt
      → H3 T2VA 生成 5 秒 640×640 视频
      → 抽第 24 帧（≈1 秒处，姿态稳定且正对镜头）
      → 存 refs/char_<角色>.png，并复制到 ComfyUI input/（LoadImage 只认那里）

## 为什么不用 NiliX 的资产阶段
它只走 Krea-2，本机没装该模型，那条路必然失败。这里用本机已有的 H3 权重，
实测 4/4 成功、每张约 40 秒。

## 抽卡
单张定妆本质是抽奖，一次不满意很正常。所以：
    gen_candidates()  同一角色用不同 seed 出 N 张 → refs/_gacha/<角色>/seed*.png
                      并拼一张对比图 _sheet.jpg，一眼看完
    adopt_candidate() 采纳某张 → 覆盖 refs/char_<角色>.png → 同步到 ComfyUI input

**采纳后不需要手动处理已渲染的镜头**：`shot_fingerprint` 里含参考图的
`名@mtime纳秒:size`，定妆照一换指纹就变 → 相关镜头自动判 stale → 下次渲染自动重做。

## 风格约定（2026-09-23 修正）
提示词**不要加** `semi-realistic stylized / not photorealistic / not a real person /
not Japanese anime style` 这类风格锚 —— 实测那样会得到廉价的 3D 卡通感，
既不像写实也不像动漫。用户原有做法（朴实的写实描述 + `Cinematic medium close-up
portrait of ... facing the camera directly`）效果明显更好。本模块会对此给出告警。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import zlib
from pathlib import Path
from typing import Callable

from .comfy import Comfy, ComfyError
from .gen import _pick_video, build_render_workflow
from .shots import Shot, seconds_to_frames

# 定妆照固定 1:1 / 640×640（与原流程一致；`--size 1x1` 就是这个尺寸）
PORTRAIT_W = 640
PORTRAIT_H = 640
PORTRAIT_SEC = 5
PORTRAIT_FRAME = 24  # 抽第 24 帧（≈1 秒处）

# 已知会拉低观感的风格锚（历史遗留），命中就给告警
_BAD_STYLE_ANCHORS = (
    "not photorealistic",
    "semi-realistic stylized",
    "not a real person",
    "not japanese anime",
)

GACHA_DIRNAME = "_gacha"


# ---------------------------------------------------------------- 基础工具


def _portrait_seed(name: str, index: int = 0, chapter: int | None = None) -> int:
    """
    角色 + 序号（+ 章）→ 稳定 seed。

    用 crc32 而不是内置 hash()：hash 有随机盐，跨进程不稳定，
    会导致"同一个角色重跑拿到完全不同的脸"，抽卡也就无法复现。

    ★ 章节专属造型必须带**不同**的 seed（同一张脸上换衣服会出粘连/穿模），
      但仍然要可复现 —— 所以章号进 crc 的 key，而不是另发明一套随机源。
    """
    key = f"{name}#{index}" if not chapter else f"{name}@ch{int(chapter):02d}#{index}"
    return 2000 + (zlib.crc32(key.encode("utf-8")) % 9000)


def costume_prompt(proj, name: str, chapter: int | None, base: str, log=print) -> str:
    """
    若该角色在这一章登记了专属造型，就把那段服装文字接在基准提示词后面。

    身份（脸/体型/发色）始终由 `prompts/char_<名>.txt` 那句 identity 锁死 ——
    变体只改**穿什么**，不改**是谁**。这与 `costumes.py` 的
    "图锁身份 + 文控服装"是同一条纪律。
    """
    if not chapter:
        return base
    from vm import costumes as _K

    v = _K.variants_of_chapter(_K.load(proj), name, chapter)
    if not v:
        log(f"  · {name} 第 {chapter} 章没有登记专属造型，用基准定妆提示词")
        return base
    extra = str(v.get("prompt") or "").strip()
    if not extra:
        return base
    return f"{base.rstrip('.')}\n\ncostume_override ({v.get('id')}): {extra}."


def check_prompt_style(prompt: str) -> list[str]:
    """检查提示词里是否残留已知会拉低观感的风格锚。返回告警列表。"""
    low = prompt.lower()
    return [a for a in _BAD_STYLE_ANCHORS if a in low]


def _extract_frame(video: Path, dst: Path, frame: int = PORTRAIT_FRAME) -> Path:
    """从定妆视频里抽一帧存为 PNG（原子落盘）。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp.png")
    # 输入已经是 PNG（Qwen-Image 通路，默认）就**直接复制** ——
    # ffmpeg 也能读 PNG，但它只会抽第 0 帧；而 `frame=12` 在单帧图上无输出，
    # 结果是 tmp 不存在 → os.replace 报 FileNotFoundError（我实测踩到）。
    if video.suffix.lower() == ".png":
        shutil.copyfile(video, tmp)
    else:
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(video),
             "-vf", f"select=eq(n\\,{frame})", "-vframes", "1", str(tmp)],
            check=True, capture_output=True,
        )
    os.replace(tmp, dst)
    return dst


def sync_refs_to_input(proj, params: dict, log: Callable[[str], None] = print) -> list[str]:
    """
    把 refs/char_*.png 同步到 ComfyUI 的 input 目录。

    LoadImage 只能读 ComfyUI 自己的 input 目录，这一步不可省。
    是**复制**不是移动 —— 项目内的 refs/ 要留档。
    """
    comfy_input = Path(params.get("comfy_input", "/home/max/ComfyUI/input"))
    if not comfy_input.is_dir():
        raise ComfyError(f"ComfyUI input 目录不存在: {comfy_input}")
    synced = []
    for src in sorted(proj.refs_dir.glob("char_*.png")):
        dst = comfy_input / src.name
        if not dst.exists() or dst.stat().st_size != src.stat().st_size:
            shutil.copy2(src, dst)
            synced.append(src.name)
    if synced:
        log(f"  ↔ 同步 {len(synced)} 张参考图到 ComfyUI input: {', '.join(synced)}")
    return synced


def _gacha_dir(proj, name: str, chapter: int | None = None) -> Path:
    """候选目录按 (角色, 章) 分开：第 2 章换装的候选不该混进默认造型的候选堆里。"""
    if chapter:
        return proj.refs_dir / "_gacha" / f"{name}@ch{int(chapter):02d}"
    return proj.refs_dir / GACHA_DIRNAME / name


def list_candidates(proj, name: str, chapter: int | None = None) -> list[Path]:
    """列出某角色（可指定某章专属）已有的抽卡候选，按 seed 数值排序。"""
    d = _gacha_dir(proj, name, chapter)
    if not d.is_dir():
        return []
    return sorted(d.glob("seed*.png"), key=lambda p: int(p.stem.replace("seed", "") or 0))


def build_contact_sheet(proj, name: str, cols: int = 3) -> Path | None:
    """
    把候选图拼成一张对比图，便于一眼挑选。

    用 ffmpeg 的 tile 滤镜；需要同尺寸输入（我们的候选都是 640×640，天然满足）。
    """
    cands = list_candidates(proj, name)
    if len(cands) < 2:
        return None
    rows = (len(cands) + cols - 1) // cols
    d = _gacha_dir(proj, name)
    out = d / "_sheet.jpg"
    tmp = out.with_suffix(".tmp.jpg")
    cmd = ["ffmpeg", "-v", "error", "-y",
           "-pattern_type", "glob", "-i", str(d / "seed*.png"),
           "-filter_complex", f"scale=320:320,tile={cols}x{rows}",
           "-frames:v", "1", str(tmp)]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        os.replace(tmp, out)
        return out
    except subprocess.CalledProcessError:
        return None


# ---------------------------------------------------------------- 生成


def _portrait_engine(params: dict) -> str:
    """
    定妆照用哪个引擎。`project.json` 的 `portrait_engine` 覆盖，默认 `qwen`。

    ## 为什么默认从 H3 改成 Qwen-Image（2026-09-24）
    原来定妆走 H3 T2VA：**为了拿一张静态图，渲 5 秒 640×640 视频再抽帧** ——
    实测 **~40 秒/张**。而场景/道具用的是 Qwen-Image，**10 秒/张**。

    为一张图渲一段视频，本身就不划算（4 倍差距）。改成 Qwen-Image 后：
      · 单张 40s → 10s；8 角色 × 2 张从 ~11 分钟降到 ~2.7 分钟
      · 输出直接是 PNG，省掉「抽帧」这一步
      · 定妆照的用途（当 H3 的 `<Picture N>` 参考图）只看**像素内容**，
        不关心它是视频抽出来的还是直接生成的

    ⚠️ 保留 H3 通路（`portrait_engine: "h3"`）：旧项目复现、以及万一
        Qwen-Image 出的定妆照在 H3 参考图位置上表现更差时，可以一键切回。
    """
    v = str((params or {}).get("portrait_engine") or "").strip().lower()
    return "h3" if v == "h3" else "qwen"


def portrait_inputs(prompt: str, params: dict, seed: int) -> tuple[str, str, dict]:
    """
    返回这张定妆照**实际会提交给模型的** `(最终提示词, 负向词, 渲染参数)`。

    ★ 指纹与提交必须用同一个来源，否则指纹会"以为变了其实没变"。
    风格后缀在这里拼上（`apply_image_suffix`），因此它天然进指纹 ——
    这就是"换风格 → 定妆照标 stale"能成立的原因。
    """
    from vm import style as _st
    from vm.qi import QIConfig

    final = _st.apply_image_suffix(prompt, params)
    neg = _st.negative_for_params(params)
    cfg = QIConfig()
    render = {"engine": _portrait_engine(params), "unet": cfg.unet_name or cfg.gen,
              "clip": cfg.clip_name, "vae": cfg.vae_name, "gen": cfg.gen,
              "w": cfg.width, "h": cfg.height}
    return final, neg, render


def _render_portrait_qwen(proj, name: str, prompt: str, params: dict, seed: int,
                          log, on_tick=None, should_stop=None) -> tuple[Path, str]:
    """Qwen-Image 出定妆照 PNG。返回 (临时 png 路径, "png")。"""
    from vm.qi import QIConfig, QwenImage, QIResult

    cfg = QIConfig()
    qi = QwenImage(cfg)
    qi.require_ready()
    log(f"  ▶ 生成 {name} seed={seed}（Qwen-Image {cfg.width}x{cfg.height}，约 10s）")
    from vm import style as _st
    _final, _neg, _ = portrait_inputs(prompt, params, seed)
    res: QIResult = qi.generate(
        _final, negative=_neg,
        seed=seed, filename_prefix=f"VM_PORTRAIT_{name}",
        on_tick=on_tick, should_stop=should_stop, log=lambda m: log(f"    ⚠ {m}"),
    )
    img = res.images[0]
    raw_dir = proj.state_dir / "chars_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    dst = raw_dir / f"{name}_{seed}.png"
    qi.comfy.download(img, dst)
    log(f"    ✅ {dst.name}  {res.seconds:.1f}s")
    return dst, "png"


def _render_portrait(proj, name: str, prompt: str, params: dict, seed: int,
                     log, on_tick=None, should_stop=None) -> Path:
    """
    出定妆照的原始文件。默认走 **Qwen-Image**（10s/张，直接出 PNG）；
    设 `portrait_engine: "h3"` 则退回老的 H3 T2VA（~40s，出 mp4 再抽帧）。
    """
    if _portrait_engine(params) == "qwen":
        dst, _ = _render_portrait_qwen(proj, name, prompt, params, seed,
                                       log, on_tick, should_stop)
        return dst
    return _render_portrait_h3(proj, name, prompt, params, seed, log, on_tick, should_stop)


def _render_portrait_h3(proj, name: str, prompt: str, params: dict, seed: int,
                        log, on_tick=None, should_stop=None) -> Path:
    """老的 H3 T2VA 通路：落一个 mp4 到 state/chars_raw/ 再抽帧。保留以便回退。"""
    p = dict(params)
    p["width"] = PORTRAIT_W
    p["height"] = PORTRAIT_H
    comfy = Comfy(p.get("comfy_url", "http://127.0.0.1:8188"))
    comfy.require_healthy()

    frames = seconds_to_frames(PORTRAIT_SEC, int(p.get("fps", 24)))
    # chars 为空 → build_render_workflow 走 T2VA 分支（无参考图，纯文字生成）
    fake = Shot(id=f"char_{name}", sec=PORTRAIT_SEC, chars=[], seed=seed, prompt=prompt)
    wf = build_render_workflow(fake, p, [], frames, seed)

    log(f"  ▶ 生成 {name} seed={seed}（{PORTRAIT_W}x{PORTRAIT_H} {PORTRAIT_SEC}s {frames}帧）")
    pid = comfy.submit(wf)
    entry = comfy.wait(pid, on_tick=on_tick, should_stop=should_stop)
    vid = _pick_video(comfy.outputs_of(entry))
    if not vid:
        raise ComfyError(f"定妆 {name} seed={seed} 完成但没有视频产物")
    return comfy.download(vid, proj.state_dir / "chars_raw" / f"{name}_{seed}.mp4")


def _load_prompt(proj, name: str, log) -> str:
    f = proj.prompts_dir / f"char_{name}.txt"
    if not f.exists():
        raise FileNotFoundError(f"缺少定妆提示词: {f}")
    prompt = f.read_text(encoding="utf-8").strip()
    if not prompt:
        raise ValueError(f"定妆提示词为空: {f}")
    for hit in check_prompt_style(prompt):
        log(f"  ⚠️ 提示词含风格锚「{hit}」—— 实测会得到廉价 3D 卡通感，建议删掉换写实描述")
    return prompt


def gen_char(proj, name: str, params: dict, log=print, *, force: bool = False,
             chapter: int | None = None, on_tick=None, should_stop=None) -> Path:
    """
    生成单个角色的定妆照，返回落盘路径。

    `chapter` 给了就产出**该章专属造型图** `refs/char_<名>@chNN.png`
    （P3.2：同一角色换装 / 受伤 / 回忆段落要另一张参考图）。
    ★ 变的只是"用哪张图"，`chars[0] ↔ ref_image_0` 的顺序契约一个字都没动 ——
      解析全走 `vm/refs.py`，与算指纹同源。
    """
    from vm import refs as _R

    ref = proj.refs_dir / _R.variant_filename(name, chapter)
    prompt = _load_prompt(proj, name, log)
    if chapter:
        prompt = costume_prompt(proj, name, chapter, prompt, log)
    seed = _portrait_seed(name, 0, chapter)
    from vm import imgfp as _fp
    _final, _neg, _render = portrait_inputs(prompt, params, seed)
    fp = _fp.fingerprint("portrait", _final, _neg, _render, seed)
    if ref.exists() and not force:
        # ★ P2.4：以前这里只看"文件在不在"，于是换风格后重跑「定妆」
        # 一张都没重出，日志还写着「⏭ 跳过（已存在）」。
        if not _fp.is_stale(ref, fp):
            log(f"  ⏭ 跳过（已存在且输入未变）: char_{name}.png")
            return ref
        log(f"  ⚠ char_{name}.png 的出图输入已变（风格/提示词/模型），按新输入重出")
    raw = _render_portrait(proj, name, prompt, params, seed, log, on_tick, should_stop)
    _extract_frame(raw, ref)
    _fp.write(ref, fp, kind="portrait", name=name, seed=seed)
    # 单张生成也必须同步到 ComfyUI input —— 否则 LoadImage 找不到图，
    # 点渲染时才失败且报错难懂（webui 验收时实测踩到：4 张里 3 张不在 input）
    sync_refs_to_input(proj, params, log)
    log(f"  ✅ {name} → refs/{ref.name} ({ref.stat().st_size/1e3:.0f} KB)")
    return ref


def gen_candidates(proj, name: str, params: dict, n: int = 6, log=print, *,
                   chapter: int | None = None,
                   on_tick=None, should_stop=None) -> list[Path]:
    """
    抽卡：同一角色出 n 张候选（不同 seed）。

    产物：refs/_gacha/<角色>/seed<NNNN>.png，外加对比图 _sheet.jpg。
    已存在的候选会跳过（可断点续抽）。
    """
    prompt = _load_prompt(proj, name, log)
    if chapter:
        prompt = costume_prompt(proj, name, chapter, prompt, log)
    d = _gacha_dir(proj, name, chapter)
    d.mkdir(parents=True, exist_ok=True)

    from vm import imgfp as _fp
    out: list[Path] = []
    for i in range(n):
        seed = _portrait_seed(name, i, chapter)
        dst = d / f"seed{seed}.png"
        _final, _neg, _render = portrait_inputs(prompt, params, seed)
        fp = _fp.fingerprint("portrait", _final, _neg, _render, seed)
        if dst.exists():
            # ★ P2.4：按 seed 跳过是更隐蔽的一种失效 ——
            # 换风格后"旧风格的候选"仍躺在目录里被当成已抽好的图，
            # 用户点采纳就把旧风格采纳成了新风格。指纹不匹配就重出。
            if not _fp.is_stale(dst, fp):
                log(f"  ⏭ 已有候选 seed={seed}")
                out.append(dst)
                continue
            log(f"  ⚠ seed={seed} 是旧输入出的，按当前风格重出")
        try:
            raw = _render_portrait(proj, name, prompt, params, seed,
                                   log, on_tick, should_stop)
            _extract_frame(raw, dst)
            _fp.write(dst, fp, kind="portrait", name=name, seed=seed)
            log(f"  ✅ 候选 {i+1}/{n} → {dst.name}")
            out.append(dst)
        except Exception as e:
            log(f"  ❌ 候选 seed={seed} 失败: {e}")

    sheet = build_contact_sheet(proj, name)
    if sheet:
        log(f"  🖼 对比图: {sheet}")
    return out


def adopt_candidate(proj, name: str, src: Path, params: dict, log=print, *,
                    chapter: int | None = None) -> Path:
    """
    采纳一张候选（或任意你提供的图）为该角色的定妆照。

    覆盖 refs/char_<角色>.png 并同步到 ComfyUI input。
    **不需要手动处理已渲染的镜头**：参考图 mtime 变了 → 指纹变 → 相关镜头自动 stale。
    """
    src = Path(src)
    if not src.exists():
        raise FileNotFoundError(f"候选图不存在: {src}")
    from vm import refs as _R

    ref = proj.refs_dir / _R.variant_filename(name, chapter)
    shutil.copy2(src, ref)  # 每次采纳都是新 mtime，足以让指纹变化
    from vm import imgfp as _fp
    # 采纳的可能是用户上传的图 —— 它没有"系统出图输入"可言，
    # 所以这里**清掉指纹记录而不是写一个**：没有指纹 = 不判 stale（见 imgfp）。
    _fp.drop(ref)
    sync_refs_to_input(proj, params, log)
    log(f"  ✅ 已采纳 {src.name} → refs/{ref.name}（相关镜头将于下次渲染自动重做）")
    return ref


def import_external_image(proj, name: str, image: Path, params: dict, log=print) -> Path:
    """不限于 H3 生成：把你自己准备的图导入为定妆照。"""
    return adopt_candidate(proj, name, image, params, log)


# ---------------------------------------------------------------- 批量


def _names(proj, only: list[str] | None) -> list[str]:
    names = sorted(f.stem[len("char_"):] for f in proj.prompts_dir.glob("char_*.txt"))
    if only:
        names = [x for x in names if x in set(only)]
    if not names:
        raise ValueError(f"{proj.prompts_dir} 下没有 char_*.txt（或 --only 未匹配）")
    return names


def gen_all_chars(proj, params: dict, *, force=False, only=None, log=print,
                  chapter: int | None = None,
                  on_tick=None, should_stop=None) -> dict[str, Path]:
    """
    批量生成定妆照，最后统一同步到 ComfyUI input。

    `chapter` 给了就是"出这一章所有角色的专属造型"（P3.2），
    产物是 `refs/char_<名>@chNN.png`，不会覆盖全局默认图。
    """
    names = _names(proj, only)
    from vm import costumes as _K
    if chapter:
        # 只处理这一章真的登记了专属造型的角色 —— 给没登记的角色出一张
        # 与默认图内容相同的"假变体"，既白花 GPU 又让人以为这章换过造型。
        withv = [n for n in names if _K.variants_of_chapter(_K.load(proj), n, chapter)]
        skipped = [n for n in names if n not in withv]
        if skipped:
            log(f"  · 第 {chapter} 章未登记专属造型，跳过 {len(skipped)} 个角色："
                f"{', '.join(skipped)}")
        names = withv
        if not names:
            log(f"  第 {chapter} 章没有任何专属造型登记，无需出图")
            return {}
        log(f"===== 第 {chapter} 章专属定妆照 {len(names)} 个角色 =====")
    else:
        log(f"===== 定妆照 {len(names)} 个角色 =====")
    out: dict[str, Path] = {}
    failed: list[str] = []
    for i, name in enumerate(names, 1):
        log(f"[{i}/{len(names)}] {name}")
        try:
            out[name] = gen_char(proj, name, params, log, force=force,
                                 chapter=chapter,
                                 on_tick=on_tick, should_stop=should_stop)
        except Exception as e:
            log(f"  ❌ {name} 失败: {e}")
            failed.append(name)
    sync_refs_to_input(proj, params, log)
    log(f"===== 定妆结束：成功 {len(out)} / 失败 {len(failed)} =====")
    if failed:
        log("失败角色: " + ", ".join(failed))
    return out


def gen_all_candidates(proj, params: dict, n: int = 6, *, only=None, log=print,
                       on_tick=None, should_stop=None) -> dict[str, list[Path]]:
    """对所有角色批量抽卡（每个角色 n 张）。"""
    names = _names(proj, only)
    log(f"===== 抽卡：{len(names)} 个角色 × {n} 张 = {len(names)*n} 张 =====")
    out: dict[str, list[Path]] = {}
    for i, name in enumerate(names, 1):
        log(f"[{i}/{len(names)}] {name}")
        try:
            out[name] = gen_candidates(proj, name, params, n, log,
                                       on_tick=on_tick, should_stop=should_stop)
        except Exception as e:
            log(f"  ❌ {name} 抽卡失败: {e}")
            out[name] = []
    return out
