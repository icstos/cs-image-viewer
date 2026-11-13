"""画布构件 —— 把 :class:`ui.ImageViewport` 与手势层装配起来。

几何（倍率 / 平移 / 框选 / 滚轮意图）全部在 ``ui.core.geometry`` 里算，这里只做
两件事：把结果交给视口渲染，把原始输入事件转交给 app.py 的动作层。构件本身无
状态、无业务判断，可以单独构造来检查控件树。

手势绑定沿用 Flet 的 ``GestureDetector``：左键拖拽框选、区内单击放大、右键拖拽
平移、滚轮翻页 / 滚动 / 缩放、双击切全屏。``drag_interval=16`` 把拖动事件节流到
约 60Hz，避免每个像素都回 Python 触发一次重绘。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import ui

from .. import design as D

__all__ = ["CanvasHandlers", "build_canvas"]


@dataclass(frozen=True, slots=True)
class CanvasHandlers:
    """一组事件回调；纯数据，测试里可以填假函数验证装配。"""

    on_tap_down: Callable | None = None
    on_tap: Callable | None = None
    on_pan_start: Callable | None = None
    on_pan_update: Callable | None = None
    on_pan_end: Callable | None = None
    on_right_pan_start: Callable | None = None
    on_right_pan_update: Callable | None = None
    on_right_pan_end: Callable | None = None
    on_scroll: Callable | None = None
    on_double_tap: Callable | None = None
    on_prev: Callable | None = None
    on_next: Callable | None = None


def build_canvas(
    *,
    src: str | bytes | None,
    image_size: ui.ViewSize,
    viewport: ui.ViewSize,
    view: ui.ViewTransform,
    selection: ui.ViewRect | None,
    height: float,
    fullscreen: bool,
    handlers: CanvasHandlers,
    show_nav: bool = False,
    placeholder: ui.Control | None = None,
) -> ui.Control:
    """构建图片区（纯函数）。

    Args:
        src: 当前图片 data URI；``None`` 表示还没有图。
        image_size / viewport: 图像尺寸与可视区尺寸（``ui.ViewSize``）。
        view: 当前变换（``ui.ViewTransform``）。
        selection: 框选矩形（视口坐标）；``None`` 不画。
        height: 图片区高度。非全屏时 = 窗口高 - 菜单栏 - 状态栏。
        fullscreen: 全屏时手势区 ``expand=True``，悬浮按钮离边更远。
        handlers: 事件回调集合。
        show_nav: 是否画左右翻页箭头；只有一张图时必须为 ``False``。
        placeholder: 无图占位；``None`` 只留深色底（启动留白，避免闪一下提示）。
    """
    stage = ui.ImageViewport(
        src=src,
        image_size=image_size,
        viewport_size=viewport,
        view=view,
        selection=selection,
        background=D.C_CANVAS,
        placeholder=placeholder,
        show_nav=show_nav,
        on_prev=handlers.on_prev,
        on_next=handlers.on_next,
        fullscreen=fullscreen,
        selection_color=D.C_PRIMARY,
    )
    return ui.GestureDetector(
        height=height,
        expand=fullscreen,
        drag_interval=16,
        on_tap_down=handlers.on_tap_down,
        on_tap=handlers.on_tap,
        on_pan_start=handlers.on_pan_start,
        on_pan_update=handlers.on_pan_update,
        on_pan_end=handlers.on_pan_end,
        on_right_pan_start=handlers.on_right_pan_start,
        on_right_pan_update=handlers.on_right_pan_update,
        on_right_pan_end=handlers.on_right_pan_end,
        on_scroll=handlers.on_scroll,
        on_double_tap=handlers.on_double_tap,
        content=stage,
    )
