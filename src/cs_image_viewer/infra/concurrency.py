"""线程卸载 —— 把阻塞调用挪出 UI 线程。

看图软件的主线程就是渲染线程：一次 ``PIL.Image.open`` 落在主线程上，窗口就会
卡半秒。因此所有磁盘 / 解码 / 子进程调用都从 :class:`AsyncioRunner` 的线程池
里走，回到主线程时只做「把结果写进状态」这一件轻活。

它实现 ``viewmodel.ports.BackgroundRunner``；与之配套的「协程怎么排队」是另一个
协议，由视图层用 ``page.run_task`` 落地（见 ``view.PageTaskScheduler``）——
这样任务的取消天然跟着页面生命周期走。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TypeVar

__all__ = ["AsyncioRunner"]

T = TypeVar("T")


class AsyncioRunner:
    """在默认线程池里执行阻塞调用（``PIL`` / 磁盘 / 注册表都走它）。"""

    def __init__(self, max_workers: int | None = None) -> None:
        self._max_workers = max_workers

    async def run(self, work: Callable[[], T]) -> T:
        loop = asyncio.get_running_loop()
        if self._max_workers is None:
            return await loop.run_in_executor(None, work)
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            return await loop.run_in_executor(pool, work)
