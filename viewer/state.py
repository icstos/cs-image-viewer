"""应用状态与文案格式化 —— **不碰控件树的一层**。

界面只负责画，业务状态与「状态栏该写什么」在这里算清楚。这样：

* 状态是普通 dataclass，几何用 :class:`ui.ViewTransform`（不可变、可复算）；
* 文案格式化是纯函数，传一个假的 manager 就能单测，不必起窗口；
* :class:`StatusSnapshot` 是三段式状态栏的唯一输入，界面与诊断读同一份数据。

图片来源由 ``manager`` 的工厂注入（`RemoteImageManager` 共享缓存 / `ImageManager`
本地解码），本模块对两者一视同仁，因为它俩接口完全一致。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Callable

from ui import ViewRect, ViewSize, ViewTransform, format_zoom

from .image_manager import ImageManager, format_size

#: 幻灯片可选间隔（秒）。菜单与帮助页都从这一份读，不会各写一份。
SLIDESHOW_INTERVALS: tuple[int, ...] = (1, 2, 3, 5, 10)

#: 图片管理器的工厂：main.py 在 render 之前注入共享缓存版；不注入时本地解码。
_manager_factory: Callable[[], ImageManager] = ImageManager


def set_manager_factory(factory: Callable[[], ImageManager]) -> None:
    """注入管理器工厂（每个窗口进程各有一份状态与缓存句柄）。"""
    global _manager_factory
    _manager_factory = factory


@dataclass(frozen=True, slots=True)
class StatusSnapshot:
    """底部状态栏三段的文案：文件名 / 规格 / 位置。

    不可变 + 可比较：``use_state`` 天然做浅比较，内容没变就不触发重绘。
    """

    name: str = ""
    detail: str = ""
    position: str = ""

    @property
    def has_image(self) -> bool:
        return bool(self.position)


@dataclass
class AppState:
    """一个窗口的全部可变状态；实例保存在 ``use_ref`` 中，回调直接读写最新值。"""

    manager: ImageManager = field(default_factory=lambda: _manager_factory())
    view: ViewTransform = field(default_factory=ViewTransform)
    fullscreen: bool = False
    slideshow_on: bool = False
    paused: bool = False
    interval: float = 3.0
    ctrl_held: bool = False
    shift_held: bool = False
    viewport: ViewSize = field(default_factory=lambda: ViewSize.of(900, 560))
    image_size: ViewSize = field(default_factory=ViewSize)
    gen: int = 0                                # 异步解码代际，防旧任务覆盖新状态
    slideshow_task: asyncio.Future | None = None
    sel_rect: ViewRect | None = None                # 框选（视口坐标，不可变）
    sel_drag_start: tuple[float, float] | None = None
    right_last: tuple[float, float] | None = None
    anim_playing: bool = False
    anim_paused: bool = False
    anim_task: asyncio.Future | None = None


def format_status(
    manager: ImageManager,
    view: ViewTransform,
    image_size: ViewSize,
    viewport: ViewSize,
) -> StatusSnapshot:
    """把管理器与视图状态算成三段式状态栏文案（纯函数）。

    ``info`` 的总数为 0 时整条留白：空窗口不该显示「0 / 0」这种没有信息量的字。
    「缓存」标记这一帧直接读自共享缓存，本窗口没有解码 —— 是诊断 UI 的关键一句。
    """
    info = manager.info()
    detail = f"{info['w']}×{info['h']} · {format_size(info['size'])}"
    if info["frames"] > 1:
        detail += f" · 动图 {manager.current_frame + 1}/{info['frames']}帧"
    if getattr(manager, "last_shared_hit", False):
        detail += " · 缓存"
    position = ""
    if info["total"]:
        position = f"{info['pos']} / {info['total']} · {format_zoom(view, image_size, viewport)}"
        return StatusSnapshot(info["name"], detail, position)
    return StatusSnapshot(info["name"], "", "")


def format_service_info(info: dict | None) -> str:
    """把共享缓存统计排成弹窗里的多行文本（纯函数，可单测）。

    信息密度按「先看角色、再看容量、最后看命中」排；末行留一句人话解释状态栏
    的「缓存」标记，让诊断结果自己说明该怎么读。
    """
    if not info:
        return "（无数据）"
    if info.get("note"):
        return str(info["note"])
    lines = [
        f"角色：{info.get('role', '-')}",
        f"服务进程：pid {info.get('pid', '-')}",
        f"连接：{info.get('backend', '-')}",
    ]
    blobs = info.get("blobs") or {}
    if blobs:
        lines += [
            "",
            f"磁盘缓存：{blobs.get('count', 0)} 个 · {format_size(blobs.get('bytes', 0))}"
            f"（上限 {format_size(blobs.get('limit_bytes', 0))}）",
            f"命中 {blobs.get('hits', 0)} 次 · 未命中 {blobs.get('misses', 0)} 次",
        ]
        hits, misses = blobs.get("hits", 0), blobs.get("misses", 0)
        if hits + misses:
            lines.append(f"命中率：{hits * 100 // (hits + misses)}%")
    decoded = info.get("decoded") or {}
    if decoded:
        lines.append(
            f"解码缓存：{decoded.get('entries', 0)} 张 · "
            f"{decoded.get('megapixels', 0)} MP"
        )
    counters = info.get("counters") or {}
    if counters:
        lines.append(
            f"预加载：完成 {counters.get('preload_done', 0)} · "
            f"排队 {info.get('pending', 0)}"
        )
    if info.get("error"):
        lines += ["", f"错误：{info['error']}"]
    lines += ["", "状态栏出现「缓存」= 这一帧直接读自共享缓存，本窗口没有解码。"]
    return "\n".join(lines)
