"""LiteView 看图 - 基于 Flet 的桌面端轻量看图软件入口。

运行：
    py -3.12 main.py
"""
from __future__ import annotations

import asyncio
import os
import sys

import flet as ft

from viewer import app as app_module
from viewer.app import ImageViewerApp


async def main(page: ft.Page) -> None:
    page.title = "LiteView 看图"
    page.bgcolor = "#141414"
    page.padding = 0
    # 在 render 前捕获初始客户端尺寸（0.86.x 的 page.width/height 可能陈旧）
    page.on_resize = lambda e: app_module.set_initial_size(e.width, e.height)
    if len(sys.argv) > 1:
        app_module.START_PATH = os.path.abspath(sys.argv[1])
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
    print(f"[smoke] argv={sys.argv[1:]} start_path={app_module.START_PATH}", flush=True)
    await asyncio.sleep(2)
    print(f"[smoke] window={page.window.width}x{page.window.height} fullscreen={page.window.full_screen}", flush=True)
    await asyncio.sleep(4)
    if os.environ.get("CSIV_SMOKE"):          # 冒烟测试模式：数秒后自动退出
        print("[smoke] closing window", flush=True)
        await page.window.close()
        print("[smoke] closed, exiting", flush=True)


if __name__ == "__main__":
    if os.environ.get("CSIV_WEB"):
        # 开发验证模式：以 Web 方式运行（同一套代码，便于截图/交互调试）
        ft.run(main, view=ft.AppView.WEB_BROWSER, port=8550)
    else:
        ft.run(main)
