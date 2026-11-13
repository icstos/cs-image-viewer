"""菜单数据 —— **纯数据、零控件、零回调分发**。

把菜单从「一堆闭包 + 一堆 SubmenuButton」改成不可变数据节点后：

* 快捷键表、动作清单、禁用 / 选中状态都能被打印、断言、检索（``shortcut_map``）；
* 状态变化（播放 ↔ 暂停、间隔 3s → 5s）由应用重建数据，不碰已挂载的冻结控件树；
* 菜单里只有稳定的 ``key``，事件分发集中在 ``app.py`` 的一张动作表里 ——
  Agent 想知道「按 Esc 会发生什么」查一张表就够，不必顺着闭包追五层。

文案密度：每项只留一个动词短语；快捷键交给键帽显示，不在文案里重复一遍。
"""

from __future__ import annotations

from dataclasses import dataclass

import ui

from .. import design as D
from ..state import SLIDESHOW_INTERVALS

__all__ = ["ACTIONS", "MenuContext", "build_sections", "shortcut_table"]

#: 全部动作键。app.py 的 ACTIONS 表必须覆盖这里每一个叶子键 ——
#: :func:`validate_dispatch` 让这条契约可执行，而不是靠人记得同步。
ACTIONS: tuple[str, ...] = (
    "open_file", "open_folder", "open_new_window",
    "assoc.install", "assoc.uninstall", "assoc.status",
    "slideshow", "pause", "export_frame", "exit",
    "anim_toggle", "frame_prev", "frame_next",
    "rotate_cw", "rotate_ccw", "flip_h", "flip_v", "delete",
    "fit_window", "fit_width", "actual_size", "zoom_in", "zoom_out",
    "fullscreen", "service_status",
    *(f"interval.{sec}" for sec in SLIDESHOW_INTERVALS),
)


@dataclass(frozen=True, slots=True)
class MenuContext:
    """生成菜单所需的状态快照；与控件树完全解耦，可单独构造来测。"""

    slideshow_on: bool = False
    paused: bool = False
    interval: float = 3.0
    anim_playing: bool = False
    anim_paused: bool = False
    has_frames: bool = False
    has_image: bool = False


def _item(
    key: str,
    label: str,
    *,
    icon: ui.IconData | None = None,
    shortcut: str = "",
    divider: bool = False,
    checked: bool = False,
    disabled: bool = False,
    data: object = None,
) -> ui.MenuAction:
    return ui.MenuAction(
        key=key,
        label=label,
        icon=icon,
        shortcut=shortcut,
        checked=checked,
        disabled=disabled,
        separator_before=divider,
        data=data,
    )


def _submenu(
    key: str, label: str, *items: ui.MenuAction,
    icon: ui.IconData | None = None,
) -> ui.MenuAction:
    return ui.MenuAction(key=key, label=label, icon=icon, children=items)


def build_sections(ctx: MenuContext) -> tuple[ui.MenuSection, ...]:
    """按当前状态生成四栏菜单（纯函数，同参同结果）。"""
    frame_off = not ctx.has_frames
    file_menu = ui.MenuSection("文件", (
        _item("open_file", "打开图片", icon=ui.Icons.IMAGE_OUTLINED,
              shortcut="Ctrl+O"),
        _item("open_folder", "打开文件夹", icon=ui.Icons.FOLDER_OPEN,
              shortcut="Ctrl+Shift+O"),
        _item("open_new_window", "在新窗口打开", icon=ui.Icons.OPEN_IN_NEW,
              shortcut="Ctrl+N"),
        _submenu("assoc", "文件关联",
                 _item("assoc.install", "注册为图片打开方式", icon=ui.Icons.LINK),
                 _item("assoc.uninstall", "取消文件关联", icon=ui.Icons.LINK_OFF),
                 _item("assoc.status", "查看关联状态", icon=ui.Icons.INFO_OUTLINE)),
        _item("slideshow", "停止播放" if ctx.slideshow_on else "幻灯片播放",
              icon=ui.Icons.SLIDESHOW, shortcut="S", divider=True),
        _item("pause", "继续" if ctx.paused else "暂停",
              icon=ui.Icons.PAUSE_CIRCLE, shortcut="P",
              disabled=not ctx.slideshow_on),
        _submenu("interval", "播放间隔",
                 *(
                     _item(f"interval.{sec}", f"{sec} 秒",
                           checked=ctx.interval == sec, data=sec)
                     for sec in SLIDESHOW_INTERVALS
                 ),
                 icon=ui.Icons.TIMER_OUTLINED),
        _item("export_frame", "导出当前帧…", icon=ui.Icons.SAVE_ALT,
              shortcut="Ctrl+E", divider=True, disabled=not ctx.has_image),
        _item("exit", "退出", icon=ui.Icons.CLOSE, shortcut="Esc"),
    ))

    anim_label = (
        "继续动画" if ctx.anim_playing and ctx.anim_paused
        else ("暂停动画" if ctx.anim_playing else "播放动画")
    )
    play_menu = ui.MenuSection("播放", (
        _item("anim_toggle", anim_label,
              icon=ui.Icons.PAUSE_CIRCLE if ctx.anim_playing else ui.Icons.PLAY_ARROW,
              shortcut="A", disabled=frame_off),
        _item("frame_prev", "上一帧", icon=ui.Icons.SKIP_PREVIOUS,
              shortcut="[", disabled=frame_off),
        _item("frame_next", "下一帧", icon=ui.Icons.SKIP_NEXT,
              shortcut="]", disabled=frame_off),
    ))

    edit_menu = ui.MenuSection("编辑", (
        _item("rotate_cw", "顺时针旋转 90°", icon=ui.Icons.ROTATE_90_DEGREES_CW,
              shortcut="R", disabled=not ctx.has_image),
        _item("rotate_ccw", "逆时针旋转 90°", icon=ui.Icons.ROTATE_90_DEGREES_CCW,
              shortcut="L", disabled=not ctx.has_image),
        _item("flip_h", "水平翻转", icon=ui.Icons.FLIP, shortcut="H",
              disabled=not ctx.has_image),
        _item("flip_v", "垂直翻转", icon=ui.Icons.FLIP, shortcut="V",
              disabled=not ctx.has_image),
        _item("delete", "删除当前图片", icon=ui.Icons.DELETE_OUTLINE,
              shortcut="Del", divider=True, disabled=not ctx.has_image),
    ))

    view_menu = ui.MenuSection("视图", (
        _item("fit_window", "适应窗口", icon=ui.Icons.FIT_SCREEN,
              shortcut="Ctrl+0"),
        _item("fit_width", "适应宽度", icon=ui.Icons.WIDTH_WIDE),
        _item("actual_size", "1:1 原始大小", icon=ui.Icons.PHOTO_SIZE_SELECT_ACTUAL,
              shortcut="Ctrl+1"),
        _item("zoom_in", "放大", icon=ui.Icons.ZOOM_IN, shortcut="+"),
        _item("zoom_out", "缩小", icon=ui.Icons.ZOOM_OUT, shortcut="-"),
        _item("fullscreen", "全屏", icon=ui.Icons.FULLSCREEN, shortcut="F",
              divider=True),
        _item("service_status", "共享缓存状态…", icon=ui.Icons.MEMORY),
    ))

    return file_menu, play_menu, edit_menu, view_menu


def shortcut_table(ctx: MenuContext | None = None) -> dict[str, str]:
    """``{动作键: 快捷键}`` 快照：帮助页 / 冲突检查 / Agent 检索读这一份。"""
    return ui.shortcut_map(build_sections(ctx or MenuContext()))


def validate_dispatch(dispatch: dict) -> tuple[str, ...]:
    """校验动作表覆盖全部叶子键，返回问题描述（空元组 = 没问题）。

    菜单与动作表是两份数据，最容易出的错是「新加了一个菜单项，点了没反应」。
    启动时跑一次，把这类静默失效变成可见的断言。
    """
    sections = build_sections(MenuContext())
    problems = list(ui.validate_sections(sections))
    leaf_keys = {item.key for item in ui.iter_actions(sections) if item.is_leaf}
    declared = set(ACTIONS)
    problems += [f"菜单缺 key：{key}" for key in sorted(declared - leaf_keys)]
    problems += [f"菜单多出 key：{key}" for key in sorted(leaf_keys - declared)]
    # 动作表允许比菜单多：next / prev 这类只由键盘触发，没有菜单入口。
    problems += [f"动作表缺 key：{key}" for key in sorted(leaf_keys - set(dispatch))]
    return tuple(problems)


# 让 design 的图标刻度参与进来，保证菜单图标尺寸与全应用同源（可查、可复算）。
MENU_ICON_SIZE = D.ICON_MENU
