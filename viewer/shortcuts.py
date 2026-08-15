"""快捷键表与按键匹配：严格对齐 IrfanView 经典按键布局。"""
from __future__ import annotations

# 单键动作表：按键标签（小写、去空白） -> 动作名
# 覆盖不同平台/输入法对按键的标签差异（如 "PageDown" / "Page Down"）
_KEYMAP: dict[str, str] = {
    # 下一张：空格 / PageDown / Up / Left
    " ": "next", "space": "next", "pagedown": "next", "page down": "next",
    "pgdn": "next", "arrowup": "next", "arrow up": "next", "up": "next", "↑": "next",
    "arrowleft": "next", "arrow left": "next", "left": "next", "←": "next",
    # 上一张：Backspace / PageUp / Down / Right
    "backspace": "prev", "pageup": "prev", "page up": "prev", "pgup": "prev",
    "arrowdown": "prev", "arrow down": "prev", "down": "prev", "↓": "prev",
    "arrowright": "prev", "arrow right": "prev", "right": "prev", "→": "prev",
    # 缩放：+ / = 放大，- / _ 缩小（含小键盘）
    "+": "zoom_in", "=": "zoom_in", "add": "zoom_in", "numpadadd": "zoom_in",
    "-": "zoom_out", "_": "zoom_out", "subtract": "zoom_out", "numpadsubtract": "zoom_out",
    # 全屏
    "f": "fullscreen",
    # 变换：R 顺时针 90°，L 逆时针 90°，H 水平翻转，V 垂直翻转
    "r": "rotate_cw", "l": "rotate_ccw",
    "h": "flip_h", "v": "flip_v",
    # 播放：S 开始/停止幻灯片，P 暂停/继续
    "s": "slideshow", "p": "pause",
    # 删除 / 取消
    "delete": "delete", "del": "delete",
    "escape": "esc", "esc": "esc",
}

# Ctrl 组合键动作表：(是否按住 Shift, 按键) -> 动作名
_CTRL_KEYMAP: dict[tuple[bool, str], str] = {
    (False, "o"): "open_file",
    (True, "o"): "open_folder",
    (False, "0"): "fit_window",     # Ctrl+0 适应窗口
    (False, "1"): "actual_size",    # Ctrl+1 1:1 原始大小
}


def match(e) -> str | None:
    """将键盘事件映射为动作名；无法识别的按键返回 None。"""
    key = e.key.strip().lower() or " "   # 空格键标签可能是 " "，strip 后还原
    if e.ctrl:
        return _CTRL_KEYMAP.get((e.shift, key))
    if key in ("0", "1"):           # 裸数字键不触发
        return None
    return _KEYMAP.get(key)
