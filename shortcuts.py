"""快捷键处理器：将 Flet KeyboardEvent 映射为语义动作。

严格对齐 IrfanView 经典按键体系。所有核心操作支持纯键盘完成。
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass(slots=True)
class KeyEvent:
    """精简的按键事件描述。"""

    key: str
    ctrl: bool = False
    shift: bool = False
    alt: bool = False
    meta: bool = False


Action = Callable[[], None | Awaitable[None]]
ActionMap = dict[str, Action]


class ShortcutHandler:
    """快捷键调度器：接收按键事件，分派到注册的动作。

    使用方式：
        handler = ShortcutHandler()
        handler.register("next", lambda: ...)
        handler.register("zoom_in", lambda: ...)
        await handler.handle(KeyEvent(key=" ", ctrl=False))
    """

    def __init__(self) -> None:
        self._actions: ActionMap = {}

    def register(self, name: str, callback: Action) -> None:
        self._actions[name] = callback

    def unregister(self, name: str) -> None:
        self._actions.pop(name, None)

    async def handle(self, e: KeyEvent) -> bool:
        """解析按键事件，执行对应动作。返回是否匹配到了动作。

        action 若返回协程则等待其完成。
        """
        action = self._resolve(e)
        if action is None:
            return False
        cb = self._actions.get(action)
        if cb is None:
            return False
        result = cb()
        if inspect.isawaitable(result):
            await result
        return True

    # ---- 按键映射 ----

    @staticmethod
    def _resolve(e: KeyEvent) -> str | None:
        k = (e.key or "").strip()
        kl = k.lower()

        # Ctrl 组合键
        if e.ctrl and not e.alt:
            if kl == "o" and e.shift:
                return "open_folder"
            if kl == "o":
                return "open_file"
            if kl == "0":
                return "fit_window"
            if kl == "1":
                return "actual_size"
            return None

        # 单键快捷键
        match kl:
            case " " | "pagedown" | "arrowright" | "arrowdown" | "next":
                return "next"
            case "backspace" | "pageup" | "arrowleft" | "arrowup" | "prior":
                return "prev"
            case "+" | "=" | "numpadadd":
                return "zoom_in"
            case "-" | "_" | "numpadsubtract":
                return "zoom_out"
            case "f":
                return "toggle_fullscreen"
            case "r":
                return "rotate_cw"
            case "l":
                return "rotate_ccw"
            case "h":
                return "flip_h"
            case "v":
                return "flip_v"
            case "delete" | "del":
                return "delete"
            case "escape" | "esc":
                return "escape"
            case "home":
                return "first"
            case "end":
                return "last"
            case "w":
                return "fit_width"
            case "s":
                return "slideshow_toggle"

        return None