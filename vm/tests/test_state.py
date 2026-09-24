"""
状态层（vm/state.py）的单测：指纹 / 三态 / 锁定位 / 渲染检查点。

为什么是最高优先级：指纹是「改了输入只重做相关镜头」的全部依据。
指纹漏一个输入 → 改了提示词却静默复用旧片（文档里点名的反面教材）；
指纹掺一个无关项 → 改端口导致全部镜头无谓重渲。
三态判错 → 要么白烧 GPU（把 current 判 stale），要么出废片（把 stale 判 current）。
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from vm.state import (
    CURRENT,
    MISSING,
    STALE,
    Checkpoint,
    Manifest,
    shot_fingerprint,
)

RENDER = {"unet": "u1", "lora": "l1", "steps": 8, "width": 864, "height": 480}


class TestFingerprint(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.td = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self.ref = self.td / "char_唐僧.png"
        self.ref.write_bytes(b"png-bytes-0123456789")

    def fp(self, prompt="p", chars=("唐僧",), refs=(None,), render=None, frames=124, seed=7):
        ref_paths = [self.ref if r is None else Path(r) for r in refs]
        return shot_fingerprint(prompt, chars, ref_paths, render or RENDER, frames, seed)

    def test_deterministic(self):
        self.assertEqual(self.fp(), self.fp(), "同输入必须同指纹（可复现是断点续跑的前提）")

    def test_each_render_input_changes_fingerprint(self):
        base = self.fp()
        for key, val in (("unet", "u2"), ("lora", "l2"), ("steps", 9),
                         ("width", 1024), ("height", 576)):
            self.assertNotEqual(base, self.fp(render=dict(RENDER, **{key: val})),
                                f"改 {key} 指纹必须变，否则该参数的改动会被静默忽略")
        self.assertNotEqual(base, self.fp(frames=141), "改帧数指纹必须变")
        self.assertNotEqual(base, self.fp(seed=8), "改种子指纹必须变")
        self.assertNotEqual(base, self.fp(prompt="P"), "改提示词指纹必须变")

    def test_chars_change_and_order_do_not_matter(self):
        base = self.fp(chars=("唐僧", "孙悟空"))
        self.assertEqual(base, self.fp(chars=("孙悟空", "唐僧")), "角色是集合语义，顺序无关")
        self.assertNotEqual(base, self.fp(chars=("唐僧",)), "少一个角色必须变指纹")
        self.assertNotEqual(base, self.fp(chars=("唐僧", "沙僧")), "换角色必须变指纹")

    def test_ref_file_change_is_detected_by_stamp(self):
        """参考图用 mtime:size 戳 —— 换图/重生成/覆盖都要能认出来。"""
        base = self.fp(refs=(None,))
        self.ref.write_bytes(b"png-bytes-DIFFERENT!!")  # size 变
        os.utime(self.ref, ns=(1, 1))
        self.assertNotEqual(base, self.fp(refs=(None,)), "参考图变了指纹必须变")

    def test_missing_ref_is_stable_and_marked(self):
        gone = self.td / "nope.png"
        self.assertEqual(self.fp(refs=(gone,)), self.fp(refs=(gone,)), "缺失参考图也要可复现")

    def test_irrelevant_render_keys_are_ignored(self):
        """端口/URL 之类不进指纹 —— 改 ComfyUI 地址不该让全片重渲。"""
        base = self.fp()
        self.assertEqual(base, self.fp(render=dict(RENDER, comfy_url="http://127.0.0.1:9999")),
                         "comfy_url 不该进指纹")


class TestManifestThreeStates(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.td = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self.clip = self.td / "1-01-01.mp4"
        self.m = Manifest.load(self.td / "manifest.json")

    def test_missing_stale_current(self):
        # missing：产物文件不存在（或 0 字节）
        self.assertEqual(self.m.status("1-01-01", "fp1", self.clip), MISSING)
        self.clip.write_bytes(b"")
        self.assertEqual(self.m.status("1-01-01", "fp1", self.clip), MISSING, "0 字节=没有产物")

        # stale：有产物但没有指纹记录（不可信）
        self.clip.write_bytes(b"video-data")
        self.assertEqual(self.m.status("1-01-01", "fp1", self.clip), STALE, "无指纹记录不可信")

        # current：指纹一致 + 大小一致
        self.m.mark("1-01-01", "fp1", self.clip)
        self.assertEqual(self.m.status("1-01-01", "fp1", self.clip), CURRENT)

        # stale：指纹变了（改了输入）
        self.assertEqual(self.m.status("1-01-01", "fp2", self.clip), STALE, "指纹变→重渲")

        # stale：产物被截断/替换（记录里的大小对不上）
        self.clip.write_bytes(b"video")   # size 从 10 变 5
        self.assertEqual(self.m.status("1-01-01", "fp1", self.clip), STALE, "产物被改过")

    def test_mark_preserves_lock_and_selection(self):
        """重渲不能把锁定/选中/收藏清掉 —— 否则"锁了再渲一次就自动解锁"，锁形同虚设。"""
        self.clip.write_bytes(b"video-data")
        self.m.mark("1-01-01", "fp1", self.clip)
        self.assertTrue(self.m.set_flag("1-01-01", "locked", True, by="tester"))
        self.assertTrue(self.m.set_flag("1-01-01", "favorite", True))
        self.m.mark("1-01-01", "fp2", self.clip)   # 重新渲染
        e = self.m.shots["1-01-01"]
        self.assertTrue(e.locked, "重渲后锁定必须保留")
        self.assertEqual(e.locked_by, "tester", "锁定操作者要可追溯")
        self.assertTrue(e.favorite)
        self.assertEqual(e.fp, "fp2", "指纹要更新到新输入")

    def test_set_flag_semantics(self):
        self.clip.write_bytes(b"video-data")
        self.m.mark("1-01-01", "fp1", self.clip)
        self.assertFalse(self.m.set_flag("9-99-01", "locked", True), "不在清单里应返回 False")
        with self.assertRaises(ValueError):
            self.m.set_flag("1-01-01", "whatever", True)
        self.assertTrue(self.m.set_flag("1-01-01", "locked", True, by="qa"))
        self.assertTrue(self.m.is_locked("1-01-01"))
        self.assertTrue(self.m.set_flag("1-01-01", "locked", False))
        self.assertFalse(self.m.is_locked("1-01-01"))

    def test_save_load_round_trip(self):
        self.clip.write_bytes(b"video-data")
        self.m.mark("1-01-01", "fp1", self.clip, qc="pass", sec_actual=5.2)
        self.m.set_flag("1-01-01", "selected", True)
        self.m.save()
        m2 = Manifest.load(self.td / "manifest.json")
        self.assertEqual(m2.status("1-01-01", "fp1", self.clip), CURRENT)
        self.assertEqual(m2.shots["1-01-01"].sec_actual, 5.2)
        self.assertTrue(m2.shots["1-01-01"].selected)

    def test_corrupt_manifest_moves_aside_not_silently_lost(self):
        """坏 manifest 挪到 .corrupt 留证再空表继续 —— 静默重建会把历史指纹全丢光。"""
        p = self.td / "manifest.json"
        p.write_text("{不是 json", encoding="utf-8")
        m = Manifest.load(p)
        self.assertEqual(m.shots, {})
        self.assertTrue(p.with_suffix(".json.corrupt").exists(), "坏文件要留证")


class TestCheckpoint(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.td = Path(self._td.name)
        self.addCleanup(self._td.cleanup)

    def test_set_get_age_clear_in_memory(self):
        ck = Checkpoint.load(self.td / "render_ck.json")
        ck.set("1-01-01", "pid-abc", at=1000)
        self.assertEqual(ck.get("1-01-01"), "pid-abc")
        self.assertGreater(ck.age("1-01-01"), 0, "提交时间要在，崩溃恢复靠它判宽限期")
        ck.clear("1-01-01")
        self.assertIsNone(ck.get("1-01-01"))

    def test_save_load_preserves_pid_and_timestamp(self):
        """
        save→load 回读必须保真 prompt_id 与提交时间。
        （曾为已知 bug：Checkpoint.load 把 dict 值 str() 化 → get() 回读整个字典字符串、
        age() 恒 inf → 崩溃恢复去 /history 回收必扑空 → 误判丢失重复提交烧 GPU。
        已修，本用例转正为回归锁。）
        """
        ck = Checkpoint.load(self.td / "render_ck.json")
        ck.set("1-01-01", "pid-abc", at=int(time.time()) - 5)
        ck.save()
        ck2 = Checkpoint.load(self.td / "render_ck.json")
        self.assertEqual(ck2.get("1-01-01"), "pid-abc")
        self.assertLess(ck2.age("1-01-01"), 60, "age 必须按 at 算，不能恒 inf")

    def test_old_string_format_still_readable(self):
        """旧格式（直接存字符串）也要能读 —— 兼容老项目；age 视为过宽限期（inf）。"""
        (self.td / "render_ck.json").write_text('{"shots": {"1-01-01": "pid-old"}}', encoding="utf-8")
        ck = Checkpoint.load(self.td / "render_ck.json")
        self.assertEqual(ck.get("1-01-01"), "pid-old")
        self.assertEqual(ck.age("1-01-01"), float("inf"), "旧格式无时间戳→视为已过宽限期")


if __name__ == "__main__":
    unittest.main()
