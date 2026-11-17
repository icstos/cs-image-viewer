"""共享缓存服务子包 —— 一个常驻主进程 + 多个窗口进程共享解码结果。

模块分工（自下而上）：

    runtime   单实例锁、服务注册表、进程存活探测、计数器
    protocol  线协议（长度前缀 JSON + 令牌语义）
    blobs     两种缓存实体：编码 blob（磁盘）与解码结果（内存）
    service   ``ImageService``：无会话的纯操作，所有窗口共享同一份
    server    loopback TCP 外壳（多线程，一连接一线程）
    client    ``ServiceClient`` / ``DirectBackend`` / ``ServiceHost`` / ``ServiceLink``
    daemon    以大进程形态运行服务（``python -m imageviewer.infra.shared.daemon``）

外部只需要认识两样东西：:class:`ServiceLink`（拿服务）与
:class:`imageviewer.infra.display.shared.SharedDisplay`（用服务）。
"""

from __future__ import annotations

from .client import (
    DirectBackend,
    ServiceClient,
    ServiceHost,
    ServiceLink,
    default_link,
    shutdown_default_link,
)
from .protocol import ServiceError
from .service import ImageService

__all__ = [
    "DirectBackend",
    "ImageService",
    "ServiceClient",
    "ServiceError",
    "ServiceHost",
    "ServiceLink",
    "default_link",
    "shutdown_default_link",
]
