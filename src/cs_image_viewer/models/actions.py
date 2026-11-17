"""动作词汇表 —— 菜单、快捷键、鼠标手势共用的**唯一动词集合**。

一个动作就是一句「用户想干什么」，与「由谁触发」无关：菜单项点它、键盘按它、
滚轮翻页也产生它。把它们收进一个 ``StrEnum`` 有三个直接好处：

1. 拼错就是 ``AttributeError``，不再有「菜单点了没反应」的静默失效；
2. 菜单数据、键位表、viewmodel 的分发表可以互相对账（见 ``tools/layered_smoke.py``）；
3. Agent 想知道「按 F 会发生什么」，从 ``Action.FULLSCREEN`` 一路查到实现即可。

``StrEnum`` 成员本身就是 ``str``，因此可以直接塞进 ``ui.MenuAction.key`` 与
``dict[str, ...]`` 里，不需要任何转换层。
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["KEYBOARD_ONLY", "SLIDESHOW_INTERVALS", "Action", "interval_action"]


class Action(StrEnum):
    """全部用户意图。值即稳定的字符串键（菜单载荷、日志、断言共用）。"""

    # 导航
    NEXT = "next"
    PREV = "prev"

    # 缩放
    ZOOM_IN = "zoom_in"
    ZOOM_OUT = "zoom_out"
    FIT_WINDOW = "fit_window"
    FIT_WIDTH = "fit_width"
    ACTUAL_SIZE = "actual_size"

    # 窗口
    FULLSCREEN = "fullscreen"
    EXIT = "exit"

    # 显示层变换
    ROTATE_CW = "rotate_cw"
    ROTATE_CCW = "rotate_ccw"
    FLIP_H = "flip_h"
    FLIP_V = "flip_v"

    # 幻灯片 / 动图
    SLIDESHOW = "slideshow"
    PAUSE = "pause"
    ANIM_TOGGLE = "anim_toggle"
    FRAME_PREV = "frame_prev"
    FRAME_NEXT = "frame_next"
    INTERVAL_1 = "interval.1"
    INTERVAL_2 = "interval.2"
    INTERVAL_3 = "interval.3"
    INTERVAL_5 = "interval.5"
    INTERVAL_10 = "interval.10"

    # 文件
    OPEN_FILE = "open_file"
    OPEN_FOLDER = "open_folder"
    OPEN_NEW_WINDOW = "open_new_window"
    EXPORT_FRAME = "export_frame"
    DELETE = "delete"
    ASSOC_INSTALL = "assoc.install"
    ASSOC_UNINSTALL = "assoc.uninstall"
    ASSOC_STATUS = "assoc.status"
    SERVICE_STATUS = "service_status"


#: 只由键盘 / 滚轮触发、**没有菜单入口**的动作。
#: 菜单是「用户可以慢慢找的功能表」，翻页是「手指已经记住的动作」——
#: 把它塞进菜单只会多两行噪声。这张表让「菜单叶子 ↔ 动作表」的对账有据可依。
KEYBOARD_ONLY: frozenset[Action] = frozenset({Action.NEXT, Action.PREV})

#: 幻灯片可选间隔（秒）。菜单、动作键、默认值都从这一份读，不会各写一份。
SLIDESHOW_INTERVALS: tuple[int, ...] = (1, 2, 3, 5, 10)

#: 默认间隔（秒）：3 秒是「看得清、又不至于等」的折中。
DEFAULT_INTERVAL: float = 3.0


def interval_action(seconds: float) -> Action:
    """秒数 → 对应的 :class:`Action`；不在候选表里时抛 ``ValueError``。

    显式抛错而不是返回 ``None``：调用方只应传菜单里出现过的间隔，
    传错了说明数据源本身有问题，早点炸比默默忽略好。
    """
    return Action(f"interval.{int(seconds)}")
