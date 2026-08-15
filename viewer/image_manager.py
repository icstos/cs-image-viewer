"""图片管理器：文件扫描、自然排序、解码缓存与显示层变换（旋转/翻转）。

纯逻辑模块，不依赖 Flet，便于复用与单元测试。
"""
from __future__ import annotations

import base64
import io
import re
from collections import OrderedDict
from pathlib import Path

from PIL import Image, ImageOps

# 支持的图片扩展名（小写）
IMAGE_EXTS = {
    # 主流格式
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".bmp", ".gif", ".webp", ".tif", ".tiff",
    # 特殊格式
    ".psd",                                        # Photoshop（Pillow 原生）
    ".svg",                                        # 矢量图（svglib + reportlab 渲染）
    ".heic", ".heif",                             # 高效图片格式（pillow-heif）
    # 相机 RAW
    ".cr2", ".cr3", ".nef", ".nrw", ".arw", ".srf", ".dng", ".raf", ".orf", ".rw2",
    ".pef", ".srw", ".erf", ".3fr", ".mef", ".mrw", ".iiq", ".kdc", ".raw", ".x3f",
}

# 相机 RAW 扩展名（走 rawpy 解码）
_RAW_EXTS = {
    ".cr2", ".cr3", ".nef", ".nrw", ".arw", ".srf", ".dng", ".raf", ".orf", ".rw2",
    ".pef", ".srw", ".erf", ".3fr", ".mef", ".mrw", ".iiq", ".kdc", ".raw", ".x3f",
}

# 注册 HEIC/HEIF 解码（可选依赖，缺失时给出友好提示）
_HEIF_AVAILABLE = False
try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    _HEIF_AVAILABLE = True
except ImportError:
    pass

_DIGIT_RE = re.compile(r"(\d+)")


def natural_key(text: str) -> tuple:
    """自然排序键：让「img2.png」排在「img10.png」之前。"""
    return tuple(
        int(part) if part.isdigit() else part.lower()
        for part in _DIGIT_RE.split(text)
    )


def format_size(num_bytes: int) -> str:
    """把字节数格式化为人类可读的 B/KB/MB/GB。"""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{num_bytes} B"


class ImageManager:
    """维护图片列表、当前索引、解码缓存与显示层变换状态。"""

    def __init__(self, cache_size: int = 3):
        self.files: list[Path] = []                        # 当前图片列表（自然排序）
        self.index: int = 0                                # 当前文件索引
        self.rotation: int = 0                             # 顺时针旋转次数（×90°）
        self.flip_h: bool = False                          # 水平翻转
        self.flip_v: bool = False                          # 垂直翻转
        self._decoded: OrderedDict[int, Image.Image] = OrderedDict()  # 解码缓存（LRU）
        self._cache_size = cache_size
        # 编码缓存：key=(索引, 旋转, 水平翻转, 垂直翻转) -> (data_uri, 宽, 高)
        self._encoded: dict[tuple, tuple[str, int, int]] = {}
        self._failed: set[int] = set()                     # 已确认无法解码的索引
        self.last_error: str = ""                          # 最近一次解码失败原因（供 UI 提示）

    # ---------- 文件列表 ----------

    def open_path(self, path: str | Path) -> bool:
        """打开单张图片（作为独立列表加载）。"""
        p = Path(path)
        if not p.is_file() or p.suffix.lower() not in IMAGE_EXTS:
            return False
        self.files = [p]
        self.index = 0
        self._reset()
        return True

    def scan_folder(self, folder: str | Path) -> int:
        """扫描目录内所有图片，按文件名自然排序；返回图片总数。"""
        folder = Path(folder)
        self.files = sorted(
            (p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS),
            key=lambda p: natural_key(p.stem),
        )
        self.index = 0
        self._reset()
        return len(self.files)

    def navigate(self, delta: int) -> bool:
        """切换图片（首尾循环），自动跳过损坏文件；成功返回 True。"""
        if len(self.files) <= 1:
            return False
        for _ in range(len(self.files)):
            self.index = (self.index + delta) % len(self.files)
            self.reset_transform()
            if self._decode(self.index) is not None:
                return True
        return False

    def ensure_valid(self) -> bool:
        """确保当前索引指向可解码图片（打开时自动跳过首张坏图）。"""
        if not self.files:
            return False
        if self._decode(self.index) is None:
            return self.navigate(1)
        return True

    # ---------- 显示层变换（不修改原文件） ----------

    def reset_transform(self) -> None:
        """切换图片时重置旋转/翻转。"""
        self.rotation = 0
        self.flip_h = False
        self.flip_v = False

    def rotate_cw(self) -> None:
        self.rotation = (self.rotation + 1) % 4

    def rotate_ccw(self) -> None:
        self.rotation = (self.rotation - 1) % 4

    def flip_horizontal(self) -> None:
        self.flip_h = not self.flip_h

    def flip_vertical(self) -> None:
        self.flip_v = not self.flip_v

    # ---------- 解码 / 编码 ----------

    def get_display(self) -> tuple[str | None, int, int]:
        """返回当前图片的 (data_uri, 渲染宽, 渲染高)；解码失败返回 (None, 0, 0)。"""
        if not self.files:
            return None, 0, 0
        key = (self.index, self.rotation, self.flip_h, self.flip_v)
        if key in self._encoded:
            return self._encoded[key]
        im = self._decode(self.index)
        if im is None:
            return None, 0, 0
        rendered = self._apply_transform(im)
        result = (self._to_data_uri(rendered), *rendered.size)
        self._encoded[key] = result
        if len(self._encoded) > 32:        # 防止变换组合无限膨胀
            self._encoded.clear()
        return result

    def preload(self) -> None:
        """预加载前后各一张图片到解码缓存，提升切换流畅度。"""
        if len(self.files) <= 1:
            return
        for delta in (-1, 1):
            self._decode((self.index + delta) % len(self.files))

    def info(self) -> dict:
        """当前文件元信息：名称、原始分辨率、文件大小、页码。"""
        if not self.files:
            return {"name": "", "w": 0, "h": 0, "size": 0, "pos": 0, "total": 0}
        im = self._decode(self.index)
        w, h = im.size if im else (0, 0)
        p = self.files[self.index]
        return {
            "name": p.name,
            "w": w,
            "h": h,
            "size": p.stat().st_size if p.exists() else 0,
            "pos": self.index + 1,
            "total": len(self.files),
        }

    def delete_current(self) -> str:
        """删除当前文件（移入回收站），返回被删除的文件名。"""
        import send2trash

        target = self.files[self.index]
        send2trash.send2trash(str(target))
        del self.files[self.index]
        self._reset()
        if self.files:
            self.index = min(self.index, len(self.files) - 1)
        return target.name

    # ---------- 内部实现 ----------

    def _reset(self) -> None:
        """文件列表变化时清空全部缓存。"""
        self._decoded.clear()
        self._encoded.clear()
        self._failed.clear()
        self.reset_transform()

    def _decode(self, idx: int) -> Image.Image | None:
        """解码指定索引的图片（LRU 缓存 + 失败记录，坏图只尝试一次）。"""
        if idx in self._decoded:
            self._decoded.move_to_end(idx)
            return self._decoded[idx]
        if idx in self._failed:
            return None
        try:
            im = self._load_image(self.files[idx])
            im = ImageOps.exif_transpose(im)      # 按 EXIF 方向摆正照片
            im = self._normalize_mode(im)
        except Exception as exc:
            self._failed.add(idx)
            self.last_error = str(exc)
            return None
        self._decoded[idx] = im
        self._decoded.move_to_end(idx)
        while len(self._decoded) > self._cache_size:   # 淘汰最久未用的解码图
            self._decoded.popitem(last=False)
        return im

    @staticmethod
    def _load_image(path: Path) -> Image.Image:
        """按扩展名路由解码器：RAW 走 rawpy、SVG 走 svglib，其余走 Pillow。"""
        ext = path.suffix.lower()
        if ext in _RAW_EXTS:
            return _decode_raw(path)
        if ext == ".svg":
            return _decode_svg(path)
        if ext in (".heic", ".heif") and not _HEIF_AVAILABLE:
            raise RuntimeError("HEIC/HEIF 需要安装 pillow-heif：pip install pillow-heif")
        with Image.open(path) as raw:
            raw.load()
        return raw

    @staticmethod
    def _normalize_mode(im: Image.Image) -> Image.Image:
        """统一色彩模式：透明通道保留为 RGBA，其余转为 RGB。"""
        if im.mode == "P":
            return im.convert("RGBA" if "transparency" in im.info else "RGB")
        if im.mode == "LA":
            return im.convert("RGBA")
        if im.mode not in ("RGB", "RGBA"):
            return im.convert("RGB")
        return im

    def _apply_transform(self, im: Image.Image) -> Image.Image:
        """对解码图应用旋转/翻转（仅显示层）。PIL 中正角度为逆时针。"""
        if self.rotation:
            im = im.rotate(-90 * self.rotation, expand=True)
        if self.flip_h:
            im = ImageOps.mirror(im)
        if self.flip_v:
            im = ImageOps.flip(im)
        return im

    @staticmethod
    def _to_data_uri(im: Image.Image) -> str:
        """编码为 data URI：带透明通道用 PNG，否则用高质量 JPEG。"""
        buf = io.BytesIO()
        if im.mode == "RGBA":
            im.save(buf, format="PNG")
            mime = "image/png"
        else:
            im.save(buf, format="JPEG", quality=88)
            mime = "image/jpeg"
        return f"data:{mime};base64,{base64.b64encode(buf.getvalue()).decode('ascii')}"


def _decode_raw(path: Path) -> Image.Image:
    """相机 RAW 解码：rawpy 出图（相机白平衡、自动亮度）。"""
    try:
        import rawpy
    except ImportError:
        raise RuntimeError("相机 RAW 需要安装 rawpy：pip install rawpy") from None
    with rawpy.imread(str(path)) as raw:
        rgb = raw.postprocess(use_camera_wb=True, no_auto_bright=False)
    return Image.fromarray(rgb)


def _decode_svg(path: Path) -> Image.Image:
    """SVG 矢量图渲染为位图（resvg，内置 Rust 渲染引擎，无系统依赖）。"""
    try:
        import resvg
    except ImportError:
        raise RuntimeError("SVG 需要安装 resvg：pip install resvg") from None
    opts = resvg.usvg.Options.default()
    opts.resources_dir = str(path.parent)      # 支持相对路径引用外部资源
    tree = resvg.usvg.Tree.from_str(path.read_text(encoding="utf-8"), opts)
    png = resvg.render(tree, transform=(1.0, 0.0, 0.0, 1.0, 0.0, 0.0))
    buf = io.BytesIO(png)
    with Image.open(buf) as im:
        im.load()
    return im
