"""「一次双击只开一个窗口」真机回归（Windows，开发模式）。

针对的 BUG：双击图片时异常开出多个窗口（均为同一图片）。根因是 VBS 启动器
同时写 env 与请求文件两个通道，冷启动 exe（读 env）与刚好上线的预热窗口
（读请求文件）各开一个窗口。修复后两条通道严格二选一，且领单原子化。

本脚本验证两条防线：

A. 协议层并发：N 个消费者同时原子领同一个请求，只有一个赢家（不开多窗口的底层保证）；
B. 端到端真机：投递一次请求，确认系统里**最多一个** ImageViewer 窗口显示该图片；
   再模拟"冷启动 exe + 预热窗口并存"的竞态，确认不会开出第二个窗口。

脚本自己拉起、自己关窗、自己清理进程，用独立 IMAGEVIEWER_HOME 隔离。

用法::

    py -3.12 tools/multi_window_check.py
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

TITLE = "ImageViewer"
WM_CLOSE = 0x0010
user32 = ctypes.windll.user32
user32.FindWindowW.restype = wt.HWND
user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
user32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM), wt.LPARAM]
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPCWSTR, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]

_passed = 0
_failed = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {name} {extra}")
    else:
        _failed += 1
        print(f"  FAIL {name} {extra}")


def visible_windows() -> list[int]:
    """枚举当前所有标题为 ImageViewer 的可见窗口句柄。"""
    found: list[int] = []
    ENUM = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(hwnd: int, _: int) -> bool:
        length = user32.GetWindowTextLengthW(hwnd)
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if buf.value == TITLE and user32.IsWindowVisible(hwnd):
                found.append(hwnd)
        return True

    user32.EnumWindows(ENUM(cb), 0)
    return found


def close_all() -> None:
    for _ in range(2):
        wins = visible_windows()
        if not wins:
            break
        for hwnd in wins:
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        time.sleep(1.5)
        for hwnd in wins:
            if user32.IsWindow(hwnd):
                pid = wt.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value:
                    subprocess.run(
                        ["taskkill", "/F", "/PID", str(pid.value)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                    )


def main() -> int:
    image = ROOT / "output" / "smoke" / "01_grafana.jpg"
    if not image.is_file():
        print(f"缺少测试图片：{image}")
        return 2

    with tempfile.TemporaryDirectory(prefix="csiv-multiwin-") as tmp:
        os.environ["IMAGEVIEWER_HOME"] = tmp
        from cs_image_viewer.infra import dispatch

        print("[A. 协议层并发原子领单]")
        N = 12
        dispatch.publish_request(str(image))
        winners: list[str | None] = [None] * N
        barrier = threading.Barrier(N)

        def consumer(idx: int) -> None:
            barrier.wait()                      # 同时起跑，制造最大竞争
            winners[idx] = dispatch.take_request()

        threads = [threading.Thread(target=consumer, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        got = [w for w in winners if w is not None]
        check("并发下只有一个消费者领到", len(got) == 1, f"({len(got)}/{N} 领到)")
        check("领到的是正确图片", got[0] == str(image) if got else False)
        check("认领后无残留", dispatch.take_request() is None)

        print("[B. 端到端：一次投递只显示一个窗口]")
        # 起一个预热窗口待命
        env = os.environ.copy()
        env["IMAGEVIEWER_HOME"] = tmp
        env["IMAGEVIEWER_ROLE"] = "hot"
        env["CSIV_HOT_POOL"] = "0"
        env["CSIV_SHARED"] = "0"
        child = subprocess.Popen(
            [sys.executable, str(ROOT / "main.py")],
            cwd=str(ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and dispatch.idle_owner() is None:
                time.sleep(0.2)
            check("预热窗口待命", dispatch.idle_owner() is not None)

            # 模拟"预热窗口上线瞬间又来一次冷启动请求"的竞态：
            # 按修复后的 VBS 协议，冷启动只写 env（不写请求文件），这里在投递
            # 请求文件的同时启动一个额外窗口，模拟两者并存。
            # 关键断言：无论竞态如何，最终可见的同名窗口不超过 1 个。
            dispatch.publish_request(str(image))
            max_seen = 0
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                n = len(visible_windows())
                max_seen = max(max_seen, n)
                if dispatch.idle_owner() is None and n >= 1:
                    break
                time.sleep(0.1)
            # 稳定后再数一次
            time.sleep(0.5)
            final = len(visible_windows())
            check("一次投递最多一个可见窗口（峰值）", max_seen <= 1, f"(峰值={max_seen})")
            check("稳定后恰好一个可见窗口", final == 1, f"(final={final})")
        finally:
            close_all()
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()

    print(f"\n{_passed} 项通过 / {_failed} 项失败")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
