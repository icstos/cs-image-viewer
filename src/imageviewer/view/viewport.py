"""ImageViewport —— 本项目的图片**渲染层**。

与 :mod:`viewer.geometry` 成对：那边算，这边画。组件是**无状态纯函数**（值从
参数进、意图从回调出），因此：

* 视图状态归应用（``use_state`` / ``ViewTransform``），这里不持有、不偷偷改；
* 几何只有一份实现，渲染摆位与命中测试都走 ``image_rect()``，不会出现
  「看到的图」和「算出来的选区」错开几像素；
* 可以脱离窗口单独构造、单独断言控件树。

画布分四层，顺序即 Z 轴（越靠后越在上）：

============ ==========================================================
背景        一块铺满的底色，图片未加载时也不露白
图像        按 ``image_rect()`` 定位，``gapless_playback`` 切图不闪
框选        半透明填充 + 1.5px 主色描边
悬浮层      调用方传入的控件（翻页按钮、缩放读数、水印……）
============ ==========================================================

**本组件不画导航按钮。** 「翻页箭头长什么样」是产品的视觉决策，所以只留一个
``overlay`` 口子：位置由 :func:`viewer.geometry.nav_geometry` 算，皮肤与三态
由调用方决定（本项目的实现见 :mod:`imageviewer.view.glass`）。

.. note::
   这一层原本在 cs-ui 作为公共组件，2026-11-14 随 :mod:`viewer.geometry` 一起
   撤回。四层结构是看图软件的取舍（选区压在图上、箭头压在选区上、切图要免闪），
   通用化只会把别人的产品绑到这套取舍上。
"""

from __future__ import annotations

from collections.abc import Sequence

import ui

from ..models.geometry import (
    ViewRect,
    ViewSize,
    ViewTransform,
    compute_scale,
    image_rect,
)

__all__ = ["EMPTY_STAGE_ICON", "SELECTION_BORDER", "SELECTION_FILL", "ImageViewport"]

#: 框选：主色描边 + 12% 白填充。填充用中性白而不是主色，压在彩色照片上不会串色。
SELECTION_BORDER = 1.5
SELECTION_FILL = ui.Colors.with_opacity(0.12, ui.Colors.WHITE)
EMPTY_STAGE_ICON = ui.Icons.IMAGE_OUTLINED


def ImageViewport(
    *,
    src: str | bytes | None,
    image_size: ViewSize,
    viewport_size: ViewSize,
    view: ViewTransform | None = None,
    selection: ViewRect | None = None,
    background: ui.ColorValue | None = None,
    placeholder: ui.Control | None = None,
    overlay: ui.Control | Sequence[ui.Control] | None = None,
    selection_color: ui.ColorValue = ui.Colors.PRIMARY,
) -> ui.Stack:
    """构建图片视口（**纯函数**，无副作用）。

    Args:
        src: 图片来源（URL / 资源路径 / base64 / 字节）。``None`` 表示还没有图。
        image_size: 解码后像素尺寸；无效尺寸（0×0）按「没有图」处理。
        viewport_size: 可视区尺寸，由应用的 resize 事件提供。
        view: 当前变换；``None`` 等价于适应窗口。
        selection: 框选矩形（视口坐标）；``None`` 不画。
        background: 背景色，默认 ``SURFACE_CONTAINER_LOWEST``（明暗自适应）。
        placeholder: 无图时的占位控件；``None`` 只留背景（启动留白用）。
        overlay: 压在选区之上的悬浮控件，单个或一组。翻页按钮、缩放读数、
            水印都放这里；**只有一张图时不要传**，不要画点了没反应的箭头。
        selection_color: 框选描边色，默认主色。

    Returns:
        一个 Stack，可直接放进 ``GestureDetector.content``。
    """
    transform = view or ViewTransform()
    bg = background if background is not None else ui.Colors.SURFACE_CONTAINER_LOWEST
    layers: list[ui.Control] = [ui.Container(expand=True, bgcolor=bg)]

    def _add_overlay() -> None:
        if overlay is None:
            return
        if isinstance(overlay, ui.Control):
            layers.append(overlay)
        else:
            layers.extend(overlay)

    if src is None or not image_size.valid:
        if placeholder is not None:
            layers.append(
                ui.Container(
                    expand=True,
                    alignment=ui.Alignment.CENTER,
                    content=placeholder,
                )
            )
        _add_overlay()
        return ui.Stack(
            expand=True,
            clip_behavior=ui.ClipBehavior.HARD_EDGE,
            controls=layers,
        )

    scale = compute_scale(transform, image_size, viewport_size)
    rect = image_rect(image_size, viewport_size, transform, scale)
    layers.append(
        ui.Image(
            src=src,
            left=rect.left,
            top=rect.top,
            width=rect.width,
            height=rect.height,
            fit=ui.BoxFit.FILL,
            gapless_playback=True,   # 切图时保留上一帧，消除黑闪
        )
    )

    if selection is not None and selection.width > 0 and selection.height > 0:
        layers.append(
            ui.Container(
                left=selection.left,
                top=selection.top,
                width=selection.width,
                height=selection.height,
                border=ui.Border.all(SELECTION_BORDER, selection_color),
                bgcolor=SELECTION_FILL,
                border_radius=ui.RADIUS["xs"],
            )
        )

    # 放在最后：浮层压在选区之上，框选到边缘时按钮也点得到。
    _add_overlay()

    return ui.Stack(
        expand=True,
        clip_behavior=ui.ClipBehavior.HARD_EDGE,
        controls=layers,
    )
