"""共享缓存的实体：编码结果（blob）与解码结果（PIL 图像）。

两级缓存各自解决一个具体浪费：

`BlobCache`（磁盘，跨进程跨启动）
    「同一张图，谁显示过就别再算一遍」。产物是显示用字节流（JPEG/PNG），
    键里带上旋转/翻转/尺寸预算 —— 变换过的显示与原始显示是两个不同结果。
    落盘而不是留在内存：窗口子进程可以直接按路径读文件，不必让服务把
    几 MB 的图片再复制一遍；进程重启后缓存依然有效。

`DecodedCache`（内存，进程内）
    「同一张图，旋转/翻转时别再解码一遍」。旋转只影响显示层，
    像素解码结果完全一样，缓存它就能让 R/L/H/V 这类操作从
    「解码 200ms + 编码 40ms」降到「编码 40ms」。
    容量按**像素总量**限制而不是按张数 —— 一张 100MP 长截图能顶几十张缩略图。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from . import paths

# 默认预算：磁盘 512MB（约可放 300~800 张常用照片），内存里只留元信息
DEFAULT_MAX_DISK_BYTES = 512 << 20
MAX_INDEX_ENTRIES = 8192          # 内存里最多记多少条「键 -> 文件」映射

# 解码缓存的预算：条目数 + 像素总量双限
DEFAULT_DECODED_ENTRIES = 3
DEFAULT_DECODED_PIXELS = 40_000_000

_HEADER_SEP = b"\n"


def _mb_env(name: str, default_bytes: int) -> int:
    """读「以 MB 为单位」的环境变量预算（非法值退回默认）。"""
    raw = os.environ.get(name)
    if not raw:
        return default_bytes
    try:
        return int(float(raw) * (1 << 20))
    except ValueError:
        return default_bytes


@dataclass(slots=True)
class Blob:
    """一条缓存命中的描述：文件在哪、多大、什么格式、原图元信息。

    `width/height` 是**渲染后**（已变换、已限尺寸）的像素尺寸，直接用来看图；
    `source_width/source_height` 是原图像素尺寸，状态栏要显示的是它。
    这些都写进缓存文件的头部，所以命中时连原文件都不必打开。
    """

    key: str
    path: Path
    mime: str
    width: int
    height: int
    source_width: int = 0
    source_height: int = 0
    frames: int = 1
    file_size: int = 0


def blob_key(
    path: Path,
    mtime_ns: int,
    size: int,
    rotation: int = 0,
    flip_h: bool = False,
    flip_v: bool = False,
    max_pixels: int = 40_000_000,
    max_dim: int = 16_000,
) -> str:
    """缓存键：文件身份（路径+修改时间+大小）+ 显示参数。

    带 mtime/size 是为了「原文件被改过就自动失效」——图片被修图软件覆盖保存后
    必须重新渲染，不能拿旧缓存糊弄用户。
    """
    raw = "\x1f".join([
        os.path.normcase(os.path.abspath(str(path))),
        str(mtime_ns), str(size),
        str(rotation), "1" if flip_h else "0", "1" if flip_v else "0",
        str(max_pixels), str(max_dim),
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


class BlobCache:
    """编码结果的磁盘二级缓存（内存里只留索引，避免服务常驻占用大内存）。"""

    def __init__(self, root: Path | None = None, max_bytes: int | None = None):
        self.root = paths.ensure(Path(root) if root else paths.blob_dir())
        if max_bytes is None:
            max_bytes = _mb_env("CSIV_CACHE_MB", DEFAULT_MAX_DISK_BYTES)
        self.max_bytes = int(max_bytes)
        self._lock = threading.Lock()
        self._index: OrderedDict[str, Blob] = OrderedDict()
        self._writes = 0
        self._total_bytes = 0
        self._hits = 0
        self._misses = 0
        self._scan_existing()

    # ---------- 读 ----------

    def get(self, key: str) -> Blob | None:
        """命中则返回 Blob；文件被外部清掉时自动当作未命中。"""
        with self._lock:
            blob = self._index.get(key)
            if blob is None:
                # 索引没记（比如上个进程写的）→ 直接看文件在不在，避免整盘扫描
                candidate = self.root / f"{key}.blob"
                if not candidate.is_file():
                    self._misses += 1
                    return None
                blob = self._load_meta(candidate, key)
                if blob is None:
                    self._misses += 1
                    return None
                self._remember(blob)
            elif not blob.path.is_file():
                self._index.pop(key, None)
                self._misses += 1
                return None
            self._index.move_to_end(key)
            self._hits += 1
            try:
                os.utime(blob.path, None)     # 刷新 mtime：LRU 淘汰看它
            except OSError:
                pass
            return blob

    # ---------- 写 ----------

    def put(
        self,
        key: str,
        data: bytes,
        mime: str,
        width: int,
        height: int,
        source_width: int = 0,
        source_height: int = 0,
        frames: int = 1,
        file_size: int = 0,
    ) -> Blob:
        """写入一条缓存；返回可直接给窗口读取的 Blob。"""
        path = self.root / f"{key}.blob"
        header = json.dumps({
            "mime": mime, "w": width, "h": height,
            "ow": source_width, "oh": source_height,
            "f": frames, "s": file_size,
        }).encode("utf-8")
        tmp = self.root / f"{key}.{os.getpid()}.tmp"
        try:
            with open(tmp, "wb") as fh:
                fh.write(header + _HEADER_SEP)
                fh.write(data)
            os.replace(tmp, path)             # 原子替换：读方永远看不到半个文件
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        blob = Blob(
            key=key, path=path, mime=mime, width=width, height=height,
            source_width=source_width or width, source_height=source_height or height,
            frames=frames, file_size=file_size,
        )
        with self._lock:
            self._remember(blob)
            self._total_bytes += len(data) + len(header)
            self._writes += 1
            if self._total_bytes > self.max_bytes:
                self._prune_locked()
        return blob

    def read(self, blob: Blob) -> bytes:
        """按 Blob 读回字节流（跳过头部）。"""
        with open(blob.path, "rb") as fh:
            head = fh.readline()
            if not head.endswith(_HEADER_SEP):
                raise OSError("缓存文件损坏")
            return fh.read()

    # ---------- 维护 ----------

    def _remember(self, blob: Blob) -> None:
        self._index[blob.key] = blob
        self._index.move_to_end(blob.key)
        while len(self._index) > MAX_INDEX_ENTRIES:
            self._index.popitem(last=False)

    def _load_meta(self, path: Path, key: str) -> Blob | None:
        try:
            with open(path, "rb") as fh:
                head = fh.readline(256)
            meta = json.loads(head.rstrip(_HEADER_SEP).decode("utf-8"))
            return Blob(
                key=key, path=path,
                mime=str(meta.get("mime", "image/jpeg")),
                width=int(meta.get("w", 0)), height=int(meta.get("h", 0)),
                source_width=int(meta.get("ow", 0)), source_height=int(meta.get("oh", 0)),
                frames=int(meta.get("f", 1) or 1), file_size=int(meta.get("s", 0)),
            )
        except (OSError, ValueError, TypeError):
            return None

    def _scan_existing(self) -> None:
        """启动时统计已有缓存体积，并把残留的临时文件清掉。"""
        total = 0
        try:
            entries = list(os.scandir(self.root))
        except OSError:
            return
        for entry in entries:
            try:
                if entry.name.endswith(".tmp"):
                    os.unlink(entry.path)
                    continue
                if not entry.name.endswith(".blob"):
                    continue
                stat = entry.stat()
                total += stat.st_size
                key = entry.name[:-5]
                blob = self._load_meta(Path(entry.path), key)
                if blob is not None:
                    self._remember(blob)
            except OSError:
                continue
        self._total_bytes = total

    def _prune_locked(self) -> None:
        """按最久未访问淘汰，直到回落到预算的 85%（留出抖动空间）。"""
        target = int(self.max_bytes * 0.85)
        removed = 0
        while self._total_bytes > target and self._index and removed < 512:
            key, blob = next(iter(self._index.items()))
            self._index.pop(key, None)
            size = 0
            try:
                size = blob.path.stat().st_size
                blob.path.unlink()
            except OSError:
                pass
            self._total_bytes = max(0, self._total_bytes - size)
            removed += 1

    def stats(self) -> dict:
        total = 0
        count = 0
        try:
            for entry in os.scandir(self.root):
                if entry.name.endswith(".blob"):
                    count += 1
                    total += entry.stat().st_size
        except OSError:
            pass
        return {
            "dir": str(self.root),
            "count": count,
            "bytes": total,
            "limit_bytes": self.max_bytes,
            "hits": self._hits,
            "misses": self._misses,
            "writes": self._writes,
        }

    def clear(self) -> int:
        """清空磁盘缓存；返回删除的文件数。"""
        removed = 0
        with self._lock:
            try:
                entries = list(os.scandir(self.root))
            except OSError:
                return 0
            for entry in entries:
                try:
                    os.unlink(entry.path)
                    removed += 1
                except OSError:
                    continue
            self._index.clear()
            self._total_bytes = 0
        return removed


class DecodedCache:
    """解码结果 LRU：让「旋转/翻转/换尺寸重编码」不再重新解码像素。

    存的是**任意值**（服务侧放 `BaseImage`：图像 + 原图尺寸 + 帧数），
    只按像素量做预算 —— 缓存实现不必知道上层业务对象长什么样。
    """

    def __init__(
        self,
        entries: int = DEFAULT_DECODED_ENTRIES,
        max_pixels: int = DEFAULT_DECODED_PIXELS,
    ):
        self._lock = threading.Lock()
        self._items: OrderedDict[str, tuple[object, int]] = OrderedDict()
        self._max_entries = max(1, entries)
        self._max_pixels = max(1, max_pixels)
        self._pixels = 0
        self._hits = 0
        self._misses = 0

    def get(self, key: str):
        with self._lock:
            item = self._items.get(key)
            if item is None:
                self._misses += 1
                return None
            self._items.move_to_end(key)
            self._hits += 1
            return item[0]

    def put(self, key: str, value: object, pixels: int) -> None:
        """存一份解码结果；超出预算时从最久未用的开始释放。"""
        if pixels > self._max_pixels:          # 单张就超预算：不缓存，免得挤掉别人
            return
        with self._lock:
            old = self._items.pop(key, None)
            if old is not None:
                self._pixels -= old[1]
            self._items[key] = (value, pixels)
            self._pixels += pixels
            while self._items and (
                len(self._items) > self._max_entries or self._pixels > self._max_pixels
            ):
                _, (victim, victim_pixels) = self._items.popitem(last=False)
                self._pixels -= victim_pixels
                _dispose(victim)

    def stats(self) -> dict:
        with self._lock:
            return {
                "entries": len(self._items),
                "megapixels": round(self._pixels / 1_000_000, 1),
                "hits": self._hits,
                "misses": self._misses,
            }

    def clear(self) -> None:
        with self._lock:
            for victim, _ in self._items.values():
                _dispose(victim)
            self._items.clear()
            self._pixels = 0


def _dispose(value: object) -> None:
    """释放缓存条目持有的资源（PIL 图像关句柄；其它对象忽略）。"""
    close = getattr(value, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass



class DirIndex:
    """目录图片列表的短期缓存：多个窗口看同一个文件夹时只扫描一次。

    TTL 默认 8 秒 —— 既能覆盖「连开好几张同目录图片」的连续操作，
    又不会让新建/删除的文件迟迟不出现（用户等不了半分钟）。
    """

    def __init__(self, ttl: float = 8.0):
        self.ttl = ttl
        self._lock = threading.Lock()
        self._items: dict[str, tuple[float, list[Path]]] = {}

    def get(self, folder: Path) -> list[Path] | None:
        key = os.path.normcase(os.path.abspath(str(folder)))
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            stamp, listing = item
            if time.time() - stamp > self.ttl:
                self._items.pop(key, None)
                return None
            return list(listing)

    def put(self, folder: Path, listing: list[Path]) -> None:
        key = os.path.normcase(os.path.abspath(str(folder)))
        with self._lock:
            if len(self._items) > 64:
                self._items.clear()
            self._items[key] = (time.time(), list(listing))

    def invalidate(self, folder: Path) -> None:
        key = os.path.normcase(os.path.abspath(str(folder)))
        with self._lock:
            self._items.pop(key, None)
