"""视图模型层 —— 界面状态与用户意图，**纯 Python，不认识 UI 框架**。

    ports       ``viewmodel`` 需要的外部能力（后台执行、协程调度）+ 它眼里的 service
    formatting  状态栏 / 数字 / 诊断文本的拼装（唯一的一处文案排版）
    viewer_vm   ``ViewerViewModel``：持有 :class:`ViewerState`，处理全部意图

它向上只暴露三样东西给视图：``state``（冻结值）、``subscribe``（订阅变化）、
以及一组意图方法。视图因此可以写成纯函数 —— 拿到状态就画，画完就等下一次通知。

依赖方向：``viewmodel`` 可以 import ``models`` / ``service`` / ``.ports``，**不得**
import ``infra`` / ``view``，也不得触碰任何控件对象。
"""

from __future__ import annotations

from .formatting import format_service_report, format_size, format_status, format_zoom
from .ports import BackgroundRunner, TaskScheduler, ViewerFacade
from .viewer_vm import ViewerViewModel

__all__ = [
    "BackgroundRunner",
    "TaskScheduler",
    "ViewerFacade",
    "ViewerViewModel",
    "format_service_report",
    "format_size",
    "format_status",
    "format_zoom",
]
