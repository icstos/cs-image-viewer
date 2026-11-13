"""诊断模型 —— 共享缓存的健康状况，**结构化**而不是一堆裸字典。

旧实现把服务端 ``stats`` 的字典直接传到弹窗里拼字符串，结果「哪个键存在、
哪个键可能缺失」只能靠读服务端代码猜。现在解析这一步收敛在
``service/diagnostics.py``，这里只声明形状：拿到模型的代码可以放心取值，
拼文案的代码也不必再写 ``info.get("blobs") or {}`` 这种防御。
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["BlobStats", "DecodeStats", "ServiceReport"]


@dataclass(frozen=True, slots=True)
class BlobStats:
    """磁盘二级缓存：条目数、占用、上限、命中 / 未命中次数。"""

    count: int = 0
    bytes: int = 0
    limit_bytes: int = 0
    hits: int = 0
    misses: int = 0

    @property
    def hit_rate(self) -> int:
        """命中率百分比；样本为 0 时返回 ``-1``（表示「没有数据」而不是「0%」）。"""
        total = self.hits + self.misses
        return self.hits * 100 // total if total else -1


@dataclass(frozen=True, slots=True)
class DecodeStats:
    """内存里的解码结果缓存（旋转 / 重编码可复用它，不重新解码像素）。"""

    entries: int = 0
    megapixels: int = 0


@dataclass(frozen=True, slots=True)
class ServiceReport:
    """一次共享缓存体检的完整结果。

    ``note`` 非空表示「不是一个正常的服务连接」——例如本地模式、不可用，
    此时其余字段没有意义，弹窗只显示这一句人话。
    """

    role: str = ""
    backend: str = "-"
    pid: int = 0
    is_host: bool = False
    acquires: int = 0
    recoveries: int = 0
    blobs: BlobStats = field(default_factory=BlobStats)
    decoded: DecodeStats = field(default_factory=DecodeStats)
    pending: int = 0
    preload_done: int = 0
    error: str = ""
    note: str = ""

    @property
    def connected(self) -> bool:
        return not self.note and not self.error
