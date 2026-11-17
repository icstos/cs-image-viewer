"""菜单数据 —— **纯数据、零控件、零回调分发**。

菜单是从 :class:`ViewerState` 派生的：播放 ↔ 暂停的文案、当前间隔的勾选、有没有
图片决定禁用项，全都由状态决定。于是「界面 = f(状态)」这条原则在菜单上也成立 ——
状态一变，菜单数据重建，不需要去改已挂载的冻结控件树。

菜单里只有稳定的 :class:`Action` 键，事件分发集中在 viewmodel 的一张动作表里。
Agent 想知道「按 Esc 会发生什么」，查动作表就够，不必顺着闭包追五层。

文案密度：每项只留一个动词短语；快捷键交给键帽显示，不在文案里重复一遍。
"""

from __future__ import annotations

import ui

from ..models import (
    KEYBOARD_ONLY,
    SLIDESHOW_INTERVALS,
    Action,
    ViewerState,
    interval_action,
)
from . import design as D

__all__ = ["build_sections", "shortcut_table", "validate_actions"]


def _item(
    action: Action,
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
        key=action.value,
        label=label,
        icon=icon,
        shortcut=shortcut,
        checked=checked,
        disabled=disabled,
        separator_before=divider,
        data=data,
    )


def _submenu(
    key: str,
    label: str,
    *items: ui.MenuAction,
    icon: ui.IconData | None = None,
) -> ui.MenuAction:
    """分组入口：自身不是叶子，点了不触发动作，只展开子菜单。"""
    return ui.MenuAction(key=key, label=label, icon=icon, children=items)


def build_sections(state: ViewerState) -> tuple[ui.MenuSection, ...]:
    """按当前状态生成四栏菜单（纯函数，同参同结果）。"""
    has_image = state.has_image
    frame_off = not state.has_frames

    file_menu = ui.MenuSection("文件", (
        _item(Action.OPEN_FILE, "打开图片", icon=ui.Icons.IMAGE_OUTLINED, shortcut="Ctrl+O"),
        _item(Action.OPEN_FOLDER, "打开文件夹", icon=ui.Icons.FOLDER_OPEN,
              shortcut="Ctrl+Shift+O"),
        _item(Action.OPEN_NEW_WINDOW, "在新窗口打开", icon=ui.Icons.OPEN_IN_NEW,
              shortcut="Ctrl+N"),
        _submenu(
            "assoc", "文件关联",
            _item(Action.ASSOC_INSTALL, "注册为图片打开方式", icon=ui.Icons.LINK),
            _item(Action.ASSOC_UNINSTALL, "取消文件关联", icon=ui.Icons.LINK_OFF),
            _item(Action.ASSOC_STATUS, "查看关联状态", icon=ui.Icons.INFO_OUTLINE),
        ),
        _item(Action.SLIDESHOW, "停止播放" if state.slideshow_on else "幻灯片播放",
              icon=ui.Icons.SLIDESHOW, shortcut="S", divider=True),
        _item(Action.PAUSE, "继续" if state.paused else "暂停",
              icon=ui.Icons.PAUSE_CIRCLE, shortcut="P", disabled=not state.slideshow_on),
        _submenu(
            "interval", "播放间隔",
            *(
                _item(interval_action(seconds), f"{seconds} 秒",
                      checked=state.interval == seconds, data=seconds)
                for seconds in SLIDESHOW_INTERVALS
            ),
            icon=ui.Icons.TIMER_OUTLINED,
        ),
        _item(Action.EXPORT_FRAME, "导出当前帧…", icon=ui.Icons.SAVE_ALT,
              shortcut="Ctrl+E", divider=True, disabled=not has_image),
        _item(Action.EXIT, "退出", icon=ui.Icons.CLOSE, shortcut="Esc"),
    ))

    anim_label = (
        "继续动画" if state.anim_playing and state.anim_paused
        else ("暂停动画" if state.anim_playing else "播放动画")
    )
    play_menu = ui.MenuSection("播放", (
        _item(Action.ANIM_TOGGLE, anim_label,
              icon=ui.Icons.PAUSE_CIRCLE if state.anim_playing else ui.Icons.PLAY_ARROW,
              shortcut="A", disabled=frame_off),
        _item(Action.FRAME_PREV, "上一帧", icon=ui.Icons.SKIP_PREVIOUS,
              shortcut="[", disabled=frame_off),
        _item(Action.FRAME_NEXT, "下一帧", icon=ui.Icons.SKIP_NEXT,
              shortcut="]", disabled=frame_off),
    ))

    edit_menu = ui.MenuSection("编辑", (
        _item(Action.ROTATE_CW, "顺时针旋转 90°", icon=ui.Icons.ROTATE_90_DEGREES_CW,
              shortcut="R"),
        _item(Action.ROTATE_CCW, "逆时针旋转 90°", icon=ui.Icons.ROTATE_90_DEGREES_CCW,
              shortcut="L"),
        _item(Action.FLIP_H, "水平翻转", icon=ui.Icons.FLIP, shortcut="H"),
        _item(Action.FLIP_V, "垂直翻转", icon=ui.Icons.FLIP, shortcut="V"),
        _item(Action.DELETE, "删除当前图片", icon=ui.Icons.DELETE_OUTLINE,
              shortcut="Del", divider=True, disabled=not has_image),
    ))

    view_menu = ui.MenuSection("视图", (
        _item(Action.FIT_WINDOW, "适应窗口", icon=ui.Icons.FIT_SCREEN, shortcut="Ctrl+0"),
        _item(Action.FIT_WIDTH, "适应宽度", icon=ui.Icons.WIDTH_WIDE),
        _item(Action.ACTUAL_SIZE, "1:1 原始大小", icon=ui.Icons.PHOTO_SIZE_SELECT_ACTUAL,
              shortcut="Ctrl+1"),
        _item(Action.ZOOM_IN, "放大", icon=ui.Icons.ZOOM_IN, shortcut="+"),
        _item(Action.ZOOM_OUT, "缩小", icon=ui.Icons.ZOOM_OUT, shortcut="-"),
        _item(Action.FULLSCREEN, "全屏", icon=ui.Icons.FULLSCREEN, shortcut="F", divider=True),
        _item(Action.SERVICE_STATUS, "共享缓存状态…", icon=ui.Icons.MEMORY),
    ))

    return file_menu, play_menu, edit_menu, view_menu


def shortcut_table(state: ViewerState | None = None) -> dict[str, str]:
    """``{动作键: 快捷键}`` 快照：帮助页 / 冲突检查 / Agent 检索读这一份。"""
    return ui.shortcut_map(build_sections(state or ViewerState()))


def validate_actions(dispatch: dict[Action, object]) -> tuple[str, ...]:
    """校验「菜单叶子键 ↔ viewmodel 动作表」是否一一覆盖，返回问题描述。

    两份数据最容易出的错是「新加了一个菜单项，点了没反应」。这条检查把静默失效
    变成可见的断言，跑在自检里而不是运行时 —— 每帧重绘不该为「检查」付代价。
    """
    sections = build_sections(ViewerState())
    problems = list(ui.validate_sections(sections))
    leaf_keys = tuple(item.key for item in ui.iter_actions(sections) if item.is_leaf)
    declared = {action.value for action in Action} - {item.value for item in KEYBOARD_ONLY}
    problems += [f"菜单缺 key：{key}" for key in sorted(declared - set(leaf_keys))]
    problems += [f"菜单多出 key：{key}" for key in sorted(set(leaf_keys) - declared)]
    handled = {action.value for action in dispatch}
    # 动作表允许比菜单多：next / prev 只由键盘触发，没有菜单入口。
    problems += [f"动作表缺 key：{key}" for key in sorted(set(leaf_keys) - handled)]
    return tuple(problems)


#: 菜单图标的尺寸来自设计层，保证与全应用同源（可查、可复算）。
MENU_ICON_SIZE = D.ICON_MENU
