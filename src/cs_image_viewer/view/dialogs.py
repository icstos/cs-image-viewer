"""对话框构件 —— 纯构造函数，不持有页面引用、不做业务判断。

弹窗的「显示 / 隐藏」由 app.py 的 ``ui.use_state`` + ``ui.use_dialog`` 决定；这里只
回答「长什么样、按钮上写什么」。所以：

* 构造可以脱离窗口单测（断言标题文案、按钮数量、是否可多选文本）；
* 按钮回调由调用方给，构件不偷偷关闭窗口、不读全局状态；
* 标题一律是**具体动作**（「删除图片」而不是「提示」），按钮文案写具体动词
  （「删除」而不是「确定」）—— 破坏性操作的确认成本不该由用户猜。
"""

from __future__ import annotations

from collections.abc import Callable

import ui

from . import design as D
from .kit import button, text, title

__all__ = ["confirm_dialog", "info_dialog"]


def _body(message: str, *, selectable: bool = False) -> ui.Control:
    return ui.Container(
        padding=ui.Padding.symmetric(horizontal=D.PAD_XL, vertical=D.PAD_MD),
        content=text(
            message,
            size=D.FONT_DIALOG,
            color=D.C_MUTED,
            max_lines=None,
            selectable=selectable,
        ),
    )


def confirm_dialog(
    *,
    heading: str,
    message: str,
    confirm_label: str,
    on_confirm: Callable,
    on_cancel: Callable,
    danger: bool = False,
    modal: bool = False,
) -> ui.AlertDialog:
    """二次确认：一句说明 + 「取消 / 具体动作」。

    删除图片用 ``modal=False`` 是刻意的：看图时弹窗不该把整幅画面锁死，用户仍
    能看清自己要删的是哪一张；Esc 优先关弹窗而不是退出程序（见 app.py）。
    """
    return ui.AlertDialog(
        modal=modal,
        title=ui.Container(
            padding=ui.Padding.only(left=D.PAD_XL, right=D.PAD_XL, top=D.PAD_LG),
            content=title(heading),
        ),
        content=_body(message),
        bgcolor=D.C_PANEL,
        actions=[
            button("取消", on_cancel),
            button(confirm_label, on_confirm, primary=not danger, danger=danger),
        ],
        actions_alignment=ui.MainAxisAlignment.END,
        on_dismiss=on_cancel,
    )


def info_dialog(
    *,
    heading: str,
    message: str,
    on_close: Callable,
    selectable: bool = False,
) -> ui.AlertDialog:
    """只读信息弹窗：关联状态、共享缓存统计这类多行诊断文本。

    ``selectable=True`` 让统计文本可复制 —— 诊断信息要能贴进 issue，而不是只能
    截图；多行文本不设 ``max_lines``，由内容自己决定高度。
    """
    return ui.AlertDialog(
        modal=True,
        title=ui.Container(
            padding=ui.Padding.only(left=D.PAD_XL, right=D.PAD_XL, top=D.PAD_LG),
            content=title(heading),
        ),
        content=_body(message, selectable=selectable),
        bgcolor=D.C_PANEL,
        actions=[button("关闭", on_close)],
        actions_alignment=ui.MainAxisAlignment.END,
        on_dismiss=on_close,
    )
