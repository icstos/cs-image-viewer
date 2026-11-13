"""玻璃翻页按钮 —— 本项目专用的媒体浮层构件。

**为什么不在 cs-ui。**
:class:`viewer.ui.viewport.ImageViewport` 只留一个 ``overlay`` 口子，「按钮长什么样」
交给调用方：
看图软件要压在照片上的半透明圆钮，PDF 阅读器可能要紧贴边缘的实心按钮，
画廊可能要带页码的胶囊。把其中一种皮肤写进公共视口，等于替所有使用者做了
视觉决策。这里保留下来的只有**几何** —— 位置仍由 ``G.nav_geometry()`` 算，
那是「按钮该摆在哪」的客观规则，不是审美。

**为什么是函数而不是 ``@ui.control`` 子类。**
子类只要多声明一个字段，``@ft.control`` 生成的 ``__init__`` 就会把子类字段
排到父类字段之前，位置参数被悄悄吃掉，图标赋给了别的字段。构造期不报错，
直到控件挂树才由渲染端抛 ``IconButton must have either icon or a visible
content specified.``。纯工厂没有隐藏的签名顺序：入参即全部可配项，返回的
就是普通的 ``ui.IconButton``，读代码和读调用点看到的是同一件事。

**为什么固定深色、不跟主题。**
浮层压在任意图像上 —— 纯白照片和纯黑夜景都可能出现。跟着主题变成白底或
浅灰底，会在其中一种画面上消失。所以固定「深色玻璃 + 亮图标 + 细亮描边」：
白底靠黑玻璃读出边界，黑底靠白图标与描边读出形状。三态配色交给
``ButtonStyle`` 的 ``ControlState`` 映射，由渲染端切换，鼠标划过不经过
Python，不会引起整页重绘。
"""

from __future__ import annotations

from collections.abc import Callable

import ui

from .. import design as D
from .. import geometry as G

__all__ = ["glass_icon_button", "glass_nav_pair"]


def _style() -> ui.ButtonStyle:
    """玻璃的三态外观。

    悬浮态写成 ``ControlState`` 映射而不是 ``on_hover`` 改属性：已挂载的控件树
    是冻结的，Python 侧改属性既会抛错，也会让「鼠标划过」变成「整页重建」。
    """
    return ui.ButtonStyle(
        shape=ui.CircleBorder(),
        padding=0,
        animation_duration=ui.DURATION["fast"],
        bgcolor={
            ui.ControlState.DEFAULT: D.C_GLASS,
            ui.ControlState.HOVERED: D.C_GLASS_ACTIVE,
            ui.ControlState.DISABLED: D.C_GLASS,
        },
        icon_color={
            ui.ControlState.DEFAULT: D.C_GLASS_ICON,
            ui.ControlState.HOVERED: D.C_GLASS_ICON_ACTIVE,
            ui.ControlState.DISABLED: ui.Colors.with_opacity(0.38, D.C_GLASS_ICON),
        },
        overlay_color=D.C_GLASS_OVERLAY,
        side=ui.BorderSide(1, D.C_GLASS_EDGE),
        elevation={
            ui.ControlState.DEFAULT: 2,
            ui.ControlState.HOVERED: 6,
        },
        shadow_color=D.C_GLASS_SHADOW,
    )


def glass_icon_button(
    *,
    icon: ui.IconData,
    on_click: Callable | None = None,
    diameter: float = D.GLASS_SIZE,
    icon_size: float = D.GLASS_ICON_SIZE,
    tooltip: str | None = None,
    left: float | None = None,
    top: float | None = None,
    disabled: bool = False,
) -> ui.IconButton:
    """一枚「深色玻璃」圆形图标按钮。

    Args:
        icon: 图标（``ui.Icons.*``）。
        on_click: 点击回调。
        diameter: 直径，默认 46（44 是桌面端舒适点击下限，多 2px 不显小）。
        icon_size: 图标边长，默认 28（比常规图标大一档，46 的圆里才不显空）。
        tooltip: 悬停提示。**默认不给** —— 贴着窗口边缘时提示框会被裁掉，
            残缺的提示比没有更难看；箭头形状 + 悬停增亮 + 手型光标已足够表意。
        left / top: 在 ``Stack`` 里的定位；``None`` 表示交给父容器排版。
        disabled: 禁用，三态配色同时退到 38%。
    """
    return ui.IconButton(
        icon=icon,
        on_click=on_click,
        width=diameter,
        height=diameter,
        icon_size=icon_size,
        tooltip=tooltip,
        left=left,
        top=top,
        disabled=disabled,
        mouse_cursor=ui.MouseCursor.CLICK,
        style=_style(),
    )


def glass_nav_pair(
    *,
    viewport: G.ViewSize,
    on_prev: Callable | None = None,
    on_next: Callable | None = None,
    diameter: float = D.GLASS_SIZE,
    margin: float = D.NAV_MARGIN,
    fullscreen: bool = False,
    icon_size: float = D.GLASS_ICON_SIZE,
) -> list[ui.IconButton]:
    """一左一右定位好的翻页按钮，直接传给 ``ImageViewport(overlay=...)``。

    位置来自 :func:`G.nav_geometry`：垂直居中、全屏时离边更远。``on_prev`` /
    ``on_next`` 有一个是 ``None`` 就只返回另一侧 —— 单张图、首尾不循环这类场景
    下，不该画一个点了没反应的箭头。
    """
    left, right, top = G.nav_geometry(
        viewport, size=diameter, margin=margin, fullscreen=fullscreen
    )
    buttons: list[ui.IconButton] = []
    if on_prev is not None:
        buttons.append(
            glass_icon_button(
                icon=ui.Icons.CHEVRON_LEFT,
                on_click=on_prev,
                diameter=diameter,
                icon_size=icon_size,
                left=left,
                top=top,
            )
        )
    if on_next is not None:
        buttons.append(
            glass_icon_button(
                icon=ui.Icons.CHEVRON_RIGHT,
                on_click=on_next,
                diameter=diameter,
                icon_size=icon_size,
                left=right,
                top=top,
            )
        )
    return buttons
