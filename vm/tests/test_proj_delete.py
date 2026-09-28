"""
P4：项目删除必须**可逆**，而且三道闸一道都不能少。

`projects/` 在 .gitignore 里 —— 用户的内容没有第二份拷贝。所以
"删除项目"这个动作的验收标准不是"文件没了"，而是：
  · 内容完整地躺在回收站里，能被列出来、能被放回原位；
  · 确认名不匹配 / 有任务在跑 / 想删 projects/ 之外的目录 ⇒ 一律不动手；
  · 恢复时目标重名 ⇒ 拒绝，**绝不覆盖**。
每条断言都配一个"如果实现偷懒会怎样"的反例，避免写成空过的样子货。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from vm import proj as P
from vm import taskctl
from vm.tests import make_project


def _seed(root: Path, name: str) -> Path:
    """造一个"看得出内容"的项目：有正文、有镜头表、有定妆照。"""
    pdir = make_project(root, name)
    (pdir / "novel" / "chapter01.md").write_text("第一章 雨夜\n\n正文一段。", encoding="utf-8")
    (pdir / "refs" / "char_唐僧.png").write_bytes(b"\x89PNG fake")
    (pdir / "project.json").write_text(json.dumps({"budget": {"max_gpu_minutes": 30}}), encoding="utf-8")
    return pdir


class DeleteProjectTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name) / "projects"
        self.root.mkdir()

    # ── 基本：删 = 移进回收站，内容一件不少 ────────────────────────────────

    def test_delete_moves_to_trash_and_keeps_content(self):
        pdir = _seed(self.root, "雨夜地铁")
        r = P.delete_project("雨夜地铁", confirm="雨夜地铁", root=self.root)

        self.assertTrue(r["ok"])
        self.assertFalse(pdir.exists(), "原位置必须没有（否则等于没删）")
        trash = Path(r["trash"])
        self.assertTrue(trash.is_dir())
        self.assertEqual(trash.parent.name, P.PROJECT_TRASH)
        # 内容逐件还在 —— "可逆"不是口号，是要能读回原文
        self.assertEqual((trash / "novel" / "chapter01.md").read_text(encoding="utf-8"),
                         "第一章 雨夜\n\n正文一段。")
        self.assertTrue((trash / "shots" / "chapter01.json").is_file())
        self.assertTrue((trash / "refs" / "char_唐僧.png").is_file())
        self.assertGreater(r["files"], 3, f"统计的文件数不对：{r}")

    def test_trash_itself_is_not_listed_as_a_project(self):
        """回收站目录不能被 discover_projects 当成一个项目 —— 否则界面会多出一个鬼项目。"""
        _seed(self.root, "p1")
        P.delete_project("p1", confirm="p1", root=self.root)
        names = taskctl.discover_projects(self.root)
        self.assertEqual(names, [], f"删完应该一个项目都不剩，实际：{names}")
        self.assertNotIn(P.PROJECT_TRASH, names)
        self.assertTrue((self.root / P.PROJECT_TRASH).is_dir(), "回收站本身得在（可逆的证据）")

    def test_two_deletes_in_same_second_do_not_collide(self):
        """同一秒删两个项目：条目名必须各自独立，第二个不能把第一个盖掉。"""
        _seed(self.root, "a1")
        _seed(self.root, "b2")
        r1 = P.delete_project("a1", confirm="a1", root=self.root)
        r2 = P.delete_project("b2", confirm="b2", root=self.root)
        self.assertNotEqual(r1["entry"], r2["entry"])
        self.assertTrue(Path(r1["trash"]).is_dir(), "第一个被第二个覆盖了")
        self.assertTrue(Path(r2["trash"]).is_dir())

    # ── 闸 1：确认名 ───────────────────────────────────────────────────────

    def test_confirm_mismatch_refuses_and_touches_nothing(self):
        pdir = _seed(self.root, "p1")
        for bad in ("", "P1", "p11", "项目", "确认", "p1-p1"):
            with self.subTest(confirm=bad):
                with self.assertRaises(taskctl.TaskError) as cm:
                    P.delete_project("p1", confirm=bad, root=self.root)
                self.assertIn("确认名不匹配", str(cm.exception))
        self.assertTrue(pdir.is_dir(), "拒绝之后项目必须原封不动")
        self.assertFalse((self.root / P.PROJECT_TRASH).exists(), "不该顺手建出回收站")

    def test_confirm_tolerates_surrounding_whitespace(self):
        """手输时带个空格是常态，不该因此卡住人 —— 但这条得写下来，否则以后有人"顺手"改成严格比较。"""
        _seed(self.root, "p1")
        r = P.delete_project("p1", confirm="  p1  ", root=self.root)
        self.assertTrue(r["ok"])

    # ── 闸 2：有任务在跑 ───────────────────────────────────────────────────

    def test_refuses_while_a_task_is_running(self):
        """
        走**真实**判据链：task.json 里的 pid 存活 + cmdline 像我们的 worker。
        用一个 argv 里带 pipeline.py / --_worker / 项目名的 `sh -c 'sleep 30'` 当假 worker，
        而不是把 task_running 打桩掉 —— 打桩了就只能证明"我调了那个函数"，
        证明不了"真在跑的任务会被认出来"。
        """
        pdir = _seed(self.root, "p1")
        fake = subprocess.Popen(
            ["sh", "-c", "sleep 30", "pipeline.py", "--_worker", "p1"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (fake.kill(), fake.wait()))
        (pdir / "state").mkdir(exist_ok=True)
        (pdir / "state" / "task.json").write_text(json.dumps(
            {"project": "p1", "stage": "render", "pid": fake.pid, "started_at": 1},
            ensure_ascii=False), encoding="utf-8")

        self.assertTrue(taskctl.task_running(taskctl.read_task(pdir)),
                        "假 worker 没被 task_running 认出来 —— 这条测试就白写了")
        with self.assertRaises(taskctl.TaskError) as cm:
            P.delete_project("p1", confirm="p1", root=self.root)
        self.assertIn("有任务在跑", str(cm.exception))
        self.assertTrue(pdir.is_dir())

    def test_finished_task_does_not_block(self):
        """跑完的历史记录（有 finished_at）不该让删除卡住 —— 否则删过一次任务的项目永远删不掉。"""
        pdir = _seed(self.root, "p1")
        (pdir / "state").mkdir(exist_ok=True)
        (pdir / "state" / "task.json").write_text(json.dumps(
            {"project": "p1", "stage": "render", "pid": os.getpid(),
             "started_at": 1, "finished_at": 2, "exit_code": 0}), encoding="utf-8")
        r = P.delete_project("p1", confirm="p1", root=self.root)
        self.assertTrue(r["ok"])

    # ── 闸 3：只许删 projects/ 的直接子目录 ────────────────────────────────

    def test_illegal_names_are_rejected(self):
        for bad in ("../etc", "a/b", "..", ".", "", "  ", "_trash", "绝对/x"):
            with self.subTest(name=bad):
                with self.assertRaises(taskctl.TaskError):
                    P.delete_project(bad, confirm=bad, root=self.root)

    def test_missing_project_errors_not_silently_ok(self):
        with self.assertRaises(taskctl.TaskError):
            P.delete_project("没有这个", confirm="没有这个", root=self.root)

    # ── 回收站可见 + 恢复 ──────────────────────────────────────────────────

    def test_trash_listing_reports_what_is_inside(self):
        _seed(self.root, "p1")
        P.delete_project("p1", confirm="p1", root=self.root)
        d = P.list_project_trash(self.root)
        self.assertEqual(len(d["items"]), 1, d)
        it = d["items"][0]
        self.assertEqual(it["project"], "p1", "条目名要能反解出原项目名，否则用户看不懂")
        self.assertEqual(it["chapters"], 1, it)
        self.assertTrue(it["has_novel"], it)
        self.assertGreater(it["size"], 0)

    def test_restore_puts_it_back(self):
        pdir = _seed(self.root, "p1")
        before = (pdir / "novel" / "chapter01.md").read_text(encoding="utf-8")
        entry = P.delete_project("p1", confirm="p1", root=self.root)["entry"]
        r = P.restore_project(entry, root=self.root)
        self.assertTrue(r["ok"])
        self.assertTrue(pdir.is_dir(), "恢复后原位置该有目录")
        self.assertFalse((self.root / P.PROJECT_TRASH / entry).exists())
        self.assertEqual((pdir / "novel" / "chapter01.md").read_text(encoding="utf-8"), before)
        self.assertEqual(taskctl.discover_projects(self.root), ["p1"])

    def test_restore_refuses_to_overwrite_an_existing_project(self):
        """删掉 → 又建了同名项目 → 再恢复：必须拒绝，**不能**把新建的覆盖掉。"""
        _seed(self.root, "p1")
        entry = P.delete_project("p1", confirm="p1", root=self.root)["entry"]
        _seed(self.root, "p1")   # 用户重建了同名项目（内容不同）
        (self.root / "p1" / "novel" / "chapter01.md").write_text("新写的正文", encoding="utf-8")

        with self.assertRaises(taskctl.TaskError) as cm:
            P.restore_project(entry, root=self.root)
        self.assertIn("不能覆盖", str(cm.exception))
        self.assertEqual((self.root / "p1" / "novel" / "chapter01.md").read_text(encoding="utf-8"),
                         "新写的正文", "新项目的文件被动过了")
        self.assertTrue((self.root / P.PROJECT_TRASH / entry).is_dir(), "条目该还留在回收站")

    def test_restore_rejects_path_traversal_and_unknown_entries(self):
        base = self.root / P.PROJECT_TRASH
        base.mkdir(parents=True)
        for bad in ("../../etc/passwd", "a/b", "", ".", "..", "1700000000__合法名"):
            with self.subTest(entry=bad):
                with self.assertRaises(taskctl.TaskError):
                    P.restore_project(bad, root=self.root)

    def test_roundtrip_after_restarting_the_root_being_empty(self):
        """空根目录：list 不炸、返回空表（界面第一次打开回收站就是这个样子）。"""
        empty = Path(self._td.name) / "fresh"
        empty.mkdir()
        d = P.list_project_trash(empty)
        self.assertEqual(d["items"], [])
        self.assertTrue(d["ok"])


class HttpDeleteGuardTest(unittest.TestCase):
    """HTTP 层那道"不回落默认项目"的门：漏传 project 就去删默认项目是不可接受的猜测。"""

    def setUp(self):
        from vm import web
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        root = Path(self._td.name)
        self.served = root / "projects"
        self.served.mkdir()
        _seed(self.served, "p1")
        _seed(self.served, "p2")
        self._saved = taskctl.PROJECTS_DIR
        taskctl.PROJECTS_DIR = root / "decoy"      # 泄漏探测器
        (root / "decoy").mkdir()
        from http.server import ThreadingHTTPServer
        import threading
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(self.served, default_project="p1"))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(setattr, taskctl, "PROJECTS_DIR", self._saved)

    def _post(self, path, body):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            c.request("POST", path, body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                      headers={"Content-Type": "application/json"})
            r = c.getresponse()
            return r.status, json.loads(r.read().decode("utf-8"))
        finally:
            c.close()

    def test_delete_without_project_is_400_not_default_project(self):
        code, b = self._post("/api/project/delete", {"confirm": "p1"})
        self.assertEqual(code, 400, b)
        self.assertEqual(b["error"], "no_project")
        self.assertTrue((self.served / "p1").is_dir(), "默认项目 p1 被误删了！")
        self.assertTrue((self.served / "p2").is_dir())

    def test_delete_wrong_confirm_is_400(self):
        code, b = self._post("/api/project/delete", {"project": "p1", "confirm": "p2"})
        self.assertEqual(code, 400, b)
        self.assertIn("确认名不匹配", b["message"])
        self.assertTrue((self.served / "p1").is_dir())

    def test_delete_then_trash_then_restore_over_http(self):
        code, b = self._post("/api/project/delete", {"project": "p1", "confirm": "p1"})
        self.assertEqual(code, 200, b)
        self.assertFalse((self.served / "p1").exists())
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", "/api/project/trash")
        d = json.loads(c.getresponse().read().decode("utf-8"))
        c.close()
        self.assertEqual([i["project"] for i in d["items"]], ["p1"], d)
        code, b = self._post("/api/project/restore", {"entry": d["items"][0]["entry"]})
        self.assertEqual(code, 200, b)
        self.assertTrue((self.served / "p1" / "novel" / "chapter01.md").is_file())


if __name__ == "__main__":
    sys.exit(unittest.main(verbosity=2))
