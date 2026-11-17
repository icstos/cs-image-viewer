"""按键映射 —— 严格对齐 IrfanView 的经典布局，**纯函数、只认字符串**。

只接受 ``(key, ctrl, shift)`` 三个原始值，不接触任何事件对象，因此可以在没有窗口、
没有框架的环境里逐条断言「按 ← 是下一张」这类规则。

两点历史包袱写在实现里：

* 不同平台 / 输入法对按键的标签不一样（``PageDown`` / ``Page Down`` / ``PgDn``），
  表里把它们一并收下；
* 裸数字键不触发任何动作 —— 它们只作为 ``Ctrl+0`` / ``Ctrl+1`` 的组合出现，
  否则用户误按一下数字就会跳视图。
"""

from __future__ import annotations

from ..models import Action

__all__ = ["CTRL_BINDINGS", "KEY_BINDINGS", "match_key"]

#: 单键动作表：按键标签（小写、去空白）→ 动作。
KEY_BINDINGS: dict[str, Action] = {
    # 下一张：空格 / PageDown / Up / Left
    " ": Action.NEXT, "space": Action.NEXT, "pagedown": Action.NEXT, "page down": Action.NEXT,
    "pgdn": Action.NEXT, "arrowup": Action.NEXT, "arrow up": Action.NEXT, "up": Action.NEXT,
    "↑": Action.NEXT,
    "arrowleft": Action.NEXT, "arrow left": Action.NEXT, "left": Action.NEXT, "←": Action.NEXT,
    # 上一张：Backspace / PageUp / Down / Right
    "backspace": Action.PREV, "pageup": Action.PREV, "page up": Action.PREV,
    "pgup": Action.PREV, "arrowdown": Action.PREV, "arrow down": Action.PREV,
    "down": Action.PREV, "↓": Action.PREV,
    "arrowright": Action.PREV, "arrow right": Action.PREV, "right": Action.PREV,
    "→": Action.PREV,
    # 缩放：+ / = 放大，- / _ 缩小（含小键盘）
    "+": Action.ZOOM_IN, "=": Action.ZOOM_IN, "add": Action.ZOOM_IN,
    "numpadadd": Action.ZOOM_IN,
    "-": Action.ZOOM_OUT, "_": Action.ZOOM_OUT, "subtract": Action.ZOOM_OUT,
    "numpadsubtract": Action.ZOOM_OUT,
    # 全屏
    "f": Action.FULLSCREEN,
    # 变换：R 顺时针，L 逆时针，H 水平翻转，V 垂直翻转
    "r": Action.ROTATE_CW, "l": Action.ROTATE_CCW,
    "h": Action.FLIP_H, "v": Action.FLIP_V,
    # 播放：S 幻灯片开关，P 暂停 / 继续
    "s": Action.SLIDESHOW, "p": Action.PAUSE,
    # 动图：A 播放 / 暂停，[ 上一帧，] 下一帧
    "a": Action.ANIM_TOGGLE, "[": Action.FRAME_PREV, "]": Action.FRAME_NEXT,
    # 删除 / 退出
    "delete": Action.DELETE, "del": Action.DELETE,
    "escape": Action.EXIT, "esc": Action.EXIT,
}

#: Ctrl 组合键动作表：(是否按住 Shift, 按键) → 动作。
CTRL_BINDINGS: dict[tuple[bool, str], Action] = {
    (False, "o"): Action.OPEN_FILE,
    (True, "o"): Action.OPEN_FOLDER,
    (False, "n"): Action.OPEN_NEW_WINDOW,      # 在新窗口打开（共享缓存）
    (False, "0"): Action.FIT_WINDOW,           # 适应窗口
    (False, "1"): Action.ACTUAL_SIZE,          # 1:1 原始大小
    (False, "e"): Action.EXPORT_FRAME,         # 导出当前帧
}


def match_key(key: str, *, ctrl: bool = False, shift: bool = False) -> Action | None:
    """把一次按键翻译成动作；不认识的键返回 ``None``。

    ``ctrl`` / ``shift`` 由调用方显式给出：Flet 1.1.x 的事件标志在失焦或输入法
    介入时会漏报，视图层用「自己跟踪的按键状态 + 系统查询」合成这两个布尔值，
    比直接信任事件字段可靠。
    """
    label = key.strip().lower() or " "
    if ctrl:
        return CTRL_BINDINGS.get((shift, label))
    if label in ("0", "1"):        # 裸数字键不触发
        return None
    return KEY_BINDINGS.get(label)
