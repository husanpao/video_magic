"""
链式渲染（vm/chain.py + vm/voice.py）的单元测试。

重点覆盖**算错就会静默出错**的地方：
  · 帧格点（段长/重叠必须是 5+17n / 5+17k）—— 错一格节点包直接报错，早测早发现
  · 编组规则（同场景 + 同角色 + 相邻 + 未锁定）—— 角色集合不同会让 <Picture N> 编号错位
  · 链上下文指纹 —— 第 0 镜必须**不**变（否则启用链渲染会让已有产物全变 stale）
  · 配音指纹 —— 改台词必须重出，否则"改了台词配音还是旧的"
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from vm import chain as C
from vm import voice as V
from vm.shots import Shot
from vm.state import shot_fingerprint

QUIET = lambda *_a, **_k: None  # noqa: E731


def _s(sid: str, sec: int, chars: list[str], scene_id: str = "S1", prompt: str = "p") -> Shot:
    return Shot(id=sid, sec=sec, chars=list(chars), seed=1, prompt=prompt, scene_id=scene_id)


class GeometryTest(unittest.TestCase):
    """帧几何：段长 = 计划帧 + 重叠 − 5（去重后每镜只损失 5 帧）。"""

    def test_frames_are_h3_aligned(self) -> None:
        ch = [_s("1-3-01", 5, ["孙悟空"]), _s("1-4-01", 6, ["孙悟空"]), _s("1-4-02", 3, ["孙悟空"])]
        segs = C.geometry(ch, 22)
        self.assertEqual([s["plan_frames"] for s in segs], [124, 141, 73])
        self.assertEqual([s["gen_frames"] for s in segs], [124, 158, 90])
        self.assertEqual([s["new_frames"] for s in segs], [124, 136, 68])
        self.assertEqual([s["start"] for s in segs], [0, 102, 238])
        self.assertEqual([s["end"] for s in segs], [124, 260, 328])
        self.assertEqual(C.total_frames(segs), 328)
        self.assertEqual(C.new_content_starts(segs), [0, 124, 260])
        for s in segs:
            self.assertEqual((s["gen_frames"] - 5) % 17, 0, "段长必须是 5+17n")

    def test_total_equals_sum_of_plan_minus_5_each(self) -> None:
        ch = [_s("a", 5, ["x"]), _s("b", 5, ["x"]), _s("c", 5, ["x"])]
        segs = C.geometry(ch, 22)
        self.assertEqual(C.total_frames(segs), 3 * 124 - 2 * 5)

    def test_overlap_must_be_on_grid(self) -> None:
        ch = [_s("a", 5, ["x"]), _s("b", 5, ["x"])]
        for bad in (0, 20, 21, 23):
            with self.assertRaises(ValueError, msg=f"overlap={bad} 应报错"):
                C.geometry(ch, bad)
        for ok in (5, 22, 39):
            C.geometry(ch, ok)  # 不抛

    def test_single_shot_geometry_has_no_overlap(self) -> None:
        """单镜几何是合法的（编组层才要求 ≥2 镜）。"""
        segs = C.geometry([_s("a", 5, ["x"])], 22)
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0]["start"], 0)
        self.assertEqual(segs[0]["gen_frames"], segs[0]["plan_frames"])
        self.assertEqual(segs[0]["new_frames"], segs[0]["plan_frames"])


class PlanChainsTest(unittest.TestCase):
    """编组规则：同场景 + 同角色（顺序也要一样）+ 相邻 + 未锁定 + 每链上限。"""

    def test_same_scene_same_chars_are_chained(self) -> None:
        shots = [_s(f"s{i}", 5, ["孙悟空"]) for i in range(3)]
        self.assertEqual(len(C.plan_chains(shots, log=QUIET)), 1)

    def test_different_chars_break_chain(self) -> None:
        shots = [_s("a", 5, ["孙悟空"]), _s("b", 5, ["唐僧"]), _s("c", 5, ["孙悟空"])]
        self.assertEqual(C.plan_chains(shots, log=QUIET), [])

    def test_char_order_matters(self) -> None:
        """顺序不同会让 <Picture N> 编号错位（画面正常但人不对）—— 必须断开。"""
        shots = [_s("a", 5, ["孙悟空", "唐僧"]), _s("b", 5, ["唐僧", "孙悟空"])]
        self.assertEqual(C.plan_chains(shots, log=QUIET), [])

    def test_different_scene_breaks_chain(self) -> None:
        shots = [_s("a", 5, ["孙悟空"], "S1"), _s("b", 5, ["孙悟空"], "S2")]
        self.assertEqual(C.plan_chains(shots, log=QUIET), [])

    def test_empty_scene_id_never_chains(self) -> None:
        shots = [_s("a", 5, ["孙悟空"], ""), _s("b", 5, ["孙悟空"], "")]
        self.assertEqual(C.plan_chains(shots, log=QUIET), [])

    def test_locked_shot_breaks_chain(self) -> None:
        shots = [_s("a", 5, ["x"]), _s("b", 5, ["x"]), _s("c", 5, ["x"])]
        got = C.plan_chains(shots, is_locked=lambda sid: sid == "b", log=QUIET)
        self.assertEqual(got, [])  # b 锁定 → a|b 断开、b|c 也断开

    def test_max_shots_splits(self) -> None:
        shots = [_s(f"s{i}", 5, ["x"]) for i in range(7)]
        got = C.plan_chains(shots, max_shots=3, log=QUIET)
        self.assertEqual([len(c) for c in got], [3, 3])   # 第 7 镜落单 → 不成链

    def test_single_shot_not_a_chain(self) -> None:
        self.assertEqual(C.plan_chains([_s("a", 5, ["x"])], log=QUIET), [])


class ChainFingerprintTest(unittest.TestCase):
    def test_first_shot_has_no_chain_context(self) -> None:
        ch = [_s("a", 5, ["x"]), _s("b", 5, ["x"])]
        self.assertEqual(C.chain_extra_fp(ch, 0, 22), "")

    def test_later_shot_depends_on_previous(self) -> None:
        ch = [_s("a", 5, ["x"], prompt="老提示词"), _s("b", 5, ["x"])]
        before = C.chain_extra_fp(ch, 1, 22)
        ch[0].prompt = "新提示词"
        self.assertNotEqual(before, C.chain_extra_fp(ch, 1, 22), "前一镜改了，后一镜必须变")

    def test_overlap_participates(self) -> None:
        ch = [_s("a", 5, ["x"]), _s("b", 5, ["x"])]
        self.assertNotEqual(C.chain_extra_fp(ch, 1, 22), C.chain_extra_fp(ch, 1, 39))


class ShotFingerprintExtraTest(unittest.TestCase):
    def test_empty_extra_keeps_old_hash(self) -> None:
        """★ 关键回归：没成链的镜头指纹必须与老版本逐字节一致。"""
        base = shot_fingerprint("p", ["a"], [], {"unet": "u"}, 124, 7)
        with_empty = shot_fingerprint("p", ["a"], [], {"unet": "u"}, 124, 7, extra="")
        self.assertEqual(base, with_empty)

    def test_nonempty_extra_changes_hash(self) -> None:
        base = shot_fingerprint("p", ["a"], [], {"unet": "u"}, 124, 7)
        self.assertNotEqual(base, shot_fingerprint("p", ["a"], [], {"unet": "u"}, 124, 7,
                                                   extra="abc123"))


class TimelineTest(unittest.TestCase):
    def test_timeline_shape(self) -> None:
        ch = [_s("a", 5, ["x"]), _s("b", 5, ["x"])]
        segs = C.geometry(ch, 22)
        tl = C.build_timeline([{"id": "p0", "file": "char_x.png", "name": "char_x.png"}],
                             segs, ["P1", "P2"])
        self.assertEqual(tl["segmentConfig"]["mode"], "timeline")
        self.assertEqual(tl["segmentConfig"]["count"], 2)
        self.assertEqual([s["startFrame"] for s in tl["segmentConfig"]["segments"]], [0, 102])
        self.assertEqual([s["endFrame"] for s in tl["segmentConfig"]["segments"]], [124, 243])
        self.assertFalse(tl["videoAudioEnabled"], "音轨由我们混，不交给模型")
        self.assertEqual(tl["audios"], [])
        self.assertFalse(tl["secondPass"])

    def test_continuity_note_mentions_overlap_seconds(self) -> None:
        note = C.continuity_note(22)
        self.assertIn("00:00.917", note)
        self.assertIn("latent continuation", note)

    def test_workflow_wires_planner_output_2(self) -> None:
        ch = [_s("a", 5, ["x"]), _s("b", 5, ["x"])]
        segs = C.geometry(ch, 22)
        tl = C.build_timeline([], segs, ["P1", "P2"])
        wf = C.build_chain_workflow({"unet": "u", "clip": "c", "vae_video": "vv",
                                     "vae_audio": "va", "steps": 8, "width": 864,
                                     "height": 480, "ref_image_size": "match", "lora": ""},
                                    tl, segs, tick_seed=42)
        self.assertIn("MiniMaxH3TimelinePlanner", wf["8"]["class_type"])
        self.assertEqual(wf["9"]["inputs"]["finite_plan"], ["8", 2], "finite_plan 取 planner 第 3 个输出")
        self.assertEqual(wf["9"]["inputs"]["seed"], 42)
        self.assertEqual(wf["9"]["inputs"]["continue_audio_latent"], False)
        self.assertIn("MiniMaxH3FiniteSegmentSampler", wf["9"]["class_type"])
        self.assertEqual(wf["11"]["class_type"], "SaveVideo")


class VoiceTest(unittest.TestCase):
    def test_fingerprint_tracks_text_and_speaker(self) -> None:
        cfg = V.VoiceConfig()
        a = V.voice_fingerprint("甲", "孙悟空", "d", cfg)
        self.assertNotEqual(a, V.voice_fingerprint("乙", "孙悟空", "d", cfg))
        self.assertNotEqual(a, V.voice_fingerprint("甲", "唐僧", "d", cfg))
        self.assertNotEqual(a, V.voice_fingerprint("甲", "孙悟空", "d2", cfg))
        cfg2 = V.VoiceConfig(steps=30)
        self.assertNotEqual(a, V.voice_fingerprint("甲", "孙悟空", "d", cfg2))
        self.assertEqual(a, V.voice_fingerprint("甲", "孙悟空", "d", V.VoiceConfig()))

    def test_description_priority(self) -> None:
        cfg = V.VoiceConfig(voice_overrides={"孙悟空": "覆盖音色"})
        self.assertEqual(V.voice_description("孙悟空", "猴脸", cfg), "覆盖音色")
        self.assertIn("清瘦僧人", V.voice_description("唐僧", "清瘦僧人", cfg))
        self.assertIn("Chinese adult speaker", V.voice_description("路人", "", cfg))

    def test_render_seconds_floor(self) -> None:
        wf = V.build_voice_workflow("你好", "孙悟空", "d", V.VoiceConfig(),
                                    {"unet": "u", "clip": "c", "vae_video": "v",
                                     "vae_audio": "a"}, 1.0)
        self.assertGreaterEqual(wf["7"]["inputs"]["render_seconds"], V.MIN_RENDER_SECONDS)

    def test_speaker_id_keeps_cjk(self) -> None:
        wf = V.build_voice_workflow("你好", "孙悟空", "d", V.VoiceConfig(),
                                    {"unet": "u", "clip": "c", "vae_video": "v",
                                     "vae_audio": "a"}, 5.0)
        self.assertEqual(wf["5"]["inputs"]["speaker_id"], "孙悟空")


class AudioTrackTest(unittest.TestCase):
    """无台词必须出**静音轨**（绝不让模型自己编台词 —— 那正是"嘴说话对不上"的根源）。"""

    def test_silent_track_when_no_voice(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "a.wav"
            got = C.build_audio_track([None, None], [0, 124], 248, out, log=QUIET)
            self.assertEqual(got, out)
            self.assertTrue(out.is_file())
            self.assertAlmostEqual(C.probe_duration(out), 248 / 24, places=1)

    def test_mix_places_voice_at_new_content_start(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tone = Path(td) / "t.wav"
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                            "sine=frequency=440:duration=1", "-ar", "32000", "-ac", "2",
                            str(tone)], check=True)
            out = Path(td) / "a.wav"
            C.build_audio_track([tone], [24], 48, out, log=QUIET)
            self.assertAlmostEqual(C.probe_duration(out), 2.0, places=1)

    def test_slice_shot_keeps_exact_frame_count(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src.mp4"
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                            "-i", "testsrc=size=64x48:rate=24:duration=2",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)], check=True)
            out = C.slice_shot(src, 0, 12, Path(td) / "cut.mp4")
            r = subprocess.run(["ffprobe", "-v", "error", "-count_frames",
                                "-select_streams", "v:0", "-show_entries",
                                "stream=nb_read_frames", "-of", "default=nw=1:nk=1", str(out)],
                               capture_output=True, text=True)
            self.assertEqual(int(r.stdout.strip()), 12)

    def test_slice_trims_audio_to_same_length(self) -> None:
        """
        ★ 回归（2026-09-28 实测踩到）：只限 `-frames:v` 时音频会继续写到下一个
        AAC 帧边界，每镜多 0.57~0.59s → `format=duration` 偏大 → 拼片/字幕逐镜漂移。
        """
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src.mp4"
            subprocess.run(["ffmpeg", "-y", "-v", "error",
                            "-f", "lavfi", "-i", "testsrc=size=64x48:rate=24:duration=2",
                            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                            "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                            "-c:a", "aac", str(src)], check=True)
            out = C.slice_shot(src, 0, 12, Path(td) / "cut.mp4")
            self.assertAlmostEqual(C.video_duration(out), 0.5, places=2)
            self.assertAlmostEqual(C.probe_duration(out), 0.5, places=1,
                                   msg="音频没跟视频一起裁 → sec_actual 会偏大")


if __name__ == "__main__":
    unittest.main()
