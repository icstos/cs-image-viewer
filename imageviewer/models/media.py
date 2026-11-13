"""媒体模型 —— 图像、探测结果、显示层变换。

这层只有**数据形状**：不可变、可比较、可打印。它是三层之间的公共语言：

* ``infra`` 把 PIL 的解码结果翻译成 :class:`ImageProbe` / :class:`DisplayImage`；
* ``service`` 持有 :class:`Transform` 并把 :class:`ImageInfo` 交给上层；
* ``viewmodel`` 把 :class:`ImageInfo` 排成状态栏文案。

没有任何字段依赖控件、路径 IO 或业务状态，因此这些类型可以直接写进日志、
断言、诊断文档，Agent 读到什么就是程序内部真正在用什么。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

__all__ = ["DisplayImage", "ImageInfo", "ImageProbe", "Transform"]


@dataclass(frozen=True, slots=True)
class Transform:
    """显示层几何变换（不写回原文件）。

    ``rotation`` 是顺时针 90° 的整数倍并归一到 ``0..3``；两个翻转互相独立。
    变换本身是纯取值，``rotated`` / ``flipped`` 返回新对象，便于在状态里比较。
    """

    rotation: int = 0
    flip_h: bool = False
    flip_v: bool = False

    @property
    def identity(self) -> bool:
        """是否等价于「原样显示」；是则解码侧可以跳过变换。"""
        return self.rotation == 0 and not self.flip_h and not self.flip_v

    def rotated(self, turns: int) -> Transform:
        """顺时针旋转 ``turns`` 个 90°（``-1`` 即逆时针）。"""
        return replace(self, rotation=(self.rotation + turns) % 4)

    def flipped(self, *, horizontal: bool) -> Transform:
        """水平或垂直翻转（同一个轴翻两次回到原状）。"""
        if horizontal:
            return replace(self, flip_h=not self.flip_h)
        return replace(self, flip_v=not self.flip_v)

    def cleared(self) -> Transform:
        """复位（切换图片时用：新图不该继承上一张的旋转）。"""
        return Transform()


@dataclass(frozen=True, slots=True)
class ImageProbe:
    """图头探测结果：能否显示、原始尺寸、帧数、文件大小。

    ``ok=False`` 时 :attr:`error` 是给用户看的一句人话（不超过 120 字），
    调用方据此跳过坏图而不是让整次导航失败。
    """

    ok: bool = False
    width: int = 0
    height: int = 0
    frames: int = 1
    size_bytes: int = 0
    error: str = ""

    @property
    def animated(self) -> bool:
        return self.ok and self.frames > 1


@dataclass(frozen=True, slots=True)
class DisplayImage:
    """一帧可直接上屏的数据（data URI + 渲染尺寸）。

    ``shared=True`` 表示这一帧直接来自共享缓存，本进程没有解码 —— 状态栏的
    「缓存」标记与诊断文案都读这一个字段，不再各自去问底层。
    """

    src: str
    width: int
    height: int
    shared: bool = False


@dataclass(frozen=True, slots=True)
class ImageInfo:
    """当前图片的元信息（状态栏与导出命名共用的那一份）。

    ``index`` 从 1 开始（给人看的页码）；``total=0`` 表示没有图片，此时状态栏
    整条留白，不写「0 / 0」这种没有信息量的字。
    """

    name: str = ""
    width: int = 0
    height: int = 0
    size_bytes: int = 0
    index: int = 0
    total: int = 0
    frames: int = 0
    error: str = ""

    @property
    def has_image(self) -> bool:
        return self.total > 0 and bool(self.name)
