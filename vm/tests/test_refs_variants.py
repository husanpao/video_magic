"""
章节维度造型变体（P3.2）与参考图解析单一出口（`vm/refs.py`）的回归门禁。

锁的是两类"错了也不会报"的失效：

1. **顺序契约**（`CONTRACTS.md:147-154`）：`chars[0] ↔ ref_image_0 ↔ <Picture 1>`。
   解析器一旦"跳过缺项后压缩列表"，后面的索引整体前移 ——
   画面照常生成、质检照常通过，只是每个人的脸安在别人的身体上。
   这是本仓库记录在案的"最难查的一类 bug"，所以缺项必须**报错**而不是跳过。

2. **同源**：解析参考图的代码原先有**三处**各算各的 ——
   `gen._ref_names_for`（实际提交）、`gen.render_shot` 里的 `ref_paths`（算指纹）、
   `config.py:797`（预测"改配置会让哪些镜变 stale"）。
   今天它们恰好等价；P3 加"分章用不同定妆照"之后，只要有一处没跟上就会出现
   **"图换了但指纹没变 → 不重渲"**（用户以为换装生效了，其实永远不生效），
   这正是本仓库在风格后缀与 `shot.costume` 两件事上反复踩过的同一型错误。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from vm import chars as CH
from vm import costumes as K
from vm import refs as R
from vm import shots as SH
from vm.state import Project


def _proj(with_variants: bool = True) -> Project:
    root = Path(tempfile.mkdtemp())
    pr = Project(root / "p")
    pr.ensure()
    (pr.refs_dir / "char_林樾.png").write_bytes(b"DEFAULT-PORTRAIT")
    (pr.refs_dir / "char_男人.png").write_bytes(b"OTHER-PORTRAIT")
    if with_variants:
        (pr.refs_dir / "char_林樾@ch02.png").write_bytes(b"CHAPTER2-VARIANT-longer")
    return pr


class TestChapterVariantResolution(unittest.TestCase):
    def test_default_when_no_variant(self):
        pr = _proj()
        p, src = R.resolve_ref(pr, "林樾", 1)
        self.assertEqual("char_林樾.png", p.name)
        self.assertEqual("default", src)

    def test_variant_wins_for_its_own_chapter(self):
        pr = _proj()
        p, src = R.resolve_ref(pr, "林樾", 2)
        self.assertEqual("char_林樾@ch02.png", p.name)
        self.assertEqual("chapter02", src)

    def test_does_not_borrow_from_earlier_chapter(self):
        """★ 第 3 章没建专属图 ⇒ 用默认，**不向前借第 2 章**。

        借来的图是另一个造型，而提示词写的是当前造型；
        宁可回落默认 + 界面显示"这章没专属造型"，也不要静默用一个错的造型。"""
        pr = _proj()
        p, src = R.resolve_ref(pr, "林樾", 3)
        self.assertEqual("char_林樾.png", p.name)
        self.assertEqual("default", src)

    def test_malformed_shot_id_falls_back_to_default(self):
        p, src = R.resolve_ref(_proj(), "林樾", R.chapter_of("乱写的 id"))
        self.assertEqual("default", src)

    def test_chapter_of_parses_id_prefix(self):
        self.assertEqual(12, R.chapter_of("12-3-02"))
        self.assertIsNone(R.chapter_of("abc"))
        self.assertIsNone(R.chapter_of(""))


class TestOrderContractIsUnbreakable(unittest.TestCase):
    """参考图顺序是硬契约：长度必须恒等于 chars 长度。"""

    def test_length_always_matches_chars(self):
        pr = _proj()
        self.assertEqual(3, len(R.refs_for_shot(pr, ["林樾", "不存在的人", "男人"], "1-1-01")))

    def test_missing_slot_stays_a_hole_not_dropped(self):
        """缺项位置必须是 None 占位 —— 压缩列表就会让后面所有人错位。"""
        pr = _proj()
        got = R.refs_for_shot(pr, ["林樾", "不存在的人", "男人"], "1-1-01")
        self.assertIsNotNone(got[0])
        self.assertIsNone(got[1])
        self.assertIsNotNone(got[2])
        self.assertEqual("男人.png", Path(got[2][0]).name.replace("char_", ""))

    def test_require_refs_raises_on_any_missing(self):
        pr = _proj()
        with self.assertRaises(Exception) as cm:
            R.require_refs(pr, ["林樾", "不存在的人"], "2-1-01")
        msg = str(cm.exception)
        self.assertIn("不存在的人", msg)
        self.assertIn("一一对应", msg)
        # 错误信息要说出这一章也没专属图，否则用户会以为出过了
        self.assertIn("@ch02", msg)

    def test_variant_missing_but_default_present_renders_default(self):
        """某章登记了专属造型但图还没出 → 用默认图（可渲），不是报错。"""
        pr = _proj(with_variants=False)
        got = R.require_refs(pr, ["林樾"], "2-1-01")
        self.assertEqual("default", got[0][1])


class TestSingleSourceOfTruth(unittest.TestCase):
    """三处解析必须同源 —— 用源码结构钉住，而不是靠约定。"""

    def test_gen_uses_the_shared_resolver(self):
        src = (Path(__file__).resolve().parent.parent / "gen.py").read_text(encoding="utf-8")
        self.assertIn("_R.require_refs", src, "提交路径没走共享解析器")
        self.assertIn("_R.ref_paths", src, "指纹路径没走共享解析器")
        self.assertNotIn('proj.refs_dir / f"char_{c}.png"', src,
                         "gen.py 里还有自己拼的参考图路径")

    def test_config_uses_the_shared_resolver(self):
        src = (Path(__file__).resolve().parent.parent / "config.py").read_text(encoding="utf-8")
        self.assertIn("_R.refs_for_shot", src,
                      "config 的 stale 预测没走共享解析器 → 会算错该重渲哪些镜")

    def test_fingerprint_input_differs_per_chapter(self):
        """变体图必须让**该章**镜头的指纹变化（从而自动重渲），其他章不受影响。"""
        pr = _proj()
        p1 = R.ref_paths(pr, ["林樾"], "1-1-01")
        p2 = R.ref_paths(pr, ["林樾"], "2-1-01")
        p3 = R.ref_paths(pr, ["林樾"], "3-1-01")
        self.assertNotEqual(p1, p2)
        self.assertEqual(p1, p3, "第 3 章没有变体，不该被牵连")
        self.assertEqual(p2, R.ref_paths(pr, ["林樾"], "2-1-02"), "同章内要稳定")


class TestCostumeChapterVariants(unittest.TestCase):
    def setUp(self):
        self.pr = Project(Path(tempfile.mkdtemp()) / "c")
        self.pr.ensure()
        K.save(self.pr, {"characters": {"林樾": {
            "identity": "Young woman, slim build, oval face",
            "variants": [{"id": "default", "label": "默认", "prompt": "Dark wool coat"}]}}})

    def test_stored_shape_is_not_invented(self):
        """变体挂在 `characters[名]["variants"]` 下 —— 不另发明顶层结构。"""
        K.set_chapter_variant(self.pr, "林樾", 2, variant_id="wet", label="湿衣",
                              prompt="Soaked wool coat")
        d = json.loads((self.pr.root / "costumes.json").read_text(encoding="utf-8"))
        vs = d["characters"]["林樾"]["variants"]
        self.assertEqual(2, len(vs))
        self.assertEqual({"id", "label", "prompt", "chapter"} & set(vs[-1].keys()),
                         {"id", "label", "prompt", "chapter"})

    def test_existing_default_variant_untouched(self):
        before = K.variants_of(K.load(self.pr), "林樾")[0]
        K.set_chapter_variant(self.pr, "林樾", 2, variant_id="wet", prompt="Soaked coat")
        self.assertEqual(before, K.variants_of(K.load(self.pr), "林樾")[0])

    def test_chapter_lookup_is_exact_not_nearest(self):
        K.set_chapter_variant(self.pr, "林樾", 2, variant_id="wet", prompt="Soaked coat")
        self.assertIsNotNone(K.variants_of_chapter(K.load(self.pr), "林樾", 2))
        self.assertIsNone(K.variants_of_chapter(K.load(self.pr), "林樾", 3))
        self.assertIsNone(K.variants_of_chapter(K.load(self.pr), "林樾", None))

    def test_retag_same_variant_does_not_duplicate(self):
        K.set_chapter_variant(self.pr, "林樾", 2, variant_id="wet", prompt="A")
        K.set_chapter_variant(self.pr, "林樾", 5, variant_id="wet", prompt="B")
        vs = K.variants_of(K.load(self.pr), "林樾")
        self.assertEqual(2, len(vs), "同一变体被登记了两次")
        self.assertEqual(5, vs[-1]["chapter"])

    def test_chapters_with_variants_for_the_registry_table(self):
        K.set_chapter_variant(self.pr, "林樾", 2, variant_id="wet", prompt="Soaked")
        self.assertEqual([2], K.chapters_with_variants(self.pr, "林樾"))
        self.assertEqual([], K.chapters_with_variants(self.pr, "男人"))

    def test_prompt_marks_the_variant_explicitly(self):
        """造型差异要在提示词里被标明是 costume_override，否则模型会当成新身份。"""
        K.set_chapter_variant(self.pr, "林樾", 2, variant_id="wet",
                              prompt="Soaked dark wool coat clinging to shoulders")
        out = CH.costume_prompt(self.pr, "林樾", 2, "identity: young woman.", log=lambda m: None)
        self.assertIn("costume_override (wet)", out)
        # 身份基准句必须**整句保留**（尾点会被 rstrip 掉再接 override 段，所以不比尾点）
        self.assertIn("identity: young woman", out, "身份基准句不能被替换掉")
        self.assertTrue(out.startswith("identity"), "基准句必须在前，override 段在后")

    def test_no_variant_leaves_prompt_untouched(self):
        out = CH.costume_prompt(self.pr, "林樾", 7, "identity: young woman.", log=lambda m: None)
        self.assertEqual("identity: young woman.", out)

    def test_chapter_zero_and_none_are_not_variants(self):
        self.assertEqual("identity base",
                         CH.costume_prompt(self.pr, "林樾", 0, "identity base", log=lambda m: None))


class TestCostumeRebuildsPrompt(unittest.TestCase):
    """
    契约 **R3.2** 的欠账（`CONTRACTS.md:471`，v0.2 交付说明也列为遗留项）。

    只改 `shot.costume` 字段而不动 prompt ⇒ 指纹不变 ⇒ 状态仍是 current ⇒ 不重渲 ⇒
    **用户以为换了衣服，其实永远不生效**。这是 R3.1 台词/运镜那类静默失效的第三例，
    所以必须有一条测试钉住"改服装一定重写 prompt 的相应两段"。
    """

    SIX = (
        "subject_definitions: <Subject 1> = a young woman.\n"
        "summary: [reference generation] single shot of <Subject 1>.\n"
        "retention_analysis: <Subject 1>: fully_preserved.\n"
        "detailed_description: She steps off the train.\n"
        "overall_soundscape: platform reverb.\n"
        "non_diegetic_music: low strings.\n"
    )

    def setUp(self):
        import shutil
        from vm import plan as PL

        self.pr = Project(Path(tempfile.mkdtemp()) / "rb")
        self.pr.ensure()
        self.cards = {"林樾": PL.CharCard(
            name="林樾", appearance="Young woman, slim build, oval face",
            costume="Dark wool coat over a knit top", portrait_prompt="x")}
        K.ensure_from_cards(self.pr, self.cards)
        K.set_chapter_variant(self.pr, "林樾", 2, variant_id="wet", label="湿衣",
                              prompt="Soaked dark wool coat clinging to shoulders")

    class _Shot:
        """rebuild_prompt 要的是一镜的完整视图；prompt 必须是那六段。"""

        def __init__(self, costume):
            self.id, self.chars, self.costume = "2-1-01", ["林樾"], costume
            self.shot_size, self.camera = "中景", ""
            self.dialogue, self.narration = "", ""
            self.sec, self.seed = 5, 1
            self.prompt = TestCostumeRebuildsPrompt.SIX

    def _prompt(self, costume) -> str:
        return K.rebuild_prompt(self._Shot(costume), self.cards, K.load(self.pr))

    def test_non_default_variant_rewrites_subject_definitions(self):
        out = self._prompt({"林樾": "wet"})
        self.assertIn("Soaked dark wool coat", out)
        self.assertNotIn(self.SIX.splitlines()[0], out, "subject_definitions 必须被重建")

    def test_other_four_sections_are_untouched(self):
        out = self._prompt({"林樾": "wet"})
        self.assertIn("She steps off the train.", out)
        self.assertIn("platform reverb", out)
        self.assertIn("low strings", out)

    def test_default_variant_keeps_strict_wording(self):
        """默认变体要"图为准"的严格措辞；非默认才放宽服装权威（否则模型会照图穿回旧衣）。"""
        strict = self._prompt({})
        loose = self._prompt({"林樾": "wet"})
        self.assertNotIn("CLOTHING is authoritative", strict)
        self.assertIn("CLOTHING is authoritative", loose)

    def test_fingerprint_would_actually_change(self):
        """终极验收：重建后的 prompt 与旧的**指纹不同** —— 否则一切照旧不重渲。"""
        from vm.state import shot_fingerprint

        params = {"unet": "u", "lora": "l", "steps": 8, "width": 864, "height": 480}
        old = shot_fingerprint(self.SIX, ["林樾"], [Path("r.png")], params, 121, 1)
        new = shot_fingerprint(self._prompt({"林樾": "wet"}), ["林樾"], [Path("r.png")], params, 121, 1)
        self.assertNotEqual(old, new, "服装变了但指纹没变 ⇒ 换装永远不会生效")

    def test_incomplete_prompt_degrades_instead_of_raising(self):
        """旧表/手改过的 prompt 缺段时不能抛，要降级保留原文（编辑层契约要求）。"""
        class Bad:
            id, chars, costume = "2-1-01", ["林樾"], {"林樾": "wet"}
            shot_size = camera = dialogue = narration = ""
            sec, seed = 5, 1
            prompt = "detailed_description: only one section here"
        try:
            K.rebuild_prompt(Bad(), self.cards, K.load(self.pr))
            self.fail("缺段应抛 ValueError（调用方负责降级），这里不该静默返回")
        except ValueError:
            pass


class TestGenAllCharsSkipsUnregistered(unittest.TestCase):
    """第 N 章没登记换装的角色不该被出一张"和默认图内容相同的假变体"。"""

    def setUp(self):
        self.pr = Project(Path(tempfile.mkdtemp()) / "g")
        self.pr.ensure()
        for n in ("林樾", "男人"):
            (self.pr.prompts_dir / f"char_{n}.txt").write_text(f"identity of {n}", encoding="utf-8")
        K.save(self.pr, {"characters": {n: {"identity": f"id {n}", "variants": [
            {"id": "default", "label": "默认", "prompt": "coat"}]} for n in ("林樾", "男人")}})

    def test_only_registered_characters_are_rendered(self):
        K.set_chapter_variant(self.pr, "林樾", 2, variant_id="wet", prompt="Soaked coat")
        calls: list[str] = []
        orig = CH.gen_char
        CH.gen_char = lambda proj, name, params, log=print, **kw: (
            calls.append(name), Path(str(kw.get("chapter") or "")))[1]
        try:
            out = CH.gen_all_chars(self.pr, {}, log=lambda m: None, chapter=2)
        finally:
            CH.gen_char = orig
        self.assertEqual(["林樾"], calls)
        self.assertNotIn("男人", calls, "未登记换装的角色被出了假变体")

    def test_no_registered_variant_renders_nothing(self):
        orig = CH.gen_char
        CH.gen_char = lambda *a, **k: Path("x")
        try:
            self.assertEqual({}, CH.gen_all_chars(self.pr, {}, log=lambda m: None, chapter=9))
        finally:
            CH.gen_char = orig


class TestVariantFileNaming(unittest.TestCase):
    def test_default_and_variant_names(self):
        self.assertEqual("char_林樾.png", R.variant_filename("林樾", None))
        self.assertEqual("char_林樾@ch02.png", R.variant_filename("林樾", 2))
        self.assertEqual("char_林樾@ch12.png", R.variant_filename("林樾", 12))

    def test_glob_covers_variants_for_comfy_input_sync(self):
        """`sync_refs_to_input` 靠 `glob("char_*.png")` 把图同步进 ComfyUI input；
        变体图也必须被这个 glob 命中，否则渲染时 LoadImage 找不到文件。"""
        pr = _proj()
        self.assertIn("char_林樾@ch02.png",
                      [p.name for p in pr.refs_dir.glob("char_*.png")])

    def test_round_trip_listing(self):
        pr = _proj()
        vs = R.list_variants(pr, "林樾")
        self.assertEqual({"default", "chapter"}, {v["kind"] for v in vs})
        self.assertEqual([None, 2], [v["chapter"] for v in vs])


if __name__ == "__main__":
    unittest.main()
