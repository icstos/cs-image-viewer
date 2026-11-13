"""窗口子进程这一侧：发现宿主、连上去、宿主没了就自己接手。

三种后端，同一个 `call(op, **kwargs)` 接口（`RemoteImageManager` 只认这个接口）：

`ServiceClient`   —— 跨进程：走 loopback TCP，从磁盘缓存读 blob；
`DirectBackend`   —— 同进程：主进程里的窗口直接调服务对象，连 socket 都省了；
`ServiceLink`     —— 门面：负责「先找现成的宿主，找不到就当宿主」，
                     以及连接断开时的重连/接管（服务迁移）。

为什么要「接管」：宿主是某个窗口进程（Flet 的窗口 = 进程，没有独立的
Python 运行时可寄生），用户关掉第一个窗口时，剩下的窗口不能就此失去共享缓存。
它们各自持有单实例锁的竞争者身份，宿主一消失，先探测到的那一个就会成为新宿主；
由于服务是无状态的（缓存都在磁盘上），交接对用户完全透明。
"""
from __future__ import annotations

import os
import secrets
import socket
import threading
import time
from typing import Any, Callable

from . import protocol
from .core import ImageService
from .protocol import ServiceError
from .runtime import Registry, SingletonLock, pid_alive
from .server import ServiceServer

DEFAULT_CALL_TIMEOUT = 8.0        # 单次调用上限（大图首次渲染留足余量）
CONNECT_BUDGET = 1.5              # 首次寻找/建立服务的总预算（超出就先用本地解码）


class ServiceClient:
    """一条到宿主的连接。调用串行化：同一个窗口的多个线程共用一条 socket。"""

    mode = "remote"

    def __init__(self, port: int, token: str, timeout: float = DEFAULT_CALL_TIMEOUT):
        self.port = int(port)
        self.pid = 0
        self._lock = threading.Lock()
        self._next_id = 0
        self._sock: socket.socket | None = None
        try:
            sock = socket.create_connection(("127.0.0.1", self.port), timeout=timeout)
        except OSError as exc:
            raise ServiceError(f"无法连接共享服务：{exc}", transport=True) from None
        protocol.tune(sock)
        sock.settimeout(timeout)
        self._sock = sock
        try:
            reply = self._exchange("hello", {"token": token})
            self.pid = int(reply.get("pid", 0) or 0)
        except ServiceError:
            self.close()
            raise

    # ------------------------------------------------------------------

    def call(self, op: str, **kwargs: Any) -> dict:
        return self._exchange(op, kwargs)

    def _exchange(self, op: str, args: dict) -> dict:
        sock = self._sock
        if sock is None:
            raise ServiceError("连接已关闭", transport=True)
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            try:
                protocol.send(sock, {"id": request_id, "op": op, "args": args})
                reply = protocol.recv(sock)
            except (OSError, ServiceError) as exc:
                self.close()
                if isinstance(exc, ServiceError):
                    raise
                raise ServiceError(f"与服务通信失败：{exc}", transport=True) from None
        if reply.get("id") not in (request_id, 0):
            self.close()
            raise ServiceError("应答错位", transport=True)
        if not reply.get("ok"):
            raise ServiceError(str(reply.get("error") or "服务返回失败"))
        return reply.get("result") or {}

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def describe(self) -> str:
        return f"127.0.0.1:{self.port} (pid {self.pid})"


class DirectBackend:
    """同进程直连：宿主窗口自己用，零序列化、零往返。"""

    mode = "direct"

    def __init__(self, service: ImageService):
        self.service = service
        self.pid = os.getpid()

    def call(self, op: str, **kwargs: Any) -> dict:
        handler = getattr(self.service, f"op_{op}", None)
        if handler is None:
            raise ServiceError(f"未知指令：{op}")
        try:
            return handler(**kwargs)
        except ServiceError:
            raise
        except Exception as exc:
            raise ServiceError(f"{exc.__class__.__name__}: {exc}") from None

    def close(self) -> None:
        pass

    def describe(self) -> str:
        return f"本进程直连 (pid {self.pid})"


class ServiceHost:
    """本进程成为「主进程」：持有单实例锁 + 服务实例 + 服务端。"""

    def __init__(self, lock: SingletonLock, service: ImageService,
                 server: ServiceServer, registry: Registry, token: str):
        self.lock = lock
        self.service = service
        self.server = server
        self.registry = registry
        self.token = token
        self.started = time.time()

    @classmethod
    def try_start(cls, registry: Registry | None = None) -> ServiceHost | None:
        """抢锁并启动服务；抢不到（已有宿主）返回 None。"""
        registry = registry or Registry()
        lock = SingletonLock()
        if not lock.acquire():
            return None
        try:
            token = secrets.token_hex(16)
            service = ImageService()
            server = ServiceServer(service, token)
            port = server.start()
            registry.write(port, token)
            return cls(lock, service, server, registry, token)
        except Exception:
            registry.clear()
            lock.release()
            return None

    def stop(self) -> None:
        """停止服务并释放锁（进程退出时由内核兜底，这里是为了优雅收尾）。"""
        try:
            self.registry.clear()
        finally:
            try:
                self.server.stop()
            finally:
                self.lock.release()

    @property
    def direct(self) -> DirectBackend:
        return DirectBackend(self.service)


class ServiceLink:
    """窗口与共享服务之间的唯一入口（`RemoteImageManager` 持有它）。

    `call()` 的语义：**永远尽力而为**——连接断了就重连，宿主退出了就自己当宿主；
    实在不行抛 `ServiceError`，由上层降级为本地解码。
    """

    def __init__(self, enabled: bool | None = None):
        if enabled is None:
            enabled = os.environ.get("CSIV_SHARED", "1") not in ("0", "false", "off")
        self.enabled = enabled
        self.registry = Registry()
        self._lock = threading.RLock()
        self._backend: ServiceClient | DirectBackend | None = None
        self._host: ServiceHost | None = None
        self.acquires = 0                     # 统计：重建连接的次数（诊断用）
        self.recoveries = 0                   # 统计：接管/重连成功的次数
        self._next_retry = 0.0                # 失败退避：别让界面反复卡在连接上
        # 由 main.py 注入：主进程不见了时用它再把主进程拉起来，
        # 窗口因此不必「兼职当宿主」（打包态也能保持真正的主进程 + 窗口进程模型）。
        self.master_starter: Callable[[], bool] | None = None

    # ------------------------------------------------------------------

    @property
    def backend(self):
        return self._backend

    def acquire(self, budget: float = CONNECT_BUDGET, allow_host: bool = True) -> bool:
        """找到或建立一个可用后端；返回是否成功。

        `allow_host=False` 时**不允许**本进程兼任宿主：这是窗口进程的默认姿态 ——
        窗口只做客户端，主进程缺失时应当由调用方先把它拉起来（见 `master_starter`），
        而不是让某个窗口悄悄变成缓存服务的主人。

        失败后 5 秒内不再重试：调用方是界面线程，反复尝试只会让每次切图
        都卡一下。5 秒足够一个正在启动的宿主把注册表写完。
        """
        if not self.enabled:
            return False
        with self._lock:
            if self._backend is not None:
                return True
            if time.monotonic() < self._next_retry:
                return False
            self.acquires += 1
            deadline = time.monotonic() + max(0.05, budget)
            while True:
                if self._connect_existing():
                    return True
                if allow_host and self._become_host():
                    return True
                if time.monotonic() >= deadline:
                    self._next_retry = time.monotonic() + 5.0
                    return False
                time.sleep(0.05)              # 主进程正在启动/写注册表，稍等再看

    def call(self, op: str, **kwargs: Any) -> dict:
        """调用服务；传输层故障时自动重连一次（含接管宿主）。"""
        backend = self._backend
        if backend is None:
            if not self.acquire():
                raise ServiceError("共享服务不可用")
            backend = self._backend
            if backend is None:
                raise ServiceError("共享服务不可用")
        try:
            return backend.call(op, **kwargs)
        except ServiceError as exc:
            if not exc.transport:
                raise                     # 业务错误：重连也没用
            self._recover()
            backend = self._backend
            if backend is None:
                raise exc
            return backend.call(op, **kwargs)

    def status(self) -> dict:
        """服务状态（菜单「缓存状态」用）：角色 + 服务端统计。"""
        if self._host is not None:
            role = "主进程（本窗口兼任）"
        elif self._backend is not None:
            role = "窗口进程"
        else:
            role = "未连接"
        info: dict[str, Any] = {
            "role": role,
            "backend": self._backend.describe() if self._backend else "-",
            "is_host": self._host is not None,
            "acquires": self.acquires,
            "recoveries": self.recoveries,
        }
        try:
            info.update(self.call("stats"))
        except ServiceError as exc:
            info["error"] = str(exc)
        return info

    def close(self) -> None:
        with self._lock:
            if self._backend is not None:
                self._backend.close()
                self._backend = None
            if self._host is not None:
                self._host.stop()
                self._host = None

    # ------------------------------------------------------------------

    def _connect_existing(self) -> bool:
        """按注册表连上已有宿主；注册表过期/连不上都返回 False。"""
        if self._host is not None:            # 自己已经是宿主，不需要连别人
            return False
        info = self.registry.read()
        if not info:
            return False
        pid = info.get("pid")
        if isinstance(pid, int) and pid != os.getpid() and not pid_alive(pid):
            return False
        try:
            client = ServiceClient(info["port"], info["token"], timeout=DEFAULT_CALL_TIMEOUT)
        except (ServiceError, KeyError, OSError):
            return False
        self._backend = client
        return True

    def _become_host(self) -> bool:
        """抢单实例锁成为宿主；本进程窗口随后直接走 DirectBackend。"""
        if self._host is not None:
            return True
        host = ServiceHost.try_start(self.registry)
        if host is None:
            return False
        self._host = host
        self._backend = host.direct
        return True

    def _recover(self) -> None:
        """传输故障后的恢复：重新找主进程 → 拉起来 → 最后才自己兼职宿主。

        **绝不在这里释放单实例锁**：一旦释放，另一个窗口可能立刻抢到锁并
        同时充当宿主，出现两个缓存服务互相覆盖注册表的局面。自己是宿主时
        服务就在本进程内，重建 backend 即可。
        """
        with self._lock:
            if self._backend is not None:
                self._backend.close()
                self._backend = None
            if self._host is not None:
                self._backend = self._host.direct
                return
            self._next_retry = 0.0            # 故障恢复时无视退避
            if self.try_connect(0.8):
                self.recoveries += 1
                return
            if self.master_starter is not None and self.master_starter():
                if self.try_connect(4.0):
                    self.recoveries += 1
                    return
            # 主进程起不来（缺运行时/只读目录）：退回本窗口兼任宿主
            if self.try_connect(0.5, allow_host=True):
                self.recoveries += 1

    def try_connect(self, budget: float = CONNECT_BUDGET, allow_host: bool = False) -> bool:
        """一次显式的接入尝试：**不受失败退避影响**。

        退避是给「界面线程被动调用」准备的（避免每次切图都卡一下）；
        启动引导与故障恢复是明确的决策点，必须真的去试。
        """
        with self._lock:
            self._next_retry = 0.0
            return self.acquire(budget=budget, allow_host=allow_host)


# ---------------------------------------------------------------------------
# 便捷函数
# ---------------------------------------------------------------------------

_default_link: ServiceLink | None = None
_default_lock = threading.Lock()


def default_link() -> ServiceLink:
    """进程级单例：一个窗口进程只维护一条到服务的连接。"""
    global _default_link
    with _default_lock:
        if _default_link is None:
            _default_link = ServiceLink()
        return _default_link


def shutdown_default_link() -> None:
    """进程退出前收尾（宿主会顺带释放单实例锁）。"""
    global _default_link
    with _default_lock:
        link, _default_link = _default_link, None
    if link is not None:
        link.close()
