"""把 viewmodel 的协程挂到页面的事件循环上。

``viewmodel`` 定义了「怎么排一个协程」的协议（``TaskScheduler``），但不认识
Flet。视图层用它自己的方式实现：``page.run_task``。这样做有两个好处：

* 任务的取消与页面生命周期天然对齐 —— 窗口关掉，页面循环结束，任务自然结束；
* 异常走 Flet 的任务通道，而不是散落在裸 ``asyncio.create_task`` 里静默丢失。
"""

from __future__ import annotations

from collections.abc import Coroutine
from typing import Any

import ui

__all__ = ["PageTaskScheduler"]


class PageTaskScheduler:
    """实现 ``viewmodel.ports.TaskScheduler``：协程交给当前页面。"""

    def __init__(self, page: ui.Page) -> None:
        self._page = page

    def spawn(self, coro: Coroutine[Any, Any, Any]) -> Any:
        """排上页面事件循环，返回可取消的句柄。"""

        async def _run() -> Any:
            return await coro

        return self._page.run_task(_run)

    def cancel(self, handle: Any) -> None:
        """取消一个还没结束的任务；已结束或从未排上队时什么都不做。"""
        cancel = getattr(handle, "cancel", None)
        if callable(cancel):
            try:
                cancel()
            except Exception:
                pass
