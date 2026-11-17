"""格式白名单 —— 「这个程序能打开什么」。

放在模型层而不是解码器里，是因为**三处都要用它，而且必须一致**：

* ``service`` 打开文件时先快速拒绝不支持的扩展名，不必等解码失败；
* ``view`` 拿它填文件对话框的过滤器；
* ``infra`` 的解码器按它路由（RAW 走 rawpy、SVG 走 resvg、其余走 Pillow）。

若把它留在解码器里，上层就只能靠「试着解码一下」来判断支持与否 —— 那意味着
一次无谓的磁盘读，且错误信息会变成底层的 ``UnidentifiedImageError``。
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["IMAGE_EXTS", "RAW_EXTS", "SEEK_FORMATS", "image_suffixes", "is_image_path"]

#: 支持的图片扩展名（小写、带点）。
IMAGE_EXTS: frozenset[str] = frozenset(
    {
        # 主流格式
        ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".bmp", ".gif", ".webp", ".tif", ".tiff",
        # 特殊格式
        ".psd",                                    # Photoshop（Pillow 原生）
        ".svg",                                    # 矢量图（resvg 渲染）
        ".heic", ".heif",                          # 高效图片格式（pillow-heif）
        # 相机 RAW
        ".cr2", ".cr3", ".nef", ".nrw", ".arw", ".srf", ".dng", ".raf", ".orf", ".rw2",
        ".pef", ".srw", ".erf", ".3fr", ".mef", ".mrw", ".iiq", ".kdc", ".raw", ".x3f",
    }
)

#: 相机 RAW 扩展名（走 rawpy 解码）。
RAW_EXTS: frozenset[str] = frozenset(
    {
        ".cr2", ".cr3", ".nef", ".nrw", ".arw", ".srf", ".dng", ".raf", ".orf", ".rw2",
        ".pef", ".srw", ".erf", ".3fr", ".mef", ".mrw", ".iiq", ".kdc", ".raw", ".x3f",
    }
)

#: 依赖文件句柄进行多帧 seek 的格式（其余格式完全解码后可释放句柄）。
SEEK_FORMATS: frozenset[str] = frozenset({".gif", ".webp", ".tif", ".tiff"})


def is_image_path(path: Path) -> bool:
    """按扩展名判断是否支持（不做文件存在性以外的 IO）。"""
    return path.suffix.lower() in IMAGE_EXTS


def image_suffixes() -> list[str]:
    """文件对话框用的扩展名列表（不带点、已排序、每次现算）。"""
    return sorted(ext.lstrip(".") for ext in IMAGE_EXTS)
