"""
comfy.py —— ComfyUI HTTP 客户端。

只做四件事：探测、提交、等待、取产物。**不启停 ComfyUI、不调 /free、不清队列**
（用户的 8188 是常驻共享服务，还跑着别的工作；本流水线只是它的一个客户端）。

两个从实战里学到的关键点，都在代码里标了：
  1. `has_node` 不能只判 HTTP 状态码 —— ComfyUI 对**不存在的节点**也返回 200 + 空对象 {}
  2. 提交超时不能设太小 —— 34GB 完整模型冷启动 staging 会超过 60 秒，
     而 h3-test-run.py 硬编码 timeout=60，实测连续 4 个任务全超时失败
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

DEFAULT_BASE = "http://127.0.0.1:8188"

# 提交超时：必须容纳模型冷启动 staging（实测 34GB 模型 >60s，21GB 剪枝版约 10-20s）
SUBMIT_TIMEOUT = 300
# 等待渲染完成的默认上限
RENDER_TIMEOUT = 1800
POLL_INTERVAL = 5


class ComfyError(Exception):
    """ComfyUI 交互失败。消息里带够上下文，便于直接贴到日志里排查。"""


class Comfy:
    def __init__(self, base: str = DEFAULT_BASE, submit_timeout: int = SUBMIT_TIMEOUT):
        self.base = base.rstrip("/")
        self.submit_timeout = submit_timeout

    # ---------- 底层 ----------

    def _get(self, path: str, timeout: int = 30) -> bytes:
        url = self.base + path
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            raise ComfyError(f"GET {path} → HTTP {e.code}: {e.read()[:300]!r}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ComfyError(f"GET {path} 失败: {e}") from e

    def _post(self, path: str, payload: dict, timeout: int) -> bytes:
        url = self.base + path
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            body = e.read()[:800]
            raise ComfyError(f"POST {path} → HTTP {e.code}: {body!r}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ComfyError(f"POST {path} 超时/失败（{timeout}s）: {e}") from e

    # ---------- 探测 ----------

    def healthy(self) -> bool:
        """ComfyUI 是否在线。任何异常都当作不在线，不抛出。"""
        try:
            self._get("/system_stats", timeout=8)
            return True
        except ComfyError:
            return False

    def require_healthy(self) -> None:
        if not self.healthy():
            raise ComfyError(
                f"ComfyUI 无响应（{self.base}）。请先确认它已启动：bash ~/comfyui/start-comfyui.sh"
            )

    def has_node(self, name: str) -> bool:
        """
        节点是否存在。

        ★ 必须看 body，不能只看状态码：
          ComfyUI 对**不存在的节点** `GET /object_info/<任意名>` 也返回 200，
          但 body 是 `{}`（实测 2 字节）。只判 200 会恒真 →
          于是"节点缺失自动降级"的分支永远不执行 → 提交时 400 missing_node_type。
        """
        try:
            raw = self._get(f"/object_info/{urllib.parse.quote(name)}", timeout=15)
        except ComfyError:
            return False
        body = raw.strip()
        if len(body) <= 2:  # b"{}" 或空
            return False
        try:
            obj = json.loads(body)
        except json.JSONDecodeError:
            return False
        return isinstance(obj, dict) and len(obj) > 0

    def object_list(self, kind: str) -> list[str]:
        """
        列出某类模型（unet_name / lora_name / clip_name / vae_name / checkpoint_name）。
        kind 对应 ComfyUI 的节点类名，如 UNETLoader / LoraLoaderModelOnly。
        """
        node_of = {
            "unet_name": "UNETLoader",
            "lora_name": "LoraLoaderModelOnly",
            "clip_name": "CLIPLoader",
            "vae_name": "VAELoader",
            "checkpoint_name": "CheckpointLoaderSimple",
        }
        node = node_of.get(kind)
        if not node:
            raise ComfyError(f"未知的模型类型: {kind}")
        raw = self._get(f"/object_info/{node}", timeout=20)
        info = json.loads(raw)
        try:
            spec = info[node]["input"]["required"][kind][0]
        except (KeyError, IndexError, TypeError) as e:
            raise ComfyError(f"无法从 {node} 读取 {kind} 列表: {raw[:200]!r}") from e
        return list(spec) if isinstance(spec, list) else []

    # ---------- 提交与等待 ----------

    def submit(self, workflow: dict) -> str:
        """提交工作流，返回 prompt_id。超时给足（见文件头说明）。"""
        raw = self._post("/prompt", {"prompt": workflow}, timeout=self.submit_timeout)
        try:
            res = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ComfyError(f"/prompt 返回非 JSON: {raw[:300]!r}") from e
        if res.get("node_errors"):
            raise ComfyError(f"工作流校验失败: {json.dumps(res['node_errors'], ensure_ascii=False)[:600]}")
        pid = res.get("prompt_id")
        if not pid:
            raise ComfyError(f"/prompt 未返回 prompt_id: {json.dumps(res, ensure_ascii=False)[:400]}")
        return pid

    def history(self, prompt_id: str) -> dict | None:
        """查单个任务的历史。返回 None = 查不到（可能还在跑/排队，也可能已丢失）。"""
        try:
            raw = self._get(f"/history/{prompt_id}", timeout=30)
        except ComfyError:
            return None
        try:
            h = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return h.get(prompt_id)

    def queue_state(self) -> tuple[int, int]:
        """返回 (running, pending)。探测失败时返回 (-1,-1)，调用方自行处理。"""
        try:
            q = json.loads(self._get("/queue", timeout=15))
        except (ComfyError, json.JSONDecodeError):
            return -1, -1
        return len(q.get("queue_running") or []), len(q.get("queue_pending") or [])

    def wait(
        self,
        prompt_id: str,
        timeout: int = RENDER_TIMEOUT,
        on_tick: Callable[[float, int, int], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        progress: Any = None,
    ) -> dict:
        """
        阻塞直到任务完成，返回 history 条目。

        on_tick(elapsed, running, pending, detail) 每轮调用一次。
        第 4 个参数 `detail` 是**人话进度**：优先用 `progress`（vm.wsclient.ComfyProgress）
        报的**步级**信息（`12/20 步 60%`）。在此之前我们只有 running/pending 计数 ——
        日志里一长串 `running=1 pending=0` 根本不像进度条。

        `progress` 连接失败时静默退化为只有计数（进度是锦上添花，不该让渲染失败）。
        should_stop() 返回 True 时抛 ComfyError 中止（用户点了"停止"）。
        """
        t0 = time.time()
        while True:
            if should_stop and should_stop():
                raise ComfyError("用户中止")
            elapsed = time.time() - t0
            if elapsed > timeout:
                raise ComfyError(f"等待超时（{timeout}s），prompt_id={prompt_id}")

            entry = self.history(prompt_id)
            if entry is not None:
                status = entry.get("status") or {}
                if status.get("completed"):
                    return entry
                if status.get("status_str") == "error":
                    msgs = status.get("messages") or []
                    detail = json.dumps(msgs[-2:], ensure_ascii=False)[:600]
                    raise ComfyError(f"执行出错: {detail}")

            running, pending = self.queue_state()
            if on_tick:
                detail = ""
                if progress is not None:
                    try:
                        detail = progress.summary or ""
                    except Exception:      # noqa: BLE001
                        detail = ""
                try:
                    on_tick(elapsed, running, pending, detail)
                except TypeError:
                    on_tick(elapsed, running, pending)   # 兼容旧的两参数回调
            time.sleep(POLL_INTERVAL)

    # ---------- 取产物 ----------

    @staticmethod
    def outputs_of(entry: dict) -> list[dict]:
        """从 history 条目里提取产物清单：[{filename, subfolder, type}]。"""
        out: list[dict] = []
        for node in (entry.get("outputs") or {}).values():
            for key in ("gifs", "videos", "images", "audio"):
                for it in node.get(key) or []:
                    if isinstance(it, dict) and it.get("filename"):
                        out.append(
                            {
                                "filename": it["filename"],
                                "subfolder": it.get("subfolder", ""),
                                "type": it.get("type", "output"),
                            }
                        )
        return out

    def download(self, item: dict, dst: Path) -> Path:
        """经 /view 取回产物并原子落盘。"""
        qs = urllib.parse.urlencode(
            {
                "filename": item["filename"],
                "subfolder": item.get("subfolder", ""),
                "type": item.get("type", "output"),
            }
        )
        data = self._get(f"/view?{qs}", timeout=180)
        if not data:
            raise ComfyError(f"取回产物为空: {item}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(dst.suffix + ".part")
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dst)
        return dst

    def cancel(self, prompt_ids: list[str] | None = None) -> dict:
        """
        A8：两段式取消 —— 先按 prompt_id 精确删队列项，再中断当前执行。

        为什么分两段（依据 MCWW `comfyAPI.py:60-96`）：
          - `POST /queue {delete:[pid]}` 只能删**排队中**的任务，对**正在执行**的无效
          - `/interrupt` 只能中断**正在执行**的，对排队中的无效
        只发其中一个都会留下残留任务继续烧 GPU。两段都发才彻底。

        ⚠️ 这是**定向**取消，只删传入的 prompt_id。**绝不调用 `/queue {clear:true}`** ——
        那会清掉用户自己排队的任务。prompt_ids 为空时**什么都不做**并返回说明。
        """
        res = {"deleted": [], "interrupted": False, "note": ""}
        ids = [p for p in (prompt_ids or []) if p]
        if not ids:
            res["note"] = "未提供 prompt_id，未执行任何取消（不做全清，避免误删用户任务）"
            return res
        try:
            self._post("/queue", {"delete": ids}, timeout=15)
            res["deleted"] = ids
        except ComfyError as e:
            res["note"] = f"删除排队项失败：{e}"
        try:
            self._post("/interrupt", {}, timeout=15)
            res["interrupted"] = True
        except ComfyError as e:
            res["note"] = (res["note"] + "；" if res["note"] else "") + f"中断失败：{e}"
        return res

    def interrupt(self) -> None:
        """
        中断当前执行。**仅在用户显式点"停止"时调用**。
        绝不用它做"清理""重置"之类的事 —— 它会打断 ComfyUI 正在跑的任何任务。
        """
        try:
            self._post("/interrupt", {}, timeout=15)
        except ComfyError:
            pass  # 中断失败不影响主流程
