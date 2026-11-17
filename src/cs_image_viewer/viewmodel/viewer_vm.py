"""视图模型 —— 一个窗口的**全部界面状态**与**全部用户意图**。

它是 MVVM 里的中间层，职责只有两条：

1. **持有状态**：一份冻结的 :class:`ViewerState`，任何变化都换一个新对象并通知
   订阅者（``UI = f(state)``）。界面因此从不「查询」当前是什么，只渲染拿到的那份。
2. **处理意图**：菜单、键盘、鼠标、滚轮最终都落到这里的方法上。方法自己不做
   界面、不碰控件、不认识 ``ui``；它只做三件事 —— 算新状态、调业务用例、给回执。

阻塞的活（解码、扫描目录、写注册表）一律通过 :class:`BackgroundRunner` 丢到后台
线程，回到主线程只做「把结果写进状态」这一件轻活。协程由 :class:`TaskScheduler`
托管，因此在没有事件循环的环境（单测、脚本）里也一样能跑。

几条写在代码里的取舍：

* **先出图、后补目录**：打开单张图不等目录扫描，首帧延迟与目录大小无关；
* **代际号防串**：连续快速翻页时，早先的解码结果回来必须被丢弃，否则画面会跳回去；
* **需要视图协助的输入（选文件 / 存盘）写成状态**：视图看到就弹、弹完回填，
  菜单因此仍然只有一张动作表，不必在视图里再分一次流。
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..models import (
    DEFAULT_INTERVAL,
    SLIDESHOW_INTERVALS,
    Action,
    DialogKind,
    DialogRequest,
    DisplayImage,
    ImageInfo,
    Notice,
    RequestKind,
    StatusSnapshot,
    Tone,
    ViewerState,
    ViewPoint,
    ViewRect,
    ViewSize,
    ViewTransform,
    clamp_pan,
    interval_action,
    scroll_intent,
    zoom_anchored,
    zoom_by,
    zoom_to_region,
)
from ..models.geometry import ZOOM_STEP
from .formatting import format_service_report, format_status
from .ports import BackgroundRunner, TaskScheduler, ViewerFacade

__all__ = ["ViewerViewModel"]


class ViewerViewModel:
    """一个窗口的视图模型。构造即就绪，不依赖任何 UI 框架。"""

    def __init__(
        self,
        viewer: ViewerFacade,
        runner: BackgroundRunner,
        scheduler: TaskScheduler,
        *,
        viewport: ViewSize | None = None,
        start_path: str | None = None,
    ) -> None:
        self._viewer = viewer
        self._runner = runner
        self._scheduler = scheduler
        self._start_path = start_path
        self._listeners: list[Callable[[ViewerState], None]] = []
        self._state = ViewerState(
            viewport=viewport or ViewSize.of(900, 560),
            # 有启动参数时先不画空态：解码完再决定画图片还是画提示
            ready=start_path is None,
            interval=DEFAULT_INTERVAL,
        )
        self._info = ImageInfo()
        self._frame = 0
        self._shared = False
        self._notice_seq = 0
        self._generation = 0
        self._drag_origin: tuple[float, float] | None = None
        self._pan_origin: tuple[float, float] | None = None
        self._loops: dict[str, Any] = {}
        self._actions = self._build_actions()

    # ==================================================================
    # 状态订阅
    # ==================================================================
    @property
    def state(self) -> ViewerState:
        """当前状态（冻结值）。界面渲染与断言都读它。"""
        return self._state

    def subscribe(self, listener: Callable[[ViewerState], None]) -> Callable[[], None]:
        """注册状态监听；返回取消订阅的函数（供 effect 清理用）。"""
        self._listeners.append(listener)

        def _unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _unsubscribe

    def _set(self, **changes: Any) -> None:
        """写状态：内容没变就不通知（``use_state`` 因此不会白重绘一次）。"""
        candidate = replace(self._state, **changes)
        if candidate == self._state:
            return
        self._state = candidate
        for listener in tuple(self._listeners):
            listener(candidate)
        self._reconcile_loops()

    def _reconcile_loops(self) -> None:
        """让后台循环与状态对齐：``anim_playing`` / ``slideshow_on`` 为真就跑。

        循环由状态驱动而不是由按钮驱动 —— 于是「开始播放」和「切到动图自动播放」
        走的是同一条路，也不会出现「按钮点了、任务没起」或「任务还在、按钮已停」。
        """
        self._sync_loop("animation", self._state.anim_playing, self.drive_animation)
        self._sync_loop("slideshow", self._state.slideshow_on, self.drive_slideshow)

    def _sync_loop(self, key: str, wanted: bool, driver: Callable[[], Any]) -> None:
        handle = self._loops.get(key)
        if wanted and handle is None:
            self._loops[key] = self._scheduler.spawn(driver())
        elif not wanted and handle is not None:
            self._loops.pop(key, None)
            self._scheduler.cancel(handle)

    def _notify(self, message: str, tone: Tone = Tone.INFO, seconds: float = 2.5) -> None:
        """弹一条一次性回执；序号自增，同文案连发两次也能各自出现。"""
        self._notice_seq += 1
        self._set(notice=Notice(self._notice_seq, message, tone, seconds))

    # ==================================================================
    # 生命周期
    # ==================================================================
    async def start(self) -> None:
        """打开启动参数指定的文件 / 文件夹（无参数时什么都不做）。"""
        path = self._start_path
        if path is None:
            return
        target = Path(path)
        try:
            if target.is_dir():
                total = await self._runner.run(lambda: self._viewer.open_folder(target))
                if total == 0:
                    self._notify("该文件夹中没有可显示的图片", Tone.WARNING)
                    return
            elif not await self._runner.run(lambda: self._viewer.open_file(target)):
                self._notify("无法打开该文件（不支持或已损坏）", Tone.WARNING)
                return
            await self._load()
        finally:
            self._set(ready=True)

    def set_viewport(self, width: float, height: float) -> None:
        """可视区尺寸变化（窗口 resize / 进出全屏）。几何全部依赖它。"""
        viewport = ViewSize.of(max(width, 80.0), max(height, 80.0))
        if viewport == self._state.viewport:
            return
        view = clamp_pan(self._state.view, self._state.image_size, viewport)
        self._set(viewport=viewport, view=view, status=self._status(view=view))

    def shutdown(self) -> None:
        """窗口关闭前停掉后台循环、释放业务侧持有的句柄。"""
        for key in tuple(self._loops):
            handle = self._loops.pop(key)
            self._scheduler.cancel(handle)
        self._listeners.clear()
        self._viewer.close()

    # ==================================================================
    # 动作分发（菜单 / 键盘 / 手势的唯一入口）
    # ==================================================================
    def dispatch(self, action: Action | str) -> None:
        """执行一个动作；协程交给调度器，同步异常在内部吞成回执。"""
        try:
            key = Action(action)
        except ValueError:
            return
        handler = self._actions.get(key)
        if handler is None:
            return
        result = handler()
        if inspect.isawaitable(result):
            self._scheduler.spawn(result)   # type: ignore[arg-type]

    @property
    def actions(self) -> dict[Action, Callable[[], Any]]:
        """动作表快照：自检与文档读它，证明「菜单上的每一项都有人接」。"""
        return dict(self._actions)

    def _build_actions(self) -> dict[Action, Callable[[], Any]]:
        """动作表：与菜单叶子键一一对应（契约由自检断言）。"""
        table: dict[Action, Callable[[], Any]] = {
            Action.NEXT: lambda: self.goto(1),
            Action.PREV: lambda: self.goto(-1),
            Action.ZOOM_IN: lambda: self.zoom(ZOOM_STEP),
            Action.ZOOM_OUT: lambda: self.zoom(1 / ZOOM_STEP),
            Action.FIT_WINDOW: self.fit_window,
            Action.FIT_WIDTH: self.fit_width,
            Action.ACTUAL_SIZE: self.actual_size,
            Action.FULLSCREEN: self.toggle_fullscreen,
            Action.EXIT: self.request_exit,
            Action.ROTATE_CW: lambda: self.rotate(1),
            Action.ROTATE_CCW: lambda: self.rotate(-1),
            Action.FLIP_H: lambda: self.flip(horizontal=True),
            Action.FLIP_V: lambda: self.flip(horizontal=False),
            Action.SLIDESHOW: self.toggle_slideshow,
            Action.PAUSE: self.toggle_pause,
            Action.ANIM_TOGGLE: self.toggle_animation,
            Action.FRAME_PREV: lambda: self.step_frame(-1),
            Action.FRAME_NEXT: lambda: self.step_frame(1),
            Action.OPEN_FILE: lambda: self._ask(RequestKind.PICK_FILE),
            Action.OPEN_FOLDER: lambda: self._ask(RequestKind.PICK_FOLDER),
            Action.OPEN_NEW_WINDOW: lambda: self._ask(RequestKind.PICK_MANY),
            Action.EXPORT_FRAME: lambda: self._ask(RequestKind.SAVE_FRAME),
            Action.DELETE: self.request_delete,
            Action.ASSOC_INSTALL: self.request_association_install,
            Action.ASSOC_UNINSTALL: self.request_association_uninstall,
            Action.ASSOC_STATUS: self.show_association_status,
            Action.SERVICE_STATUS: self.show_service_report,
        }
        for seconds in SLIDESHOW_INTERVALS:
            table[interval_action(seconds)] = lambda s=seconds: self.set_interval(s)
        return table

    # ==================================================================
    # 打开（需要视图协助：选路径后回填）
    # ==================================================================
    def supported_extensions(self) -> tuple[str, ...]:
        """文件对话框的扩展名过滤（不带点）。"""
        return self._viewer.supported_extensions()

    def _ask(self, request: RequestKind) -> None:
        self._set(request=request)

    def cancel_request(self) -> None:
        """用户取消选择：把请求清掉即可（不产生任何副作用）。"""
        if self._state.request is not None:
            self._set(request=None)

    def submit_pick(self, paths: Sequence[str | Path]) -> None:
        """视图选完路径后回填。"""
        request = self._state.request
        self._set(request=None)
        if request is None or not paths:
            return
        first = Path(paths[0])
        if request is RequestKind.PICK_FILE:
            self.dispatch_async(self.open_file(first))
        elif request is RequestKind.PICK_FOLDER:
            self.dispatch_async(self.open_folder(first))
        elif request is RequestKind.PICK_MANY:
            self.dispatch_async(self.open_in_new_windows([Path(p) for p in paths]))

    def submit_save(self, target: str | Path) -> None:
        """视图选完保存路径后回填。"""
        request = self._state.request
        self._set(request=None)
        if request is RequestKind.SAVE_FRAME and target:
            self.dispatch_async(self.export_current(Path(target)))

    def dispatch_async(self, coro: Any) -> None:
        """把一个协程交给调度器（视图回填路径、按钮点击都用它）。"""
        self._scheduler.spawn(coro)

    async def open_file(self, path: str | Path) -> None:
        ok = await self._runner.run(lambda: self._viewer.open_file(path))
        if not ok:
            self._notify("无法打开该文件（不支持或已损坏）", Tone.WARNING)
            return
        await self._load()

    async def open_folder(self, folder: str | Path) -> None:
        total = await self._runner.run(lambda: self._viewer.open_folder(folder))
        if total == 0:
            self._notify("该文件夹中没有可显示的图片", Tone.WARNING)
            self._collapse()
            return
        await self._load()

    async def open_in_new_windows(self, paths: Sequence[Path]) -> None:
        launched = await self._runner.run(lambda: self._viewer.open_in_new_windows(paths))
        if launched == 0:
            self._notify("无法启动新窗口", Tone.ERROR)
        elif launched > 1:
            self._notify(f"已在新窗口打开 {launched} 张图片", Tone.SUCCESS)

    def report_failure(self, action: str, error: object) -> None:
        """视图侧失败（文件对话框抛错等）转成回执：文案拼装仍在本层。"""
        self._notify(f"{action}失败：{error}", Tone.ERROR)

    # ==================================================================
    # 导航
    # ==================================================================
    async def goto(self, delta: int) -> None:
        """切换图片（首尾循环、跳过坏图）。"""
        moved = await self._runner.run(lambda: self._viewer.navigate(delta))
        if moved:
            await self._load()
        elif self._state.src is None and self._viewer.has_files:
            self._notify("没有可显示的图片", Tone.WARNING)

    # ==================================================================
    # 缩放 / 视图
    # ==================================================================
    def zoom(self, factor: float) -> None:
        state = self._state
        self._commit_view(
            zoom_by(state.view, state.image_size, state.viewport, factor)
        )

    def fit_window(self) -> None:
        self._commit_view(self._state.view.reset())

    def fit_width(self) -> None:
        self._commit_view(self._state.view.with_mode("fit_width"))

    def actual_size(self) -> None:
        self._commit_view(self._state.view.with_mode("actual"))

    def _commit_view(self, view: ViewTransform) -> None:
        """落地一个视图变换：夹住边界、作废旧选区、刷新倍率文案。"""
        view = clamp_pan(view, self._state.image_size, self._state.viewport)
        if view == self._state.view and self._state.selection is None:
            return
        self._set(view=view, selection=None, status=self._status(view=view))

    def scroll(self, delta_y: float, *, x: float | None = None,
               y: float | None = None, ctrl: bool = False) -> None:
        """滚轮：三种意图由几何层判定，这里只负责执行。"""
        state = self._state
        intent = scroll_intent(
            state.image_size, state.viewport, state.view, delta_y, ctrl=ctrl
        )
        if intent.kind == "zoom":
            anchor = ViewPoint(
                x if x is not None else state.viewport.width / 2,
                y if y is not None else state.viewport.height / 2,
            )
            self._commit_view(
                zoom_anchored(state.view, state.image_size, state.viewport, anchor, intent.factor)
            )
        elif intent.kind == "page":
            self._scheduler.spawn(self.goto(intent.direction))
        elif intent.kind == "pan":
            moved = ViewTransform(
                state.view.mode,
                state.view.zoom,
                state.view.pan_x,
                state.view.pan_y + (intent.delta if intent.direction < 0 else -intent.delta),
            )
            self._commit_view(moved)

    # ==================================================================
    # 手势：框选放大 / 右键平移
    # ==================================================================
    def selection_start(self, x: float, y: float) -> None:
        self._drag_origin = (x, y)
        self._set(selection=ViewRect.from_corners(x, y, x, y))

    def selection_update(self, x: float, y: float) -> None:
        if self._drag_origin is None:
            return
        x0, y0 = self._drag_origin
        self._set(selection=ViewRect.from_corners(x0, y0, x, y))

    def selection_end(self) -> None:
        self._drag_origin = None

    def tap(self, x: float, y: float) -> None:
        """单击：落在选区内就放大到该区域，否则取消选区。"""
        region = self._state.selection
        if region is None:
            return
        if region.contains(ViewPoint(x, y)):
            state = self._state
            self._commit_view(
                zoom_to_region(state.view, state.image_size, state.viewport, region)
            )
        else:
            self._set(selection=None)

    def drag_start(self, x: float, y: float) -> None:
        self._pan_origin = (x, y)
        if self._state.selection is not None:
            self._set(selection=None)

    def drag_update(self, x: float, y: float) -> None:
        if self._pan_origin is None:
            return
        ox, oy = self._pan_origin
        self._pan_origin = (x, y)
        state = self._state
        self._commit_view(
            ViewTransform(
                state.view.mode,
                state.view.zoom,
                state.view.pan_x + x - ox,
                state.view.pan_y + y - oy,
            )
        )

    def drag_end(self) -> None:
        self._pan_origin = None

    # ==================================================================
    # 显示层变换
    # ==================================================================
    async def rotate(self, turns: int) -> None:
        if not self._state.has_image:
            return
        await self._runner.run(lambda: self._viewer.rotate(turns))
        await self._load()

    async def flip(self, *, horizontal: bool) -> None:
        if not self._state.has_image:
            return
        await self._runner.run(lambda: self._viewer.flip(horizontal=horizontal))
        await self._load()

    # ==================================================================
    # 模式：全屏 / 幻灯片 / 动图
    # ==================================================================
    def toggle_fullscreen(self) -> None:
        self._set(fullscreen=not self._state.fullscreen)

    def toggle_slideshow(self) -> None:
        running = not self._state.slideshow_on
        self._set(slideshow_on=running, paused=False if running else self._state.paused)

    def toggle_pause(self) -> None:
        if self._state.slideshow_on:
            self._set(paused=not self._state.paused)

    def set_interval(self, seconds: float) -> None:
        self._set(interval=float(seconds))

    def toggle_animation(self) -> None:
        if not self._state.has_frames:
            return
        playing, paused = self._state.anim_playing, self._state.anim_paused
        if playing and paused:
            self._set(anim_paused=False)
        elif playing:
            self._set(anim_paused=True)
        else:
            self._set(anim_playing=True, anim_paused=False)

    async def step_frame(self, delta: int) -> None:
        if not self._state.has_frames:
            return
        await self._runner.run(lambda: self._viewer.step_frame(delta))
        self._set(anim_paused=True)
        await self._refresh_frame()

    async def drive_animation(self) -> None:
        """动图循环：由视图的 effect 托管，``anim_playing`` 变假时取消。"""
        while self._state.anim_playing:
            await asyncio.sleep(self._viewer.frame_duration_ms() / 1000)
            if self._state.anim_paused or not self._state.anim_playing:
                continue
            await self._runner.run(lambda: self._viewer.step_frame(1))
            await self._refresh_frame()

    async def drive_slideshow(self) -> None:
        """幻灯片循环：同样由视图托管，``slideshow_on`` 变假时取消。"""
        while self._state.slideshow_on:
            await asyncio.sleep(self._state.interval)
            if self._state.paused:
                continue
            await self.goto(1)

    # ==================================================================
    # 导出 / 删除
    # ==================================================================
    def default_export_name(self) -> str:
        """保存对话框里的默认文件名：``原文件名_frame003.png``。"""
        stem = Path(self._info.name).stem if self._info.name else "frame"
        return f"{stem}_frame{self._frame + 1:03d}.png"

    async def export_current(self, target: Path) -> None:
        try:
            await self._runner.run(lambda: self._viewer.export_frame(target))
        except Exception as exc:                  # 磁盘满 / 无权限 / 解码失败
            self._notify(f"导出失败：{exc}", Tone.ERROR)
            return
        self._notify(f"已导出：{target.name}", Tone.SUCCESS)

    def request_delete(self) -> None:
        if not self._viewer.has_files:
            return
        name = self._info.name or "当前图片"
        self._set(
            dialog=DialogRequest(
                kind=DialogKind.DELETE,
                heading="删除图片",
                message=f"确定将「{name}」移至回收站吗？",
                confirm_label="删除",
                danger=True,
                # 非模态：看图时不该把整幅画面锁死，用户仍要看清楚删的是哪一张
                modal=False,
            )
        )

    async def _delete_current(self) -> None:
        try:
            removed = await self._runner.run(self._viewer.delete_current)
        except Exception as exc:
            self._notify(f"删除失败：{exc}", Tone.ERROR)
            await self._load()
            return
        self._notify(f"已删除：{removed}", Tone.SUCCESS)
        if self._viewer.has_files:
            await self._load()
        else:
            self._collapse()

    # ==================================================================
    # 文件关联
    # ==================================================================
    def request_association_install(self) -> None:
        if not self._viewer.association_available():
            self._notify("文件关联仅适用于 Windows", Tone.WARNING)
            return
        self._set(
            dialog=DialogRequest(
                kind=DialogKind.ASSOCIATION,
                heading="文件关联",
                message="将 ImageViewer 注册到 Windows 图片文件的「打开方式」和右键菜单。",
                confirm_label="注册",
                payload="install",
            )
        )

    def request_association_uninstall(self) -> None:
        if not self._viewer.association_available():
            self._notify("文件关联仅适用于 Windows", Tone.WARNING)
            return
        self._set(
            dialog=DialogRequest(
                kind=DialogKind.ASSOCIATION,
                heading="文件关联",
                message="将撤销 ImageViewer 写入的文件关联和右键菜单。",
                confirm_label="取消关联",
                payload="uninstall",
            )
        )

    async def show_association_status(self) -> None:
        if not self._viewer.association_available():
            self._notify("文件关联仅适用于 Windows", Tone.WARNING)
            return
        try:
            text = await self._runner.run(self._viewer.association_status)
        except Exception as exc:
            self._notify(f"读取文件关联状态失败：{exc}", Tone.ERROR)
            return
        self._set(
            dialog=DialogRequest(
                kind=DialogKind.ASSOCIATION_STATUS,
                heading="文件关联状态",
                message=text,
                selectable=True,
            )
        )

    async def _run_association(self, payload: str) -> None:
        def operate() -> None:
            if payload == "install":
                self._viewer.install_association()
            else:
                self._viewer.uninstall_association()

        try:
            await self._runner.run(operate)
        except (Exception, SystemExit) as exc:
            message = str(exc) or exc.__class__.__name__
            self._notify(f"文件关联设置失败：{message}", Tone.ERROR)
            return
        self._notify(
            "已注册 ImageViewer 文件关联" if payload == "install"
            else "已取消 ImageViewer 文件关联",
            Tone.SUCCESS,
        )

    # ==================================================================
    # 诊断
    # ==================================================================
    async def show_service_report(self) -> None:
        try:
            report = await self._runner.run(self._viewer.service_report)
        except Exception as exc:
            self._set(
                dialog=DialogRequest(
                    kind=DialogKind.SERVICE_REPORT,
                    heading="共享缓存状态",
                    message=f"读取失败：{exc}",
                )
            )
            return
        self._set(
            dialog=DialogRequest(
                kind=DialogKind.SERVICE_REPORT,
                heading="共享缓存状态",
                message=format_service_report(report),
                selectable=True,
            )
        )

    # ==================================================================
    # 对话框生命周期
    # ==================================================================
    def confirm(self) -> None:
        """当前对话框的「确认」：按种类分派，真正的动作仍走协程。"""
        dialog = self._state.dialog
        if dialog is None:
            return
        self._set(dialog=None)
        if dialog.kind is DialogKind.DELETE:
            self._scheduler.spawn(self._delete_current())
        elif dialog.kind is DialogKind.ASSOCIATION:
            self._scheduler.spawn(self._run_association(dialog.payload))

    def dismiss(self) -> None:
        if self._state.dialog is not None:
            self._set(dialog=None)

    def request_exit(self) -> None:
        """Esc / 菜单退出：有弹窗时先关弹窗，否则请求关闭窗口。"""
        if self._state.dialog is not None:
            self._set(dialog=None)
            return
        self._set(closing=True)

    # ==================================================================
    # 内部：加载与状态拼装
    # ==================================================================
    async def _load(self) -> None:
        """打开 / 翻页后的完整加载：渲染当前帧 + 预热邻居 + 补全目录。"""
        self._generation += 1
        generation = self._generation
        image = await self._runner.run(self._viewer.load_current)
        if generation != self._generation:
            return                                 # 已被更新的请求取代
        if image is None:
            self._fail()
            return
        frames = self._viewer.frames()
        animated = frames > 1
        self._present(
            image,
            has_frames=animated,
            anim_playing=animated,
            anim_paused=False if animated else self._state.anim_paused,
            show_nav=self._viewer.total > 1,
            selection=None,
        )
        if self._viewer.total == 1:
            # 首帧已上屏，再补同目录列表：出图耗时与目录里有多少文件无关
            self._scheduler.spawn(self._expand_folder(generation))

    async def _expand_folder(self, generation: int) -> None:
        listing = await self._runner.run(self._viewer.candidates)
        if generation != self._generation:
            return
        changed = await self._runner.run(lambda: self._viewer.apply_listing(listing))
        if not changed:
            return
        self._info = self._viewer.info()
        self._set(show_nav=self._viewer.total > 1, status=self._status())
        await self._runner.run(self._viewer.preload)

    async def _refresh_frame(self) -> None:
        """只重取当前帧（动图逐帧用），不重置播放标志。"""
        generation = self._generation
        image = await self._runner.run(self._viewer.render_current)
        if generation != self._generation or image is None:
            return
        self._present(image)

    def _present(self, image: DisplayImage, **changes: Any) -> None:
        """把一帧新画面写进状态：几何重新夹取、元信息刷新、倍率重算。"""
        self._info = self._viewer.info()
        self._frame = self._viewer.frame
        self._shared = image.shared
        image_size = ViewSize.of(image.width, image.height)
        view = clamp_pan(self._state.view, image_size, self._state.viewport)
        self._set(
            src=image.src,
            image_size=image_size,
            view=view,
            status=self._status(view=view, image_size=image_size),
            **changes,
        )

    def _fail(self) -> None:
        """当前图打不开：提示一句，然后把画面收回到空态。"""
        reason = self._viewer.error_text
        hint = f"（{reason}）" if reason and len(reason) < 80 else ""
        if self._viewer.has_files:
            self._notify(f"无法打开图片{hint}，已跳过", Tone.WARNING)
        self._collapse()

    def _collapse(self) -> None:
        """没有可显示的画面：清掉 src 并把状态栏留白。"""
        self._info = self._viewer.info()
        self._frame = 0
        self._shared = False
        self._set(
            src=None,
            image_size=ViewSize(),
            view=ViewTransform(),
            selection=None,
            has_frames=False,
            anim_playing=False,
            show_nav=self._viewer.total > 1,
            status=self._status(view=ViewTransform(), image_size=ViewSize()),
        )

    def _status(
        self,
        *,
        view: ViewTransform | None = None,
        image_size: ViewSize | None = None,
    ) -> StatusSnapshot:
        """状态栏文案：唯一入口，任何改变倍率 / 帧号的地方都从这里重算。"""
        state = self._state
        return format_status(
            self._info,
            view if view is not None else state.view,
            state.viewport,
            frame=self._frame,
            image_size=image_size if image_size is not None else state.image_size,
            shared=self._shared,
        )
