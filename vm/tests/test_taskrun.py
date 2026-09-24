"""
任务基建（pipeline.py worker + taskctl.start/stop）的进程级单测。

只用 fake_sleep 替身：不起 GPU、不连 ComfyUI、不烧一分钱 —— 但**跑的是真代码**：
真的派生 pipeline.py 子进程、真的走项目锁/task.json/run.log、真的发 SIGTERM。

为什么要费劲做垫片（shim）：worker 子进程里 vm.taskctl.PROJECTS_DIR 是全局默认值，
start_async(root=tmp) 只影响父进程 —— 子进程照样把项目解析到用户真实的 projects/ 下。
所以测试在 tmp 里放一个**同名 pipeline.py 垫片**（文件名必须叫 pipeline.py：
taskctl.is_our_worker 靠 cmdline 里的 "pipeline.py + --_worker + 项目名" 验证 worker
身份，改了名 stop() 会拒绝给自己的 worker 发信号，测的就不是真实停止链路了），
垫片只把 PROJECTS_DIR 指到 tmp 再转交真正的 pipeline.main()。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from vm import taskctl
from vm.tests import REPO_ROOT, make_project

# 垫片：与真 pipeline.py 同名（理由见模块头）。VM_TEST_STUCK=1 时把替身阶段换成
# "永远卡住"，用来测看门狗（等价于阶段函数卡在 ffmpeg 这类阻塞调用里出不来）。
SHIM = '''#!/usr/bin/env python3
import os, sys, time
from pathlib import Path
sys.path.insert(0, os.environ["VM_TEST_WORKSPACE"])
from vm import taskctl
taskctl.PROJECTS_DIR = Path(os.environ["VM_TEST_PROJECTS_ROOT"])
if os.environ.get("VM_TEST_KILL_GRACE"):
    taskctl.WORKER_KILL_GRACE = float(os.environ["VM_TEST_KILL_GRACE"])
import pipeline
if os.environ.get("VM_TEST_STUCK") == "1":
    def _stuck(seconds, should_stop):
        time.sleep(3600)  # 永不返回：模拟卡死在阻塞调用里的阶段函数
    pipeline._fake_sleep_stage = _stuck
raise SystemExit(pipeline.main(sys.argv[1:]))
'''


class TaskInfraTest(unittest.TestCase):
    """基类：tmp 项目根 + 垫片 + 全局指针打补丁，用完恢复。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name) / "projects"
        self.root.mkdir(parents=True)
        self.pdir = make_project(self.root, "p1")
        # 垫片落 tmp，文件名保持 pipeline.py（is_our_worker 的身份校验要看它）
        self.shim = Path(self._td.name) / "pipeline.py"
        self.shim.write_text(SHIM, encoding="utf-8")

        # 父进程（测试自己）与子进程（垫片）都把 projects 根指到 tmp
        self._saved = {
            "PROJECTS_DIR": taskctl.PROJECTS_DIR,
            "PIPELINE": taskctl.PIPELINE,
        }
        taskctl.PROJECTS_DIR = self.root
        taskctl.PIPELINE = self.shim
        os.environ["VM_TEST_WORKSPACE"] = str(REPO_ROOT)
        os.environ["VM_TEST_PROJECTS_ROOT"] = str(self.root)
        os.environ.pop("VM_TEST_STUCK", None)
        os.environ.pop("VM_TEST_KILL_GRACE", None)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        # 兜底：任何用例失败也不能把 fake_sleep worker 留在世上
        try:
            task = taskctl.read_task("p1", self.root)
            if task and task.get("pid"):
                try:
                    os.kill(int(task["pid"]), 9)
                except OSError:
                    pass
        except Exception:
            pass
        taskctl.PROJECTS_DIR = self._saved["PROJECTS_DIR"]
        taskctl.PIPELINE = self._saved["PIPELINE"]
        for k in ("VM_TEST_WORKSPACE", "VM_TEST_PROJECTS_ROOT",
                  "VM_TEST_STUCK", "VM_TEST_KILL_GRACE"):
            os.environ.pop(k, None)
        self._td.cleanup()

    def _log(self) -> str:
        return taskctl.log_path("p1", self.root).read_text(encoding="utf-8")

    def _wait_exit(self, handle, timeout=20.0):
        rc = handle.wait(timeout)
        self.assertIsNotNone(rc, f"worker {timeout}s 内没退出 —— 停止链路失效")
        return rc


class TestStartAndFinish(TaskInfraTest):
    def test_fake_sleep_runs_to_natural_end(self):
        """替身任务自然跑完：rc=0、日志有痕迹、task.json 有退出码。"""
        h = taskctl.start_async("p1", "render", fake_sleep=1.0, root=self.root)
        self.assertGreater(h.pid, 0)
        rc = self._wait_exit(h)
        self.assertEqual(rc, 0, f"自然结束应 rc=0，实得 {rc}；日志：{self._log()[-500:]}")
        self.assertIn("替身任务自然结束", self._log())
        task = taskctl.read_task("p1", self.root)
        self.assertFalse(taskctl.task_running(task))
        self.assertFalse(task["stopped"], "自然结束不该被标记为停止")

    def test_run_log_appends_across_runs(self):
        """日志是追加不是截断：高频小任务（点一下起一个）下一次点击不该清掉上一次的日志。"""
        h = taskctl.start_async("p1", "render", fake_sleep=0.3, root=self.root)
        self._wait_exit(h)
        h2 = taskctl.start_async("p1", "render", fake_sleep=0.3, root=self.root)
        self._wait_exit(h2)
        self.assertEqual(self._log().count("替身任务自然结束"), 2,
                         f"两次运行的日志都该在：{self._log()[-400:]}")


class TestConcurrencyMutex(TaskInfraTest):
    def test_second_start_is_rejected_while_running(self):
        """同项目互斥：已有任务在跑时再来一个必须 TaskBusy（CLI/Web 同一咽喉）。"""
        h = taskctl.start_async("p1", "render", fake_sleep=3.0, root=self.root)
        try:
            with self.assertRaises(taskctl.TaskBusy):
                taskctl.start_async("p1", "qc", fake_sleep=3.0, root=self.root)
        finally:
            taskctl.stop("p1", root=self.root, wait=5)
            self._wait_exit(h)

    def test_mutex_releases_after_finish(self):
        h = taskctl.start_async("p1", "render", fake_sleep=0.3, root=self.root)
        self._wait_exit(h)
        # 结束后必须能立刻再起 —— 互斥锁不能"用完不放"
        h2 = taskctl.start_async("p1", "render", fake_sleep=0.3, root=self.root)
        self._wait_exit(h2)


class TestStopSignal(TaskInfraTest):
    def test_stop_terminates_running_worker(self):
        """停止 = 对精确 pid 发 SIGTERM，替身任务收到就退（rc=143），日志留痕。"""
        h = taskctl.start_async("p1", "render", fake_sleep=30, root=self.root)
        time.sleep(0.8)  # 等 worker 装好信号处理器
        r = taskctl.stop("p1", root=self.root, wait=8)
        self.assertTrue(r["ok"], f"stop 应成功：{r}")
        self.assertEqual(r["signal"], "SIGTERM")
        rc = self._wait_exit(h, timeout=10)
        self.assertEqual(rc, 143, f"被停止的 worker 应 rc=143，实得 {rc}")
        self.assertIn("收到停止", self._log())
        task = taskctl.read_task("p1", self.root)
        self.assertTrue(task["stopped"])
        self.assertFalse(taskctl.task_running(task))

    def test_stop_without_task_is_noop(self):
        r = taskctl.stop("p1", root=self.root, wait=1)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "no_task")

    def test_stop_refuses_pid_of_foreign_process(self):
        """
        pid 防误杀：pid 被系统回收给了别的进程时必须拒绝发信号
        （绝不 pkill -f / 按端口杀的同一条纪律：一枪只能打在自己的 worker 上）。
        """
        decoy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            taskctl.write_task(self.pdir, {
                "project": "p1", "stage": "render", "pid": decoy.pid,
                "started_at": int(time.time()), "stopped": False,
                "finished_at": None, "exit_code": None,
            })
            r = taskctl.stop("p1", root=self.root, wait=1)
            self.assertFalse(r["ok"], f"命令行不像 worker 必须拒绝，实得 {r}")
            self.assertEqual(r["reason"], "pid_reused")
            self.assertIsNone(decoy.poll(), "别人的进程必须毫发无损")
        finally:
            decoy.kill()
            decoy.wait()

    def test_double_signal_exits_immediately(self):
        """第二次收到停止信号立即退出（不等看门狗）—— 用户连按两次停止要立竿见影。"""
        os.environ["VM_TEST_STUCK"] = "1"
        os.environ["VM_TEST_KILL_GRACE"] = "30"   # 看门狗放到 30s，排除它的功劳
        h = taskctl.start_async("p1", "render", fake_sleep=30, root=self.root)
        time.sleep(0.8)
        t0 = time.time()
        os.kill(h.pid, 15)
        time.sleep(0.4)
        os.kill(h.pid, 15)
        rc = self._wait_exit(h, timeout=8)
        self.assertEqual(rc, 143)
        self.assertLess(time.time() - t0, 5, "双信号应秒退，不该等 30s 看门狗")
        self.assertIn("再次收到停止信号", self._log())


class TestWatchdog(TaskInfraTest):
    def test_stuck_stage_is_force_killed_after_grace(self):
        """
        看门狗：停止信号到了但阶段函数卡住（阻塞调用出不来）→ 宽限期后强退。
        「停止」必须是确定性的；渲染检查点提交时已落盘，强退不丢进度。
        （把宽限期从契约的 8s 调小到 1.5s 只为提速，测的是机制不是数字。）
        """
        os.environ["VM_TEST_STUCK"] = "1"
        os.environ["VM_TEST_KILL_GRACE"] = "1.5"
        h = taskctl.start_async("p1", "render", fake_sleep=60, root=self.root)
        time.sleep(0.8)
        # wait 要比宽限期短：回来时看门狗还没到期，卡死的进程应该还在（alive=True）
        r = taskctl.stop("p1", root=self.root, wait=0.5)
        self.assertTrue(r["ok"], f"SIGTERM 必须发出去：{r}")
        self.assertTrue(r["alive"], "卡死的替身此刻应该还活着（看门狗还没到期）")
        rc = self._wait_exit(h, timeout=12)
        self.assertEqual(rc, 143, f"看门狗强退应 rc=143，实得 {rc}")
        self.assertIn("强制退出", self._log())

    def test_watchdog_grace_contract(self):
        """契约值：SIGTERM 后 8s 看门狗（文档/功能梳理里冻结的数字）。"""
        self.assertEqual(taskctl.WORKER_KILL_GRACE, 8.0)


class TestQueueStageNoPreCharge(TaskInfraTest):
    def test_queue_stage_no_longer_pre_records_ledger(self):
        """
        queue 阶段不预记账（task-4 语义）：启动时的整队列预估只用于预算 check，
        真正的记账由 drain 逐作业按实际张数记 —— 两边都记「今日已用」会直接翻倍。
        """
        from vm import budget as B
        self.assertEqual(B.spent(self.pdir)["runs"], 0, "前置：台账为空")
        h = taskctl.start_async("p1", "queue", root=self.root)
        rc = self._wait_exit(h, timeout=25)
        self.assertEqual(rc, 0, f"空队列应顺利跑完，实得 rc={rc}；日志：{self._log()[-300:]}")
        self.assertIn("队列处理完毕", self._log())
        self.assertEqual(B.spent(self.pdir)["runs"], 0,
                         "queue 启动不许预记账（逐作业记账在 drain，重复计费会让额度翻倍）")


if __name__ == "__main__":
    unittest.main()
