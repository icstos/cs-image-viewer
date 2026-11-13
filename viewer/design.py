"""设计层 —— cs-ui 令牌在 ImageViewer 里的**唯一语义投影**。

这里不定义任何颜色、字号、间距或圆角：色表由 :class:`ui.MediaPalette` 从品牌色
与明暗档推导，排印 / 图标 / 4px 网格全部取自 ``ui.core.tokens``。本模块只做两件
事：

1. 声明这个产品唯一的自选输入（品牌色 + 固定深色档），交给 ``MediaPalette.derive``
   算出整套色表；
2. 把色表与令牌投影成业务侧读得懂的名字（``C_CANVAS`` / ``MENU_H`` …）。

于是「换配色」只改一个常量；「查当前配色」读 :func:`snapshot`；「核对某个间距
有没有越界」对照 ``SPACE`` 即可 —— 用户看到的效果、开发者读的代码、Agent 检索
的事实是同一份数据。

深色是看图软件的固定档：图片本身是内容，浅色界面会在四周形成一圈亮边框抢注意力。
"""

from __future__ import annotations

import ui
from ui import FONT_SIZE, ICON_SIZE, RADIUS, SPACE
from ui.core.tokens import PRIMARY_SEEDS
from ui.display.media import MediaPalette

# ─── 唯一色值输入 ────────────────────────────────────────────────────────
#: 品牌色沿用 cs-ui 默认蓝：算出来的中性表面带一丝冷蓝相，接近中性灰但不发闷。
BRAND_PRIMARY: str = PRIMARY_SEEDS["blue"]
#: 看图软件固定深色（画面是内容，浅色边框会抢注意力）。
MODE: str = "dark"
#: 14 个语义角色，由品牌色 + 明暗档唯一决定，带缓存、可复算。
PALETTE = MediaPalette.derive(BRAND_PRIMARY, mode=MODE)

# ─── 表面：由深到浅，四级梯度 ────────────────────────────────────────────
C_CANVAS = PALETTE.darkest         # 图片区（最沉浸的一层）
C_SURFACE = PALETTE.surface        # 窗口底衬：任何漏画帧露出的都是深色
C_PANEL = PALETTE.panel            # 菜单栏 / 状态栏 / 弹窗
C_DIVIDER = PALETTE.divider        # 菜单内分隔线
C_LINE = PALETTE.border            # 面板描边

# ─── 文字：三档视觉重量 ──────────────────────────────────────────────────
C_TEXT = PALETTE.text
C_MUTED = PALETTE.muted
C_DIM = ui.Colors.with_opacity(0.62, C_TEXT)   # 空态图标 / 极次要说明
C_PRIMARY = PALETTE.primary                    # 选中态 / 框选描边

# ─── 布局：全部落在 4px 网格上 ───────────────────────────────────────────
MENU_H: float = float(SPACE["12"])             # 48
STATUS_H: float = float(SPACE["8"])            # 32
NAV_MARGIN: float = float(SPACE["3"]) + 2      # 14（贴边留白）
CHROME_H: float = MENU_H + STATUS_H            # 80，非全屏时视口高度要减掉它

# ─── 排印 / 图标：只取 cs-ui 字阶与图标刻度 ──────────────────────────────
FONT_MENU = FONT_SIZE["small"]                 # 12
FONT_STATUS = FONT_SIZE["small"]               # 12
FONT_HINT = FONT_SIZE["small"]                 # 12
FONT_DIALOG = FONT_SIZE["body"]                # 14
FONT_TITLE = FONT_SIZE["title"]                # 16
ICON_MENU = ICON_SIZE["sm"]                    # 14
ICON_EMPTY = SPACE["12"] + SPACE["2"]          # 56：空态图标（48 + 8）

# ─── 间距 / 圆角 ────────────────────────────────────────────────────────
PAD_XS = SPACE["1"]                            # 4
PAD_SM = SPACE["2"]                            # 8
PAD_MD = SPACE["3"]                            # 12
PAD_LG = SPACE["4"]                            # 16
PAD_XL = SPACE["6"]                            # 24
RADIUS_SM = RADIUS["sm"]
RADIUS_MD = RADIUS["md"]
RADIUS_LG = RADIUS["lg"]


def snapshot() -> dict[str, object]:
    """设计层快照：Agent / 诊断 / 文档可直接读，不必反推一堆常量。

    同参数必然得到同结果（色表由品牌色推导，尺寸由令牌投影），是「可复算」的
    落脚点；界面上任何一处出现对不上的颜色，先和这份快照比。
    """
    return {
        "brand": BRAND_PRIMARY,
        "mode": MODE,
        "colors": PALETTE.as_dict(),
        "derived": {
            "canvas": C_CANVAS,
            "surface": C_SURFACE,
            "panel": C_PANEL,
            "line": C_LINE,
            "text": C_TEXT,
            "muted": C_MUTED,
            "dim": C_DIM,
        },
        "layout": {
            "menu_h": MENU_H,
            "status_h": STATUS_H,
            "chrome_h": CHROME_H,
            "nav_margin": NAV_MARGIN,
        },
        "type": {
            "menu": FONT_MENU,
            "status": FONT_STATUS,
            "dialog": FONT_DIALOG,
            "title": FONT_TITLE,
            "icon_menu": ICON_MENU,
            "icon_empty": ICON_EMPTY,
        },
    }
