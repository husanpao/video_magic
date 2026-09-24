"""
预算护栏（vm/budget.py）的单测：估算 / 判定 / 一次性放行 / 成本台账。

为什么先测它：预算是「花钱前唯一的刹车点」（E3）。它的两类错误都贵 ——
该拦不拦（)vB2 抽卡漏管）和不该拦乱拦（估算把队列按全项目渲染估，drainer 起不来）。
纯函数 + tmp 台账就能把这两类都钉死。
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from vm import budget
from vm.state import Project
from vm.tests import make_project, make_shot


class TestEstimate(unittest.TestCase):
    def setUp(self):
        # 每个用例一个干净的 tmp 项目根 —— 不碰 projects/ 下的真实项目
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.addCleanup(self._td.cleanup)

    def test_render_estimate_counts_only_missing_clips(self):
        """render 只算**还没产物**的镜头（有指纹跳过的不该再花 GPU）——真实增量口径。"""
        pdir = make_project(self.root, shots=[make_shot(i) for i in (1, 2, 3)])
        # 1-02-01 已有产物
        (pdir / "clips" / "1-02-01.mp4").write_bytes(b"x" * 128)
        est = budget.estimate(pdir, "render")
        self.assertEqual(est["shots"], 3)
        self.assertEqual(est["shots_pending"], 2, f"应只算没产物的 2 镜，实得 {est}")
        self.assertAlmostEqual(est["gpu_sec"], budget.COST["h3_sec_per_shot"] * 2, places=6)

    def test_only_filter_limits_scope(self):
        pdir = make_project(self.root, shots=[make_shot(i) for i in (1, 2, 3)])
        est = budget.estimate(pdir, "render", only=["1-01-01", "1-03-01"])
        self.assertEqual(est["shots"], 2, f"only 过滤后应只剩 2 镜，实得 {est}")
        self.assertEqual(est["shots_pending"], 2)

    def test_plan_estimate_includes_llm_tokens(self):
        """plan 阶段的成本主体是 LLM：按 次数×实测 tokens 计，且必须>0（B2 的教训：单价为 0=不设防）。"""
        pdir = make_project(self.root)
        est = budget.estimate(pdir, "plan")
        self.assertGreater(est["llm_calls"], 0, f"plan 应有 LLM 调用，实得 {est}")
        self.assertEqual(est["llm_tokens"], est["llm_calls"] * budget.COST["llm_tokens_per_call"])

    def test_chars_estimate_charges_per_character(self):
        """定妆按角色数计费（每角色一次出图+抽帧），不是按镜头数。"""
        pdir = make_project(self.root, shots=[
            make_shot(1, chars=["唐僧", "孙悟空"]),
            make_shot(2, chars=["唐僧"]),
            make_shot(3, chars=["孙悟空", "沙僧"]),
        ])
        est = budget.estimate(pdir, "chars")
        self.assertEqual(est["chars"], 3, f"角色去重后应 3 个，实得 {est}")
        self.assertAlmostEqual(est["gpu_sec"], budget.COST["chars_sec_per_run"] * 3, places=6)

    def test_count_adds_gacha_candidates(self):
        """抽卡张数 count 必须进估算 —— 连点抽卡是 GPU 出口（B2），估不到就拦不到。"""
        pdir = make_project(self.root)
        est0 = budget.estimate(pdir, "render")
        est = budget.estimate(pdir, "render", count=8)
        self.assertGreater(est["gpu_sec"], est0["gpu_sec"],
                           f"count=8 应比 count=0 贵，实得 {est['gpu_sec']} vs {est0['gpu_sec']}")

    def test_count_pricing_is_per_card_with_no_discount_hiding(self):
        """count 张**每张**按抽卡单价计（读它自己的单价表，锁语义不锁数值）：delta == count × 单价。"""
        pdir = make_project(self.root)
        unit = budget.JOB_UNIT_GPU_SEC["chars_gacha"]
        est0 = budget.estimate(pdir, "render")
        est8 = budget.estimate(pdir, "render", count=8)
        self.assertAlmostEqual(est8["gpu_sec"] - est0["gpu_sec"], round(unit * 8, 1), places=1,
                               msg=f"count=8 的差价应为 8×抽卡单价({unit})，实得 "
                                   f"{est8['gpu_sec'] - est0['gpu_sec']}")

    def test_gacha_stage_priced_by_cards_not_shots(self):
        """
        gacha 阶段按「角色 × 张数」计价，不按镜头（B2 修复点：only 给的是角色名，
        走镜头口径会滤成 0 镜 → 成本恒 0 → 连点 24 张永不触发护栏）。
        """
        pdir = make_project(self.root)
        est = budget.estimate(pdir, "gacha", only=["唐僧", "孙悟空"], count=3)
        self.assertEqual(est["chars"], 2)
        self.assertEqual(est["cards"], 6, f"2 角色 × 3 张 = 6 张，实得 {est}")
        self.assertAlmostEqual(est["gpu_sec"],
                               round(budget.JOB_UNIT_GPU_SEC["chars_gacha"] * 6, 1), places=1,
                               msg=f"6 张 × 抽卡单价，实得 {est['gpu_sec']}")

    def test_queue_estimate_is_per_job_not_whole_project(self):
        """
        队列 drainer 的估算是「排队作业 × 单图成本」——
        曾经它按全项目渲染估算 → 超预算 → NeedsApproval → drainer 永远起不来（实测卡死过）。
        """
        pdir = make_project(self.root)
        proj = Project(pdir)
        from vm import queue as Q
        for i in range(3):
            Q.add(proj, "asset_gen", {"kind": "scene", "id": f"S{i}", "n": 2})
        est = budget.estimate(pdir, "queue")
        self.assertEqual(est["stage"], "queue")
        self.assertEqual(est["jobs_pending"], 3, f"实得 {est}")
        self.assertLess(est["gpu_sec"], budget.COST["h3_sec_per_shot"] * 10,
                        "队列估算绝不能按整项目渲染口径来")

    def test_all_stages_have_positive_gpu_or_llm_guard(self):
        """
        护栏覆盖全部 GPU 出口：任何"会花 GPU/LLM"的阶段，单价不能是 0 死代码（B2）。
        抽卡（gacha）单价曾经为 0 —— 这条断言在 T2(task-4) 改完单价表后生效；
        现阶段 chars_sec_per_run>0 已能锁住"出图类必须有价"。
        """
        self.assertGreater(budget.COST["chars_sec_per_run"], 0)
        self.assertGreater(budget.COST["h3_sec_per_shot"], 0)
        self.assertGreater(budget.COST["qwen_sec_per_shot"], 0)


class TestCheck(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.addCleanup(self._td.cleanup)

    def _est(self, *, gpu_min=1.0, shots_pending=2, llm_tokens=0, stage="render"):
        return {"stage": stage, "shots": shots_pending, "shots_pending": shots_pending,
                "gpu_min": gpu_min, "gpu_sec": gpu_min * 60, "llm_tokens": llm_tokens}

    def test_no_budget_means_never_block(self):
        """向后兼容：project.json 没配 budget → 永远放行。"""
        pdir = make_project(self.root)  # 不写 project.json
        chk = budget.check(pdir, self._est(gpu_min=9999))
        self.assertFalse(chk["needs_approval"], f"没配护栏不该拦，实得 {chk}")
        self.assertIn("未启用", chk["note"])

    def test_over_gpu_minutes_needs_approval_with_three_answers(self):
        """放行载荷必须直接回答三问：已花 / 卡在哪 / 再放行多少（调研硬要求）。"""
        pdir = make_project(self.root, budget={"max_gpu_minutes": 1})
        chk = budget.check(pdir, self._est(gpu_min=5, stage="render"))
        self.assertTrue(chk["needs_approval"])
        msg = chk["message"]
        for must in ("已花", "卡在", "再放行"):
            self.assertIn(must, msg, f"载荷缺「{must}」这一问：{msg}")
        self.assertIn("render", msg, f"要说清卡在哪个阶段：{msg}")
        self.assertTrue(chk["reasons"], "要有人话原因清单")

    def test_limits_are_strictly_greater_than(self):
        """上限语义：正好等于上限不拦（超了才拦）。"""
        pdir = make_project(self.root, budget={"max_gpu_minutes": 1,
                                               "max_shots_per_run": 2,
                                               "max_llm_tokens": 100})
        chk = budget.check(pdir, self._est(gpu_min=1, shots_pending=2, llm_tokens=100))
        self.assertFalse(chk["needs_approval"], f"等于上限不该拦，实得 {chk['reasons']}")
        chk2 = budget.check(pdir, self._est(gpu_min=1, shots_pending=3, llm_tokens=101))
        self.assertTrue(chk2["needs_approval"])
        self.assertEqual(len(chk2["reasons"]), 2, f"超了两条上限应给两条原因：{chk2['reasons']}")

    def test_daily_limit_counts_already_spent(self):
        """每日上限要把**今天已花**的算进去（台账先行）。"""
        pdir = make_project(self.root, budget={"max_gpu_minutes_per_day": 10})
        budget.record(pdir, "render", {"gpu_sec": 9 * 60, "llm_tokens": 0, "shots": 5})
        chk = budget.check(pdir, self._est(gpu_min=2))
        self.assertTrue(chk["needs_approval"], f"9+2>10 应拦，实得 {chk['reasons']}")
        self.assertIn("今日已用", chk["reasons"][0])

    def test_approval_is_one_shot(self):
        """一次性放行：被下一个任务消费掉就没了（防永久放行）。"""
        pdir = make_project(self.root)
        self.assertIsNone(budget.pending_approval(pdir))
        budget.grant_approval(pdir, by="tester", stage="render")
        self.assertIsNotNone(budget.pending_approval(pdir))
        got = budget.consume_approval(pdir)
        self.assertEqual(got["by"], "tester")
        self.assertIsNone(budget.consume_approval(pdir), "第二次消费必须拿不到")


class TestJobEstimate(unittest.TestCase):
    """drainer 逐作业过护栏用的单价口径（S4）：任何 GPU 出口都必须有价、价随张数走。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.addCleanup(self._td.cleanup)

    def test_every_job_kind_has_positive_price(self):
        from vm import queue as Q
        for kind in Q.JOB_KINDS:
            e2 = budget.job_estimate({"kind": kind, "args": {"n": 2}})
            e1 = budget.job_estimate({"kind": kind, "args": {"n": 1}})
            self.assertGreater(e2["gpu_sec"], 0, f"{kind} 的单价不能是 0（B2：零单价=不设防）")
            self.assertAlmostEqual(e2["gpu_sec"], round(e1["gpu_sec"] * 2, 1), places=1,
                                   msg=f"{kind} 的成本必须随张数线性走")

    def test_job_estimate_feeds_check_with_kind_level_stage(self):
        """超预算时审批框要能看出卡在哪种作业上（stage=queue:<kind>）。"""
        pdir = make_project(self.root, budget={"max_gpu_minutes": 0.01})
        est = budget.job_estimate({"kind": "chars_gacha", "args": {"n": 2}})
        chk = budget.check(pdir, est)
        self.assertTrue(chk["needs_approval"], f"2 张定妆远超 0.01 分钟上限：{chk['reasons']}")
        self.assertIn("queue:chars_gacha", chk["message"],
                      f"要说清卡在哪种作业：{chk['message'][:80]}")
        for must in ("已花", "卡在", "再放行"):
            self.assertIn(must, chk["message"])


class TestLedger(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.addCleanup(self._td.cleanup)

    def test_record_and_spent_round_trip(self):
        pdir = make_project(self.root)
        budget.record(pdir, "render", {"gpu_sec": 12.5, "llm_tokens": 1700, "shots": 3})
        budget.record(pdir, "plan", {"gpu_sec": 0, "llm_tokens": 3400, "shots": 3})
        sp = budget.spent(pdir)
        self.assertEqual(sp["runs"], 2)
        self.assertAlmostEqual(sp["gpu_sec"], 12.5, places=6)
        self.assertEqual(sp["llm_tokens"], 5100)
        self.assertEqual(sp["shots"], 6)

    def test_ledger_tolerates_bad_lines(self):
        """台账不该因为一行写坏就整份读不出来（对账场景，坏行跳过）。"""
        pdir = make_project(self.root)
        budget.record(pdir, "render", {"gpu_sec": 1, "llm_tokens": 0, "shots": 1})
        f = budget.ledger_path(pdir)
        with open(f, "a", encoding="utf-8") as fh:
            fh.write("{坏行,不是 json\n")
        budget.record(pdir, "render", {"gpu_sec": 2, "llm_tokens": 0, "shots": 1})
        sp = budget.spent(pdir)
        self.assertEqual(sp["runs"], 2, f"坏行应跳过、好行应保留，实得 {sp}")
        self.assertAlmostEqual(sp["gpu_sec"], 3.0, places=6)

    def test_spent_excludes_stale_entries(self):
        """spent 只算窗口内（默认今天）的台账 —— 昨天的花费不该顶今天的额度。"""
        pdir = make_project(self.root)
        f = budget.ledger_path(pdir)
        f.parent.mkdir(parents=True, exist_ok=True)
        old = {"at": time.time() - 3 * 86400, "stage": "render", "gpu_sec": 999,
               "llm_tokens": 999, "shots": 999}
        f.write_text(json.dumps(old) + "\n", encoding="utf-8")
        sp = budget.spent(pdir)
        self.assertEqual(sp["gpu_sec"], 0, f"三天前的记录不该算进今天，实得 {sp}")


if __name__ == "__main__":
    unittest.main()
