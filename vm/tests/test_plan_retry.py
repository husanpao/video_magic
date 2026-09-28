"""
test_plan_retry.py —— 拆镜重试策略的回归锁（2026-09-28 事故）。

事故经过（真事，日志在案）：新项目、零角色卡 → 第一稿拆镜 LLM 把「王梅」写进 chars
→ `_add_cards_for` 补卡成功（**交付物已到手**）→ 代码随即追加一条「补卡后本稿未出镜」
的软意见，而当时它用的是 `__fix__` 前缀 → 触发第二稿重写 → 第二稿被 max_tokens=8192
截断 → PlanError → **整章拆镜失败**，本来已经合格的第一稿一起丢掉（rc=1）。

修法三条，本文件逐条上锁：
  1. 补卡后的软意见改用 `__note__`：只记录、**绝不**触发重写；
  2. 重写只由 硬问题(hard) 与 必须重写的问题(`__fix__`) 驱动；
  3. 第一稿硬问题已清零时先存快照，第二稿失败**自动回退**，不再白丢一稿。

为什么这里用源码级断言：策略嵌在 3000 行的 `plan_chapter` 重试循环里，行为级测试要伪造
整套 LLM 交互（风格/场景/道具/补卡/拆镜表五类调用），成本远高于收益；而这三条是**接线**，
接错一次就是整章失败 + 用户白等，值得用最直接的方式钉死。行为侧的 `__fix__` 语义
（对白丢失重写后只告警不阻断）由 plan 的既有用例覆盖。
"""

from __future__ import annotations

import inspect
import unittest

from vm import plan as P


class TestCardNoteNotARewriteTrigger(unittest.TestCase):
    """补卡后的软意见必须只记录 —— 这条接错就是整章失败。"""

    def setUp(self) -> None:
        self.src = inspect.getsource(P.plan_chapter)

    def test_card_issue_uses_note_prefix(self) -> None:
        self.assertIn('f"__note__「{n}」', self.src,
                      "补卡后的提示必须用 __note__（只记录）；用 __fix__ 会触发重写")

    def test_card_issue_no_longer_uses_fix_prefix(self) -> None:
        self.assertNotIn('f"__fix__「{n}」', self.src,
                         "2026-09-28 事故的元凶就是这个前缀，不得回归")

    def test_rewrite_is_driven_by_hard_and_fix_only(self) -> None:
        self.assertIn("if not hard and not fix:", self.src,
                      "没有硬问题且没有 __fix__ 时必须直接接受本稿（不再为软意见重写）")
        self.assertIn("feedback = \"\\n\".join(f\"- {i}\" for i in (hard + fix))", self.src)


class TestRewriteFailureFallsBack(unittest.TestCase):
    """第二稿失败（最常见：max_tokens 截断）不能连累已合格的第一稿。"""

    def setUp(self) -> None:
        self.src = inspect.getsource(P.plan_chapter)

    def test_snapshot_taken_when_first_draft_has_no_hard_issue(self) -> None:
        self.assertIn("if not hard:", self.src)
        self.assertIn("fallback = {\"rows\": rows, \"cards_out\": cards_out,", self.src,
                      "第一稿硬问题清零时必须存快照，否则第二稿失败就没得回退")

    def test_truncation_on_rewrite_uses_fallback(self) -> None:
        self.assertIn("except PlanError as e:", self.src)
        self.assertIn("if fallback is None:", self.src, "没有快照才允许真的抛错")
        self.assertIn("回退到第 1 稿继续", self.src)

    def test_fallback_discards_nothing_user_visible(self) -> None:
        # 回退时要把"哪些问题还没解决"写进 stats.warnings，用户能在 UI 的审计/日志里看到
        self.assertIn("已回退第 1 稿", self.src)
        self.assertIn('snap = [i[len("__fix__") :] for i in fallback["issues"]', self.src)


class TestAllowNewCharactersDefault(unittest.TestCase):
    """「未设置 = 默认允许」是三态语义的关键：设错一次，新项目/新章节就整章拆不动。"""

    def setUp(self) -> None:
        self.src = inspect.getsource(P.plan_chapter)

    def test_unset_means_allow(self) -> None:
        self.assertIn(
            "allow_new = True if allow_new_raw is None else bool(allow_new_raw)", self.src,
            "allow_new_characters 未设置时必须默认允许建卡，否则零卡项目/新角色章节必然失败",
        )

    def test_old_false_default_is_gone(self) -> None:
        self.assertNotIn('pcfg.get("allow_new_characters", False)', self.src,
                         "2026-09-28 事故的默认值，不得回归")


class TestIssuePrefixContract(unittest.TestCase):
    """四档前缀的契约（无前缀=硬 / __fix__ / __note__ / __style__）。"""

    def test_validator_documents_four_tiers(self) -> None:
        doc = P._validate_table.__doc__ or ""
        for token in ("__fix__", "__note__", "__style__"):
            self.assertIn(token, doc, f"_validate_table 文档必须写清 {token} 的处置方式")


if __name__ == "__main__":
    unittest.main()
