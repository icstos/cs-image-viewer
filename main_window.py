"""主窗口组件：声明式构建菜单栏 / 图片显示区 / 状态栏，处理全部交互。

采用 Flet 声明式组件模型：``@ft.component`` 函数根据 ``ViewerState``
（可观察对象）构建 UI 树，事件处理器仅修改状态字段，
状态变量变更自动驱动组件重新渲染。
"""

from __future__ import annotations

import asyncio
import inspect

import flet as ft

from image_manager import ImageManager
from shortcuts import KeyEvent, ShortcutHandler
from state import ViewerState

# ---- 常量 ----

MIN_ZOOM = 0.1
MAX_ZOOM = 10.0
ZOOM_FACTOR = 1.25  # 每次缩放倍率
SLIDESHOW_INTERVALS = (1, 2, 3, 5, 10)  # 可选幻灯片间隔（秒）

BG_COLOR = "#1e1e1e"
PANEL_COLOR = "#2d2d2d"
TEXT_COLOR = "#e0e0e0"


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.2f} MB"


def _quarter_turns(rotation: int) -> int:
    return (rotation // 90) % 4


def _effective_dims(state: ViewerState) -> tuple[int, int]:
    """计算考虑旋转后的有效宽高。"""
    w, h = state.img_width, state.img_height
    if _quarter_turns(state.rotation) in (1, 3) and w and h:
        return h, w
    return w, h


# =====================================================================
# 主窗口组件
# =====================================================================


@ft.component
def MainWindow(state: ViewerState) -> ft.Control:
    """应用主窗口。state 为可观察状态对象，UI 由它声明式驱动。"""

    # 稳定对象：跨渲染保持同一实例
    mgr_ref = ft.use_ref(lambda: ImageManager())
    shortcuts_ref = ft.use_ref(lambda: ShortcutHandler())
    picker_ref = ft.use_ref(lambda: ft.FilePicker())
    page_ref = ft.use_ref(None)
    held_keys_ref = ft.use_ref(set())

    if page_ref.current is None:
        page_ref.current = ft.context.page

    mgr: ImageManager = mgr_ref.current
    shortcuts: ShortcutHandler = shortcuts_ref.current
    picker: ft.FilePicker = picker_ref.current
    page: ft.Page = page_ref.current

    def _ctrl_held() -> bool:
        keys = held_keys_ref.current
        return bool(keys & {"control", "controlleft", "controlright", "ctrl"})

    # ---------------------------------------------------------------
    # 导航
    # ---------------------------------------------------------------

    def _navigate(direction: int) -> None:
        if mgr.is_empty():
            return
        if direction > 0:
            mgr.next(loop=True)
        else:
            mgr.prev(loop=True)
        _apply_current_image()
        _trigger_preload()

    def _goto_index(idx: int) -> None:
        if mgr.is_empty():
            return
        mgr.goto(idx)
        _apply_current_image()
        _trigger_preload()

    def _apply_current_image() -> None:
        info = mgr.current
        if info is None or not info.valid:
            state.reset_view()
            state.notify_image_changed(
                name="（无可用图片）",
                total=mgr.count,
                index=mgr.index + 1 if mgr.count else 0,
                data=None,
            )
            return

        data = mgr.get_display_bytes(info)
        if data is None:
            state.error_message = f"无法加载图片：\n{info.name}"
            state.show_error = True
            return

        state.reset_view()
        state.notify_image_changed(
            name=info.name,
            path=info.path,
            size=info.size,
            width=info.width,
            height=info.height,
            total=mgr.count,
            index=mgr.index + 1,
            data=data,
        )
        _apply_fit_mode()

    def _trigger_preload() -> None:
        mgr.preload_adjacent(loop=True)

    # ---------------------------------------------------------------
    # 缩放 / 适配
    # ---------------------------------------------------------------

    def _compute_fit_scale(mode: str) -> float:
        ew, eh = _effective_dims(state)
        if ew == 0 or eh == 0:
            return 1.0
        vw, vh = state.viewport_w, state.viewport_h
        if vw <= 0 or vh <= 0:
            return 1.0
        if mode == "width":
            return vw / ew
        if mode == "actual":
            return 1.0
        # window
        return min(vw / ew, vh / eh)

    def _apply_fit_mode() -> None:
        state.zoom = _compute_fit_scale(state.fit_mode)
        state.pan_x = 0.0
        state.pan_y = 0.0

    def _fit_window() -> None:
        state.fit_mode = "window"
        _apply_fit_mode()

    def _fit_width() -> None:
        state.fit_mode = "width"
        _apply_fit_mode()

    def _actual_size() -> None:
        state.fit_mode = "actual"
        _apply_fit_mode()

    def _zoom_by(factor: float) -> None:
        new_zoom = _clamp(state.zoom * factor, MIN_ZOOM, MAX_ZOOM)
        state.fit_mode = "custom"
        state.zoom = new_zoom

    # ---------------------------------------------------------------
    # 显示层变换（仅显示，不修改原文件）
    # ---------------------------------------------------------------

    def _rotate(delta: int) -> None:
        state.rotation = (state.rotation + delta) % 360
        _apply_fit_mode()

    def _flip(axis: str) -> None:
        if axis == "h":
            state.flip_h = not state.flip_h
        else:
            state.flip_v = not state.flip_v

    # ---------------------------------------------------------------
    # 浏览模式
    # ---------------------------------------------------------------

    def _toggle_fullscreen() -> None:
        state.fullscreen = not state.fullscreen

    def _toggle_slideshow() -> None:
        state.slideshow = not state.slideshow

    def _set_slideshow_interval(sec: float) -> None:
        state.slideshow_interval = sec

    def _on_escape() -> None:
        if state.show_error:
            state.show_error = False
        elif state.confirm_delete:
            state.confirm_delete = False
        elif state.slideshow:
            state.slideshow = False
        elif state.fullscreen:
            state.fullscreen = False

    # ---------------------------------------------------------------
    # 文件打开
    # ---------------------------------------------------------------

    async def _open_file() -> None:
        try:
            files = await picker.pick_files(
                dialog_title="打开图片",
                file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=[
                    "jpg", "jpeg", "png", "bmp", "gif",
                    "webp", "tif", "tiff", "ico", "jfif",
                ],
                allow_multiple=False,
            )
            if not files:
                return
            path = files[0].path or files[0].name
            if mgr.load_single(path):
                _apply_current_image()
                _trigger_preload()
            else:
                state.error_message = "无法打开此文件，可能不是支持的图片格式。"
                state.show_error = True
        except Exception as ex:
            state.error_message = f"打开文件时出错：\n{ex}"
            state.show_error = True

    async def _open_folder() -> None:
        try:
            dir_path = await picker.get_directory_path(dialog_title="打开文件夹")
            if not dir_path:
                return
            if mgr.load_folder(dir_path):
                _apply_current_image()
                _trigger_preload()
            else:
                state.error_message = "该文件夹中没有找到支持的图片文件。"
                state.show_error = True
        except Exception as ex:
            state.error_message = f"打开文件夹时出错：\n{ex}"
            state.show_error = True

    # ---------------------------------------------------------------
    # 删除
    # ---------------------------------------------------------------

    def _request_delete() -> None:
        if mgr.current is None:
            return
        state.confirm_delete = True

    def _confirm_delete() -> None:
        state.confirm_delete = False
        if mgr.delete_current():
            _apply_current_image()
            _trigger_preload()
            state.toast = "已移至回收站。"
        else:
            state.error_message = "删除失败，文件可能被占用。"
            state.show_error = True

    # ---------------------------------------------------------------
    # 鼠标交互
    # ---------------------------------------------------------------

    def _on_pan_update(e: ft.DragUpdateEvent) -> None:
        if e.local_delta is None:
            return
        ew, eh = _effective_dims(state)
        display_w = ew * state.zoom
        display_h = eh * state.zoom
        vw, vh = state.viewport_w, state.viewport_h

        new_x = state.pan_x + e.local_delta.x
        new_y = state.pan_y + e.local_delta.y

        # pan 是相对“居中位置”的偏移，换算为 left/top 后需限制在可视区内
        if vw > 0:
            center_x = (vw - display_w) / 2
            if display_w > vw:
                new_x = _clamp(
                    new_x, (vw - display_w) - center_x, -center_x
                )
            else:
                new_x = 0.0
        if vh > 0:
            center_y = (vh - display_h) / 2
            if display_h > vh:
                new_y = _clamp(
                    new_y, (vh - display_h) - center_y, -center_y
                )
            else:
                new_y = 0.0

        state.pan_x = new_x
        state.pan_y = new_y

    def _on_scroll(e: ft.ScrollEvent) -> None:
        if e.scroll_delta is None:
            return
        dy = e.scroll_delta.y
        if _ctrl_held():
            factor = ZOOM_FACTOR if dy < 0 else 1 / ZOOM_FACTOR
            _zoom_by(factor)
        else:
            if dy > 0:
                _navigate(1)
            elif dy < 0:
                _navigate(-1)

    def _on_double_tap(e: ft.ControlEvent) -> None:
        state.fullscreen = not state.fullscreen

    def _on_viewport_resize(e: ft.LayoutSizeChangeEvent) -> None:
        if e.width == state.viewport_w and e.height == state.viewport_h:
            return
        state.viewport_w = e.width
        state.viewport_h = e.height
        if state.fit_mode != "custom":
            _apply_fit_mode()

    # ---------------------------------------------------------------
    # 键盘
    # ---------------------------------------------------------------

    async def _on_keyboard(e: ft.KeyboardEvent) -> None:
        await shortcuts.handle(KeyEvent(
            key=e.key, ctrl=e.ctrl, shift=e.shift, alt=e.alt, meta=e.meta,
        ))

    def _on_key_down(e: ft.KeyDownEvent) -> None:
        held_keys_ref.current.add((e.key or "").lower())

    def _on_key_up(e: ft.KeyUpEvent) -> None:
        held_keys_ref.current.discard((e.key or "").lower())

    # ---------------------------------------------------------------
    # Effects：注册快捷键、键盘监听、全屏、幻灯片、退出清理
    # ---------------------------------------------------------------

    def _register_shortcuts() -> None:
        shortcuts.register("next", lambda: _navigate(1))
        shortcuts.register("prev", lambda: _navigate(-1))
        shortcuts.register("first", lambda: _goto_index(0))
        shortcuts.register("last", lambda: _goto_index(len(mgr.files) - 1))
        shortcuts.register("zoom_in", lambda: _zoom_by(ZOOM_FACTOR))
        shortcuts.register("zoom_out", lambda: _zoom_by(1 / ZOOM_FACTOR))
        shortcuts.register("fit_window", _fit_window)
        shortcuts.register("fit_width", _fit_width)
        shortcuts.register("actual_size", _actual_size)
        shortcuts.register("toggle_fullscreen", _toggle_fullscreen)
        shortcuts.register("rotate_cw", lambda: _rotate(90))
        shortcuts.register("rotate_ccw", lambda: _rotate(-90))
        shortcuts.register("flip_h", lambda: _flip("h"))
        shortcuts.register("flip_v", lambda: _flip("v"))
        shortcuts.register("open_file", lambda: asyncio.create_task(_open_file()))
        shortcuts.register("open_folder", lambda: asyncio.create_task(_open_folder()))
        shortcuts.register("delete", _request_delete)
        shortcuts.register("escape", _on_escape)
        shortcuts.register("slideshow_toggle", _toggle_slideshow)

    ft.use_effect(_register_shortcuts, dependencies=[])

    def _setup_keyboard() -> None:
        page.on_keyboard_event = _on_keyboard
        page.update()
    ft.use_effect(_setup_keyboard, dependencies=[])

    def _apply_fullscreen() -> None:
        page.window.full_screen = state.fullscreen
        page.window.update()
    ft.use_effect(_apply_fullscreen, dependencies=[state.fullscreen])

    async def _slideshow_loop() -> None:
        while state.slideshow:
            await asyncio.sleep(state.slideshow_interval)
            if state.slideshow:
                _navigate(1)

    def _setup_slideshow():
        if not state.slideshow:
            return None
        task = asyncio.create_task(_slideshow_loop())

        def _cancel():
            task.cancel()
        return _cancel

    ft.use_effect(
        _setup_slideshow,
        dependencies=[state.slideshow, state.slideshow_interval],
    )

    # ---------------------------------------------------------------
    # 声明式对话框
    # ---------------------------------------------------------------

    ft.use_dialog(
        ft.SnackBar(
            content=ft.Text(state.toast),
            bgcolor="#3a9d5d",
            duration=2500,
            show_close_icon=True,
            on_dismiss=lambda e: setattr(state, "toast", ""),
        )
        if state.toast
        else None
    )

    ft.use_dialog(
        ft.AlertDialog(
            modal=True,
            title=ft.Text("提示"),
            content=ft.Text(state.error_message),
            actions=[
                ft.TextButton(
                    "确定",
                    on_click=lambda e: setattr(state, "show_error", False),
                ),
            ],
        )
        if state.show_error
        else None
    )

    ft.use_dialog(
        ft.AlertDialog(
            modal=True,
            title=ft.Text("确认删除"),
            content=ft.Text(f"确定要将以下文件移至回收站？\n\n{state.file_name}"),
            actions=[
                ft.TextButton(
                    "删除",
                    style=ft.ButtonStyle(color=ft.Colors.RED),
                    on_click=lambda e: _confirm_delete(),
                ),
                ft.TextButton(
                    "取消",
                    on_click=lambda e: setattr(state, "confirm_delete", False),
                ),
            ],
        )
        if state.confirm_delete
        else None
    )

    # ---------------------------------------------------------------
    # 构建 UI 树
    # ---------------------------------------------------------------

    def _run_menu_action(name: str) -> None:
        """菜单点击：执行动作，若返回协程则调度到事件循环。"""
        callbacks: dict[str, object] = {
            "quit": lambda: page.window.close(),
            "open_file": _open_file,
            "open_folder": _open_folder,
            "delete": _request_delete,
            "fit_window": _fit_window,
            "fit_width": _fit_width,
            "actual_size": _actual_size,
            "rotate_cw": lambda: _rotate(90),
            "rotate_ccw": lambda: _rotate(-90),
            "flip_h": lambda: _flip("h"),
            "flip_v": lambda: _flip("v"),
            "toggle_fullscreen": _toggle_fullscreen,
            "slideshow_toggle": _toggle_slideshow,
        }
        cb = callbacks.get(name)
        if cb is None:
            return
        result = cb()
        if inspect.isawaitable(result):
            asyncio.create_task(result)

    image_area = _build_image_area(
        state, _on_viewport_resize, _on_pan_update, _on_scroll, _on_double_tap
    )
    body: ft.Control
    if state.fullscreen:
        body = ft.Container(content=image_area, bgcolor=BG_COLOR, expand=True)
    else:
        body = ft.Column(
            controls=[
                _build_menu_bar(_run_menu_action, state),
                ft.Container(content=image_area, bgcolor=BG_COLOR, expand=True),
                _build_status_bar(state),
            ],
            spacing=0,
            expand=True,
        )

    return ft.KeyboardListener(
        content=body,
        autofocus=True,
        on_key_down=_on_key_down,
        on_key_up=_on_key_up,
    )


# =====================================================================
# UI 构建辅助函数
# =====================================================================


def _build_menu_bar(run_action, state: ViewerState) -> ft.MenuBar:
    """顶部菜单栏：文件 / 编辑 / 视图。"""

    def item(label: str, action: str, icon) -> ft.MenuItemButton:
        return ft.MenuItemButton(
            content=ft.Text(label),
            leading=ft.Icon(icon),
            on_click=lambda e: run_action(action),
        )

    interval_items = [
        ft.MenuItemButton(
            content=ft.Text(f"{sec} 秒"),
            on_click=lambda e, s=sec: setattr(state, "slideshow_interval", s),
        )
        for sec in SLIDESHOW_INTERVALS
    ]

    return ft.MenuBar(
        controls=[
            ft.SubmenuButton(
                content=ft.Text("文件"),
                controls=[
                    item("打开文件...", "open_file", ft.Icons.FILE_OPEN),
                    item("打开文件夹...", "open_folder", ft.Icons.FOLDER_OPEN),
                    item("退出", "quit", ft.Icons.EXIT_TO_APP),
                ],
            ),
            ft.SubmenuButton(
                content=ft.Text("编辑"),
                controls=[
                    item("删除（移至回收站）", "delete", ft.Icons.DELETE),
                ],
            ),
            ft.SubmenuButton(
                content=ft.Text("视图"),
                controls=[
                    item("适应窗口", "fit_window", ft.Icons.FIT_SCREEN),
                    item("适应宽度", "fit_width", ft.Icons.ALIGN_HORIZONTAL_CENTER),
                    item("1:1 原始大小", "actual_size", ft.Icons.ZOOM_IN_MAP),
                    item("顺时针旋转 90°", "rotate_cw", ft.Icons.ROTATE_RIGHT),
                    item("逆时针旋转 90°", "rotate_ccw", ft.Icons.ROTATE_LEFT),
                    item("水平翻转", "flip_h", ft.Icons.FLIP),
                    item("垂直翻转", "flip_v", ft.Icons.FLIP),
                    item("全屏", "toggle_fullscreen", ft.Icons.FULLSCREEN),
                    item("幻灯片播放", "slideshow_toggle", ft.Icons.PLAY_CIRCLE),
                    ft.SubmenuButton(
                        content=ft.Text("幻灯片间隔"),
                        controls=interval_items,
                    ),
                ],
            ),
        ],
    )


def _build_status_bar(state: ViewerState) -> ft.Control:
    """底部状态栏：左侧文件名，中间分辨率+大小，右侧缩放+页码。"""
    dim_text = f"{state.img_width} × {state.img_height}" if state.img_width else ""
    size_text = _human_size(state.file_size) if state.file_size else ""
    mid = "  ·  ".join(filter(None, [dim_text, size_text])) or "—"

    page_text = f"{state.index} / {state.total}" if state.total else "0 / 0"
    zoom_text = f"{int(round(state.zoom * 100))}%"

    return ft.Container(
        content=ft.Row(
            controls=[
                ft.Container(
                    content=ft.Text(
                        state.file_name or "未打开图片",
                        size=12,
                        color=TEXT_COLOR,
                        overflow=ft.TextOverflow.ELLIPSIS,
                    ),
                    expand=True,
                ),
                ft.Text(mid, size=12, color=TEXT_COLOR),
                ft.Text(zoom_text, size=12, color=TEXT_COLOR),
                ft.Text(page_text, size=12, color=TEXT_COLOR),
            ],
            spacing=16,
        ),
        bgcolor=PANEL_COLOR,
        padding=ft.Padding.symmetric(horizontal=12, vertical=6),
    )


def _build_image_area(
    state: ViewerState,
    on_resize,
    on_pan,
    on_scroll,
    on_double_tap,
) -> ft.Control:
    """图片显示区域：GestureDetector + Stack + 定位图片。"""
    if not state.has_image or state.image_data is None:
        return ft.Container(
            content=ft.Column(
                controls=[
                    ft.Icon(
                        ft.Icons.IMAGE,
                        size=64,
                        color=ft.Colors.with_opacity(0.3, ft.Colors.WHITE),
                    ),
                    ft.Text(
                        "按 Ctrl+O 打开文件，Ctrl+Shift+O 打开文件夹",
                        color=ft.Colors.with_opacity(0.5, ft.Colors.WHITE),
                        size=13,
                    ),
                ],
                alignment=ft.MainAxisAlignment.CENTER,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=12,
            ),
            alignment=ft.Alignment.CENTER,
            expand=True,
            on_size_change=on_resize,
        )

    # 计算显示尺寸和位置（居中对齐 + 平移）
    ew, eh = _effective_dims(state)
    display_w = ew * state.zoom
    display_h = eh * state.zoom
    vw, vh = state.viewport_w, state.viewport_h

    if vw > 0:
        center_x = (vw - display_w) / 2
        if display_w > vw:
            pan_x = _clamp(
                center_x + state.pan_x,
                vw - display_w,
                0,
            )
        else:
            pan_x = center_x
    else:
        pan_x = 0.0

    if vh > 0:
        center_y = (vh - display_h) / 2
        if display_h > vh:
            pan_y = _clamp(
                center_y + state.pan_y,
                vh - display_h,
                0,
            )
        else:
            pan_y = center_y
    else:
        pan_y = 0.0

    # 旋转（90° 整数倍）+ 翻转
    q = _quarter_turns(state.rotation)
    rotated = ft.RotatedBox(
        quarter_turns=q,
        content=ft.Image(
            src=state.image_data,
            width=max(state.img_width * state.zoom, 1),
            height=max(state.img_height * state.zoom, 1),
            fit=ft.BoxFit.FILL,
            gapless_playback=True,
            filter_quality=ft.FilterQuality.LOW,
        ),
    )

    displayed = ft.Container(
        content=rotated,
        left=pan_x,
        top=pan_y,
        width=max(display_w, 1),
        height=max(display_h, 1),
        flip=ft.Flip(flip_x=state.flip_h, flip_y=state.flip_v),
    )

    return ft.GestureDetector(
        content=ft.Stack(
            controls=[displayed],
            expand=True,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
        ),
        on_pan_update=on_pan,
        on_scroll=on_scroll,
        on_double_tap=on_double_tap,
        mouse_cursor=ft.MouseCursor.GRAB,
        expand=True,
        drag_interval=16,
        on_size_change=on_resize,
    )