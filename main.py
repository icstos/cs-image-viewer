"""ImageViewer - 基于 Flet 的桌面端轻量看图软件入口。

运行：
    py -3.12 main.py
    py -3.12 main.py <图片或文件夹>

打包后由文件关联（“打开方式”）启动时，路径通过 `IMAGEVIEWER_OPEN` 环境变量传入，
详见 `viewer/file_assoc.py` 顶部的说明。

## 进程模型（共享缓存）

一个窗口 = 一个 Flutter 进程（Flet 1.0 没有多窗口 API），因此「同时看多张图」
必然是多个进程。这些进程通过一个**独立的常驻主进程**协作：

    主进程（无窗口、不加载 Flutter，约 30MB）
      ├── 缓存（磁盘 blob + 内存 LRU 解码结果）
      ├── 预加载队列
      ├── 窗口进程 #1  ── 只发请求：取显示数据、取目录列表
      ├── 窗口进程 #2  ── 命中缓存时不必解码，直接读已编码好的文件
      └── 窗口进程 #N

窗口进程**永远只做客户端**：打开应用时先找主进程，找不到就用应用自带的
CPython 运行时拉起一个（`viewer/service/spawn.py`），然后再连上去。所以
「主进程 + 多个窗口进程」是常态，而不是「谁先启动谁兼职当服务」。
只有运行时不可用（例如装在只读目录、python.exe 落不了地）时才退回
「第一个窗口兼职宿主」，功能一致，只是主进程不独立。

主进程在所有窗口关闭后仍常驻，空闲 `CSIV_MASTER_IDLE` 秒（默认 900）后自行退出；
`CSIV_MASTER_IDLE=0` 表示永不退出。

`IMAGEVIEWER_ROLE=daemon` 时不建窗口，直接跑主进程（打包态也支持）。

启动顺序（消除「左上角白屏一闪」的关键）：
    1. 客户端窗口以**隐藏**状态创建（开发模式 `FLET_APP_HIDDEN`，打包版由
       `[tool.flet.app] hide_window_on_start` 控制），未布局的窗口不会出现在屏幕上，
       因此不存在白屏、也不存在先落在左上角再跳到中间的过程；
    2. Python 侧构建完 UI 树后，**一次性**下发窗口尺寸 / 最小尺寸 / 主题 / 底色；
    3. 窗口仍在隐藏状态下居中，并把标题栏切成深色（DWM）；
    4. 最后一步才显示窗口——用户第一眼看到的就是渲染完成、位于屏幕中央的界面。

计时开关：`CSIV_TIMING=1` 会把各阶段耗时打印到控制台（打包版写入 console.log）。
冒烟测试：`CSIV_SMOKE=1` 启动数秒后自动退出。
共享缓存开关：`CSIV_SHARED=0` 关闭共享（退回单进程本地解码，用于对比性能）。
"""
from __future__ import annotations

import asyncio
import atexit
import os
import sys
import time
from pathlib import Path

import flet as ft

from viewer import app as app_module
from viewer.app import ImageViewerApp
from viewer.service import spawn as service_spawn
from viewer.service.client import ServiceLink, default_link, shutdown_default_link
from viewer.shared_manager import RemoteImageManager

# 文件关联启动器写入的通道（与 viewer/file_assoc.py 保持一致）
OPEN_ENV = "IMAGEVIEWER_OPEN"
REQUEST_FILE = Path(os.environ.get("LOCALAPPDATA", "")) / "ImageViewer" / "open-request.txt"
REQUEST_MAX_AGE = 15.0  # 秒：超过视为上次残留，直接丢弃

# 进程角色（与 viewer/service/spawn.py 保持一致）
ROLE_ENV = "IMAGEVIEWER_ROLE"

# 窗口与配色：与 viewer/app.py 中的 BG_COLOR 保持一致，
# 这样即使是窗口底色（漏画区域）也不会闪出白色。
WINDOW_WIDTH = 1100
WINDOW_HEIGHT = 760
WINDOW_MIN_WIDTH = 640
WINDOW_MIN_HEIGHT = 480
WINDOW_TITLE = "ImageViewer"
BG_COLOR = "#141414"

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
    """把异常写进 `%LOCALAPPDATA%\\ImageViewer\\logs\\crash.log`。

    打包版没有控制台，Python 的 traceback 会随进程一起消失。写文件是唯一
    能让"双击没反应/窗口闪一下就没了"变成可诊断问题的办法。
    """
    import traceback

    try:
        from viewer.service import paths

        paths.ensure(paths.log_dir())
        with open(paths.log_dir() / "crash.log", "a", encoding="utf-8") as fh:
            fh.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {where} ===\n")
            traceback.print_exc(file=fh)
    except Exception:
        pass


def _take_request_file() -> str | None:
    """取走启动器留下的“打开请求”文件（环境变量通道的兜底）。

    无论是否采用都删除，避免残留影响后续手动启动；只有足够新的文件才认。
    """
    try:
        if not REQUEST_FILE.is_file():
            return None
        age = time.time() - REQUEST_FILE.stat().st_mtime
        text = REQUEST_FILE.read_text(encoding="utf-16").strip()
    except (OSError, UnicodeError):
        return None
    finally:
        try:
            REQUEST_FILE.unlink()
        except OSError:
            pass
    return text if text and age <= REQUEST_MAX_AGE else None


def _resolve_start_path() -> tuple[str | None, str]:
    """确定启动时要打开的文件/文件夹，返回 (绝对路径, 来源说明)。

    三条通道，按优先级：

    1. `IMAGEVIEWER_OPEN` 环境变量 —— 打包后“打开方式”的主通道。
       `flet build` 产物的 Dart 外壳会把任何命令行参数当成“开发模式”的页面地址，
       关联命令直接传 `"%1"` 会导致 Python 端完全不启动，因此改由启动器写环境变量
       并以无参数方式拉起 exe。
    2. 请求文件 —— 同一启动器的兜底通道（环境变量被系统策略拦截时仍可用）。
    3. 命令行参数 —— 开发期 `py -3.12 main.py <路径>` 及控制台脚本 `imageviewer <路径>`。

    不存在的路径直接跳过（关联表里的历史记录可能已指向被删除的文件）。
    """
    candidates: list[tuple[str | None, str]] = [
        (os.environ.get(OPEN_ENV), f"env:{OPEN_ENV}"),
        (_take_request_file(), "request-file"),
        *((arg, "argv") for arg in sys.argv[1:]),
    ]
    for raw, source in candidates:
        if not raw:
            continue
        candidate = os.path.abspath(raw.strip().strip('"'))
        if os.path.exists(candidate):
            return candidate, source
    return None, "none"


# ---------------------------------------------------------------------------
# 窗口外观（Windows 原生细节，纯 ctypes，无额外依赖、无跨平台副作用）
# ---------------------------------------------------------------------------

_WIN_CLASS = "FLUTTER_RUNNER_WIN32_WINDOW"


def _flutter_hwnds() -> list[int]:
    """枚举本进程内的 Flutter 顶层窗口句柄（隐藏状态下同样能取到）。"""
    if os.name != "nt":
        return []
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        pid = os.getpid()
        found: list[int] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _collect(hwnd, _lparam):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value != pid:
                return True
            name = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, name, 64)
            if name.value == _WIN_CLASS:
                found.append(hwnd)
            return True

        user32.EnumWindows(_collect, 0)
        return found
    except Exception:
        return []


def _apply_dark_title_bar() -> None:
    """把标题栏切成深色（DWM），避免深色界面顶一条白边。

    必须在显示窗口之前调用：窗口一旦显示，浅色标题栏就会作为「白」被看到；
    而深色标题栏可以让整个窗口从出现的第一帧就是统一的深色。
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        dwmapi = ctypes.windll.dwmapi
        enabled = ctypes.c_int(1)
        for hwnd in _flutter_hwnds():
            # 20 = Windows 11 起的 DWMWA_USE_IMMERSIVE_DARK_MODE，19 = 旧版编号
            for attribute in (20, 19):
                dwmapi.DwmSetWindowAttribute(
                    hwnd, attribute, ctypes.byref(enabled), ctypes.sizeof(enabled)
                )
    except Exception:
        pass


def _ensure_visible() -> None:
    """兜底：窗口若因任何异常仍处于隐藏状态，直接由 Win32 显示出来。"""
    if os.name != "nt":
        return
    try:
        import ctypes

        user32 = ctypes.windll.user32
        for hwnd in _flutter_hwnds():
            if not user32.IsWindowVisible(hwnd):
                user32.ShowWindow(hwnd, 1)      # SW_SHOWNORMAL
            user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def _watchdog_start(delay: float = 3.0) -> None:
    """看门狗：万一显示流程卡住/抛错，也不能留下一个「看不见的进程」。"""
    import threading

    def _later() -> None:
        time.sleep(delay)
        if any(not _is_visible(hwnd) for hwnd in _flutter_hwnds()):
            _ensure_visible()

    threading.Thread(target=_later, name="csiv-show-watchdog", daemon=True).start()


def _is_visible(hwnd: int) -> bool:
    try:
        import ctypes

        return bool(ctypes.windll.user32.IsWindowVisible(hwnd))
    except Exception:
        return True


# ---------------------------------------------------------------------------
# 页面入口
# ---------------------------------------------------------------------------

def _configure_theme(page: ft.Page) -> None:
    """主题与字体（深色 + 本地普惠体）。放在 render 之前，首帧即最终排版。"""
    page.theme_mode = ft.ThemeMode.DARK
    # 字体挂在 Theme 上：菜单、状态栏、弹窗、提示等所有控件统一继承，
    # 不需要逐个 ft.Text 指定 font_family；Material 图标用的是独立图标字体，不受影响。
    page.theme = ft.Theme(font_family=FONT_FAMILY)


def _configure_window(page: ft.Page) -> None:
    """窗口属性（尺寸/最小尺寸/底色）集中下发，此时窗口仍是隐藏的。"""
    # render 有可能把主题重置回默认，这里再钉一次深色模式（字体仍由 page.theme 决定）
    page.theme_mode = ft.ThemeMode.DARK
    window = page.window
    window.min_width = WINDOW_MIN_WIDTH
    window.min_height = WINDOW_MIN_HEIGHT
    window.width = WINDOW_WIDTH
    window.height = WINDOW_HEIGHT
    # 窗口底衬与界面同色：即使某帧漏画，露出的也是「深色」而不是白色
    window.bgcolor = BG_COLOR


async def _reveal_window(page: ft.Page) -> None:
    """居中 + 深色标题栏 + 显示：三步都在窗口不可见时完成，用户只看到最终结果。"""
    _watchdog_start()
    try:
        await page.window.center()          # 隐藏状态下移动，用户看不到任何跳动
        _apply_dark_title_bar()             # 显示前定好标题栏配色
        page.window.visible = True          # 唯一一次「出现」
        page.window.focused = True          # 同时抢焦点：键盘操作立刻可用
        page.update()
    except Exception as exc:                # 任何意外都不能让程序隐身
        print(f"[boot] reveal failed: {exc!r}", flush=True)
        _ensure_visible()
        return
    _mark("window visible")


async def main(page: ft.Page) -> None:
    """Flet 页面入口：任何异常都先落盘再抛出（打包版没有控制台）。"""
    try:
        await _main(page)
    except BaseException:
        log_crash("main(page)")
        raise


async def _main(page: ft.Page) -> None:
    page.title = WINDOW_TITLE
    page.bgcolor = BG_COLOR
    page.padding = 0
    # 字体必须在 render 之前登记：首帧就用上本地字体，避免先按系统字体排版再跳字。
    page.fonts = {FONT_FAMILY: FONT_FILE}
    _configure_theme(page)
    # 在 render 前捕获初始客户端尺寸（page.width/height 可能陈旧）
    page.on_resize = lambda e: app_module.set_initial_size(e.width, e.height)

    start_path, start_source = _resolve_start_path()
    if start_path:
        app_module.START_PATH = start_path
    os.environ.pop(OPEN_ENV, None)          # 用掉即清，避免泄漏给子进程
    _mark("start path resolved")

    # 共享服务要在构建 UI 之前就位：启动时那张图的首帧直接吃缓存/由服务渲染，
    # 不必先本地解码再"升级"。限时 1.5 秒，失败就用本地解码，绝不拖慢启动。
    _bootstrap_service(web=page.web)
    _mark("service bootstrapped")

    page.render(ImageViewerApp)             # 构建 UI 树（尚未推送到客户端）
    _mark("ui tree built")

    if page.web:
        # Web 调试模式：浏览器尺寸不受 Flet 窗口属性控制
        page.update()
        return

    # 注意：窗口尺寸属性须在 render 之后设置，否则会被重置为默认值。
    # 尺寸与 UI 树在同一次 update 中下发，窗口隐藏期间完成，避免「先小窗后跳大」。
    _configure_window(page)
    page.update()
    _mark("ui pushed")

    await _reveal_window(page)

    if os.environ.get("CSIV_SMOKE"):        # 冒烟测试模式：数秒后自动退出
        page.run_task(_smoke_run, page, start_source, start_path)


def _bootstrap_service(web: bool) -> ServiceLink | None:
    """接入共享服务：**先找主进程，找不到就拉起一个**，窗口始终只做客户端。

    1. 已有常驻主进程 → 直接接上（几十毫秒）；
    2. 没有 → 用应用自带的 CPython 运行时拉起主进程（无窗口、约 30MB），再连；
    3. 运行时不可用（只读目录等）→ 本窗口兼职宿主，功能不变；
    4. Web 调试 / `CSIV_SHARED=0` → 不启用共享，本地解码。
    """
    if web:
        return None                          # 浏览器里没有"多进程"这回事
    link = default_link()                    # 进程级单例：诊断/菜单读的是同一条连接
    if not link.enabled:
        _mark("service disabled (local decode)")
        return None

    link.master_starter = service_spawn.launch_daemon
    mode = "attached"
    # 探测预算给得很小：主进程在的话，握手只要几毫秒；不在的话没必要干等
    ok = link.try_connect(0.2)                        # 1) 已有主进程
    if not ok:
        started = service_spawn.launch_daemon()       # 2) 拉起主进程
        if started:
            ok = link.try_connect(4.0)
            mode = "master started"
    if not ok:
        ok = link.try_connect(0.5, allow_host=True)    # 3) 兜底：本窗口兼职宿主
        mode = "self-host" if ok else "unavailable"
        if ok and not service_spawn.service_available():
            mode += " (independent runtime unavailable)"

    app_module.set_manager_factory(lambda: RemoteImageManager(link))
    atexit.register(shutdown_default_link)
    _mark(f"service {mode if ok else 'unavailable (local decode)'}")
    return link


async def _smoke_run(page: ft.Page, start_source: str, start_path: str | None) -> None:
    """CSIV_SMOKE=1 时的自检输出与自动退出（正常启动不执行）。"""
    print(f"[smoke] argv={sys.argv[1:]} start={start_source} start_path={start_path}", flush=True)
    print(f"[smoke] role={os.environ.get(ROLE_ENV, 'window')}", flush=True)
    await asyncio.sleep(2)
    print(
        f"[smoke] window={page.window.width}x{page.window.height} "
        f"fullscreen={page.window.full_screen}",
        flush=True,
    )
    try:
        status = await asyncio.to_thread(_smoke_status)
        print(f"[smoke] service={status}", flush=True)
    except Exception as exc:
        print(f"[smoke] service=error {exc!r}", flush=True)
    await asyncio.sleep(2)
    print("[smoke] closing window", flush=True)
    try:
        await page.window.close()
    except RuntimeError:
        pass        # 窗口已关闭 → 会话断开，flet 的 close() 会抛 "Session closed"


def _smoke_status() -> dict:
    """冒烟模式：把服务角色与缓存统计打出来（验证共享是否真的生效）。"""
    from viewer.service.client import default_link

    return default_link().status()


def run() -> None:
    """启动入口：`py -3.12 main.py` 与 `imageviewer` 控制台脚本共用。"""
    if os.environ.get(ROLE_ENV) == "daemon":
        # 角色分发：不建窗口，只跑共享服务（等价于 `python -m viewer.daemon`）
        from viewer.daemon import main as daemon_main

        raise SystemExit(daemon_main([]))
    if os.environ.get("CSIV_WEB"):
        # 开发验证模式：以 Web 方式运行（同一套代码，便于截图/交互调试）
        ft.run(main, view=ft.AppView.WEB_BROWSER, port=8550)
        return
    # FLET_APP_HIDDEN：桌面客户端先建窗口但不显示，等 Python 把 UI 布置好再亮出来。
    # 打包版没有这个参数（Python 嵌在 Flutter 进程内），由 pyproject 的
    # [tool.flet.app] hide_window_on_start 达成同样效果。
    try:
        ft.run(main, view=ft.AppView.FLET_APP_HIDDEN)
    except BaseException:
        log_crash("ft.run")
        raise


if __name__ == "__main__":
    run()
