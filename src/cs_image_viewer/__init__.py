"""ImageViewer —— 分层架构的看图软件。

五层，单向依赖：

    models     数据结构、状态枚举、常量、值语义（谁都可以依赖，自己不依赖谁）
    infra      技术实现：PIL、共享缓存、注册表、子进程、窗口外观
    service    业务：画册（文件/游标/变换/帧）与用例编排，只认端口协议
    viewmodel  界面状态与用户意图，纯 Python，不认识 UI 框架
    view       声明式渲染与事件分发，只通过 viewmodel 与外界交互

依赖方向：``view`` → ``viewmodel`` → ``service`` → ``infra``，四层都可以依赖
``models``；反向依赖与跨层调用均被自检（``tools/layered_smoke.py``）禁止。

本包不在 ``__init__`` 里 import 任何子层：拿到 ``cs_image_viewer.models`` 不应该
顺带把 flet、PIL、socket 全拖进来 —— 那会让「不开窗口跑几何断言」变慢。
"""

from __future__ import annotations

__version__ = "0.2.0"
