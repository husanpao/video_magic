"""
test_media_url_contract.py —— 「接口给出的地址必须真能用」的回归锁（2026-09-28 事故）。

事故：`/api/status` 的 `finals[].url` 还是旧形态 `/view?...&kind=final&file=EP01.mp4`，
而同一版 `/view` 只认 `final=1&episode=EP01` → **谁信这个字段谁吃 400**。
前端自己拼地址所以"看起来没事"（播放器照常播），但外部脚本、书签、下载链接、
以及任何按契约使用该字段的调用方，都要等到点下去那一刻才发现是坏的。

两条锁：
  1. 生成侧：`episodes()[].url` 必须是 `/view` 现在认的形态，且 project 名要百分号编码；
  2. 兼容侧：`/view` 仍接受旧形态（老书签/老脚本不能因为一次重构就失效）。
"""

from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path

from vm import taskctl
from vm import web as W


class _StubProject:
    """`episodes()` 只用到 root / final_dir 两个属性 —— 不为它拖起整套 Project。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.final_dir = root / "final"


class TestEpisodesUrlShape(unittest.TestCase):
    def _one(self) -> dict:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "final").mkdir()
            (root / "final" / "EP01.mp4").write_bytes(b"\x00" * 16)
            (root / "final" / "EP01.srt").write_text("1\n", encoding="utf-8")
            eps = taskctl.episodes(_StubProject(root))
            self.assertEqual(len(eps), 1, "应在 final/ 下发现一集成片")
            return eps[0]

    def test_url_uses_current_view_shape(self) -> None:
        u = self._one()["url"]
        self.assertIn("final=1&episode=EP01", u, f"地址形态必须与 /view 一致：{u}")
        self.assertNotIn("kind=final", u, "旧形态在新 /view 里会 400，不得再生成")

    def test_project_name_is_percent_encoded(self) -> None:
        # 本项目的项目名是中文（还可能带全角逗号）—— 直接拼进 URL 会在复制/转发时断掉
        u = self._one()["url"]
        path_part = u.split("project=", 1)[1].split("&", 1)[0]
        self.assertTrue(path_part.isascii(), f"project 名必须被编码：{path_part}")
        self.assertNotIn(" ", path_part)

    def test_srt_is_reported(self) -> None:
        self._one()  # 存在性由 _one 内部断言
        e = self._one()
        self.assertEqual(e["srt"], "EP01.srt")


class TestViewAcceptsLegacyForm(unittest.TestCase):
    """旧形态必须继续被接受 —— 重构不该让用户的书签/脚本失效。"""

    def setUp(self) -> None:
        self.src = Path(W.__file__).read_text(encoding="utf-8")

    def test_legacy_kind_final_maps_to_final_episode(self) -> None:
        self.assertIn('q.get("kind") == "final"', self.src,
                      "/view 必须兼容 kind=final&file=EP01.mp4（2026-09-28 之前对外给的形态）")
        self.assertIn('"episode": Path(str(q["file"])).stem', self.src,
                      "旧形态的 file 要去掉 .mp4 后缀映射成 episode")

    def test_handler_still_enforces_episode_name(self) -> None:
        # 兼容不能放松校验：集名仍要过 SHOT_RE（拒绝 ../ 之类）
        self.assertIn("bad_episode", self.src)


if __name__ == "__main__":
    unittest.main()
