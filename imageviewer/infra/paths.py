"""运行期目录：缓存、注册表、日志都放在用户级数据目录下。

Windows: %LOCALAPPDATA%\\ImageViewer
macOS:   ~/Library/Application Support/ImageViewer
其它:    $XDG_DATA_HOME/ImageViewer 或 ~/.local/share/ImageViewer

为什么不用临时目录：缓存的价值在于**跨进程、跨启动**复用，
临时目录会被系统清理、也会随重启丢失。多窗口共享的前提是「一份稳定的落盘位置」。

三层分工：
    cache/blobs/   显示用编码结果的磁盘二级缓存（可随时删除，丢了只是重算）
    run/           单实例锁 + 服务注册表（service.json），进程间协商用
    logs/          常驻服务日志
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR_NAME = "ImageViewer"


def app_dir() -> Path:
    """用户级数据根目录；环境变量 `IMAGEVIEWER_HOME` 可整体覆盖（测试/便携用）。"""
    override = os.environ.get("IMAGEVIEWER_HOME")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / APP_DIR_NAME


def cache_dir() -> Path:
    return app_dir() / "cache"


def blob_dir() -> Path:
    return cache_dir() / "blobs"


def run_dir() -> Path:
    return app_dir() / "run"


def log_dir() -> Path:
    return app_dir() / "logs"


def ensure(path: Path) -> Path:
    """建目录（已存在则忽略）；返回同一个 Path 便于链式调用。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return path
