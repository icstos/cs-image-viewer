"""系统级查询 —— 只有三件事：是不是 Windows、当前 exe 在哪、Ctrl 是否按着。

为什么需要它们：

* **Ctrl 状态**：Flet 1.1.x 的键盘事件上报的组合键标志在失焦 / 输入法介入时
  会漏报，滚轮缩放因此偶尔失灵。用 ``GetAsyncKeyState`` 直接问系统是可靠兜底。
* **exe 路径**：文件关联要把「当前程序」写进注册表；打包态下 ``sys.executable``
  指向的是内嵌解释器而不是那个 exe，必须走 ``GetModuleFileNameW``。
* **is_windows**：注册表相关能力只在 Windows 存在，其余平台要给出明确提示而不是
  抛一个看不懂的 ``AttributeError``。

全部函数在失败时返回中性值（``False`` / ``None``），调用方据此决定是否降级；
它们从不抛异常，因为「查不到 Ctrl 状态」绝不值得让一次滚轮操作崩掉。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = ["SystemPlatform", "ctrl_pressed", "executable", "is_windows"]


def is_windows() -> bool:
    return os.name == "nt"


def ctrl_pressed() -> bool:
    """系统级 Ctrl 状态（Windows）；其它平台或查询失败返回 ``False``。"""
    try:
        import ctypes

        return bool(ctypes.windll.user32.GetAsyncKeyState(0x11) & 0x8000)
    except Exception:
        return False


def executable() -> Path | None:
    """当前进程可执行文件（Windows 走内核 API）；取不到返回 ``None``。"""
    if is_windows():
        try:
            import ctypes

            buffer = ctypes.create_unicode_buffer(32768)
            length = ctypes.windll.kernel32.GetModuleFileNameW(None, buffer, len(buffer))
            if length:
                return Path(buffer.value).resolve()
        except Exception:
            return None
    try:
        return Path(os.path.abspath(sys.executable))
    except Exception:
        return None


class SystemPlatform:
    """实现 ``service.ports.PlatformInfo``。"""

    def is_windows(self) -> bool:
        return is_windows()

    def executable(self) -> Path | None:
        return executable()
