"""解码器的两种落地：本地进程解码与跨进程共享缓存。

两者实现同一个端口（``service.ports.DisplayProvider``），因此 ``service`` 层
完全不知道自己拿的是哪一个 —— 换实现只需要改 ``main.py`` 的一行装配。
"""

from __future__ import annotations

from .local import LocalDisplay
from .shared import SharedDisplay

__all__ = ["LocalDisplay", "SharedDisplay"]
