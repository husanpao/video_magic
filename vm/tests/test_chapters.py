"""
章节清单（vm/chapters.py）与单章增量拆镜（P0.1 / P0.3）的回归门禁。

两条线锁的东西不同：

**P0.1 锁"顺序与章号"。** 收敛前没有任何地方存着章的顺序，每次现算
`sorted(novel_dir.glob("*.md"))` —— 按 Unicode 码位排中文文件名，实测《第一章/第二章/第三章》
排成「一,三,二」。这个排序同时被三处当真相（`stage_plan` 的"第 N 章"、
`chapter_table.index`、`plan.chapter_number` 的文件名兜底），所以它不是显示问题。
下面的 `test_chinese_numerals_are_not_sorted_by_codepoint` 就是那条 bug 的直接反证。

**P0.3 锁"参数到得到 worker"。** `--chapter` 在 `pipeline` 里定义了、worker 也认，
但 `_build_cmd` 从不拼它、`run_foreground` 不传它、Web 的 `_start` 不收它 ——
三条路各自断在一个不同的地方。这类"每段代码都对、连起来不通"的缺陷，
只有从**入口一路断到命令行**才测得出来，所以这里三处都断。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from vm import chapters as Ch
from vm import taskctl
from vm.state import Project


def _mk_project(root: Path, name: str, chapters: list[str]) -> Path:
    pdir = root / name
    for sub in ("novel", "shots", "refs", "prompts", "clips", "final", "state"):
        (pdir / sub).mkdir(parents=True, exist_ok=True)
    for stem in chapters:
        (pdir / "novel" / f"{stem}.md").write_text(
            f"# {stem}\n\n正文内容。\n" * 3, encoding="utf-8")
    return pdir


CN_ORDER = ["第一章_甲", "第二章_乙", "第三章_丙", "第四章_丁",
            "第五章_戊", "第六章_己", "第七章_庚", "第八章_辛",
            "第九章_壬", "第十章_癸", "第十一章_子", "第十二章_丑"]


class TestChapterNumberParsing(unittest.TestCase):
    def test_chinese_numerals(self):
        cases = {"一": 1, "二": 2, "两": 2, "十": 10, "十一": 11, "二十": 20,
                 "二十三": 23, "一百零八": 108, "3": 3, "007": 7}
        for s, want in cases.items():
            self.assertEqual(want, Ch.parse_cn_number(s), f"第{s}章")

    def test_unparseable_raises_instead_of_becoming_zero(self):
        """旧 `taskctl._cn_num` 用 `d.get(a, 0)` 兜底 —— 「第萬章」→ 0 →
        退化成排序序号，症状是"某章莫名跑到最前面"。现在必须显式抛。"""
        for bad in ["萬", "", "甲", ""]:
            with self.assertRaises(ValueError):
                Ch.parse_cn_number(bad)

    def test_zhang_hui_jie_all_count(self):
        self.assertEqual(3, Ch.parse_chapter_no("第三章_丙"))
        self.assertEqual(12, Ch.parse_chapter_no("第十二回_唐僧"))
        self.assertEqual(5, Ch.parse_chapter_no("第5节"))
        self.assertIsNone(Ch.parse_chapter_no("序章"))
        self.assertEqual(7, Ch.parse_chapter_no("007_影子"))   # 纯数字前缀


class TestOrderingIsNotCodepoint(unittest.TestCase):
    """P0.1 的全部动机：中文序数文件名不能再按码位排。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.pdir = _mk_project(self.root, "p", CN_ORDER)
        self.proj = Project(self.pdir)

    def test_codepoint_order_is_broken_as_documented(self):
        """化石断言：证明"按码位排序 + 按位置选章"这个旧做法确实是错的。
        不硬背码位顺序（那是 Unicode 表分配的巧合，背下来反而会在换字表时误报），
        只断两件事：整体顺序不对 + 取"第 2 个"取不到第二章。"""
        by_codepoint = sorted(p.stem for p in (self.pdir / "novel").glob("*.md"))
        self.assertNotEqual(CN_ORDER, by_codepoint)
        self.assertEqual("第一章_甲", by_codepoint[0])          # 只有第一章碰巧对
        self.assertNotEqual("第二章_乙", by_codepoint[1])       # 往后全是乱的
        self.assertEqual(CN_ORDER, sorted(by_codepoint, key=lambda n: Ch.parse_chapter_no(n)))

    def test_chinese_numerals_are_not_sorted_by_codepoint(self):
        """同一批文件走新代码 —— 必须完全是另一个（正确的）顺序。"""
        got = [p.stem for p in Ch.list_novel_files(self.proj)]
        self.assertEqual(CN_ORDER, got)

    def test_no_manifest_falls_back_to_parsed_order(self):
        """没有清单的老项目也必须立刻是对的（读路径还不许写盘）。"""
        self.assertFalse(Ch.manifest_path(self.proj).exists())
        self.assertEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
                         [n for n, _p in Ch.plan_targets(self.proj)])
        self.assertFalse(Ch.manifest_path(self.proj).exists())   # 读没顺手写

    def test_selecting_by_number_hits_the_right_file(self):
        """`--chapter 3` 修好后拿到的必须是第三章 —— 旧行为是"码位排序第 3 个文件"。"""
        ch_of = dict((p, n) for n, p in Ch.plan_targets(self.proj))
        hit = [p for p, n in ch_of.items() if n == 3]
        self.assertEqual(1, len(hit))
        self.assertEqual("第三章_丙", hit[0].stem)


class TestManifest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.pdir = _mk_project(self.root, "p", ["第一章_甲", "第二章_乙", "第十章_丙"])
        self.proj = Project(self.pdir)

    def test_round_trip(self):
        Ch.save(self.proj, Ch.rebuild(self.proj))
        entries = Ch.load(self.proj)
        self.assertEqual([1, 2, 10], [e.no for e in entries])
        self.assertTrue(all(e.body_sha and e.chars for e in entries))

    def test_duplicate_chapter_number_is_resolved_not_collided(self):
        """「第二回」和「第二章」都解析成 2 —— 同号会让两个章节写同一个
        `chapter02.json`，**后跑的整表覆盖前者**，而画面照样渲得出来。"""
        (self.pdir / "novel" / "第二回_丁.md").write_text("第二回\n正文", encoding="utf-8")
        Ch.save(self.proj, Ch.rebuild(self.proj))
        nos = [e.no for e in Ch.load(self.proj)]
        self.assertEqual(len(nos), len(set(nos)), f"章号重复：{nos}")
        self.assertEqual([], Ch.find_conflicts(self.proj))

    def test_find_conflicts_reports_duplicate_no(self):
        """手工改坏的清单必须被点名，不能静默。"""
        Ch.save(self.proj, [Ch.ChapterEntry(1, "甲", "novel/第一章_甲.md"),
                            Ch.ChapterEntry(1, "乙", "novel/第二章_乙.md")])
        probs = Ch.find_conflicts(self.proj)
        self.assertEqual(1, len(probs))
        self.assertIn("chapter01.json", probs[0])
        self.assertIn("覆盖", probs[0])

    def test_find_conflicts_reports_missing_body(self):
        Ch.save(self.proj, [Ch.ChapterEntry(1, "甲", "novel/不存在的章.md")])
        self.assertEqual(1, len(Ch.find_conflicts(self.proj)))

    def test_sync_keeps_user_order(self):
        """顺序是用户的东西（P1 给拖拽）。扫描目录这个动作没有资格重排它。"""
        Ch.save(self.proj, Ch.rebuild(self.proj))
        Ch.move(self.proj, 10, before_no=1)
        self.assertEqual([10, 1, 2], [e.no for e in Ch.load(self.proj)])
        (self.pdir / "novel" / "第三章_戊.md").write_text("第三章\n新章", encoding="utf-8")
        r = Ch.sync(self.proj)
        self.assertEqual([10, 1, 2, 3], [c["no"] for c in r["chapters"]])
        self.assertEqual(["novel/第三章_戊.md"], r["added"])

    def test_sync_detects_body_change(self):
        """`changed` 是 P1.5「需重拆」的判定依据。"""
        Ch.save(self.proj, Ch.rebuild(self.proj))
        (self.pdir / "novel" / "第一章_甲.md").write_text("第一章_甲\n重写过的正文", encoding="utf-8")
        r = Ch.sync(self.proj)
        self.assertEqual(["novel/第一章_甲.md"], r["changed"])

    def test_sync_removes_deleted_body(self):
        Ch.save(self.proj, Ch.rebuild(self.proj))
        (self.pdir / "novel" / "第十章_丙.md").unlink()
        r = Ch.sync(self.proj)
        self.assertEqual(["novel/第十章_丙.md"], r["removed"])
        self.assertEqual([1, 2], [c.no for c in Ch.load(self.proj)])

    def test_move_never_renumbers(self):
        """`no` 是镜头表文件名 + 镜头 id 前缀 + 成片集数三处共同的主键，
        改 no = 三处一起错位。所以 move 只准动位置。"""
        Ch.save(self.proj, Ch.rebuild(self.proj))
        before = {e.title: e.no for e in Ch.load(self.proj)}
        self.assertEqual([1, 2, 10], [e.no for e in Ch.load(self.proj)])
        Ch.move(self.proj, 10, before_no=1)          # 把第十章提到最前
        after = {e.title: e.no for e in Ch.load(self.proj)}
        self.assertEqual(before, after)              # ★ 没有任何一章被改号
        self.assertEqual([10, 1, 2], [e.no for e in Ch.load(self.proj)])

    def test_tmp_residue_is_not_a_chapter(self):
        """原子写失败留下的 `.tmp` 不能被当成一章（队列/候选图按目录计数栽过一次）。"""
        (self.pdir / "novel" / "第一章_甲.md.tmp").write_text("半截", encoding="utf-8")
        (self.pdir / "novel" / ".隐藏.md").write_text("隐藏", encoding="utf-8")
        self.assertEqual(3, len(Ch.list_novel_files(self.proj)))

    def test_hidden_dot_and_txt_md_precedence(self):
        (self.pdir / "novel" / "第一章_甲.txt").write_text("重复的 txt 版", encoding="utf-8")
        names = [p.name for p in Ch.list_novel_files(self.proj)]
        self.assertIn("第一章_甲.md", names)
        self.assertIn("第一章_甲.txt", names)
        self.assertLess(names.index("第一章_甲.md"), names.index("第一章_甲.txt"))


class TestChapterTableReadsManifest(unittest.TestCase):
    """`/api/chapters` 的数据源必须与拆镜用的是同一个真相。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self._saved = taskctl.PROJECTS_DIR
        taskctl.PROJECTS_DIR = self.root
        self.addCleanup(setattr, taskctl, "PROJECTS_DIR", self._saved)
        self.pdir = _mk_project(self.root, "p", CN_ORDER[:12])

    def test_index_and_no_agree(self):
        """旧行为：index 是码位位置、no 是文件名解析值 —— 两者分叉，
        界面按 no 筛、按 index 编号，同一章两个身份。"""
        r = taskctl.chapter_table("p")
        for c in r["chapters"]:
            self.assertEqual(c["index"], c["no"],
                             f"index/no 分叉：{c['title']} index={c['index']} no={c['no']}")

    def test_reports_manifest_presence(self):
        self.assertFalse(taskctl.chapter_table("p")["has_manifest"])
        Ch.save(self.pdir, Ch.rebuild(self.pdir))
        r = taskctl.chapter_table("p")
        self.assertTrue(r["has_manifest"])
        self.assertEqual([], r["conflicts"])

    def test_read_path_does_not_write_manifest(self):
        """只读的 /api/chapters 每 2s 被轮询一次；它不该改项目状态。"""
        taskctl.chapter_table("p")
        self.assertFalse(Ch.manifest_path(self.pdir).exists())

    def test_chapter_without_body_still_listed(self):
        """镜头表里有、正文已删 —— 必须仍然出现，否则那 14 镜在界面上凭空消失。"""
        Ch.save(self.pdir, Ch.rebuild(self.pdir))
        (self.pdir / "shots" / "chapter07.json").write_text(
            json.dumps([{"id": "7-1-01", "sec": 5, "chars": ["甲"], "seed": 1,
                         "prompt": "x"}], ensure_ascii=False), encoding="utf-8")
        (self.pdir / "novel" / "第七章_庚.md").unlink()
        nos = [c["no"] for c in taskctl.chapter_table("p")["chapters"]]
        self.assertIn(7, nos)


class TestChapterReachesWorker(unittest.TestCase):
    """P0.3：三处断点各自钉一条。"""

    def test_build_cmd_carries_chapter(self):
        """断点①：`_build_cmd` 从不拼 --chapter（worker 于是永远收到 None）。"""
        cmd = taskctl._build_cmd("p", "plan", only=None, force=False, dry=True,
                                 fake_sleep=0.0, chapter="3")
        self.assertIn("--chapter", cmd)
        self.assertEqual("3", cmd[cmd.index("--chapter") + 1])

    def test_build_cmd_omits_chapter_when_absent(self):
        cmd = taskctl._build_cmd("p", "plan", only=None, force=False, dry=True, fake_sleep=0.0)
        self.assertNotIn("--chapter", cmd)

    def test_argparse_accepts_chapter_for_plan_only(self):
        """断点②：前台 CLI 入口 `run_foreground(...)` 以前不转发 chapter。
        这里锁 `--chapter` 的**语义**：它是章号，不是位置。"""
        import pipeline
        ap = pipeline.build_parser()
        a = ap.parse_args(["p", "--stage", "plan", "--chapter", "3"])
        self.assertEqual("3", a.chapter)
        act = next(s for s in ap._actions if s.dest == "chapter")
        self.assertIn("章号", act.help)
        self.assertIn("不是", act.help)      # 明确写了"不是从前数第 N 个"


class TestStagePlanChapterSelection(unittest.TestCase):
    """`stage_plan` 自己：N 当章号用，取不到要报错而不是静默全跑。"""

    def setUp(self):
        import pipeline
        self.pipeline = pipeline
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.pdir = _mk_project(self.root, "p", CN_ORDER)
        self.proj = Project(self.pdir)
        Ch.save(self.proj, Ch.rebuild(self.proj))

    def _dry(self, chapter_sel):
        res = self.pipeline.stage_plan(self.proj, {}, only=[], force=False, dry=True,
                                       chapter_sel=chapter_sel)
        return [Path(p).stem for p in res["chapters"]]

    def test_dry_run_single_chapter_hits_right_file(self):
        self.assertEqual(["第三章_丙"], self._dry("3"))
        self.assertEqual(["第十章_癸"], self._dry("10"))

    def test_dry_run_all_chapters_is_in_number_order(self):
        got = self._dry(None)
        self.assertEqual(CN_ORDER, got)

    def test_unknown_number_errors(self):
        with self.assertRaises(self.pipeline.taskctl.TaskError):
            self._dry("77")

    def test_non_numeric_errors(self):
        with self.assertRaises(self.pipeline.taskctl.TaskError):
            self._dry("第三章")


if __name__ == "__main__":
    unittest.main()
