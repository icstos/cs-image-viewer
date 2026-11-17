"""看图的用例编排 —— viewmodel 唯一认识的那张「业务面孔」。

这一层把三件事缝在一起：**画册**（哪一批图、第几张、倒了几下）、**解码器**
（把当前那张变成一帧）、**周边能力**（回收站、文件关联、另开窗口、诊断）。
它自己不含状态机，也不产生任何界面文案：方法要么返回模型，要么抛异常。

这样分层以后，viewmodel 想「翻到下一张」只需要 ``await runner.run(service.navigate)``
—— 阻塞的活全在后台线程，主线程只处理结果。业务规则因此与线程模型、与 UI
框架都无关，可以被脚本直接驱动。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from ..models import DisplayImage, ImageInfo, ServiceReport, image_suffixes
from .diagnostics import parse_report
from .gallery import Gallery
from .ports import (
    AssociationStore,
    DisplayProvider,
    FileTrash,
    PlatformInfo,
    ProcessLauncher,
)

__all__ = ["ViewerService"]


class ViewerService:
    """一个窗口的全部业务用例。所有方法都同步；调用方决定在哪个线程跑。"""

    def __init__(
        self,
        display: DisplayProvider,
        trash: FileTrash,
        association: AssociationStore,
        platform: PlatformInfo,
        launcher: ProcessLauncher,
    ) -> None:
        self._display = display
        self._trash = trash
        self._association = association
        self._platform = platform
        self._launcher = launcher
        self._gallery = Gallery(display, trash)

    # ------------------------------------------------------------------
    # 打开
    # ------------------------------------------------------------------

    def open_file(self, path: str | Path) -> bool:
        """打开单张图片（不同步扫描目录）。"""
        return self._gallery.open_file(path)

    def open_folder(self, folder: str | Path) -> int:
        """打开整个文件夹，返回图片数量（0 表示这里没有可显示的东西）。"""
        return self._gallery.scan_folder(folder)

    def candidates(self) -> list[Path]:
        """当前图片所在目录的列表（后台线程用，不改状态）。"""
        return self._gallery.candidates()

    def apply_listing(self, listing: Sequence[Path]) -> bool:
        """把目录扫描结果应用到单文件列表上；返回是否扩展了。"""
        return self._gallery.apply_listing(listing)

    def expand_folder(self) -> bool:
        """一步到位的「扫 + 应用」（脚本与自检用；界面走两段式）。"""
        return self.apply_listing(self.candidates())

    # ------------------------------------------------------------------
    # 导航与渲染
    # ------------------------------------------------------------------

    def navigate(self, delta: int) -> bool:
        """切换图片（首尾循环、跳过坏图）。"""
        return self._gallery.navigate(delta)

    def load_current(self) -> DisplayImage | None:
        """把当前图片渲染成一帧可上屏数据，并预热前后各一张。

        这是「打开 / 翻页」的完整动作：先保证游标指向可解码的图（否则自动跳过），
        再渲染，最后把邻居交给后台预热。返回 ``None`` 表示这一张打不开。
        """
        if not self._gallery.has_files:
            return None
        if not self._gallery.ensure_valid():
            return None
        image = self.render_current()
        if image is not None:
            self.preload()
        return image

    def render_current(self) -> DisplayImage | None:
        """只渲染（不预热）：动图逐帧走这条，避免每帧都排一次预热。"""
        path = self._gallery.current
        if path is None:
            return None
        return self._display.display(path, self._gallery.transform, self._gallery.frame)

    def preload(self) -> None:
        """预热前后各一张，让「按下方向键」变成「命中缓存」。"""
        files = self._gallery.files
        if len(files) <= 1:
            return
        index = self._gallery.index
        neighbours = (
            files[(index - 1) % len(files)],
            files[(index + 1) % len(files)],
        )
        self._display.preload(neighbours, self._gallery.transform)

    # ------------------------------------------------------------------
    # 变换 / 帧
    # ------------------------------------------------------------------

    def rotate(self, turns: int) -> None:
        """顺时针旋转（``-1`` 逆时针）。"""
        self._gallery.rotate(turns)

    def flip(self, *, horizontal: bool) -> None:
        self._gallery.flip(horizontal=horizontal)

    def frames(self) -> int:
        return self._gallery.frames()

    def frame_duration_ms(self) -> int:
        return self._gallery.frame_duration_ms()

    def step_frame(self, delta: int) -> bool:
        return self._gallery.step_frame(delta)

    # ------------------------------------------------------------------
    # 元信息 / 导出 / 删除
    # ------------------------------------------------------------------

    def info(self) -> ImageInfo:
        return self._gallery.info()

    @property
    def total(self) -> int:
        return self._gallery.total

    @property
    def frame(self) -> int:
        """当前动图帧号（0 基）；非动图恒为 0。"""
        return self._gallery.frame

    @property
    def has_files(self) -> bool:
        return self._gallery.has_files

    @property
    def error_text(self) -> str:
        """最近一次解码失败的原因（端口未强制，按可选能力读取）。"""
        reader = getattr(self._display, "error_text", None)
        return str(reader()) if callable(reader) else ""

    def supported_extensions(self) -> tuple[str, ...]:
        """文件对话框的过滤器（不带点）。"""
        return tuple(image_suffixes())

    def export_frame(self, target: Path) -> None:
        """把当前帧导出为 PNG（失败抛异常，由调用方翻译成提示）。"""
        path = self._gallery.current
        if path is None:
            raise RuntimeError("没有可导出的图片")
        self._display.export(path, target, self._gallery.transform, self._gallery.frame)

    def delete_current(self) -> str:
        """移入回收站并返回文件名。"""
        return self._gallery.delete_current()

    # ------------------------------------------------------------------
    # 文件关联
    # ------------------------------------------------------------------

    def association_available(self) -> bool:
        return self._platform.is_windows()

    def association_status(self) -> str:
        return self._association.status_text()

    def install_association(self) -> None:
        """把当前 exe 注册进 Windows 的「打开方式」。

        开发态（``python main.py``）拿到的不是 exe，注册上去只会得到一条打不开
        的关联，因此这里明确拒绝，并把路径写进错误信息方便判断。
        """
        executable = self._platform.executable()
        if executable is None or executable.suffix.lower() != ".exe" or not executable.is_file():
            raise RuntimeError(f"当前程序不是有效的 exe：{executable}")
        self._association.install(executable)

    def uninstall_association(self) -> None:
        self._association.uninstall()

    # ------------------------------------------------------------------
    # 诊断 / 多窗口
    # ------------------------------------------------------------------

    def service_report(self) -> ServiceReport:
        """共享缓存体检：拿不到共享能力时返回一条「本地模式」说明。"""
        return parse_report(self._display.stats())

    def open_in_new_windows(self, paths: Sequence[Path]) -> int:
        """每个路径起一个新窗口进程，返回真正拉起来的个数。"""
        return self._launcher.launch(paths)

    def close(self) -> None:
        """释放解码器持有的句柄与缓存。"""
        self._display.close()
