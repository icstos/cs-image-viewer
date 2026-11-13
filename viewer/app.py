"""ImageViewer 主界面 —— 声明式组件，基于 :mod:`ui` 门面。

不再直接依赖底层框架包：控件、组件、设计令牌、几何计算全部经 ``ui`` 取得，
配色由 :mod:`viewer.design` 投影，界面构件在 :mod:`viewer.ui` 里分层装配。

组件内部只做四件事：

============ ==========================================================
状态         ``AppState``（``use_memo`` 钉住）+ 一组 ``use_state`` 镜像
同步         ``sync_ui()`` 是「状态 → 界面」的唯一入口
意图         菜单 / 快捷键 / 手势统一汇入一张 ``ACTIONS`` 表
生命周期     ``use_effect`` 管动图、幻灯片、启动打开与页面事件
============ ==========================================================

几何（倍率、平移、锚点缩放、框选放大、滚轮分派）在 ``ui.core.geometry`` 里是纯
函数，可脱离窗口复算；渲染在 ``ui.ImageViewport`` 里，二者共用同一套坐标约定。

进程模型：本模块只负责**一个窗口**。图片来自 ``set_manager_factory`` 注入的
`RemoteImageManager`（共享缓存）或 `ImageManager`（本地解码），接口完全一致。
"""

from __future__ import annotations

import asyncio
import ctypes
import os
from pathlib import Path
from typing import Callable

import ui

from . import design as D
from .image_manager import IMAGE_EXTS
from .shortcuts import match
from .state import (
    AppState,
    StatusSnapshot,
    format_service_info,
    format_status,
    set_manager_factory,
)
from .ui.bars import build_menu_bar, build_status_bar
from .ui.canvas import CanvasHandlers, build_canvas
from .ui.dialogs import confirm_dialog, info_dialog
from .ui.kit import empty_stage, toast
from .ui.menu import MenuContext, build_sections

__all__ = [
    "ACTIONS",
    "INITIAL_SIZE",
    "START_PATH",
    "ImageViewerApp",
    "set_initial_size",
    "set_manager_factory",
]

#: 启动时自动打开的文件/文件夹（由 main.py 注入）。
START_PATH: str | None = None
#: 初始客户端尺寸（main.py 在 render 前由 resize 事件捕获；0.86.x 的 page.height 陈旧）。
INITIAL_SIZE: tuple[float, float] | None = None


def set_initial_size(width: float, height: float) -> None:
    """main.py 注册的尺寸记录器；视口高度以 resize 事件为准。"""
    global INITIAL_SIZE
    INITIAL_SIZE = (width, height)


def _ctrl_pressed() -> bool:
    """系统级 Ctrl 状态（KeyboardListener 失焦时的兜底，Windows）。"""
    try:
        return bool(ctypes.windll.user32.GetAsyncKeyState(0x11) & 0x8000)
    except Exception:
        return False


def _image_extensions() -> list[str]:
    """文件对话框的后缀过滤表（不带点）；每次现算，避免模块级可变列表被外部改。"""
    return sorted(ext.lstrip(".") for ext in IMAGE_EXTS)


@ui.component
def ImageViewerApp():
    page = ui.context.page
    # use_memo 锁住同一份状态对象：组件每次重绘都复用它，不会丢掉异步任务句柄。
    app = ui.use_memo(lambda: AppState(), [])

    # ── 界面镜像：统一由 sync_ui() 写入 ─────────────────────────────────
    img_src, set_img_src = ui.use_state(None)
    image_size, set_image_size = ui.use_state(ui.ViewSize())
    view, set_view = ui.use_state(ui.ViewTransform())
    status, set_status = ui.use_state(StatusSnapshot())
    fullscreen, set_fullscreen = ui.use_state(False)
    slideshow_on, set_slideshow_on = ui.use_state(False)
    paused, set_paused = ui.use_state(False)
    anim_playing, set_anim_playing = ui.use_state(False)
    anim_paused, set_anim_paused = ui.use_state(False)
    selection, set_selection = ui.use_state(None)          # ui.ViewRect | None
    _, set_render_tick = ui.use_state(0)                    # 纯视图变化的强制重绘
    show_delete, set_show_delete = ui.use_state(False)
    assoc_action, set_assoc_action = ui.use_state(None)
    assoc_status, set_assoc_status = ui.use_state(None)
    service_info, set_service_info = ui.use_state(None)
    # 启动参数还在解码时不画占位，避免「占位 → 图片」闪一下。
    startup_done, set_startup_done = ui.use_state(START_PATH is None)

    dialog_ref = ui.use_ref(None)                 # ui.FileDialog（构造即注册到页面）
    if dialog_ref.current is None:
        dialog_ref.current = ui.FileDialog()
    dialog: ui.FileDialog = dialog_ref.current
    dispatch_ref = ui.use_ref(None)               # 键盘分发（每帧刷新，闭包不陈旧）
    resize_ref = ui.use_ref(None)
    kb_listener_ref = ui.use_ref(None)            # KeyboardListener（用于重聚焦）

    # ── 状态同步 ───────────────────────────────────────────────────────
    def sync_ui() -> None:
        try:
            _sync_ui_body()
        except RuntimeError:
            pass                                  # 窗口关闭后的迟到同步直接忽略

    def _sync_ui_body() -> None:
        if app.sel_rect is not None:               # 视图变化后选区失效
            app.sel_rect = None
            set_selection(None)
        uri, w, h = app.manager.get_display()
        set_img_src(uri)
        app.image_size = ui.ViewSize.of(w, h)
        set_image_size(app.image_size)
        # 倍率变了要重新夹平移边界；clamp_pan 返回新对象，原样返回表示无需变化。
        app.view = ui.clamp_pan(app.view, app.image_size, app.viewport)
        set_view(app.view)
        set_status(format_status(app.manager, app.view, app.image_size, app.viewport))
        set_fullscreen(app.fullscreen)
        set_slideshow_on(app.slideshow_on)
        set_paused(app.paused)
        set_anim_playing(app.anim_playing)
        set_anim_paused(app.anim_paused)
        # use_state 浅比较：纯视图操作可能让所有镜像都相同，用计数强制重绘一次。
        set_render_tick(lambda tick: tick + 1)

    def notify(message: str, *, tone: str = "info", seconds: float = 2.5) -> None:
        toast(page, message, tone=tone, seconds=seconds)

    # ── 图片加载 ───────────────────────────────────────────────────────
    def _decode_worker() -> None:
        if not app.manager.files:
            return
        app.manager.ensure_valid()
        app.manager.get_display()
        app.manager.preload()

    async def _load_current_async() -> None:
        app.gen += 1
        gen = app.gen
        await asyncio.get_running_loop().run_in_executor(None, _decode_worker)
        if gen != app.gen:
            return                                # 已被更新的请求取代
        sync_ui()
        if app.manager.get_display()[0] is None:
            reason = app.manager.last_error
            hint = f"（{reason}）" if reason and len(reason) < 80 else ""
            notify(f"无法打开图片{hint}，已跳过", tone="warning")
        frames = app.manager.frame_count()
        if (frames > 1) != app.anim_playing:
            app.anim_playing = frames > 1
            app.anim_paused = False
            sync_ui()
        if len(app.manager.files) == 1:
            page.run_task(_expand_folder_async, gen)

    async def _expand_folder_async(gen: int) -> None:
        """首帧之后再补全同目录列表：出图耗时与目录里有多少文件无关。"""
        loop = asyncio.get_running_loop()
        listing = await loop.run_in_executor(None, app.manager.folder_candidates)
        if gen != app.gen or not app.manager.apply_folder_listing(listing):
            return
        sync_ui()
        await loop.run_in_executor(None, app.manager.preload)

    async def _goto(delta: int) -> None:
        """切换图片（首尾循环、自动跳过坏图），异步解码。"""
        if app.manager.navigate(delta):
            await _load_current_async()
        elif app.manager.files and app.manager.get_display()[0] is None:
            notify("没有可显示的图片", tone="warning")
            sync_ui()

    def _nav_click(delta: int) -> None:
        """点击箭头后把焦点还给 KeyboardListener，否则 Ctrl+滚轮会失灵。"""
        page.run_task(_goto, delta)
        listener = kb_listener_ref.current
        if listener is not None:
            page.run_task(listener.focus)

    # ── 动图播放 ───────────────────────────────────────────────────────
    async def _refresh_frame_async() -> None:
        await asyncio.get_running_loop().run_in_executor(None, app.manager.get_display)
        sync_ui()

    async def _anim_loop() -> None:
        try:
            while app.anim_playing:
                await asyncio.sleep(app.manager.frame_duration() / 1000)
                if app.anim_paused or not app.anim_playing:
                    continue
                app.manager.next_frame()
                await _refresh_frame_async()
        except asyncio.CancelledError:
            pass
        finally:
            app.anim_task = None

    def _anim_effect() -> None:
        if app.anim_playing and (app.anim_task is None or app.anim_task.done()):
            app.anim_task = page.run_task(_anim_loop)
        elif not app.anim_playing and app.anim_task is not None:
            app.anim_task.cancel()
            app.anim_task = None

    ui.use_effect(_anim_effect, [anim_playing])

    # ── 动作（快捷键 / 菜单 / 鼠标共用） ────────────────────────────────
    async def act_open_file() -> None:
        try:
            files = await dialog.pick_files(
                title="打开图片", extensions=_image_extensions(), multiple=False
            )
        except Exception as exc:
            notify(f"打开文件失败：{exc}", tone="error")
            return
        if not files:
            return
        if not app.manager.open_path(str(files[0])):
            notify("无法打开该文件（不支持或已损坏）", tone="warning")
            return
        await _load_current_async()

    async def act_open_folder() -> None:
        try:
            folder = await dialog.pick_directory(title="打开文件夹")
        except Exception as exc:
            notify(f"打开文件夹失败：{exc}", tone="error")
            return
        if folder is None:
            return
        total = await asyncio.get_running_loop().run_in_executor(
            None, app.manager.scan_folder, str(folder)
        )
        if total == 0:
            notify("该文件夹中没有可显示的图片", tone="warning")
            sync_ui()
            return
        await _load_current_async()

    async def act_open_new_window() -> None:
        """真正另起一个进程，复用主进程的共享缓存。"""
        try:
            files = await dialog.pick_files(
                title="在新窗口打开图片", extensions=_image_extensions(), multiple=True
            )
        except Exception as exc:
            notify(f"打开文件失败：{exc}", tone="error")
            return
        if not files:
            return
        from .service import spawn

        launched = sum(1 for item in files if spawn.launch_window(str(item)))
        if launched == 0:
            notify("无法启动新窗口", tone="error")
        elif launched > 1:
            notify(f"已在新窗口打开 {launched} 张图片", tone="success")

    def act_zoom_in() -> None:
        app.view = ui.zoom_by(app.view, app.image_size, app.viewport, ui.ZOOM_STEP)
        sync_ui()

    def act_zoom_out() -> None:
        app.view = ui.zoom_by(app.view, app.image_size, app.viewport, 1 / ui.ZOOM_STEP)
        sync_ui()

    def act_fit_window() -> None:
        app.view = app.view.reset()
        sync_ui()

    def act_fit_width() -> None:
        app.view = app.view.with_mode("fit_width")
        sync_ui()

    def act_actual_size() -> None:
        app.view = app.view.with_mode("actual")
        sync_ui()

    async def act_rotate_cw() -> None:
        app.manager.rotate_cw()
        await _load_current_async()

    async def act_rotate_ccw() -> None:
        app.manager.rotate_ccw()
        await _load_current_async()

    async def act_flip_h() -> None:
        app.manager.flip_horizontal()
        await _load_current_async()

    async def act_flip_v() -> None:
        app.manager.flip_vertical()
        await _load_current_async()

    async def act_fullscreen() -> None:
        app.fullscreen = not app.fullscreen
        page.window.full_screen = app.fullscreen
        _refresh_viewport()
        page.update()
        sync_ui()

    def act_slideshow() -> None:
        app.slideshow_on = not app.slideshow_on
        if app.slideshow_on:
            app.paused = False
        sync_ui()

    def act_pause() -> None:
        if app.slideshow_on:
            app.paused = not app.paused
            sync_ui()

    def act_anim_toggle() -> None:
        if app.manager.frame_count() <= 1:
            return
        if app.anim_playing and app.anim_paused:
            app.anim_paused = False
        elif app.anim_playing:
            app.anim_paused = True
        else:
            app.anim_playing = True
        sync_ui()

    def _step_frame(delta: int) -> None:
        if app.manager.frame_count() <= 1:
            return
        app.anim_paused = True
        app.manager.next_frame(delta)
        page.run_task(_refresh_frame_async)

    def act_frame_prev() -> None:
        _step_frame(-1)

    def act_frame_next() -> None:
        _step_frame(1)

    async def act_export_frame() -> None:
        if not app.manager.files:
            return
        name = Path(app.manager.info()["name"]).stem
        try:
            target = await dialog.save_file(
                title="导出当前帧",
                file_name=f"{name}_frame{app.manager.current_frame + 1:03d}.png",
                extensions=["png"],
            )
        except Exception as exc:
            notify(f"导出失败：{exc}", tone="error")
            return
        if target is None:
            return
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: app.manager.save_current_frame(str(target))
            )
            notify(f"已导出：{target.name}", tone="success")
        except Exception as exc:
            notify(f"导出失败：{exc}", tone="error")

    # ── 文件关联 ───────────────────────────────────────────────────────
    def act_assoc_install() -> None:
        set_assoc_action("install")

    def act_assoc_uninstall() -> None:
        set_assoc_action("uninstall")

    def act_assoc_status() -> None:
        page.run_task(_load_assoc_status)

    async def _load_assoc_status() -> None:
        if os.name != "nt":
            notify("文件关联仅适用于 Windows", tone="warning")
            return

        def read_status() -> str:
            from . import file_assoc

            return file_assoc.status_text()

        try:
            text = await asyncio.get_running_loop().run_in_executor(None, read_status)
        except Exception as exc:
            notify(f"读取文件关联状态失败：{exc}", tone="error")
            return
        set_assoc_status(text)

    async def _run_assoc(action: str) -> None:
        set_assoc_action(None)
        if os.name != "nt":
            notify("文件关联仅适用于 Windows", tone="warning")
            return

        def operate() -> None:
            from . import file_assoc

            buffer = ctypes.create_unicode_buffer(32768)
            length = ctypes.windll.kernel32.GetModuleFileNameW(None, buffer, len(buffer))
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
            notify(f"文件关联设置失败：{message}", tone="error")
            return
        notify(
            "已注册 ImageViewer 文件关联" if action == "install"
            else "已取消 ImageViewer 文件关联",
            tone="success",
        )

    # ── 共享缓存诊断 ───────────────────────────────────────────────────
    def act_service_status() -> None:
        page.run_task(_load_service_status)

    async def _load_service_status() -> None:
        stats = getattr(app.manager, "stats", None)
        if stats is None:
            set_service_info({
                "role": "本地模式",
                "note": "共享缓存未启用（Web 调试或 CSIV_SHARED=0）",
            })
            return
        try:
            info = await asyncio.get_running_loop().run_in_executor(None, stats)
        except Exception as exc:
            set_service_info({"role": "不可用", "error": str(exc)})
            return
        set_service_info(info or {})

    # ── 删除 / 退出 ────────────────────────────────────────────────────
    def act_delete() -> None:
        if app.manager.files:
            set_show_delete(True)

    async def confirm_delete() -> None:
        set_show_delete(False)
        try:
            removed = app.manager.delete_current()
        except Exception as exc:
            notify(f"删除失败：{exc}", tone="error")
            sync_ui()
            return
        sync_ui()
        notify(f"已删除：{removed}", tone="success")
        await _load_current_async()

    async def act_exit() -> None:
        """Esc / 菜单退出；删除确认弹窗打开时 Esc 只关弹窗。"""
        if show_delete:
            set_show_delete(False)
            return
        try:
            await page.window.close()
        except Exception:
            pass                                  # 会话已断开即达到目的

    def set_interval(seconds: float) -> None:
        app.interval = float(seconds)
        sync_ui()

    # ── 动作表：菜单、键盘、鼠标的唯一分发入口 ──────────────────────────
    ACTIONS: dict[str, Callable[[], object]] = {
        "next": lambda: _goto(1),
        "prev": lambda: _goto(-1),
        "zoom_in": act_zoom_in,
        "zoom_out": act_zoom_out,
        "fit_window": act_fit_window,
        "fit_width": act_fit_width,
        "actual_size": act_actual_size,
        "fullscreen": act_fullscreen,
        "rotate_cw": act_rotate_cw,
        "rotate_ccw": act_rotate_ccw,
        "flip_h": act_flip_h,
        "flip_v": act_flip_v,
        "slideshow": act_slideshow,
        "pause": act_pause,
        "anim_toggle": act_anim_toggle,
        "frame_prev": act_frame_prev,
        "frame_next": act_frame_next,
        "export_frame": act_export_frame,
        "open_file": act_open_file,
        "open_folder": act_open_folder,
        "open_new_window": act_open_new_window,
        "assoc.install": act_assoc_install,
        "assoc.uninstall": act_assoc_uninstall,
        "assoc.status": act_assoc_status,
        "service_status": act_service_status,
        "delete": act_delete,
        "exit": act_exit,
        **{f"interval.{sec}": (lambda s=sec: set_interval(s))
           for sec in (1, 2, 3, 5, 10)},
    }

    # 菜单数据 ↔ 动作表的覆盖契约放在 tools/ui_logic_smoke.py 里做断言，
    # 运行时不做任何遍历：每帧重绘不该为「检查」付一次代价。

    async def _run_action(action: str) -> None:
        handler = ACTIONS.get(action)
        if handler is None:
            return
        result = handler()
        if asyncio.iscoroutine(result):
            await result

    def _on_menu_action(action: ui.MenuAction) -> None:
        page.run_task(_run_action, action.key)

    # ── 键盘 ───────────────────────────────────────────────────────────
    def _on_keyboard(e) -> None:
        """组合键标志上报不可靠，以 KeyboardListener 状态 + 系统查询为准。"""
        if not _ctrl_pressed() and not e.ctrl:
            app.ctrl_held = False                  # 自愈
        ctrl = e.ctrl or app.ctrl_held or _ctrl_pressed()
        shift = e.shift or app.shift_held
        action = match(e, ctrl=ctrl, shift=shift)
        if action:
            page.run_task(_run_action, action)

    def _on_key_down(e) -> None:
        key = (e.key or "").lower()
        if "control" in key:
            app.ctrl_held = True
        elif "shift" in key:
            app.shift_held = True

    def _on_key_up(e) -> None:
        key = (e.key or "").lower()
        if "control" in key:
            app.ctrl_held = False
        elif "shift" in key:
            app.shift_held = False

    def _on_tap_down(_e) -> None:
        listener = kb_listener_ref.current
        if listener is not None:
            page.run_task(listener.focus)

    # ── 视口 ───────────────────────────────────────────────────────────
    def _refresh_viewport(width: float | None = None,
                          height: float | None = None) -> None:
        """尺寸优先级：resize 事件 > INITIAL_SIZE > page.width/height。"""
        vw = width if width is not None else (
            INITIAL_SIZE[0] if INITIAL_SIZE else (page.width or 900)
        )
        vh = height if height is not None else (
            INITIAL_SIZE[1] if INITIAL_SIZE else (page.height or 600)
        )
        if not app.fullscreen:
            vh -= D.CHROME_H
        app.viewport = ui.ViewSize.of(max(vw, 80), max(vh, 80))

    def _on_resize(e) -> None:
        set_initial_size(e.width, e.height)
        _refresh_viewport(e.width, e.height)
        sync_ui()

    def _install_handlers() -> None:
        page.on_keyboard_event = lambda e: dispatch_ref.current(e)
        page.on_resize = lambda e: resize_ref.current(e)
        page.update()
        page.run_task(_calibrate_viewport)

    async def _calibrate_viewport() -> None:
        """等首个真实 resize 事件拿到客户端尺寸后校准（最多约 2 秒）。"""
        for _ in range(8):
            if INITIAL_SIZE:
                _refresh_viewport()
                sync_ui()
                return
            await asyncio.sleep(0.25)

    dispatch_ref.current = _on_keyboard
    resize_ref.current = _on_resize
    ui.use_effect(_install_handlers, [])

    # ── 启动参数 ───────────────────────────────────────────────────────
    def _startup_open() -> None:
        if START_PATH:
            page.run_task(_open_startup_path, START_PATH)

    async def _open_startup_path(path: str) -> None:
        target = Path(path)
        try:
            if target.is_dir():
                total = await asyncio.get_running_loop().run_in_executor(
                    None, app.manager.scan_folder, target
                )
                if total == 0:
                    notify("该文件夹中没有可显示的图片", tone="warning")
                    sync_ui()
                    return
            elif not app.manager.open_path(target):
                notify("无法打开该文件（不支持或已损坏）", tone="warning")
                sync_ui()
                return
            await _load_current_async()
        finally:
            set_startup_done(True)

    ui.use_effect(_startup_open, [])

    # ── 幻灯片 ─────────────────────────────────────────────────────────
    def _slideshow_effect() -> None:
        if app.slideshow_on and (app.slideshow_task is None or app.slideshow_task.done()):
            app.slideshow_task = page.run_task(_slideshow_loop)
        elif not app.slideshow_on and app.slideshow_task is not None:
            app.slideshow_task.cancel()
            app.slideshow_task = None

    ui.use_effect(_slideshow_effect, [slideshow_on])

    async def _slideshow_loop() -> None:
        try:
            while app.slideshow_on:
                await asyncio.sleep(app.interval)
                if app.paused:
                    continue
                await _goto(1)
        except asyncio.CancelledError:
            pass
        finally:
            app.slideshow_task = None

    # ── 鼠标：滚轮 / 框选 / 平移 ───────────────────────────────────────
    def _on_scroll(e) -> None:
        """三种意图由 ui.scroll_intent 判定，回调只负责执行。"""
        intent = ui.scroll_intent(
            app.image_size, app.viewport, app.view, e.scroll_delta.y,
            ctrl=app.ctrl_held or _ctrl_pressed(),
        )
        if intent.kind == "none":
            return
        if intent.kind == "zoom":
            loc = e.local_position
            anchor = ui.ViewPoint(
                loc.x if loc is not None else app.viewport.width / 2,
                loc.y if loc is not None else app.viewport.height / 2,
            )
            app.view = ui.zoom_anchored(
                app.view, app.image_size, app.viewport, anchor, intent.factor
            )
            sync_ui()
        elif intent.kind == "page":
            page.run_task(_goto, intent.direction)
        else:
            _clear_selection()
            app.view = ui.ViewTransform(
                app.view.mode, app.view.zoom,
                app.view.pan_x,
                app.view.pan_y + (intent.delta if intent.direction < 0 else -intent.delta),
            )
            app.view = ui.clamp_pan(app.view, app.image_size, app.viewport)
            set_view(app.view)
            set_render_tick(lambda tick: tick + 1)

    def _clear_selection() -> None:
        app.sel_rect = None
        set_selection(None)

    def _zoom_to_region(region: ui.ViewRect) -> None:
        next_view = ui.zoom_to_region(
            app.view, app.image_size, app.viewport, region
        )
        if next_view == app.view:
            return
        app.view = next_view
        _clear_selection()
        sync_ui()

    def _on_tap(e) -> None:
        region = app.sel_rect
        if region is None or e.local_position is None:
            return
        point = ui.ViewPoint(e.local_position.x, e.local_position.y)
        if region.contains(point):
            _zoom_to_region(region)
        else:
            _clear_selection()

    def _on_pan_start(e) -> None:
        _clear_selection()
        if e.local_position is not None:
            x, y = e.local_position.x, e.local_position.y
            app.sel_drag_start = (x, y)
            app.sel_rect = ui.ViewRect.from_corners(x, y, x, y)
            set_selection(app.sel_rect)

    def _on_pan_update(e) -> None:
        if app.sel_drag_start is None or e.local_position is None:
            return
        x0, y0 = app.sel_drag_start
        app.sel_rect = ui.ViewRect.from_corners(x0, y0, e.local_position.x, e.local_position.y)
        set_selection(app.sel_rect)

    def _on_pan_end(_e) -> None:
        app.sel_drag_start = None

    def _on_right_pan_start(e) -> None:
        _clear_selection()
        app.right_last = (
            (e.local_position.x, e.local_position.y) if e.local_position is not None else None
        )

    def _on_right_pan_update(e) -> None:
        if e.local_position is None or app.right_last is None:
            return
        lx, ly = e.local_position.x, e.local_position.y
        app.view = ui.ViewTransform(
            app.view.mode, app.view.zoom,
            app.view.pan_x + lx - app.right_last[0],
            app.view.pan_y + ly - app.right_last[1],
        )
        app.right_last = (lx, ly)
        app.view = ui.clamp_pan(app.view, app.image_size, app.viewport)
        set_view(app.view)
        set_render_tick(lambda tick: tick + 1)

    def _on_right_pan_end(_e) -> None:
        app.right_last = None

    def _on_double_tap(_e) -> None:
        page.run_task(act_fullscreen)

    # ── 对话框 ─────────────────────────────────────────────────────────
    ui.use_dialog(
        confirm_dialog(
            heading="删除图片",
            message=f"确定将「{status.name}」移至回收站吗？",
            confirm_label="删除",
            on_confirm=confirm_delete,
            on_cancel=lambda _e: set_show_delete(False),
            danger=True,
        )
        if show_delete else None
    )
    ui.use_dialog(
        info_dialog(
            heading="文件关联状态",
            message=assoc_status or "",
            on_close=lambda _e: set_assoc_status(None),
        )
        if assoc_status is not None else None
    )
    ui.use_dialog(
        confirm_dialog(
            heading="文件关联",
            message=(
                "将 ImageViewer 注册到 Windows 图片文件的「打开方式」和右键菜单。"
                if assoc_action == "install"
                else "将撤销 ImageViewer 写入的文件关联和右键菜单。"
            ),
            confirm_label="注册" if assoc_action == "install" else "取消关联",
            on_confirm=lambda _e: page.run_task(_run_assoc, assoc_action),
            on_cancel=lambda _e: set_assoc_action(None),
            modal=True,
        )
        if assoc_action else None
    )
    ui.use_dialog(
        info_dialog(
            heading="共享缓存状态",
            message=format_service_info(service_info),
            on_close=lambda _e: set_service_info(None),
            selectable=True,
        )
        if service_info is not None else None
    )

    # ── 界面装配 ───────────────────────────────────────────────────────
    menu_ctx = MenuContext(
        slideshow_on=slideshow_on,
        paused=paused,
        interval=app.interval,
        anim_playing=anim_playing,
        anim_paused=anim_paused,
        has_frames=app.manager.frame_count() > 1,
        has_image=status.has_image,
    )
    menu_bar = build_menu_bar(build_sections(menu_ctx), _on_menu_action)
    status_bar = build_status_bar(status)

    # 布局沿用显式高度：Column 内 expand 子控件会吞掉后续兄弟空间，
    # 因此菜单 / 图片区 / 状态栏三段高度之和恰为窗口高度。
    page_h = (INITIAL_SIZE[1] if INITIAL_SIZE else (page.height or 600)) or 600
    image_h = page_h if fullscreen else max(page_h - D.CHROME_H, 80)
    canvas = build_canvas(
        src=img_src,
        image_size=image_size,
        viewport=app.viewport,
        view=view,
        selection=selection,
        height=image_h,
        fullscreen=fullscreen,
        show_nav=len(app.manager.files) > 1,
        placeholder=None if not startup_done else empty_stage(),
        handlers=CanvasHandlers(
            on_tap_down=_on_tap_down,
            on_tap=_on_tap,
            on_pan_start=_on_pan_start,
            on_pan_update=_on_pan_update,
            on_pan_end=_on_pan_end,
            on_right_pan_start=_on_right_pan_start,
            on_right_pan_update=_on_right_pan_update,
            on_right_pan_end=_on_right_pan_end,
            on_scroll=_on_scroll,
            on_double_tap=_on_double_tap,
            on_prev=lambda _e: _nav_click(-1),
            on_next=lambda _e: _nav_click(1),
        ),
    )
    controls = [canvas] if fullscreen else [menu_bar, canvas, status_bar]
    root = ui.Column(expand=True, spacing=0, controls=controls)
    kb_listener_ref.current = ui.KeyboardListener(
        content=root,
        autofocus=True,
        on_key_down=_on_key_down,
        on_key_up=_on_key_up,
    )
    return kb_listener_ref.current
