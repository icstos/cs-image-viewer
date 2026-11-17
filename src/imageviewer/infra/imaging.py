"""图像解码 / 变换 / 编码的**纯函数层**（无状态、无框架依赖）。

一次「打开图片」要经过 解码 → EXIF 摆正 → 色彩归一化 → 显示层变换 → 限尺寸 →
编码 六步。这条管线有**两个执行者**：

1. :class:`imageviewer.infra.display.local.LocalDisplay` —— 窗口进程内的本地解码
   （动图逐帧、共享服务不可用时的降级、导出原图）；
2. :class:`imageviewer.infra.shared.service.ImageService` —— 常驻服务的共享缓存
   （把编码结果写进磁盘缓存给所有窗口复用）。

两条路径必须逐字节一致，否则「走缓存」和「走本地」会看到不同的图。因此这里只放
无状态函数：谁调用都一样，也便于单独测试。

约定：本模块的函数**不持有文件句柄**（打开即关闭），句柄的生命周期由调用方自己
管理 —— 服务侧完全不保留句柄，因为它的产物是磁盘上的 blob。
"""

from __future__ import annotations

import base64
import io
from pathlib import Path

from PIL import Image, ImageOps

from ..models.formats import RAW_EXTS

# 编码预算：超限图片按比例压缩后再编码，避免卡顿与内存爆炸。
MAX_ENCODE_PIXELS = 40_000_000       # 编码像素上限（40MP）
MAX_ENCODE_DIM = 16_000              # 编码最长边上限（16000px）
_FLATTEN_ALPHA_PIXELS = 20_000_000   # 超大透明图压平为 JPEG（PNG 编码太慢）

# 注册 HEIC/HEIF 解码（可选依赖，缺失时给出友好提示）
HEIF_AVAILABLE = False
try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_AVAILABLE = True
except ImportError:
    pass

# 超大图支持：解除 Pillow 默认的解压炸弹限制（约 1.78 亿像素），
# 超清图/长截图（如 1080x30000、100MP 照片）可正常打开。
Image.MAX_IMAGE_PIXELS = None


# ---------------------------------------------------------------------------
# 解码
# ---------------------------------------------------------------------------


def load_image(path: str | Path) -> Image.Image:
    """按扩展名路由解码器：RAW 走 rawpy、SVG 走 resvg，其余走 Pillow。

    不在本函数内关闭句柄：调用方可能还要 seek 多帧，
    由调用方决定何时 `close()`。
    """
    p = Path(path)
    ext = p.suffix.lower()
    if ext in RAW_EXTS:
        return _decode_raw(p)
    if ext == ".svg":
        return _decode_svg(p)
    if ext in (".heic", ".heif") and not HEIF_AVAILABLE:
        raise RuntimeError("HEIC/HEIF 需要安装 pillow-heif：pip install pillow-heif")
    return Image.open(p)


def frame_count(im: Image.Image) -> int:
    """帧数（异常一律按单帧处理，坏图不该让上层崩）。"""
    try:
        return int(getattr(im, "n_frames", 1) or 1)
    except Exception:
        return 1


def probe(path: str | Path) -> tuple[int, int, int]:
    """读头信息：(宽, 高, 帧数)。RAW/SVG 会触发完整解码（由调用方缓存）。"""
    im = load_image(path)
    try:
        return im.size[0], im.size[1], frame_count(im)
    finally:
        _close(im)


# ---------------------------------------------------------------------------
# 变换
# ---------------------------------------------------------------------------


def normalize_mode(im: Image.Image) -> Image.Image:
    """统一色彩模式：透明通道保留为 RGBA，其余转为 RGB。"""
    if im.mode == "P":
        return im.convert("RGBA" if "transparency" in im.info else "RGB")
    if im.mode == "LA":
        return im.convert("RGBA")
    if im.mode not in ("RGB", "RGBA"):
        return im.convert("RGB")
    return im


def apply_transform(
    im: Image.Image,
    rotation: int = 0,
    flip_h: bool = False,
    flip_v: bool = False,
) -> Image.Image:
    """显示层旋转/翻转（不修改原文件）。PIL 中正角度为逆时针。

    全为 0 时原样返回**同一个对象**（不复制）——大图上来回切图不会白拷一份内存。
    调用方因此必须把入参视为只读。
    """
    if rotation:
        im = im.rotate(-90 * rotation, expand=True)
    if flip_h:
        im = ImageOps.mirror(im)
    if flip_v:
        im = ImageOps.flip(im)
    return im


def limit_size(
    im: Image.Image,
    max_pixels: int = MAX_ENCODE_PIXELS,
    max_dim: int = MAX_ENCODE_DIM,
) -> Image.Image:
    """超大图压缩到编码预算内（保持宽高比），避免编码/传输卡顿。

    仅作用于显示层；导出当前帧仍使用全分辨率。
    """
    w, h = im.size
    scale = 1.0
    if w * h > max_pixels:
        scale = min(scale, (max_pixels / (w * h)) ** 0.5)
    if max(w, h) > max_dim:
        scale = min(scale, max_dim / max(w, h))
    if scale < 1.0:
        im = im.resize((max(int(w * scale), 1), max(int(h * scale), 1)), Image.LANCZOS)
    return im


# ---------------------------------------------------------------------------
# 编码
# ---------------------------------------------------------------------------


def encode_display(im: Image.Image) -> tuple[bytes, str]:
    """编码为可直接显示的字节流：带透明通道用 PNG，否则用高质量 JPEG。

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
    return buf.getvalue(), mime


def to_data_uri(im: Image.Image) -> str:
    """编码为 data URI（Flet 的 `Image.src` 直接可用）。"""
    data, mime = encode_display(im)
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def bytes_to_data_uri(data: bytes, mime: str) -> str:
    """已编码字节流 -> data URI（共享缓存读回 blob 时用）。"""
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


# ---------------------------------------------------------------------------
# 一次性完整管线（服务侧与导出共用）
# ---------------------------------------------------------------------------


def render(
    path: str | Path,
    rotation: int = 0,
    flip_h: bool = False,
    flip_v: bool = False,
    frame: int = 0,
    max_pixels: int = MAX_ENCODE_PIXELS,
    max_dim: int = MAX_ENCODE_DIM,
) -> tuple[bytes, str, int, int]:
    """从头解码一张图并渲染成显示用字节流。

    返回 `(字节, mime, 显示宽, 显示高)`；一次调用内完成句柄的打开与释放，
    调用方不需要（也不应该）缓存 PIL 对象。
    """
    im = load_image(path)
    try:
        if frame_count(im) > 1:
            im.seek(frame)
        im.load()
        cur = ImageOps.exif_transpose(im)        # 按 EXIF 方向摆正照片
        cur = normalize_mode(cur)
        cur = apply_transform(limit_size(cur, max_pixels, max_dim), rotation, flip_h, flip_v)
        data, mime = encode_display(cur)
        return data, mime, cur.width, cur.height
    finally:
        _close(im)


def save_png(im: Image.Image, out_path: str | Path) -> None:
    """把已渲染好的图像写成 PNG（导出当前帧）。"""
    im.save(out_path, format="PNG")


def _close(im: Image.Image) -> None:
    try:
        im.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 特殊格式解码器
# ---------------------------------------------------------------------------


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
