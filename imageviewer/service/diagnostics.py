"""把服务端的裸字典翻译成 :class:`ServiceReport`。

解析集中在这里，而不是散在弹窗代码里，是为了让「哪个键可能缺失」只回答一次。
服务的 ``stats`` 是跨进程协议的一部分，字段可能随版本增减；这里全部走
``.get(..., 默认值)`` 并做强转，拿到模型的代码就不必再写防御性判空。
"""

from __future__ import annotations

from typing import Any

from ..models import BlobStats, DecodeStats, ServiceReport

__all__ = ["parse_report"]

#: 本地模式 / 未启用共享时给用户看的一句话（与菜单里的开关说明对得上）。
LOCAL_MODE_NOTE = "共享缓存未启用（Web 调试或 CSIV_SHARED=0）"


def parse_report(raw: dict[str, Any] | None) -> ServiceReport:
    """``stats`` 字典 → :class:`ServiceReport`（缺失字段一律取默认值）。"""
    if not raw:
        return ServiceReport(role="本地模式", note=LOCAL_MODE_NOTE)
    blobs = raw.get("blobs") or {}
    decoded = raw.get("decoded") or {}
    counters = raw.get("counters") or {}
    return ServiceReport(
        role=str(raw.get("role", "-")),
        backend=str(raw.get("backend", "-")),
        pid=_int(raw.get("pid")),
        is_host=bool(raw.get("is_host", False)),
        acquires=_int(raw.get("acquires")),
        recoveries=_int(raw.get("recoveries")),
        blobs=BlobStats(
            count=_int(blobs.get("count")),
            bytes=_int(blobs.get("bytes")),
            limit_bytes=_int(blobs.get("limit_bytes")),
            hits=_int(blobs.get("hits")),
            misses=_int(blobs.get("misses")),
        ),
        decoded=DecodeStats(
            entries=_int(decoded.get("entries")),
            megapixels=_int(decoded.get("megapixels")),
        ),
        pending=_int(raw.get("pending")),
        preload_done=_int(counters.get("preload_done")),
        error=str(raw.get("error") or ""),
        note=str(raw.get("note") or ""),
    )


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
