"""视图层 —— 声明式渲染与事件分发，**没有业务逻辑**。

    design      设计令牌投影（色表 / 尺寸 / 排印的唯一事实源）
    keymap      按键 → 动作（纯函数，只认字符串）
    kit         文本 / 面板 / 按钮 / 空态 / 提示 的词汇层
    glass       压在图片上的玻璃翻页按钮（纯工厂）
    viewport    图片视口四层渲染：背景 / 图像 / 框选 / 浮层
    canvas      画布装配：视口 + 浮层 + 手势绑定
    bars        顶栏菜单与底栏状态
    menu        菜单数据（从状态派生，可导出快捷键表）
    dialogs     对话框构件（纯构造）
    scheduler   把 viewmodel 的协程挂到页面事件循环
    app         根组件：状态 → 控件，事件 → viewmodel

原则：**界面是状态的函数**。视图不缓存业务状态、不做判断、不直接读写文件；
它只把拿到的 :class:`ViewerState` 铺成控件，并把用户动作翻译成 viewmodel 的调用。

依赖方向：本层可以 import ``models`` / ``viewmodel`` / ``.``，**不得** import
``service`` / ``infra``，也不得直接 import 底层框架包（一律经 ``ui`` 门面）。
"""

from __future__ import annotations

from .app import build_app, remember_window_size

__all__ = ["build_app", "remember_window_size"]
