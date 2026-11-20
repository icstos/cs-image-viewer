"""预热窗口端到端真机验证（Windows，开发模式）。

验证双击极速响应的完整闭环：
1. 起一个 `IMAGEVIEWER_ROLE=hot` 的窗口进程 → 应装配完成但**保持隐藏**；
2. 出现 `hot-idle` 标记（就绪待命）；
3. 投递一个打开请求（模拟 VBS 启动器）；
4. 预热窗口领单 → 写 ack、清 idle、加载图片 → **窗口变可见**；
5. 写补位标记（请 daemon 补一个预热窗口）。

脚本自己拉起、自己关窗、自己清理进程，用独立 IMAGEVIEWER_HOME 隔离，
不碰用户真实缓存/注册表。

用法::

    py -3.12 tools/hot_window_check.py
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

TITLE = "ImageViewer"
WM_CLOSE = 0x0010
user32 = ctypes.windll.user32
user32.FindWindowW.restype = wt.HWND
user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
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


def main() -> int:
    image = ROOT / "output" / "smoke" / "01_grafana.jpg"
    if not image.is_file():
        print(f"缺少测试图片：{image}")
        return 2

    with tempfile.TemporaryDirectory(prefix="csiv-hot-") as tmp:
        # 本进程与子进程必须用同一个 HOME，否则 idle/ack 检查会看错目录
        os.environ["IMAGEVIEWER_HOME"] = tmp
        from cs_image_viewer.infra import dispatch

        env = os.environ.copy()
        env["IMAGEVIEWER_HOME"] = tmp
        env["IMAGEVIEWER_ROLE"] = "hot"
        env["CSIV_HOT_POOL"] = "0"          # 关闭自动池，由本脚本手动验证
        env["CSIV_SHARED"] = "0"            # 本地解码，避免连到用户当前 daemon

        # 1) 启动预热窗口
        child = subprocess.Popen(
            [sys.executable, str(ROOT / "main.py")],
            cwd=str(ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        try:
            print("[预热窗口装配]")
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and dispatch.idle_owner() is None:
                time.sleep(0.2)
            check("出现 hot-idle 就绪标记", dispatch.idle_owner() is not None)

            hwnd = user32.FindWindowW(None, TITLE)
            check("预热窗口已创建", bool(hwnd))
            if hwnd:
                visible = bool(user32.IsWindowVisible(hwnd))
                check("预热窗口保持隐藏", not visible, f"(visible={visible})")

            print("[投递打开请求]")
            t0 = time.monotonic()
            dispatch.publish_request(str(image))

            # 2) 等领单回执
            deadline = time.monotonic() + 10
            acked = False
            while time.monotonic() < deadline:
                acks = list(dispatch.paths.run_dir().glob("hot-*.ack"))
                if acks:
                    acked = True
                    break
                time.sleep(0.05)
            elapsed = (time.monotonic() - t0) * 1000
            check("预热窗口领单（ack 出现）", acked, f"({elapsed:.0f}ms)")
            check("idle 标记已清除", dispatch.idle_owner() is None)

            # 3) 等窗口变可见（补位标记在 reveal 之后写，需等窗口可见）
            deadline = time.monotonic() + 10
            became_visible = False
            while time.monotonic() < deadline:
                hwnd2 = user32.FindWindowW(None, TITLE)
                if hwnd2 and user32.IsWindowVisible(hwnd2):
                    became_visible = True
                    hwnd = hwnd2
                    break
                time.sleep(0.1)
            visible_at = (time.monotonic() - t0) * 1000
            check("领取后窗口变可见", became_visible, f"({visible_at:.0f}ms 从投递起)")
            check("极速响应 < 2s", became_visible and visible_at < 2000)

            # 补位请求由 _hot_standby 在 reveal 后投递
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not dispatch.replenish_path().is_file():
                time.sleep(0.05)
            check("补位请求已投递", dispatch.replenish_path().is_file())
        finally:
            # 关窗（flet.exe 客户端不随 python 退出，必须显式关）
            for _ in range(2):
                hwnd = user32.FindWindowW(None, TITLE)
                if not hwnd:
                    break
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
                time.sleep(1.5)
                if hwnd and user32.IsWindow(hwnd):
                    pid = wt.DWORD()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    if pid.value:
                        subprocess.run(["taskkill", "/F", "/PID", str(pid.value)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       check=False)
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()

    print(f"\n{_passed} 项通过 / {_failed} 项失败")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
