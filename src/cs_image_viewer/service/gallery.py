"""画册 —— 看图的**业务状态机**：哪一批文件、当前第几张、倒了多少、第几帧。

这是整个程序里唯一「有记性」的地方，也是唯一该有记性的地方。它只依赖两个端口
（解码器、回收站）与模型，因此每条规则都能用假的 provider 逐条断言 ——
例如「首尾循环、跳过坏图」不需要任何一张真图片。

几个被刻意写死的业务规则：

* **打开单张不动目录**：先出图，同目录的列表稍后补。首帧延迟因此与「目录里有
  多少文件」「是不是网络盘」无关；
* **补全目录不换当前图**：按文件名重新定位索引，画面不闪、缓存不重建；
* **切换图片复位变换**：新图不该继承上一张的旋转，否则用户会以为图本身歪了；
* **删除走回收站**：先释放句柄再删（Windows 上文件被占用就删不掉）。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from ..models import ImageInfo, Transform, is_image_path
from .ports import DisplayProvider, FileTrash

__all__ = ["Gallery"]


class Gallery:
    """一个窗口的图片列表与浏览位置。所有方法都是同步的（调用方负责挪线程）。"""

    def __init__(self, display: DisplayProvider, trash: FileTrash) -> None:
        self._display = display
        self._trash = trash
        self._files: list[Path] = []
        self._index = 0
        self._transform = Transform()
        self._frame = 0

    # ------------------------------------------------------------------
    # 只读视图（viewmodel 直接读，不需要再问一遍底层）
    # ------------------------------------------------------------------

    @property
    def files(self) -> Sequence[Path]:
        return tuple(self._files)

    @property
    def index(self) -> int:
        return self._index

    @property
    def total(self) -> int:
        return len(self._files)

    @property
    def has_files(self) -> bool:
        return bool(self._files)

    @property
    def current(self) -> Path | None:
        return self._files[self._index] if self._files else None

    @property
    def transform(self) -> Transform:
        return self._transform

    @property
    def frame(self) -> int:
        return self._frame

    # ------------------------------------------------------------------
    # 打开
    # ------------------------------------------------------------------

    def open_file(self, path: str | Path) -> bool:
        """打开单张图片：**只加载这一张**，不碰目录。

        同目录的其余图片由 :meth:`apply_listing` 在首帧之后异步补上：补上之后
        上一张 / 下一张即可用，当前这张不会闪、也不会重新解码。
        """
        target = Path(path)
        if not target.is_file() or not is_image_path(target):
            return False
        self._files = [target]
        self._index = 0
        self._reset_view()
        return True

    def scan_folder(self, folder: str | Path) -> int:
        """扫描目录内所有图片（自然排序），游标回到第一张；返回图片总数。"""
        self._files = list(self._display.list_folder(Path(folder)))
        self._index = 0
        self._reset_view()
        return len(self._files)

    def candidates(self) -> list[Path]:
        """（后台线程）读出当前图片所在目录的图片列表，**不修改任何状态**。

        拆出这一步是为了让慢的 I/O 只发生在后台线程里：扫描结果拿回主线程后
        再决定要不要应用，避免「扫描途中用户已切到别的文件、列表却被旧结果覆盖」。
        """
        current = self.current
        if current is None:
            return []
        return self._display.list_folder(current.parent)

    def apply_listing(self, listing: Sequence[Path]) -> bool:
        """（主线程）把扫描结果应用到「单文件列表」上；返回是否真的扩展了。

        - 已经在浏览目录（列表 > 1 张）→ 什么都不做；
        - 目录里也只有这一张 → 保持单文件；
        - 成功 → 当前文件仍是同一张图，索引重定位，画面与缓存都不用重建。
        """
        if len(self._files) != 1 or len(listing) <= 1:
            return False
        current = self._files[self._index]
        new_index = _locate(current, listing)
        if new_index < 0:                 # 目录里居然没有它（权限/竞态）：保持原状
            return False
        self._files = list(listing)
        self._index = new_index
        return True

    # ------------------------------------------------------------------
    # 导航
    # ------------------------------------------------------------------

    def navigate(self, delta: int) -> bool:
        """切换图片（首尾循环），自动跳过坏图；成功返回 ``True``。"""
        if len(self._files) <= 1:
            return False
        for _ in range(len(self._files)):
            self._index = (self._index + delta) % len(self._files)
            self._reset_view()
            if self._decodable(self._index):
                return True
        return False

    def ensure_valid(self) -> bool:
        """确保游标指向可解码的图片（打开时自动跳过首张坏图）。"""
        if not self._files:
            return False
        if not self._decodable(self._index):
            return self.navigate(1)
        return True

    # ------------------------------------------------------------------
    # 显示层变换
    # ------------------------------------------------------------------

    def rotate(self, turns: int) -> None:
        """顺时针旋转 ``turns`` 个 90°（``-1`` 逆时针）。"""
        self._transform = self._transform.rotated(turns)

    def flip(self, *, horizontal: bool) -> None:
        self._transform = self._transform.flipped(horizontal=horizontal)

    # ------------------------------------------------------------------
    # 动图
    # ------------------------------------------------------------------

    def frames(self) -> int:
        """当前图片的帧数；非动图返回 1，无图返回 0。"""
        current = self.current
        if current is None:
            return 0
        return max(1, self._display.probe(current).frames or 1)

    def set_frame(self, frame: int) -> bool:
        """跳到指定帧；非动图复位为 0 并返回 ``False``。"""
        count = self.frames()
        if count <= 1:
            self._frame = 0
            return False
        self._frame = max(0, min(frame, count - 1))
        return True

    def step_frame(self, delta: int) -> bool:
        """动图内循环前进 / 后退一帧。"""
        count = self.frames()
        if count <= 1:
            self._frame = 0
            return False
        self._frame = (self._frame + delta) % count
        return True

    def frame_duration_ms(self) -> int:
        current = self.current
        if current is None:
            return 100
        return self._display.frame_duration_ms(current, self._frame)

    # ------------------------------------------------------------------
    # 元信息 / 删除
    # ------------------------------------------------------------------

    def info(self) -> ImageInfo:
        """当前图片的元信息（状态栏与导出命名的唯一输入）。"""
        current = self.current
        if current is None:
            return ImageInfo()
        probe = self._display.probe(current)
        return ImageInfo(
            name=current.name,
            width=probe.width,
            height=probe.height,
            size_bytes=probe.size_bytes,
            index=self._index + 1,
            total=len(self._files),
            frames=max(1, probe.frames or 1),
            error=probe.error,
        )

    def delete_current(self) -> str:
        """把当前文件移入回收站，返回文件名；游标落到下一张（或最后一张）。"""
        current = self.current
        if current is None:
            raise RuntimeError("没有可删除的图片")
        # 释放句柄：Windows 下文件被本进程占用时 send2trash 会失败
        self._display.close()
        self._trash.delete(current)
        del self._files[self._index]
        self._reset_view()
        if self._files:
            self._index = min(self._index, len(self._files) - 1)
        return current.name

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _reset_view(self) -> None:
        """换图时复位变换与帧号（新图不继承上一张的旋转/翻转）。"""
        self._transform = Transform()
        self._frame = 0

    def _decodable(self, index: int) -> bool:
        return self._display.probe(self._files[index]).ok


def _locate(target: Path, files: Sequence[Path]) -> int:
    """在列表中定位同名文件的下标；找不到返回 -1。

    先按原始文件名比一遍（C 层字符串比较，不产生临时对象），再退回大小写不敏感
    比较 —— Windows 文件名大小写不敏感，改名后仍应认出是同一张图。
    """
    name = target.name
    for i, item in enumerate(files):
        if item.name == name:
            return i
    lowered = name.lower()
    for i, item in enumerate(files):
        if item.name.lower() == lowered:
            return i
    return -1
