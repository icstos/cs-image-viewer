"""LiteView 看图 - 基于 Flet 的桌面端轻量看图软件入口。

运行：
    py -3.12 main.py
    py -3.12 main.py <图片或文件夹>

打包后由文件关联（“打开方式”）启动时，路径通过 `LITEVIEW_OPEN` 环境变量传入，
详见 `viewer/file_assoc.py` 顶部的说明。
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import flet as ft

from viewer import app as app_module
from viewer.app import ImageViewerApp

# 文件关联启动器写入的通道（与 viewer/file_assoc.py 保持一致）
OPEN_ENV = "LITEVIEW_OPEN"
REQUEST_FILE = Path(os.environ.get("LOCALAPPDATA", "")) / "LiteView" / "open-request.txt"
REQUEST_MAX_AGE = 15.0  # 秒：超过视为上次残留，直接丢弃


def _take_request_file() -> str | None:
    """取走启动器留下的“打开请求”文件（环境变量通道的兜底）。

    无论是否采用都删除，避免残留影响后续手动启动；只有足够新的文件才认。
    """
    try:
        if not REQUEST_FILE.is_file():
            return None
        age = time.time() - REQUEST_FILE.stat().st_mtime
        text = REQUEST_FILE.read_text(encoding="utf-16").strip()
    except (OSError, UnicodeError):
        return None
    finally:
        try:
            REQUEST_FILE.unlink()
        except OSError:
            pass
    return text if text and age <= REQUEST_MAX_AGE else None


def _resolve_start_path() -> tuple[str | None, str]:
    """确定启动时要打开的文件/文件夹，返回 (绝对路径, 来源说明)。

    三条通道，按优先级：

    1. `LITEVIEW_OPEN` 环境变量 —— 打包后“打开方式”的主通道。
       `flet build` 产物的 Dart 外壳会把任何命令行参数当成“开发模式”的页面地址，
       关联命令直接传 `"%1"` 会导致 Python 端完全不启动，因此改由启动器写环境变量
       并以无参数方式拉起 exe。
    2. 请求文件 —— 同一启动器的兜底通道（环境变量被系统策略拦截时仍可用）。
    3. 命令行参数 —— 开发期 `py -3.12 main.py <路径>` 及控制台脚本 `liteview <路径>`。

    不存在的路径直接跳过（关联表里的历史记录可能已指向被删除的文件）。
    """
    candidates: list[tuple[str | None, str]] = [
        (os.environ.get(OPEN_ENV), f"env:{OPEN_ENV}"),
        (_take_request_file(), "request-file"),
        *((arg, "argv") for arg in sys.argv[1:]),
    ]
    for raw, source in candidates:
        if not raw:
            continue
        candidate = os.path.abspath(raw.strip().strip('"'))
        if os.path.exists(candidate):
            return candidate, source
    return None, "none"


async def main(page: ft.Page) -> None:
    page.title = "LiteView 看图"
    page.bgcolor = "#141414"
    page.padding = 0
    # 在 render 前捕获初始客户端尺寸（0.86.x 的 page.width/height 可能陈旧）
    page.on_resize = lambda e: app_module.set_initial_size(e.width, e.height)
    start_path, start_source = _resolve_start_path()
    if start_path:
        app_module.START_PATH = start_path
    os.environ.pop(OPEN_ENV, None)          # 用掉即清，避免泄漏给子进程
    page.render(ImageViewerApp)
    if page.web:
        # Web 调试模式：浏览器尺寸不受 Flet 窗口属性控制
        return
    # 注意：0.86.x 的窗口/主题属性须在 render 之后统一设置，否则尺寸会被重置为默认值
    page.theme_mode = ft.ThemeMode.DARK
    page.window.min_width = 640
    page.window.min_height = 480
    page.window.width = 1100
    page.window.height = 760
    page.update()
    await page.window.center()
    print("[smoke] rendered", flush=True)
    print(
        f"[smoke] argv={sys.argv[1:]} start={start_source} start_path={start_path}",
        flush=True,
    )
    await asyncio.sleep(2)
    print(f"[smoke] window={page.window.width}x{page.window.height} fullscreen={page.window.full_screen}", flush=True)
    await asyncio.sleep(4)
    if os.environ.get("CSIV_SMOKE"):          # 冒烟测试模式：数秒后自动退出
        print("[smoke] closing window", flush=True)
        await page.window.close()
        print("[smoke] closed, exiting", flush=True)


def run() -> None:
    """启动入口：`py -3.12 main.py` 与 `liteview` 控制台脚本共用。"""
    if os.environ.get("CSIV_WEB"):
        # 开发验证模式：以 Web 方式运行（同一套代码，便于截图/交互调试）
        ft.run(main, view=ft.AppView.WEB_BROWSER, port=8550)
    else:
        ft.run(main)


if __name__ == "__main__":
    run()
