"""窗口子进程 <-> 常驻服务之间的线协议。

设计取舍（为什么长这样）：

- **只走 loopback TCP**：不引入 pywin32 / 命名管道的平台差异；127.0.0.1 上的
  一次往返在 Windows 上是几十微秒级，比解码一帧便宜三个数量级。
- **长度前缀 + JSON**：消息都是小字典（路径、参数、元信息），不需要 pickle，
  也不会因为某个窗口进程版本不同而反序列化出可执行代码。
- **不传图片本体**：服务把编码结果写到磁盘缓存，消息里只给路径。
  一帧几 MB 的 JPEG 走 socket 会白白复制两遍（进内核再进 Python），
  落盘后由子进程直接读文件，还能蹭到操作系统页缓存 —— 第二次打开几乎零成本。

协议形状：
    请求  {"id": 1, "op": "show", "args": {...}}
    应答  {"id": 1, "ok": true,  "result": {...}}
          {"id": 1, "ok": false, "error": "..."}
握手第一帧固定为 {"id": 0, "op": "hello", "args": {"token": "..."}}。
"""
from __future__ import annotations

import json
import socket
import struct
from typing import Any

PROTOCOL_VERSION = 1

# 单帧上限：协议里只有元信息，8MB 已经非常宽松（防御异常输入）
MAX_FRAME_BYTES = 8 << 20

_HEADER = struct.Struct("!I")


class ServiceError(RuntimeError):
    """服务侧错误或传输失败（调用方据此决定降级到本地解码）。

    `transport=True` 表示「连接层出问题」（断开、超时、协议错位）——
    这种错误值得重连重试；业务错误（文件不存在、解码失败）重试也没用，
    调用方应当直接降级到本地解码。
    """

    def __init__(self, message: str, transport: bool = False):
        super().__init__(message)
        self.transport = transport


def send(sock: socket.socket, obj: dict[str, Any]) -> None:
    """发送一条消息（4 字节大端长度 + UTF-8 JSON）。"""
    body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_FRAME_BYTES:
        raise ServiceError(f"消息过大：{len(body)} 字节", transport=True)
    sock.sendall(_HEADER.pack(len(body)) + body)


def recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            raise ServiceError("连接已关闭", transport=True)
        chunks.extend(chunk)
    return bytes(chunks)


def recv(sock: socket.socket) -> dict[str, Any]:
    """接收一条消息；连接关闭或格式错误时抛 `ServiceError`。"""
    (size,) = _HEADER.unpack(recv_exact(sock, _HEADER.size))
    if size <= 0 or size > MAX_FRAME_BYTES:
        raise ServiceError(f"非法消息长度：{size}", transport=True)
    body = recv_exact(sock, size)
    try:
        message = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ServiceError(f"消息解析失败：{exc}", transport=True) from None
    if not isinstance(message, dict):
        raise ServiceError("消息格式错误", transport=True)
    return message


def tune(sock: socket.socket) -> None:
    """关 Nagle：请求-应答模式下一次往返立即发出，不要等凑包。"""
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
