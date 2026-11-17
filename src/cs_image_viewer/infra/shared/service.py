"""常驻服务的能力核心：`ImageService`。

它是一组**无会话（stateless）的纯操作**：给路径和显示参数，返回磁盘上
编码好的 blob。所有窗口共享同一个实例，因此：

- 谁先打开某张图，谁付解码成本，其余窗口直接命中；
- 服务不需要记住「哪个窗口在看第几张」，窗口崩了、宿主换人了，
  重连之后下一次请求照样能命中 —— 共享缓存天然免疫进程更替。

对外操作（每个都对应一条 IPC 指令）：
    show     取显示用 blob（命中直接返回，未命中则解码+编码+落盘）
    probe    读图头（判断能不能解码、帧数、原始尺寸），不解像素
    list     列出目录内的图片（带 TTL 缓存，多个窗口只扫一次盘）
    preload  后台预渲染若干张（上一张/下一张），把「切换」变成「命中」
    export   导出当前帧为 PNG（含显示层变换）
    stats    服务与缓存健康状况（菜单里的「缓存状态」）
    clear    清空磁盘缓存
"""
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from .. import imaging
from ..catalog import list_images
from .blobs import Blob, BlobCache, DecodedCache, DirIndex, blob_key
from .protocol import ServiceError
from .runtime import Counter

# 预加载队列上限：超过就丢弃最远端请求（用户已经翻过去了，不值得再算）
MAX_PENDING_PRELOADS = 24


@dataclass(slots=True)
class BaseImage:
    """解码结果：EXIF 已摆正、色彩已归一化的图像 + 原图元信息。

    存在的意义是让「同一张图的第二次编码」跳过解码：
    旋转、翻转、切换尺寸预算都只需要重新编码这一张已经在内存里的图。
    """

    image: Image.Image
    source_size: tuple[int, int]
    frames: int


class ImageService:
    """共享缓存服务：解码、编码、目录索引、预加载。线程安全。"""

    def __init__(self, blob_root: Path | None = None, preload_workers: int = 2):
        self.blobs = BlobCache(blob_root)
        self.decoded = DecodedCache()
        self.dirs = DirIndex(ttl=float(os.environ.get("CSIV_DIR_TTL", "8.0")))
        self.counters = Counter()
        self.started = time.time()
        self._render_lock = threading.RLock()
        self._pending: set[str] = set()
        self._pending_lock = threading.Lock()
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, preload_workers), thread_name_prefix="csiv-preload"
        )
        self._closed = False

    # ------------------------------------------------------------------
    # 对外操作
    # ------------------------------------------------------------------

    def op_show(
        self,
        path: str,
        rotation: int = 0,
        flip_h: bool = False,
        flip_v: bool = False,
        frame: int = 0,
    ) -> dict:
        """取当前显示需要的 blob 元信息（真正的大块数据留在磁盘上）。"""
        p = Path(path)
        mtime_ns, size = self._stat(p)
        key = blob_key(p, mtime_ns, size, rotation, flip_h, flip_v,
                       imaging.MAX_ENCODE_PIXELS, imaging.MAX_ENCODE_DIM)
        blob = self.blobs.get(key)
        if blob is not None:
            self.counters.add("show_hit")
            return self._reply(blob, hit=True)

        self.counters.add("show_miss")
        started = time.perf_counter()
        blob, reused = self._render_blob(p, key, (mtime_ns, size), rotation, flip_h, flip_v, frame)
        elapsed_ms = (time.perf_counter() - started) * 1000
        self.counters.add("render_ms", int(elapsed_ms))
        self.counters.add("render_calls")
        # reused：另一个窗口刚好在渲染同一张图，等锁结束后拿到了它的结果 ——
        # 本窗口同样没有解码，对用户而言和命中缓存是一回事
        return self._reply(blob, hit=reused)

    def op_probe(self, path: str) -> dict:
        """读图头信息：能否解码、原始尺寸、帧数、文件大小。"""
        p = Path(path)
        _, size = self._stat(p)
        try:
            width, height, frames = imaging.probe(p)
        except Exception as exc:
            self.counters.add("probe_fail")
            return {"ok": False, "error": _short(exc), "size": size}
        self.counters.add("probe_ok")
        return {
            "ok": True, "w": width, "h": height, "frames": frames, "size": size,
        }

    def op_list(self, folder: str) -> dict:
        """列出目录内的图片（自然排序），命中索引缓存时零磁盘扫描。"""
        path = Path(folder)
        cached = self.dirs.get(path)
        if cached is not None:
            self.counters.add("list_hit")
            return {"paths": [str(item) for item in cached], "cached": True}
        listing = list_images(path)
        self.dirs.put(path, listing)
        self.counters.add("list_miss")
        return {"paths": [str(item) for item in listing], "cached": False}

    def op_preload(
        self,
        paths: list[str],
        rotation: int = 0,
        flip_h: bool = False,
        flip_v: bool = False,
    ) -> dict:
        """后台预渲染：把「切换图片」的开销提前到用户还没按键的时候。"""
        queued = 0
        for raw in paths or []:
            if self._closed:
                break
            p = Path(raw)
            try:
                mtime_ns, size = self._stat(p)
            except ServiceError:
                continue
            key = blob_key(p, mtime_ns, size, rotation, flip_h, flip_v,
                           imaging.MAX_ENCODE_PIXELS, imaging.MAX_ENCODE_DIM)
            if self.blobs.get(key) is not None:
                self.counters.add("preload_skip")
                continue
            with self._pending_lock:
                if key in self._pending:
                    continue
                if len(self._pending) >= MAX_PENDING_PRELOADS:
                    self.counters.add("preload_dropped")
                    continue
                self._pending.add(key)
            self._pool.submit(
                self._preload_one, p, key, (mtime_ns, size), rotation, flip_h, flip_v
            )
            queued += 1
        self.counters.add("preload_queued", queued)
        return {"queued": queued, "pending": len(self._pending)}

    def op_export(
        self,
        path: str,
        out: str,
        frame: int = 0,
        rotation: int = 0,
        flip_h: bool = False,
        flip_v: bool = False,
    ) -> dict:
        """导出当前帧为 PNG（全分辨率，含旋转/翻转）。"""
        p = Path(path)
        self._stat(p)
        im = imaging.load_image(p)
        try:
            if imaging.frame_count(im) > 1:
                im.seek(frame)
            im.load()
            cur = ImageOps.exif_transpose(im)
            cur = imaging.normalize_mode(cur)
            cur = imaging.apply_transform(cur, rotation, flip_h, flip_v)
            imaging.save_png(cur, out)
            return {"ok": True, "bytes": os.path.getsize(out)}
        except Exception as exc:
            raise ServiceError(f"导出失败：{_short(exc)}") from None
        finally:
            try:
                im.close()
            except Exception:
                pass

    def op_stats(self) -> dict:
        return {
            "pid": os.getpid(),
            "uptime": round(time.time() - self.started, 1),
            "blobs": self.blobs.stats(),
            "decoded": self.decoded.stats(),
            "counters": self.counters.snapshot(),
            "pending": len(self._pending),
        }

    def op_clear(self) -> dict:
        """清空磁盘缓存（内存里的解码缓存一并释放）。"""
        removed = self.blobs.clear()
        self.decoded.clear()
        self.dirs = DirIndex(ttl=self.dirs.ttl)
        return {"removed": removed}

    def close(self) -> None:
        self._closed = True
        self._pool.shutdown(wait=False, cancel_futures=True)
        self.decoded.clear()

    # ------------------------------------------------------------------
    # 渲染细节
    # ------------------------------------------------------------------

    def _render_blob(
        self,
        p: Path,
        key: str,
        signature: tuple[int, int],
        rotation: int,
        flip_h: bool,
        flip_v: bool,
        frame: int,
    ) -> tuple[Blob, bool]:
        """未命中时真正干活：解码（可复用）-> 变换 -> 限尺寸 -> 编码 -> 落盘。

        返回 `(blob, reused)`：`reused=True` 表示等锁期间别人已经算好了同一份结果，
        本次并没有做解码/编码。
        """
        if frame != 0:
            # 指定帧（动图导出）无法复用解码缓存：seek 会改变对象内部状态
            data, mime, width, height = imaging.render(
                p, rotation, flip_h, flip_v, frame=frame
            )
            return self.blobs.put(key, data, mime, width, height), False

        with self._render_lock:
            again = self.blobs.get(key)          # 并发下别人可能刚算完
            if again is not None:
                return again, True
            base = self._base_image(p, signature)   # RLock：可重入，不担心嵌套
            cur = imaging.apply_transform(
                imaging.limit_size(base.image), rotation, flip_h, flip_v
            )
            data, mime = imaging.encode_display(cur)
            src_w, src_h = base.source_size
            blob = self.blobs.put(
                key, data, mime, cur.width, cur.height,
                source_width=src_w, source_height=src_h,
                frames=base.frames, file_size=signature[1],
            )
            return blob, False

    def _base_image(self, p: Path, signature: tuple[int, int]) -> BaseImage:
        """取「摆正 + 归一化」的解码图，尽量复用解码缓存。"""
        dkey = _decoded_key(p, signature)
        cached = self.decoded.get(dkey)
        if cached is not None:
            return cached

        with self._render_lock:                  # 同一张图不并发解码两遍
            cached = self.decoded.get(dkey)
            if cached is not None:
                return cached
            im = imaging.load_image(p)
            detach = False
            try:
                source_size = (im.size[0], im.size[1])
                frames = imaging.frame_count(im)
                if frames > 1:
                    im.seek(0)
                    cur = imaging.normalize_mode(im.copy())
                else:
                    im.load()
                    cur = ImageOps.exif_transpose(im)
                    cur = imaging.normalize_mode(cur)
                    if cur is im:
                        # 没有 EXIF 旋转、模式已就绪：像素就是 im 本体，
                        # 不能 close(im)，只把文件句柄单独还掉
                        detach = True
                        fp = getattr(im, "fp", None)
                        if fp is not None:
                            try:
                                fp.close()
                            except Exception:
                                pass
            finally:
                if not detach:
                    try:
                        im.close()
                    except Exception:
                        pass

            base = BaseImage(image=cur, source_size=source_size, frames=frames)
            if frames <= 1:                      # 动图不缓存：seek 状态会被别的窗口踩坏
                self.decoded.put(dkey, base, cur.width * cur.height)
            return base

    def _preload_one(
        self,
        p: Path,
        key: str,
        signature: tuple[int, int],
        rotation: int,
        flip_h: bool,
        flip_v: bool,
    ) -> None:
        """预加载工作线程：失败无所谓（用户切过去时会重算）。"""
        try:
            if self._closed or self.blobs.get(key) is not None:
                return
            self._render_blob(p, key, signature, rotation, flip_h, flip_v, 0)
            self.counters.add("preload_done")
        except Exception:
            self.counters.add("preload_fail")
        finally:
            with self._pending_lock:
                self._pending.discard(key)

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    @staticmethod
    def _stat(p: Path) -> tuple[int, int]:
        """(mtime_ns, size)；文件不存在时给出明确错误给前端提示。"""
        try:
            st = p.stat()
        except OSError:
            raise ServiceError(f"文件不存在或无法访问：{p.name}") from None
        if not p.is_file():
            raise ServiceError(f"不是文件：{p.name}")
        return st.st_mtime_ns, st.st_size

    @staticmethod
    def _reply(blob: Blob, hit: bool) -> dict:
        return {
            "blob": str(blob.path),
            "mime": blob.mime,
            "dw": blob.width,
            "dh": blob.height,
            "w": blob.source_width or blob.width,
            "h": blob.source_height or blob.height,
            "frames": blob.frames,
            "size": blob.file_size,
            "hit": hit,
        }


def _decoded_key(p: Path, signature: tuple[int, int]) -> str:
    return f"{os.path.normcase(os.path.abspath(str(p)))}|{signature[0]}|{signature[1]}"


def _short(exc: Exception) -> str:
    text = str(exc)
    return text if len(text) <= 120 else text[:117] + "..."
