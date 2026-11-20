"""ImageViewer —— 桌面端轻量看图软件（基于 cs-ui 声明式 UI）。

运行::

    py -3.12 main.py                 # 空窗口
    py -3.12 main.py <图片或文件夹>    # 直接打开

本文件是**唯一的组合根**：它认识所有具体实现，因此其它文件都不必互相认识。
装配顺序即依赖方向::

    infra   →   service   →   viewmodel   →   view
    （PIL/共享缓存/注册表）  （画册用例）   （界面状态）     （控件树）

换掉任何一层（本地解码换成共享缓存、回收站换成别的实现）都只改这里一行。

## 进程模型（共享缓存）

Flet 1.0 没有多窗口 API，一个窗口就是一个 Flutter 进程。因此「同时看多张图」
必然是多个进程，它们通过一个**独立的常驻主进程**协作::

    主进程（无窗口、不加载 Flutter，约 30MB）
      ├── 缓存（磁盘 blob + 内存 LRU 解码结果）
      ├── 预加载队列
      ├── 窗口进程 #1  ── 只发请求：取显示数据、取目录列表
      ├── 窗口进程 #2  ── 命中缓存时不必解码，直接读已编码好的文件
      └── 窗口进程 #N

窗口进程**永远只做客户端**：启动时先找主进程，找不到就用应用自带的 CPython
运行时拉起一个（``infra/procs.py``），再连上去。只有运行时不可用（只读目录等）
时才退回「第一个窗口兼职宿主」，功能一致，只是主进程不独立。

## 启动顺序（消除「左上角白屏一闪」）

1. 窗口以**隐藏**状态创建（开发模式 ``FLET_APP_HIDDEN``，打包版由
   ``[tool.flet.app] hide_window_on_start`` 控制），未布局的窗口不会出现在屏幕上；
2. Python 侧构建完 UI 树后，**一次性**下发窗口尺寸 / 最小尺寸 / 主题 / 底色；
3. 窗口仍在隐藏状态下居中，并把标题栏切成深色（DWM）；
4. 最后一步才显示窗口 —— 用户第一眼看到的就是渲染完成、位于屏幕中央的界面。

## 环境变量

============================ ==================================================
``IMAGEVIEWER_ROLE=daemon``  不建窗口，直接跑共享缓存主进程
``IMAGEVIEWER_OPEN``         启动器（文件关联）写入的打开请求
``CSIV_SHARED=0``            关闭共享缓存，退回单进程本地解码（用于对比性能）
``CSIV_MASTER_IDLE``         主进程空闲多少秒后退出（默认 900；0 = 永不退出）
``IMAGEVIEWER_ROLE=hot``     预热窗口：装配完整 UI 后隐藏待命，领到打开请求才显示
``CSIV_HOT_POOL=0``          关闭主进程的预热窗口池（默认维持 1 个）
``CSIV_TIMING=1``            打印各启动阶段耗时
``CSIV_SMOKE=1``             冒烟模式：数秒后自动退出
``CSIV_WEB=1``               以 Web 方式启动（同一套代码，便于截图调试）
============================ ==================================================
"""

from __future__ import annotations

import asyncio
import atexit
import os
import sys
import time
from pathlib import Path

# src 布局必需：隔离打包环境（flet build / PyInstaller 等）不会执行 pip install -e .，
# 包被编译到 <app>/src/cs_image_viewer/，而运行时 sys.path 只有 <app>/ 根、不含 src。
# 这段同时覆盖开发态与打包态；删掉即打包后 ModuleNotFoundError。
_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path and (_SRC / "cs_image_viewer").is_dir():
    sys.path.insert(0, str(_SRC))

import ui

from cs_image_viewer.infra import dispatch, paths, procs
from cs_image_viewer.infra.association import WindowsAssociation
from cs_image_viewer.infra.concurrency import AsyncioRunner
from cs_image_viewer.infra.display import LocalDisplay, SharedDisplay
from cs_image_viewer.infra.platform import SystemPlatform, ctrl_pressed
from cs_image_viewer.infra.procs import WindowLauncher
from cs_image_viewer.infra.shared.client import (
    ServiceLink,
    default_link,
    shutdown_default_link,
)
from cs_image_viewer.infra.trash import TrashBin
from cs_image_viewer.infra.windows import (
    apply_dark_title_bar,
    ensure_visible,
    start_show_watchdog,
)
from cs_image_viewer.service import ViewerService
from cs_image_viewer.view import build_app, design, remember_window_size
from cs_image_viewer.view.scheduler import PageTaskScheduler
from cs_image_viewer.viewmodel import ViewerViewModel

# 文件关联启动器写入的通道（与 infra/association.py、infra/procs.py、infra/dispatch.py 保持一致）
OPEN_ENV = "IMAGEVIEWER_OPEN"

# 进程角色（与 infra/procs.py 保持一致）
ROLE_ENV = "IMAGEVIEWER_ROLE"

WINDOW_WIDTH = 1100
WINDOW_HEIGHT = 760
WINDOW_MIN_WIDTH = 640
WINDOW_MIN_HEIGHT = 480
WINDOW_TITLE = "ImageViewer"

# 窗口底色取色表的 surface：与 page.bgcolor、主题下发的画布色同族，
# 隐藏窗口期间的漏画帧也不会闪出白色。
BG_COLOR = design.C_SURFACE

# 全局字体：阿里普惠体，随应用一起打包，不依赖目标机器装了什么中文字体。
# 路径相对 assets 目录（flet build 会把 assets/ 整体拷进安装包），
# 因此开发运行与打包后的路径写法完全一致。
FONT_FAMILY = "AlibabaPuHuiTi"
FONT_FILE = "/fonts/AlibabaPuHuiTi-3-55-Regular.otf"

# 启动计时起点（模块导入即开始，覆盖 Python 端全部启动开销）
_T0 = time.perf_counter()
_TIMING = bool(os.environ.get("CSIV_TIMING"))


def _mark(label: str) -> None:
    """打印启动阶段耗时（仅 CSIV_TIMING 开启时输出，正常启动零开销）。"""
    if _TIMING:
        print(f"[boot] {label} +{(time.perf_counter() - _T0) * 1000:.0f}ms", flush=True)


def log_crash(where: str) -> None:
    """把异常写进 ``%LOCALAPPDATA%\\ImageViewer\\logs\\crash.log``。

    打包版没有控制台，Python 的 traceback 会随进程一起消失。写文件是唯一
    能让「双击没反应 / 窗口闪一下就没了」变成可诊断问题的办法。
    """
    import traceback

    try:
        paths.ensure(paths.log_dir())
        with open(paths.log_dir() / "crash.log", "a", encoding="utf-8") as handle:
            handle.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {where} ===\n")
            traceback.print_exc(file=handle)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 启动参数
# ---------------------------------------------------------------------------
def _resolve_start_path(*, take_request_file: bool = True) -> tuple[str | None, str]:
    """确定启动时要打开的文件 / 文件夹，返回 ``(绝对路径, 来源说明)``。

    三条通道，按优先级：

    1. ``IMAGEVIEWER_OPEN`` 环境变量 —— 打包后「打开方式」的主通道。
       ``flet build`` 产物的 Dart 外壳会把任何命令行参数当成「开发模式」的页面
       地址，关联命令直接传 ``"%1"`` 会导致 Python 端完全不启动，因此改由启动器
       写环境变量并以**无参数**方式拉起 exe。
    2. 请求文件 —— 由 ``dispatch.take_request()`` **原子认领**：并发下只有一个
       进程能拿到，从根上杜绝「冷启动 exe + 预热窗口同时领单」开出多个窗口。
       环境变量通道被系统策略拦截时仍可用。
    3. 命令行参数 —— 开发期 ``py -3.12 main.py <路径>`` 及控制台脚本。

    ``take_request_file=False`` 给**预热窗口启动**用：它只通过 ``_hot_standby``
    领单，若在装配阶段就消费了请求文件，会加载图片却保持隐藏，导致这次双击丢单。

    不存在的路径直接跳过（关联表里的历史记录可能已指向被删除的文件）。
    """
    candidates: list[tuple[str | None, str]] = [
        (os.environ.get(OPEN_ENV), f"env:{OPEN_ENV}"),
        (dispatch.take_request() if take_request_file else None, "request-file"),
        *((argument, "argv") for argument in sys.argv[1:]),
    ]
    for raw, source in candidates:
        if not raw:
            continue
        candidate = os.path.abspath(raw.strip().strip('"'))
        if os.path.exists(candidate):
            return candidate, source
    return None, "none"


# ---------------------------------------------------------------------------
# 窗口外观
# ---------------------------------------------------------------------------
def _configure_theme(page: ui.Page) -> None:
    """主题与字体：交给 ui 的主题运行时一次下发。

    ``apply_theme`` 会同时写 ``page.theme`` / ``page.dark_theme`` 两棵由品牌色算出的
    完整色板，并设置画布底色 —— cs-ui 组件里的 ``ON_SURFACE_VARIANT`` 这类角色名
    由 Flutter 按这棵树解析，所以深色界面里文字不会变成浅色主题下的深灰。
    字体挂在 Theme 上，菜单 / 状态栏 / 弹窗 / 提示统一继承。
    """
    state = ui.theme_state()
    state.set_mode(design.MODE)
    state.set_primary(design.BRAND_PRIMARY)
    state.font_family = FONT_FAMILY
    ui.apply_theme(page, state)


def _configure_window(page: ui.Page) -> None:
    """窗口属性（尺寸 / 最小尺寸 / 底色）集中下发，此时窗口仍是隐藏的。"""
    # render 有可能把主题重置回默认，这里再钉一次深色模式。
    page.theme_mode = ui.ThemeMode.DARK
    window = page.window
    window.min_width = WINDOW_MIN_WIDTH
    window.min_height = WINDOW_MIN_HEIGHT
    window.width = WINDOW_WIDTH
    window.height = WINDOW_HEIGHT
    # 窗口底衬与界面同色：即使某帧漏画，露出的也是「深色」而不是白色
    window.bgcolor = BG_COLOR


async def _reveal_window(page: ui.Page) -> None:
    """居中 + 深色标题栏 + 显示：三步都在窗口不可见时完成，用户只看到最终结果。"""
    start_show_watchdog(title=WINDOW_TITLE)
    try:
        await page.window.center()          # 隐藏状态下移动，用户看不到任何跳动
        apply_dark_title_bar(WINDOW_TITLE)  # 显示前定好标题栏配色
        page.window.visible = True          # 唯一一次「出现」
        page.window.focused = True          # 同时抢焦点：键盘操作立刻可用
        page.update()
    except Exception as exc:                # 任何意外都不能让程序隐身
        print(f"[boot] reveal failed: {exc!r}", flush=True)
        ensure_visible(WINDOW_TITLE)
        return
    _mark("window visible")


# ---------------------------------------------------------------------------
# 共享缓存装配
# ---------------------------------------------------------------------------
def _bootstrap_service(web: bool) -> ServiceLink | None:
    """接入共享服务：**先找主进程，找不到就拉起一个**，窗口始终只做客户端。

    1. 已有常驻主进程 → 直接接上（几十毫秒）；
    2. 没有 → 用应用自带的 CPython 运行时拉起主进程（无窗口、约 30MB），再连；
    3. 运行时不可用（只读目录等）→ 本窗口兼职宿主，功能不变；
    4. Web 调试 / ``CSIV_SHARED=0`` → 不启用共享，本地解码。
    """
    if web:
        return None                          # 浏览器里没有「多进程」这回事
    link = default_link()                    # 进程级单例：诊断/菜单读的是同一条连接
    if not link.enabled:
        _mark("service disabled (local decode)")
        return None

    link.master_starter = procs.launch_daemon
    mode = "attached"
    ok = link.try_connect(0.2)                        # 1) 已有主进程
    if not ok:
        started = procs.launch_daemon()               # 2) 拉起主进程
        if started:
            ok = link.try_connect(4.0)
            mode = "master started"
    if not ok:
        ok = link.try_connect(0.5, allow_host=True)    # 3) 兜底：本窗口兼职宿主
        mode = "self-host" if ok else "unavailable"
        if ok and not procs.service_available():
            mode += " (independent runtime unavailable)"

    atexit.register(shutdown_default_link)
    _mark(f"service {mode if ok else 'unavailable (local decode)'}")
    return link if ok else None


def build_view_model(page: ui.Page, *, start_path: str | None) -> ViewerViewModel:
    """装配一条完整的依赖链，返回可交给视图的 viewmodel。

    注意这里的**顺序**：基础设施 → 业务 → 视图模型。谁都不需要知道下游是谁的
    具体类型，除了这一行一行的构造。
    """
    link = _bootstrap_service(web=page.web)
    display = SharedDisplay(link) if link is not None else LocalDisplay(cache_size=3)
    service = ViewerService(
        display=display,
        trash=TrashBin(),
        association=WindowsAssociation(),
        platform=SystemPlatform(),
        launcher=WindowLauncher(),
    )
    return ViewerViewModel(
        service,
        AsyncioRunner(),
        PageTaskScheduler(page),
        start_path=start_path,
    )


# ---------------------------------------------------------------------------
# 页面入口
# ---------------------------------------------------------------------------
async def main(page: ui.Page) -> None:
    """Flet 页面入口：任何异常都先落盘再抛出（打包版没有控制台）。"""
    try:
        await _main(page)
    except BaseException:
        log_crash("main(page)")
        raise


async def _main(page: ui.Page) -> None:
    page.title = WINDOW_TITLE
    page.bgcolor = BG_COLOR
    page.padding = 0
    # 字体必须在 render 之前登记：首帧就用上本地字体，避免先按系统字体排版再跳字。
    page.fonts = {FONT_FAMILY: FONT_FILE}
    _configure_theme(page)
    # 在 render 前捕获初始客户端尺寸（page.width/height 可能陈旧）
    page.on_resize = lambda event: remember_window_size(event.width, event.height)

    is_hot = os.environ.get(ROLE_ENV) == procs.HOT_ROLE
    # 预热窗口在装配阶段绝不能消费请求文件：它还没进入 _hot_standby 轮询，
    # 领了也无法显示，会把这次双击变成一次丢单。
    start_path, start_source = _resolve_start_path(take_request_file=not is_hot)
    os.environ.pop(OPEN_ENV, None)          # 用掉即清，避免泄漏给子进程
    _mark("start path resolved")

    # 共享服务要在构建 UI 之前就位：启动时那张图的首帧直接吃缓存/由服务渲染，
    # 不必先本地解码再「升级」。限时 1.5 秒，失败就用本地解码，绝不拖慢启动。
    vm = build_view_model(page, start_path=start_path)
    _mark("service bootstrapped")

    page.render(build_app(vm, ctrl_probe=ctrl_pressed))   # 构建 UI 树（尚未推送）
    _mark("ui tree built")

    if page.web:
        page.update()                       # 浏览器尺寸不受 Flet 窗口属性控制
        return

    # 窗口尺寸属性须在 render 之后设置，否则会被重置为默认值。
    # 尺寸与 UI 树在同一次 update 中下发，窗口隐藏期间完成，避免「先小窗后跳大」。
    _configure_window(page)
    page.update()
    _mark("ui pushed")

    if is_hot:
        # 预热窗口：UI/服务/缓存已全部就绪，但不显示。转入待命，
        # 启动器双击时把路径投进来，这里加载后立即显示 —— 跳过全部冷启动开销。
        page.run_task(_hot_standby, page, vm)
        return

    await _reveal_window(page)

    if os.environ.get("CSIV_SMOKE"):        # 冒烟测试模式：数秒后自动退出
        page.run_task(_smoke_run, page, start_source, start_path)


# ---------------------------------------------------------------------------
# 冒烟
# ---------------------------------------------------------------------------
async def _smoke_run(page: ui.Page, start_source: str, start_path: str | None) -> None:
    """``CSIV_SMOKE=1`` 时的自检输出与自动退出（正常启动不执行）。"""
    print(f"[smoke] argv={sys.argv[1:]} start={start_source} start_path={start_path}", flush=True)
    print(f"[smoke] role={os.environ.get(ROLE_ENV, 'window')}", flush=True)
    await asyncio.sleep(2)
    print(
        f"[smoke] window={page.window.width}x{page.window.height} "
        f"client={page.width}x{page.height} fullscreen={page.window.full_screen}",
        flush=True,
    )
    try:
        status = await asyncio.to_thread(lambda: default_link().status())
        print(f"[smoke] service={status}", flush=True)
    except Exception as exc:
        print(f"[smoke] service=error {exc!r}", flush=True)
    await asyncio.sleep(2)
    print("[smoke] closing window", flush=True)
    try:
        await page.window.close()
    except RuntimeError:
        pass        # 窗口已关闭 → 会话断开，flet 的 close() 会抛 "Session closed"


async def _hot_standby(page: ui.Page, vm: ViewerViewModel) -> None:
    """预热窗口待命循环：装配完成后隐藏，轮询启动器投递的打开请求。

    领单流程（对应 infra/dispatch.py 的协议）：
    1. 登记 ``hot-idle``（只写一个，多个预热窗口自然互斥）；
    2. 每 ~120ms 看一次 ``open-request.txt``，发现新请求立即写 ack + 删 idle；
    3. 用 viewmodel 的常规 ``open_file`` / ``open_folder`` 加载（复用首帧全部逻辑）；
    4. 显示窗口、抢焦点；若已开着窗口则只切换内容；
    5. 通知 daemon 补一个预热窗口，维持池中始终有一个待命。

    收到请求前窗口始终隐藏，Do not call ``_reveal_window`` until a request arrives。
    """
    import asyncio

    pid = os.getpid()
    dispatch.mark_idle(pid)
    dispatch.clear_stale_acks()
    _mark("hot standby (hidden)")
    seen_mtime = dispatch.request_path().stat().st_mtime if dispatch.request_path().is_file() else 0.0
    try:
        while True:                         # 窗口关闭时 Flet 会取消该任务（CancelledError）
            await asyncio.sleep(0.12)
            dispatch.mark_idle(pid)         # 心跳：刷新 mtime，daemon 据此判断我还活着
            path = dispatch.request_path()
            try:
                mtime = path.stat().st_mtime if path.is_file() else 0.0
            except OSError:
                mtime = 0.0
            # 请求文件必须是「新的」：mtime 变化，且内容非空、未过期
            if mtime <= seen_mtime or mtime == 0.0:
                continue
            seen_mtime = mtime
            target = dispatch.take_request(max_age=dispatch.REQUEST_MAX_AGE)
            if not target:
                continue
            # 领单成功：写回执 + 注销 idle，让启动器/其它窗口知道这单已被接走
            dispatch.write_ack(pid, target)
            dispatch.clear_idle(pid)
            candidate = os.path.abspath(target.strip().strip('"'))
            if not os.path.exists(candidate):
                continue
            try:
                if os.path.isdir(candidate):
                    await vm.open_folder(candidate)
                else:
                    await vm.open_file(candidate)
            except Exception as exc:
                print(f"[hot] load failed: {exc!r}", flush=True)
                log_crash("hot load")
            await _reveal_window(page)
            _mark("hot promoted")
            # 请求 daemon 补位：写标记后交给 daemon 循环处理（它持有 launch_hot_window）
            dispatch.request_replenish()
            return
    except asyncio.CancelledError:
        raise
    finally:
        dispatch.clear_idle(pid)


def run() -> None:
    """启动入口：``py -3.12 main.py`` 与 ``imageviewer`` 控制台脚本共用。"""
    if os.environ.get(ROLE_ENV) == "daemon":
        # 角色分发：不建窗口，只跑共享服务（等价于 python -m cs_image_viewer.infra.shared.daemon）
        from cs_image_viewer.infra.shared.daemon import main as daemon_main

        raise SystemExit(daemon_main([]))
    if os.environ.get("CSIV_WEB"):
        # 开发验证模式：以 Web 方式运行（同一套代码，便于截图/交互调试）
        ui.run(main, view=ui.AppView.WEB_BROWSER, port=8550)
        return
    # FLET_APP_HIDDEN：桌面客户端先建窗口但不显示，等 Python 把 UI 布置好再亮出来。
    # 打包版没有这个参数（Python 嵌在 Flutter 进程内），由 pyproject.toml 的
    # [tool.flet.app] hide_window_on_start 达成同样效果。
    try:
        ui.run(main, view=ui.AppView.FLET_APP_HIDDEN)
    except BaseException:
        log_crash("ui.run")
        raise


if __name__ == "__main__":
    run()
