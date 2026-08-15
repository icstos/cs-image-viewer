"""LiteView 看图 - 主界面（声明式 Flet 组件）。

结构：
    ImageViewerApp  根组件：顶部菜单栏 + 图片浏览区 + 底部状态栏
    AppState        全部可变状态，保存在 use_ref 中，事件回调直接读写
    sync_ui()       状态 -> UI 镜像（use_state）的统一同步入口，驱动组件重绘
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import flet as ft

from .image_manager import ImageManager, format_size
from .shortcuts import match

START_PATH: str | None = None   # 启动时自动打开的文件/文件夹（由 main.py 注入）
INITIAL_SIZE: tuple[float, float] | None = None   # 初始客户端尺寸（由 main.py 在 render 前捕获）


def set_initial_size(width: float, height: float) -> None:
    """main.py 在 render 前注册的 resize 记录器：捕获初始客户端尺寸。"""
    global INITIAL_SIZE
    INITIAL_SIZE = (width, height)

# ---------- 布局与主题常量 ----------
MENU_H = 48.0                       # 顶部菜单栏高度
STATUS_H = 32.0                     # 底部状态栏高度
BG_COLOR = "#141414"                # 图片区背景（近黑，沉浸式看图）
PANEL_COLOR = "#1f1f1f"             # 菜单 / 状态栏面板底色
PANEL_BORDER = "#333333"
FG_COLOR = "#e6e6e6"
FG_DIM = "#8f8f8f"

MIN_ZOOM = 0.1                      # 最小缩放 10%
MAX_ZOOM = 10.0                     # 最大缩放 1000%
ZOOM_STEP = 1.12                    # 单次缩放步进系数

SLIDESHOW_INTERVALS = (1, 2, 3, 5, 10)   # 幻灯片可选间隔（秒）


def _ctrl_pressed() -> bool:
    """查询系统级 Ctrl 键是否按住（Windows）。

    作为 KeyboardListener 的兜底：焦点不在图片区时监听器收不到修饰键事件。
    """
    try:
        import ctypes

        return bool(ctypes.windll.user32.GetAsyncKeyState(0x11) & 0x8000)
    except Exception:
        return False


@dataclass
class ViewState:
    """视图状态：显示模式、缩放倍率与平移偏移。"""
    mode: str = "fit"               # fit(适应窗口) | fit_width(适应宽度) | actual(1:1) | custom(自定义)
    zoom: float = 1.0               # 自定义缩放倍率（0.1 ~ 10）
    pan_x: float = 0.0
    pan_y: float = 0.0

    def reset(self) -> None:
        """回到「适应窗口」并复位平移。"""
        self.mode = "fit"
        self.zoom = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0


@dataclass
class AppState:
    """应用全部可变状态；实例保存在 use_ref 中，回调直接读写最新值。"""
    manager: ImageManager = field(default_factory=ImageManager)
    view: ViewState = field(default_factory=ViewState)
    fullscreen: bool = False
    slideshow_on: bool = False
    paused: bool = False
    interval: float = 3.0                       # 幻灯片间隔（秒）
    ctrl_held: bool = False                     # Ctrl 是否按住（滚轮缩放判定）
    viewport: tuple[float, float] = (900.0, 560.0)   # 图片区可视尺寸
    gen: int = 0                                # 异步解码代际计数，防止旧任务覆盖新状态
    slideshow_task: asyncio.Future | None = None


def _compute_scale(view: ViewState, iw: float, ih: float, vw: float, vh: float) -> float:
    """按显示模式计算缩放倍率（严格保持宽高比）。"""
    if iw <= 0 or ih <= 0 or vw <= 0 or vh <= 0:
        return 1.0
    if view.mode == "fit":
        return min(vw / iw, vh / ih)
    if view.mode == "fit_width":
        return vw / iw
    if view.mode == "actual":
        return 1.0
    return view.zoom


def _clamp_pan(view: ViewState, iw: float, ih: float, vw: float, vh: float, scale: float) -> None:
    """平移边界限制：图片边缘不允许进入视口内部；未溢出时强制居中。"""
    disp_w, disp_h = iw * scale, ih * scale
    max_x = max(0.0, (disp_w - vw) / 2)
    max_y = max(0.0, (disp_h - vh) / 2)
    view.pan_x = min(max(-max_x, view.pan_x), max_x)
    view.pan_y = min(max(-max_y, view.pan_y), max_y)


def _zoom_anchored(view: ViewState, iw: float, ih: float, vw: float, vh: float,
                   cx: float, cy: float, factor: float) -> bool:
    """以视口内 (cx, cy) 为锚点缩放：锚点下的图片像素在缩放前后保持不动。

    返回 True 表示缩放生效。
    """
    if iw <= 0 or vw <= 0:
        return False
    s0 = _compute_scale(view, iw, ih, vw, vh)          # 当前渲染倍率
    s1 = max(MIN_ZOOM, min(MAX_ZOOM, s0 * factor))
    if abs(s1 - s0) < 1e-9:
        return False
    left0 = (vw - iw * s0) / 2 + view.pan_x            # 缩放前图片左上角
    top0 = (vh - ih * s0) / 2 + view.pan_y
    ix = (cx - left0) / s0                             # 锚点下的图片像素（图像坐标）
    iy = (cy - top0) / s0
    view.mode = "custom"
    view.zoom = s1
    view.pan_x = cx - ix * s1 - (vw - iw * s1) / 2     # 缩放后锚点像素仍位于光标处
    view.pan_y = cy - iy * s1 - (vh - ih * s1) / 2
    _clamp_pan(view, iw, ih, vw, vh, s1)
    return True


@ft.component
def ImageViewerApp():
    page = ft.context.page
    app = ft.use_ref(AppState())

    # ---- UI 镜像状态：统一由 sync_ui() 写入 ----
    img_src, set_img_src = ft.use_state(None)              # 当前图片 data URI
    img_wh, set_img_wh = ft.use_state((0, 0))              # 渲染后像素尺寸
    file_name, set_file_name = ft.use_state("")
    info_text, set_info_text = ft.use_state("")
    page_text, set_page_text = ft.use_state("")
    fullscreen, set_fullscreen = ft.use_state(False)
    slideshow_on, set_slideshow_on = ft.use_state(False)
    paused, set_paused = ft.use_state(False)
    pan_xy, set_pan_xy = ft.use_state((0.0, 0.0))
    show_delete, set_show_delete = ft.use_state(False)
    snack, set_snack = ft.use_state(None)

    picker_ref = ft.use_ref(None)                  # FilePicker 服务（首帧注册）
    if picker_ref.current is None:
        picker_ref.current = ft.FilePicker()
    dispatch_ref = ft.use_ref(None)             # 键盘分发（每帧刷新，闭包不陈旧）
    resize_ref = ft.use_ref(None)               # 窗口尺寸分发
    kb_listener_ref = ft.use_ref(None)          # KeyboardListener 实例（用于重聚焦）

    # ---------- 状态同步 ----------

    def sync_ui() -> None:
        """把 AppState 最新值同步到 UI 镜像状态，驱动一次组件重绘。"""
        st = app.current
        uri, w, h = st.manager.get_display()
        set_img_src(uri)
        set_img_wh((w, h))
        info = st.manager.info()
        set_file_name(info["name"])
        set_info_text(
            f"{info['w']}×{info['h']} · {format_size(info['size'])}" if info["total"] else ""
        )
        set_page_text(f"{info['pos']} / {info['total']}" if info["total"] else "")
        set_fullscreen(st.fullscreen)
        set_slideshow_on(st.slideshow_on)
        set_paused(st.paused)
        # 计算当前缩放并夹紧平移边界
        scale = _compute_scale(st.view, w, h, *st.viewport)
        _clamp_pan(st.view, w, h, *st.viewport, scale)
        set_pan_xy((st.view.pan_x, st.view.pan_y))

    def toast(message: str, seconds: float = 2.5) -> None:
        """底部轻提示（SnackBar）。"""
        set_snack(ft.SnackBar(
            content=ft.Text(message, color=FG_COLOR),
            bgcolor=PANEL_COLOR,
            duration=ft.Duration(seconds=seconds),
        ))

        async def _clear() -> None:
            await asyncio.sleep(seconds)
            set_snack(None)

        page.run_task(_clear)

    # ---------- 图片加载 ----------

    def _decode_worker(st: AppState) -> None:
        """后台线程：定位首张有效图片 -> 编码当前图 -> 预加载前后各一张。"""
        if not st.manager.files:
            return
        st.manager.ensure_valid()
        st.manager.get_display()
        st.manager.preload()

    async def _load_current_async(st: AppState) -> None:
        """异步加载当前图片：后台解码/编码，完成后同步 UI。"""
        st.gen += 1
        gen = st.gen
        await asyncio.get_running_loop().run_in_executor(None, _decode_worker, st)
        if gen != st.gen:
            return                              # 已被更新的请求取代
        sync_ui()
        if st.manager.get_display()[0] is None:
            toast("无法打开图片，已跳过")

    async def _goto(delta: int) -> None:
        """切换图片（首尾循环、自动跳过坏图），异步解码。"""
        st = app.current
        if st.manager.navigate(delta):
            await _load_current_async(st)
        elif st.manager.files and st.manager.get_display()[0] is None:
            toast("没有可显示的图片")
            sync_ui()

    # ---------- 动作（快捷键 / 菜单 / 鼠标共用） ----------

    async def act_open_file(_e=None) -> None:
        try:
            files = await picker_ref.current.pick_files(
                dialog_title="打开图片",
                file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=["jpg", "jpeg", "png", "bmp", "gif", "webp", "tif", "tiff"],
                allow_multiple=False,
            )
        except Exception as exc:
            toast(f"打开文件失败：{exc}")
            return
        if not files:
            return
        st = app.current
        if not st.manager.open_path(files[0].path):
            toast("无法打开该文件（不支持或已损坏）")
            return
        await _load_current_async(st)

    async def act_open_folder(_e=None) -> None:
        try:
            folder = await picker_ref.current.get_directory_path(dialog_title="打开文件夹")
        except Exception as exc:
            toast(f"打开文件夹失败：{exc}")
            return
        if not folder:
            return
        st = app.current
        if st.manager.scan_folder(folder) == 0:
            toast("该文件夹中没有可显示的图片")
            sync_ui()
            return
        await _load_current_async(st)

    def act_zoom_in(_e=None) -> None:
        st = app.current
        st.view.mode = "custom"
        st.view.zoom = min(MAX_ZOOM, st.view.zoom * ZOOM_STEP)
        sync_ui()

    def act_zoom_out(_e=None) -> None:
        st = app.current
        st.view.mode = "custom"
        st.view.zoom = max(MIN_ZOOM, st.view.zoom / ZOOM_STEP)
        sync_ui()

    def act_fit_window(_e=None) -> None:
        app.current.view.reset()
        sync_ui()

    def act_fit_width(_e=None) -> None:
        st = app.current
        st.view.mode = "fit_width"
        st.view.pan_x = st.view.pan_y = 0.0
        sync_ui()

    def act_actual_size(_e=None) -> None:
        st = app.current
        st.view.mode = "actual"
        st.view.pan_x = st.view.pan_y = 0.0
        sync_ui()

    async def act_rotate_cw(_e=None) -> None:
        app.current.manager.rotate_cw()
        await _load_current_async(app.current)

    async def act_rotate_ccw(_e=None) -> None:
        app.current.manager.rotate_ccw()
        await _load_current_async(app.current)

    async def act_flip_h(_e=None) -> None:
        app.current.manager.flip_horizontal()
        await _load_current_async(app.current)

    async def act_flip_v(_e=None) -> None:
        app.current.manager.flip_vertical()
        await _load_current_async(app.current)

    async def act_fullscreen(_e=None) -> None:
        st = app.current
        st.fullscreen = not st.fullscreen
        page.window.full_screen = st.fullscreen
        _refresh_viewport()
        page.update()
        sync_ui()

    def act_slideshow(_e=None) -> None:
        st = app.current
        st.slideshow_on = not st.slideshow_on
        if st.slideshow_on:
            st.paused = False
        sync_ui()

    def act_pause(_e=None) -> None:
        st = app.current
        if st.slideshow_on:
            st.paused = not st.paused
            sync_ui()

    def set_interval(seconds: float) -> None:
        app.current.interval = seconds
        sync_ui()

    def act_delete(_e=None) -> None:
        if app.current.manager.files:
            set_show_delete(True)

    async def confirm_delete(_e=None) -> None:
        set_show_delete(False)
        st = app.current
        try:
            removed = st.manager.delete_current()
        except Exception as exc:
            toast(f"删除失败：{exc}")
            sync_ui()
            return
        sync_ui()
        toast(f"已删除：{removed}")

    async def act_esc(_e=None) -> None:
        st = app.current
        if st.fullscreen:
            st.fullscreen = False
            page.window.full_screen = False
            _refresh_viewport()
            page.update()
            sync_ui()

    async def act_exit(_e=None) -> None:
        await page.window.close()

    # ---------- 键盘 / 窗口事件 ----------

    ACTIONS: dict[str, object] = {
        "next": lambda: _goto(1),
        "prev": lambda: _goto(-1),
        "zoom_in": lambda: act_zoom_in(),
        "zoom_out": lambda: act_zoom_out(),
        "fullscreen": lambda: act_fullscreen(),
        "rotate_cw": lambda: act_rotate_cw(),
        "rotate_ccw": lambda: act_rotate_ccw(),
        "flip_h": lambda: act_flip_h(),
        "flip_v": lambda: act_flip_v(),
        "slideshow": lambda: act_slideshow(),
        "pause": lambda: act_pause(),
        "open_file": lambda: act_open_file(),
        "open_folder": lambda: act_open_folder(),
        "fit_window": lambda: act_fit_window(),
        "actual_size": lambda: act_actual_size(),
        "delete": lambda: act_delete(),
        "esc": lambda: act_esc(),
        "exit": lambda: act_exit(),
    }

    async def _run_action(action: str) -> None:
        result = ACTIONS[action]()
        if asyncio.iscoroutine(result):
            await result

    def _on_keyboard(e: ft.KeyboardEvent) -> None:
        """页面级键盘分发：快捷键匹配。

        Ctrl 键状态由 KeyboardListener 的 on_key_down/on_key_up 维护，
        此处不再自行推断（0.86.x 的页面级事件收不到修饰键本身）。
        """
        action = match(e)
        if action:
            page.run_task(_run_action, action)

    # ---------- 修饰键跟踪（KeyboardListener） ----------

    def _on_key_down(e) -> None:
        """Ctrl 按下：标记 Ctrl 状态（滚轮缩放判断用）。"""
        if "control" in (e.key or "").lower():
            app.current.ctrl_held = True

    def _on_key_up(e) -> None:
        """Ctrl 抬起：清除 Ctrl 状态。"""
        if "control" in (e.key or "").lower():
            app.current.ctrl_held = False

    def _on_tap_down(_e) -> None:
        """点击图片区时把键盘焦点还给 KeyboardListener，恢复修饰键跟踪。"""
        listener = kb_listener_ref.current
        if listener is not None:
            page.run_task(listener.focus)

    def _on_resize(e: ft.PageResizeEvent) -> None:
        set_initial_size(e.width, e.height)   # 同步 INITIAL_SIZE，布局尺寸以此为准
        _refresh_viewport(e.width, e.height)
        sync_ui()

    def _refresh_viewport(width: float | None = None, height: float | None = None) -> None:
        """按客户端尺寸（减去菜单/状态栏）更新图片区可视尺寸。

        尺寸优先级：resize 事件 > 启动时捕获的 INITIAL_SIZE > page.width/height。
        0.86.x 中 page.width/height 在窗口尺寸变化后可能陈旧，需以事件值为准。
        """
        st = app.current
        vw = width if width is not None else (
            INITIAL_SIZE[0] if INITIAL_SIZE else (page.width or 900)
        )
        vh = height if height is not None else (
            INITIAL_SIZE[1] if INITIAL_SIZE else (page.height or 600)
        )
        if not st.fullscreen:
            vh -= MENU_H + STATUS_H
        st.viewport = (max(vw, 80), max(vh, 80))

    def _install_handlers() -> None:
        """仅挂载一次：页面级键盘与窗口尺寸事件。"""
        page.on_keyboard_event = lambda e: dispatch_ref.current(e)
        page.on_resize = lambda e: resize_ref.current(e)
        page.update()
        # 启动后延迟重校准：page.width/height 在首个 resize 事件后才准确
        page.run_task(_calibrate_viewport)

    async def _calibrate_viewport() -> None:
        """等待初始 resize 事件更新 INITIAL_SIZE 后，重算视口并同步 UI。

        0.86.x 的 page.width/height 在窗口建立后可能长时间保持陈旧值，
        必须等到 resize 事件携带的真实客户端尺寸。
        """
        last: tuple[float, float] | None = None
        for _ in range(15):
            await asyncio.sleep(0.5)
            if INITIAL_SIZE and INITIAL_SIZE != last:
                last = INITIAL_SIZE
                _refresh_viewport()
                sync_ui()

    dispatch_ref.current = _on_keyboard
    resize_ref.current = _on_resize
    ft.use_effect(_install_handlers, [])

    # 启动参数：自动打开指定文件/文件夹
    def _startup_open() -> None:
        if START_PATH:
            page.run_task(_open_startup_path, START_PATH)

    async def _open_startup_path(path: str) -> None:
        st = app.current
        p = Path(path)
        if p.is_dir():
            if st.manager.scan_folder(p) == 0:
                toast("该文件夹中没有可显示的图片")
                sync_ui()
                return
        elif not st.manager.open_path(p):
            toast("无法打开该文件（不支持或已损坏）")
            sync_ui()
            return
        await _load_current_async(st)

    ft.use_effect(_startup_open, [])

    # ---------- 幻灯片 ----------

    def _slideshow_effect() -> None:
        st = app.current
        if st.slideshow_on and (st.slideshow_task is None or st.slideshow_task.done()):
            st.slideshow_task = page.run_task(_slideshow_loop)
        elif not st.slideshow_on and st.slideshow_task is not None:
            st.slideshow_task.cancel()
            st.slideshow_task = None

    ft.use_effect(_slideshow_effect, [app.current.slideshow_on])

    async def _slideshow_loop() -> None:
        st = app.current
        try:
            while st.slideshow_on:
                await asyncio.sleep(st.interval)
                if st.paused:
                    continue
                await _goto(1)
        except asyncio.CancelledError:
            pass
        finally:
            st.slideshow_task = None

    # ---------- 鼠标交互 ----------

    def _on_scroll(e: ft.ScrollEvent) -> None:
        """滚轮三段式：
        1. Ctrl+滚轮：以鼠标位置为锚点缩放（向上放大、向下缩小）；
        2. 图片完全可见：向上翻上一张、向下翻下一张；
        3. 图片溢出视口：向上/向下滚动浏览图片区域（到边界即停，由 clamp 保证）。
        Windows 实测滚轮上滚的 scroll_delta.y 为负值，故 dy<0 视为向上。
        """
        st = app.current
        dy = e.scroll_delta.y
        if dy == 0:
            return
        ctrl = st.ctrl_held or _ctrl_pressed()   # 修饰键状态以系统查询为准
        w, h = img_wh
        vw, vh = st.viewport
        scale = _compute_scale(st.view, w, h, vw, vh)
        if ctrl:
            # Ctrl+滚轮：基于鼠标位置缩放
            loc = e.local_position
            cx = loc.x if loc is not None else vw / 2
            cy = loc.y if loc is not None else vh / 2
            _zoom_anchored(st.view, w, h, vw, vh, cx, cy,
                           ZOOM_STEP if dy < 0 else 1 / ZOOM_STEP)
            set_pan_xy((st.view.pan_x, st.view.pan_y))
            sync_ui()
        elif w * scale <= vw and h * scale <= vh:
            # 图片完全在视野中：翻页（向上=上一张，向下=下一张）
            page.run_task(_goto, -1 if dy < 0 else 1)
        else:
            # 图片溢出：滚动浏览（步长随滚轮幅度，边界处自动停止）
            step = max(60.0, vh * 0.12) * max(1.0, abs(dy) / 100.0)
            st.view.pan_y += step if dy < 0 else -step
            _clamp_pan(st.view, w, h, vw, vh, scale)
            set_pan_xy((st.view.pan_x, st.view.pan_y))

    def _on_pan_update(e: ft.DragUpdateEvent) -> None:
        """按住左键拖动平移（带边界限制）。"""
        st = app.current
        delta = e.global_delta
        if delta is None:
            return
        st.view.pan_x += delta.x
        st.view.pan_y += delta.y
        w, h = img_wh
        scale = _compute_scale(st.view, w, h, *st.viewport)
        _clamp_pan(st.view, w, h, *st.viewport, scale)
        set_pan_xy((st.view.pan_x, st.view.pan_y))

    def _on_double_tap(_e) -> None:
        """双击：切换全屏（全屏下双击即退出）。"""
        page.run_task(act_fullscreen)

    # ---------- 界面构建 ----------

    def _image_area() -> ft.Control:
        """图片浏览区：无图时显示占位提示。"""
        st = app.current
        if img_src is None:
            return ft.Container(
                expand=True,
                bgcolor=BG_COLOR,
                alignment=ft.Alignment.CENTER,
                content=ft.Column(
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=12,
                    controls=[
                        ft.Icon(ft.Icons.IMAGE, size=56, color=FG_DIM),
                        ft.Text("Ctrl+O 打开图片　·　Ctrl+Shift+O 打开文件夹", size=13, color=FG_DIM),
                    ],
                ),
            )
        vw, vh = st.viewport
        w, h = img_wh
        scale = _compute_scale(st.view, w, h, vw, vh)
        disp_w = max(w * scale, 1.0)
        disp_h = max(h * scale, 1.0)
        pan_x, pan_y = pan_xy
        left = (vw - disp_w) / 2 + pan_x
        top = (vh - disp_h) / 2 + pan_y
        return ft.Stack(
            expand=True,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            controls=[
                ft.Container(expand=True, bgcolor=BG_COLOR),
                ft.Image(
                    src=img_src,
                    width=disp_w,
                    height=disp_h,
                    fit=ft.BoxFit.FILL,
                    left=left,
                    top=top,
                    gapless_playback=True,      # 切换图片时无闪烁
                ),
            ],
        )

    def _menu_item(text: str, action, icon: ft.IconData | None = None, shortcut: str = "") -> ft.MenuItemButton:
        return ft.MenuItemButton(
            content=ft.Text(f"{text}  {shortcut}" if shortcut else text, size=13),
            leading=ft.Icon(icon, size=16) if icon else None,
            on_click=action,
        )

    file_menu = ft.SubmenuButton(
        content=ft.Text("文件", size=13),
        controls=[
            _menu_item("打开图片", act_open_file, ft.Icons.IMAGE, "Ctrl+O"),
            _menu_item("打开文件夹", act_open_folder, ft.Icons.FOLDER_OPEN, "Ctrl+Shift+O"),
            _menu_item("停止播放" if slideshow_on else "幻灯片播放", act_slideshow, ft.Icons.SLIDESHOW, "S"),
            _menu_item("继续" if paused else "暂停", act_pause, ft.Icons.PAUSE_CIRCLE, "P"),
            ft.SubmenuButton(
                content=ft.Text("播放间隔", size=13),
                controls=[
                    _menu_item(f"{sec} 秒", lambda _e, s=sec: set_interval(s))
                    for sec in SLIDESHOW_INTERVALS
                ],
            ),
            _menu_item("退出", act_exit, ft.Icons.CLOSE),
        ],
    )

    edit_menu = ft.SubmenuButton(
        content=ft.Text("编辑", size=13),
        controls=[
            _menu_item("顺时针旋转 90°", act_rotate_cw, ft.Icons.ROTATE_90_DEGREES_CW, "R"),
            _menu_item("逆时针旋转 90°", act_rotate_ccw, ft.Icons.ROTATE_90_DEGREES_CCW, "L"),
            _menu_item("水平翻转", act_flip_h, ft.Icons.FLIP, "H"),
            _menu_item("垂直翻转", act_flip_v, ft.Icons.FLIP, "V"),
            _menu_item("删除当前图片", act_delete, ft.Icons.DELETE_OUTLINE, "Del"),
        ],
    )

    view_menu = ft.SubmenuButton(
        content=ft.Text("视图", size=13),
        controls=[
            _menu_item("适应窗口", act_fit_window, ft.Icons.FIT_SCREEN, "Ctrl+0"),
            _menu_item("适应宽度", act_fit_width, ft.Icons.WIDTH_WIDE),
            _menu_item("1:1 原始大小", act_actual_size, ft.Icons.PHOTO_SIZE_SELECT_ACTUAL, "Ctrl+1"),
            _menu_item("放大", act_zoom_in, ft.Icons.ZOOM_IN, "+"),
            _menu_item("缩小", act_zoom_out, ft.Icons.ZOOM_OUT, "-"),
            _menu_item("全屏", act_fullscreen, ft.Icons.FULLSCREEN, "F"),
        ],
    )

    # 删除确认弹窗
    ft.use_dialog(
        ft.AlertDialog(
            modal=False,
            title=ft.Text("删除图片"),
            content=ft.Text(f"确定将「{file_name}」移至回收站吗？"),
            actions=[
                ft.Button("删除", on_click=confirm_delete),
                ft.TextButton("取消", on_click=lambda _e: set_show_delete(False)),
            ],
            on_dismiss=lambda _e: set_show_delete(False),
        )
        if show_delete
        else None
    )
    ft.use_dialog(snack)

    # 注意：0.86.x 中 Column 内 expand 子控件之后的固定高度兄弟控件不渲染，
    # 因此整体采用 Stack 绝对定位布局（菜单/状态栏/图片区互不干扰）。
    def _menu_bar() -> ft.Container:
        return ft.Container(
            height=MENU_H,
            bgcolor=PANEL_COLOR,
            border=ft.Border(bottom=ft.BorderSide(1, PANEL_BORDER)),
            alignment=ft.Alignment.CENTER_LEFT,
            padding=ft.Padding.symmetric(horizontal=4),
            content=ft.MenuBar(controls=[file_menu, edit_menu, view_menu]),
        )

    def _status_bar() -> ft.Container:
        return ft.Container(
            height=STATUS_H,
            bgcolor=PANEL_COLOR,
            border=ft.Border(top=ft.BorderSide(1, PANEL_BORDER)),
            padding=ft.Padding.symmetric(horizontal=12),
            content=ft.Row(
                expand=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Text(file_name, size=12, color=FG_COLOR, expand=1,
                            max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                    ft.Text(info_text, size=12, color=FG_DIM, expand=1,
                            text_align=ft.TextAlign.CENTER),
                    ft.Text(page_text, size=12, color=FG_COLOR, expand=1,
                            text_align=ft.TextAlign.END),
                ],
            ),
        )

    # 布局说明：0.86.x 的 expand 与 Stack 定位存在缺陷（expand 子控件会吞掉
    # 后续兄弟的空间、Stack 的 bottom/right 定位失效），因此这里全部使用
    # 显式高度：菜单 48 + 图片区(客户端高度-80) + 状态栏 32，总和恰为窗口高度。
    ph = (INITIAL_SIZE[1] if INITIAL_SIZE else (page.height or 600)) or 600
    image_h = ph if fullscreen else max(ph - MENU_H - STATUS_H, 80)
    image_view = ft.GestureDetector(
        height=image_h,
        expand=fullscreen,                  # 全屏时占满整窗
        drag_interval=16,                   # 节流拖动事件，保证流畅
        on_tap_down=_on_tap_down,           # 点击时恢复键盘焦点
        on_scroll=_on_scroll,
        on_pan_update=_on_pan_update,
        on_double_tap=_on_double_tap,
        content=_image_area(),
    )
    controls = [image_view] if fullscreen else [_menu_bar(), image_view, _status_bar()]
    root = ft.Column(expand=True, spacing=0, controls=controls)
    # 用 KeyboardListener 接收 Ctrl 键按下/抬起（焦点丢失时由 _ctrl_pressed 兜底）
    kb_listener_ref.current = ft.KeyboardListener(
        content=root,
        autofocus=True,
        on_key_down=_on_key_down,
        on_key_up=_on_key_up,
    )
    return kb_listener_ref.current
