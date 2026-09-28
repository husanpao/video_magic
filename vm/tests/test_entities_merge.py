"""
跨章实体登记（P0.2）的回归门禁。

锁的是一个**线上已经发生的真实故障**，不是假想：
`_extract_scenes()` 只看本章正文、`save_scenes()` 整表覆写，于是跑第二章就把第一章
抽的场景全部冲掉；而 `id` 每章都从 S1 重新编号，同一个 id 在两章指向两个不同地点。

实测证据（`projects/雨夜地铁`，本项目唯一跑通全流程的样片）：
  · `scenes.json` 里的 S1 = 「站台」（第二章的地点）
  · 而 `shots/chapter01.json` 的 14 镜引用 `scene_id: "S1"`
  → 第一章那 14 镜的场景锚定文字已被静默替换成另一个地方。

为什么必须钉死**「id 一旦被引用就绝不重排」**：`scene_id` 是存在镜头表里的外键
（`shots.py:137`，逐字注入靠它）。重排的后果是已渲的镜头指向另一个地点，
而画面照样正常生成、质检照样通过 —— 属于最难发现的一类错位。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from vm import plan as P
from vm.state import Project


def _scene(sid: str, name: str, desc: str = "A place, some visual detail here") -> P.SceneCard:
    return P.SceneCard(id=sid, name=name, location="somewhere", time_of_day="night",
                       lighting="dim", atmosphere="quiet", description=desc)


def _prop(pid: str, name: str, desc: str = "An object, material and wear described") -> P.PropCard:
    return P.PropCard(id=pid, name=name, owner="", description=desc)


class TestNoCrossChapterOverwrite(unittest.TestCase):
    """P0.2 的第一性要求：第二章不许冲掉第一章。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.proj = Project(Path(self._td.name) / "p")
        self.proj.root.mkdir(parents=True)

    def test_chapter_two_keeps_chapter_one_entities(self):
        one, _ = P.merge_scenes(self.proj, [_scene("S1", "末班地铁车厢"),
                                            _scene("S2", "车厢连接处")], 1)
        P.save_scenes(self.proj, one)
        two, rep2 = P.merge_scenes(self.proj, [_scene("S1", "站台"),
                                               _scene("S2", "下行台阶")], 2)
        P.save_scenes(self.proj, two, rep2["next_id"])

        names = {c.name for c in two}
        self.assertIn("末班地铁车厢", names, "第一章的场景被第二章冲掉了")
        self.assertIn("车厢连接处", names)
        self.assertIn("站台", names)
        self.assertEqual(4, len(two))

    def test_reused_id_becomes_new_id_and_is_reported(self):
        """两章各自从 S1 编号 —— 撞号时**不覆盖已登记的**，另发新号并报冲突。"""
        P.save_scenes(self.proj, P.merge_scenes(self.proj, [_scene("S1", "甲地")], 1)[0])
        merged, rep = P.merge_scenes(self.proj, [_scene("S1", "乙地")], 2)
        self.assertEqual(1, len(rep["conflicts"]))
        self.assertIn("未覆盖", rep["conflicts"][0])
        got = [c for c in merged if c.name == "乙地"][0]
        self.assertNotEqual("S1", got.id)
        # 原 S1 的名字与描述一个字都不能动
        self.assertEqual("甲地", merged[0].name)

    def test_same_place_in_two_chapters_merges_instead_of_duplicating(self):
        one, _ = P.merge_scenes(self.proj, [_scene("S1", "末班地铁车厢")], 1)
        P.save_scenes(self.proj, one)
        merged, rep = P.merge_scenes(
            self.proj, [_scene("S3", "末班地铁车厢")], 2)     # 本章把它编成了 S3
        self.assertEqual(1, len(merged), "同一地点被建成了两条")
        self.assertEqual([1, 2], merged[0].chapters)
        self.assertTrue(any("归并" in m for m in rep["matched"]))

    def test_authoritative_description_is_not_silently_rewritten(self):
        """
        已登记的 description 已经逐字注入过前面的镜头了 —— 后一章抽出不同文字时
        只能**沿用旧的并告警**，静默改写会让"同一个 S1 在不同章长得不一样"。
        """
        one, _ = P.merge_scenes(self.proj, [_scene("S1", "站台", "TILED FLOOR, v1")], 1)
        P.save_scenes(self.proj, one)
        merged, rep = P.merge_scenes(self.proj, [_scene("S1", "站台", "CONCRETE WALL, v2")], 2)
        self.assertEqual("TILED FLOOR, v1", merged[0].description)
        self.assertEqual(1, len(rep["desc_differs"]))

    def test_re_running_same_chapter_is_idempotent(self):
        a, _ = P.merge_scenes(self.proj, [_scene("S1", "甲地"), _scene("S2", "乙地")], 1)
        P.save_scenes(self.proj, a)
        b, rep = P.merge_scenes(self.proj, [_scene("S1", "甲地"), _scene("S2", "乙地")], 1)
        self.assertEqual([], rep["added"])
        self.assertEqual(2, len(b))
        self.assertEqual([1], b[0].chapters)          # 同一章不重复登记


class TestIdNeverRecycled(unittest.TestCase):
    """id 高水位必须存盘：删掉 S2 之后新场景不能重新拿到 S2。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.proj = Project(Path(self._td.name) / "p")
        self.proj.root.mkdir(parents=True)

    def test_next_id_survives_round_trip(self):
        m, rep = P.merge_scenes(self.proj, [_scene("S1", "甲"), _scene("S2", "乙"),
                                            _scene("S3", "丙")], 1)
        P.save_scenes(self.proj, m, rep["next_id"])
        d = json.loads((self.proj.root / "scenes.json").read_text(encoding="utf-8"))
        self.assertEqual(3, d["next_id"])
        self.assertEqual(3, P.registry_next_id(self.proj, "scenes.json"))

    def test_deleted_entity_frees_its_name_not_its_id(self):
        m, rep = P.merge_scenes(self.proj, [_scene("S1", "甲"), _scene("S2", "乙")], 1)
        P.save_scenes(self.proj, m, rep["next_id"])
        kept = [c for c in m if c.id != "S2"]
        P.save_scenes(self.proj, kept, P.registry_next_id(self.proj, "scenes.json"))
        # 第 3 章来了个新地点，且 LLM 没给 id（走自动分配）
        merged, _ = P.merge_scenes(self.proj, [_scene("", "丙")], 3)
        new = [c for c in merged if c.name == "丙"][0]
        self.assertEqual("S3", new.id, f"回收了已被镜头引用过的 id：{new.id}")

    def test_props_follow_the_same_rule(self):
        m, rep = P.merge_props(self.proj, [_prop("P1", "铜灯"), _prop("P2", "车票")], 1)
        P.save_props(self.proj, m, rep["next_id"])
        m2, rep2 = P.merge_props(self.proj, [_prop("P1", "金箍棒")], 2)
        self.assertEqual(1, len(rep2["conflicts"]))
        names = {c.name for c in m2}
        self.assertEqual({"铜灯", "车票", "金箍棒"}, names)


class TestLegacyFilesStillRead(unittest.TestCase):
    """v1（没有 chapters 字段）的文件必须照读，且不能被当成"0 章"。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.proj = Project(Path(self._td.name) / "p")
        self.proj.root.mkdir(parents=True)

    def test_v1_scene_file_loads_with_unknown_chapters(self):
        (self.proj.root / "scenes.json").write_text(json.dumps({
            "version": 1, "scenes": [{"id": "S1", "name": "古寺山门", "location": "gate",
                                      "time_of_day": "dusk", "lighting": "dim",
                                      "atmosphere": "old", "description": "A gate."}]
        }, ensure_ascii=False), encoding="utf-8")
        sc = P.load_scenes(self.proj)
        self.assertEqual([], sc["S1"].chapters)
        self.assertEqual(0, P.registry_next_id(self.proj, "scenes.json"))
        # 在 v1 之上继续 merge，不能把 S1 的号抢走
        merged, _ = P.merge_scenes(self.proj, [_scene("S1", "别的地点")], 2)
        self.assertEqual("古寺山门", merged[0].name)
        self.assertEqual("S2", merged[-1].id)

    def test_corrupt_file_reads_as_empty_not_crash(self):
        (self.proj.root / "scenes.json").write_text('{"scenes": [{"id"', encoding="utf-8")
        self.assertEqual({}, P.load_scenes(self.proj))
        self.assertEqual(0, P.registry_next_id(self.proj, "scenes.json"))


class TestExtractionSeesTheRegistry(unittest.TestCase):
    """抽取提示词里必须带上已登记实体，否则 LLM 每章都从 S1 重编 —— 那是撞号的根源。"""

    def _user_prompt(self, known, fn):
        captured = {}

        def fake_llm(cfg, system, user, **kw):
            captured["user"] = user
            return {"scenes": [], "props": []}, {}

        orig = P._llm_json
        P._llm_json = fake_llm
        try:
            fn({}, "正文" * 50, lambda m: None, {}, known=known)
        finally:
            P._llm_json = orig
        return captured.get("user", "")

    def test_scene_prompt_lists_registered_entities(self):
        user = self._user_prompt([_scene("S1", "末班地铁车厢"), _scene("S2", "站台")],
                                 P._extract_scenes)
        self.assertIn("S1 末班地铁车厢", user)
        self.assertIn("S2 站台", user)
        self.assertIn("沿用", user)

    def test_prop_prompt_lists_registered_props(self):
        user = self._user_prompt([_prop("P1", "铜灯")], P._extract_props)
        self.assertIn("P1 铜灯", user)

    def test_first_chapter_gets_no_registry_block(self):
        user = self._user_prompt([], P._extract_scenes)
        self.assertNotIn("已登记实体总表", user)


if __name__ == "__main__":
    unittest.main()
