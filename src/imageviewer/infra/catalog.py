"""目录扫描 —— 「这个文件夹里有哪些图，按什么顺序」。

单独成文件而不是塞进解码器里，是因为它有一条自己的性能底线：**大目录与网络盘**。
``os.scandir`` 的目录项自带类型信息，不必对每个文件再 stat 一次 —— 一千张图的
目录里，省下的就是一千次系统调用；而它返回的顺序是文件系统的随机顺序，因此
自然排序（``img2`` 在 ``img10`` 之前）必须在这里做完，不能让界面层各排一遍。

纯函数、无状态：给同一个目录两次调用会得到同样的列表（除非目录内容变了），
所以它可以被直接断言，也可以被共享缓存服务与本地解码器同时使用。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from ..models.formats import IMAGE_EXTS

__all__ = ["IMAGE_EXTS", "SortKey", "is_image", "list_images", "natural_key"]

_DIGIT_RE = re.compile(r"(\d+)")

#: 自然排序键：数字段按整数比较、文字段按小写比较。
SortKey = tuple[int | str, ...]


def natural_key(text: str) -> SortKey:
    """自然排序键：让「img2.png」排在「img10.png」之前。

    切分后数字段转 ``int``、其余转小写，元组逐项比较即可；纯数字文件名与
    大小写混杂的文件名都能得到符合直觉的顺序。
    """
    return tuple(
        int(part) if part.isdigit() else part.lower()
        for part in _DIGIT_RE.split(text)
    )


def is_image(path: Path) -> bool:
    """按扩展名判断是否支持解码（不做文件是否存在以外的 IO）。"""
    return path.suffix.lower() in IMAGE_EXTS


def list_images(folder: Path) -> list[Path]:
    """列出目录内的图片文件（自然排序）；目录不可读时返回空列表。

    用 ``os.scandir`` 而非 ``iterdir``：文件类型来自目录项自带的元数据，
    不必对每个文件再 stat 一次 —— 大目录与网络盘上省下的就是成百上千次系统调用。
    """
    try:
        with os.scandir(folder) as entries:
            files = [
                Path(entry.path)
                for entry in entries
                if entry.is_file() and os.path.splitext(entry.name)[1].lower() in IMAGE_EXTS
            ]
    except OSError:
        return []
    return sorted(files, key=lambda p: natural_key(p.stem))
