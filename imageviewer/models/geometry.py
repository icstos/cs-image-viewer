"""视图几何 —— 模型层的**值语义**：尺寸、倍率、平移、锚点缩放、框选、滚轮意图。

这一层没有任何 I/O、没有控件、没有全局可变状态：给同样的入参，任何环境、任何
时刻都会得到同样的出参。因此它同时是三个角色的唯一事实源：

* **可测**：``compute_scale(...) == 0.5`` 这类断言不必起窗口；
* **可复算**：状态栏写的倍率、渲染摆位、框选命中用的是同一组函数，不存在偏漂；
* **可换壳**：视图层随时可以重画，坐标约定仍然只有这一份。

坐标约定（**所有函数共用，越界即错**）
--------------------------------------
* ``image`` 是解码后图像的像素尺寸，``viewport`` 是可视区逻辑像素尺寸；
* ``view.pan_x / pan_y`` 是相对「居中位置」的偏移，不是图片左上角坐标；
* ``anchor`` / 选区一律用**视口坐标**（左上角为原点），不是图像坐标；
* 变换一律**返回新对象**，不就地修改：调用方拿 ``view == next_view`` 就能判断
  有没有变化，也不必担心异步任务里旧闭包读到被改过一半的状态。

.. note::
   几何函数放在模型层是刻意的读法：它们是**值对象的语义**（两个 ``ViewTransform``
   是否相等、某个矩形是否命中某点），不是业务流程。真正的业务流程（先适应窗口
   还是先适应宽度、翻页阈值取「图片全可见」）在 ``service`` / ``viewmodel`` 里
   由调用顺序表达；这里只提供可复算的关系式。本模块只依赖标准库。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = [
    "MAX_ZOOM",
    "MIN_REGION",
    "MIN_ZOOM",
    "NAV_MARGIN",
    "NAV_SIZE",
    "ZOOM_STEP",
    "ScrollIntent",
    "ViewMode",
    "ViewPoint",
    "ViewRect",
    "ViewSize",
    "ViewTransform",
    "clamp_pan",
    "clamp_zoom",
    "compute_scale",
    "image_rect",
    "nav_geometry",
    "scroll_intent",
    "zoom_anchored",
    "zoom_by",
    "zoom_to_region",
]

#: 缩放边界与步进。10% ~ 1000% 覆盖缩略图到像素级检查；1.12 是每格滚轮「看得见
#: 又不跳」的经验值（连续滚 10 格约变 3.5 倍，与多数看图软件一致）。
MIN_ZOOM: float = 0.1
MAX_ZOOM: float = 10.0
ZOOM_STEP: float = 1.12

#: 小于这个边长的框选视为误拖（一次普通点击带出的 1~2px 抖动不该放大到全屏）。
MIN_REGION: float = 4.0

#: 悬浮翻页按钮的桌面端舒适下限：44 是 Fitts 定律下常见的最小点击目标。
NAV_SIZE: float = 46.0
NAV_MARGIN: float = 14.0

ViewMode = Literal["fit", "fit_width", "actual", "custom"]


@dataclass(frozen=True, slots=True)
class ViewSize:
    """宽高对。``valid`` 统一拦掉 0 / 负数，避免下游除法炸掉。"""

    width: float = 0.0
    height: float = 0.0

    @property
    def valid(self) -> bool:
        return self.width > 0 and self.height > 0

    @classmethod
    def of(cls, width: float, height: float) -> ViewSize:
        return cls(float(width), float(height))


@dataclass(frozen=True, slots=True)
class ViewPoint:
    """视口内的一点（左上角为原点）。"""

    x: float = 0.0
    y: float = 0.0


@dataclass(frozen=True, slots=True)
class ViewRect:
    """轴对齐矩形：位置用左上角，尺寸恒为正。"""

    left: float
    top: float
    width: float
    height: float

    @classmethod
    def from_corners(cls, x0: float, y0: float, x1: float, y1: float) -> ViewRect:
        """两点定矩形；拖动方向任意（右下往左上拖也一样）。"""
        return cls(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))

    @property
    def right(self) -> float:
        return self.left + self.width

    @property
    def bottom(self) -> float:
        return self.top + self.height

    @property
    def center(self) -> ViewPoint:
        return ViewPoint(self.left + self.width / 2, self.top + self.height / 2)

    @property
    def is_dragged_region(self) -> bool:
        """是否够格当作「框选放大」的区域。"""
        return self.width >= MIN_REGION and self.height >= MIN_REGION

    def contains(self, point: ViewPoint) -> bool:
        return (
            self.left <= point.x <= self.right
            and self.top <= point.y <= self.bottom
        )


@dataclass(frozen=True, slots=True)
class ViewTransform:
    """一次视图变换：显示模式 + 自定义倍率 + 相对居中位置的平移。

    ``mode`` 四选一：

    ============= ==========================================
    ``fit``       适应窗口（等比，取宽高较小的那个比例）
    ``fit_width`` 适应宽度（高度可溢出）
    ``actual``    1:1 原始像素
    ``custom``    用 ``zoom`` 作为倍率（滚轮 / 键盘缩放后进入）
    ============= ==========================================
    """

    mode: ViewMode = "fit"
    zoom: float = 1.0
    pan_x: float = 0.0
    pan_y: float = 0.0

    def reset(self) -> ViewTransform:
        """回到适应窗口，复位平移。"""
        return ViewTransform()

    def with_mode(self, mode: ViewMode) -> ViewTransform:
        """切模式时**清平移**：新倍率下的旧偏移会把图片推到看不见的地方。"""
        return ViewTransform(mode=mode, zoom=self.zoom)


@dataclass(frozen=True, slots=True)
class ScrollIntent:
    """一次滚轮事件的**意图**，由 :func:`scroll_intent` 判定。

    把「Ctrl 是缩放 / 全可见是翻页 / 溢出是滚动」这条分派规则从事件回调里搬出来，
    回调就只剩「执行」这一件事，规则本身可以被单测逐条钉住。

    Attributes:
        kind: ``"zoom"`` / ``"page"`` / ``"pan"`` / ``"none"``。
        direction: ``-1`` 向上（上一张 / 放大 / 上滚），``1`` 向下。
        factor: 缩放系数（仅 ``zoom`` 有意义）。
        delta: 平移像素数（仅 ``pan`` 有意义，恒为正）。
    """

    kind: Literal["zoom", "page", "pan", "none"]
    direction: int = 0
    factor: float = 1.0
    delta: float = 0.0


# ---------------------------------------------------------------------------
# 基础计算
# ---------------------------------------------------------------------------
def clamp_zoom(value: float) -> float:
    """把倍率夹进 :data:`MIN_ZOOM` ~ :data:`MAX_ZOOM`。"""
    return max(MIN_ZOOM, min(MAX_ZOOM, value))


def compute_scale(view: ViewTransform, image: ViewSize, viewport: ViewSize) -> float:
    """按显示模式求实际渲染倍率（严格保持宽高比）。

    任一尺寸无效时返回 ``1.0``：调用方随后会拿它算显示宽高，返回 0 会让整块
    图片消失，返回 1 至少留得住轮廓。
    """
    if not image.valid or not viewport.valid:
        return 1.0
    if view.mode == "fit":
        return min(viewport.width / image.width, viewport.height / image.height)
    if view.mode == "fit_width":
        return viewport.width / image.width
    if view.mode == "actual":
        return 1.0
    return clamp_zoom(view.zoom)


def clamp_pan(
    view: ViewTransform,
    image: ViewSize,
    viewport: ViewSize,
    scale: float | None = None,
) -> ViewTransform:
    """把平移夹在边界内：图片边缘不许进入视口；未溢出时强制居中。

    返回**新对象**；已经合法时原样返回（``is`` 相同），调用方可据此跳过重绘。
    """
    s = compute_scale(view, image, viewport) if scale is None else scale
    if not image.valid or not viewport.valid or s <= 0:
        return view
    max_x = max(0.0, (image.width * s - viewport.width) / 2)
    max_y = max(0.0, (image.height * s - viewport.height) / 2)
    pan_x = min(max(-max_x, view.pan_x), max_x)
    pan_y = min(max(-max_y, view.pan_y), max_y)
    if pan_x == view.pan_x and pan_y == view.pan_y:
        return view
    return ViewTransform(view.mode, view.zoom, pan_x, pan_y)


def image_rect(
    image: ViewSize, viewport: ViewSize, view: ViewTransform, scale: float | None = None
) -> ViewRect:
    """图片在视口中的实际矩形（含平移），供渲染与命中测试共用。

    渲染用它摆位、框选用它把视口坐标换算回图像坐标 —— 两边用同一个函数，
    才不会出现「看到的图」和「算出来的选区」错开几像素。
    """
    s = compute_scale(view, image, viewport) if scale is None else scale
    width = max(image.width * s, 1.0)
    height = max(image.height * s, 1.0)
    return ViewRect(
        left=(viewport.width - width) / 2 + view.pan_x,
        top=(viewport.height - height) / 2 + view.pan_y,
        width=width,
        height=height,
    )


# ---------------------------------------------------------------------------
# 变换
# ---------------------------------------------------------------------------
def zoom_by(
    view: ViewTransform, image: ViewSize, viewport: ViewSize, factor: float
) -> ViewTransform:
    """键盘 + / - 的**视口中心**缩放，从当前渲染倍率起步。

    从 ``fit`` 按一次「+」时若直接拿 ``zoom=1.0`` 乘系数，一张被缩到 12% 的大图
    会瞬间跳到 112% —— 这正是多数看图软件「放大键像失灵」的成因。这里复用
    :func:`zoom_anchored`，倍率与平移一起算，视口中心那一点始终不动。
    """
    center = ViewPoint(viewport.width / 2, viewport.height / 2)
    return zoom_anchored(view, image, viewport, center, factor)


def zoom_anchored(
    view: ViewTransform,
    image: ViewSize,
    viewport: ViewSize,
    anchor: ViewPoint,
    factor: float,
) -> ViewTransform:
    """以视口内 ``anchor`` 为锚点缩放：该点下的图片像素缩放前后保持不动。

    返回新变换；倍率没变（已到上下限）时原样返回。推导只有一步：
    先由「当前矩形」把锚点反解成图像坐标，再令缩放后同一像素仍落在锚点处，
    最后用 :func:`clamp_pan` 收敛边界。

    .. note::
        贴边锚点 + 图片几乎不溢出时，算出的平移会越界。此时 **边界优先于锚点**
        （``clamp_pan`` 保证图片边缘不进视口），锚点下的像素会有一个受夹取量
        限制的小偏移。宁可让锚点偏几像素，也不能让图片边缘露出背景。
    """
    if not image.valid or not viewport.valid:
        return view
    s0 = compute_scale(view, image, viewport)
    s1 = clamp_zoom(s0 * factor)
    if abs(s1 - s0) < 1e-9:
        return view
    rect = image_rect(image, viewport, view, s0)
    ix = (anchor.x - rect.left) / s0
    iy = (anchor.y - rect.top) / s0
    next_view = ViewTransform(
        mode="custom",
        zoom=s1,
        pan_x=anchor.x - ix * s1 - (viewport.width - image.width * s1) / 2,
        pan_y=anchor.y - iy * s1 - (viewport.height - image.height * s1) / 2,
    )
    return clamp_pan(next_view, image, viewport, s1)


def zoom_to_region(
    view: ViewTransform,
    image: ViewSize,
    viewport: ViewSize,
    region: ViewRect,
) -> ViewTransform:
    """把框选区域放大到填满视野（保持宽高比、中心对准视野中心）。

    过小的选区（:attr:`ViewRect.is_dragged_region`）原样退回 —— 单击带出的抖动不该
    触发放大。越界的选区由视口矩形自然夹住图像坐标，不会把图推飞。
    """
    if not image.valid or not viewport.valid or not region.is_dragged_region:
        return view
    s0 = compute_scale(view, image, viewport)
    if s0 <= 0:
        return view
    rect = image_rect(image, viewport, view, s0)
    ix0 = (region.left - rect.left) / s0
    iy0 = (region.top - rect.top) / s0
    iw, ih = region.width / s0, region.height / s0
    if iw <= 0 or ih <= 0:
        return view
    s1 = clamp_zoom(min(viewport.width / iw, viewport.height / ih))
    cx, cy = ix0 + iw / 2, iy0 + ih / 2
    next_view = ViewTransform(
        mode="custom",
        zoom=s1,
        pan_x=viewport.width / 2 - cx * s1 - (viewport.width - image.width * s1) / 2,
        pan_y=viewport.height / 2 - cy * s1 - (viewport.height - image.height * s1) / 2,
    )
    return clamp_pan(next_view, image, viewport, s1)


# ---------------------------------------------------------------------------
# 交互意图
# ---------------------------------------------------------------------------
def scroll_intent(
    image: ViewSize,
    viewport: ViewSize,
    view: ViewTransform,
    delta_y: float,
    *,
    ctrl: bool = False,
) -> ScrollIntent:
    """把一次滚轮事件判成「缩放 / 翻页 / 滚动」三种意图之一。

    规则（Windows 实测上滚 ``delta_y < 0``）：

    ============ ==========================================================
    Ctrl         → 缩放，上滚放大、下滚缩小（锚点由调用方给）
    图片全可见    → 翻页，上滚上一张、下滚下一张
    图片溢出      → 滚动浏览；步长随滚轮幅度，边界由 ``clamp_pan`` 兜住
    ============ ==========================================================

    只**判定**不执行：调用方按 ``kind`` 分派即可，规则本身可以脱离页面单测。
    """
    if delta_y == 0:
        return ScrollIntent("none")
    direction = -1 if delta_y < 0 else 1
    if ctrl:
        factor = ZOOM_STEP if delta_y < 0 else 1 / ZOOM_STEP
        return ScrollIntent("zoom", direction, factor=factor)
    if not image.valid or not viewport.valid:
        return ScrollIntent("none", direction)
    scale = compute_scale(view, image, viewport)
    if image.width * scale <= viewport.width and image.height * scale <= viewport.height:
        return ScrollIntent("page", direction)
    step = max(60.0, viewport.height * 0.12) * max(1.0, abs(delta_y) / 100.0)
    return ScrollIntent("pan", direction, delta=step)


def nav_geometry(
    viewport: ViewSize,
    *,
    size: float = NAV_SIZE,
    margin: float = NAV_MARGIN,
    fullscreen: bool = False,
) -> tuple[float, float, float]:
    """左右悬浮翻页按钮的 ``(左距, 右距, 上距)``。

    全屏时离屏幕边缘再远一点：贴边既难点，也显得局促；垂直方向永远居中，
    但至少留 8px，极扁的窗口里按钮不会顶到菜单栏。
    """
    gap = margin * 1.8 if fullscreen else margin
    left = gap
    right = max(viewport.width - gap - size, gap)
    top = max((viewport.height - size) / 2, 8.0)
    return left, right, top
