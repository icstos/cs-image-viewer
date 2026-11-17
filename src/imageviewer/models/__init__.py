"""模型层 —— 全应用共用的数据结构、状态枚举、常量与值语义。

这一层是唯一「谁都可以依赖」的一层：``infra`` / ``service`` / ``viewmodel`` /
``view`` 都 import 它，而它谁也不 import（标准库除外）。因此它是理解整个程序的
起点，也是断言的落脚点：

* 想知道「窗口上一共有哪些状态」→ :class:`ViewerState`；
* 想知道「一个视图变换是什么」→ :mod:`imageviewer.models.geometry`；
* 想知道「用户能触发哪些意图」→ :class:`Action`；
* 想知道「共享缓存体检报告长什么样」→ :class:`ServiceReport`。

约定：这里**没有**文件 IO、没有控件、没有网络、没有全局可变状态。任何需要
「读一次磁盘」「开一个窗口」的东西都属于 ``infra``。
"""

from __future__ import annotations

from .actions import (
    DEFAULT_INTERVAL,
    KEYBOARD_ONLY,
    SLIDESHOW_INTERVALS,
    Action,
    interval_action,
)
from .diagnostics import BlobStats, DecodeStats, ServiceReport
from .formats import (
    IMAGE_EXTS,
    RAW_EXTS,
    SEEK_FORMATS,
    image_suffixes,
    is_image_path,
)
from .geometry import (
    MAX_ZOOM,
    MIN_REGION,
    MIN_ZOOM,
    NAV_MARGIN,
    NAV_SIZE,
    ZOOM_STEP,
    ScrollIntent,
    ViewMode,
    ViewPoint,
    ViewRect,
    ViewSize,
    ViewTransform,
    clamp_pan,
    clamp_zoom,
    compute_scale,
    image_rect,
    nav_geometry,
    scroll_intent,
    zoom_anchored,
    zoom_by,
    zoom_to_region,
)
from .media import DisplayImage, ImageInfo, ImageProbe, Transform
from .view_state import (
    DialogKind,
    DialogRequest,
    Notice,
    RequestKind,
    StatusSnapshot,
    Tone,
    ViewerState,
)

__all__ = [
    "DEFAULT_INTERVAL",
    "IMAGE_EXTS",
    "KEYBOARD_ONLY",
    "MAX_ZOOM",
    "MIN_REGION",
    "MIN_ZOOM",
    "NAV_MARGIN",
    "NAV_SIZE",
    "RAW_EXTS",
    "SEEK_FORMATS",
    "SLIDESHOW_INTERVALS",
    "ZOOM_STEP",
    "Action",
    "BlobStats",
    "DecodeStats",
    "DialogKind",
    "DialogRequest",
    "DisplayImage",
    "ImageInfo",
    "ImageProbe",
    "Notice",
    "RequestKind",
    "ScrollIntent",
    "ServiceReport",
    "StatusSnapshot",
    "Tone",
    "Transform",
    "ViewMode",
    "ViewPoint",
    "ViewRect",
    "ViewSize",
    "ViewTransform",
    "ViewerState",
    "clamp_pan",
    "clamp_zoom",
    "compute_scale",
    "image_rect",
    "image_suffixes",
    "interval_action",
    "is_image_path",
    "nav_geometry",
    "scroll_intent",
    "zoom_anchored",
    "zoom_by",
    "zoom_to_region",
]
