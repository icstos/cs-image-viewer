"""「打开请求」的跨进程投递与领取：让双击可以复用预热窗口，而不是每次新起 exe。

为什么需要这一层
----------------
``flet build`` 的 exe 是 Dart 外壳，每次冷启动要付一次 Flutter 引擎初始化的
固定成本（实测 ~0.7s）。要「极速响应」，唯一办法是让这个过程在用户双击之前
就发生：维护一个**隐藏的预热窗口**（已完成 Python 导入、UI 装配、连上共享
服务），双击只把文件路径投进来，预热窗口立刻显示图片，响应降到毫秒级。

投递协议（全部落在 ``paths.run_dir()``，纯文件、跨进程、无端口）
---------------------------------------------------------------
* ``open-request.txt``  启动器写入的目标路径（UTF-16），与 main.py 既有通道共用；
* ``hot-idle``          预热窗口「空闲待命」的标记，内容是它的 pid；
* ``hot-<pid>.ack``     预热窗口领单后写的回执（含目标路径）。

启动器（VBS）逻辑 —— 两条通道**严格二选一**：
1. 先判 ``hot-idle``（存在 + 进程活 + mtime 未超时）；
2. 有效 -> **只写** ``open-request.txt``、**不启动 exe**，预热窗口领单显示；
3. 失效 -> **只写** ``IMAGEVIEWER_OPEN`` 环境变量、**不写请求文件**，冷启动 exe。
   绝不能两条都写：冷启动 exe（读 env）与刚好上线的预热窗口（读请求文件）
   会各自打开同一张图，就是「一次双击开出多个相同窗口」的 BUG。

预热窗口逻辑：
1. 完成全部初始化后隐藏，写 ``hot-idle``（登记自己空闲）；
2. 后台轮询 ``open-request.txt``：发现新请求（内容变化 + mtime 更新）→
   写一个 ack，读走路径，删 ``hot-idle``（不再空闲），加载图片并显示；
3. 显示后请求 daemon 再补一个预热窗口。

关键正确性点：
* **领单原子化**：``take_request()`` 用 ``os.replace`` 把请求文件原子改名到
  进程私有名，并发下只有一个进程能成功 —— 杜绝 TOCTOU 双领；
* **先写唯一 ack 再删 idle** + mtime 双判据，避免两个预热窗口重复显示；
* **预热窗口装配期不消费请求**（``_resolve_start_path(take_request_file=False)``），
  否则它加载图片却保持隐藏，会把这次双击变成丢单。
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from . import paths

REQUEST_FILE = "open-request.txt"     # 与 association.py / main.py 保持一致
IDLE_FILE = "hot-idle"                # 预热窗口空闲标记（内容是 pid）
REPLENISH_FILE = "hot-replenish"      # 转正后请求 daemon 补一个预热窗口
ACK_PREFIX = "hot-"                   # + "<pid>.ack"：领单回执
ACK_SUFFIX = ".ack"
REQUEST_MAX_AGE = 15.0                # 秒：超过视为残留
IDLE_MAX_AGE = 8.0                    # 秒：预热窗口心跳超时（超过认为它已死）


def request_path() -> Path:
    # 与 association.py 的 VBS 启动器、main.py 的 _resolve_start_path 共用同一路径
    return paths.app_dir() / REQUEST_FILE


def idle_path() -> Path:
    return paths.run_dir() / IDLE_FILE


def ack_path(pid: int) -> Path:
    return paths.run_dir() / f"{ACK_PREFIX}{pid}{ACK_SUFFIX}"


def _alive(pid: int) -> bool:
    """目标 pid 是否仍活着（Windows 用 OpenProcess，其余用 signal 0）。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def publish_request(target: str) -> Path:
    """启动器写入打开请求；UTF-16 与既有 VBS 通道一致。返回请求文件路径。"""
    root = paths.ensure(paths.app_dir())
    dest = root / REQUEST_FILE
    dest.write_text(target, encoding="utf-16")
    return dest


def take_request(*, max_age: float = REQUEST_MAX_AGE) -> str | None:
    """**原子地**认领并删除一个有效请求；无请求/已过期返回 None。

    为什么必须原子：双击时冷启动 exe 与预热窗口可能同时尝试消费同一个请求文件。
    原来的「先 read_text 再 unlink」有竞态 —— 两个进程都可能读到同一条路径，各自
    打开一个窗口，正是「双击一次开多个窗口」的根因。这里改用 ``os.replace`` 把
    请求文件原子地改名为一个进程私有的临时名（同一文件系统上 rename 是原子的,
    并发下只有一个进程能成功），成功者独占内容，其余拿到 FileNotFoundError。

    无条件认领（无论是否采用都删除），避免残留请求让下次启动误开旧文件。
    """
    path = request_path()
    # claim 名必须包含线程标识：同进程内多个消费者共用 pid，若只用 pid 命名，
    # 先到者的 claim 会被后到者的 os.replace 覆盖，导致所有线程都领不到。
    # 真实多进程场景 pid 已足够，加上线程 id 让两种并发都安全。
    suffix = f"{os.getpid()}.{threading.get_ident()}"
    claim = path.with_name(f"{path.name}.{suffix}.claim")
    try:
        os.replace(path, claim)              # 原子：并发下只有一个消费者成功
    except (OSError, FileNotFoundError):
        return None
    try:
        age = time.time() - claim.stat().st_mtime
        text = claim.read_text(encoding="utf-16").strip()
    except (OSError, UnicodeError):
        return None
    finally:
        try:
            claim.unlink()
        except OSError:
            pass
    return text if text and age <= max_age else None


def idle_owner(*, max_age: float = IDLE_MAX_AGE) -> int | None:
    """返回空闲预热窗口的 pid；无有效预热窗口返回 None。

    有效性 = 标记文件足够新 **且** 对应进程仍活着。两个判据缺一不可：
    mtime 防「标记忘了删」，进程存活防「窗口崩了标记还在」。
    """
    path = idle_path()
    try:
        if not path.is_file():
            return None
        age = time.time() - path.stat().st_mtime
        pid = int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None
    if age > max_age or not _alive(pid):
        return None
    return pid


def mark_idle(pid: int) -> None:
    """登记自己（pid）为空闲预热窗口，并刷新心跳时间。"""
    run = paths.ensure(paths.run_dir())
    (run / IDLE_FILE).write_text(str(pid), encoding="ascii")


def clear_idle(pid: int | None = None) -> None:
    """注销空闲标记；给了 pid 时只注销自己的，避免误删别人的。"""
    path = idle_path()
    if pid is not None:
        owner = idle_owner()
        if owner is not None and owner != pid:
            return
    try:
        path.unlink()
    except OSError:
        pass


def write_ack(pid: int, target: str) -> None:
    """领单回执：诊断用，记录是哪个预热窗口领走了哪张图。"""
    try:
        paths.ensure(paths.run_dir())
        ack_path(pid).write_text(target, encoding="utf-8")
    except OSError:
        pass


def replenish_path() -> Path:
    return paths.run_dir() / REPLENISH_FILE


def request_replenish() -> None:
    """预热窗口转正后调用：请 daemon 补一个空闲预热窗口（幂等）。"""
    try:
        paths.ensure(paths.run_dir())
        replenish_path().write_text(str(int(time.time())), encoding="ascii")
    except OSError:
        pass


def take_replenish(*, max_age: float = 30.0) -> bool:
    """daemon 侧消费补位请求；无请求/已过期返回 False。"""
    path = replenish_path()
    try:
        if not path.is_file():
            return False
        age = time.time() - path.stat().st_mtime
    except OSError:
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return age <= max_age


def clear_stale_acks() -> None:
    """清理超过 1 分钟的旧回执，避免目录无限增长。"""
    run = paths.run_dir()
    try:
        entries = list(run.glob(f"{ACK_PREFIX}*{ACK_SUFFIX}"))
    except OSError:
        return
    now = time.time()
    for entry in entries:
        try:
            if now - entry.stat().st_mtime > 60:
                entry.unlink()
        except OSError:
            pass
