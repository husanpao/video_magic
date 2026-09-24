"""
e2e_fixture.py —— E2E 合成夹具（S14「夹具自治」提前落地，lead 2026 裁决）。

为什么：真实项目数据会丢、会变（西游记成品数据丢失事件），E2E 断言锚在
真实项目上就永远不稳 ——「46 已生成 / 52 分镜图 / audit 9 条」这类硬编码
随数据漂移必然腐烂。这里在 tmp projects 根**现造**一个已知值的合成项目：
6 镜 × 2 章、真 ffmpeg 生成的极小彩条 mp4 / 最小 PNG 占位产物，
manifest / qc / audit / storyboard / script 全部**用真实模块按真实 schema 写出** ——
UI 走的是真代码路径，只有数据是合成的，断言值因此永远精确。

    python3 -m vm.tests.e2e_fixture --build            # 只造夹具（默认 /tmp/vm-e2e）
    python3 -m vm.tests.e2e_fixture --serve --port 8899 # 造夹具并起 Web（E2E 打它）

已知值锚点（改这里必须同步 web/ui-e2e.mjs 的对应断言）：
    项目 e2efx；镜头 6（第 1 章 4 + 第 2 章 2）；
    三态 current 3 / stale 1 / missing 2；
    qc：pass 2 / suspicious 1 / fail 1，rerender 1 条、review 1 条；
    audit：3 条 findings（镜头级 2 + 全片级 1）；
    场景 2（山门/古井）；道具 2（禅杖/念珠）；角色 2（唐僧/孙悟空，各带 2 张抽卡候选）；
    分镜图 6 张全 current；成片 EP01 + EP02；剧本逐字校验 gate_passed=True；
    budget.max_gpu_minutes 极小（点「全链」必弹 E3 审批，不起真任务）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURE_NAME = "e2efx"
DEFAULT_ROOT = Path("/tmp/vm-e2e")

# ── 已知值锚点（断言与夹具的共同真相）────────────────────────────────────────
SHOTS_CH1 = [
    # (id, sec, chars, seed, scene, props, dialogue, narration, 产物形态)
    ("1-1-01", 5, ["唐僧"], 3001, "S1", ["P1"], "山门之外，可有异动？", "", "current"),
    ("1-1-02", 4, ["孙悟空"], 3002, "S1", ["P1"], "", "悟空按住禅杖，望向山门。", "current"),
    ("1-2-01", 6, ["唐僧", "孙悟空"], 3003, "S1", [], "莫要惊动寺中僧人。", "", "stale"),
    ("1-3-01", 5, ["孙悟空"], 3004, "S2", ["P2"], "这井底透着古怪。", "", "current"),
]
SHOTS_CH2 = [
    ("2-1-01", 5, ["唐僧"], 3005, "S2", ["P2"], "夜里再来探看。", "", "missing"),
    ("2-1-02", 4, [], 3006, "S2", [], "", "夜色笼罩古井。", "missing"),
]
NOVEL = {
    1: ("第1章-山门",
        "# 第1章 山门\n\n"
        "夜色深沉，山门紧闭。\n"
        "唐僧立在阶前问：山门之外，可有异动？\n"
        "悟空按住禅杖，望向山门。\n"
        "唐僧低声道：莫要惊动寺中僧人。\n"
        "两人绕到殿后，悟空盯着井口说：这井底透着古怪。\n"),
    2: ("第2章-古井",
        "# 第2章 古井\n\n"
        "唐僧沉吟片刻，道：夜里再来探看。\n"
        "夜色笼罩古井。\n"),
}
PROMPT = (
    "subject_definitions: <Subject 1> = a young monk in grey robe.\n"
    "summary: [reference generation] single shot of <Subject 1>.\n"
    "retention_analysis: <Subject 1>: fully_preserved.\n"
    "detailed_description: Cinematic realism. A mountain temple gate at night, "
    "the wind moving the cloth of the robe. CAMERA DISCIPLINE: slow push in.\n"
    "overall_soundscape: wind and insects.\n"
    "non_diegetic_music: low strings.\n"
)


# ── 占位产物（真 ffmpeg 彩条 mp4 / 最小合法 PNG）─────────────────────────────


def tiny_png(w: int = 64, h: int = 64) -> bytes:
    """只有 IHDR+IEND 的最小合法 PNG（png_info/浏览器都认，几十字节）。"""
    def chunk(typ: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


def tiny_mp4(path: Path, seconds: float = 1.2) -> None:
    """真 ffmpeg 出 1~2 秒彩条 —— ffprobe/缩略图/播放器全链路都要认它。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", f"testsrc=size=320x180:rate=24:duration={seconds}",
         "-pix_fmt", "yuv420p", "-c:v", "libx264", "-movflags", "+faststart",
         str(path)],
        capture_output=True, text=True, timeout=60)
    if r.returncode != 0 or not path.is_file():
        raise RuntimeError(f"ffmpeg 造彩条失败：{r.stderr[:200]}")


# ── 构建 ────────────────────────────────────────────────────────────────────


def build(root: Path) -> Path:
    """在 root（projects 根）下造 e2efx 项目，返回项目目录。重复调用会先清掉重造。"""
    from vm import taskctl
    from vm.qi import qi_fingerprint
    from vm import script as SB
    from vm import shots as SH
    from vm import storyboard as STB
    from vm import style as ST
    from vm.state import Manifest, Project, shot_fingerprint

    pdir = Path(root) / FIXTURE_NAME
    if pdir.exists():
        shutil.rmtree(pdir)
    proj = Project(pdir).ensure()
    (proj.root / "storyboard").mkdir(exist_ok=True)
    (proj.refs_dir / "_gacha" / "唐僧").mkdir(parents=True, exist_ok=True)

    # ① 小说 + 镜头表（六段式 prompt + 对白逐字进正文，剧本校验要 100% 覆盖）
    for no, (stem, text) in NOVEL.items():
        (proj.novel_dir / f"{stem}.md").write_text(text, encoding="utf-8")

    def _shot_dict(t) -> dict:
        sid, sec, chars, seed, scene, props, dlg, nar, _ = t
        return {
            "id": sid, "sec": sec, "chars": chars, "seed": seed, "prompt": PROMPT,
            "shot_size": "中景", "camera": "slow push in",
            "dialogue": dlg, "narration": nar,
            "action": "静立对望", "scene_id": scene, "prop_ids": props,
            # 回填时长（A2 三层的 sec_actual）：给分段条一点宽度差异
            "sec_actual": round(sec - 0.12, 3),
        }

    for no, rows in ((1, SHOTS_CH1), (2, SHOTS_CH2)):
        (proj.shots_dir / f"chapter{no:02d}.json").write_text(
            json.dumps([_shot_dict(t) for t in rows], ensure_ascii=False, indent=2),
            encoding="utf-8")

    # ② 场景/道具实体（跨镜一致性锚点）
    (pdir / "scenes.json").write_text(json.dumps({"scenes": [
        {"id": "S1", "name": "山门", "description": "A mountain temple gate at night, stone steps."},
        {"id": "S2", "name": "古井", "description": "An ancient well behind the hall, moss on stone."},
    ]}, ensure_ascii=False, indent=2), encoding="utf-8")
    (pdir / "props.json").write_text(json.dumps({"props": [
        {"id": "P1", "name": "禅杖", "description": "A bronze khakkhara staff with rings."},
        {"id": "P2", "name": "念珠", "description": "A string of dark wooden prayer beads."},
    ]}, ensure_ascii=False, indent=2), encoding="utf-8")

    # ③ 角色：提示词 + 定妆 PNG + 抽卡候选（char_table 的全部输入）
    for name, text in (("唐僧", "a young Buddhist monk, grey robe, calm face"),
                       ("孙悟空", "a monkey warrior, golden headband, tiger-skin kilt")):
        (proj.prompts_dir / f"char_{name}.txt").write_text(text, encoding="utf-8")
        (proj.refs_dir / f"char_{name}.png").write_bytes(tiny_png())
    for seed in (7, 13):
        (proj.refs_dir / "_gacha" / "唐僧" / f"seed{seed}.png").write_bytes(tiny_png())

    # ④ 片段产物：current/stale 的 4 镜有彩条 mp4，missing 的 2 镜没有
    for t in SHOTS_CH1 + SHOTS_CH2:
        sid, _, _, _, _, _, _, _, kind = t
        if kind in ("current", "stale"):
            tiny_mp4(proj.clip(sid))

    # ⑤ manifest（真 schema）：current 3 / stale 1（指纹故意错）/ missing 2；一镜锁+藏
    # ★ 指纹不自己算，直接取 shot_table 现算的值（单一真相）——
    #   自己算过一遍与查询口径差之毫厘时，"current"会静默变"需重渲"，
    #   断言就漂了（实测踩过：两套计算差一位，锚点从 3/1/2 漂到 2/2/2）。
    tbl = taskctl.shot_table(pdir)
    fp_by_id = {r["id"]: r["fp"] for r in tbl["shots"]}
    params = taskctl.load_params(pdir)
    fps = int(params.get("fps") or 24)
    m = Manifest.load(proj.state_dir / "manifest.json")
    for t in SHOTS_CH1 + SHOTS_CH2:
        sid, sec, chars, seed, _, _, _, _, kind = t
        if kind == "missing":
            continue
        clip = proj.clip(sid)
        m.mark(sid, "stale-on-purpose" if kind == "stale" else fp_by_id[sid], clip,
               qc="fail" if sid == "1-3-01" else "unknown",
               sec_actual=round(sec - 0.12, 3))
    m.set_flag("1-3-01", "locked", True, by="e2e")
    m.set_flag("1-3-01", "favorite", True)
    m.save()

    # ⑥ qc.json（_tolerant_qc 的容器形状）：pass 2 / suspicious 1 / fail 1
    (proj.state_dir / "qc.json").write_text(json.dumps({
        "generated_at": 1700000000,
        "results": {
            "1-1-01": {"ok": True, "verdict": "pass", "issues": [], "metrics": {"luma": 100}},
            "1-1-02": {"ok": True, "verdict": "suspicious", "issues": ["夜戏偏暗"],
                       "metrics": {"luma": 18}},
            "1-2-01": {"ok": True, "verdict": "pass", "issues": [], "metrics": {"luma": 90}},
            "1-3-01": {"ok": False, "verdict": "fail", "issues": ["尾帧冻结"],
                       "metrics": {"freeze": 1}},
        },
        "rerender": ["1-3-01"],
        "review": ["1-1-02"],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # ⑦ audit.json（audit_table 的原始形状）：镜头级 2 + 全片级 1
    (proj.state_dir / "audit.json").write_text(json.dumps({
        "generated_at": "2026-01-01T00:00:00Z",
        "inputs": {"shots": 6},
        "summary": {"total_findings": 3, "findings": {"b2": 1, "b4": 1, "b5": 1},
                    "gate_passed": False},
        "manifest_entry": {}, "b1": {}, "b2": {}, "b5": {},
        "findings": [
            {"code": "ambiguous-speaker", "severity": "warning", "module": "b4",
             "shot_ids": ["1-1-02"], "from_id": "1-1-02", "to_id": "1-1-02",
             "chars": ["孙悟空"], "message": "旁白与台词说话人不清：1-1-02",
             "rewrite_hint": "在 detailed_description 前写明说话人"},
            {"code": "lost-dialogue", "severity": "error", "module": "b5",
             "shot_ids": ["2-1-01"], "from_id": "2-1-01", "to_id": "2-1-01",
             "chars": ["唐僧"], "message": "台词丢失：2-1-01 的对白没进任何镜头画面",
             "rewrite_hint": "把对白逐字写进该镜提示词"},
            {"code": "no-climax", "severity": "info", "module": "b2",
             "message": "全片无明显高潮（冲突张力平缓）", "rewrite_hint": ""},
        ],
        "limitations": ["B2 代理分：冲突张力为逐镜估算"],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # ⑧ 分镜图：6 张最小 PNG + 真指纹索引（status 全 current）
    cfg = STB.QIConfig()
    index = STB.SBIndex.load(proj.state_dir / STB.INDEX_FILENAME)
    shots = SH.load_shots_dir(proj.shots_dir)
    for s in shots:
        png = proj.root / "storyboard" / f"{s.id}.png"
        png.write_bytes(tiny_png())
        prompt, _ = STB.image_prompt_of(s.prompt)
        negative = ST.negative_for(getattr(cfg, "style_preset", None))
        fp = qi_fingerprint(prompt, negative, cfg.render_dict(), int(s.seed))
        index.mark(s.id, fp, png, prompt_fp=fp, width=64, height=64, seconds=1.2,
                   prompt_id=f"pid-{s.id}", seed=int(s.seed), vram_peak_mb=1,
                   prompt_used=prompt, info={"fixture": True})
    index.save()

    # ⑨ 剧本视图：真 build_from_shots（对白逐字回查正文 → gate_passed）
    for no in (1, 2):
        ch_shots = [s for s in shots if s.id.split("-")[0] == str(no)]
        sc = SB.build_from_shots(proj, no, ch_shots, NOVEL[no][1])
        SB.save_script(proj, sc)

    # ⑩ 成片：逐章出集（EP01/EP02）+ 一份 srt
    tiny_mp4(proj.final_dir / "EP01.mp4", seconds=2.0)
    tiny_mp4(proj.final_dir / "EP02.mp4", seconds=2.0)
    (proj.final_dir / "EP01.srt").write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n山门之外，可有异动？\n", encoding="utf-8")

    # ⑪ 预算护栏：额度极小 → 点「全链」必弹 E3 审批（409），绝不起真任务
    (pdir / "project.json").write_text(json.dumps({
        "budget": {"max_gpu_minutes": 0.001, "max_llm_tokens": 1},
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # ⑫ 构建自检：锚点不对直接报错，别把漂移的夹具交给 E2E（改这里必须同步 ui-e2e.mjs）
    taskctl._SHOT_TABLE_CACHE.clear()
    counts = taskctl.shot_table(pdir)["counts"]
    want = {"total": 6, "current": 3, "stale": 1, "missing": 2}
    if {k: counts[k] for k in want} != want:
        raise RuntimeError(f"夹具锚点漂移！期望 {want}，实得 {counts} —— 先修夹具再跑 E2E")
    return pdir


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m vm.tests.e2e_fixture",
                                 description="E2E 合成夹具：造已知值的假项目（可顺手起服务）。")
    ap.add_argument("--root", default=str(DEFAULT_ROOT), help="projects 根（默认 /tmp/vm-e2e）")
    ap.add_argument("--build", action="store_true", help="只造夹具")
    ap.add_argument("--serve", action="store_true", help="造夹具并起 Web UI")
    ap.add_argument("--port", type=int, default=8899, help="服务端口（默认 8899，别占用户的 8801）")
    a = ap.parse_args(argv)
    root = Path(a.root)
    root.mkdir(parents=True, exist_ok=True)
    pdir = build(root)
    print(f"[e2e-fixture] 合成项目已就绪：{pdir}")
    print("[e2e-fixture] 锚点：6 镜（current 3 / stale 1 / missing 2）；"
          "qc pass 2 / suspicious 1 / fail 1；audit 3 条；场景 山门/古井；道具 禅杖/念珠；"
          "角色 唐僧/孙悟空；分镜 6 张；成片 EP01+EP02")
    if a.serve:
        from vm import web
        web.serve(root, a.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
