"""
`vm/fsutil.py` 的回归门禁（P0.0 · 原子写收敛）。

锁的是**收敛这件事本身**，而不是 fsutil 的实现细节。三种漂移在收敛前真实存在过，
所以每条都拿一个"旧写法"当参照物去比对：

  · **排版漂移** —— 同一个 `_atomic_write_json` 有 6 份副本，5 份 `sort_keys=True`、
    `costumes.py` 那份不排序；内联那 6 处带行尾换行、函数版不带。
    收敛时若"顺手统一"，现存 21 个文件的字节就全变了 —— 那本次重构就没法验收。
    → 逐字节比对钉住：**参数传对，输出必须和旧写法一字不差**。
  · **落盘漂移** —— 内联那 6 处没有 `fsync`，其中两处写的是**用户上传的图**（丢了拿不回来）。
    → 用"不存在的深层目录"做探针：漏 `mkdir` 会 FileNotFoundError，
      那正是 `chars.py:85` 记录过的事故。
  · **残骸漂移** —— 失败时留不留 `.tmp` 不一致。`shots.load_shots_dir` 显式跳过 `.tmp`，
    而队列/候选图是按目录数出数的，残留会被数进去。
    → 钉住"失败不留 .tmp"。

断言的是"落盘出去的东西对不对"，不是"某个函数被调用过"——本模块唯一的价值就是前者。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from vm import fsutil

DOC = {"z": 1, "a": {"中文": "值", "n": [1, 2, 3]}, "m": 2}


class TestMatchesLegacyByteForByte(unittest.TestCase):
    """收敛不许改变任何现存文件的字节。"""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def _legacy_json(self, path: Path, obj, *, sort_keys: bool) -> bytes:
        """收敛前"函数版"的写法（fsync + json.dump，无行尾换行）。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=sort_keys)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return path.read_bytes()

    def test_sorted_no_newline_matches_five_copies(self):
        """state / taskctl / config / storyboard / qc 那 5 份：sort_keys=True、无行尾换行。"""
        want = self._legacy_json(self.d / "a.json", DOC, sort_keys=True)
        got = fsutil.write_json(self.d / "b.json", DOC,
                                sort_keys=True, trailing_newline=False)
        self.assertEqual(want, got.read_bytes())

    def test_costumes_style_keeps_insertion_order(self):
        """costumes.py 那份**不排序** —— 收敛时最容易被顺手加上 sort_keys 的一处。"""
        want = self._legacy_json(self.d / "a.json", DOC, sort_keys=False)
        got = fsutil.write_json(self.d / "b.json", DOC, trailing_newline=False)
        self.assertEqual(want, got.read_bytes())
        second_line = got.read_text(encoding="utf-8").splitlines()[1]
        self.assertIn('"z"', second_line)          # z 仍是第一个键

    def test_inline_family_matches(self):
        """plan 的 scenes/props、queue、script、assets、budget 那 6 处内联：带行尾换行。
        唯一允许的差异是多出来的 fsync —— 它不改变文件内容。"""
        p = self.d / "sub" / "c.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(DOC, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        want = p.read_bytes()
        got = fsutil.write_json(self.d / "sub" / "d.json", DOC)
        self.assertEqual(want, got.read_bytes())


class TestWriteSequenceIsUniform(unittest.TestCase):
    """所有写路径走同一序列 —— 漏 mkdir 就是 chars.py:85 那次事故。"""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def test_json_creates_missing_deep_parents(self):
        p = self.d / "a" / "b" / "c" / "manifest.json"
        fsutil.write_json(p, DOC)
        self.assertTrue(p.is_file())

    def test_text_creates_missing_deep_parents(self):
        p = self.d / "x" / "y" / "char_唐僧.txt"
        fsutil.write_text(p, "integrated_multimodal_description: …")
        self.assertEqual("integrated_multimodal_description: …",
                         p.read_text(encoding="utf-8"))

    def test_bytes_creates_missing_deep_parents(self):
        p = self.d / "u" / "v" / "upload.png"
        fsutil.write_bytes(p, b"\x89PNG\r\n\x1a\n")
        self.assertEqual(b"\x89PNG\r\n\x1a\n", p.read_bytes())

    def test_tmp_file_does_not_linger(self):
        """`.json.tmp` 约定不变 —— `load_shots_dir` 靠跳过 `.tmp` 工作。"""
        p = self.d / "s.json"
        fsutil.write_json(p, DOC)
        self.assertFalse((self.d / "s.json.tmp").exists())

    def test_text_writes_no_carriage_return(self):
        """字幕/concat 清单原先显式 `newline="\\n"`；fsutil 走字节写，天然不转换。"""
        p = self.d / "sub.srt"
        fsutil.write_text(p, "1\n00:00:00,000 --> 00:00:02,000\n台词\n\n")
        self.assertNotIn(b"\r", p.read_bytes())


class TestNoDebrisOnFailure(unittest.TestCase):
    """失败不能留 `.tmp`：队列与候选图列表按目录计数，残留会被数成产物。"""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def test_unserializable_json_leaves_no_tmp(self):
        with self.assertRaises(TypeError):
            fsutil.write_json(self.d / "bad.json", {"o": object()})
        self.assertEqual([], [q.name for q in self.d.iterdir()
                              if q.name.endswith(".tmp")])
        self.assertFalse((self.d / "bad.json").exists())

    def test_failed_write_does_not_clobber_existing_target(self):
        """原子性的全部意义：写坏的东西不能碰已经好的那份。"""
        p = self.d / "state.json"
        fsutil.write_json(p, {"good": True})
        with self.assertRaises(TypeError):
            fsutil.write_json(p, {"boom": object()})
        self.assertEqual({"good": True}, json.loads(p.read_text(encoding="utf-8")))


class TestReadJsonIsTolerient(unittest.TestCase):
    """只读入口：坏文件当"没有"，不要让一个坏产物炸掉整张表的展示。"""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def test_missing_file_returns_default(self):
        self.assertEqual({}, fsutil.read_json(self.d / "nope.json", {}))
        self.assertIsNone(fsutil.read_json(self.d / "nope.json"))

    def test_truncated_file_returns_default_instead_of_raising(self):
        p = self.d / "queue.json"
        p.write_text('{"version": 1, "jobs": [{"id"', encoding="utf-8")     # 半截
        self.assertEqual({}, fsutil.read_json(p, {}))

    def test_undecodable_bytes_return_default(self):
        p = self.d / "task.json"
        p.write_bytes(b"\xff\xfe\x00broken")
        self.assertEqual({}, fsutil.read_json(p, {}))

    def test_round_trip(self):
        p = fsutil.write_json(self.d / "ok.json", DOC)
        self.assertEqual(DOC, fsutil.read_json(p))


if __name__ == "__main__":
    unittest.main()
