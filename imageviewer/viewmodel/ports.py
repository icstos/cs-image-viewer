"""视图模型层向外要的两样东西（依赖倒置的接口端）。

``viewmodel`` 是纯 Python：它不 import 任何 UI 框架、不碰控件、不读全局可变状态。
但它有两件事自己做不到，必须由外部提供 —— 而这两件事又都**不能被写死**，
否则单测就得起事件循环、起线程池：

* :class:`BackgroundRunner` —— 把阻塞调用挪出 UI 线程（``infra`` 用线程池实现）；
* :class:`TaskScheduler` —— 把协程挂到事件循环上（视图层用 ``page.run_task`` 实现，
  因此任务的取消与页面生命周期天然对齐）。

另外声明 :class:`ViewerFacade`：``viewmodel`` 眼里的 service 长什么样。
它把「我依赖哪些业务用例」写成一份可读的清单，``service.ViewerService`` 按结构
满足它，不需要继承任何东西。
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Sequence
from pathlib import Path
from typing import Any, Protocol, TypeVar, runtime_checkable

from ..models import DisplayImage, ImageInfo, ServiceReport

__all__ = ["BackgroundRunner", "TaskScheduler", "ViewerFacade"]

T = TypeVar("T")


@runtime_checkable
class BackgroundRunner(Protocol):
    """在后台执行同步调用，返回其结果。"""

    async def run(self, work: Callable[[], T]) -> T:
        ...


@runtime_checkable
class TaskScheduler(Protocol):
    """把协程排上事件循环，并允许取消。

    ``spawn`` 的返回值由实现决定（``asyncio.Task``、flet 的 ``TaskFuture`` 都可以），
    只需原样交给 :meth:`cancel`。
    """

    def spawn(self, coro: Coroutine[Any, Any, Any]) -> Any:
        ...

    def cancel(self, handle: Any) -> None:
        ...


@runtime_checkable
class ViewerFacade(Protocol):
    """``viewmodel`` 需要的全部业务用例（由 ``service.ViewerService`` 实现）。"""

    # 打开
    def open_file(self, path: str | Path) -> bool:
        ...

    def open_folder(self, folder: str | Path) -> int:
        ...

    def candidates(self) -> list[Path]:
        ...

    def apply_listing(self, listing: Sequence[Path]) -> bool:
        ...

    # 导航与渲染
    def navigate(self, delta: int) -> bool:
        ...

    def load_current(self) -> DisplayImage | None:
        ...

    def render_current(self) -> DisplayImage | None:
        ...

    def preload(self) -> None:
        ...

    # 变换 / 帧
    def rotate(self, turns: int) -> None:
        ...

    def flip(self, *, horizontal: bool) -> None:
        ...

    def frames(self) -> int:
        ...

    def frame_duration_ms(self) -> int:
        ...

    def step_frame(self, delta: int) -> bool:
        ...

    # 元信息
    def info(self) -> ImageInfo:
        ...

    @property
    def total(self) -> int:
        ...

    @property
    def frame(self) -> int:
        ...

    @property
    def has_files(self) -> bool:
        ...

    @property
    def error_text(self) -> str:
        ...

    def supported_extensions(self) -> tuple[str, ...]:
        ...

    # 导出 / 删除
    def export_frame(self, target: Path) -> None:
        ...

    def delete_current(self) -> str:
        ...

    # 文件关联
    def association_available(self) -> bool:
        ...

    def association_status(self) -> str:
        ...

    def install_association(self) -> None:
        ...

    def uninstall_association(self) -> None:
        ...

    # 诊断 / 多窗口
    def service_report(self) -> ServiceReport:
        ...

    def open_in_new_windows(self, paths: Sequence[Path]) -> int:
        ...

    def close(self) -> None:
        ...
