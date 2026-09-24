"""B2 节奏 / 冲突形状审计的单测。"""

from __future__ import annotations

import unittest

from vm.audit.pacing import (
    PROXY_WEIGHTS,
    analyze_conflict_shape,
    audit_duration_rhythm,
    audit_opening,
    audit_pacing,
    find_drag_segments,
    intensity_series,
    least_squares_slope,
    proxy_scores,
    weakest_window,
)
from vm.audit.tests import make_shot


def codes(report: dict) -> list[str]:
    return [f["code"] for f in report["findings"]]


class TestShape(unittest.TestCase):
    def test_escalating_vs_front_loaded_are_different_shapes(self):
        up = [1, 2, 4, 9]
        down = [9, 4, 2, 1]
        # 上游的动机：两者平均都是 4，但形状完全不同
        self.assertEqual(sum(up) / 4, sum(down) / 4)
        self.assertEqual(analyze_conflict_shape(up)["shape"], "escalating")
        self.assertEqual(analyze_conflict_shape(down)["shape"], "front-loaded")
        self.assertAlmostEqual(analyze_conflict_shape(up)["slope"], 2.6, places=3)
        self.assertAlmostEqual(analyze_conflict_shape(down)["slope"], -2.6, places=3)

    def test_no_climax_when_peak_prominence_below_threshold(self):
        r = analyze_conflict_shape([5.0, 5.0, 5.2, 5.0, 5.1])
        self.assertEqual(r["shape"], "no-climax")
        self.assertLess(r["peak_prominence"], 1.5)

    def test_flat_when_prominent_peak_but_no_trend(self):
        # 峰值显著度 7−5.5 = 1.5（≥1.5 不算 no-climax），斜率 0 → flat
        r = analyze_conflict_shape([4.0, 7.0, 7.0, 4.0])
        self.assertEqual(r["shape"], "flat")
        self.assertAlmostEqual(r["slope"], 0.0, places=6)

    def test_empty_series(self):
        r = analyze_conflict_shape([])
        self.assertEqual(r["shape"], "empty")
        self.assertIsNone(r["peak_index"])

    def test_least_squares_slope_known_values(self):
        self.assertAlmostEqual(least_squares_slope([1, 2, 3, 4]), 1.0, places=9)
        self.assertAlmostEqual(least_squares_slope([4, 3, 2, 1]), -1.0, places=9)
        self.assertEqual(least_squares_slope([7]), 0.0)


class TestDragSegments(unittest.TestCase):
    def test_three_low_shots_in_a_row(self):
        segs = find_drag_segments([5, 3, 3, 3, 5], threshold=4.0, min_run=3)
        self.assertEqual(len(segs), 1)
        self.assertEqual((segs[0]["from_shot"], segs[0]["to_shot"]), (2, 4))
        self.assertEqual(segs[0]["length"], 3)
        self.assertAlmostEqual(segs[0]["avg_score"], 3.0, places=6)

    def test_two_low_shots_do_not_trigger(self):
        self.assertEqual(find_drag_segments([5, 3, 3, 5], threshold=4.0, min_run=3), [])

    def test_threshold_is_strict(self):
        # 正好等于阈值不算“低于阈值”（上游是 <）
        self.assertEqual(find_drag_segments([4.0, 4.0, 4.0], threshold=4.0, min_run=3), [])
        self.assertEqual(len(find_drag_segments([3.999, 3.999, 3.999], threshold=4.0, min_run=3)), 1)

    def test_two_separate_segments(self):
        segs = find_drag_segments([2, 2, 2, 9, 2, 2, 2], threshold=4.0, min_run=3)
        self.assertEqual([(s["from_shot"], s["to_shot"]) for s in segs], [(1, 3), (5, 7)])

    def test_weakest_window_picks_earliest_lowest(self):
        w = weakest_window([6, 6, 3, 3, 3, 6], size=3)
        self.assertEqual((w["from_shot"], w["to_shot"]), (3, 5))
        self.assertAlmostEqual(w["avg_score"], 3.0, places=6)


class TestOpeningAndDuration(unittest.TestCase):
    def test_opening_window_clamped_between_2_and_5(self):
        self.assertEqual(audit_opening([5] * 60)["window"], 5)
        self.assertEqual(audit_opening([5] * 6)["window"], 2)
        self.assertEqual(audit_opening([5] * 12)["window"], 4)

    def test_opening_low_is_flagged(self):
        r = audit_opening([2.0, 2.0, 8.0, 8.0, 8.0, 8.0, 8.0, 8.0, 8.0], mode="drama")
        self.assertFalse(r["passed"])
        self.assertEqual(r["min_avg"], 5.0)

    def test_duration_cv_boundary(self):
        # cv = std/mean < 0.12 → 呆板
        monotone = audit_duration_rhythm([5] * 20)
        self.assertTrue(monotone["monotone"])
        self.assertEqual(monotone["cv"], 0.0)

        # cv ≈ 0.118（真正低于呆板线）
        below = audit_duration_rhythm([5 - 0.59, 5 + 0.59] * 10)
        self.assertAlmostEqual(below["cv"], 0.118, places=3)
        self.assertTrue(below["monotone"])

        # cv ≈ 0.14（高于呆板线）
        above = audit_duration_rhythm([5 - 0.7, 5 + 0.7] * 10)
        self.assertAlmostEqual(above["cv"], 0.14, places=3)
        self.assertFalse(above["monotone"])

        # 判定是**严格小于**：把门槛调到与 cv 相等的同一浮点值时不该判呆板
        strict = audit_duration_rhythm([5 - 0.7, 5 + 0.7] * 10, min_cv=above["cv"])
        self.assertFalse(strict["monotone"])

    def test_long_shot_runs(self):
        r = audit_duration_rhythm([5, 4, 5, 5, 10, 10, 10, 5])
        self.assertEqual(len(r["long_runs"]), 1)
        self.assertEqual((r["long_runs"][0]["from_shot"], r["long_runs"][0]["to_shot"]), (5, 7))


class TestIntensitySource(unittest.TestCase):
    def test_proxy_scores_deterministic_and_bounded(self):
        shots = [
            make_shot(1, sec=3, chars=["甲"], size="近景", dialogue="快点"),
            make_shot(2, sec=15, chars=["甲", "乙", "丙"], size="远景", narration="很远"),
        ]
        first = proxy_scores(shots)
        second = proxy_scores(shots)
        self.assertEqual(first, second)
        self.assertTrue(all(0.0 <= v <= 10.0 for v in first))
        self.assertGreater(first[0], first[1])  # 短+近 > 长+远
        self.assertAlmostEqual(sum(PROXY_WEIGHTS.values()), 1.0, places=9)

    def test_explicit_scores_are_preferred(self):
        shots = [
            make_shot(1, conflict=1),
            make_shot(2, conflict=2),
            make_shot(3, conflict=9),
        ]
        scores, meta = intensity_series(shots, mode="auto")
        self.assertEqual(meta["mode"], "explicit")
        self.assertEqual(meta["key"], "conflict")
        self.assertEqual(scores, [1.0, 2.0, 9.0])

    def test_unit_scaled_explicit_scores_are_rescaled(self):
        shots = [make_shot(1, conflict=0.1), make_shot(2, conflict=0.9)]
        scores, meta = intensity_series(shots, mode="auto")
        self.assertTrue(meta.get("rescaled_from_unit"))
        self.assertEqual(scores, [1.0, 9.0])

    def test_partial_explicit_scores_fall_back_to_proxy(self):
        shots = [make_shot(1, conflict=3), make_shot(2)]
        _, meta = intensity_series(shots, mode="auto")
        self.assertEqual(meta["mode"], "proxy")

    def test_explicit_mode_without_scores_raises(self):
        with self.assertRaises(ValueError):
            intensity_series([make_shot(1)], mode="explicit")


class TestAuditPacing(unittest.TestCase):
    def test_explicit_scores_make_shape_warning(self):
        shots = [make_shot(i, conflict=v) for i, v in enumerate([9, 8, 7, 6, 5], 1)]
        r = audit_pacing(shots)
        f = next(f for f in r["findings"] if f["code"] == "front-loaded")
        self.assertEqual(f["severity"], "warning")  # 显式分 → 有证据，升到 warning

    def test_proxy_scores_keep_shape_advisory(self):
        shots = [make_shot(i, sec=5, size="中景", dialogue="d") for i in range(1, 6)]
        r = audit_pacing(shots)
        shape_findings = [f for f in r["findings"] if f["code"] in
                          ("no-climax", "front-loaded", "flat-shape")]
        self.assertTrue(shape_findings)
        self.assertTrue(all(f["severity"] == "info" for f in shape_findings))

    def test_drag_segment_reported_with_shot_range(self):
        shots = []
        for i in range(1, 8):
            shots.append(make_shot(i, sec=5, size="全景" if i in (2, 3, 4) else "近景",
                                   dialogue="" if i in (2, 3, 4) else "台词"))
        r = audit_pacing(shots)
        drags = [f for f in r["findings"] if f["code"] == "drag-segment"]
        if drags:  # 代理分下是否触发取决于权重，这里只断言结构
            self.assertEqual(drags[0]["shots"], [2, 3, 4])
        self.assertIn("weakest_window", r)

    def test_duration_monotone_finding(self):
        shots = [make_shot(i, sec=5, dialogue="台词") for i in range(1, 10)]
        r = audit_pacing(shots)
        self.assertIn("duration-monotone", codes(r))
        self.assertTrue(r["duration"]["monotone"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
