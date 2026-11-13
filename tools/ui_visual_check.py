"""ImageViewer 真机截图 + 定量验收（Windows）。

纯逻辑自检抓不到、只有截图能发现的三类事故：

* **ErrorWidget 灰块**（``#BDBDBD`` 附近）：控件树里混入不可渲染的值；
* **Flutter 溢出**（debug 下纯黄斜纹）：内容撑破固定高度盒子；
* **条件渲染没出现**：多图目录时左右两枚玻璃箭头应当真的画出来 —— 这次
  ``IconButton`` 挂树崩溃正是单图启动冒烟漏掉的那条路径。

用法::

    py -3.12 tools/ui_visual_check.py <图片目录>   # 多图：含翻页箭头
    py -3.12 tools/ui_visual_check.py               # 空窗口

截图存到 ``output/ui_visual/``，可直接打开比对。脚本自己拉起窗口、自己关掉，
不依赖 CSIV_SMOKE，也不改用户数据（共享服务仍是常驻那个）。
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "output" / "ui_visual"
TITLE = "ImageViewer"

#: 窗口置顶标志：抢不到前台时（Windows 会拒绝非前台进程的 SetForegroundWindow），
#: 屏幕抓取就会抓到盖在上面的 IDE 或浏览器。置顶比抢焦点可靠得多。
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
user32.FindWindowW.restype = wt.HWND
user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
user32.PrintWindow.argtypes = [wt.HWND, wt.HDC, wt.UINT]
try:
    user32.SetProcessDPIAware()          # 让窗口矩形与屏幕像素同一坐标系
except AttributeError:
    pass


# ─── PrintWindow 抓帧所需的结构（GDI 的 BITMAPINFO）────────────────────────
class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


class _RGBQUAD(ctypes.Structure):
    _fields_ = [("rgbBlue", ctypes.c_ubyte), ("rgbGreen", ctypes.c_ubyte),
                ("rgbRed", ctypes.c_ubyte), ("rgbReserved", ctypes.c_ubyte)]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", _RGBQUAD * 1)]


def capture(hwnd: int):
    """抓窗口**自身**内容，返回 PIL Image。

    为什么不用 ``ImageGrab``：它是屏幕抓取，窗口一旦不在前台（Windows 会拒绝
    非前台进程的 ``SetForegroundWindow``），拍到的就是盖在上面的 IDE / 浏览器。
    实测抓到过 WorkBuddy 界面：那张图里画布色占比 0、右侧一片亮 —— 会被误判成
    「箭头错位」。``PrintWindow`` + ``PW_RENDERFULLCONTENT`` 让 Flutter 自己把帧
    画进内存 DC，窗口被完全挡住也能拿到真实内容。

    返回的是**客户区逻辑尺寸**（Flutter 按逻辑像素绘制），所以比屏幕截图小一圈；
    比例类指标（灰块占比、箭头亮像素占比）不受影响。
    """
    from PIL import Image

    r = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0:
        raise RuntimeError(f"客户区尺寸异常：{w}x{h}")

    # 说明：PrintWindow 把帧画进我们给的 DC，但**按窗口的逻辑像素**绘制，而本进程
    # 是 DPI 感知的，GetClientRect 给的是物理像素。两者不换算，内容就只填左上角、
    # 右侧一片纯黑（箭头检测直接失效）。这里不去猜缩放比（GetDpiForWindow 实测
    # 报 1.5，真实 DPR 是 1.75，猜错更糟），而是按物理尺寸抓、再裁到非纯黑的
    # 内容包围盒 —— 直接量结果，不推理过程。

    hdc_win = user32.GetDC(hwnd)
    hdc = gdi32.CreateCompatibleDC(hdc_win)
    bmp = gdi32.CreateCompatibleBitmap(hdc_win, w, h)
    gdi32.SelectObject(hdc, bmp)
    try:
        # 连续抓两次取第二次：首帧可能仍是 Flutter 尚未提交的旧 surface。
        user32.PrintWindow(hwnd, hdc, 2)     # 2 = PW_RENDERFULLCONTENT
        ok = user32.PrintWindow(hwnd, hdc, 2)

        bi = _BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = w
        bi.bmiHeader.biHeight = -h           # 负号 = 自上而下，省去翻转
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = 0
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(hdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    finally:
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(hdc)
        user32.ReleaseDC(hwnd, hdc_win)

    if not ok:
        raise RuntimeError("PrintWindow 返回 0")
    img = Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)

    # 裁到真实内容：画布色是 #0E1013（不是纯黑），未绘制区域才是 (0,0,0)。
    # 整块纯黑时不裁，交给 canvas_ratio 判据报「抓错窗口」。
    box = img.convert("L").point(lambda v: 255 if v > 2 else 0).getbbox()
    if box and box[2] - box[0] > w * 0.3 and box[3] - box[1] > h * 0.3:
        img = img.crop(box)
    return img


def find_window(timeout: float = 30.0) -> int:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        hwnd = user32.FindWindowW(None, TITLE)
        if hwnd:
            return hwnd
        time.sleep(0.3)
    return 0


def rect_of(hwnd: int) -> tuple[int, int, int, int]:
    """客户区在屏幕上的矩形。

    用 ``GetWindowRect`` 会把 DWM 的不可见边框一起框进来，截图左右各会透出
    十几像素的桌面内容（看起来像应用有杂边）。客户区才是真正要验收的画面。
    """
    r = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    pt = wt.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return pt.x, pt.y, pt.x + (r.right - r.left), pt.y + (r.bottom - r.top)


def _pixels(img):
    fetch = getattr(img, "get_flattened_data", None) or img.getdata
    return list(fetch())


def analyse(path: Path) -> dict[str, object]:
    from PIL import Image

    from viewer import design

    canvas = design.C_CANVAS.lstrip("#")
    cr, cg, cb = (int(canvas[i:i + 2], 16) for i in (0, 2, 4))

    img = Image.open(path).convert("RGB")
    w, h = img.size
    core = img.crop((int(w * 0.2), int(h * 0.15), int(w * 0.95), int(h * 0.92)))
    pixels = _pixels(core)
    grey = sum(1 for r, g, b in pixels
               if abs(r - g) <= 6 and abs(g - b) <= 6 and 0xAC <= r <= 0xCC)
    overflow = sum(1 for r, g, b in pixels if r >= 240 and g >= 225 and b <= 80)
    # 抓错窗口的定量信号：ImageViewer 的图片区底色必然占相当面积，
    # 抓到 IDE / 浏览器时这一项会掉到 0。
    canvas_ratio = sum(1 for r, g, b in pixels
                       if abs(r - cr) <= 6 and abs(g - cg) <= 6 and abs(b - cb) <= 6
                       ) / max(1, len(pixels))

    def bright_ratio(x0: float, x1: float) -> float:
        """箭头所在竖带里「亮且低饱和」像素占比：白色图标的定量信号。"""
        band = img.crop((int(w * x0), int(h * 0.42), int(w * x1), int(h * 0.58)))
        px = _pixels(band)
        return sum(1 for r, g, b in px
                   if r >= 170 and g >= 170 and b >= 170) / max(1, len(px))

    return {
        "size": f"{w}x{h}",
        "grey_ratio": round(grey / max(1, len(pixels)), 4),
        "overflow_px": overflow,
        "unique_colors": len(set(pixels)),
        "canvas_ratio": round(canvas_ratio, 4),
        "left_bright": round(bright_ratio(0.02, 0.13), 4),
        "right_bright": round(bright_ratio(0.87, 0.98), 4),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="ImageViewer 真机截图 + 灰块/溢出/箭头检测")
    ap.add_argument("folder", nargs="?", default="", help="图片目录（多图时显示翻页箭头）")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(ROOT / "main.py")]
    if args.folder:
        cmd.append(args.folder)
    env = os.environ.copy()
    env.pop("CSIV_SMOKE", None)           # 这次要窗口一直开着

    proc = subprocess.Popen(
        cmd, cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )
    try:
        hwnd = find_window()
        if not hwnd:
            print("FAIL 没找到 ImageViewer 窗口")
            return 1
        # 置顶 + 前置：SetForegroundWindow 常被系统拒绝，单靠它保证不了可见性。
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)
        user32.ShowWindow(hwnd, 9)        # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        time.sleep(2.5)                   # 等首帧、图片解码与预加载稳定

        shot = OUT / ("multi.png" if args.folder else "empty.png")
        try:
            capture(hwnd).save(shot)
        except Exception as exc:           # 少数驱动下 PrintWindow 不可用
            print(f"  ! PrintWindow 失败（{exc}），回退屏幕抓取")
            from PIL import ImageGrab
            ImageGrab.grab(bbox=rect_of(hwnd), all_screens=True).save(shot)
        report = analyse(shot)

        print(f"截图：{shot}")
        for key, value in report.items():
            print(f"  {key}: {value}")

        ok = (
            report["grey_ratio"] < 0.55        # 没有被 ErrorWidget 灰块占满
            and report["overflow_px"] == 0     # 没有 Flutter 溢出斜纹
            and report["unique_colors"] > 20   # 不是纯色/全黑空帧
            and report["canvas_ratio"] > 0.05  # 抓到的确实是本应用（图片区底色在）
        )
        if report["canvas_ratio"] <= 0.05:
            print("  ! 画布色几乎不存在：多半抓到了盖在上面的别的窗口，重跑一次")
        if args.folder:
            # 两枚玻璃箭头各应贡献一小片亮图标像素；单图时这两项接近 0。
            ok = ok and report["left_bright"] > 0.0005 and report["right_bright"] > 0.0005
        print("结果：" + ("通过" if ok else "失败"))
        return 0 if ok else 1
    finally:
        try:
            user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE)
        except NameError:                 # 没走到 find_window 时 hwnd 不存在
            pass
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
