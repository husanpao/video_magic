"""B5 对白零丢失（覆盖率门禁 + src_span 逐字校验）的单测。"""

from __future__ import annotations

import unittest

from vm.audit.dialogue_fidelity import (
    audit_dialogue_fidelity,
    coverage_line,
    extract_source_quotes,
    locate_chapter_text,
    to_manifest_entry,
)
from vm.audit.tests import make_shot

# 正文用直引号写对白（本项目的实际写法），chapter_dialogues() 的判据能认出
CHAPTER = (
    "唐僧勒住缰绳，回头道：\"悟空，你听这风声。\"\n\n"
    "孙悟空纵身跳上青石，笑道：\"师父，不是风哭。\"\n"
)
Q1 = "悟空，你听这风声。"
Q2 = "师父，不是风哭。"


def codes(report: dict) -> list[str]:
    return [f["code"] for f in report["findings"]]


def span_of(q: str) -> list[int]:
    a = CHAPTER.index(q)
    return [a, a + len(q)]


class TestSourceQuotes(unittest.TestCase):
    def test_extract_source_quotes(self):
        quotes = extract_source_quotes(CHAPTER)
        self.assertEqual([q["quote"] for q in quotes], [Q1, Q2])
        self.assertEqual([q["index"] for q in quotes], [1, 2])


class TestZeroLoss(unittest.TestCase):
    def test_verbatim_dialogue_passes_gate(self):
        shots = [
            make_shot(1, dialogue=Q1),
            make_shot(2, dialogue=Q2),
        ]
        r = audit_dialogue_fidelity(shots, CHAPTER)
        self.assertEqual((r["adopted"], r["missing"]), (2, 0))
        self.assertEqual(r["coverage_pct"], 100.0)
        self.assertTrue(r["gate"]["passed"])
        self.assertEqual(coverage_line(r), "原文 2 句，采纳 2 句，遗漏 0 句")

    def test_long_quote_split_across_shots_is_still_adopted(self):
        shots = [
            make_shot(1, dialogue=Q1[:4]),
            make_shot(2, dialogue=Q1[4:]),
            make_shot(3, dialogue=Q2),
        ]
        r = audit_dialogue_fidelity(shots, CHAPTER)
        self.assertEqual(r["missing"], 0)
        span = next(a for a in r["adopted_spans"] if a["index"] == 1)
        self.assertEqual(span["shots"], [1, 2])

    def test_dialogue_separated_by_narration_in_source_is_not_false_missing(self):
        # 原文里两句之间夹着叙述文字，但分镜把两句合并进同一镜
        # （朴素 substring 判据会在这里误报 —— 实测真实项目有 3 处）
        chapter = "他抬头道：\"第一句。\" 风停了。 他又道：\"第二句。\""
        shots = [make_shot(1, dialogue="第一句。第二句。")]
        r = audit_dialogue_fidelity(shots, chapter)
        self.assertEqual(r["missing"], 0)
        self.assertEqual(r["extraneous"]["chars"], 0)

    def test_missing_quote_is_error_and_fails_gate(self):
        shots = [make_shot(1, dialogue=Q1)]
        r = audit_dialogue_fidelity(shots, CHAPTER)
        self.assertEqual(r["missing"], 1)
        self.assertEqual(r["coverage_pct"], 50.0)
        self.assertFalse(r["gate"]["passed"])
        f = next(f for f in r["findings"] if f["code"] == "dialogue-missing")
        self.assertEqual(f["severity"], "error")
        # 相似度低于 30% 时不硬指一镜（避免误导人去改无关台词）
        self.assertEqual(f["shots"], [])
        self.assertIn("整句丢失", f["message"])

    def test_rewritten_quote_is_classified_partial(self):
        shots = [make_shot(1, dialogue=Q1), make_shot(2, dialogue="师父，不是风在哭。")]
        r = audit_dialogue_fidelity(shots, CHAPTER)
        self.assertEqual(r["missing"], 1)
        self.assertEqual(r["missing_quotes"][0]["kind"], "partial")
        self.assertIn("dialogue-rewritten", codes(r))
        self.assertFalse(r["gate"]["passed"])

    def test_extraneous_dialogue_is_reported(self):
        shots = [make_shot(1, dialogue=Q1), make_shot(2, dialogue="师父，不是风在哭。")]
        r = audit_dialogue_fidelity(shots, CHAPTER)
        self.assertGreater(r["extraneous"]["chars"], 0)
        self.assertIn("dialogue-extraneous", codes(r))

    def test_reordered_dialogue_is_reported(self):
        shots = [make_shot(1, dialogue=Q2), make_shot(2, dialogue=Q1)]
        r = audit_dialogue_fidelity(shots, CHAPTER)
        self.assertIn("dialogue-reordered", codes(r))
        self.assertFalse(r["gate"]["passed"])

    def test_empty_chapter_never_passes_gate(self):
        r = audit_dialogue_fidelity([make_shot(1, dialogue=Q1)], "")
        self.assertFalse(r["gate"]["passed"])
        self.assertIn("chapter-text-missing", codes(r))
        self.assertIn("没有正文可比对", r["gate"]["failures"][0])


class TestSrcSpan(unittest.TestCase):
    def test_exact_span_passes(self):
        shots = [
            make_shot(1, dialogue=[{"char": "唐僧", "quote": Q1, "src_span": span_of(Q1)}]),
            make_shot(2, dialogue=[{"char": "孙悟空", "quote": Q2, "src_span": span_of(Q2)}]),
        ]
        r = audit_dialogue_fidelity(shots, CHAPTER)
        self.assertEqual(r["mode"], "src-span+stream")
        self.assertEqual(r["span_checks"]["provided"], 2)
        self.assertEqual(r["span_checks"]["exact_ok"], 2)
        self.assertEqual(r["span_checks"]["failures"], [])
        self.assertTrue(r["gate"]["passed"])

    def test_span_mismatch_is_error(self):
        shots = [make_shot(1, dialogue=[{"char": "唐僧", "quote": Q1, "src_span": [0, 5]}])]
        r = audit_dialogue_fidelity(shots, CHAPTER)
        self.assertEqual(len(r["span_checks"]["failures"]), 1)
        self.assertIn("src-span-mismatch", codes(r))
        self.assertFalse(r["gate"]["passed"])

    def test_span_out_of_range_is_error(self):
        shots = [make_shot(1, dialogue=[{"char": "唐僧", "quote": Q1, "src_span": [0, 99999]}])]
        r = audit_dialogue_fidelity(shots, CHAPTER)
        self.assertIn("src-span-out-of-range", codes(r))
        self.assertFalse(r["gate"]["passed"])

    def test_punctuation_only_difference_counts_as_normalized_ok(self):
        shots = [make_shot(1, dialogue=[{"char": "唐僧", "quote": "悟空你听这风声",
                                        "src_span": span_of(Q1)}])]
        r = audit_dialogue_fidelity(shots, CHAPTER)
        self.assertEqual(r["span_checks"]["exact_ok"], 0)
        self.assertEqual(r["span_checks"]["normalized_ok"], 1)
        self.assertEqual(r["span_checks"]["failures"], [])


class TestCompatAndManifest(unittest.TestCase):
    def test_old_string_dialogue_does_not_raise(self):
        r = audit_dialogue_fidelity([make_shot(1, dialogue=Q1)], CHAPTER)
        self.assertEqual(r["mode"], "substring-stream")
        self.assertEqual(r["span_checks"]["provided"], 0)

    def test_manifest_entry_is_compact_and_has_gate(self):
        r = audit_dialogue_fidelity([make_shot(1, dialogue=Q1), make_shot(2, dialogue=Q2)], CHAPTER)
        m = to_manifest_entry(r)
        self.assertEqual(
            set(m),
            {"b5_schema_version", "source_quotes", "adopted", "missing", "coverage_pct",
             "extraneous_chars", "span_checks", "span_failures", "gate_passed"},
        )
        self.assertTrue(m["gate_passed"])

    def test_locate_chapter_text_uses_first_novel_file(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            novel = Path(td) / "novel"
            novel.mkdir(parents=True)
            (novel / "第一章.md").write_text(CHAPTER, encoding="utf-8")
            text, path, warns = locate_chapter_text(Path(td))
            self.assertEqual(text, CHAPTER)
            self.assertTrue(path.endswith("第一章.md"))
            self.assertEqual(warns, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
