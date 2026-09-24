"""`vm.audit.cli` 的端到端单测（临时项目目录，不碰真实 projects/）。"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from vm.audit.cli import build_report, format_report, main
from vm.audit.tests import make_shot

CHAPTER = "唐僧回头道：\"悟空，你听这风声。\"\n\n孙悟空笑道：\"师父，不是风哭。\"\n"
Q1 = "悟空，你听这风声。"
Q2 = "师父，不是风哭。"


def write_project(root: Path, *, drop_q2: bool = False) -> Path:
    proj = root / "测试项目"
    (proj / "shots").mkdir(parents=True)
    (proj / "novel").mkdir(parents=True)
    (proj / "novel" / "第一章.md").write_text(CHAPTER, encoding="utf-8")
    shots = [
        make_shot(1, chars=["唐僧"], size="中景", dialogue=Q1),
        make_shot(2, chars=["孙悟空"], size="近景", dialogue="" if drop_q2 else Q2),
    ]
    (proj / "shots" / "chapter01.json").write_text(
        json.dumps(shots, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return proj


def run_main(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code = main(argv)
    return code, buf.getvalue()


class TestBuildReport(unittest.TestCase):
    def test_report_shape_on_temp_project(self):
        with tempfile.TemporaryDirectory() as td:
            proj = write_project(Path(td))
            r = build_report(proj)
            self.assertEqual(r["schema_version"], 1)
            self.assertEqual(r["tool"], "vm.audit")
            self.assertEqual(r["inputs"]["shots"], 2)
            self.assertEqual(r["inputs"]["chars"], 2)
            self.assertEqual(r["inputs"]["total_sec"], 10)
            self.assertTrue(r["summary"]["gate_passed"])
            for key in ("b1", "b2", "b5", "findings", "limitations", "manifest_entry"):
                self.assertIn(key, r)
            self.assertTrue(r["manifest_entry"]["gate_passed"])

    def test_broken_project_fails_gate_and_lists_error(self):
        with tempfile.TemporaryDirectory() as td:
            proj = write_project(Path(td), drop_q2=True)
            r = build_report(proj)
            self.assertFalse(r["summary"]["gate_passed"])
            self.assertEqual(r["summary"]["findings"]["error"], 1)
            self.assertIn("B5 dialogue-missing", r["summary"]["errors"])
            self.assertFalse(r["b5"]["gate"]["passed"])

    def test_explicit_scores_mode_raises_without_conflict_field(self):
        with tempfile.TemporaryDirectory() as td:
            proj = write_project(Path(td))
            with self.assertRaises(ValueError):
                build_report(proj, scores_mode="explicit")

    def test_format_report_contains_all_sections(self):
        with tempfile.TemporaryDirectory() as td:
            proj = write_project(Path(td))
            text = format_report(build_report(proj))
            for token in ("[B1]", "[B2]", "[B5]", "coverageScore", "门禁", "已知局限"):
                self.assertIn(token, text)


class TestCliMain(unittest.TestCase):
    def test_main_writes_state_audit_json(self):
        with tempfile.TemporaryDirectory() as td:
            proj = write_project(Path(td))
            code, out = run_main([str(proj)])
            self.assertEqual(code, 0)
            audit_path = proj / "state" / "audit.json"
            self.assertTrue(audit_path.is_file())
            data = json.loads(audit_path.read_text(encoding="utf-8"))
            self.assertEqual(data["inputs"]["shots"], 2)
            self.assertIn("已写入", out)

    def test_gate_exit_code(self):
        with tempfile.TemporaryDirectory() as td:
            proj = write_project(Path(td))
            self.assertEqual(run_main([str(proj), "--gate", "--no-write"])[0], 0)
            broken = write_project(Path(td) / "b", drop_q2=True)
            self.assertEqual(run_main([str(broken), "--gate", "--no-write"])[0], 1)

    def test_json_flag_emits_parseable_json(self):
        with tempfile.TemporaryDirectory() as td:
            proj = write_project(Path(td))
            code, out = run_main([str(proj), "--json", "--no-write"])
            self.assertEqual(code, 0)
            start = out.index("{")
            payload = json.loads(out[start:])
            self.assertEqual(payload["tool"], "vm.audit")

    def test_unknown_project_returns_2(self):
        code, out = run_main(["不存在的项目xyz", "--no-write"])
        self.assertEqual(code, 2)
        self.assertIn("输入错误", out)

    def test_bad_scores_mode_returns_2(self):
        with tempfile.TemporaryDirectory() as td:
            proj = write_project(Path(td))
            code, _ = run_main([str(proj), "--scores-mode", "explicit", "--no-write"])
            self.assertEqual(code, 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
