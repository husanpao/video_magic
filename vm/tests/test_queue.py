"""
作业队列（vm/queue.py）的单测：状态机语义 + 并发写不丢作业。

并发用例为什么现在是 skip：queue.json 目前是无锁的读-改-写（B3，web 线程 +
drainer 进程并发写同一文件会丢作业）。task-4（T2）上锁落地后本用例自动生效 ——
激活条件是 vm/queue.py 里出现真实加锁手段（flock/Lock），不是靠人记得来解锁。
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from vm import queue as Q
from vm.state import Project
from vm.tests import make_project


def _queue_has_lock() -> bool:
    """检测 task-4 是否已给 queue.json 上锁（flock / 任意 Lock 句柄）。"""
    src = (Path(__file__).resolve().parent.parent / "queue.py").read_text(encoding="utf-8")
    return bool(re.search(r"fcntl|flock|Lock\(\)|filelock", src))


class TestJobStateMachine(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self.proj = Project(make_project(self.root))

    def test_add_claim_finish_flow(self):
        j = Q.add(self.proj, "asset_gen", {"kind": "scene", "id": "S1", "n": 2})
        self.assertEqual(j.status, Q.STATUS_PENDING)
        self.assertEqual(j.label, "素材概念图", "label 要给人看，UI 直接显示")

        got = Q.claim_next(self.proj)
        self.assertEqual(got.id, j.id)
        self.assertEqual(got.status, Q.STATUS_RUNNING, "claim 后必须标 running（UI 要显示在跑哪个）")

        Q.finish(self.proj, j.id, ok=True, note="新出 2 张")
        jobs = Q.read_all(self.proj)
        self.assertEqual(jobs[0].status, Q.STATUS_DONE)
        self.assertEqual(jobs[0].note, "新出 2 张")
        self.assertIsNone(Q.claim_next(self.proj), "没有 pending 时 claim 应返回 None")

    def test_claim_is_fifo(self):
        ids = [Q.add(self.proj, "storyboard", {"id": f"1-0{i}-01", "n": 1}).id for i in range(1, 4)]
        self.assertEqual([Q.claim_next(self.proj).id for _ in range(3)], ids,
                         "排队要按先来后到，不然用户连点的顺序毫无意义")

    def test_unknown_kind_rejected(self):
        with self.assertRaises(ValueError):
            Q.add(self.proj, "no_such_kind", {})

    def test_cancel_only_pending(self):
        j1 = Q.add(self.proj, "asset_gen", {"kind": "scene", "id": "S1", "n": 1})
        j2 = Q.add(self.proj, "asset_gen", {"kind": "scene", "id": "S2", "n": 1})
        Q.claim_next(self.proj)   # j1 变 running
        self.assertFalse(Q.cancel(self.proj, j1.id), "正在跑的不能算取消（只能按停止）")
        self.assertTrue(Q.cancel(self.proj, j2.id))
        self.assertEqual(Q.read_all(self.proj)[1].status, Q.STATUS_CANCELED)

    def test_cancel_pending_and_clear_finished(self):
        for i in range(3):
            Q.add(self.proj, "storyboard", {"id": f"S{i}", "n": 1})
        j = Q.claim_next(self.proj)
        Q.finish(self.proj, j.id, ok=False, note="崩了")
        self.assertEqual(Q.cancel_pending(self.proj), 2, "只剩 2 条 pending 可取消")
        self.assertEqual(Q.clear_finished(self.proj), 3, "done/failed/canceled 共 3 条可清")
        self.assertEqual(Q.read_all(self.proj), [], "pending/running 才保留")

    def test_note_truncated_to_400(self):
        """失败原因可能拖着整页 traceback —— 截断，别把队列文件撑爆。"""
        j = Q.add(self.proj, "storyboard", {"id": "S1", "n": 1})
        Q.claim_next(self.proj)
        Q.finish(self.proj, j.id, ok=False, note="x" * 1000)
        self.assertEqual(len(Q.read_all(self.proj)[0].note), 400)

    def test_stats_counts_and_next_label(self):
        Q.add(self.proj, "chars_gacha", {"name": "唐僧", "n": 2})
        Q.add(self.proj, "storyboard", {"id": "1-01-01", "n": 1})
        st = Q.stats(self.proj)
        self.assertEqual(st["total"], 2)
        self.assertEqual(st["pending"], 2)
        self.assertEqual(st["next_label"], "角色抽卡", "next_label 让用户知道下一个跑的是啥")

    def test_release_to_pending_is_wait_not_fail(self):
        """
        「等批准」不是「失败」：预算不足暂停的作业要放回 pending，不能标 failed
        骗用户手动重加。只对 running 生效，重复释放要返回 False。
        """
        j = Q.add(self.proj, "asset_gen", {"kind": "scene", "id": "S1", "n": 2})
        self.assertIsNotNone(Q.claim_next(self.proj))
        self.assertTrue(Q.release_to_pending(self.proj, j.id), "running 的作业必须能放回")
        job = Q.read_all(self.proj)[0]
        self.assertEqual(job.status, Q.STATUS_PENDING)
        self.assertEqual(job.started_at, 0.0, "放回时要清 started_at，别留'跑过一半'的假象")
        self.assertFalse(Q.release_to_pending(self.proj, j.id), "已是 pending 不该重复放回")
        self.assertFalse(Q.release_to_pending(self.proj, "no-such-job"))

    def test_write_all_caps_at_200(self):
        """队列文件不无限增长：只留最近 200 条。"""
        for i in range(210):
            Q.add(self.proj, "storyboard", {"id": f"S{i}", "n": 1})
        self.assertEqual(len(Q.read_all(self.proj)), 200)

    def test_corrupt_queue_file_reads_empty(self):
        (self.proj.state_dir / Q.QUEUE_FILENAME).write_text("{坏数据", encoding="utf-8")
        self.assertEqual(Q.read_all(self.proj), [], "坏文件不能把 UI 打挂")
        self.assertEqual(Q.stats(self.proj)["total"], 0)


@unittest.skipUnless(_queue_has_lock(),
                     "task-4 未落地：queue.json 仍是无锁读改写（B3），并发语义用例待上锁后自动生效")
class TestConcurrentSafety(unittest.TestCase):
    """
    并发正确性（B3 的回归闸）：web 是多线程服务器、drainer 是另一个进程，
    同时动 queue.json 时「读-改-写」互相覆盖就会丢作业。
    """

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self.proj = Project(make_project(self.root))

    def test_concurrent_add_loses_nothing(self):
        import threading
        n_threads, per = 8, 10

        def worker(t):
            for i in range(per):
                Q.add(self.proj, "storyboard", {"id": f"T{t}-{i}", "n": 1})

        ts = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        jobs = Q.read_all(self.proj)
        self.assertEqual(len(jobs), n_threads * per,
                         f"并发入队丢了作业：应 {n_threads * per} 条，剩 {len(jobs)} 条")

    def test_concurrent_claim_never_double_claims(self):
        import threading
        for i in range(20):
            Q.add(self.proj, "storyboard", {"id": f"S{i}", "n": 1})
        claimed: list = []
        guard = threading.Lock()

        def worker():
            while True:
                j = Q.claim_next(self.proj)
                if j is None:
                    return
                with guard:
                    claimed.append(j.id)

        ts = [threading.Thread(target=worker) for _ in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(len(claimed), 20, f"claim 数不对：{len(claimed)}")
        self.assertEqual(len(set(claimed)), 20, f"同一个作业被领了多次：{claimed}")


class TestDrainBudgetGate(unittest.TestCase):
    """
    drainer 逐作业过护栏（S4）：每作业开跑前 check，超了放回 pending 并停（等批准≠失败）；
    完成后按作业实际张数记账（start_async 对 queue 不再预记账，防重复计费）。
    出图动作一律打桩 —— 这里测的是「队列 × 预算」的编排，不是 ComfyUI。
    """

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self.calls = []

        def fake_gen(proj, kind, aid, n=2, log=None, **kw):
            self.calls.append((kind, aid, n))
            return [f"{aid}_{i}.png" for i in range(n)]

        import vm.assets as A
        self._old_gen = A.gen_candidates
        A.gen_candidates = fake_gen
        self.addCleanup(setattr, A, "gen_candidates", self._old_gen)

    def test_drain_records_one_ledger_entry_per_job(self):
        pdir = make_project(self.root)   # 没配 budget = 不拦
        proj = Project(pdir)
        Q.add(proj, "asset_gen", {"kind": "scene", "id": "S1", "n": 2})
        Q.add(proj, "asset_gen", {"kind": "prop", "id": "P1", "n": 1})
        from vm import budget as B
        res = Q.drain(proj, log=lambda m: None)
        self.assertEqual((res["done"], res["failed"]), (2, 0), f"实得 {res}")
        self.assertEqual(len(self.calls), 2, "两个作业都要真跑（桩记录调用）")
        sp = B.spent(pdir)
        self.assertEqual(sp["runs"], 2, f"每作业记一笔（queue:<kind>），实得台账 {sp}")
        self.assertAlmostEqual(sp["gpu_sec"],
                               round(B.JOB_UNIT_GPU_SEC["asset_gen"] * 3, 1), places=1,
                               msg="按作业实际张数记账（2+1 张），不是整队列预估")

    def test_drain_over_budget_releases_job_and_stops(self):
        """超预算 → 当前作业放回 pending、后面的不跑、**不记账**（没花的钱不记）。"""
        pdir = make_project(self.root, budget={"max_gpu_minutes": 0.01})
        proj = Project(pdir)
        j1 = Q.add(proj, "asset_gen", {"kind": "scene", "id": "S1", "n": 2})
        Q.add(proj, "asset_gen", {"kind": "scene", "id": "S2", "n": 2})
        from vm import budget as B
        res = Q.drain(proj, log=lambda m: None)
        self.assertEqual(res["done"], 0, f"超预算不该开跑：{res}")
        self.assertEqual(self.calls, [], "出图桩绝不该被调用")
        jobs = {j.id: j for j in Q.read_all(proj)}
        self.assertEqual(jobs[j1.id].status, Q.STATUS_PENDING, "等批准≠失败：要放回 pending")
        self.assertEqual(jobs[j1.id].started_at, 0.0)
        self.assertEqual(B.spent(pdir)["runs"], 0, "没跑的作业不许记账")

    def test_one_shot_approval_unblocks_exactly_one_job(self):
        """一次性放行只放行一个作业：第二个照样被拦（防止批准变永久通行证）。"""
        pdir = make_project(self.root, budget={"max_gpu_minutes": 0.01})
        proj = Project(pdir)
        j1 = Q.add(proj, "asset_gen", {"kind": "scene", "id": "S1", "n": 2})
        j2 = Q.add(proj, "asset_gen", {"kind": "scene", "id": "S2", "n": 2})
        from vm import budget as B
        Q.drain(proj, log=lambda m: None)          # 都被拦
        B.grant_approval(pdir, by="tester")
        res = Q.drain(proj, log=lambda m: None)
        self.assertEqual(res["done"], 1, f"放行一次应恰好跑掉一个：{res}")
        jobs = {j.id: j for j in Q.read_all(proj)}
        self.assertEqual(jobs[j1.id].status, Q.STATUS_DONE)
        self.assertEqual(jobs[j2.id].status, Q.STATUS_PENDING, "第二次没放行必须继续拦")
        self.assertEqual(B.spent(pdir)["runs"], 1, "只记真正跑掉的那个作业")


if __name__ == "__main__":
    unittest.main()
