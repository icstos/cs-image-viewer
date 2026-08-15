"""图片管理器：文件扫描、自然排序、解码缓存、动图逐帧与显示层变换。

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
    ".svg",                                        # 矢量图（resvg 渲染）
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

# 超大图支持：解除 Pillow 默认的解压炸弹限制（约 1.78 亿像素），
# 超清图/长截图（如 1080x30000、100MP 照片）可正常打开。
Image.MAX_IMAGE_PIXELS = None

# 编码预算：超限图片按比例压缩后再编码，避免卡顿与内存爆炸。
MAX_ENCODE_PIXELS = 40_000_000       # 编码像素上限（40MP）
MAX_ENCODE_DIM = 16_000              # 编码最长边上限（16000px）
_FLATTEN_ALPHA_PIXELS = 20_000_000   # 超大透明图压平为 JPEG（PNG 编码太慢）

# 依赖文件句柄进行多帧 seek 的格式（其余格式完全解码后可释放句柄）
_SEEK_FORMATS = {".gif", ".webp", ".tif", ".tiff"}

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
    """维护图片列表、当前索引、解码缓存、动图帧状态与显示层变换。"""

    def __init__(self, cache_size: int = 3):
        self.files: list[Path] = []                        # 当前图片列表（自然排序）
        self.index: int = 0                                # 当前文件索引
        self.current_frame: int = 0                        # 动图当前帧
        self.rotation: int = 0                             # 顺时针旋转次数（×90°）
        self.flip_h: bool = False                          # 水平翻转
        self.flip_v: bool = False                          # 垂直翻转
        # 已打开图片对象（保留多帧 seek 能力；文件句柄由其生命周期管理）
        self._opened: OrderedDict[int, Image.Image] = OrderedDict()
        self._cache_size = cache_size
        # 编码缓存：key=(索引, 帧, 旋转, 水平翻转, 垂直翻转) -> (data_uri, 宽, 高)
        self._encoded: dict[tuple, tuple[str, int, int]] = {}
        self._failed: set[int] = set()                     # 已确认无法解码的索引
        self.last_error: str = ""                          # 最近一次解码失败原因（供 UI 提示）
        # APNG 支持（Pillow 只读首帧，由 apng 包解码帧列表）
        self._apng_frames: list[Image.Image] | None = None
        self._apng_durations: list[int] = []
        self._apng_canvas: tuple[int, int] = (0, 0)

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
            if self._decodable(self.index):
                return True
        return False

    def ensure_valid(self) -> bool:
        """确保当前索引指向可解码图片（打开时自动跳过首张坏图）。"""
        if not self.files:
            return False
        if not self._decodable(self.index):
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

    # ---------- 动图逐帧 ----------

    def frame_count(self) -> int:
        """当前图片的帧数；非动图返回 1，无图返回 0。"""
        if not self.files:
            return 0
        if self._apng_frames is not None:
            return len(self._apng_frames)
        im = self._open(self.index)
        if im is None:
            return 0
        try:
            return int(getattr(im, "n_frames", 1) or 1)
        except Exception:
            return 1

    def set_frame(self, frame: int) -> bool:
        """跳转到指定帧；非动图复位为 0。返回是否成功。"""
        n = self.frame_count()
        if n <= 1:
            self.current_frame = 0
            return False
        self.current_frame = max(0, min(frame, n - 1))
        return True

    def next_frame(self, delta: int = 1) -> bool:
        """动图内循环前进/后退一帧。"""
        n = self.frame_count()
        if n <= 1:
            return False
        self.current_frame = (self.current_frame + delta) % n
        return True

    def frame_duration(self) -> int:
        """当前帧显示时长（毫秒）；未知时取默认 100ms。"""
        if self._apng_frames is not None and self._apng_durations:
            i = min(self.current_frame, len(self._apng_durations) - 1)
            return max(self._apng_durations[i], 20)
        im = self._opened.get(self.index)
        if im is None:
            return 100
        try:
            im.seek(self.current_frame)
            dur = im.info.get("duration")
            return max(int(dur), 20) if dur else 100
        except Exception:
            return 100

    def save_current_frame(self, out_path: str | Path) -> None:
        """导出当前帧（含旋转/翻转）为 PNG。"""
        im = self._frame_image(self.index, self.current_frame)
        if im is None:
            raise RuntimeError("无法解码当前帧")
        self._apply_transform(im).save(out_path, format="PNG")

    # ---------- 解码 / 编码 ----------

    def get_display(self) -> tuple[str | None, int, int]:
        """返回当前图片/帧的 (data_uri, 渲染宽, 渲染高)；解码失败返回 (None, 0, 0)。"""
        if not self.files:
            return None, 0, 0
        key = (self.index, self.current_frame, self.rotation, self.flip_h, self.flip_v)
        if key in self._encoded:
            return self._encoded[key]
        im = self._frame_image(self.index, self.current_frame)
        if im is None:
            return None, 0, 0
        rendered = self._apply_transform(self._limit_size(im))
        result = (self._to_data_uri(rendered), *rendered.size)
        self._encoded[key] = result
        if len(self._encoded) > 64:        # 防止组合无限膨胀
            self._encoded.clear()
        return result

    def preload(self) -> None:
        """预加载前后各一张图片，提升切换流畅度。"""
        if len(self.files) <= 1:
            return
        for delta in (-1, 1):
            self._open((self.index + delta) % len(self.files))

    def info(self) -> dict:
        """当前文件元信息：名称、分辨率、文件大小、页码、帧数。"""
        if not self.files:
            return {"name": "", "w": 0, "h": 0, "size": 0, "pos": 0, "total": 0, "frames": 0}
        im = self._open(self.index)
        if self._apng_frames is not None:
            w, h = self._apng_canvas
        else:
            w, h = im.size if im else (0, 0)
        p = self.files[self.index]
        return {
            "name": p.name,
            "w": w,
            "h": h,
            "size": p.stat().st_size if p.exists() else 0,
            "pos": self.index + 1,
            "total": len(self.files),
            "frames": self.frame_count(),
        }

    def delete_current(self) -> str:
        """删除当前文件（移入回收站），返回被删除的文件名。"""
        import send2trash

        target = self.files[self.index]
        self._close_all()          # 释放文件句柄（Windows 下删除要求文件未被占用）
        send2trash.send2trash(str(target))
        del self.files[self.index]
        self._reset()
        if self.files:
            self.index = min(self.index, len(self.files) - 1)
        return target.name

    # ---------- 内部实现 ----------

    def _reset(self) -> None:
        """文件列表变化时清空全部缓存并复位帧状态。"""
        self._close_all()
        self._encoded.clear()
        self._failed.clear()
        self.reset_transform()
        self.current_frame = 0
        self._apng_frames = None
        self._apng_durations = []

    def _close_all(self) -> None:
        """关闭全部已打开图片对象，释放文件句柄。"""
        for im in self._opened.values():
            try:
                im.close()
            except Exception:
                pass
        self._opened.clear()

    def _open(self, idx: int) -> Image.Image | None:
        """打开指定索引的图片对象（保留多帧 seek 能力；坏图只尝试一次）。"""
        if idx in self._opened:
            self._opened.move_to_end(idx)
            return self._opened[idx]
        if idx in self._failed:
            return None
        try:
            im = self._load_image(self.files[idx])
            if self.files[idx].suffix.lower() == ".png":
                self._extract_apng(idx)          # APNG：Pillow 只读首帧，需单独解码
            # 单帧且非 seek 依赖格式：完全解码后释放文件句柄，避免占用文件
            try:
                n = int(getattr(im, "n_frames", 1) or 1)
            except Exception:
                n = 1
            if n <= 1 and self.files[idx].suffix.lower() not in _SEEK_FORMATS:
                im.load()
                fp = getattr(im, "fp", None)
                if fp is not None:
                    fp.close()
            self._opened[idx] = im
            self._opened.move_to_end(idx)
            while len(self._opened) > self._cache_size:   # 淘汰最久未用
                old = self._opened.popitem(last=False)
                try:
                    old[1].close()
                except Exception:
                    pass
            return im
        except Exception as exc:
            self._failed.add(idx)
            self.last_error = str(exc)
            return None

    def _decodable(self, idx: int) -> bool:
        """该索引能否解码出至少一帧。"""
        if idx in self._failed:
            return False
        if idx in self._opened:
            return True
        return self._open(idx) is not None

    def _frame_image(self, idx: int, frame: int) -> Image.Image | None:
        """取第 frame 帧的规范化图像（EXIF 摆正 + 色彩归一化）。"""
        if self._apng_frames is not None:
            if frame >= len(self._apng_frames):
                return None
            return self._normalize_mode(self._apng_frames[frame])
        im = self._open(idx)
        if im is None:
            return None
        try:
            im.seek(frame)
            cur = im.copy()                      # 复制当前帧，避免后续 seek 干扰
        except Exception:
            return None
        cur = ImageOps.exif_transpose(cur)       # 按 EXIF 方向摆正照片
        return self._normalize_mode(cur)

    def _extract_apng(self, idx: int) -> None:
        """检测并解码 APNG 帧列表（文件含 acTL 块才处理）。"""
        try:
            with open(self.files[idx], "rb") as fh:
                if b"acTL" not in fh.read(4096):
                    return
            import apng

            anim = apng.APNG.open(self.files[idx])
            frames: list[Image.Image] = []
            durations: list[int] = []
            cw, ch = 0, 0
            for png, fc in anim.frames:
                with Image.open(io.BytesIO(png.to_bytes())) as f:
                    f.load()
                    frames.append(f.convert("RGBA"))
                d = (fc.delay / fc.delay_den * 1000) if fc.delay_den else 100
                durations.append(max(int(round(d)), 20))
                cw = max(cw, fc.width or frames[-1].width)
                ch = max(ch, fc.height or frames[-1].height)
            if len(frames) > 1:
                self._apng_frames = frames
                self._apng_durations = durations
                self._apng_canvas = (cw or frames[0].width, ch or frames[0].height)
        except Exception:
            self._apng_frames = None       # 非 APNG / 解码失败 → 按单帧处理

    @staticmethod
    def _load_image(path: Path) -> Image.Image:
        """按扩展名路由解码器：RAW 走 rawpy、SVG 走 resvg，其余走 Pillow。

        注意：不关闭文件句柄（多帧 seek 需要），由其缓存生命周期管理。
        """
        ext = path.suffix.lower()
        if ext in _RAW_EXTS:
            return _decode_raw(path)
        if ext == ".svg":
            return _decode_svg(path)
        if ext in (".heic", ".heif") and not _HEIF_AVAILABLE:
            raise RuntimeError("HEIC/HEIF 需要安装 pillow-heif：pip install pillow-heif")
        return Image.open(path)

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
    def _limit_size(im: Image.Image) -> Image.Image:
        """超大图压缩到编码预算内（保持宽高比），避免编码/传输卡顿。

        仅作用于显示层；导出当前帧仍使用全分辨率。
        """
        w, h = im.size
        scale = 1.0
        if w * h > MAX_ENCODE_PIXELS:
            scale = min(scale, (MAX_ENCODE_PIXELS / (w * h)) ** 0.5)
        if max(w, h) > MAX_ENCODE_DIM:
            scale = min(scale, MAX_ENCODE_DIM / max(w, h))
        if scale < 1.0:
            im = im.resize((max(int(w * scale), 1), max(int(h * scale), 1)), Image.LANCZOS)
        return im

    @staticmethod
    def _to_data_uri(im: Image.Image) -> str:
        """编码为 data URI：带透明通道用 PNG，否则用高质量 JPEG。

        超大透明图压平为 JPEG（PNG 编码太慢且体积巨大）。
        """
        buf = io.BytesIO()
        if im.mode == "RGBA" and im.width * im.height > _FLATTEN_ALPHA_PIXELS:
            bg = Image.new("RGB", im.size, (20, 20, 20))   # 与看图背景色一致
            bg.paste(im, mask=im.getchannel("A"))
            im = bg
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
