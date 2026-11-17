"""顶栏与底栏 —— 两件「页面级 chrome」，都走 cs-ui 公共组件。

菜单栏用 :class:`ui.MenuBar`（数据驱动、键帽显示快捷键），状态栏用
:class:`ui.StatusBar` 的三段式（文件名 / 规格 / 位置各占 1/3）。两者都不手写
裸色值、裸字号，配色由 main.py 下发的 ui 主题与 design.py 的色表共同决定。
"""

from __future__ import annotations

from collections.abc import Callable

import ui

from ..models import StatusSnapshot
from . import design as D

__all__ = ["build_menu_bar", "build_status_bar"]


def build_menu_bar(
    sections: tuple[ui.MenuSection, ...],
    on_action: Callable[[ui.MenuAction], None],
) -> ui.Control:
    """顶部菜单栏：48 高、面板底色、一条同族发丝线。"""
    return ui.MenuBar(
        sections=sections,
        on_action=on_action,
        height=D.MENU_H,
        bgcolor=D.C_PANEL,
        divider_color=D.C_LINE,
    )


def build_status_bar(snapshot: StatusSnapshot) -> ui.Control:
    """底部状态栏：三段等分，长文件名只在自己那一段里省略。

    ``show_dot=False``：三段式没有「系统是否正常」要报，一个孤零零的圆点只会
    在中间和右段之间制造噪声。
    """
    return ui.StatusBar(
        summary=snapshot.name,
        center=snapshot.detail,
        status=snapshot.position,
        show_dot=False,
        bar_height=D.STATUS_H,
        bgcolor=D.C_PANEL,
        border=ui.Border(top=ui.BorderSide(1, D.C_LINE)),
    )
