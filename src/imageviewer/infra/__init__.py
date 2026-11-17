"""基础设施层 —— 一切「与具体技术打交道」的实现都收在这里。

这一层向上提供 ``service`` 定义的端口的具体实现，向下把操作系统、PIL、子进程、
注册表、socket 这些东西关在门内。它**没有业务流程**：不会决定「失败后应该提示
什么」「要不要跳到下一张」，只负责「做或抛错」。

    paths        用户级目录约定（缓存 / 注册表 / 日志）
    catalog      目录扫描与自然排序
    imaging      PIL 解码 / 变换 / 编码的纯函数
    display/*    解码器：本地进程 / 跨进程共享缓存
    shared/*     常驻共享缓存服务（锁、协议、缓存、服务端、客户端、守护进程）
    procs        拉起窗口进程与主进程
    association  Windows 文件关联（注册表读写）
    trash        回收站
    platform     系统级查询（Ctrl 状态、可执行文件路径）
    windows      窗口外观（暗色标题栏、揭示、看门狗）
    concurrency  线程卸载（把阻塞调用挪出 UI 线程）

依赖方向：本层可以 import ``models``，**不得** import ``service`` / ``viewmodel``
/ ``view``。端口协议住在 ``service.ports``，本层按结构实现即可，不做反向 import。
"""

from __future__ import annotations

__all__: list[str] = []
