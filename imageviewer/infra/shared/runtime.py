"""进程间协商用的运行时设施：单实例锁、服务注册表、进程存活探测。

只有一条规则需要记住：**谁拿到单实例锁，谁就是「主进程」（服务的宿主）**。
锁是操作系统级的（Windows `msvcrt.locking` / POSIX `flock`），
所以宿主进程无论是正常退出还是崩溃，锁都会被内核释放 —— 不会留下
「文件还在、进程已死」的僵尸状态，这正是裸 pid 文件做不到的事。

其余窗口都是「轻量子进程」：读注册表拿到端口与令牌，连上去用宿主共享的缓存。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from .. import paths

REGISTRY_NAME = "service.json"
LOCK_NAME = "service.lock"


# ---------------------------------------------------------------------------
# 单实例锁
# ---------------------------------------------------------------------------


class SingletonLock:
    """独占文件锁：拿得到 = 本进程当宿主；拿不到 = 已有宿主在跑。"""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else paths.run_dir() / LOCK_NAME
        self._fd: int | None = None

    def acquire(self) -> bool:
        """非阻塞尝试加锁；成功返回 True（并一直持有到 release 或进程退出）。"""
        if self._fd is not None:
            return True
        paths.ensure(self.path.parent)
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError:
            return False
        try:
            if os.fstat(fd).st_size == 0:      # Windows 的字节锁要求范围真实存在
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)                       # 已被别的进程持有
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 服务注册表
# ---------------------------------------------------------------------------


class Registry:
    """`run/service.json`：宿主把「端口 + 令牌 + pid」写在这里供子进程发现。

    写入用「临时文件 + 原子替换」，避免子进程读到写了一半的 JSON。
    """

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else paths.run_dir() / REGISTRY_NAME

    def write(self, port: int, token: str, pid: int | None = None) -> None:
        payload = {
            "pid": pid or os.getpid(),
            "port": int(port),
            "token": token,
            "started": round(time.time(), 3),
        }
        paths.ensure(self.path.parent)
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    def read(self) -> dict | None:
        """读注册表；内容残缺/过期（宿主已死）时返回 None。"""
        try:
            info = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(info, dict) or "port" not in info or "token" not in info:
            return None
        pid = info.get("pid")
        if isinstance(pid, int) and pid != os.getpid() and not pid_alive(pid):
            return None                        # 宿主已退出，注册表是残留
        return info

    def clear(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 进程存活探测
# ---------------------------------------------------------------------------


def pid_alive(pid: int) -> bool:
    """目标进程是否还活着（用于判断注册表是否为残留）。"""
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
            if not handle:
                return False
            try:
                return kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return True           # 探测失败时保守认为存活，交给连接超时兜底
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class Counter:
    """线程安全的计数器（服务统计用，读多写多但都是单次自增）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, int] = {}

    def add(self, name: str, delta: int = 1) -> None:
        with self._lock:
            self._values[name] = self._values.get(name, 0) + delta

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._values)


__all__ = ["Counter", "Registry", "SingletonLock", "pid_alive"]
