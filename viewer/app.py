"""LiteView 看图 - 主界面（声明式 Flet 组件）。

结构：
    ImageViewerApp  根组件：顶部菜单栏 + 图片浏览区 + 底部状态栏
    AppState        全部可变状态，保存在 use_ref 中，事件回调直接读写
    sync_ui()       状态 -> UI 镜像（use_state）的统一同步入口，驱动组件重绘
"""
from __future__ import annotations

import asyncio
import ctypes
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import flet as ft

from .image_manager import IMAGE_EXTS, ImageManager, format_size
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
    ctrl_held: bool = False                     # Ctrl 是否按住（KeyboardListener 跟踪）
    shift_held: bool = False                    # Shift 是否按住（KeyboardListener 跟踪）
    viewport: tuple[float, float] = (900.0, 560.0)   # 图片区可视尺寸
    gen: int = 0                                # 异步解码代际计数，防止旧任务覆盖新状态
    slideshow_task: asyncio.Future | None = None
    sel_rect: tuple[float, float, float, float] | None = None   # 左键拖拽选区（视口坐标 x0,y0,x1,y1）
    sel_drag_start: tuple[float, float] | None = None          # 选区拖拽起点（本地坐标）
    right_last: tuple[float, float] | None = None              # 右键拖拽上一位置（本地坐标）
    anim_playing: bool = False                                 # 动图播放中
    anim_paused: bool = False                                  # 动图暂停
    anim_task: asyncio.Future | None = None                    # 动图播放任务


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
    anim_playing, set_anim_playing = ft.use_state(False)
    anim_paused, set_anim_paused = ft.use_state(False)
    pan_xy, set_pan_xy = ft.use_state((0.0, 0.0))
    sel_rect_state, set_sel_rect_state = ft.use_state(None)   # 选区矩形（视口坐标）
    _, set_render_tick = ft.use_state(0)   # 渲染计数：强制视图状态变化后重绘
    show_delete, set_show_delete = ft.use_state(False)
    snack, set_snack = ft.use_state(None)
    assoc_action, set_assoc_action = ft.use_state(None)
    assoc_status, set_assoc_status = ft.use_state(None)

    picker_ref = ft.use_ref(None)                  # FilePicker 服务（首帧注册）
    if picker_ref.current is None:
        picker_ref.current = ft.FilePicker()
    dispatch_ref = ft.use_ref(None)             # 键盘分发（每帧刷新，闭包不陈旧）
    resize_ref = ft.use_ref(None)               # 窗口尺寸分发
    kb_listener_ref = ft.use_ref(None)          # KeyboardListener 实例（用于重聚焦）

    # ---------- 状态同步 ----------

    def sync_ui() -> None:
        """把 AppState 最新值同步到 UI 镜像状态，驱动一次组件重绘。"""
        try:
            _sync_ui_body()
        except RuntimeError:
            pass   # 窗口关闭、会话销毁后的迟到同步直接忽略

    def _sync_ui_body() -> None:
        st = app.current
        if st.sel_rect is not None:           # 视图变化后选区失效，统一清除
            st.sel_rect = None
            set_sel_rect_state(None)
        uri, w, h = st.manager.get_display()
        set_img_src(uri)
        set_img_wh((w, h))
        info = st.manager.info()
        info_str = f"{info['w']}×{info['h']} · {format_size(info['size'])}"
        if info["frames"] > 1:
            info_str += f" · 动图 {st.manager.current_frame + 1}/{info['frames']}帧"
        set_file_name(info["name"])
        set_info_text(info_str if info["total"] else "")
        set_fullscreen(st.fullscreen)
        set_slideshow_on(st.slideshow_on)
        set_paused(st.paused)
        set_anim_playing(st.anim_playing)
        set_anim_paused(st.anim_paused)
        # 计算当前缩放并夹紧平移边界
        scale = _compute_scale(st.view, w, h, *st.viewport)
        _clamp_pan(st.view, w, h, *st.viewport, scale)
        set_pan_xy((st.view.pan_x, st.view.pan_y))
        # 状态栏实时缩放百分比（像素级查看时确认当前倍率）
        if st.view.mode == "fit":
            zoom_label = f"{scale * 100:.0f}% 适应"
        elif st.view.mode == "fit_width":
            zoom_label = f"{scale * 100:.0f}% 适宽"
        elif st.view.mode == "actual":
            zoom_label = "100%"
        else:
            zoom_label = f"{st.view.zoom * 100:.0f}%"
        set_page_text(
            f"{info['pos']} / {info['total']} · {zoom_label}" if info["total"] else ""
        )
        # 0.86.x 的 use_state 做浅比较：纯视图操作（适应窗口/1:1/缩放）的镜像
        # 状态可能全部相同而跳过重绘，这里用递增计数强制渲染一次
        set_render_tick(lambda t: t + 1)

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
            reason = st.manager.last_error
            hint = f"（{reason}）" if reason and len(reason) < 80 else ""
            toast(f"无法打开图片{hint}，已跳过")
        # 动图自动播放 / 静态图停止播放
        n = st.manager.frame_count()
        if (n > 1) != st.anim_playing:
            st.anim_playing = n > 1
            st.anim_paused = False
            sync_ui()

    async def _goto(delta: int) -> None:
        """切换图片（首尾循环、自动跳过坏图），异步解码。"""
        st = app.current
        if st.manager.navigate(delta):
            await _load_current_async(st)
        elif st.manager.files and st.manager.get_display()[0] is None:
            toast("没有可显示的图片")
            sync_ui()

    # ---------- 动图播放 ----------

    async def _refresh_frame_async() -> None:
        """后台重编码当前帧并同步 UI（动图逐帧显示）。"""
        st = app.current
        await asyncio.get_running_loop().run_in_executor(None, st.manager.get_display)
        sync_ui()

    async def _anim_loop() -> None:
        """动图播放循环：按每帧时长推进。"""
        st = app.current
        try:
            while st.anim_playing:
                await asyncio.sleep(st.manager.frame_duration() / 1000)
                if st.anim_paused or not st.anim_playing:
                    continue
                st.manager.next_frame()
                await _refresh_frame_async()
        except asyncio.CancelledError:
            pass
        finally:
            st.anim_task = None

    def _anim_effect() -> None:
        """动图播放任务的生命周期管理（随 anim_playing 启停）。"""
        st = app.current
        if st.anim_playing and (st.anim_task is None or st.anim_task.done()):
            st.anim_task = page.run_task(_anim_loop)
        elif not st.anim_playing and st.anim_task is not None:
            st.anim_task.cancel()
            st.anim_task = None

    ft.use_effect(_anim_effect, [app.current.anim_playing])

    # ---------- 动作（快捷键 / 菜单 / 鼠标共用） ----------

    async def act_open_file(_e=None) -> None:
        try:
            files = await picker_ref.current.pick_files(
                dialog_title="打开图片",
                file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=sorted(ext.lstrip(".") for ext in IMAGE_EXTS),
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

    # ---------- 动图控制 / 导出 ----------

    def act_anim_toggle(_e=None) -> None:
        """动图播放 / 暂停 / 继续。"""
        st = app.current
        if st.manager.frame_count() <= 1:
            return
        if st.anim_playing and st.anim_paused:
            st.anim_paused = False            # 继续
        elif st.anim_playing:
            st.anim_paused = True             # 暂停
        else:
            st.anim_playing = True            # 播放
        sync_ui()

    def _step_frame(delta: int) -> None:
        """动图逐帧步进（自动暂停播放）。"""
        st = app.current
        if st.manager.frame_count() <= 1:
            return
        st.anim_paused = True
        st.manager.next_frame(delta)
        page.run_task(_refresh_frame_async)

    def act_frame_prev(_e=None) -> None:
        """上一帧。"""
        _step_frame(-1)

    def act_frame_next(_e=None) -> None:
        """下一帧。"""
        _step_frame(1)

    async def act_export_frame(_e=None) -> None:
        """导出当前帧（含旋转/翻转）为 PNG。"""
        st = app.current
        if not st.manager.files:
            return
        name = Path(st.manager.info()["name"]).stem
        try:
            path = await picker_ref.current.save_file(
                dialog_title="导出当前帧",
                file_name=f"{name}_frame{st.manager.current_frame + 1:03d}.png",
                allowed_extensions=["png"],
                file_type=ft.FilePickerFileType.IMAGE,
            )
        except Exception as exc:
            toast(f"导出失败：{exc}")
            return
        if not path:
            return
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: st.manager.save_current_frame(path)
            )
            toast(f"已导出：{Path(path).name}")
        except Exception as exc:
            toast(f"导出失败：{exc}")

    def act_file_assoc_install(_e=None) -> None:
        set_assoc_action("install")

    def act_file_assoc_uninstall(_e=None) -> None:
        set_assoc_action("uninstall")

    def act_file_assoc_status(_e=None) -> None:
        page.run_task(_load_file_assoc_status)

    async def _load_file_assoc_status() -> None:
        if os.name != "nt":
            toast("文件关联仅适用于 Windows")
            return

        def read_status() -> str:
            from . import file_assoc

            return file_assoc.status_text()

        try:
            status_text = await asyncio.get_running_loop().run_in_executor(
                None, read_status
            )
        except Exception as exc:
            toast(f"读取文件关联状态失败：{exc}")
            return
        set_assoc_status(status_text)

    async def _run_file_assoc(action: str) -> None:
        set_assoc_action(None)
        if os.name != "nt":
            toast("文件关联仅适用于 Windows")
            return

        def operate() -> None:
            from . import file_assoc

            buffer = ctypes.create_unicode_buffer(32768)
            length = ctypes.windll.kernel32.GetModuleFileNameW(
                None, buffer, len(buffer)
            )
            if not length:
                raise RuntimeError("无法获取当前程序 exe 路径")
            exe = Path(buffer.value).resolve()
            if exe.suffix.lower() != ".exe" or not exe.is_file():
                raise RuntimeError(f"当前程序不是有效的 exe：{exe}")
            if action == "install":
                file_assoc.install(exe, verbose=False)
            else:
                file_assoc.uninstall(verbose=False)

        try:
            await asyncio.get_running_loop().run_in_executor(None, operate)
        except (Exception, SystemExit) as exc:
            message = str(exc) or exc.__class__.__name__
            toast(f"文件关联设置失败：{message}")
            return
        toast("已注册 LiteView 文件关联" if action == "install" else "已取消 LiteView 文件关联")

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
        if st.sel_rect is not None:           # Esc：取消选区
            _clear_selection()
            return
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
        "anim_toggle": lambda: act_anim_toggle(),
        "frame_prev": lambda: act_frame_prev(),
        "frame_next": lambda: act_frame_next(),
        "export_frame": lambda: act_export_frame(),
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

        0.86.x 客户端在组合键事件上报告的 ctrl/shift 标志不可靠（实测恒为
        False），因此以 KeyboardListener 跟踪的按键状态 + 系统键查询为准。
        """
        st = app.current
        if not _ctrl_pressed() and not e.ctrl:
            st.ctrl_held = False              # 自愈：系统确认 Ctrl 已松开
        ctrl = e.ctrl or st.ctrl_held or _ctrl_pressed()
        shift = e.shift or st.shift_held
        action = match(e, ctrl=ctrl, shift=shift)
        if action:
            page.run_task(_run_action, action)

    # ---------- 修饰键跟踪（KeyboardListener） ----------

    def _on_key_down(e) -> None:
        """Ctrl/Shift 按下：记录修饰键状态（滚轮缩放与组合键判断用）。"""
        key = (e.key or "").lower()
        if "control" in key:
            app.current.ctrl_held = True
        elif "shift" in key:
            app.current.shift_held = True

    def _on_key_up(e) -> None:
        """Ctrl/Shift 抬起：清除修饰键状态。"""
        key = (e.key or "").lower()
        if "control" in key:
            app.current.ctrl_held = False
        elif "shift" in key:
            app.current.shift_held = False

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
            _clear_selection()
            step = max(60.0, vh * 0.12) * max(1.0, abs(dy) / 100.0)
            st.view.pan_y += step if dy < 0 else -step
            _clamp_pan(st.view, w, h, vw, vh, scale)
            set_pan_xy((st.view.pan_x, st.view.pan_y))

    # ---------- 鼠标拖拽：左键选区 / 右键平移 ----------

    def _clear_selection() -> None:
        """清除选区（同步 AppState 与 UI 状态）。"""
        app.current.sel_rect = None
        set_sel_rect_state(None)

    def _zoom_to_region(sel: tuple[float, float, float, float]) -> None:
        """把选区对应的图片区域放大到填满视野（保持宽高比）。"""
        st = app.current
        w, h = img_wh
        vw, vh = st.viewport
        if w <= 0 or vw <= 0:
            return
        x0, y0, x1, y1 = sel
        left, top = min(x0, x1), min(y0, y1)
        sw, sh = abs(x1 - x0), abs(y1 - y0)
        if sw < 4 or sh < 4:                  # 忽略过小的“选区”
            return
        s0 = _compute_scale(st.view, w, h, vw, vh)
        img_left = (vw - w * s0) / 2 + st.view.pan_x   # 当前图片左上角（视口坐标）
        img_top = (vh - h * s0) / 2 + st.view.pan_y
        # 选区对应的图片像素区域
        ix0 = (left - img_left) / s0
        iy0 = (top - img_top) / s0
        iw_sel, ih_sel = sw / s0, sh / s0
        s1 = max(MIN_ZOOM, min(MAX_ZOOM, min(vw / iw_sel, vh / ih_sel)))
        st.view.mode = "custom"
        st.view.zoom = s1
        # 选区中心对准视野中心
        cx, cy = ix0 + iw_sel / 2, iy0 + ih_sel / 2
        st.view.pan_x = vw / 2 - cx * s1 - (vw - w * s1) / 2
        st.view.pan_y = vh / 2 - cy * s1 - (vh - h * s1) / 2
        _clamp_pan(st.view, w, h, vw, vh, s1)
        _clear_selection()
        set_pan_xy((st.view.pan_x, st.view.pan_y))
        sync_ui()

    def _on_tap(e: ft.TapEvent) -> None:
        """单击：选区已存在且点击落在区内 -> 放大选区；否则取消选区。"""
        st = app.current
        sel = st.sel_rect
        if sel is None:
            return
        pos = e.local_position
        if pos is None:
            return
        x0, y0, x1, y1 = sel
        if min(x0, x1) <= pos.x <= max(x0, x1) and min(y0, y1) <= pos.y <= max(y0, y1):
            _zoom_to_region(sel)
        else:
            _clear_selection()

    def _on_pan_start(e: ft.DragStartEvent) -> None:
        """左键拖拽开始：创建新选区。"""
        st = app.current
        _clear_selection()
        if e.local_position is not None:
            st.sel_drag_start = (e.local_position.x, e.local_position.y)
            st.sel_rect = (e.local_position.x, e.local_position.y) * 2
            set_sel_rect_state(st.sel_rect)

    def _on_pan_update(e: ft.DragUpdateEvent) -> None:
        """左键拖拽中：更新选区矩形。"""
        st = app.current
        if st.sel_drag_start is None or e.local_position is None:
            return
        x0, y0 = st.sel_drag_start
        st.sel_rect = (x0, y0, e.local_position.x, e.local_position.y)
        set_sel_rect_state(st.sel_rect)

    def _on_pan_end(_e: ft.DragEndEvent) -> None:
        """左键拖拽结束：保留选区，等待区内单击放大。"""
        app.current.sel_drag_start = None

    def _on_right_pan_start(e) -> None:
        """右键拖拽开始：记录起点，平移前清除选区。"""
        st = app.current
        _clear_selection()
        st.right_last = (
            (e.local_position.x, e.local_position.y) if e.local_position is not None else None
        )

    def _on_right_pan_update(e) -> None:
        """右键拖拽：滚动图片（带边界限制）。"""
        st = app.current
        if e.local_position is None or st.right_last is None:
            return
        lx, ly = e.local_position.x, e.local_position.y
        st.view.pan_x += lx - st.right_last[0]
        st.view.pan_y += ly - st.right_last[1]
        st.right_last = (lx, ly)
        w, h = img_wh
        scale = _compute_scale(st.view, w, h, *st.viewport)
        _clamp_pan(st.view, w, h, *st.viewport, scale)
        set_pan_xy((st.view.pan_x, st.view.pan_y))

    def _on_right_pan_end(_e) -> None:
        app.current.right_last = None

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
        overlay = []
        sel = sel_rect_state
        if sel is not None:
            x0, y0, x1, y1 = sel
            overlay = [ft.Container(
                left=min(x0, x1),
                top=min(y0, y1),
                width=max(abs(x1 - x0), 1.0),
                height=max(abs(y1 - y0), 1.0),
                border=ft.Border.all(1.5, ft.Colors.BLUE_400),
                bgcolor=ft.Colors.WHITE_24,     # 半透明填充，提升选区可见性
            )]
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
                *overlay,
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
            ft.SubmenuButton(
                content=ft.Text("文件关联", size=13),
                controls=[
                    _menu_item("注册为图片打开方式", act_file_assoc_install, ft.Icons.LINK),
                    _menu_item("取消文件关联", act_file_assoc_uninstall, ft.Icons.LINK_OFF),
                    _menu_item("查看关联状态", act_file_assoc_status, ft.Icons.INFO_OUTLINE),
                ],
            ),
            _menu_item("停止播放" if slideshow_on else "幻灯片播放", act_slideshow, ft.Icons.SLIDESHOW, "S"),
            _menu_item("继续" if paused else "暂停", act_pause, ft.Icons.PAUSE_CIRCLE, "P"),
            ft.SubmenuButton(
                content=ft.Text("播放间隔", size=13),
                controls=[
                    _menu_item(f"{sec} 秒", lambda _e, s=sec: set_interval(s))
                    for sec in SLIDESHOW_INTERVALS
                ],
            ),
            _menu_item("导出当前帧…", act_export_frame, ft.Icons.SAVE_ALT, "Ctrl+E"),
            _menu_item("退出", act_exit, ft.Icons.CLOSE),
        ],
    )

    play_menu = ft.SubmenuButton(
        content=ft.Text("播放", size=13),
        controls=[
            _menu_item(
                "继续动画" if anim_playing and anim_paused
                else ("暂停动画" if anim_playing else "播放动画"),
                act_anim_toggle,
                ft.Icons.PAUSE_CIRCLE if anim_playing else ft.Icons.PLAY_ARROW,
                "A",
            ),
            _menu_item("上一帧", act_frame_prev, ft.Icons.SKIP_PREVIOUS, "["),
            _menu_item("下一帧", act_frame_next, ft.Icons.SKIP_NEXT, "]"),
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

    ft.use_dialog(
        ft.AlertDialog(
            modal=True,
            title=ft.Text("文件关联状态"),
            content=ft.Text(assoc_status or ""),
            actions=[
                ft.TextButton("关闭", on_click=lambda _e: set_assoc_status(None)),
            ],
        )
        if assoc_status is not None
        else None
    )

    ft.use_dialog(
        ft.AlertDialog(
            modal=True,
            title=ft.Text("文件关联"),
            content=ft.Text(
                "将 LiteView 注册到 Windows 图片文件的“打开方式”和右键菜单。"
                if assoc_action == "install"
                else "将撤销 LiteView 写入的文件关联和右键菜单。"
            ),
            actions=[
                ft.Button(
                    "注册" if assoc_action == "install" else "取消关联",
                    on_click=lambda _e: page.run_task(_run_file_assoc, assoc_action),
                ),
                ft.TextButton("取消", on_click=lambda _e: set_assoc_action(None)),
            ],
        )
        if assoc_action
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
            content=ft.MenuBar(controls=[file_menu, play_menu, edit_menu, view_menu]),
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
        on_tap=_on_tap,                     # 区内单击放大选区 / 区外单击取消
        on_pan_start=_on_pan_start,         # 左键拖拽：选区
        on_pan_update=_on_pan_update,
        on_pan_end=_on_pan_end,
        on_right_pan_start=_on_right_pan_start,   # 右键拖拽：滚动图片
        on_right_pan_update=_on_right_pan_update,
        on_right_pan_end=_on_right_pan_end,
        on_scroll=_on_scroll,
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
