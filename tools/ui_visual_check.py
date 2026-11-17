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
sys.path.insert(0, str(ROOT / "src"))

OUT = ROOT / "output" / "ui_visual"
TITLE = "ImageViewer"

#: 窗口置顶标志：抢不到前台时（Windows 会拒绝非前台进程的 SetForegroundWindow），
#: 屏幕抓取就会抓到盖在上面的 IDE 或浏览器。置顶比抢焦点可靠得多。
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
WM_CLOSE = 0x0010

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
user32.FindWindowW.restype = wt.HWND
user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
user32.PrintWindow.argtypes = [wt.HWND, wt.HDC, wt.UINT]
user32.IsWindow.argtypes = [wt.HWND]
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
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
    """抓窗口**自身**内容，返回 PIL Image（客户区像素）。

    为什么不用 ``ImageGrab``：它是屏幕抓取，窗口一旦不在前台（Windows 会拒绝
    非前台进程的 ``SetForegroundWindow``），拍到的就是盖在上面的 IDE / 浏览器。
    实测抓到过 WorkBuddy 界面：那张图里画布色占比 0、右侧一片亮 —— 会被误判成
    「箭头错位」。``PrintWindow`` + ``PW_RENDERFULLCONTENT`` 让 Flutter 自己把帧
    画进内存 DC，窗口被完全挡住也能拿到真实内容。

    **位图必须按整个窗口开**：``PrintWindow`` 会把标题栏之类的非客户区一起画进
    DC，DC 原点对齐窗口左上角。早先按客户区开位图，等于把整窗画进一块偏小的
    画布 —— 底部状态栏被整条裁掉（截图里图片一直铺到底，像布局挤爆了，其实只是
    抓帧少了 64 物理像素）。这里改成「整窗抓 → 按客户区偏移裁」。

    裁剪之后若内容仍只填左上角（某些驱动把帧按逻辑像素绘制），再按非纯黑包围盒
    兜底裁一次 —— 直接量结果，不猜缩放比。
    """
    from PIL import Image

    win = wt.RECT()
    cli = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(win))
    user32.GetClientRect(hwnd, ctypes.byref(cli))
    w, h = win.right - win.left, win.bottom - win.top
    cw, ch = cli.right - cli.left, cli.bottom - cli.top
    if w <= 0 or h <= 0 or cw <= 0 or ch <= 0:
        raise RuntimeError(f"窗口尺寸异常：窗 {w}x{h} 客户区 {cw}x{ch}")

    origin = wt.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(origin))
    left, top = origin.x - win.left, origin.y - win.top

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

    # 裁到客户区：去掉标题栏 / 边框，只留应用自己的画面。
    img = img.crop((left, top, min(left + cw, w), min(top + ch, h)))

    # 兜底：内容只填了一部分时，裁到真实内容包围盒。
    box = img.convert("L").point(lambda v: 255 if v > 2 else 0).getbbox()
    if box:
        bw, bh = box[2] - box[0], box[3] - box[1]
        if bw > img.width * 0.3 and bh > img.height * 0.3:
            img = img.crop(box)
    return img


def windows_titled(title: str) -> list[int]:
    """所有标题精确匹配的顶层窗口句柄（按 z 序，最前最先）。"""
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _collect(hwnd: int, _lparam: int) -> bool:
        caption = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, caption, 256)
        if caption.value == title:
            found.append(hwnd)
        return True

    user32.EnumWindows(_collect, 0)
    return found


def find_window(timeout: float = 30.0, exclude: set[int] | None = None) -> int:
    """等一个**新的** ImageViewer 窗口；``exclude`` 里的旧窗口跳过。

    开发模式下窗口属于 ``flet.exe`` 客户端进程，**不随 python 退出**（见
    :func:`close_window`）。上一次运行留下的断线窗口标题仍是 ImageViewer，
    直接取第一个就会抓到那张断线重连的白色引导页 —— 必须排除旧句柄。
    """
    skip = exclude or set()
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        for hwnd in windows_titled(TITLE):
            if hwnd not in skip:
                return hwnd
        time.sleep(0.3)
    return 0


def close_window(hwnd: int, wait: float = 6.0) -> None:
    """关掉**本脚本拉起**的窗口，必要时结束它的客户端进程。

    先发 ``WM_CLOSE`` 走正常关闭；窗口还在就结束窗口所属进程。这一步不是洁癖：
    残留的 ``flet.exe`` 会在下一次运行时冒充目标窗口，让验收随机失败。
    """
    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    end = time.monotonic() + wait
    while time.monotonic() < end:
        if not user32.IsWindow(hwnd):
            return
        time.sleep(0.2)
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value:
        subprocess.run(
            ["taskkill", "/F", "/PID", str(pid.value)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )


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


def _panel_rgb() -> tuple[int, int, int]:
    """菜单栏 / 状态栏的面板底色（唯一事实源是 design.py，这里只读一次）。"""
    from cs_image_viewer.view import design

    raw = design.C_PANEL.lstrip("#")
    return int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16)


def _band_ratio(img, y0: float, y1: float) -> float:
    """横带里「面板底色」占比 —— 顶栏 / 底栏是否真的画出来了。"""
    w, h = img.size
    band = img.crop((0, int(h * y0), w, int(h * y1)))
    px = _pixels(band)
    pr, pg, pb = _panel_rgb()
    hit = sum(1 for r, g, b in px
              if abs(r - pr) <= 6 and abs(g - pg) <= 6 and abs(b - pb) <= 6)
    return hit / max(1, len(px))


def _frame_is_app(img) -> bool:
    """顶栏与底栏都到位，才算「应用已经画完一帧」。

    只判顶栏不够：首帧可能只画了上半部分，底栏位置还是画布色 —— 那样截出来的图
    看着正常，``status_dark`` 却会掉到 0.6 附近，让判据在阈值边缘晃。
    """
    return _band_ratio(img, 0.005, 0.030) > 0.75 and _band_ratio(img, 0.970, 1.000) > 0.75


def capture_ready(hwnd: int, timeout: float = 20.0, delay: float = 0.4):
    """抓帧直到拿到**本应用**的画面。

    三种「看起来抓到了、其实没有」的帧都要等：

    * 纯黑位图 —— Flutter 还没提交首帧，``PrintWindow`` 却返回成功；
    * Flet 的 **「Working...」白色引导页** —— 窗口已经存在，Python 侧还没
      ``page.render``（启动约 2.4 秒，固定 ``sleep(2.5)`` 是在赌）；
    * 半张帧（图片还在解码）—— 由调用方在拿到首帧后再等一小会儿。

    判据是顶栏的菜单面板底色：应用一旦渲染完成，这条 48px 面板必然在。
    """
    end = time.monotonic() + timeout
    frame = capture(hwnd)
    while time.monotonic() < end and not _frame_is_app(frame):
        time.sleep(delay)
        frame = capture(hwnd)
    return frame


def analyse(path: Path) -> dict[str, object]:
    from PIL import Image

    img = Image.open(path).convert("RGB")
    w, h = img.size
    core = img.crop((int(w * 0.2), int(h * 0.15), int(w * 0.95), int(h * 0.92)))
    pixels = _pixels(core)
    grey = sum(1 for r, g, b in pixels
               if abs(r - g) <= 6 and abs(g - b) <= 6 and 0xAC <= r <= 0xCC)
    overflow = sum(1 for r, g, b in pixels if r >= 240 and g >= 225 and b <= 80)

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
        # 顶栏 / 底栏的面板底色占比 = 「抓到的确实是本应用、且没被裁掉」，与图片内容无关。
        # 早先用「画布色占比」判断，但适应窗口后图片几乎铺满画布，正常截图反而被判成抓错窗口。
        "menu_dark": round(_band_ratio(img, 0.005, 0.030), 4),
        "status_dark": round(_band_ratio(img, 0.970, 1.000), 4),
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

    stale = set(windows_titled(TITLE))    # 上一次运行可能留下的断线窗口
    proc = subprocess.Popen(
        cmd, cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )
    hwnd = 0
    try:
        hwnd = find_window(exclude=stale)
        if not hwnd:
            print("FAIL 没找到 ImageViewer 窗口")
            return 1
        # 置顶 + 前置：SetForegroundWindow 常被系统拒绝，单靠它保证不了可见性。
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)
        user32.ShowWindow(hwnd, 9)        # SW_RESTORE
        user32.SetForegroundWindow(hwnd)

        shot = OUT / ("multi.png" if args.folder else "empty.png")
        try:
            capture_ready(hwnd).save(shot)          # 等第一帧本应用的画面
            time.sleep(0.8)                        # 再等图片解码 / 箭头稳定
            capture_ready(hwnd, timeout=6.0).save(shot)
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
            and report["menu_dark"] > 0.60     # 菜单栏在（顶栏面板底色）
            and report["status_dark"] > 0.60   # 状态栏在（底栏面板底色，没被裁掉）
        )
        if report["menu_dark"] <= 0.60 or report["status_dark"] <= 0.60:
            print("  ! 顶栏/底栏面板底色缺失：多半抓到了盖在上面的别的窗口，重跑一次")
        if args.folder:
            # 两枚玻璃箭头各应贡献一小片亮图标像素；单图时这两项接近 0。
            ok = ok and report["left_bright"] > 0.0005 and report["right_bright"] > 0.0005
        print("结果：" + ("通过" if ok else "失败"))
        return 0 if ok else 1
    finally:
        user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE)
        if hwnd:
            close_window(hwnd)            # 窗口属于 flet 客户端进程，必须显式关掉
        try:
            proc.wait(timeout=5)          # 窗口关闭后 python 自己会退，给它 5 秒
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
