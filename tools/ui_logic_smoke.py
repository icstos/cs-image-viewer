"""界面层自检 —— **不开窗口**，把「可复算」变成可执行的断言。

跑法::

    py -3.12 tools/ui_logic_smoke.py

覆盖四层契约：

============ ==========================================================
几何         ``ui.core.geometry``：倍率 / 平移 / 锚点缩放 / 框选 / 滚轮意图
菜单         :mod:`viewer.ui.menu`：动作键、快捷键表、状态联动、分发覆盖
文案         :mod:`viewer.state`：状态栏三段、共享缓存诊断
构件与迁移   控件树可构造；项目源码不再直接依赖底层框架包
============ ==========================================================

断言写「规则」不写「色值」：换品牌色时这些检查不该假报警。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ui

from viewer import design
from viewer.state import StatusSnapshot, format_service_info, format_status
from viewer.ui import bars, canvas, dialogs, kit, menu

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
class FakeManager:
    """只实现状态文案需要的最小接口，不碰磁盘、不解码。"""

    current_frame = 0
    last_shared_hit = False

    def info(self) -> dict:
        return {
            "name": "photo_001.jpg",
            "w": 1920, "h": 1080, "size": 2 * 1024 * 1024,
            "frames": 1, "pos": 3, "total": 12,
        }

    def frame_count(self) -> int:
        return 1


# ---------------------------------------------------------------------------
def check_geometry() -> None:
    section("几何（ui.core.geometry）")
    image = ui.ViewSize.of(1920, 1080)
    viewport = ui.ViewSize.of(900, 560)
    fit = ui.ViewTransform()

    s = ui.compute_scale(fit, image, viewport)
    check("适应窗口取较小比例", abs(s - 900 / 1920) < 1e-9, f"got {s}")
    check("1:1 恒为 1", ui.compute_scale(ui.ViewTransform(mode="actual"), image, viewport) == 1.0)
    check("适应宽度只看宽", abs(ui.compute_scale(ui.ViewTransform(mode="fit_width"), image, viewport) - 900 / 1920) < 1e-9)
    check("无效尺寸退回 1", ui.compute_scale(fit, ui.ViewSize(), viewport) == 1.0)
    check("倍率夹在边界内", ui.clamp_zoom(999) == ui.MAX_ZOOM and ui.clamp_zoom(0) == ui.MIN_ZOOM)

    rect = ui.image_rect(image, viewport, fit)
    check("适应窗口后图片居中", abs(rect.top - (560 - 1080 * s) / 2) < 1e-9)
    check("图片矩形保持宽高比", abs(rect.width / rect.height - 1920 / 1080) < 1e-9)

    panned = ui.ViewTransform(mode="custom", zoom=1.0, pan_x=9999, pan_y=-9999)
    clamped = ui.clamp_pan(panned, image, viewport)
    check("平移超界被夹住", clamped.pan_x < 9999 and clamped.pan_y > -9999)
    check("已合法时返回原对象", ui.clamp_pan(clamped, image, viewport) is clamped)
    small = ui.ViewSize.of(200, 100)
    check("未溢出时强制居中", ui.clamp_pan(panned, small, viewport) == ui.ViewTransform("custom", 1.0, 0.0, 0.0))

    anchor = ui.ViewPoint(450, 280)   # 视口中心：不会触发边界夹取
    z0 = ui.compute_scale(fit, image, viewport)
    r0 = ui.image_rect(image, viewport, fit)
    pixel = ui.ViewPoint((anchor.x - r0.left) / z0, (anchor.y - r0.top) / z0)
    zoomed = ui.zoom_anchored(fit, image, viewport, anchor, ui.ZOOM_STEP)
    z1 = ui.compute_scale(zoomed, image, viewport)
    r1 = ui.image_rect(image, viewport, zoomed)
    check("锚点缩放改变了倍率", z1 > z0)
    check("锚点下的图像像素不动",
          abs(r1.left + pixel.x * z1 - anchor.x) < 1e-6
          and abs(r1.top + pixel.y * z1 - anchor.y) < 1e-6)
    # 贴边锚点在「图片几乎没溢出」时会算出一个越界的平移；clamp_pan 优先保证
    # 图片边缘不进视口，因此锚点精度让位于边界 —— 这是刻意的取舍。
    edge = ui.zoom_anchored(fit, image, viewport, ui.ViewPoint(10, 10), ui.ZOOM_STEP)
    check("边界夹取优先于贴边锚点", ui.clamp_pan(edge, image, viewport) == edge)
    check("缩放进入 custom 模式", zoomed.mode == "custom")
    check("到上下限时原样返回",
          ui.zoom_anchored(ui.ViewTransform("custom", ui.MAX_ZOOM), image, viewport,
                           anchor, 2.0).zoom == ui.MAX_ZOOM)
    check("键盘缩放从当前倍率起步", ui.zoom_by(fit, image, viewport, ui.ZOOM_STEP).zoom < 1.0)

    tiny = ui.ViewRect.from_corners(10, 10, 12, 12)
    check("过小框选被忽略", ui.zoom_to_region(fit, image, viewport, tiny) is fit)
    region = ui.ViewRect.from_corners(200, 100, 500, 300)
    region_view = ui.zoom_to_region(fit, image, viewport, region)
    check("框选放大提高倍率", ui.compute_scale(region_view, image, viewport) > z0)
    check("框选后仍在边界内", ui.clamp_pan(region_view, image, viewport) == region_view)
    check("矩形由任意方向两点定出", ui.ViewRect.from_corners(500, 300, 200, 100) == region)
    check("点命中判定", region.contains(ui.ViewPoint(250, 150)) and not region.contains(ui.ViewPoint(10, 10)))

    check("Ctrl+滚轮 = 缩放", ui.scroll_intent(image, viewport, fit, -100, ctrl=True).kind == "zoom")
    check("上滚放大 / 下滚缩小",
          ui.scroll_intent(image, viewport, fit, -100, ctrl=True).factor > 1
          and ui.scroll_intent(image, viewport, fit, 100, ctrl=True).factor < 1)
    check("图片全可见时翻页", ui.scroll_intent(image, viewport, fit, 100).kind == "page")
    check("上滚翻上一张", ui.scroll_intent(image, viewport, fit, -100).direction == -1)
    overflow = ui.ViewTransform(mode="custom", zoom=3.0)
    check("图片溢出时滚动浏览", ui.scroll_intent(image, viewport, overflow, 100).kind == "pan")
    check("零增量不产生意图", ui.scroll_intent(image, viewport, fit, 0).kind == "none")
    check("全屏时按钮离边更远", ui.nav_geometry(viewport, fullscreen=True)[0] > ui.nav_geometry(viewport)[0])
    check("倍率文案带模式", "适应" in ui.format_zoom(fit, image, viewport))
    check("1:1 文案写 100%", ui.format_zoom(ui.ViewTransform(mode="actual"), image, viewport) == "100%")


# ---------------------------------------------------------------------------
def check_menu() -> None:
    section("菜单数据与分发")
    sections = menu.build_sections(menu.MenuContext())
    check("四栏菜单", len(sections) == 4, f"got {len(sections)}")
    check("栏名稳定", tuple(s.label for s in sections) == ("文件", "播放", "编辑", "视图"))
    shortcuts = menu.shortcut_table()
    check("快捷键表可导出", shortcuts.get("open_file") == "Ctrl+O", f"got {shortcuts.get('open_file')}")
    check("快捷键表覆盖导出", shortcuts.get("export_frame") == "Ctrl+E")
    check("动作键全部登记", set(menu.ACTIONS) == {
        item.key for item in ui.iter_actions(sections) if item.is_leaf
    })
    check("菜单数据体检通过", ui.validate_sections(sections) == ())
    check("动作表覆盖完整", menu.validate_dispatch({k: (lambda: None) for k in menu.ACTIONS}) == ())
    broken = {k: (lambda: None) for k in menu.ACTIONS if k != "exit"}
    check("漏一个 key 能被查出来", any("exit" in issue for issue in menu.validate_dispatch(broken)))

    playing = menu.build_sections(menu.MenuContext(slideshow_on=True, paused=True, interval=5))
    file_items = {item.key: item for item in ui.iter_actions(playing[:1])}
    check("播放中文案随状态变", file_items["slideshow"].label == "停止播放")
    check("暂停中文案随状态变", file_items["pause"].label == "继续")
    check("当前间隔打勾", file_items["interval.5"].checked and not file_items["interval.3"].checked)
    check("间隔项带秒数载荷", file_items["interval.5"].data == 5)
    check("带子菜单的入口不触发动作", not file_items["assoc"].is_leaf)

    idle = {item.key: item for item in ui.iter_actions(menu.build_sections(menu.MenuContext()))}
    check("无图时删除禁用", idle["delete"].disabled)
    check("无动图时帧控制禁用", idle["frame_next"].disabled)
    check("暂停项在停止播放时禁用", idle["pause"].disabled)


# ---------------------------------------------------------------------------
def check_text() -> None:
    section("状态文案（state）")
    image = ui.ViewSize.of(1920, 1080)
    viewport = ui.ViewSize.of(900, 560)
    snap = format_status(FakeManager(), ui.ViewTransform(), image, viewport)
    check("三段式文件名", snap.name == "photo_001.jpg")
    check("中段含分辨率", "1920×1080" in snap.detail)
    check("右段含页码与倍率", "3 / 12" in snap.position and "%" in snap.position)
    check("有图标记", snap.has_image)

    class Empty(FakeManager):
        def info(self) -> dict:
            return {"name": "", "w": 0, "h": 0, "size": 0, "frames": 0, "pos": 0, "total": 0}

    empty = format_status(Empty(), ui.ViewTransform(), ui.ViewSize(), viewport)
    check("空窗口不写 0 / 0", empty.position == "" and empty.detail == "")
    check("状态快照可比较", StatusSnapshot() == StatusSnapshot("", "", ""))
    check("无数据诊断有兜底", format_service_info(None) == "（无数据）")
    check("note 优先于逐项排版", format_service_info({"note": "本地模式"}) == "本地模式")
    stats = format_service_info({"role": "窗口", "pid": 12, "blobs": {"hits": 3, "misses": 1, "count": 4, "bytes": 1024, "limit_bytes": 2048}})
    check("诊断含命中率", "命中率：75%" in stats, stats)
    check("诊断末行解释缓存标记", "缓存" in stats.splitlines()[-1])

    shot = design.snapshot()
    check("设计快照含色表", len(shot["colors"]) >= 10)
    check("色表可由品牌色复算", ui.MediaPalette.derive(design.BRAND_PRIMARY, mode=design.MODE).as_dict() == shot["colors"])
    check("布局高度落在网格上", design.MENU_H % 4 == 0 and design.STATUS_H % 4 == 0)
    check("chrome 高度等于两段之和", design.CHROME_H == design.MENU_H + design.STATUS_H)


# ---------------------------------------------------------------------------
def check_widgets() -> None:
    section("构件装配与迁移")
    sections = menu.build_sections(menu.MenuContext(has_image=True, has_frames=True))
    bar = bars.build_menu_bar(sections, lambda action: None)
    check("菜单栏可构造", bar.height == design.MENU_H)
    sb = bars.build_status_bar(StatusSnapshot("a.jpg", "1920×1080", "1 / 2"))
    check("三段式状态栏可构造", sb.height == design.STATUS_H and len(sb.content.controls) == 3)
    dialog = dialogs.confirm_dialog(
        heading="删除图片", message="确定吗？", confirm_label="删除",
        on_confirm=lambda e: None, on_cancel=lambda e: None, danger=True,
    )
    check("确认框两个动作", len(dialog.actions) == 2)
    check("确认框内容由调用方给", dialog.content is not None)
    check("空态可构造", len(kit.empty_stage().controls) == 3)
    stage = canvas.build_canvas(
        src=None, image_size=ui.ViewSize(), viewport=ui.ViewSize.of(900, 560),
        view=ui.ViewTransform(), selection=None, height=560, fullscreen=False,
        handlers=canvas.CanvasHandlers(), placeholder=kit.empty_stage(),
    )
    check("画布可构造", stage.height == 560)
    with_nav = canvas.build_canvas(
        src="data:image/png;base64,AAAA", image_size=ui.ViewSize.of(1600, 900),
        viewport=ui.ViewSize.of(900, 560), view=ui.ViewTransform(),
        selection=ui.ViewRect.from_corners(10, 10, 100, 100), height=560,
        fullscreen=False, show_nav=True,
        handlers=canvas.CanvasHandlers(on_prev=lambda e: None, on_next=lambda e: None),
    )
    # 背景 + 图像 + 选区 + 左右两枚玻璃按钮 = 5 层
    check("画布层级顺序正确", len(with_nav.content.controls) == 5,
          f"got {len(with_nav.content.controls)}")
    # 渲染端要求 IconButton 有 icon 或可见 content，Python 侧构造不报错。
    # 单图时导航按钮隐藏，启动冒烟会漏掉这条路径，必须在控件树上显式断言。
    nav_buttons = [c for c in with_nav.content.controls
                   if isinstance(c, ui.GlassIconButton)]
    check("两枚翻页按钮都带图标",
          len(nav_buttons) == 2 and all(b.icon is not None for b in nav_buttons),
          f"got {[getattr(b, 'icon', None) for b in nav_buttons]}")
    check("图标没被位置参数塞进 tooltip",
          all(not b.tooltip for b in nav_buttons))

    sources = [ROOT / "main.py", *sorted((ROOT / "viewer").rglob("*.py"))]
    direct = []
    for path in sources:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith(("import flet", "from flet")) and not stripped.startswith("#"):
                direct.append(f"{path.name}: {stripped}")
    check("项目源码不直接 import flet", not direct, "; ".join(direct))


def check_config() -> None:
    """pyproject 是静态配置，design 是运行时唯一事实源 —— 两者不许漂移。"""
    section("配置一致性（pyproject ↔ design）")
    import tomllib

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    boot = data["tool"]["flet"]["boot_screen"]["flet"]
    check("启动屏底色 = 窗口底色",
          boot["bgcolor_light"].upper() == design.C_SURFACE.upper()
          and boot["bgcolor_dark"].upper() == design.C_SURFACE.upper(),
          f'boot={boot["bgcolor_light"]} surface={design.C_SURFACE}')
    deps = data["project"]["dependencies"]
    check("界面层依赖 cs-ui", any(d.startswith("cs-ui") for d in deps), f"got {deps}")
    check("打包包含 viewer.ui 子包",
          "viewer.ui" in data["tool"]["setuptools"]["packages"])
    check("窗口隐藏启动仍开启",
          data["tool"]["flet"]["app"]["hide_window_on_start"] is True)


def main() -> int:
    print("ImageViewer 界面层自检")
    check_geometry()
    check_menu()
    check_text()
    check_widgets()
    check_config()
    print(f"\n{PASS} 项通过 / {len(FAIL)} 项失败")
    for item in FAIL:
        print(f"  - {item}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
