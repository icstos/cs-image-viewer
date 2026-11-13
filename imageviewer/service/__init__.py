"""服务层 —— 业务规则与编排。**只依赖模型和自己的端口协议。**

它回答的问题都是「业务上该怎么做」，与技术无关：

* 打开一张图要不要顺手扫整个目录？（不，先出图，目录列表稍后补）
* 翻到下一张时遇到坏图？（跳过，首尾循环，最多绕一圈）
* 删除之后游标落在哪？（下一张；已是最后一张就退到新的最后一张）
* 旋转过的图导出时按哪个方向写？（显示层变换，不写回原文件）

结构：

    ports       ``service`` 需要的外部能力（解码器 / 回收站 / 关联 / 平台 / 拉起进程）
    gallery     画册：文件夹列表、游标、变换、帧号
    viewer      ``ViewerService``：把画册、解码器与周边能力缝成一组用例
    diagnostics 服务端裸字典 → :class:`ServiceReport`

单向依赖：``view`` → ``viewmodel`` → ``service`` → ``infra``；本层可以 import
``models`` 与 ``.ports``，**不得** import ``infra`` / ``viewmodel`` / ``view``。
"""

from __future__ import annotations

from .diagnostics import LOCAL_MODE_NOTE, parse_report
from .gallery import Gallery
from .ports import (
    AssociationStore,
    DisplayProvider,
    FileTrash,
    PlatformInfo,
    ProcessLauncher,
)
from .viewer import ViewerService

__all__ = [
    "LOCAL_MODE_NOTE",
    "AssociationStore",
    "DisplayProvider",
    "FileTrash",
    "Gallery",
    "PlatformInfo",
    "ProcessLauncher",
    "ViewerService",
    "parse_report",
]
