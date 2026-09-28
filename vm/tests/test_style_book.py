"""
整本级风格（P2.1/2.2/2.6）、图像指纹（P2.4）与风格后缀（P2.3）的回归门禁。

三件事各自锁的都不是"函数返回对不对"，而是**曾经真实存在的失效模式**：

· **2.6 风格口径**：5 处各写一遍 `params.get("style_preset")`，其中 4 处不回落 `style`，
  而 `DEFAULT_PARAMS` 根本没有 `style_preset` 这个键 ⇒ 老项目（只有手写
  `"style": "anime"`）会**拆镜按 anime 写、抽卡用 realistic 的负向词**，两边静默打架。
· **2.3 风格后缀**：`style.image_suffix` 早就写好了，注释还标着"只有这个字段可以进
  图像模型的提示词"，但**全仓库零调用** ⇒ 换风格对任何一张出图都没有影响。
· **2.4 图指纹**：`gen_char` 的跳过条件是 `if ref.exists()`，`gen_candidates` 是按 seed 跳过
  ⇒ 换风格后重跑「定妆」一张都不重出、旧风格的候选继续被当成"已抽好的图"。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vm import assets as A
from vm import chars as C
from vm import chapters as CH
from vm import imgfp as F
from vm import queue as Q
from vm import register as R
from vm import style as S
from vm.state import Project


class TestPresetSingleVoice(unittest.TestCase):
    """2.6：全仓库只能有一个"当前用哪个预设"的口径。"""

    def test_falls_back_to_style_when_no_preset(self):
        """老项目只有手写 `"style": "anime"` —— 必须被认出来，不能退回 realistic。"""
        self.assertEqual("anime", S.preset_of({"style": "anime"}))
        self.assertEqual("cg", S.preset_of({"style": "3d"}))

    def test_auto_is_not_a_real_style(self):
        """`auto` 是"没定"的意思，不能当成一个预设用。"""
        self.assertEqual("cg", S.preset_of({"style_preset": "auto", "style": "cg"}))
        self.assertEqual(S.DEFAULT_PRESET, S.preset_of({"style_preset": "auto"}))

    def test_explicit_preset_wins_over_style(self):
        self.assertEqual("cg", S.preset_of({"style_preset": "cg", "style": "anime"}))

    def test_negative_and_suffix_agree_with_preset(self):
        """负向词与后缀必须按**同一个**预设取 —— 一个是 anime 一个是 cg 就白修了。"""
        p = {"style": "anime"}
        self.assertIn("cel-shaded", S.suffix_for_params(p))
        self.assertNotIn("cel-shaded", S.suffix_for_params({"style": "realistic"}))
        self.assertEqual(S.negative_for("anime"), S.negative_for_params(p))

    def test_accepts_object_shape_too(self):
        """`storyboard.py` 原来是 `getattr(cfg, "style_preset")` 在对象上取 —— 口径也要支持。"""
        class Cfg:
            style_preset = "anime"
            style = None
        self.assertEqual("anime", S.preset_of(Cfg()))


class TestImageSuffixActuallyReachesPrompt(unittest.TestCase):
    """2.3：`image_suffix` 从死代码变成真的进提示词。"""

    def test_appended_for_each_preset(self):
        base = "A tall man in a grey trench coat, wet hair"
        for k, needle in (("anime", "cel-shaded"), ("cg", "Semi-realistic 3D"),
                          ("realistic", "natural film")):
            out = S.apply_image_suffix(base, {"style_preset": k})
            self.assertTrue(out.startswith(base), "原文不能被改写")
            self.assertIn(needle.split()[0].lower(), out.lower(), f"{k} 的后缀没进去")

    def test_idempotent(self):
        """同一份风格拼两次不能出现两遍（幂等）。"""
        once = S.apply_image_suffix("A monk", {"style": "anime"})
        twice = S.apply_image_suffix(once, {"style": "anime"})
        self.assertEqual(once, twice)
        self.assertEqual(1, twice.count("cel-shaded"))

    def test_portrait_inputs_is_the_single_source(self):
        """★ 指纹与提交必须同源，否则会出现"指纹说变了其实没变"。"""
        prompt, params, seed = "A monk", {"style_preset": "cg"}, 7100
        final, neg, render = C.portrait_inputs(prompt, params, seed)
        self.assertEqual(final, S.apply_image_suffix(prompt, params))
        self.assertEqual(neg, S.negative_for_params(params))
        self.assertTrue(render)

    def test_asset_keeps_composition_discipline_alongside_style(self):
        """
        概念图要**同时**保留"空景/无人"的构图纪律与风格后缀 —— 不能为了加风格挤掉纪律。

        ★ 走真实的 `build_prompt` 路径。上一版直接给 `asset_inputs` 传了裸描述，
        而 `SCENE_SUFFIX` 是 `build_prompt` 在上游拼的 —— 于是断言的是我自己
        没拼进去的东西（测试写错，不是代码错）。
        """
        import tempfile
        from vm import plan as PL
        from vm.state import Project

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "p"
            root.mkdir()
            PL.save_scenes(Project(root), [PL.SceneCard(
                id="S1", name="站台", location="platform", time_of_day="night",
                lighting="dim", atmosphere="quiet",
                description="An empty underground platform, tiled floor")])
            _name, built = A.build_prompt(Project(root), "scene", "S1")
            self.assertIn("COMPLETELY EMPTY", built, "构图纪律本来就该在 build_prompt 里")
            final, _neg, _r = A.asset_inputs(built, {"style_preset": "anime"}, 7100, "scene", "S1")
            self.assertIn("COMPLETELY EMPTY", final)
            self.assertIn("cel-shaded", final)
            self.assertLess(final.index("COMPLETELY EMPTY"), final.index("cel-shaded"),
                            "风格后缀应拼在构图纪律之后，顺序反了会稀释纪律")


class TestImageFingerprint(unittest.TestCase):
    """2.4：风格变了，图必须被认出来是旧的。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.d = Path(self._td.name)

    def test_preset_change_changes_fingerprint(self):
        a = C.portrait_inputs("A monk", {"style_preset": "anime"}, 1)
        b = C.portrait_inputs("A monk", {"style_preset": "cg"}, 1)
        fa = F.fingerprint("portrait", a[0], a[1], a[2], 1)
        fb = F.fingerprint("portrait", b[0], b[1], b[2], 1)
        self.assertNotEqual(fa, fb)

    def test_deterministic_for_same_inputs(self):
        x = C.portrait_inputs("A monk", {"style_preset": "cg"}, 1)
        y = C.portrait_inputs("A monk", {"style_preset": "cg"}, 1)
        self.assertEqual(F.fingerprint("portrait", x[0], x[1], x[2], 1),
                         F.fingerprint("portrait", y[0], y[1], y[2], 1))

    def test_seed_is_part_of_identity(self):
        x = C.portrait_inputs("A monk", {"style_preset": "cg"}, 1)
        self.assertNotEqual(F.fingerprint("portrait", x[0], x[1], x[2], 1),
                            F.fingerprint("portrait", x[0], x[1], x[2], 2))

    def test_untracked_is_never_stale(self):
        """
        ★ 没有指纹记录的图**不判 stale** —— 这条是刻意的。

        老项目的产物与用户上传的自有图都没有指纹；把它们判成 stale，
        "改风格"就等于"全部重抽"，而用户上传的图**根本不该被系统重抽**。
        """
        img = self.d / "char_x.png"
        img.write_bytes(b"\x89PNG")
        self.assertEqual("", F.current(img))
        self.assertFalse(F.is_stale(img, "whatever"))

    def test_recorded_fingerprint_detects_drift(self):
        img = self.d / "seed7100.png"
        img.write_bytes(b"\x89PNG")
        F.write(img, "aaa", kind="portrait", name="x", seed=7100)
        self.assertFalse(F.is_stale(img, "aaa"))
        self.assertTrue(F.is_stale(img, "bbb"))

    def test_no_sidecar_left_when_dropped(self):
        img = self.d / "s.png"
        img.write_bytes(b"x")
        F.write(img, "aaa")
        self.assertTrue(F.fp_path(img).is_file())
        F.drop(img)
        self.assertFalse(F.fp_path(img).is_file())


class TestBookLevelStyle(unittest.TestCase):
    """2.1/2.2：风格是整本一份，且人表态过就不被自动覆盖。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.proj = Project(Path(self._td.name) / "p")
        (self.proj.root / "novel").mkdir(parents=True)
        for i, cn in enumerate(["一", "二", "三"], 1):
            (self.proj.root / "novel" / f"第{cn}章_正文.md").write_text(
                f"第{cn}章\n" + "剑光与山门，云海翻涌。" * 60, encoding="utf-8")
        CH.save(self.proj, CH.rebuild(self.proj))

    def test_no_style_file_means_undecided(self):
        self.assertEqual({}, R.load(self.proj))
        _preset, sentence, meta = R.resolve(self.proj)
        self.assertEqual("preset", meta["source"])
        self.assertTrue(sentence)

    def test_user_confirmation_blocks_auto_overwrite(self):
        """★ 关键守卫：人已表态后，自动推荐不得静默改回去。"""
        R.set_style(self.proj, preset="anime", sentence="cel look", source="user")
        self.assertTrue(R.load(self.proj)["confirmed"])
        doc = R.ensure_style(self.proj, {}, log=lambda m: None)
        self.assertEqual("cel look", doc["sentence"])
        self.assertEqual("user", doc["source"])

    def test_ensure_does_not_write_when_already_decided(self):
        R.set_style(self.proj, preset="cg", sentence="pbr look")
        before = R.path_of(self.proj).read_bytes()
        R.ensure_style(self.proj, {}, log=lambda m: None)
        self.assertEqual(before, R.path_of(self.proj).read_bytes())

    def test_sample_covers_head_mid_and_tail(self):
        """只取开头会漏掉"后半本换了地方"，所以抽样要覆盖头/中/尾。"""
        text, total, used = R.collect_sample(self.proj, max_chars=200)
        self.assertGreater(total, 200)
        self.assertLessEqual(len(text), 400)
        self.assertGreaterEqual(len(used), 2, f"只抽到第 {used} 章")

    def test_impact_separates_images_from_shots(self):
        """
        ★ 影响清单必须**分开**说图和镜头，因为代价差一个数量级：
        图是分钟级；镜头要生效得重跑拆镜 ⇒ 全片重渲（实测 52 镜约 36 分钟 GPU）。
        """
        R.set_style(self.proj, preset="realistic",
                    sentence="muted earthy tones, film grain")
        (self.proj.root / "shots").mkdir(exist_ok=True)
        im = R.style_impact(self.proj, preset="anime")
        self.assertTrue(im["changed"])
        self.assertEqual(0, im["shots"]["auto_stale"],
                         "改风格**不该**谎称镜头会自动失效")
        self.assertEqual("replan", im["shots"]["requires"])
        self.assertIn("不会", im["shots"]["note"])
        self.assertIn("portraits", im["images"])

    def test_impact_reports_untracked_not_as_no_work(self):
        """没指纹的图要单列 `untracked`，报成 `stale: 0` 是虚假安心。"""
        (self.proj.root / "prompts").mkdir(exist_ok=True)
        (self.proj.root / "refs").mkdir(exist_ok=True)
        (self.proj.root / "prompts" / "char_唐僧.txt").write_text("A monk", encoding="utf-8")
        (self.proj.root / "refs" / "char_唐僧.png").write_bytes(b"\x89PNG")
        im = R.style_impact(self.proj, preset="anime")
        self.assertEqual(1, im["images"]["portraits"]["untracked"])
        self.assertIn("唐僧", im["images"]["portraits"]["names"])

    def test_unnamed_project_path_still_works(self):
        """`Project` / `Path` / `str` 三种形态都要能传（Path.root 陷阱见 chapters 的说明）。"""
        self.assertTrue(R.path_of(self.proj).name.endswith("style.json"))
        self.assertTrue(str(R.path_of(self.proj.root)).endswith("style.json"))


class TestDeclaredJobKindsAreExecutable(unittest.TestCase):
    """
    锁住一个"声明了但没实现"的缺口。

    `storyboard` 在 `JOB_KINDS` 里登记了、`budget.JOB_UNIT_GPU_SEC` 也给了单价，
    但 `drain` 里**没有对应分支** ⇒ 谁把这个类型的作业入队，谁就收获一个必然失败的作业。
    P2 的「按新风格重出分镜图」是第一个真的用它的人，所以顺手把这条钉成断言。
    """

    def test_every_declared_kind_has_a_drain_branch(self):
        src = (Path(__file__).resolve().parent.parent / "queue.py").read_text(encoding="utf-8")
        for kind in Q.JOB_KINDS:
            self.assertIn(f'j.kind == "{kind}"', src,
                          f"{kind} 在 JOB_KINDS 里登记了，但 drain 没有执行分支")

    def test_every_declared_kind_is_priced(self):
        """GPU 出口每个都要有单价 —— 这是 B2「抽卡不设防」那次的教训。"""
        from vm import budget as B
        for kind in Q.JOB_KINDS:
            self.assertIn(kind, B.JOB_UNIT_GPU_SEC)
            self.assertGreater(B.JOB_UNIT_GPU_SEC[kind], 0,
                               f"{kind} 单价为 0 等于不设防")


if __name__ == "__main__":
    unittest.main()
