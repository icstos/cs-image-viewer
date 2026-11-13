"""根组件 —— 把 :class:`ViewerState` 铺成界面，把事件交回 viewmodel。

这个文件里没有业务逻辑，只有三件事：

============ ==========================================================
渲染         ``build_*`` 构件按状态装配（``UI = f(state)``）
分发        菜单 / 键盘 / 鼠标 / 滚轮 → ``vm.dispatch`` 与手势方法
副作用       订阅、窗口尺寸、全屏、提示、弹窗、关闭 —— 全部由 effect 声明
============ ==========================================================

副作用为什么都做成 effect 而不是「点了就执行」：这样「状态是唯一真相」不会破。
例如全屏：按钮只把 ``fullscreen`` 翻过来，真正设置窗口属性的是依赖该字段的
effect；于是无论谁改了状态（按钮、快捷键、双击），窗口属性都会跟上。

由于 viewmodel 自己托管播放循环（见 ``_reconcile_loops``），本文件连定时器都
不需要 —— 视图只负责「画」，以及把用户动作翻译成对 viewmodel 的调用。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import ui

from ..models import DialogKind, DialogRequest, RequestKind
from ..viewmodel import ViewerViewModel
from . import design as D
from .bars import build_menu_bar, build_status_bar
from .canvas import CanvasHandlers, build_canvas
from .dialogs import confirm_dialog, info_dialog
from .keymap import match_key
from .kit import empty_stage, toast
from .menu import build_sections

#: ``ImageViewerApp`` 是 :func:`build_app` **内部**定义的组件（它要闭包捕获 vm），
#: 不是模块级名字，因此不能出现在 ``__all__`` 里（否则 ``from ... import *`` 直接炸）。
__all__ = ["build_app", "remember_window_size"]

#: 最近一次窗口 resize 报告的尺寸。0.86.x 的 ``page.height`` 常常是陈旧值，
#: 因此启动阶段的视口高度以这个值为准（由 main.py 的 resize 钩子先记下来）。
_window_size: tuple[float, float] | None = None


def remember_window_size(width: float, height: float) -> None:
    """记录窗口尺寸（main.py 在 render 之前挂到 ``page.on_resize`` 上）。"""
    global _window_size
    _window_size = (width, height)


def build_app(
    vm: ViewerViewModel,
    *,
    ctrl_probe: Callable[[], bool] | None = None,
) -> Callable[[], ui.Control]:
    """构造根组件。``ctrl_probe`` 是「系统级 Ctrl 状态」的注入点（便于测试）。"""
    probe_ctrl = ctrl_probe or (lambda: False)

    @ui.component
    def ImageViewerApp() -> ui.Control:
        page = ui.context.page
        state, set_state = ui.use_state(vm.state)

        # ── 订阅：viewmodel 每次换状态就重绘一次 ───────────────────────
        ui.use_effect(lambda: vm.subscribe(set_state), [])

        # ── 引用：对话框、键盘监听、最新闭包 ───────────────────────────
        dialog_ref = ui.use_ref(None)
        if dialog_ref.current is None:
            dialog_ref.current = ui.FileDialog()
        dialog: ui.FileDialog = dialog_ref.current
        listener_ref = ui.use_ref(None)
        dispatch_ref = ui.use_ref(None)        # 键盘分发（每帧刷新，闭包不陈旧）
        resize_ref = ui.use_ref(None)
        ctrl_ref = ui.use_ref(False)
        shift_ref = ui.use_ref(False)

        # ── 视口尺寸 ───────────────────────────────────────────────────
        def _sync_viewport(width: float | None = None, height: float | None = None) -> None:
            """尺寸优先级：resize 事件 > 记录的窗口尺寸 > page 属性。"""
            size = _window_size
            total_w = width if width is not None else (size[0] if size else (page.width or 900))
            total_h = height if height is not None else (size[1] if size else (page.height or 600))
            stage_h = total_h if state.fullscreen else total_h - D.CHROME_H
            vm.set_viewport(total_w, stage_h)

        async def _calibrate() -> None:
            """等首个真实 resize 事件拿到客户端尺寸后校准（最多约 2 秒）。"""
            for _ in range(8):
                if _window_size:
                    _sync_viewport()
                    return
                await _sleep(0.25)

        def _on_resize(event: Any) -> None:
            remember_window_size(event.width, event.height)
            _sync_viewport(event.width, event.height)

        def _install_handlers() -> None:
            page.on_keyboard_event = lambda event: dispatch_ref.current(event)
            page.on_resize = lambda event: resize_ref.current(event)
            page.update()
            page.run_task(_calibrate)

        ui.use_effect(_install_handlers, [])

        # ── 键盘 ───────────────────────────────────────────────────────
        def _ctrl_down() -> bool:
            """组合键标志不可靠，以监听器状态 + 系统查询为准。"""
            return ctrl_ref.current or probe_ctrl()

        def _on_key(event: Any) -> None:
            if not probe_ctrl() and not event.ctrl:
                ctrl_ref.current = False                # 自愈：抬起事件丢了也不要卡住
            ctrl = event.ctrl or _ctrl_down()
            shift = event.shift or shift_ref.current
            action = match_key(event.key or "", ctrl=ctrl, shift=shift)
            if action is not None:
                vm.dispatch(action)

        def _on_key_down(event: Any) -> None:
            label = (event.key or "").lower()
            if "control" in label:
                ctrl_ref.current = True
            elif "shift" in label:
                shift_ref.current = True

        def _on_key_up(event: Any) -> None:
            label = (event.key or "").lower()
            if "control" in label:
                ctrl_ref.current = False
            elif "shift" in label:
                shift_ref.current = False

        def _refocus(_event: Any = None) -> None:
            """点击控件后把焦点还给键盘监听，否则快捷键会失灵。"""
            listener = listener_ref.current
            if listener is not None:
                page.run_task(listener.focus)

        # ── 鼠标：位置取值 + 转交 viewmodel ─────────────────────────────
        def _point(event: Any) -> tuple[float | None, float | None]:
            local = getattr(event, "local_position", None)
            if local is None:
                return None, None
            return local.x, local.y

        def _on_tap(event: Any) -> None:
            x, y = _point(event)
            if x is not None and y is not None:
                vm.tap(x, y)

        def _on_pan_start(event: Any) -> None:
            x, y = _point(event)
            if x is not None and y is not None:
                vm.selection_start(x, y)

        def _on_pan_update(event: Any) -> None:
            x, y = _point(event)
            if x is not None and y is not None:
                vm.selection_update(x, y)

        def _on_right_pan_start(event: Any) -> None:
            x, y = _point(event)
            if x is not None and y is not None:
                vm.drag_start(x, y)

        def _on_right_pan_update(event: Any) -> None:
            x, y = _point(event)
            if x is not None and y is not None:
                vm.drag_update(x, y)

        def _on_scroll(event: Any) -> None:
            x, y = _point(event)
            vm.scroll(event.scroll_delta.y, x=x, y=y, ctrl=_ctrl_down())

        def _navigate(delta: int) -> Callable[[Any], None]:
            def _handler(_event: Any) -> None:
                vm.dispatch_async(vm.goto(delta))
                _refocus()

            return _handler

        dispatch_ref.current = _on_key
        resize_ref.current = _on_resize

        # ── 副作用：由状态驱动 ─────────────────────────────────────────
        def _apply_fullscreen() -> None:
            page.window.full_screen = state.fullscreen
            page.update()
            _sync_viewport()

        ui.use_effect(_apply_fullscreen, [state.fullscreen])

        def _show_notice() -> None:
            notice = state.notice
            if notice is not None:
                toast(page, notice.message, tone=notice.tone, seconds=notice.seconds)

        ui.use_effect(_show_notice, [state.notice.seq if state.notice else 0])

        def _close_window() -> None:
            if not state.closing:
                return
            vm.shutdown()
            page.run_task(_close_page)

        async def _close_page() -> None:
            try:
                await page.window.close()
            except Exception:
                pass                                   # 会话已断开即达到目的

        ui.use_effect(_close_window, [state.closing])

        # ── 文件对话框：viewmodel 只写「我要一个路径」 ─────────────────
        async def _ask_user(request: RequestKind) -> None:
            try:
                if request is RequestKind.PICK_FILE:
                    picked = await dialog.pick_files(
                        title="打开图片",
                        extensions=list(vm.supported_extensions()),
                        multiple=False,
                    )
                    _submit_pick(picked)
                elif request is RequestKind.PICK_FOLDER:
                    folder = await dialog.pick_directory(title="打开文件夹")
                    _submit_pick([folder] if folder else None)
                elif request is RequestKind.PICK_MANY:
                    picked = await dialog.pick_files(
                        title="在新窗口打开图片",
                        extensions=list(vm.supported_extensions()),
                        multiple=True,
                    )
                    _submit_pick(picked)
                elif request is RequestKind.SAVE_FRAME:
                    target = await dialog.save_file(
                        title="导出当前帧",
                        file_name=vm.default_export_name(),
                        extensions=["png"],
                    )
                    if target:
                        vm.submit_save(target)
                    else:
                        vm.cancel_request()
            except Exception as exc:                   # 对话框本身失败（权限/被占用）
                vm.report_failure(_request_label(request), exc)
                vm.cancel_request()

        def _submit_pick(picked: Any) -> None:
            if picked:
                vm.submit_pick(picked)
            else:
                vm.cancel_request()

        def _handle_request() -> None:
            if state.request is not None:
                page.run_task(_ask_user, state.request)

        ui.use_effect(_handle_request, [state.request])

        # ── 启动参数 ───────────────────────────────────────────────────
        def _start() -> None:
            page.run_task(vm.start)

        ui.use_effect(_start, [])

        # ── 场景装配 ───────────────────────────────────────────────────
        total_h = _window_size[1] if _window_size else (page.height or 600)
        stage_h = total_h if state.fullscreen else max(total_h - D.CHROME_H, 80)
        canvas = build_canvas(
            src=state.src,
            image_size=state.image_size,
            viewport=state.viewport,
            view=state.view,
            selection=state.selection,
            height=stage_h,
            fullscreen=state.fullscreen,
            show_nav=state.show_nav,
            placeholder=empty_stage() if state.show_placeholder else None,
            handlers=CanvasHandlers(
                on_tap_down=_refocus,
                on_tap=_on_tap,
                on_pan_start=_on_pan_start,
                on_pan_update=_on_pan_update,
                on_pan_end=lambda _e: vm.selection_end(),
                on_right_pan_start=_on_right_pan_start,
                on_right_pan_update=_on_right_pan_update,
                on_right_pan_end=lambda _e: vm.drag_end(),
                on_scroll=_on_scroll,
                on_double_tap=lambda _e: vm.toggle_fullscreen(),
                on_prev=_navigate(-1),
                on_next=_navigate(1),
            ),
        )
        controls: list[ui.Control] = [canvas]
        if not state.fullscreen:
            controls = [
                build_menu_bar(build_sections(state), lambda action: vm.dispatch(action.key)),
                canvas,
                build_status_bar(state.status),
            ]

        # 显式高度而非 expand：Column 里的 expand 子控件会吞掉后续兄弟的空间，
        # 因此「菜单 + 图片区 + 状态栏」三段高度之和恰为窗口高度。
        root = ui.Column(expand=True, spacing=0, controls=controls)

        # ── 弹窗（状态里有就画，没有就不画）────────────────────────────
        ui.use_dialog(_build_dialog(state.dialog, vm) if state.dialog else None)

        listener_ref.current = ui.KeyboardListener(
            content=root,
            autofocus=True,
            on_key_down=_on_key_down,
            on_key_up=_on_key_up,
        )
        return listener_ref.current

    return ImageViewerApp


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _request_label(request: RequestKind) -> str:
    """对话框失败时回执的开头（「导出失败」而不是「操作失败」）。"""
    return "导出" if request is RequestKind.SAVE_FRAME else "打开文件"


def _build_dialog(request: DialogRequest, vm: ViewerViewModel) -> ui.Control:
    """把一个 :class:`DialogRequest` 画成 cs-ui 对话框。

    标题一律是**具体动作**（「删除图片」而不是「提示」），按钮文案写具体动词
    （「删除」而不是「确定」）—— 破坏性操作的确认成本不该由用户猜。
    """
    if request.kind in (DialogKind.DELETE, DialogKind.ASSOCIATION):
        return confirm_dialog(
            heading=request.heading,
            message=request.message,
            confirm_label=request.confirm_label,
            on_confirm=lambda _e: vm.confirm(),
            on_cancel=lambda _e: vm.dismiss(),
            danger=request.danger,
            modal=request.modal,
        )
    return info_dialog(
        heading=request.heading,
        message=request.message,
        on_close=lambda _e: vm.dismiss(),
        selectable=request.selectable,
    )
