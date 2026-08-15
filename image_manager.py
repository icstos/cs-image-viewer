"""图片管理器：目录扫描、自然排序、索引导航、预加载缓存、元信息读取、安全删除。"""

from __future__ import annotations

import os
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PIL import Image, UnidentifiedImageError

SUPPORTED_EXTS: frozenset[str] = frozenset({
    ".jpg", ".jpeg", ".png", ".bmp", ".gif",
    ".webp", ".tif", ".tiff", ".ico", ".jfif",
})

# Flutter 原生可解码的格式，可直接传原始 bytes
_NATIVE_BYTES_EXTS: frozenset[str] = frozenset({
    ".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp",
})


@dataclass(slots=True)
class ImageInfo:
    """单张图片的元信息（纯数据，不含像素）。"""

    path: str
    name: str
    size: int  # 文件字节数
    width: int = 0
    height: int = 0
    valid: bool = True


def _natural_sort_key(s: str) -> list[tuple[int, int | str]]:
    """自然排序键：数字段按数值比较，文本段按字典序比较。"""
    parts = re.split(r"(\d+)", s.lower())
    key: list[tuple[int, int | str]] = []
    for part in parts:
        if not part:
            continue
        if part.isdigit():
            key.append((0, int(part)))
        else:
            key.append((1, part))
    return key


class ImageManager:
    """管理当前图片列表、索引导航和预加载缓存。

    线程安全说明：导航方法（load_folder / load_single / next / prev / goto）
    在 Flet 事件循环线程中调用；预加载在后台线程中执行，仅读写
    `_cache` 和 `_preloading`，通过锁保护。
    """

    PRELOAD_RANGE: int = 1  # 前后各预加载 1 张

    def __init__(self) -> None:
        self._files: list[ImageInfo] = []
        self._index: int = -1
        self._cache: OrderedDict[str, bytes] = OrderedDict()
        self._cache_lock = threading.Lock()
        self._preloading: set[int] = set()
        self._preloading_lock = threading.Lock()
        self._max_cache: int = 5

    # ---- 文件列表 ----

    @property
    def files(self) -> list[ImageInfo]:
        return self._files

    @property
    def count(self) -> int:
        return len(self._files)

    @property
    def index(self) -> int:
        return self._index

    @property
    def current(self) -> ImageInfo | None:
        if 0 <= self._index < len(self._files):
            return self._files[self._index]
        return None

    def is_empty(self) -> bool:
        return not self._files

    # ---- 加载 ----

    def load_folder(self, dir_path: str) -> bool:
        """扫描目录内所有支持格式的图片，按文件名自然排序。"""
        directory = Path(dir_path)
        if not directory.is_dir():
            return False

        collected: list[ImageInfo] = []
        for entry in sorted(directory.iterdir(), key=lambda p: _natural_sort_key(p.name)):
            if entry.is_file() and entry.suffix.lower() in SUPPORTED_EXTS:
                info = self._probe(entry)
                collected.append(info)

        if not collected:
            return False

        self._files = collected
        # 跳过坏图，定位到第一张有效图
        self._index = self._first_valid_index(0)
        self._reset_cache()
        return self._index >= 0

    def load_single(self, file_path: str) -> bool:
        """打开单张图片。若同目录下有其他图片则一并纳入列表，并定位到所选文件。"""
        p = Path(file_path)
        if not p.is_file() or p.suffix.lower() not in SUPPORTED_EXTS:
            return False

        if not self.load_folder(str(p.parent)):
            return False

        for idx, info in enumerate(self._files):
            if Path(info.path) == p.resolve():
                self._index = idx
                return True

        # 理论上不应发生；若目录扫描未包含该文件，则回退到首张有效图。
        self._index = self._first_valid_index(0)
        return self._index >= 0

    def _probe(self, path: Path) -> ImageInfo:
        """读取文件基本信息并尝试获取像素尺寸以验证可解码。"""
        try:
            size = path.stat().st_size
        except OSError:
            return ImageInfo(str(path), path.name, 0, valid=False)

        try:
            with Image.open(path) as img:
                img.verify()
            # verify 后需重新打开才能读 size
            with Image.open(path) as img:
                w, h = img.size
            return ImageInfo(str(path), path.name, size, w, h, valid=True)
        except (UnidentifiedImageError, OSError, ValueError):
            return ImageInfo(str(path), path.name, size, valid=False)

    def _first_valid_index(self, start: int) -> int:
        for i in range(start, len(self._files)):
            if self._files[i].valid:
                return i
        return -1

    # ---- 导航 ----

    def next(self, loop: bool = True) -> int:
        """跳到下一张有效图片，返回新索引（-1 表示无可用图）。"""
        if not self._files:
            return -1
        n = len(self._files)
        for step in range(1, n + 1):
            idx = self._index + step
            if loop:
                idx %= n
            elif idx >= n:
                return self._index
            if self._files[idx].valid:
                self._index = idx
                return idx
        return self._index

    def prev(self, loop: bool = True) -> int:
        """跳到上一张有效图片。"""
        if not self._files:
            return -1
        n = len(self._files)
        for step in range(1, n + 1):
            idx = self._index - step
            if loop:
                idx %= n
            elif idx < 0:
                return self._index
            if self._files[idx].valid:
                self._index = idx
                return idx
        return self._index

    def goto(self, index: int) -> int:
        if not self._files:
            return -1
        if 0 <= index < len(self._files) and self._files[index].valid:
            self._index = index
        return self._index

    # ---- 图像数据获取 ----

    def get_display_bytes(self, info: ImageInfo) -> bytes | None:
        """获取用于显示的图像 bytes。

        原生格式直接返回原始文件 bytes；TIFF / ICO 等非原生格式用 Pillow
        转 PNG。结果会被缓存。
        """
        if not info.valid:
            return None

        with self._cache_lock:
            if info.path in self._cache:
                # 移到队尾（LRU）
                self._cache.move_to_end(info.path)
                return self._cache[info.path]

        data = self._decode(info)
        if data is not None:
            with self._cache_lock:
                self._cache[info.path] = data
                self._evict()
        return data

    def _decode(self, info: ImageInfo) -> bytes | None:
        """解码图片为可被 Flutter Image 控件渲染的 bytes。"""
        ext = Path(info.path).suffix.lower()

        # 原生格式直接读取原始 bytes，速度最快
        if ext in _NATIVE_BYTES_EXTS:
            try:
                with open(info.path, "rb") as f:
                    return f.read()
            except OSError:
                return None

        # 非原生格式（TIFF/ICO/JFIF 等）转 PNG
        try:
            with Image.open(info.path) as img:
                img = img.convert("RGBA") if img.mode not in ("RGB", "RGBA") else img
                import io
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                return buf.getvalue()
        except (UnidentifiedImageError, OSError, ValueError):
            return None

    def _reset_cache(self) -> None:
        with self._cache_lock:
            self._cache.clear()
        with self._preloading_lock:
            self._preloading.clear()

    def _evict(self) -> None:
        while len(self._cache) > self._max_cache:
            self._cache.popitem(last=False)

    # ---- 预加载 ----

    def preload_adjacent(
        self,
        loop: bool,
        on_done: Callable[[int, bytes | None], None] | None = None,
    ) -> None:
        """在后台线程预加载前后各 1 张图片的 bytes。

        on_done 回调在后台线程中调用，用于将结果通知 UI 层。
        """
        if not self._files:
            return

        indices: list[int] = []
        n = len(self._files)
        for delta in (-self.PRELOAD_RANGE, self.PRELOAD_RANGE):
            idx = self._index + delta
            if loop:
                idx %= n
            if 0 <= idx < n and self._files[idx].valid:
                indices.append(idx)

        for idx in indices:
            with self._preloading_lock:
                if idx in self._preloading:
                    continue
                self._preloading.add(idx)

            info = self._files[idx]
            thread = threading.Thread(
                target=self._preload_worker,
                args=(idx, info, on_done),
                daemon=True,
            )
            thread.start()

    def _preload_worker(
        self,
        idx: int,
        info: ImageInfo,
        on_done: Callable[[int, bytes | None], None] | None,
    ) -> None:
        try:
            data = self.get_display_bytes(info)
        except Exception:
            data = None
        finally:
            with self._preloading_lock:
                self._preloading.discard(idx)
        if on_done is not None:
            on_done(idx, data)

    # ---- 删除 ----

    def delete_current(self) -> bool:
        """将当前图片移至回收站，并从列表中移除。"""
        info = self.current
        if info is None:
            return False
        try:
            from send2trash import send2trash
            send2trash(info.path)
        except Exception:
            return False

        del self._files[self._index]
        with self._cache_lock:
            self._cache.pop(info.path, None)

        if self._files:
            self._index = min(self._index, len(self._files) - 1)
            # 确保落在有效图上
            if not self._files[self._index].valid:
                self._index = self._first_valid_index(self._index)
                if self._index < 0:
                    self._index = self._first_valid_index(0)
        else:
            self._index = -1
        return True
