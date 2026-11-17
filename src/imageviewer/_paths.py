"""项目级路径的唯一出口。

src 布局下包从 ``<root>/imageviewer/`` 移到 ``<root>/src/imageviewer/``，任何
「按 ``__file__`` 向上数 N 层」的代码都会静默多算一层且不报错。所有指向仓库
根 / 资源目录的路径都从这里取，迁移或再调整层级时只改这一处。
"""
from __future__ import annotations

from pathlib import Path

_ASSETS_NAME = "assets"


def repo_root() -> Path:
    """仓库根（开发态）/ 打包应用根（打包态，main.py 所在目录）。

    本文件位于 ``<root>/src/imageviewer/_paths.py``：
    ``parents[0]=imageviewer``、``[1]=src``、``[2]=<root>``。打包后包被编译到
    ``<app>/src/imageviewer/``，上三级同样是 ``<app>``，两种场景一致。
    """
    return Path(__file__).resolve().parents[2]


def assets_dir() -> Path:
    """返回项目资源目录；优先向上找真实存在的 ``assets/``。

    找不到时回退到仓库根下的 ``assets/``，由调用方决定如何降级（例如跳过
    可选字体）。不要在调用处再写 ``../assets`` —— 层级一变就会静默失效。
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / _ASSETS_NAME
        if candidate.is_dir():
            return candidate
    return repo_root() / _ASSETS_NAME
