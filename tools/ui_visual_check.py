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
OUT = ROOT / "output" / "ui_visual"
TITLE = "ImageViewer"

user32 = ctypes.windll.user32
user32.FindWindowW.restype = wt.HWND
user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
try:
    user32.SetProcessDPIAware()          # 让窗口矩形与屏幕像素同一坐标系
except AttributeError:
    pass


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
        user32.ShowWindow(hwnd, 9)        # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        time.sleep(2.5)                   # 等首帧、图片解码与预加载稳定

        from PIL import ImageGrab

        shot = OUT / ("multi.png" if args.folder else "empty.png")
        ImageGrab.grab(bbox=rect_of(hwnd), all_screens=True).save(shot)
        report = analyse(shot)

        print(f"截图：{shot}")
        for key, value in report.items():
            print(f"  {key}: {value}")

        ok = (
            report["grey_ratio"] < 0.55        # 没有被 ErrorWidget 灰块占满
            and report["overflow_px"] == 0     # 没有 Flutter 溢出斜纹
            and report["unique_colors"] > 20   # 不是纯色/全黑空帧
        )
        if args.folder:
            # 两枚玻璃箭头各应贡献一小片亮图标像素；单图时这两项接近 0。
            ok = ok and report["left_bright"] > 0.0005 and report["right_bright"] > 0.0005
        print("结果：" + ("通过" if ok else "失败"))
        return 0 if ok else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
