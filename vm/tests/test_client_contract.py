"""
前端 API 客户端 ↔ 后端路由表的**契约一致性**测试（F3.2）。

## 为什么要跨语言去解析前端源码

老计划的 **B1** 是这么坏的：`api_budget` handler 写好了，但 `do_GET` 的分发链没接线，
前端 `client.ts` 照常在调 `/api/budget` —— 于是**预算面板恒 404**，
而且因为前端不崩、后端不崩，只有"数字不显示"这一条症状，靠人点很久才发现。

`web.py` 已经有一道**启动自检**（`ROUTES_*` 表项必须真有方法），它防的是
"登记了但没实现"。但 B1 是反过来的方向：**前端调了，后端没登记** ——
启动自检看不见，因为它不知道前端有哪些调用。

所以这里补另一个方向：把 `web/src/api/client.ts` 里出现的每个 `/api/...` 路径
抓出来，逐个断言它在后端路由表里。前端新增一次调用而忘了登记路由，
这条测试会红，且红得指名道姓。

不引 OpenAPI 代码生成：`config.py:1235` 那份手写 SCHEMA 已经够表达"字段形状"，
而这里要保证的只是**路径存在性**，一个正则的成本远低于一套生成管线。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from vm import web

WEB_SRC = Path(__file__).resolve().parent.parent.parent / "web" / "src"


def _call_dirs(src: str) -> re.Pattern[str]:
    """
    抓 `request…(<路径>` / `post…(<路径>`。

    ★ 泛型参数**不能**用 `<[^>]*>` 去匹配：`request<{ check: Record<string, unknown> }>`
      里有嵌套尖括号，`[^>]*` 会在 `Record<string, number>` 的第一个 `>` 处截断，
      于是 `/api/budget` 明明被调用却报"前端没调" —— 一份会误报的清单比没有清单更坏。
      改成"从 request/post 起，在最近距离内找第一个 /api/ 字符串"。
    """
    return re.compile(r"\b(request|post)\b[^/;]{0,220}?[{`'\"](/api/[A-Za-z0-9_\-/]+)", re.S)


def _client_paths() -> tuple[list[str], list[str]]:
    """返回 (GET 路径, POST 路径)。

    扫**所有** API 客户端文件：`api/client.ts` 与 `api/config.ts` 是两个独立客户端，
    只扫前者会把 `/api/config` 整组误报成"后端有、前端没调"。
    """
    files = [WEB_SRC / "api" / "client.ts", WEB_SRC / "api" / "config.ts"]
    for f in files:
        if not f.is_file():
            raise AssertionError(f"找不到前端 API 客户端：{f}")
    gets: list[str] = []
    posts: list[str] = []
    for f in files:
        for m in _call_dirs("").finditer(f.read_text(encoding="utf-8")):
            (posts if m.group(1) == "post" else gets).append(m.group(2))
    return gets, posts


class TestClientPathsExist(unittest.TestCase):
    def test_every_get_path_is_registered(self):
        gets, _ = _client_paths()
        self.assertGreater(len(gets), 20, f"只抓到 {len(gets)} 个 GET 调用，正则可能失效了")
        routes = web.ROUTES_GET
        missing = sorted({p for p in gets if p not in routes})
        self.assertEqual([], missing,
                         "前端在调这些 GET 路径，但后端 ROUTES_GET 没登记（就是 B1 的形状）")

    def test_every_post_path_is_registered(self):
        _, posts = _client_paths()
        self.assertGreater(len(posts), 25, f"只抓到 {len(posts)} 个 POST 调用，正则可能失效了")
        routes = web.ROUTES_POST
        missing = sorted({p for p in posts if p not in routes})
        self.assertEqual([], missing,
                         "前端在调这些 POST 路径，但后端 ROUTES_POST 没登记")

    def test_body_limits_cover_the_big_payload_routes(self):
        """
        大请求体的路由必须放宽上限，否则会被 1MB 默认值**静默截断**。

        章节正文与图片 base64 是最容易超限的两类；超限的表现是"保存失败"，
        用户完全看不出是体积问题。
        """
        big = {"/api/chars/upload", "/api/asset/upload", "/api/chapter/save", "/api/chapter/import"}
        limits = set(web.BODY_LIMITS)
        self.assertTrue(big <= limits, f"这些大载荷路由没在 BODY_LIMITS 里：{sorted(big - limits)}")

    # 关于"后端登记了但前端没调"的反向清单：**刻意不做**。
    # 试过，两次都失败：
    #   ① 严格正则（`request<…>(` 紧跟引号）会把折行写法与嵌套泛型
    #      （`request<{ check: Record<string, unknown> }>`）漏掉 ⇒ 把在调的 `/api/budget` 报成没调；
    #   ② 为覆盖率把正则放松（跨 220 字符找最近的 /api/ 字符串）⇒ 误报更多（10 条里多数是活的）。
    # 一份会误报的清单的真实代价，是让人学会忽略它 —— 那比没有清单更坏。
    # 上面的**正向断言**（前端调的必须都登记）不受影响：它是集合包含检查，
    # 只要多抓不会漏抓，变异复验（删 4 条路由）已证明它真的会红。


class TestHandlerNamesResolve(unittest.TestCase):
    """路由表的值必须真是 handler 类上的方法（启动自检的同款检查，这里在测试期再跑一次）。"""

    @staticmethod
    def _missing_handler_names(routes: dict) -> list[str]:
        src = Path(web.__file__).read_text(encoding="utf-8")
        # 路由表的值必须是文件里真有的 `def 名字(` —— 不实例化 handler（它要 projects_root）
        missing = sorted({name for name in routes.values()
                          if not re.search(rf"\bdef {re.escape(name)}\(", src)})
        # 少数例外定义在类外（走前缀/别名分发），逐个放行而不是整体跳过
        allowed_outside = {"view"}
        return [m for m in missing if m not in allowed_outside]

    def test_all_get_handler_names_exist(self):
        self.assertEqual([], self._missing_handler_names(web.ROUTES_GET),
                         "ROUTES_GET 指向不存在的方法名（漏写 handler 或改了名没同步表）")

    def test_all_post_handler_names_exist(self):
        self.assertEqual([], self._missing_handler_names(web.ROUTES_POST),
                         "ROUTES_POST 指向不存在的方法名（漏写 handler 或改了名没同步表）")


if __name__ == "__main__":
    unittest.main()
