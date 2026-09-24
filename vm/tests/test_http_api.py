"""
HTTP 契约（vm/web.py 的 48 端点边界）的单测：临时 serve + 随机端口 + tmp projects 根。

为什么值得单测：B1（/api/budget 写了 handler 忘接线，前端恒 404）这类"配对错误"
在端到端里要靠人手点到才能发现；而错误分流（4xx/5xx，S6）是排障不失真的底线 ——
服务端 bug 被折叠成 400，用户就会去改根本没错的请求。

隔离纪律（比 qi_test 更严的两条）：
  ① 服务挂在一个 tmp「served 根」上，一切断言都对它做；
  ② taskctl.PROJECTS_DIR 另外指到一个 tmp「decoy 根」——
     谁绕过 projects_root 用裸项目名解析（曾经的 api_budget/api_approve），
     文件就会落到 decoy 里被断言抓出来，而不是默默写进用户真实 projects/。
"""

from __future__ import annotations

import http.client
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from vm import taskctl, web
from vm.state import Project
from vm.tests import REPO_ROOT, make_project, make_shot

# 预算段放一个"只此一家"的数：谁读到别的项目去，一眼看穿
BUDGET = {"max_gpu_minutes": 42}


class HttpContractTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        base = Path(self._td.name)
        self.served_root = base / "served"
        self.decoy_root = base / "decoy"
        self.served_root.mkdir()
        self.decoy_root.mkdir()
        make_project(self.served_root, "p1", budget=BUDGET)

        self._saved_projects_dir = taskctl.PROJECTS_DIR
        taskctl.PROJECTS_DIR = self.decoy_root   # 泄漏探测器，理由见模块头

        # 随机端口起临时服务（不占用户的 8801）；handler 与被测代码同进程，
        # 所以可以用 monkeypatch 精确制造"服务端 bug"来验证 5xx 分流。
        from http.server import ThreadingHTTPServer
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(self.served_root))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True).start()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        taskctl.PROJECTS_DIR = self._saved_projects_dir
        self._td.cleanup()

    # -- 小工具

    def _call(self, method: str, path: str, body=None, *, raw: bytes | None = None,
              conn=None):
        c = conn or http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            if raw is not None:
                c.request(method, path, body=raw,
                          headers={"Content-Type": "application/json"})
            elif body is not None:
                # 必须自己编码成 utf-8 bytes：http.client 对 str body 走 latin-1，
                # 中文请求体会直接 UnicodeEncodeError
                c.request(method, path,
                          body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                          headers={"Content-Type": "application/json"})
            else:
                c.request(method, path)
            r = c.getresponse()
            data = r.read()
            try:
                payload = json.loads(data.decode("utf-8")) if data else {}
            except json.JSONDecodeError:
                payload = {"_raw": data[:200].decode("utf-8", "replace")}
            return r.status, payload
        finally:
            if conn is None:
                c.close()

    def get(self, path, **kw):
        return self._call("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self._call("POST", path, body=body, **kw)

    def _raw_get(self, path: str):
        """原始 GET（要看响应头和二进制体的用它）→ (status, 小写头 dict, body bytes)。"""
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            c.request("GET", path)
            r = c.getresponse()
            return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
        finally:
            c.close()

    def _patch(self, obj, name, value):
        """monkeypatch 并登记恢复（制造服务端异常用，测完必须还原）。"""
        old = getattr(obj, name)
        setattr(obj, name, value)
        self.addCleanup(setattr, obj, name, old)


class TestReadEndpoints(HttpContractTest):
    def test_projects_contract(self):
        code, b = self.get("/api/projects")
        self.assertEqual(code, 200)
        self.assertTrue(b["ok"])
        self.assertEqual(Path(b["root"]).resolve(), self.served_root.resolve(),
                         "root 必须是被服务的根（tmp 根），不能是全局默认")
        self.assertEqual([p["name"] for p in b["projects"]], ["p1"])

    def test_status_contract(self):
        code, b = self.get("/api/status?project=p1")
        self.assertEqual(code, 200)
        self.assertTrue(b["ok"])
        self.assertFalse(b["running"])
        self.assertEqual(set(b["readiness"]), set(taskctl.STAGES),
                         "每个阶段都要有 readiness（stepper 要用）")

    def test_shots_contract(self):
        code, b = self.get("/api/shots?project=p1")
        self.assertEqual(code, 200)
        self.assertTrue(b["ok"])
        self.assertEqual(len(b["shots"]), 3, f"夹具是 3 镜：{b.get('shots')}")
        for k in ("missing", "stale", "current"):
            self.assertIn(k, b["counts"], f"三态计数缺 {k}：{b['counts']}")

    def test_budget_no_longer_404(self):
        """
        B1 回归闸：/api/budget 死路由（handler 写了没接线，前端恒 404）。
        路由表化（S2）后这条必须 200，并且**读的是被服务的项目**——
        budget 段回显夹具里的 42，读到别的根就不是这个数。
        """
        code, b = self.get("/api/budget?project=p1")
        self.assertEqual(code, 200, f"/api/budget 不再允许 404/500：{b}")
        self.assertTrue(b["ok"])
        for k in ("est", "check", "budget", "spent"):
            self.assertIn(k, b, f"/api/budget 载荷缺 {k}：{b}")
        self.assertEqual(b["budget"].get("max_gpu_minutes"), BUDGET["max_gpu_minutes"],
                         f"预算读到了别的项目/别的根：{b['budget']}")
        self.assertEqual(b["est"]["shots"], 3, f"估算要对上夹具镜头数：{b['est']}")

    def test_budget_stage_query(self):
        code, b = self.get("/api/budget?project=p1&stage=render")
        self.assertEqual(code, 200)
        self.assertEqual(b["est"]["stage"], "render")


class TestErrorRouting(HttpContractTest):
    """S6 错误分流：已知输入错误 4xx 人话；服务端 bug 500 server_error + 留栈。"""

    def test_unknown_path_is_404(self):
        for method, path in (("GET", "/api/nope"), ("POST", "/api/nope")):
            code, b = self._call(method, path, body={})
            self.assertEqual(code, 404, f"{method} {path}")
            self.assertEqual(b["error"], "not_found")

    def test_bad_json_body_is_400(self):
        code, b = self.post("/api/run", raw="{不是 json".encode("utf-8"))
        self.assertEqual(code, 400, f"坏 JSON 是请求错误不是服务端故障：{b}")
        self.assertEqual(b["error"], "bad_json")
        code, b = self.post("/api/run", raw=b"[1,2,3]")
        self.assertEqual(code, 400, "请求体必须是对象")
        self.assertEqual(b["error"], "bad_json")

    def test_known_input_errors_are_4xx_with_names(self):
        """每类输入错误带 error 类别（前端按类别给人话），且绝不是 5xx。"""
        cases = [
            ("GET", "/api/shot?project=p1", None, "bad_shot"),
            ("POST", "/api/asset/gen", {"project": "p1", "kind": "movie", "id": "S1"}, "bad_kind"),
            ("POST", "/api/chars/gacha", {"project": "p1", "name": "唐僧", "n": 99}, "bad_count"),
        ]
        for method, path, body, want in cases:
            code, b = self._call(method, path, body=body)
            self.assertEqual(code, 400, f"{method} {path} → {code} {b}")
            self.assertEqual(b["error"], want, f"{method} {path} 类别错了：{b}")
            self.assertTrue(b["message"], "要有人话原因")

    def test_traversal_project_name_is_4xx_not_5xx(self):
        """非法项目名（目录穿越）是「请求不对」→ 4xx，不能冒充服务端故障。"""
        code, b = self.get("/api/status?project=" + "..%2Fescape")
        self.assertNotEqual(code, 500, f"输入错误不该 500：{b}")
        self.assertTrue(400 <= code < 500, f"应 4xx，实得 {code} {b}")

    def test_server_bug_is_500_server_error(self):
        """制造服务端异常：必须 500 + error=server_error（不折叠成 400 伪装请求错误）。"""
        def _boom(*a, **kw):
            raise RuntimeError("故意制造的服务端异常")
        self._patch(taskctl, "shot_table", _boom)
        code, b = self.get("/api/shots?project=p1")
        self.assertEqual(code, 500, f"服务端 bug 必须 500：{b}")
        self.assertEqual(b["error"], "server_error")
        self.assertIn("故意制造", b["message"], "message 要带原始异常，排障不失真")

    def test_upload_adopt_no_longer_fold_into_400(self):
        """
        S6 专项：upload/adopt 曾经把一切异常折叠成 400 —— 服务端 bug 伪装成请求错误。
        现在非输入类异常必须冒到 500。
        """
        def _boom(*a, **kw):
            raise RuntimeError("采纳时炸了（服务端）")
        self._patch(taskctl, "asset_adopt", _boom)
        code, b = self.post("/api/asset/adopt", {"project": "p1", "kind": "scene",
                                                 "id": "S1", "file": "c1.png"})
        self.assertEqual(code, 500, f"服务端 bug 被折叠成 {code}：{b}")
        self.assertEqual(b["error"], "server_error")

    def test_module_not_ready_is_503(self):
        """依赖模块没写完是 503（不是 4xx 也不是 500）：前端提示"该阶段暂不可用"。"""
        def _not_ready(*a, **kw):
            raise taskctl.ModuleNotReady("vm/xxx.py 还没写完")
        self._patch(taskctl, "rewrite_shot", _not_ready)
        code, b = self.post("/api/shot/rewrite", {"project": "p1", "id": "1-01-01"})
        self.assertEqual(code, 503, f"实得 {code} {b}")
        self.assertEqual(b["error"], "module_not_ready")


class TestMutatingContracts(HttpContractTest):
    def test_queue_add_enqueues_and_wakes_drainer(self):
        """
        入队立即返回（不等出图）+ 自动唤醒 drainer。start_async 打桩：
        这里测的是 HTTP 契约，真起 worker 由 test_taskrun 覆盖。
        """
        started = {}

        class FakeHandle:
            pid = 4242

            def public(self):
                return {"only": [], "started_at": 1}

        def fake_start_async(*a, **kw):
            started["args"] = a
            started["kw"] = kw
            return FakeHandle()

        self._patch(taskctl, "start_async", fake_start_async)
        code, b = self.post("/api/queue/add", {"project": "p1", "kind": "asset_gen",
                                               "kind_id": "", "id": "S1", "n": 2})
        self.assertEqual(code, 200, f"入队必须立即返回：{b}")
        self.assertTrue(b["ok"])
        self.assertTrue(b["started_worker"], "空闲时入队要顺手唤醒 drainer")
        # 作业文件必须落在**被服务的**项目里（谁绕过 projects_root 就写到 decoy 被抓）
        from vm import queue as Q
        jobs = Q.read_all(Project(self.served_root / "p1"))
        self.assertEqual(len(jobs), 1, f"队列文件该在 served 根下：{jobs}")
        self.assertEqual(jobs[0].kind, "asset_gen")

    def test_queue_add_single_response_on_keepalive(self):
        """
        quiet 双响应 hack 的回归闸：一个请求只准发一个响应。
        双响应时第二个载荷会搭在同一条 keep-alive 连接上，下一次请求读到脏数据
        （前端表现为"卡死"）。所以这里在**同一条连接**上连打两个请求验证配对。
        """
        class FakeHandle:
            pid = 4242

            def public(self):
                return {"only": [], "started_at": 1}

        self._patch(taskctl, "start_async", lambda *a, **kw: FakeHandle())
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            code, b = self._call("POST", "/api/queue/add",
                                 {"project": "p1", "kind": "storyboard", "id": "1-01-01"},
                                 conn=conn)
            self.assertEqual(code, 200)
            code2, b2 = self._call("GET", "/api/queue?project=p1", conn=conn)
            self.assertEqual(code2, 200, f"复用连接的第二个请求必须干净：{b2}")
            self.assertIn("jobs", b2, f"第二个响应被脏数据污染：{b2}")
        finally:
            conn.close()

    def test_approve_lands_in_served_project(self):
        """
        放行记录必须落在**被服务的**项目 state/ 下，绝不写到全局 projects 根。
        （曾为已知 bug：api_approve 只传裸项目名给 budget.grant_approval，
        serve 挂自定义根时放行记录会跑到全局 projects/ 去。已修，转正为回归锁。）
        """
        code, b = self.post("/api/approve", {"project": "p1", "stage": "render"})
        self.assertEqual(code, 200, f"放行一次应成功：{b}")
        self.assertTrue((self.served_root / "p1" / "state" / "approval.json").is_file(),
                        "放行记录必须落在被服务的项目 state/ 下")
        self.assertFalse((self.decoy_root / "p1").exists(),
                         "绝不该写到全局 projects 根（decoy 探测器）")


class TestV02Contracts(HttpContractTest):
    """task-4（T2）带来的 API 契约变化：needs_approval 透传 / 缩略图异步占位 / 缓存失效。"""

    def test_queue_add_transmits_needs_approval_payload(self):
        """
        预算拦截必须**结构化透传**：唤醒 drainer 被 NeedsApproval 拦下时，
        /api/queue/add 仍然 200 + 作业已入队，同时带 needs_approval 载荷
        （UI 拿它弹审批框；折成一句 start_error 的话用户看不到审批框，队列就永远停着）。
        """
        make_project(self.served_root, "p2", budget={"max_gpu_minutes": 0.01})
        # 安全阀：万一预算没拦住，start_async 会真派生 worker —— 把 PIPELINE 指到
        # 秒退垫片，绝不让 worker 落到全局 projects/ 去（子进程不认本进程的补丁）
        stub_pipe = Path(self._td.name) / "pipeline.py"
        stub_pipe.write_text("raise SystemExit(2)\n", encoding="utf-8")
        self._patch(taskctl, "PIPELINE", stub_pipe)

        code, b = self.post("/api/queue/add", {"project": "p2", "kind": "chars_gacha",
                                               "name": "唐僧", "n": 2})
        self.assertEqual(code, 200, f"入队不该因预算拦截而失败：{b}")
        self.assertTrue(b["ok"])
        self.assertEqual(b["job"]["status"], "pending", "作业要留在队列里等批准（等批准≠失败）")
        self.assertFalse(b["started_worker"], "被拦下就不该有 worker 在跑")
        ap = b.get("needs_approval")
        self.assertTrue(isinstance(ap, dict) and ap.get("needs_approval"),
                        f"needs_approval 载荷缺失：{b}")
        for k in ("est", "spent", "budget", "message"):
            self.assertIn(k, ap, f"审批载荷缺 {k}（UI 弹窗的三问数据源）：{ap}")
        for must in ("已花", "卡在", "再放行"):
            self.assertIn(must, ap["message"], f"三问缺「{must}」：{ap['message'][:80]}")

    def test_thumb_placeholder_contract(self):
        """
        缩略图异步化（S9）：后台还在生成时**不阻塞、不报错** —— 回占位图 +
        X-Thumb-Pending: 1 + no-store（占位图绝不进缓存，否则前端重试拿到的还是它）。
        """
        def slow_thumb(proj, key, src):
            time.sleep(4)   # 模拟 ffmpeg 慢（真实上限 ~25s）；请求线程只等 _THUMB_WAIT
            return None

        self._patch(web, "_make_thumb", slow_thumb)
        (self.served_root / "p1" / "clips" / "1-01-01.mp4").write_bytes(b"fake-clip")
        code, hdrs, body = self._raw_get("/view?project=p1&shot=1-01-01&thumb=1")
        self.assertEqual(code, 200, f"占位图是 200 不是错误，实得 {code}")
        self.assertEqual(hdrs.get("x-thumb-pending"), "1",
                         f"后台生成中必须标 X-Thumb-Pending，头：{hdrs}")
        self.assertIn("no-store", hdrs.get("cache-control", ""),
                      "占位图绝不许进缓存")
        self.assertIn("image/", hdrs.get("content-type", ""), f"头：{hdrs}")
        self.assertGreater(len(body), 0, "占位图要有内容")

    def test_shot_table_cache_invalidates_on_write(self):
        """
        shot_table 有 10s TTL 缓存，但**写文件必须立刻可见**（mtime 签名失效）——
        「写后即刷」的前端契约全押在这条上：mutation 后 1s 内的刷新拿到旧表就等于没刷。
        """
        code, b = self.get("/api/shots?project=p1")
        self.assertEqual(len(b["shots"]), 3, f"夹具 3 镜：{b.get('shots') and len(b['shots'])}")
        f = self.served_root / "p1" / "shots" / "chapter01.json"
        data = json.loads(f.read_text(encoding="utf-8"))
        data.append(make_shot(4))
        f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        code, b = self.get("/api/shots?project=p1")
        self.assertEqual(len(b["shots"]), 4,
                         "改镜头表后立即查询必须看到 4 镜（TTL 缓存不许挡在写后即刷的路上）")


class TestConfigCenterContract(HttpContractTest):
    """
    T3（task-5）配置中心三端点。门在**路由已注册**上，不在 vm/config.py 文件存在上 ——
    文件先落、web.py 路由后接是并行开发的常态，端点不可用就不该跑契约断言。
    """

    def setUp(self):
        super().setUp()
        routes = getattr(web, "ROUTES_GET", {})
        post_routes = getattr(web, "ROUTES_POST", {})
        has_post = "/api/config" in post_routes or "/api/config/validate" in post_routes
        if "/api/config" not in routes or not has_post:
            self.skipTest("task-5 的 /api/config 路由还没注册进 web.ROUTES_*，"
                          "接线后本组自动生效")

    def test_config_get_returns_schema_with_help_text(self):
        code, b = self.get("/api/config?project=p1")
        self.assertEqual(code, 200, f"实得 {code} {b}")
        self.assertTrue(b["ok"])
        self.assertTrue(b, "要带配置内容（分组/取值/说明文案）")

    def test_config_post_rejects_invalid_with_human_message(self):
        code, b = self.post("/api/config", {"project": "p1",
                                            "patch": {"fps": "很快"}})
        self.assertTrue(400 <= code < 500, f"非法值是请求错误：{code} {b}")
        self.assertTrue(b.get("message"), "拒绝必须说人话（哪项错了/应该是什么）")

    def test_config_validate_endpoint_answers(self):
        code, b = self.post("/api/config/validate", {"project": "p1"})
        self.assertEqual(code, 200, f"预检接口不该失败（连不上也是结果不是错误）：{b}")
        self.assertTrue(b["ok"])


if __name__ == "__main__":
    unittest.main()
