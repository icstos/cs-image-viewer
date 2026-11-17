"""本进程解码器 —— ``DisplayProvider`` 的本地实现。

它只做技术活：把文件变成第一帧可上屏的 data URI、读出图头、逐帧取值、导出。
**不持有业务状态**：当前看第几张、往哪边翻、旋转过几次，全部在 ``service`` 层。
因此它没有 index、没有「当前文件」这类字段，任何一次调用都可以独立复算。

三处值得记住的取舍：

1. **句柄生命周期**：GIF/WebP/TIFF 依赖文件句柄做多帧 seek，不能提前关；单帧
   格式则完全解码后立刻归还句柄，避免 Windows 上「删除文件失败」。
2. **编码缓存按「路径 + 时间戳 + 变换 + 帧」做键**，而不是按索引。索引会随目录
   补全而整体位移，路径不会；缓存因此不需要在补全时搬家。
3. **APNG 单独处理**：Pillow 只读首帧，需要用 ``apng`` 包解出全部帧，否则动图
   会静默变成一张静止图。
"""

from __future__ import annotations

import io
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from ...models import DisplayImage, ImageProbe, Transform
from ...models.formats import SEEK_FORMATS
from .. import imaging
from ..catalog import list_images

__all__ = ["LocalDisplay"]

#: 编码结果缓存条目上限：旋转 / 翻转 / 逐帧会组合出很多键，留 64 条足够翻页。
_ENCODED_LIMIT = 64

#: 依赖文件句柄进行多帧 seek 的格式（其余格式完全解码后可释放句柄）。
#: 直接取自模型层，不经 ``infra.imaging`` 转发 —— 格式白名单只有一处定义。
_SEEK_FORMATS = SEEK_FORMATS


@dataclass(slots=True)
class _Apng:
    """APNG 的帧列表（Pillow 只认首帧，这里存解码后的完整序列）。"""

    frames: list[Image.Image] = field(default_factory=list)
    durations: list[int] = field(default_factory=list)
    canvas: tuple[int, int] = (0, 0)


@dataclass(slots=True)
class _Loaded:
    """一次「打开」的结果：图像对象 + 帧数 + 可能的 APNG 帧序列。"""

    path: Path
    stamp: int                    # mtime_ns，用于判断文件是否被外部修改
    image: Image.Image | None = None
    frames: int = 1
    error: str = ""
    apng: _Apng | None = None


class LocalDisplay:
    """本地解码 + 显示层变换 + 编码。实现 ``service.ports.DisplayProvider``。"""

    def __init__(self, cache_size: int = 3) -> None:
        self._cache: OrderedDict[str, _Loaded] = OrderedDict()
        self._cache_size = max(1, cache_size)
        self._encoded: OrderedDict[tuple[Any, ...], DisplayImage] = OrderedDict()
        self._last_error: str = ""

    # ------------------------------------------------------------------
    # 端口：探测 / 显示 / 帧 / 目录 / 预热 / 导出 / 统计
    # ------------------------------------------------------------------

    def probe(self, path: Path) -> ImageProbe:
        """读图头（宽高、帧数、文件大小），不保留句柄。"""
        try:
            size = path.stat().st_size
        except OSError as exc:
            return ImageProbe(error=_short(exc))
        loaded = self._load(path)
        if loaded.image is None:
            return ImageProbe(size_bytes=size, error=loaded.error or "无法解码")
        width, height = loaded.image.size
        if loaded.apng is not None:
            width, height = loaded.apng.canvas
        return ImageProbe(
            ok=True, width=width, height=height,
            frames=loaded.frames, size_bytes=size,
        )

    def display(self, path: Path, transform: Transform, frame: int = 0) -> DisplayImage | None:
        """渲染一帧为 data URI；失败返回 ``None``（原因见 :attr:`last_error`）。"""
        stamp = self._stamp(path)
        key = (str(path), stamp, transform, frame)
        cached = self._encoded.get(key)
        if cached is not None:
            self._encoded.move_to_end(key)
            return cached
        image = self._frame_image(path, frame)
        if image is None:
            return None
        if transform.identity:
            rendered = imaging.limit_size(image)
        else:
            rendered = imaging.apply_transform(
                imaging.limit_size(image), transform.rotation,
                transform.flip_h, transform.flip_v,
            )
        result = DisplayImage(
            src=imaging.to_data_uri(rendered),
            width=rendered.width,
            height=rendered.height,
        )
        self._encoded[key] = result
        while len(self._encoded) > _ENCODED_LIMIT:
            self._encoded.popitem(last=False)
        return result

    def frame_duration_ms(self, path: Path, frame: int) -> int:
        """该帧的显示时长（毫秒）；未知或非动图时给 100。"""
        loaded = self._load(path)
        if loaded.apng is not None and loaded.apng.durations:
            index = min(max(frame, 0), len(loaded.apng.durations) - 1)
            return max(loaded.apng.durations[index], 20)
        image = loaded.image
        if image is None:
            return 100
        try:
            image.seek(frame)
            duration = image.info.get("duration")
            return max(int(duration), 20) if duration else 100
        except Exception:
            return 100

    def list_folder(self, folder: Path) -> list[Path]:
        """目录内的图片（自然排序）；不可读时返回空列表。"""
        return list_images(folder)

    def preload(self, paths: tuple[Path, ...] | list[Path], transform: Transform) -> None:
        """打开但**不编码**：编码在真正显示时做，省掉无用的 CPU。

        预热的价值在「句柄已开、像素已解压」，切过去只差编码这一步；
        提前编码反而会让预加载把磁盘写满。
        """
        for path in paths:
            self._load(path)

    def export(self, path: Path, out: Path, transform: Transform, frame: int) -> None:
        """导出当前帧（全分辨率 + 显示层变换）为 PNG。"""
        image = self._frame_image(path, frame)
        if image is None:
            raise RuntimeError(self._last_error or "无法解码当前帧")
        imaging.save_png(
            imaging.apply_transform(image, transform.rotation, transform.flip_h, transform.flip_v),
            out,
        )

    def stats(self) -> dict[str, Any] | None:
        """本地解码没有服务可报；返回 ``None`` 让上层显示「未启用共享缓存」。"""
        return None

    def error_text(self) -> str:
        """最近一次失败的原因（用户可读）；没有失败时为空串。"""
        return self._last_error

    def close(self) -> None:
        """释放全部已打开的文件句柄（进程退出 / 换源时调用）。"""
        for loaded in self._cache.values():
            _close(loaded.image)
            if loaded.apng is not None:
                for frame in loaded.apng.frames:
                    _close(frame)
        self._cache.clear()
        self._encoded.clear()

    # ------------------------------------------------------------------
    # 内部：打开与取帧
    # ------------------------------------------------------------------

    def _stamp(self, path: Path) -> int:
        """mtime_ns；取不到时返回 0（等价于「不缓存」）。"""
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return 0

    def _load(self, path: Path) -> _Loaded:
        """打开（或复用）一张图，维护 LRU 与「坏图只试一次」。"""
        key = str(path)
        loaded = self._cache.get(key)
        if loaded is not None:
            self._cache.move_to_end(key)
            return loaded
        item = self._open(path)
        self._cache[key] = item
        while len(self._cache) > self._cache_size:
            _, evicted = self._cache.popitem(last=False)
            _close(evicted.image)
            if evicted.apng is not None:
                for frame in evicted.apng.frames:
                    _close(frame)
        return item

    def _open(self, path: Path) -> _Loaded:
        """真正打开：RAW / SVG 走专用解码器，PNG 额外检测 APNG。"""
        item = _Loaded(path=path, stamp=self._stamp(path))
        try:
            image = imaging.load_image(path)
        except Exception as exc:
            item.error = _short(exc)
            self._last_error = item.error
            return item
        try:
            item.frames = imaging.frame_count(image)
            if path.suffix.lower() == ".png":
                item.apng = _read_apng(path)
                if item.apng is not None:
                    item.frames = max(item.frames, len(item.apng.frames))
            if item.frames <= 1 and path.suffix.lower() not in _SEEK_FORMATS:
                # 单帧：完全解码后归还句柄，避免占用文件（Windows 上删不掉）
                image.load()
                handle = getattr(image, "fp", None)
                if handle is not None:
                    handle.close()
        except Exception as exc:
            item.error = _short(exc)
            self._last_error = item.error
            _close(image)
            item.image = None
            return item
        item.image = image
        return item

    def _frame_image(self, path: Path, frame: int) -> Image.Image | None:
        """取第 ``frame`` 帧的规范化图像（EXIF 摆正 + 色彩归一化）。"""
        loaded = self._load(path)
        if loaded.apng is not None:
            frames = loaded.apng.frames
            if frame >= len(frames):
                return None
            return imaging.normalize_mode(frames[frame])
        image = loaded.image
        if image is None:
            self._last_error = loaded.error or "无法解码"
            return None
        try:
            image.seek(frame)
            current = image.copy()          # 复制当前帧，避免后续 seek 干扰
        except Exception as exc:
            self._last_error = _short(exc)
            return None
        current = ImageOps.exif_transpose(current)   # 按 EXIF 方向摆正照片
        return imaging.normalize_mode(current)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _read_apng(path: Path) -> _Apng | None:
    """检测并解码 APNG 帧列表（文件头含 ``acTL`` 块才处理）。"""
    try:
        with open(path, "rb") as handle:
            if b"acTL" not in handle.read(4096):
                return None
        import apng

        animation = apng.APNG.open(path)
        result = _Apng()
        for png, control in animation.frames:
            with Image.open(io.BytesIO(png.to_bytes())) as raw:
                raw.load()
                result.frames.append(raw.convert("RGBA"))
            delay = (control.delay / control.delay_den * 1000) if control.delay_den else 100
            result.durations.append(max(round(delay), 20))
            width = control.width or result.frames[-1].width
            height = control.height or result.frames[-1].height
            result.canvas = (
                max(result.canvas[0], width),
                max(result.canvas[1], height),
            )
        if len(result.frames) <= 1:
            return None
        if result.canvas == (0, 0):
            result.canvas = result.frames[0].size
        return result
    except Exception:
        return None                        # 非 APNG / 解码失败 → 按单帧处理


def _close(image: Image.Image | None) -> None:
    """尽力关闭；句柄已关或对象已被回收都不该冒泡。"""
    if image is None:
        return
    try:
        image.close()
    except Exception:
        pass


def _short(exc: Exception) -> str:
    text = str(exc)
    return text if len(text) <= 120 else text[:117] + "..."
