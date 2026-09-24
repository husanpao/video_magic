"""
taskctl 纯函数层的单测：镜头排序 / 质检结果宽容解析 / 帧数换算。

这三个是任务层里被 48 个端点共享的"地基函数"：
  · _shot_order 排错 → 用户看到的镜头表整个错乱（1-10-01 排到 1-2-01 前面）；
  · _tolerant_qc 解析僵化 → 队友改一次 qc.json 形状 UI 就 500；
  · 帧换算双实现不一致 → 同一个 sec 换出两种帧数，指纹/拼接/时长预算对不上账。
"""

from __future__ import annotations

import unittest

from vm.shots import (
    FRAME_MAX,
    FRAME_MIN,
    frames_to_sec,
    seconds_to_frames,
)
from vm.taskctl import _shot_order, _tolerant_qc, frames_for_seconds


class TestShotOrder(unittest.TestCase):
    def test_numeric_not_lexicographic(self):
        """字符串排序会把 1-10-01 排在 1-2-01 前面（'-' < '0'），必须按数值元组排。"""
        ids = ["1-10-01", "1-2-01", "1-1-02", "1-1-01", "2-1-01", "1-1-01"]
        got = sorted(ids, key=_shot_order)
        self.assertEqual(got, ["1-1-01", "1-1-01", "1-1-02", "1-2-01", "1-10-01", "2-1-01"],
                         f"排序结果错：{got}")

    def test_padded_and_unpadded_are_equal(self):
        self.assertEqual(_shot_order("1-02-01"), _shot_order("1-2-01"),
                         "补零与不补零的同一镜必须同序")

    def test_partial_ids_pad_with_zero(self):
        self.assertEqual(_shot_order("1-2"), (1, 2, 0))
        self.assertEqual(_shot_order("3"), (3, 0, 0))
        self.assertEqual(_shot_order(""), (0, 0, 0))
        self.assertEqual(_shot_order(None), (0, 0, 0))


class TestTolerantQc(unittest.TestCase):
    def test_results_container_wins(self):
        """形状宽容解析：容器名优先（results/shots/clips），不因形状微调就 500。"""
        raw = {"results": {"1-01-01": {"ok": True, "verdict": "suspicious", "issues": ["夜戏偏暗"],
                                       "metrics": {"luma": 12}}},
               "rerender": ["1-02-01"]}
        out = _tolerant_qc(raw)
        self.assertEqual(set(out), {"1-01-01"}, f"不该把 rerender 混进结果：{out}")
        self.assertEqual(out["1-01-01"]["verdict"], "suspicious",
                         "ok=True 但可疑必须保持 suspicious —— 画成绿灯就吞掉了人工复核档")
        self.assertEqual(out["1-01-01"]["issues"], ["夜戏偏暗"])
        self.assertEqual(out["1-01-01"]["metrics"]["luma"], 12)

    def test_verdict_derived_when_missing(self):
        out = _tolerant_qc({"1-01-01": {"ok": True}, "1-02-01": {"ok": False}})
        self.assertEqual(out["1-01-01"]["verdict"], "pass")
        self.assertEqual(out["1-02-01"]["verdict"], "fail")

    def test_verdict_from_metrics_and_bool_shorthand(self):
        out = _tolerant_qc({"results": {"a": {"ok": True, "metrics": {"verdict": "fail"}},
                                        "b": True, "c": False}})
        self.assertEqual(out["a"]["verdict"], "fail", "verdict 也接受 metrics 里的同名字段")
        self.assertEqual(out["b"], {"ok": True, "verdict": "pass", "issues": [], "metrics": {}})
        self.assertEqual(out["c"]["verdict"], "fail")

    def test_scalar_issue_becomes_list(self):
        out = _tolerant_qc({"a": {"ok": False, "issues": "静音"}})
        self.assertEqual(out["a"]["issues"], ["静音"], "标量 issues 要归一成列表")

    def test_list_of_shot_dicts(self):
        out = _tolerant_qc([{"shot": "1-01-01", "ok": True}, {"shot": "1-02-01", "ok": False}])
        self.assertEqual(out["1-02-01"]["verdict"], "fail")

    def test_garbage_in_empty_out(self):
        self.assertEqual(_tolerant_qc(None), {})
        self.assertEqual(_tolerant_qc("坏数据"), {})
        self.assertEqual(_tolerant_qc([1, 2, 3]), {})
        self.assertEqual(_tolerant_qc({"a": {"跟质检无关": 1}}), {}, "非结果形状的键要跳过")


class TestFrameConversion(unittest.TestCase):
    """帧网格契约（CONTRACTS.md 冻结）：frames = 17n+5，clamp [56,362]。"""

    def test_contract_values(self):
        for sec, want in ((5, 124), (10, 243), (15, 362)):
            self.assertEqual(seconds_to_frames(sec), want, f"契约自证值：seconds_to_frames({sec})")
            self.assertEqual(frames_for_seconds(sec), want, f"兜底实现也要对齐：frames_for_seconds({sec})")

    def test_clamp_and_bad_input(self):
        for fn in (seconds_to_frames, frames_for_seconds):
            self.assertEqual(fn(0), FRAME_MIN, f"{fn.__name__}(0)")
            self.assertEqual(fn(-3), FRAME_MIN, f"{fn.__name__}(-3)")
            self.assertEqual(fn("abc"), FRAME_MIN, f"{fn.__name__}('abc')")
            self.assertEqual(fn(None), FRAME_MIN, f"{fn.__name__}(None)")
            self.assertEqual(fn(999), FRAME_MAX, f"{fn.__name__}(999) 应夹到上限")

    def test_grid_shape(self):
        """合法区间内每个结果都落在 17n+5 网格上。"""
        for i in range(30, 363):
            s = i / 24
            f = seconds_to_frames(s)
            self.assertTrue(FRAME_MIN <= f <= FRAME_MAX, f"越界：sec={s} → {f}")
            self.assertEqual((f - 5) % 17, 0, f"不在网格上：sec={s} → {f}")

    def test_both_implementations_agree_on_integer_seconds(self):
        """
        双实现一致性（文档里点名的"重复实现"隐患）：
        表里的 sec 是整数秒，这个域上两个换算必须给出同一个帧数。
        """
        for sec in range(0, 31):
            self.assertEqual(
                frames_for_seconds(sec), seconds_to_frames(sec),
                f"sec={sec} 时双实现分歧：frames_for_seconds={frames_for_seconds(sec)} "
                f"vs seconds_to_frames={seconds_to_frames(sec)}")

    def test_both_implementations_agree_on_all_seconds(self):
        """
        双实现全域一致性回归锁。
        （曾为已知 bug：taskctl.frames_for_seconds 用 int(round()) 银行家舍入、
        shots.seconds_to_frames 用 floor(+0.5) 半向上，sec×fps 落半帧值时差一格 17 帧，
        共 9 处分歧。已修：兜底实现统一代理权威实现，本用例转正。）
        """
        diverge = []
        for i in range(0, 20 * 48 + 1):
            s = i / 48
            if frames_for_seconds(s) != seconds_to_frames(s):
                diverge.append((s, frames_for_seconds(s), seconds_to_frames(s)))
        self.assertEqual(diverge, [], f"双实现分歧 {len(diverge)} 处，例：{diverge[:3]}")

    def test_frames_to_sec_is_inverse_display(self):
        self.assertAlmostEqual(frames_to_sec(124), 124 / 24, places=3)


if __name__ == "__main__":
    unittest.main()
