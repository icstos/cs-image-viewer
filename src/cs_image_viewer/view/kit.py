"""UI 词汇层 —— 把 cs-ui 组件装配成 ImageViewer 说话的方式。

三条约定（与 ``cs-video-player`` 的 kit 同构，跨项目读代码不用重新学）：

1. **控件一律走 ``ui`` 门面**，本模块不出现底层框架包的导入；
2. **构件是纯函数**：值从参数进、意图从回调出，不持有状态、不读全局可变状态；
3. **文案密度克制**：一个控件只承担一个意思，能用键帽说清的不再加一行字。

颜色、字号、间距、圆角全部来自 :mod:`cs_image_viewer.view.design`，因此换配色、调密度只动一处。
"""

from __future__ import annotations

from collections.abc import Callable

import ui

from ..models import Tone
from . import design as D

__all__ = [
    "button",
    "caption",
    "confirm_button",
    "empty_stage",
    "muted",
    "panel",
    "text",
    "title",
    "toast",
]


# ─── 文本 ────────────────────────────────────────────────────────────────
def text(
    value: str,
    *,
    size: int = D.FONT_STATUS,
    color: str = D.C_TEXT,
    weight: ui.FontWeight | None = None,
    align: ui.TextAlign | None = None,
    max_lines: int | None = 1,
    expand: bool | int | None = None,
    selectable: bool = False,
) -> ui.Control:
    """主文本：默认单行 + 省略号，长文件名不会把布局撑破。"""
    kwargs: dict = {}
    if max_lines is not None:
        kwargs.update(max_lines=max_lines, overflow=ui.TextOverflow.ELLIPSIS)
    if align is not None:
        kwargs["text_align"] = align
    if expand is not None:
        kwargs["expand"] = expand
    if selectable:
        kwargs["selectable"] = True
    return ui.Text(
        value,
        size=size,
        color=color,
        weight=weight or ui.FontWeight.W_400,
        **kwargs,
    )


def muted(value: str, **kwargs) -> ui.Control:
    """次级信息：规格、耗时、说明文字。"""
    return text(value, color=D.C_MUTED, **kwargs)


def caption(value: str, **kwargs) -> ui.Control:
    """空态 / 提示区的辅助说明，比正文矮一档。"""
    return text(value, size=D.FONT_HINT, color=D.C_MUTED, **kwargs)


def title(value: str, **kwargs) -> ui.Control:
    """对话框 / 分区标题。"""
    return text(
        value,
        size=D.FONT_TITLE,
        weight=ui.FontWeight.W_600,
        **kwargs,
    )


# ─── 容器与按钮 ──────────────────────────────────────────────────────────
def panel(
    content: ui.Control,
    *,
    height: float | None = None,
    expand: bool | int | None = None,
    bgcolor: str = D.C_PANEL,
    border: ui.Border | None = None,
    padding: ui.Padding | float | None = None,
) -> ui.Control:
    """统一面板外壳：底色 + 可选边框 / 高度 / 内边距。"""
    kwargs: dict = {}
    if height is not None:
        kwargs["height"] = height
    if expand is not None:
        kwargs["expand"] = expand
    if border is not None:
        kwargs["border"] = border
    if padding is not None:
        kwargs["padding"] = padding
    return ui.Container(content=content, bgcolor=bgcolor, **kwargs)


def button(
    label: str,
    on_click: Callable,
    *,
    primary: bool = False,
    danger: bool = False,
) -> ui.Control:
    """对话框操作按钮：文案写具体动作，破坏性操作用危险色。"""
    style = (
        ui.StyleType.ERROR
        if danger
        else (ui.StyleType.PRIMARY if primary else ui.StyleType.DEFAULT)
    )
    return ui.Button(
        content=ui.Text(label, size=D.FONT_DIALOG),
        style_type=style,
        on_click=on_click,
    )


def confirm_button(label: str, on_click: Callable) -> ui.Control:
    """次要动作（取消 / 关闭）：文字按钮，不与主操作抢视觉重量。"""
    return button(label, on_click)


# ─── 空态 ────────────────────────────────────────────────────────────────
def empty_stage() -> ui.Control:
    """图片区空态：一句主提示 + 两组键帽，不铺一整屏帮助文字。

    键帽由 :class:`ui.Kbd` 画，用户读到的是「这是一个键」而不是一行灰字；
    「打开图片 / 打开文件夹」两个入口分行语义，密度比原来的一长串更松。
    """
    hint = ui.Row(
        spacing=D.PAD_SM,
        tight=True,
        vertical_alignment=ui.CrossAxisAlignment.CENTER,
        controls=[
            ui.Kbd("Ctrl+O"),
            caption("打开图片"),
            caption("·"),
            ui.Kbd("Ctrl+Shift+O"),
            caption("打开文件夹"),
        ],
    )
    return ui.Column(
        spacing=D.PAD_MD,
        tight=True,
        horizontal_alignment=ui.CrossAxisAlignment.CENTER,
        controls=[
            ui.Icon(ui.Icons.IMAGE_OUTLINED, size=D.ICON_EMPTY, color=D.C_DIM),
            text("还没有打开图片", size=D.FONT_DIALOG, color=D.C_MUTED),
            hint,
        ],
    )


# ─── 反馈 ────────────────────────────────────────────────────────────────
_TONES: dict[Tone, ui.StyleType] = {
    Tone.INFO: ui.StyleType.INFO,
    Tone.SUCCESS: ui.StyleType.SUCCESS,
    Tone.WARNING: ui.StyleType.WARNING,
    Tone.ERROR: ui.StyleType.ERROR,
}


def toast(page: ui.Page, message: str, *, tone: Tone = Tone.INFO, seconds: float = 2.5) -> None:
    """一次性回执：cs-ui 的顶部居中 Message，同款可合并、自动消失。

    浮层宿主不可用时降级到 SnackBar —— 提示可以换位置，但不能丢：打开文件失败、
    导出完成这类回执是用户判断「刚才那下有没有生效」的唯一依据。
    """
    style = _TONES.get(Tone(tone), ui.StyleType.INFO)
    try:
        ui.Message(message, style_type=style, duration=int(seconds * 1000)).show(page)
        return
    except Exception:
        pass
    try:
        page.show_dialog(
            ui.SnackBar(
                content=text(message, max_lines=None),
                bgcolor=D.C_PANEL,
                duration=ui.Duration(seconds=seconds),
            )
        )
    except Exception:
        pass
