"""界面状态模型 —— **声明式 UI 的那个「状态」**。

整个应用只有一份 :class:`ViewerState`：它冻结、可比较、可打印，界面是它的
纯函数（``UI = f(state)``）。因此这里出现的每个字段都必须回答一个问题：
「界面上的哪一处由它决定？」答不上来的字段不该在这里。

同理，状态里**没有控件**：只有字符串、数字、几何值域与几个小 dataclass。这让
状态可以被单测直接构造、被日志直接打印、被 Agent 直接比对，而不需要起窗口。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from .geometry import ViewRect, ViewSize, ViewTransform

__all__ = [
    "DialogKind",
    "DialogRequest",
    "Notice",
    "RequestKind",
    "StatusSnapshot",
    "Tone",
    "ViewerState",
]


class Tone(StrEnum):
    """回执语气。渲染层把它映射成 cs-ui 的 ``StyleType``，与文案解耦。"""

    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


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


@dataclass(frozen=True, slots=True)
class Notice:
    """一次性回执（toast）。

    ``seq`` 是自增序号：文案可能连续两次完全相同（例如连按两下到边界），
    仅靠内容比较会漏掉第二次。渲染层用 ``(seq)`` 作为 effect 的依赖。
    """

    seq: int = 0
    message: str = ""
    tone: Tone = Tone.INFO
    seconds: float = 2.5


class RequestKind(StrEnum):
    """需要**视图**协助的输入请求（选文件、选目录、存盘）。

    viewmodel 不持有文件对话框（那是控件），但它必须决定「什么时候该弹」。
    于是它把请求写成状态的一格：视图看到就弹、弹完回填、然后把它清空。
    这样「点菜单打开图片」仍然走同一张动作表，不需要在视图里再分一次流。
    """

    PICK_FILE = "pick_file"
    PICK_FOLDER = "pick_folder"
    PICK_MANY = "pick_many"
    SAVE_FRAME = "save_frame"


class DialogKind(StrEnum):
    """当前弹出的对话框种类。确认动作由 viewmodel 按种类分派。"""

    NONE = "none"
    DELETE = "delete"
    ASSOCIATION = "association"
    ASSOCIATION_STATUS = "association_status"
    SERVICE_REPORT = "service_report"


@dataclass(frozen=True, slots=True)
class DialogRequest:
    """一个已经排版好的对话框请求（标题、正文、按钮文案都在这里）。

    视图层只负责把它画成 ``ui.AlertDialog``：它不做文案拼装，也不决定
    「点确认之后干什么」—— 那是 :meth:`ViewerState` 旁边的 viewmodel 的事。
    """

    kind: DialogKind = DialogKind.NONE
    heading: str = ""
    message: str = ""
    confirm_label: str = ""
    payload: str = ""
    danger: bool = False
    modal: bool = True
    selectable: bool = False

    @property
    def confirmable(self) -> bool:
        """是否有「确认」这条路（只读信息框只有关闭）。"""
        return bool(self.confirm_label)


@dataclass(frozen=True, slots=True)
class ViewerState:
    """一个窗口的全部界面状态。

    分组看比逐字段看更清楚：

    ============ ==========================================================
    画面          ``src`` / ``image_size`` / ``view`` / ``viewport`` / ``selection``
    模式          ``fullscreen`` / ``slideshow_on`` / ``paused`` / ``interval``
    动图          ``anim_playing`` / ``anim_paused`` / ``has_frames``
    chrome       ``status`` / ``show_nav`` / ``ready`` / ``dialog`` / ``notice``
    生命周期      ``closing``
    ============ ==========================================================
    """

    # ── 画面 ──
    src: str | None = None
    image_size: ViewSize = field(default_factory=ViewSize)
    view: ViewTransform = field(default_factory=ViewTransform)
    viewport: ViewSize = field(default_factory=lambda: ViewSize.of(900, 560))
    selection: ViewRect | None = None

    # ── 模式 ──
    fullscreen: bool = False
    slideshow_on: bool = False
    paused: bool = False
    interval: float = 3.0

    # ── 动图 ──
    anim_playing: bool = False
    anim_paused: bool = False
    has_frames: bool = False

    # ── chrome ──
    status: StatusSnapshot = field(default_factory=StatusSnapshot)
    show_nav: bool = False
    ready: bool = True
    dialog: DialogRequest | None = None
    notice: Notice | None = None
    request: RequestKind | None = None

    # ── 生命周期 ──
    closing: bool = False

    @property
    def has_image(self) -> bool:
        """有可显示的图片（状态栏右段非空即代表有图）。"""
        return self.status.has_image

    @property
    def show_placeholder(self) -> bool:
        """是否画空态。

        启动参数还在解码时不画占位：否则会闪一下「还没有打开图片」，
        紧接着被真实图片替代 —— 一个只在慢盘上出现、却很难查的视觉抖动。
        """
        return self.ready and self.src is None
