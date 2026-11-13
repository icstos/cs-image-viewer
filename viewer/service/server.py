"""共享服务的网络外壳：把 `ImageService` 挂到 loopback TCP 上。

为什么每个连接一个线程而不是 asyncio：窗口只有几个、请求都是「一来一回」，
线程模型更直观，也不会因为某个窗口在等一张 100MP 的 RAW 解码而卡住别人
（解码本身在服务内部还有一把渲染锁控制并发度）。

安全边界：
- 只绑 127.0.0.1（不接受外部连接）；
- 首帧必须带随机令牌，令牌只存在于当前用户的 `run/service.json` 里，
  所以同一台机器上的其它账户/程序无法借用这个服务；
- 任何异常都转成 `{ok: false, error}` 回给客户端，不让一个坏文件打死服务。
"""
from __future__ import annotations

import logging
import socket
import threading
import traceback
from typing import Any

from . import protocol
from .core import ImageService
from .protocol import ServiceError

log = logging.getLogger("csiv.service")

IDLE_TIMEOUT = 300.0     # 秒：连接空闲多久算「没动静」（不是断开，仅防止线程永久阻塞）


class ServiceServer:
    """把 `ImageService` 暴露为本地 RPC；一个实例就是一个「主进程」的服务端。"""

    def __init__(
        self,
        service: ImageService,
        token: str,
        host: str = "127.0.0.1",
        backlog: int = 16,
    ):
        self.service = service
        self.token = token
        self.host = host
        self.port = 0
        self._backlog = backlog
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._clients = 0
        self._clients_lock = threading.Lock()
        self._conns: set[socket.socket] = set()
        self._conns_lock = threading.Lock()

    # ------------------------------------------------------------------

    def start(self) -> int:
        """绑定端口并开始接受连接；返回实际端口（端口 0 时由系统分配）。"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, 0))
        sock.listen(self._backlog)
        sock.settimeout(0.5)                 # 让 accept 能被 stop() 打断
        self._sock = sock
        self.port = sock.getsockname()[1]
        self._thread = threading.Thread(target=self._accept_loop, name="csiv-accept", daemon=True)
        self._thread.start()
        log.info("服务端已启动：127.0.0.1:%s", self.port)
        return self.port

    def stop(self) -> None:
        self._stop.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        # 主动断开所有窗口连接：客户端会立刻感知到 EOF 并触发接管/重连，
        # 不必干等一次请求超时（宿主正常退出时尤其重要）
        with self._conns_lock:
            conns = list(self._conns)
            self._conns.clear()
        for conn in conns:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
        self.service.close()

    # ------------------------------------------------------------------

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            sock = self._sock
            if sock is None:
                return
            try:
                conn, _addr = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(
                target=self._serve_client, args=(conn,), name="csiv-client", daemon=True
            ).start()

    def _serve_client(self, conn: socket.socket) -> None:
        protocol.tune(conn)
        # 空闲超时：窗口可能几十分钟不动。超时只代表「没话说」，不是断开，
        # 因此继续等（真正的断开由对端 EOF 或写失败暴露）。
        conn.settimeout(IDLE_TIMEOUT)
        with self._conns_lock:
            self._conns.add(conn)
        try:
            hello = protocol.recv(conn)
            if hello.get("op") != "hello" or hello.get("args", {}).get("token") != self.token:
                protocol.send(conn, {"id": 0, "ok": False, "error": "令牌无效"})
                return
            protocol.send(conn, {
                "id": hello.get("id", 0), "ok": True,
                "result": {"pid": self.service.op_stats()["pid"], "clients": self._bump(1)},
            })
            while not self._stop.is_set():
                try:
                    message = protocol.recv(conn)
                except socket.timeout:
                    continue
                protocol.send(conn, self._dispatch(message))
        except (ServiceError, OSError):
            pass                              # 窗口关掉/网络断开：正常收尾
        except Exception:
            log.debug("客户端连接异常\n%s", traceback.format_exc())
        finally:
            self._bump(-1)
            with self._conns_lock:
                self._conns.discard(conn)
            try:
                conn.close()
            except OSError:
                pass

    def _dispatch(self, message: dict[str, Any]) -> dict[str, Any]:
        """把一条请求路由到 `ImageService.op_*`。"""
        request_id = message.get("id", 0)
        op = str(message.get("op", ""))
        args = message.get("args") or {}
        handler = getattr(self.service, f"op_{op}", None)
        if handler is None or not callable(handler):
            return {"id": request_id, "ok": False, "error": f"未知指令：{op}"}
        try:
            return {"id": request_id, "ok": True, "result": handler(**args)}
        except ServiceError as exc:
            return {"id": request_id, "ok": False, "error": str(exc)}
        except TypeError as exc:
            return {"id": request_id, "ok": False, "error": f"参数错误：{exc}"}
        except Exception as exc:
            log.debug("指令 %s 失败\n%s", op, traceback.format_exc())
            return {"id": request_id, "ok": False, "error": f"{exc.__class__.__name__}: {exc}"}

    def _bump(self, delta: int) -> int:
        with self._clients_lock:
            self._clients = max(0, self._clients + delta)
            return self._clients

    @property
    def clients(self) -> int:
        with self._clients_lock:
            return self._clients
