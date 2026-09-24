"""B1 对话覆盖度审计的单测。"""

from __future__ import annotations

import unittest

from vm.audit.dialogue_coverage import (
    audit_dialogue_coverage,
    detect_dialogue_scenes,
    location_key,
)
from vm.audit.tests import make_shot


def codes(report: dict) -> list[str]:
    return [f["code"] for f in report["findings"]]


class TestGrouping(unittest.TestCase):
    def test_consecutive_dialogue_shots_merge_into_one_scene(self):
        shots = [make_shot(i, dialogue=f"第{i}句台词") for i in range(1, 4)]
        scenes, meta = detect_dialogue_scenes(shots)
        self.assertEqual(meta["mode"], "consecutive")
        self.assertEqual(len(scenes), 1)
        self.assertEqual(scenes[0].shots, [1, 2, 3])

    def test_non_dialogue_shot_splits_scene(self):
        shots = [
            make_shot(1, dialogue="甲"),
            make_shot(2, dialogue="乙"),
            make_shot(3),  # 无台词
            make_shot(4, dialogue="丙"),
        ]
        scenes, _ = detect_dialogue_scenes(shots)
        self.assertEqual([s.shots for s in scenes], [[1, 2], [4]])

    def test_bridge_view_merges_single_reaction_shot(self):
        shots = [
            make_shot(1, dialogue="甲"),
            make_shot(2, dialogue="乙"),
            make_shot(3),  # 一个反应镜
            make_shot(4, dialogue="丙"),
        ]
        strict, _ = detect_dialogue_scenes(shots, bridge_non_dialogue=0)
        bridged, _ = detect_dialogue_scenes(shots, bridge_non_dialogue=1)
        self.assertEqual([s.shots for s in strict], [[1, 2], [4]])
        self.assertEqual([s.shots for s in bridged], [[1, 2, 3, 4]])

    def test_explicit_location_changes_grouping(self):
        shots = [
            make_shot(1, chars=["甲", "乙"], dialogue="A1", location="temple"),
            make_shot(2, chars=["甲"], dialogue="A2", location="temple"),
            make_shot(3, chars=["甲", "乙"], dialogue="B1", location="well"),
        ]
        scenes, meta = detect_dialogue_scenes(shots)
        self.assertEqual(meta["mode"], "location")
        self.assertEqual([s.shots for s in scenes], [[1, 2], [3]])

    def test_same_location_always_merges_even_when_speaker_switches(self):
        shots = [
            make_shot(1, chars=["甲"], dialogue="A1", location="temple"),
            make_shot(2, chars=["乙"], dialogue="B1", location="temple"),
            make_shot(3, chars=["甲"], dialogue="A2", location="temple"),
        ]
        scenes, _ = detect_dialogue_scenes(shots)
        self.assertEqual(len(scenes), 1)
        self.assertEqual(scenes[0].chars, ["甲", "乙"])

    def test_location_key_prefers_explicit_field(self):
        key, src = location_key(make_shot(1, location="云隐寺"))
        self.assertEqual((key, src), ("云隐寺", "explicit"))

    def test_location_key_uses_prompt_place_word(self):
        shot = make_shot(
            1,
            prompt="detailed_description: Cinematic realism with natural film lighting. "
            "Medium shot on a mountain road at dusk, a monk rides west.",
        )
        key, src = location_key(shot)
        self.assertEqual(key, "mountain road")
        self.assertEqual(src, "prompt")


class TestUpstreamChecks(unittest.TestCase):
    def test_all_wide_dialogue_is_flagged(self):
        shots = [
            make_shot(1, chars=["甲", "乙"], size="全景", dialogue="甲说", location="temple"),
            make_shot(2, chars=["甲", "乙"], size="全景", dialogue="乙说", location="temple"),
            make_shot(3, chars=["甲", "乙"], size="远景", dialogue="甲再说", location="temple"),
        ]
        r = audit_dialogue_coverage(shots)
        self.assertIn("wide-only-dialogue", codes(r))
        self.assertEqual(r["needs_close_up_shots"], [1])
        self.assertEqual(r["coverage_score"], 0.0)

    def test_normal_shot_reverse_is_not_flagged(self):
        shots = [
            make_shot(1, chars=["甲", "乙"], size="近景", dialogue="甲说", location="temple"),
            make_shot(2, chars=["甲", "乙"], size="中景", dialogue="乙说", location="temple"),
            make_shot(3, chars=["甲", "乙"], size="近景", dialogue="甲再说", location="temple"),
        ]
        r = audit_dialogue_coverage(shots)
        self.assertNotIn("wide-only-dialogue", codes(r))
        self.assertNotIn("needs-reverse-shot", codes(r))
        self.assertEqual(r["coverage_score"], 100.0)

    def test_single_shot_multi_char_dialogue_needs_reverse_shot(self):
        shots = [make_shot(1, chars=["甲", "乙"], dialogue="两个人说话", location="temple")]
        r = audit_dialogue_coverage(shots)
        self.assertIn("needs-reverse-shot", codes(r))
        self.assertEqual(r["needs_reverse_shot_shots"], [1])
        self.assertEqual(r["coverage_score"], 0.0)
        hint = next(f for f in r["findings"] if f["code"] == "needs-reverse-shot")["rewrite_hint"]
        self.assertIn("反应镜", hint)

    def test_single_shot_single_char_is_not_a_dialogue_scene_problem(self):
        shots = [make_shot(1, chars=["甲"], dialogue="自言自语", location="temple")]
        r = audit_dialogue_coverage(shots)
        self.assertEqual(r["findings"], [])
        self.assertEqual(r["coverage_score"], 100.0)

    def test_coverage_score_math(self):
        # 4 个多角色场景：1 个只有 1 镜、1 个全 wide、2 个正常 → (4-2)/4 = 50%
        shots = []
        n = 0
        for loc in ("L1", "L2", "L3", "L4"):
            n += 1
            shots.append(make_shot(n, chars=["甲", "乙"], dialogue="d", location=loc))
            if loc in ("L2", "L3"):
                n += 1
                size = "全景" if loc == "L2" else "近景"
                shots.append(make_shot(n, chars=["甲", "乙"], size=size, dialogue="d", location=loc))
                if loc == "L3":
                    n += 1
                    shots.append(make_shot(n, chars=["甲", "乙"], size="近景", dialogue="d", location=loc))
        r = audit_dialogue_coverage(shots)
        self.assertEqual(r["multi_char_scene_count"], 4)
        self.assertEqual(r["coverage_score"], 50.0)


class TestExtensions(unittest.TestCase):
    def test_monotone_shot_size_advisory(self):
        shots = [
            make_shot(i, chars=["甲", "乙"], size="中景", dialogue=f"台词{i}", location="temple")
            for i in range(1, 4)
        ]
        r = audit_dialogue_coverage(shots)
        f = next(f for f in r["findings"] if f["code"] == "monotone-shot-size")
        self.assertEqual(f["severity"], "info")
        self.assertTrue(f["ext"])  # 本仓库扩展（非上游检查）
        self.assertEqual(f["shots"], [1, 2, 3])

    def test_monotone_not_flagged_when_close_ups_vary(self):
        shots = [
            make_shot(1, chars=["甲", "乙"], size="中景", dialogue="a", location="temple"),
            make_shot(2, chars=["甲", "乙"], size="近景", dialogue="b", location="temple"),
            make_shot(3, chars=["甲", "乙"], size="中景", dialogue="c", location="temple"),
        ]
        r = audit_dialogue_coverage(shots)
        self.assertNotIn("monotone-shot-size", codes(r))

    def test_no_two_shot_advisory_uses_extended_scene(self):
        shots = [
            make_shot(1, chars=["甲"], dialogue="a", location="temple"),
            make_shot(2, chars=["甲", "乙"], location="temple"),  # 双人同框反应镜（无台词）
            make_shot(3, chars=["乙"], dialogue="b", location="temple"),
        ]
        r = audit_dialogue_coverage(shots)
        # 严格分组把它切成两段，扩展分组把反应镜桥进来 → 有双人同框，不该报
        self.assertNotIn("no-two-shot", codes(r))

    def test_no_two_shot_advisory_fires_without_any_group_shot(self):
        shots = [
            make_shot(i, chars=["甲"] if i % 2 else ["乙"], dialogue=f"台词{i}",
                      location="temple")
            for i in range(1, 5)
        ]
        r = audit_dialogue_coverage(shots)
        f = next(f for f in r["findings"] if f["code"] == "no-two-shot")
        self.assertEqual(f["severity"], "info")

    def test_ambiguous_speaker_only_when_no_speaker_field(self):
        plain = audit_dialogue_coverage(
            [make_shot(1, chars=["甲", "乙"], dialogue="谁在说", location="temple")]
        )
        self.assertIn("ambiguous-speaker", codes(plain))

        with_speaker = audit_dialogue_coverage(
            [
                make_shot(
                    1,
                    chars=["甲", "乙"],
                    dialogue=[{"char": "甲", "quote": "我在说"}],
                    location="temple",
                )
            ]
        )
        self.assertNotIn("ambiguous-speaker", codes(with_speaker))

    def test_dialogue_list_shape_is_understood(self):
        shots = [
            make_shot(1, chars=["甲"], dialogue=[{"char": "甲", "quote": "一"}], location="temple"),
            make_shot(2, chars=["乙"], dialogue=[{"char": "乙", "quote": "二"}], location="temple"),
        ]
        r = audit_dialogue_coverage(shots)
        self.assertEqual(r["scenes"][0]["speakers"], ["甲", "乙"])
        self.assertEqual(r["scenes"][0]["chars"], ["甲", "乙"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
