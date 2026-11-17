"""窗口外观 —— Windows 原生的三件小事，全部用 ``ctypes``，不加依赖。

启动顺序要求「先布置、后显示」，而 Flutter 的窗口在显示之前就已经存在（只是
不可见），所以这三件事必须在显示**之前**做完：

1. **暗色标题栏**：DWM 属性一旦窗口显示再改，用户会先看到一条白边；
2. **居中**：隐藏状态下移动窗口，用户看不到任何跳动；
3. **看门狗**：万一显示流程抛错，也不能留下一个「看不见的进程」——几秒后
   兜底把窗口拉出来，用户至少能看到界面和错误提示。

失败一律静默：这些是锦上添花的平台细节，绝不能因为某个 API 版本差异让程序起不来。

**窗口归属**：打包版里窗口与解释器同进程；开发模式下窗口由 ``flet.exe`` 客户端
进程创建，两者 pid 不同。因此所有入口都接受一个可选的窗口标题，pid 找不到时按
标题回落（细节见 :func:`flutter_windows`）——否则开发期暗色标题栏与看门狗都是空转。
"""

from __future__ import annotations

import os
import threading
import time

__all__ = [
    "apply_dark_title_bar",
    "ensure_visible",
    "flutter_windows",
    "is_visible",
    "start_show_watchdog",
]

#: Flutter 桌面端的 Win32 窗口类名（flet 的 windows runner 固定用它）。
_WINDOW_CLASS = "FLUTTER_RUNNER_WIN32_WINDOW"


def flutter_windows(title: str | None = None) -> list[int]:
    """枚举本应用的 Flutter 顶层窗口句柄（隐藏状态下同样能取到）。

    两条通道，先快后慢：

    1. **本进程 pid** —— 打包版（``flet build``）里 Python 被嵌进 Flutter 进程，
       窗口与解释器同 pid，这是主通道；
    2. **窗口标题** —— 开发模式（``py main.py``）下窗口由 ``flet.exe`` 客户端进程
       创建，与解释器**不同 pid**，只按 pid 找会得到空列表，于是暗色标题栏和
       看门狗在开发期全部失效（表现为深色界面顶着一条白标题栏）。给标题时退回
       按标题精确匹配，两个环境行为一致。

    标题通道要求类名仍是 ``FLUTTER_RUNNER_WIN32_WINDOW``：宁可找不到，也不要
    误伤一个同名但不属于本应用的窗口。
    """
    if os.name != "nt":
        return []
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        pid = os.getpid()
        by_pid: list[int] = []
        by_title: list[int] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _collect(hwnd: int, _lparam: int) -> bool:
            name = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, name, 64)
            if name.value != _WINDOW_CLASS:
                return True
            if title is not None:
                caption = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(hwnd, caption, 256)
                if caption.value == title:
                    by_title.append(hwnd)
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid:
                by_pid.append(hwnd)
            return True

        user32.EnumWindows(_collect, 0)
        return by_pid or by_title
    except Exception:
        return []


def apply_dark_title_bar(title: str | None = None) -> None:
    """把标题栏切成深色（DWM），避免深色界面顶一条白边。"""
    if os.name != "nt":
        return
    try:
        import ctypes

        dwmapi = ctypes.windll.dwmapi
        enabled = ctypes.c_int(1)
        for hwnd in flutter_windows(title):
            # 20 = Windows 11 起的 DWMWA_USE_IMMERSIVE_DARK_MODE，19 = 旧版编号
            for attribute in (20, 19):
                dwmapi.DwmSetWindowAttribute(
                    hwnd, attribute, ctypes.byref(enabled), ctypes.sizeof(enabled)
                )
    except Exception:
        pass


def is_visible(hwnd: int) -> bool:
    """窗口当前是否可见（查询失败按「可见」处理，避免看门狗反复折腾）。"""
    try:
        import ctypes

        return bool(ctypes.windll.user32.IsWindowVisible(hwnd))
    except Exception:
        return True


def ensure_visible(title: str | None = None) -> None:
    """兜底：窗口若因任何异常仍处于隐藏状态，直接由 Win32 显示出来。"""
    if os.name != "nt":
        return
    try:
        import ctypes

        user32 = ctypes.windll.user32
        for hwnd in flutter_windows(title):
            if not user32.IsWindowVisible(hwnd):
                user32.ShowWindow(hwnd, 1)      # SW_SHOWNORMAL
            user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def start_show_watchdog(delay: float = 3.0, title: str | None = None) -> None:
    """看门狗：``delay`` 秒后若仍有窗口不可见，就强制显示。"""
    if os.name != "nt":
        return

    def _later() -> None:
        time.sleep(delay)
        if any(not is_visible(hwnd) for hwnd in flutter_windows(title)):
            ensure_visible(title)

    threading.Thread(target=_later, name="csiv-show-watchdog", daemon=True).start()
