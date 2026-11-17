"""服务层向外要的能力（依赖倒置的**接口端**）。

``service`` 只写业务：谁被打开、往哪边翻、旋转几次、失败以后怎么办。它不认识
PIL、不认识 socket、不认识注册表，也不认识 ``send2trash``。它需要的每一项
技术能力都在这里声明成一个 :class:`typing.Protocol`，由 ``infra`` 提供实现，
由 ``main.py`` 在装配时注入。

这样做的直接收益：

* **可测**：写一个 20 行的假 provider 就能测全部业务规则，不需要磁盘、不需要图；
* **可换**：共享缓存换成别的机制、回收站换成别的实现，service 一行不改；
* **可查**：一个文件的 Protocol 就是「service 到底依赖哪些外部能力」的清单。

.. note::
    协议是**结构化**的：``infra`` 的实现类不需要 import 本模块，也不需要继承
    任何东西。``isinstance(obj, DisplayProvider)``（``runtime_checkable``）可以
    在自检里验证「实现是否还完整」。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..models import DisplayImage, ImageProbe, Transform

__all__ = [
    "AssociationStore",
    "DisplayProvider",
    "FileTrash",
    "PlatformInfo",
    "ProcessLauncher",
]


@runtime_checkable
class DisplayProvider(Protocol):
    """把「一张图」变成「一帧可以上屏的数据」，外加几项相关查询。

    实现方可以是本地解码器，也可以是走共享缓存的跨进程实现；无论如何，
    **它不持有业务状态**（当前第几张、翻页方向都不关它的事），因此每个方法
    都可以独立调用、独立缓存、独立降级。
    """

    def probe(self, path: Path) -> ImageProbe:
        """读图头：能否显示、原始尺寸、帧数、文件大小。"""
        ...

    def display(self, path: Path, transform: Transform, frame: int = 0) -> DisplayImage | None:
        """渲染一帧为可上屏数据；失败返回 ``None``。"""
        ...

    def frame_duration_ms(self, path: Path, frame: int) -> int:
        """该帧的显示时长（毫秒），非动图返回 100 之类的默认值。"""
        ...

    def list_folder(self, folder: Path) -> list[Path]:
        """目录内的图片（自然排序）；不可读时返回空列表。"""
        ...

    def preload(self, paths: Sequence[Path], transform: Transform) -> None:
        """预热若干张（后台进行，失败无所谓）。"""
        ...

    def export(self, path: Path, out: Path, transform: Transform, frame: int) -> None:
        """把某帧按当前变换导出为 PNG。"""
        ...

    def stats(self) -> dict[str, Any] | None:
        """共享缓存的健康数据；没有共享能力时返回 ``None``。"""
        ...

    def error_text(self) -> str:
        """最近一次失败的原因（一句人话）；没有失败时返回空串。"""
        ...

    def close(self) -> None:
        """释放句柄与缓存。"""
        ...


@runtime_checkable
class FileTrash(Protocol):
    """把文件移入回收站。"""

    def delete(self, path: Path) -> None:
        ...


@runtime_checkable
class AssociationStore(Protocol):
    """Windows 文件关联的读写（注册表）。"""

    def status_text(self) -> str:
        """当前关联状态的多行说明。"""
        ...

    def install(self, executable: Path) -> None:
        """把指定 exe 注册为图片的「打开方式」之一。"""
        ...

    def uninstall(self) -> None:
        """撤销注册。"""
        ...


@runtime_checkable
class PlatformInfo(Protocol):
    """平台相关的两个事实。"""

    def is_windows(self) -> bool:
        ...

    def executable(self) -> Path | None:
        """当前可执行文件；打包态是那个 exe，开发态是解释器。"""
        ...


@runtime_checkable
class ProcessLauncher(Protocol):
    """另起窗口进程。"""

    def launch(self, paths: Sequence[Path]) -> int:
        """每个路径起一个窗口，返回真正拉起来的个数。"""
        ...
