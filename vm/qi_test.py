"""
qi_test.py —— qi.py / storyboard.py 的自测脚本（默认不联网、不碰 GPU）。

    python3 -m vm.qi_test            # 纯离线单测（提示词清洗 / 指纹 / 工作流 / 索引 / PNG）
    python3 -m vm.qi_test --live     # 额外打一次 ComfyUI 探测（只读，不提交任务）

为什么默认离线：这两条链路里最容易悄悄坏掉的是**不改文件也看不出来**的部分 ——
提示词到底剥干净了没、指纹能不能分辨"改了提示词"、工作流里挂的是不是 16 通道 latent。
那些都不需要 GPU 就能钉死；需要 GPU 的部分（真出图）交给 --live 与实跑。
"""

from __future__ import annotations

import json
import struct
import sys
import tempfile
import zlib
from pathlib import Path

from .qi import QIConfig, QwenImage, qi_fingerprint, snap_size, text_fingerprint
from .storyboard import (
    SBIndex,
    image_prompt_of,
    png_info,
    split_sections,
)

FAILS: list[str] = []
PASSES = 0

ROOT = Path(__file__).resolve().parent.parent
CHAPTER = ROOT / "projects" / "西游记" / "shots" / "chapter01.json"


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSES
    if cond:
        PASSES += 1
        print(f"  ✅ {name}")
    else:
        FAILS.append(f"{name} {detail}")
        print(f"  ❌ {name} {detail}")


# ── 1. 尺寸吸附 ─────────────────────────────────────────────────────────────


def t_snap() -> None:
    print("[1] 尺寸吸附（必须能被 16 整除）")
    check("1024×576 不动", snap_size(1024, 576) == (1024, 576, ""))
    w, h, note = snap_size(1000, 570)
    check("1000×570 → 992×560 且有说明", (w, h) == (992, 560) and bool(note), f"得到 {(w, h, note)}")
    w, h, _ = snap_size(10, 10)
    check("过小尺寸夹到 16", (w, h) == (16, 16), f"得到 {(w, h)}")


# ── 2. 指纹 ─────────────────────────────────────────────────────────────────


def t_fp() -> None:
    print("[2] 指纹：可复现 + 能分辨变化")
    cfg = QIConfig()
    r = cfg.render_dict()
    a = qi_fingerprint("p", "", r, 1)
    check("同输入同指纹", a == qi_fingerprint("p", "", cfg.render_dict(), 1))
    check("改提示词 → 指纹变", a != qi_fingerprint("P", "", r, 1))
    check("改 seed → 指纹变", a != qi_fingerprint("p", "", r, 2))
    r2 = dict(r, width=1152)
    check("改分辨率 → 指纹变", a != qi_fingerprint("p", "", r2, 1))
    r3 = dict(r, steps=30)
    check("改步数 → 指纹变", a != qi_fingerprint("p", "", r3, 1))
    check("改负向词 → 指纹变", a != qi_fingerprint("p", "bad", r, 1))
    check("render_dict 不进 URL/端口", "url" not in r and "base" not in r)
    check("text_fingerprint 稳定", text_fingerprint("abc") == text_fingerprint("abc"))
    check("text_fingerprint 分辨", text_fingerprint("abc") != text_fingerprint("abd"))


# ── 3. 提示词清洗 ───────────────────────────────────────────────────────────


def t_prompt() -> None:
    print("[3] 提示词清洗（真镜头表 + 合成用例）")
    if not CHAPTER.exists():
        check("找到 projects/西游记/shots/chapter01.json", False, str(CHAPTER))
        return
    shots = json.loads(CHAPTER.read_text(encoding="utf-8"))
    by_id = {s["id"]: s for s in shots}

    s = by_id["1-1-01"]
    orig = split_sections(s["prompt"])["detailed_description"]
    clean, info = image_prompt_of(s["prompt"])
    check("用的是 detailed_description", info["section_used"] == "detailed_description")
    check("剥掉 1 处 <d>", info["stripped"]["dialogue"] == 1, str(info["stripped"]))
    check("剥掉 CAMERA+POSITION 两条纪律行", info["stripped"]["discipline_lines"] == 2,
          str(info["stripped"]))
    check("<d> 标签与台词正文都没了",
          "<d>" not in clean.lower() and "师徒四人一路向西" not in clean)
    check("CAMERA DISCIPLINE 没了", "CAMERA DISCIPLINE" not in clean)
    check("POSITION DISCIPLINE 没了", "POSITION DISCIPLINE" not in clean)
    check("画面正文保留（长度只缩短个位数%）",
          len(clean) > len(orig) * 0.95, f"{len(clean)} vs {len(orig)}")
    check("保留了镜头描述里真正的画面句", "winding mountain road at dusk" in clean)
    check("标注了 <Subject N> 占位符风险", any("<Subject" in w or "Picture" in w
                                              for w in info["warnings"]))

    # 合成六段式：纪律行写在 detailed_description **段内**（位置漂移也要兜住）
    synth = (
        "subject_definitions: <Subject 1> = a young monk shown in <Picture 1>: pale face, moon-white robe.\n"
        "summary: [reference generation] single shot of <Subject 1>.\n"
        "retention_analysis: <Subject 1> ([Shot 1]): fully_preserved.\n"
        "detailed_description: A rainy alley. CAMERA DISCIPLINE: slow push in.\n"
        "POSITION DISCIPLINE: keep subject centre.\n"
        "overall_soundscape: rain on tiles.\n"
        "non_diegetic_music: low strings.\n"
    )
    c2, i2 = image_prompt_of(synth)
    check("段内纪律行也剥掉", i2["stripped"]["discipline_lines"] == 2, str(i2["stripped"]))
    check("段内清洗后只剩画面句", c2.strip() == "A rainy alley.", repr(c2))
    check("声音段没被喂进图像模型", "rain on tiles" not in c2 and "low strings" not in c2)

    # 带 <d> 的合成用例
    c3, i3 = image_prompt_of(
        "detailed_description: A hall at night. The elder says: <d>悟空，不得无礼。</d> "
        "Lamps flicker.\noverall_soundscape: wind.\n"
    )
    check("合成 <d> 剥掉", i3["stripped"]["dialogue"] == 1 and "悟空" not in c3)

    # 旧表自由文本：容忍 + 必须留警告
    c4, i4 = image_prompt_of("一个雨夜的巷子，青石板反光。")
    check("旧表自由文本走 raw 分支", i4["section_used"].startswith("raw"), i4["section_used"])
    check("旧表分支带警告", bool(i4["warnings"]))
    check("旧表内容原样保留", c4 == "一个雨夜的巷子，青石板反光。")

    # 只有声音段 → 必须报错（拒绝把整段六段式喂给图像模型）
    try:
        image_prompt_of("overall_soundscape: wind.\nnon_diegetic_music: strings.\n")
        check("没有画面段时抛错", False, "居然没抛")
    except ValueError as e:
        check("没有画面段时抛错且说清原因", "画面段" in str(e))

    # --with-subject-defs（默认关闭，开了才拼）
    c5, i5 = image_prompt_of(s["prompt"], with_subject_defs=True)
    check("--with-subject-defs 会拼外观描述", "Chinese Buddhist monk" in c5 and len(c5) > len(clean))
    check("--with-subject-defs 有留痕警告", any("subject_definitions" in w for w in i5["warnings"]))
    check("默认不拼（关闭时不出现）", "Chinese Buddhist monk" not in clean)

    # 全表冒烟：52 镜都能算出画面提示词
    bad = []
    for sh in shots:
        try:
            image_prompt_of(sh["prompt"])
        except ValueError as e:
            bad.append(f"{sh['id']}: {e}")
    check(f"整表 {len(shots)} 镜都能算出提示词", not bad, "; ".join(bad[:3]))


# ── 4. 工作流形状 ───────────────────────────────────────────────────────────


def t_workflow() -> None:
    print("[4] 工作流拼装")
    cfg = QIConfig()
    qi = QwenImage(cfg)
    g = qi.build("a cat", seed=7, filename_prefix="VM_SB_x")
    types = {k: v["class_type"] for k, v in g.items()}
    for need in ("UNETLoader", "CLIPLoader", "VAELoader", "CLIPTextEncode",
                 "EmptySD3LatentImage", "KSampler", "VAEDecode", "SaveImage"):
        check(f"含 {need}", need in types.values(), str(sorted(types.values())))
    check("latent 用 16 通道的 EmptySD3LatentImage",
          "EmptyLatentImage" not in types.values())
    ks = g["8"]["inputs"]
    check("KSampler 接的是 16 通道 latent", ks["latent_image"] == ["7", 0])
    check("KSampler model 默认直连 UNETLoader", ks["model"] == ["1", 0])
    check("KSampler 正/负向都接 CLIPTextEncode", ks["positive"] == ["5", 0] and ks["negative"] == ["6", 0])
    check("VAEDecode 接 KSampler + VAELoader", g["9"]["inputs"]["samples"] == ["8", 0]
          and g["9"]["inputs"]["vae"] == ["3", 0])
    check("SaveImage 接 VAEDecode", g["10"]["inputs"]["images"] == ["9", 0])
    # 断言要**跟随配置**而不是硬编码某一代模型：我们在 2.1 → 2.0 之间切过一次，
    # 写死 "qwen3.5_9b" 的断言在切换后就变成假失败（测试锁的是写法不是行为）。
    check("CLIPLoader type=qwen_image 且 clip_name 来自配置",
          g["2"]["inputs"]["type"] == "qwen_image"
          and g["2"]["inputs"]["clip_name"] == cfg.clip_name)
    check("尺寸 1024×576 传进 latent",
          (g["7"]["inputs"]["width"], g["7"]["inputs"]["height"]) == (1024, 576))
    check("seed 透传", ks["seed"] == 7)
    check("默认不挂 ModelSamplingAuraFlow", "4" not in g)

    cfg2 = QIConfig(aura_shift=3.1)
    g2 = QwenImage(cfg2).build("a cat", seed=1)
    check("aura_shift 开启时挂节点 4", g2.get("4", {}).get("class_type") == "ModelSamplingAuraFlow")
    check("aura_shift 时 KSampler 改接节点 4", g2["8"]["inputs"]["model"] == ["4", 0])

    notes: list[str] = []
    g3 = QwenImage(QIConfig()).build("a cat", width=1000, height=570, extra_note=notes.append)
    check("非法尺寸吸附有回调说明", bool(notes) and (g3["7"]["inputs"]["width"], g3["7"]["inputs"]["height"]) == (992, 560))

    try:
        QwenImage(QIConfig()).build("   ")
        check("空提示词拒绝提交", False, "居然没抛")
    except Exception as e:  # ComfyError
        check("空提示词拒绝提交", "空" in str(e))


# ── 5. 索引三态 ─────────────────────────────────────────────────────────────


def t_index() -> None:
    print("[5] 索引幂等三态 + PNG 头解析")
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        png = td / "1-1-01.png"
        data = _tiny_png(1024, 576)
        png.write_bytes(data)

        idx = SBIndex.load(td / "storyboard.json")
        check("无记录 → missing 之前先判文件", idx.status("1-1-01", "fp1", td / "nope.png") == "missing")
        check("有文件但无记录 → stale", idx.status("1-1-01", "fp1", png) == "stale")
        idx.mark("1-1-01", "fp1", png, prompt_fp="pf1", width=1024, height=576,
                 seconds=1.5, prompt_id="pid", seed=3, vram_peak_mb=1234,
                 prompt_used="x", info={"a": 1})
        idx.save()
        idx2 = SBIndex.load(td / "storyboard.json")
        check("落盘可回读", idx2.status("1-1-01", "fp1", png) == "current")
        check("指纹不一致 → stale（改了提示词就认得出）",
              idx2.status("1-1-01", "fp2", png) == "stale")
        check("记录了 prompt_used / prompt_id / 显存",
              idx2.shots["1-1-01"].prompt_id == "pid"
              and idx2.shots["1-1-01"].prompt_used == "x"
              and idx2.shots["1-1-01"].vram_peak_mb == 1234)
        png.write_bytes(data + b"\x00")  # 大小对不上
        check("产物被改动（大小不符）→ stale", idx2.status("1-1-01", "fp1", png) == "stale")

        w, h, size = png_info(png)
        check("PNG 回读宽高", (w, h) == (1024, 576) and size == len(data) + 1)
        bogus = td / "bogus.png"
        bogus.write_bytes(b"not a png at all..........")
        try:
            png_info(bogus)
            check("非 PNG 报错", False, "居然没抛")
        except ValueError:
            check("非 PNG 报错", True)


def _tiny_png(w: int, h: int) -> bytes:
    """构一个只有 IHDR+IEND 的最小 PNG（够 png_info 读宽高，不需要真图像数据）。"""
    def chunk(typ: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


# ── 6. 联网探测（只读）─────────────────────────────────────────────────────


def t_live() -> None:
    print("[6] --live：ComfyUI 探测（只读，不提交任务）")
    p = QwenImage(QIConfig()).probe()
    print(p.report())
    check("ComfyUI 在线", p.healthy)
    check("必需节点齐全（缺节点必须靠更新 ComfyUI 解决，不降级）", not p.nodes_missing,
          ", ".join(p.nodes_missing))
    if p.models_missing:
        print("  ℹ 模型尚未就绪（下载中/未放置）：" + ", ".join(p.models_missing))


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    print("== vm 自测：Qwen-Image 分镜图链路 ==")
    t_snap()
    t_fp()
    t_prompt()
    t_workflow()
    t_index()
    if "--live" in argv:
        t_live()
    print()
    print(f"通过 {PASSES} 项" + (f"，失败 {len(FAILS)} 项：" if FAILS else "，无失败"))
    for f in FAILS:
        print("  ❌ " + f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
