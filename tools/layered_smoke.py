"""分层自检 —— **不开窗口**，把架构契约与业务规则变成可执行的断言。

跑法::

    py -3.12 tools/layered_smoke.py

覆盖六组契约：

============ ==========================================================
依赖方向     五层单向依赖、models 纯净、界面层不直接 import flet
模型层       几何 / 格式白名单 / 动作枚举 / 状态派生
服务层       画册规则：补全目录不换图、跳过坏图、切图复位、删除落位
视图模型     文案排版、动作分发、对话框与输入请求的状态机
视图层       菜单 / 状态栏 / 画布层级 / 玻璃按钮 / 键位表
装配         端口实现完整性、pyproject 与设计层不漂移
============ ==========================================================

断言写「规则」不写「色值」，也不写死文案：换品牌色、改措辞时这里不该假报警。
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"                       # src 布局：包位于 src/imageviewer/
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SRC))

#: 真实存在（内容无关）的图片占位目录：``Gallery`` 会做 ``is_file()`` 与后缀校验，
#: 因此断言需要真路径，但解码全部由假 provider 接管 —— 不产生任何耗时。
TMP = Path(tempfile.mkdtemp(prefix="csiv-smoke-"))


def _touch(*names: str) -> list[Path]:
    """在临时目录里建空文件，返回路径。"""
    made = []
    for name in names:
        path = TMP / name
        path.write_bytes(b"")
        made.append(path)
    return made

import ui

from imageviewer import models as M
from imageviewer.models import geometry as G
from imageviewer.service import DisplayProvider, FileTrash, ViewerService
from imageviewer.service.gallery import Gallery
from imageviewer.view import bars, canvas, design, dialogs, glass, keymap, kit, menu
from imageviewer.view.app import build_app
from imageviewer.view.viewport import ImageViewport
from imageviewer.viewmodel import ViewerViewModel
from imageviewer.viewmodel.formatting import (
    format_service_report,
    format_size,
    format_status,
)

PASS = 0
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASS
    if condition:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL.append(f"{label}: {detail}")
        print(f"  FAIL {label} — {detail}")


def section(name: str) -> None:
    print(f"\n[{name}]")


# ---------------------------------------------------------------------------
# 假实现：一律放在这层，业务层不需要知道自己被谁测着
# ---------------------------------------------------------------------------
class FakeProvider:
    """内存里的解码器：给定尺寸与帧数，不碰磁盘。"""

    def __init__(self) -> None:
        self.sizes: dict[str, tuple[int, int, int]] = {}
        self.broken: set[str] = set()
        self.preloaded: list[str] = []
        self.closed = 0
        self.exported: list[str] = []
        self._error = ""

    def add(self, name: str, width: int = 800, height: int = 600, frames: int = 1) -> Path:
        self.sizes[name] = (width, height, frames)
        return TMP / name

    def probe(self, path: Path) -> M.ImageProbe:
        key = path.name
        if key in self.broken or key not in self.sizes:
            return M.ImageProbe(ok=False, error="坏图" if key in self.broken else "缺图")
        width, height, frames = self.sizes[key]
        return M.ImageProbe(ok=True, width=width, height=height, frames=frames,
                            size_bytes=width * height)

    def display(self, path: Path, transform: M.Transform, frame: int = 0) -> M.DisplayImage | None:
        probe = self.probe(path)
        if not probe.ok:
            self._error = probe.error
            return None
        width, height = probe.width, probe.height
        if transform.rotation % 2:
            width, height = height, width
        return M.DisplayImage(src=f"data:image/png;base64,{path.name}-{frame}",
                             width=width, height=height)

    def frame_duration_ms(self, path: Path, frame: int) -> int:
        return 90

    def list_folder(self, folder: Path) -> list[Path]:
        return [folder / name for name in sorted(self.sizes)]

    def preload(self, paths: Sequence[Path], transform: M.Transform) -> None:
        self.preloaded.extend(str(path) for path in paths)

    def export(self, path: Path, out: Path, transform: M.Transform, frame: int) -> None:
        self.exported.append(str(out))

    def stats(self) -> dict | None:
        return None

    def error_text(self) -> str:
        return self._error

    def close(self) -> None:
        self.closed += 1


class FakeTrash:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete(self, path: Path) -> None:
        self.deleted.append(path.name)


class StubAssociation:
    def __init__(self) -> None:
        self.installed: list[str] = []
        self.uninstalled = 0
        self.status = "（未注册）"

    def status_text(self) -> str:
        return self.status

    def install(self, executable: Path) -> None:
        self.installed.append(str(executable))

    def uninstall(self) -> None:
        self.uninstalled += 1


class StubPlatform:
    def __init__(self, windows: bool = True, exe: Path | None = Path("app.exe")) -> None:
        self._windows = windows
        self._exe = exe

    def is_windows(self) -> bool:
        return self._windows

    def executable(self) -> Path | None:
        return self._exe


class StubLauncher:
    def __init__(self, ok: bool = True) -> None:
        self.launched: list[str] = []
        self._ok = ok

    def launch(self, paths: Sequence[Path]) -> int:
        self.launched.extend(str(path) for path in paths)
        return len(paths) if self._ok else 0


class SyncRunner:
    """同步执行（业务是本地的，不需要真的挪线程）。"""

    async def run(self, work):
        return work()


class RecordingScheduler:
    """记录协程而不立即执行，便于逐条断言「排了哪些队」。"""

    LOOPS = ("drive_animation", "drive_slideshow")

    def __init__(self) -> None:
        self.pending: list = []
        self.cancelled = 0

    def spawn(self, coro):
        self.pending.append(coro)
        return coro

    def cancel(self, handle) -> None:
        self.cancelled += 1
        if inspect.iscoroutine(handle):
            handle.close()
            if handle in self.pending:
                self.pending.remove(handle)
        elif handle in self.pending:
            self.pending.remove(handle)

    def loops(self) -> list:
        return [coro for coro in self.pending if coro.cr_code.co_name in self.LOOPS]

    def clear_loops(self) -> None:
        for coro in self.loops():
            coro.close()
            self.pending.remove(coro)

    async def drain(self) -> None:
        while True:
            work = next(
                (coro for coro in self.pending if coro.cr_code.co_name not in self.LOOPS),
                None,
            )
            if work is None:
                return
            self.pending.remove(work)
            await work


# ---------------------------------------------------------------------------
# 1. 依赖方向
# ---------------------------------------------------------------------------
LAYER_RULES: dict[str, set[str]] = {
    "models": set(),
    "infra": {"models"},
    "service": {"models"},
    "viewmodel": {"models", "service"},
    "view": {"models", "viewmodel"},
}
PACKAGE = "imageviewer"


def _resolve_import(node: ast.ImportFrom, package: list[str]) -> str:
    """把相对导入还原成绝对模块名（``from ..models import x`` → ``imageviewer.models``）。"""
    base = package[: len(package) - (node.level - 1)] if node.level else []
    tail = (node.module or "").split(".") if node.module else []
    return ".".join([*base, *tail]).strip(".")


def _imported_modules(path: Path) -> set[str]:
    """一个文件里所有绝对模块名（相对导入已还原）。"""
    # 包内文件相对 src/（还原出 imageviewer.*），根 main.py 相对仓库根。
    base = SRC if path.is_relative_to(SRC) else ROOT
    rel = path.relative_to(base).with_suffix("")
    package = list(rel.parts[:-1])
    modules: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add(_resolve_import(node, package))
    return {name for name in modules if name}


def check_layers() -> None:
    section("依赖方向（五层单向）")
    violations: list[str] = []
    flet_imports: list[str] = []
    for path in sorted((SRC / PACKAGE).rglob("*.py")):
        layer = path.relative_to(SRC / PACKAGE).parts[0]
        if layer not in LAYER_RULES:
            continue
        for module in _imported_modules(path):
            root = module.split(".")[0]
            if root == "flet":
                flet_imports.append(f"{path.name}: {module}")
                continue
            if root != PACKAGE:
                continue
            parts = module.split(".")
            if len(parts) < 2:                       # import imageviewer 本身
                continue
            target = parts[1]
            if target not in LAYER_RULES:
                continue                              # 非分层模块（如 __init__ 常量）
            if target != layer and target not in LAYER_RULES[layer]:
                violations.append(f"{path.relative_to(SRC)} → {module}")
    check("层间不存在反向/跨层依赖", not violations, "; ".join(violations[:6]))
    check("包内不直接 import flet", not flet_imports, "; ".join(flet_imports[:6]))

    models_imports = {
        module.split(".")[0]
        for path in (SRC / PACKAGE / "models").rglob("*.py")
        for module in _imported_modules(path)
    }
    check("models 只依赖标准库",
          models_imports <= {"imageviewer"} | set(sys.stdlib_module_names),
          f"got {sorted(models_imports - set(sys.stdlib_module_names))}")

    app_imports = _imported_modules(ROOT / "main.py")
    check("组合根认识所有层",
          {"imageviewer.models", "imageviewer.infra", "imageviewer.service",
           "imageviewer.viewmodel", "imageviewer.view"} <= app_imports
          or {"imageviewer.service", "imageviewer.viewmodel", "imageviewer.view"} <= app_imports,
          f"got {sorted(app_imports)[:8]}")


# ---------------------------------------------------------------------------
# 2. 模型层
# ---------------------------------------------------------------------------
def check_models() -> None:
    section("模型层（几何 / 格式 / 动作 / 状态）")
    image = G.ViewSize.of(1920, 1080)
    viewport = G.ViewSize.of(900, 560)
    fit = G.ViewTransform()

    scale = G.compute_scale(fit, image, viewport)
    check("适应窗口取较小比例", abs(scale - 900 / 1920) < 1e-9, f"got {scale}")
    check("1:1 恒为 1", G.compute_scale(G.ViewTransform(mode="actual"), image, viewport) == 1.0)
    check("适应宽度只看宽",
          abs(G.compute_scale(G.ViewTransform(mode="fit_width"), image, viewport) - 900 / 1920) < 1e-9)
    check("无效尺寸退回 1", G.compute_scale(fit, G.ViewSize(), viewport) == 1.0)
    check("倍率夹在边界内", G.clamp_zoom(999) == G.MAX_ZOOM and G.clamp_zoom(0) == G.MIN_ZOOM)

    rect = G.image_rect(image, viewport, fit)
    check("适应窗口后图片居中", abs(rect.top - (560 - 1080 * scale) / 2) < 1e-9)
    check("图片矩形保持宽高比", abs(rect.width / rect.height - 1920 / 1080) < 1e-9)

    panned = G.ViewTransform(mode="custom", zoom=1.0, pan_x=9999, pan_y=-9999)
    clamped = G.clamp_pan(panned, image, viewport)
    check("平移超界被夹住", clamped.pan_x < 9999 and clamped.pan_y > -9999)
    check("已合法时返回原对象", G.clamp_pan(clamped, image, viewport) is clamped)

    anchor = G.ViewPoint(450, 280)                 # 视口中心：不会触发边界夹取
    x0 = G.compute_scale(fit, image, viewport)
    r0 = G.image_rect(image, viewport, fit)
    pixel = G.ViewPoint((anchor.x - r0.left) / x0, (anchor.y - r0.top) / x0)
    zoomed = G.zoom_anchored(fit, image, viewport, anchor, G.ZOOM_STEP)
    x1 = G.compute_scale(zoomed, image, viewport)
    r1 = G.image_rect(image, viewport, zoomed)
    check("锚点缩放改变了倍率", x1 > x0)
    check("锚点下的图像像素不动",
          abs(r1.left + pixel.x * x1 - anchor.x) < 1e-6
          and abs(r1.top + pixel.y * x1 - anchor.y) < 1e-6)
    check("缩放进入 custom 模式", zoomed.mode == "custom")
    check("到上下限时原样返回",
          G.zoom_anchored(G.ViewTransform("custom", G.MAX_ZOOM), image, viewport,
                          anchor, 2.0).zoom == G.MAX_ZOOM)
    check("键盘缩放从当前倍率起步", G.zoom_by(fit, image, viewport, G.ZOOM_STEP).zoom < 1.0)

    tiny = G.ViewRect.from_corners(10, 10, 12, 12)
    check("过小框选被忽略", G.zoom_to_region(fit, image, viewport, tiny) is fit)
    region = G.ViewRect.from_corners(200, 100, 500, 300)
    check("框选放大提高倍率",
          G.compute_scale(G.zoom_to_region(fit, image, viewport, region), image, viewport) > x0)
    check("矩形由任意方向两点定出",
          G.ViewRect.from_corners(500, 300, 200, 100) == region)
    check("点命中判定",
          region.contains(G.ViewPoint(250, 150)) and not region.contains(G.ViewPoint(10, 10)))

    check("Ctrl+滚轮 = 缩放",
          G.scroll_intent(image, viewport, fit, -100, ctrl=True).kind == "zoom")
    check("上滚放大 / 下滚缩小",
          G.scroll_intent(image, viewport, fit, -100, ctrl=True).factor > 1
          and G.scroll_intent(image, viewport, fit, 100, ctrl=True).factor < 1)
    check("图片全可见时翻页", G.scroll_intent(image, viewport, fit, 100).kind == "page")
    check("上滚翻上一张", G.scroll_intent(image, viewport, fit, -100).direction == -1)
    overflow = G.ViewTransform(mode="custom", zoom=3.0)
    check("图片溢出时滚动浏览", G.scroll_intent(image, viewport, overflow, 100).kind == "pan")
    check("零增量不产生意图", G.scroll_intent(image, viewport, fit, 0).kind == "none")
    check("全屏时按钮离边更远",
          G.nav_geometry(viewport, fullscreen=True)[0] > G.nav_geometry(viewport)[0])

    check("格式白名单不含点", all(ext.startswith(".") for ext in M.IMAGE_EXTS))
    check("RAW 是白名单子集", M.RAW_EXTS <= M.IMAGE_EXTS)
    check("扩展名判断大小写无关", M.is_image_path(Path("a.JPG")))
    check("对话框过滤表有序且不带点",
          M.image_suffixes() == sorted(M.image_suffixes())
          and all(not item.startswith(".") for item in M.image_suffixes()))

    check("动作枚举值唯一", len({action.value for action in M.Action}) == len(M.Action))
    check("间隔动作可反查", M.interval_action(5) is M.Action.INTERVAL_5)

    state = M.ViewerState()
    check("空状态画占位", state.show_placeholder and not state.has_image)
    check("解码中不画占位",
          not M.ViewerState(ready=False).show_placeholder)
    check("状态可比较（use_state 依赖它）", state == M.ViewerState())
    check("变换切模式清平移",
          M.ViewTransform("custom", 2.0, 30, 40).with_mode("fit").pan_x == 0.0)
    check("变换旋转可逆",
          M.Transform().rotated(1).rotated(-1) == M.Transform())


# ---------------------------------------------------------------------------
# 3. 服务层：画册规则
# ---------------------------------------------------------------------------
def _gallery(files: Sequence[tuple[str, int, int, int]] = ()) -> tuple[Gallery, FakeProvider, FakeTrash]:
    provider = FakeProvider()
    for name, width, height, frames in files:
        provider.add(name, width, height, frames)
    _touch(*[name for name, *_ in files])
    trash = FakeTrash()
    return Gallery(provider, trash), provider, trash


def check_service() -> None:
    section("服务层（画册业务规则）")
    b_path, _a_path, c_path = _touch("b.jpg", "a.jpg", "c.jpg")
    gallery, provider, trash = _gallery([
        ("b.jpg", 400, 300, 1),
        ("a.jpg", 200, 100, 1),
        ("c.jpg", 60, 40, 1),
    ])

    check("打开单张只加载这一张", gallery.open_file(b_path) and gallery.total == 1)
    check("打开不支持的后缀被拒绝", not gallery.open_file(TMP / "notes.txt"))
    check("目录列表来自解码器",
          [item.name for item in provider.list_folder(TMP)] == ["a.jpg", "b.jpg", "c.jpg"])

    listing = provider.list_folder(TMP)
    check("补全目录后仍是同一张图",
          gallery.apply_listing(listing) and gallery.current.name == "b.jpg"
          and gallery.total == 3 and gallery.index == 1)
    check("已在浏览目录时不重复补全", not gallery.apply_listing(listing))

    check("首尾循环：末张再下一张回到首张",
          gallery.navigate(1) and gallery.index == 2)
    check("再下一张回到第一张", gallery.navigate(1) and gallery.index == 0)

    provider.broken.add("a.jpg")
    gallery.open_file(c_path)
    gallery.apply_listing(listing)
    moved = gallery.navigate(1)
    check("跳过坏图", moved and gallery.current.name == "b.jpg", str(gallery.current))
    provider.broken.clear()

    gallery.open_file(b_path)
    gallery.apply_listing(listing)
    gallery.rotate(1)
    gallery.flip(horizontal=True)
    check("旋转与翻转被记录",
          gallery.transform.rotation == 1 and gallery.transform.flip_h)
    gallery.navigate(1)
    check("切图复位变换", gallery.transform == M.Transform())

    _touch("anim.gif")
    animated, _anim_provider, _ = _gallery([("anim.gif", 320, 240, 4)])
    animated.open_file(TMP / "anim.gif")
    check("动图帧数来自探测", animated.frames() == 4)
    check("帧前进循环", animated.step_frame(1) and animated.frame == 1)
    for _ in range(3):
        animated.step_frame(1)
    check("帧序号回到 0", animated.frame == 0)

    gallery.open_file(b_path)
    gallery.apply_listing(listing)
    info = gallery.info()
    check("元信息含页码与总数",
          info.name == "b.jpg" and (info.index, info.total) == (2, 3))
    check("元信息含尺寸与帧数",
          (info.width, info.height, info.frames) == (400, 300, 1))

    closed_before = provider.closed
    gallery.open_file(b_path)
    gallery.apply_listing(listing)
    removed = gallery.delete_current()
    check("删除走回收站", trash.deleted == ["b.jpg"] and removed == "b.jpg")
    check("删除前先释放句柄", provider.closed > closed_before)
    check("删除后游标停在原位置（指向下一张）",
          gallery.index == 1 and gallery.current.name == "c.jpg", str(gallery.current))

    from imageviewer.infra.display import LocalDisplay
    from imageviewer.infra.trash import TrashBin

    local_display = LocalDisplay()
    check("本地解码器满足 DisplayProvider", isinstance(local_display, DisplayProvider))
    check("回收站满足 FileTrash", isinstance(TrashBin(), FileTrash))
    local_display.close()


# ---------------------------------------------------------------------------
# 4. 视图模型层
# ---------------------------------------------------------------------------
def _view_model(files: Sequence[tuple[str, int, int, int]] = (),
                *, start: str | None = None) -> tuple[ViewerViewModel, ViewerService, RecordingScheduler]:
    provider = FakeProvider()
    for name, width, height, frames in files:
        provider.add(name, width, height, frames)
    _touch(*[name for name, *_ in files])
    service = ViewerService(provider, FakeTrash(), StubAssociation(),
                            StubPlatform(), StubLauncher())
    scheduler = RecordingScheduler()
    vm = ViewerViewModel(service, SyncRunner(), scheduler, start_path=start)
    return vm, service, scheduler


def check_viewmodel() -> None:
    section("视图模型层（文案 / 分发 / 状态机）")
    viewport = G.ViewSize.of(900, 560)
    info = M.ImageInfo(name="photo.jpg", width=1920, height=1080, size_bytes=2 * 1024 * 1024,
                       index=3, total=12, frames=1)
    snap = format_status(info, G.ViewTransform(), viewport)
    check("三段式文件名", snap.name == "photo.jpg")
    check("中段含分辨率与体积", "1920×1080" in snap.detail and "MB" in snap.detail)
    check("右段含页码与倍率", "3 / 12" in snap.position and "%" in snap.position)
    check("有图标记", snap.has_image)
    check("空元信息整条留白", format_status(M.ImageInfo(), G.ViewTransform(), viewport) == M.StatusSnapshot())
    check("缓存标记只在命中时出现",
          "缓存" in format_status(info, G.ViewTransform(), viewport, shared=True).detail)
    check("动图写帧号",
          "动图 2/5帧" in format_status(
              M.ImageInfo(name="a.gif", width=10, height=10, size_bytes=1, index=1, total=1, frames=5),
              G.ViewTransform(), viewport, frame=1).detail)
    check("倍率文案带模式", "适应" in format_status(info, G.ViewTransform(), viewport).position)
    check("1:1 文案写 100%",
          format_status(info, G.ViewTransform(mode="actual"), viewport).position.endswith("100%"))
    check("体积格式化", format_size(2 * 1024 * 1024) == "2.0 MB")

    check("无数据诊断有兜底", format_service_report(M.ServiceReport()) != "")
    check("本地模式只写一句人话",
          format_service_report(M.ServiceReport(note="本地模式")) == "本地模式")
    report = M.ServiceReport(
        role="窗口进程", backend="127.0.0.1:1", pid=42,
        blobs=M.BlobStats(count=4, bytes=1024, limit_bytes=2048, hits=3, misses=1),
        decoded=M.DecodeStats(entries=2, megapixels=6), pending=1, preload_done=5,
    )
    text = format_service_report(report)
    check("诊断含命中率", "命中率：75%" in text, text)
    check("诊断末行解释缓存标记", "缓存" in text.splitlines()[-1])
    check("诊断含预加载统计", "完成 5" in text)

    # —— 意图分发 ——
    vm, service, scheduler = _view_model([
        ("b.jpg", 400, 300, 1), ("a.jpg", 200, 100, 1), ("c.jpg", 60, 40, 1),
    ], start=str(TMP / "b.jpg"))

    async def run_flow() -> None:
        await vm.start()
        await scheduler.drain()
        check("启动后出图", vm.state.src is not None)
        check("启动后状态栏有页码", "/ 3" in vm.state.status.position, vm.state.status.position)
        check("启动后出现翻页箭头", vm.state.show_nav)
        check("预热交给解码器", bool(service._display.preloaded))  # noqa: SLF001

        await vm.goto(1)
        check("翻页换图", vm.state.status.name == "c.jpg", vm.state.status.name)
        check("翻页后有选区被清空", vm.state.selection is None)

        vm.dispatch(M.Action.ZOOM_IN)
        check("放大后进入自定义倍率", vm.state.view.mode == "custom")
        vm.dispatch(M.Action.FIT_WINDOW)
        check("适应窗口回到 fit", vm.state.view.mode == "fit")

        vm.dispatch(M.Action.FULLSCREEN)
        check("全屏是状态", vm.state.fullscreen)
        vm.dispatch(M.Action.FULLSCREEN)
        check("再按一次退出全屏", not vm.state.fullscreen)

        vm.dispatch(M.Action.SLIDESHOW)
        check("幻灯片开始后循环被排上", bool(scheduler.loops()))
        vm.dispatch(M.Action.SLIDESHOW)
        check("停止后循环被取消", not scheduler.loops())

        vm.dispatch(M.Action.OPEN_FILE)
        check("打开图片先请求视图选路径", vm.state.request is M.RequestKind.PICK_FILE)
        vm.submit_pick([TMP / "a.jpg"])
        await scheduler.drain()
        check("选完路径后换图", vm.state.status.name == "a.jpg", vm.state.status.name)
        check("请求被清空", vm.state.request is None)

        vm.dispatch(M.Action.DELETE)
        check("删除先弹确认框",
              vm.state.dialog is not None and vm.state.dialog.kind is M.DialogKind.DELETE)
        vm.request_exit()
        check("有弹窗时 Esc 只关弹窗",
              vm.state.dialog is None and not vm.state.closing)
        vm.request_exit()
        check("无弹窗时 Esc 请求关闭", vm.state.closing)

        vm.dispatch(M.Action.ASSOC_STATUS)
        await scheduler.drain()
        check("关联状态进只读弹窗",
              vm.state.dialog is not None
              and vm.state.dialog.kind is M.DialogKind.ASSOCIATION_STATUS
              and vm.state.dialog.selectable)
        vm.dismiss()
        vm.dispatch(M.Action.SERVICE_STATUS)
        await scheduler.drain()
        check("缓存状态弹窗有内容",
              vm.state.dialog is not None and bool(vm.state.dialog.message))

        # —— 手势 ——
        geometry_vm, _, _ = _view_model([("big.jpg", 4000, 3000, 1)],
                                        start=str(TMP / "big.jpg"))

        async def gestures() -> None:
            await geometry_vm.start()
            geometry_vm.set_viewport(900, 560)
            before = G.compute_scale(geometry_vm.state.view, geometry_vm.state.image_size,
                                     geometry_vm.state.viewport)
            geometry_vm.scroll(-100, x=450, y=280, ctrl=True)
            after = G.compute_scale(geometry_vm.state.view, geometry_vm.state.image_size,
                                    geometry_vm.state.viewport)
            check("Ctrl+滚轮放大",
                  after > before and geometry_vm.state.view.mode == "custom")
            geometry_vm.selection_start(100, 100)
            geometry_vm.selection_update(400, 300)
            region = geometry_vm.state.selection
            check("拖拽产生选区", region is not None and region.width == 300)
            zoom_before = geometry_vm.state.view.zoom
            geometry_vm.tap(200, 200)
            check("点选区内放大", geometry_vm.state.view.zoom > zoom_before)
            check("放大后选区被清空", geometry_vm.state.selection is None)
            geometry_vm.drag_start(100, 100)
            geometry_vm.drag_update(160, 130)
            geometry_vm.drag_end()
            check("右键拖拽改变平移", geometry_vm.state.view.pan_x != 0)

        await gestures()

    asyncio.run(run_flow())

    check("动作表覆盖菜单全部叶子", menu.validate_actions(vm.actions) == (),
          "; ".join(menu.validate_actions(vm.actions)))
    check("动作表比菜单多出键盘专属动作",
          {M.Action.NEXT, M.Action.PREV} <= set(vm.actions))


# ---------------------------------------------------------------------------
# 5. 视图层
# ---------------------------------------------------------------------------
def check_view() -> None:
    section("视图层（菜单 / 状态栏 / 画布 / 键位）")
    sections = menu.build_sections(M.ViewerState(has_frames=True,
                                                 status=M.StatusSnapshot("a.jpg", "", "1 / 1")))
    check("四栏菜单", len(sections) == 4, f"got {len(sections)}")
    check("栏名稳定", tuple(item.label for item in sections) == ("文件", "播放", "编辑", "视图"))
    check("菜单数据体检通过", ui.validate_sections(sections) == ())

    shortcuts = menu.shortcut_table()
    check("快捷键表可导出", shortcuts.get(M.Action.OPEN_FILE.value) == "Ctrl+O")
    check("快捷键表覆盖导出", shortcuts.get(M.Action.EXPORT_FRAME.value) == "Ctrl+E")

    playing = menu.build_sections(M.ViewerState(slideshow_on=True, paused=True,
                                                interval=5,
                                                status=M.StatusSnapshot("a", "", "1 / 1")))
    items = {item.key: item for item in ui.iter_actions(playing[:1])}
    check("播放中文案随状态变", items[M.Action.SLIDESHOW.value].label == "停止播放")
    check("暂停中文案随状态变", items[M.Action.PAUSE.value].label == "继续")
    check("当前间隔打勾",
          items[M.Action.INTERVAL_5.value].checked
          and not items[M.Action.INTERVAL_3.value].checked)
    check("间隔项带秒数载荷", items[M.Action.INTERVAL_5.value].data == 5)
    check("带子菜单的入口不触发动作", not items["assoc"].is_leaf)

    idle = {item.key: item for item in ui.iter_actions(menu.build_sections(M.ViewerState()))}
    check("无图时删除禁用", idle[M.Action.DELETE.value].disabled)
    check("无动图时帧控制禁用", idle[M.Action.FRAME_NEXT.value].disabled)
    check("暂停项在停止播放时禁用", idle[M.Action.PAUSE.value].disabled)

    check("键位：空格翻下一张", keymap.match_key(" ") is M.Action.NEXT)
    check("键位：← 也是下一张（IrfanView 习惯）", keymap.match_key("ArrowLeft") is M.Action.NEXT)
    check("键位：Ctrl+O 打开", keymap.match_key("o", ctrl=True) is M.Action.OPEN_FILE)
    check("键位：Ctrl+Shift+O 打开文件夹",
          keymap.match_key("o", ctrl=True, shift=True) is M.Action.OPEN_FOLDER)
    check("键位：裸数字不触发", keymap.match_key("1") is None)
    check("键位：可识别键全覆盖",
          set(keymap.KEY_BINDINGS.values()) <= set(M.Action))

    bar = bars.build_menu_bar(sections, lambda action: None)
    check("菜单栏高度取自设计层", bar.height == design.MENU_H)
    status_bar = bars.build_status_bar(M.StatusSnapshot("a.jpg", "1920×1080", "1 / 2"))
    check("三段式状态栏可构造",
          status_bar.height == design.STATUS_H and len(status_bar.content.controls) == 3)

    dialog = dialogs.confirm_dialog(
        heading="删除图片", message="确定吗？", confirm_label="删除",
        on_confirm=lambda event: None, on_cancel=lambda event: None, danger=True,
    )
    check("确认框两个动作", len(dialog.actions) == 2)
    check("确认框内容由调用方给", dialog.content is not None)
    check("空态可构造", len(kit.empty_stage().controls) == 3)

    stage = canvas.build_canvas(
        src=None, image_size=G.ViewSize(), viewport=G.ViewSize.of(900, 560),
        view=G.ViewTransform(), selection=None, height=560, fullscreen=False,
        handlers=canvas.CanvasHandlers(), placeholder=kit.empty_stage(),
    )
    check("画布可构造", stage.height == 560)

    with_nav = canvas.build_canvas(
        src="data:image/png;base64,AAAA", image_size=G.ViewSize.of(1600, 900),
        viewport=G.ViewSize.of(900, 560), view=G.ViewTransform(),
        selection=G.ViewRect.from_corners(10, 10, 100, 100), height=560,
        fullscreen=False, show_nav=True,
        handlers=canvas.CanvasHandlers(on_prev=lambda event: None,
                                       on_next=lambda event: None),
    )
    # 背景 + 图像 + 选区 + 左右两枚浮层按钮 = 5 层
    check("画布层级顺序正确", len(with_nav.content.controls) == 5,
          f"got {len(with_nav.content.controls)}")
    # 渲染端要求 IconButton 有可见图标，而 Python 侧构造不报错；单图时导航隐藏，
    # 启动冒烟会漏掉这条路径，所以必须在控件树上显式断言。
    nav_buttons = [item for item in with_nav.content.controls if isinstance(item, ui.IconButton)]
    check("两枚翻页按钮都带图标",
          len(nav_buttons) == 2 and all(button.icon is not None for button in nav_buttons),
          f"got {[getattr(button, 'icon', None) for button in nav_buttons]}")
    check("按钮尺寸取自设计层",
          all(button.width == design.GLASS_SIZE and button.height == design.GLASS_SIZE
              for button in nav_buttons))
    check("三态配色交给渲染端",
          all(set(button.style.bgcolor) == {ui.ControlState.DEFAULT,
                                            ui.ControlState.HOVERED,
                                            ui.ControlState.DISABLED}
              for button in nav_buttons))
    check("不给 tooltip（贴边会被裁）",
          all(button.tooltip is None for button in nav_buttons))

    single = glass.glass_nav_pair(viewport=G.ViewSize.of(900, 560),
                                  on_prev=lambda event: None)
    check("单侧回调只画一枚", len(single) == 1, f"got {len(single)}")
    check("无回调不画按钮", glass.glass_nav_pair(viewport=G.ViewSize.of(900, 560)) == [])

    viewport_params = set(inspect.signature(ImageViewport).parameters)
    check("视口不替调用方定按钮皮肤",
          "overlay" in viewport_params and "show_nav" not in viewport_params,
          f"got {sorted(viewport_params)}")

    vm, _, _ = _view_model()
    app = build_app(vm)
    check("根组件由 viewmodel 构造", callable(app))
    design_shot = design.snapshot()
    check("设计快照含色表", len(design_shot["colors"]) >= 10)
    check("色表可由品牌色复算",
          design.build_scheme(design.BRAND_PRIMARY, design.MODE) == design_shot["colors"])
    check("布局高度落在 4px 网格",
          design.MENU_H % 4 == 0 and design.STATUS_H % 4 == 0)
    check("chrome 高度等于两段之和", design.CHROME_H == design.MENU_H + design.STATUS_H)
    check("浮层色值进入设计快照",
          design_shot["overlay"]["size"] == design.GLASS_SIZE
          and design_shot["overlay"]["idle"] == design.C_GLASS)


# ---------------------------------------------------------------------------
# 6. 装配与配置
# ---------------------------------------------------------------------------
def check_wiring() -> None:
    section("装配与配置一致性")
    import tomllib

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    boot = data["tool"]["flet"]["boot_screen"]["flet"]
    check("启动屏底色 = 窗口底色",
          boot["bgcolor_light"].upper() == design.C_SURFACE.upper()
          and boot["bgcolor_dark"].upper() == design.C_SURFACE.upper(),
          f'boot={boot["bgcolor_light"]} surface={design.C_SURFACE}')
    deps = data["project"]["dependencies"]
    check("界面层依赖 cs-ui", any(item.startswith("cs-ui") for item in deps), f"got {deps}")
    check("窗口隐藏启动仍开启",
          data["tool"]["flet"]["app"]["hide_window_on_start"] is True)
    scripts = data["project"]["scripts"]
    check("控制台脚本指向新入口",
          scripts["imageviewer"] == "main:run"
          and scripts["imageviewer-daemon"] == "imageviewer.infra.shared.daemon:main",
          str(scripts))

    # src 布局：包必须在 src/ 下被发现，同时根 main.py 不能被 package-dir 漏掉
    setuptools_cfg = data["tool"]["setuptools"]
    check("src 布局声明完整",
          setuptools_cfg.get("package-dir") == {"": ".", "imageviewer": "src/imageviewer"}
          and setuptools_cfg.get("py-modules") == ["main"]
          and setuptools_cfg["packages"]["find"]["where"] == ["src"]
          and setuptools_cfg["packages"]["find"]["include"] == ["imageviewer*"],
          str(setuptools_cfg))
    layers = ("models", "infra", "service", "viewmodel", "view")
    check("五个分层目录都存在",
          all((SRC / PACKAGE / layer).is_dir() for layer in layers))

    from imageviewer import service as service_pkg
    from imageviewer.viewmodel.ports import ViewerFacade

    provider = FakeProvider()
    provider.add("a.jpg")
    concrete = ViewerService(provider, FakeTrash(), StubAssociation(),
                             StubPlatform(), StubLauncher())
    check("ViewerService 满足 ViewerFacade", isinstance(concrete, ViewerFacade))
    check("服务层导出端口协议",
          service_pkg.DisplayProvider is DisplayProvider and service_pkg.FileTrash is FileTrash)

    # 开发模式下窗口由 flet.exe 客户端进程创建（pid 与解释器不同），窗口外观函数
    # 必须能按标题回落到那个窗口；main.py 也必须真的把标题传进去，否则暗色标题栏
    # 与「显示看门狗」在开发期静默空转 —— 表现就是深色界面永远顶着一条白标题栏。
    from imageviewer.infra import windows as win

    appearance = (win.flutter_windows, win.apply_dark_title_bar,
                  win.ensure_visible, win.start_show_watchdog)
    check("窗口外观函数可接受窗口标题",
          all("title" in inspect.signature(fn).parameters for fn in appearance))
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    check("main.py 把窗口标题交给窗口外观",
          "apply_dark_title_bar(WINDOW_TITLE)" in main_source
          and "start_show_watchdog(title=WINDOW_TITLE)" in main_source)
    # src 布局打包必需：删掉这段引导，打包后包在 <app>/src/ 而 sys.path 不含它。
    check("main.py 保留 src 布局引导",
          "sys.path.insert(0, str(_SRC))" in main_source
          and '(_SRC / "imageviewer").is_dir()' in main_source)
    check("路径出口唯一（_paths）", (SRC / PACKAGE / "_paths.py").is_file())


def main() -> int:
    print("ImageViewer 分层自检")
    check_layers()
    check_models()
    check_service()
    check_viewmodel()
    check_view()
    check_wiring()
    print(f"\n{PASS} 项通过 / {len(FAIL)} 项失败")
    for item in FAIL:
        print(f"  - {item}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
