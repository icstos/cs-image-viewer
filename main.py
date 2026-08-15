"""CS Image Viewer 入口。

直接运行即可启动：
    python main.py
"""

from __future__ import annotations

import flet as ft

from main_window import MainWindow
from state import ViewerState


def main(page: ft.Page) -> None:
    page.title = "CS Image Viewer"
    page.theme_mode = ft.ThemeMode.DARK
    page.bgcolor = "#1e1e1e"
    page.padding = 0

    page.window.width = 1100
    page.window.height = 750
    page.window.min_width = 600
    page.window.min_height = 400

    page.render(MainWindow, ViewerState())


if __name__ == "__main__":
    ft.run(main)