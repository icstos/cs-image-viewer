"""共享服务子包：主进程（宿主）+ 轻量子进程（窗口）+ 共享缓存。

分层（自下而上）：

    paths     用户级目录约定（缓存/注册表/日志）
    runtime   单实例锁、服务注册表、进程存活探测
    protocol  线协议（长度前缀 JSON + 令牌语义）
    cache     共享缓存的两种实体：编码 blob（磁盘）与解码结果（内存）
    core      `ImageService`：无会话的纯操作，所有窗口共享同一份
    server    loopback TCP 外壳（多线程，一连接一线程）
    client    `ServiceClient` / `DirectBackend` / `ServiceHost` / `ServiceLink`
    spawn     拉起新窗口、拉起独立守护进程

外部只需要认识两样东西：`ServiceLink`（拿服务）与 `shared_manager`（用服务）。
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
from .core import ImageService
from .protocol import ServiceError

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
