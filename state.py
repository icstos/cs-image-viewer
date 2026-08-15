"""可观察状态对象：状态变量驱动 UI 更新。

使用 ``@ft.observable`` 装饰的 dataclass，任何公开字段被赋值时都会
通知订阅者（Flet 组件），触发声明式重新渲染。

注意：字段必须平铺在状态对象上（不要嵌套可观察对象），
否则修改嵌套字段不会通知外层组件。
"""

from __future__ import annotations

from dataclasses import dataclass

import flet as ft


@ft.observable
@dataclass
class ViewerState:
    """全局视图状态 —— UI 的唯一数据源。"""

    # 文件信息
    file_name: str = ""
    file_path: str = ""
    file_size: int = 0
    img_width: int = 0
    img_height: int = 0
    total: int = 0
    index: int = 0
    has_image: bool = False

    # 视图
    zoom: float = 1.0              # 缩放比例 (0.1 ~ 10.0)
    fit_mode: str = "window"       # window | width | actual | custom
    pan_x: float = 0.0
    pan_y: float = 0.0

    # 显示层变换（仅显示，不修改原文件）
    rotation: int = 0              # 0/90/180/270 顺时针角度
    flip_h: bool = False
    flip_v: bool = False

    # 浏览模式
    fullscreen: bool = False
    slideshow: bool = False
    slideshow_interval: float = 3.0  # 秒

    # 视口尺寸（由 on_size_change 驱动）
    viewport_w: float = 0.0
    viewport_h: float = 0.0

    # 提示消息（SnackBar 内容），空字符串表示无提示
    toast: str = ""

    # 确认删除对话框
    confirm_delete: bool = False

    # 错误提示对话框
    error_message: str = ""
    show_error: bool = False

    # 图片数据（bytes 或 None）—— 驱动 Image.src
    image_data: bytes | None = None

    # 是否正在加载
    loading: bool = False

    # ---- 便捷方法 ----

    def notify_image_changed(
        self,
        *,
        name: str = "",
        path: str = "",
        size: int = 0,
        width: int = 0,
        height: int = 0,
        total: int = 0,
        index: int = 0,
        data: bytes | None = None,
    ) -> None:
        """批量更新图片相关字段。"""
        self.file_name = name
        self.file_path = path
        self.file_size = size
        self.img_width = width
        self.img_height = height
        self.total = total
        self.index = index
        self.image_data = data
        self.has_image = data is not None

    def reset_view(self) -> None:
        """切换图片时重置变换与缩放状态。"""
        self.rotation = 0
        self.flip_h = False
        self.flip_v = False
        self.pan_x = 0.0
        self.pan_y = 0.0
        self.zoom = 1.0
        self.fit_mode = "window"
