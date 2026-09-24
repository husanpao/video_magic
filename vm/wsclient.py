"""
wsclient.py —— 极简 WebSocket 客户端（只用标准库），用来读 ComfyUI 的**实时进度**。

## 为什么需要
我们的 `Comfy.wait()` 只轮询 `/history`，所以日志里永远只有：
    ⏳ ComfyUI 0s：running=1 pending=0
    ⏳ ComfyUI 10s：running=1 pending=0
—— **看不到"第几步/共几步"**。用户一眼就看出这不像进度条。

而 ComfyUI **本来就在发进度**：`/ws?clientId=…` 上会推
  · 文本帧 JSON：`{"type":"progress","data":{"value":N,"max":M,"prompt_id":…}}`
  · 文本帧 JSON：`{"type":"executing","data":{"node":…,"prompt_id":…}}`
  · 二进制帧：预览图 / 文本（`BinaryEventTypes`）
只是我们从来没连过。

## 为什么自己写而不是装 websocket-client
本项目的纪律是**零第三方依赖**（审计层、质检层都是纯标准库）。
RFC6455 的客户端握手 + 服务端→客户端的帧解码大约 100 行，够用了：
我们**只读**，不发数据帧，所以不需要掩码、不需要分片重组、不需要扩展协商。

## 安全边界
· 只连本机 ComfyUI（默认 127.0.0.1），不做 TLS
· 单帧上限 `MAX_FRAME`，超过就断开（防止坏帧把内存吃光）
· 全部收包都有超时，绝不无限阻塞
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import threading
import time
from typing import Any, Callable

MAX_FRAME = 8 << 20  # 单帧上限 8MB（预览图会比较大）


def _handshake(sock: socket.socket, host: str, path: str, timeout: float) -> None:
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    sock.sendall(req.encode())
    sock.settimeout(timeout)
    buf = b""
    deadline = time.time() + timeout
    while b"\r\n\r\n" not in buf:
        if time.time() > deadline:
            raise TimeoutError("WebSocket 握手超时")
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("WebSocket 握手期间连接被关闭")
        buf += chunk
    head = buf.split(b"\r\n\r\n", 1)[0].decode("latin-1")
    if "101" not in head.split("\r\n")[0]:
        raise ConnectionError(f"WebSocket 握手失败：{head.splitlines()[0] if head else '(空响应)'}")


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    out = b""
    while len(out) < n:
        chunk = sock.recv(n - len(out))
        if not chunk:
            raise ConnectionError("连接关闭")
        out += chunk
    return out


def _read_frame(sock: socket.socket) -> tuple[int, bytes]:
    """读一个帧，返回 (opcode, payload)。服务端→客户端**不加掩码**，所以解码简单。"""
    b1, b2 = _recv_exact(sock, 2)
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    ln = b2 & 0x7F
    if ln == 126:
        ln = struct.unpack(">H", _recv_exact(sock, 2))[0]
    elif ln == 127:
        ln = struct.unpack(">Q", _recv_exact(sock, 8))[0]
    if ln > MAX_FRAME:
        raise ValueError(f"帧过大（{ln} 字节 > {MAX_FRAME}），断开")
    mask = _recv_exact(sock, 4) if masked else b""
    payload = _recv_exact(sock, ln) if ln else b""
    if masked:
        payload = bytes(payload[i] ^ mask[i % 4] for i in range(len(payload)))
    return opcode, payload


class ComfyProgress:
    """
    连上 ComfyUI 的 `/ws`，把进度回调出来。**后台线程**，不阻塞渲染主循环。

    用法：
        with ComfyProgress("127.0.0.1", 8188, on_progress=cb) as p:
            comfy.wait(pid, on_tick=p.tick)   # on_tick 里能拿到步级进度

    `on_progress(ev)` 会收到 dict：
        {"type": "progress", "value": N, "max": M, "prompt_id": ...}
        {"type": "executing", "node": ..., "prompt_id": ...}
        {"type": "done", "prompt_id": ...}
        {"type": "text", "text": "...", "node": ...}    ← send_progress_text（二进制 TEXT 帧）
        {"type": "error", "error": "..."}
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 8188,
                 on_progress: Callable[[dict], None] | None = None,
                 timeout: float = 10.0):
        self.host, self.port = host, port
        self.on_progress = on_progress
        self.timeout = timeout
        self.client_id = f"vm-{os.getpid()}-{int(time.time() * 1000) % 100000}"
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # 最近一次进度（供 on_tick 这类同步调用方直接读，避免跨线程等待）
        self.latest: dict[str, Any] = {}
        self.error: str = ""

    # ── 生命周期 ──
    def start(self) -> "ComfyProgress":
        try:
            s = socket.create_connection((self.host, self.port), timeout=self.timeout)
            _handshake(s, f"{self.host}:{self.port}", f"/ws?clientId={self.client_id}", self.timeout)
            s.settimeout(self.timeout)
            self._sock = s
            self._thread = threading.Thread(target=self._loop, name="comfy-ws", daemon=True)
            self._thread.start()
        except Exception as e:                      # noqa: BLE001
            # 进度是**锦上添花**：连不上绝不能让渲染失败
            self.error = f"{type(e).__name__}: {e}"
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)

    def __enter__(self) -> "ComfyProgress":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # ── 回调 ──
    def _emit(self, ev: dict) -> None:
        self.latest = ev
        if self.on_progress:
            try:
                self.on_progress(ev)
            except Exception:                        # noqa: BLE001
                pass                                 # 回调自己的错不该拖垮连接

    def tick(self, *args: Any, **kw: Any) -> None:
        """可以塞给 `Comfy.wait(on_tick=…)` —— 它只负责把最新进度带出去。"""
        return None

    @property
    def summary(self) -> str:
        """给日志用的一行人话，例如 `12/20 步` 或 `节点 7`。"""
        ev = self.latest
        if not ev:
            return ""
        if ev.get("type") == "progress":
            v, m = ev.get("value"), ev.get("max")
            return f"{v}/{m} 步" + (f" {v / m * 100:.0f}%" if m else "")
        if ev.get("type") == "text":
            return str(ev.get("text") or "")[:40]
        if ev.get("type") == "executing" and ev.get("node"):
            return f"节点 {ev['node']}"
        return ""

    # ── 主循环 ──
    def _loop(self) -> None:
        assert self._sock is not None
        sock = self._sock
        while not self._stop.is_set():
            try:
                opcode, payload = _read_frame(sock)
            except (TimeoutError, socket.timeout):
                continue                              # 静默期正常
            except Exception as e:                    # noqa: BLE001
                if not self._stop.is_set():
                    self.error = f"{type(e).__name__}: {e}"
                return

            if opcode == 0x8:                         # close
                return
            if opcode == 0x9:                         # ping → pong（必须回，否则服务端会断）
                try:
                    sock.sendall(b"\x8a\x80" + b"\x00\x00\x00\x00")
                except OSError:
                    return
                continue
            if opcode == 0xA:                         # pong
                continue

            if opcode == 0x1:                         # 文本帧 → JSON
                try:
                    msg = json.loads(payload.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    continue
                t = msg.get("type")
                data = msg.get("data") or {}
                if t == "progress":
                    self._emit({"type": "progress", "value": data.get("value"),
                                "max": data.get("max"), "prompt_id": data.get("prompt_id")})
                elif t == "executing":
                    done = data.get("node") is None
                    self._emit({"type": "done" if done else "executing",
                                "node": data.get("node"), "prompt_id": data.get("prompt_id")})
                elif t == "execution_error":
                    self._emit({"type": "error",
                                "error": str(data.get("exception_message") or "执行错误")})
                elif t == "status":
                    q = (data.get("status") or {}).get("exec_info") or {}
                    self._emit({"type": "status", "pending": q.get("queue_remaining")})
            elif opcode == 0x2 and len(payload) >= 4:  # 二进制帧
                # 头 4 字节是事件类型（大端 uint32），1=TEXT（send_progress_text）
                etype = struct.unpack(">I", payload[:4])[0]
                if etype == 1 and len(payload) > 8:
                    n = struct.unpack(">I", payload[4:8])[0]
                    node = payload[8:8 + n].decode("utf-8", "replace")
                    text = payload[8 + n:].decode("utf-8", "replace")
                    self._emit({"type": "text", "text": text, "node": node})


def probe(host: str = "127.0.0.1", port: int = 8188, seconds: float = 4.0) -> dict:
    """自测：连上 /ws，收集 `seconds` 秒内的事件，返回统计。"""
    got: list[dict] = []
    p = ComfyProgress(host, port, on_progress=got.append)
    p.start()
    if p.error:
        return {"ok": False, "error": p.error, "events": 0}
    time.sleep(seconds)
    p.stop()
    kinds: dict[str, int] = {}
    for e in got:
        kinds[e["type"]] = kinds.get(e["type"], 0) + 1
    return {"ok": True, "client_id": p.client_id, "events": len(got),
            "kinds": kinds, "sample": got[-3:], "summary": p.summary}


if __name__ == "__main__":
    import sys
    h = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    pt = int(sys.argv[2]) if len(sys.argv) > 2 else 8188
    sec = float(sys.argv[3]) if len(sys.argv) > 3 else 4.0
    print(json.dumps(probe(h, pt, sec), ensure_ascii=False, indent=2))
