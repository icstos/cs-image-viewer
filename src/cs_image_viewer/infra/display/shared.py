"""共享缓存解码器 —— ``DisplayProvider`` 的跨进程实现。

窗口进程本身**不解码静态图**：它把「路径 + 显示参数」发给常驻主进程，主进程
解码、变换、编码后落盘，窗口只读那份已编码好的文件。所以同一张图开第二个窗口，
第一个窗口付过的成本可以直接命中。

两条不能省的降级路径：

* **动图必须本地逐帧**：GIF/WebP 靠文件句柄 seek，跨进程做只会更慢；
* **服务不可用就退回本地**：没启动、被禁用、崩了，功能一个不少，只是慢一点。

它持有一份 :class:`LocalDisplay` 正是为了这两件事 —— 不是重复实现，而是
「同一套本地能力」在需要时被调用。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...models import DisplayImage, ImageProbe, Transform
from .. import imaging
from ..shared.client import ServiceLink
from ..shared.protocol import ServiceError
from .local import LocalDisplay

__all__ = ["SharedDisplay"]


class SharedDisplay:
    """先问共享缓存、必要时本地解码。实现 ``service.ports.DisplayProvider``。"""

    def __init__(self, link: ServiceLink, local: LocalDisplay | None = None) -> None:
        self._link = link
        self._local = local or LocalDisplay(cache_size=2)
        self._frames: dict[str, int] = {}          # 路径 -> 帧数（服务告知）
        self._encoded: dict[tuple[Any, ...], DisplayImage] = {}
        self._probe_cache: dict[str, ImageProbe] = {}
        self.offline = False                       # 最近一次调用是否已降级为本地
        self._last_error: str = ""

    # ------------------------------------------------------------------
    # 端口实现
    # ------------------------------------------------------------------

    def probe(self, path: Path) -> ImageProbe:
        """图头信息：静态图问服务（不解像素），动图交本地。"""
        key = f"{path}|{_stamp(path)}"
        cached = self._probe_cache.get(key)
        if cached is not None:
            return cached
        result: ImageProbe | None = None
        if self._remote_ok():
            try:
                meta = self._link.call("probe", path=str(path))
            except ServiceError as exc:
                self._mark_offline(exc)
            else:
                self.offline = False
                result = ImageProbe(
                    ok=bool(meta.get("ok", True)),
                    width=int(meta.get("w", 0) or 0),
                    height=int(meta.get("h", 0) or 0),
                    frames=int(meta.get("frames", 1) or 1),
                    size_bytes=int(meta.get("size", 0) or 0),
                    error=str(meta.get("error") or ""),
                )
                if result.ok:
                    self._frames[str(path)] = result.frames
        if result is None:
            result = self._local.probe(path)
            self._last_error = self._local.error_text()
            if result.ok:
                self._frames[str(path)] = result.frames
        self._probe_cache[key] = result
        return result

    def display(self, path: Path, transform: Transform, frame: int = 0) -> DisplayImage | None:
        """取一帧可上屏数据：静态图优先共享缓存，动图本地逐帧。"""
        key = (str(path), _stamp(path), transform, frame)
        cached = self._encoded.get(key)
        if cached is not None:
            return cached
        if self._remote_ok() and self._frame_count(path) <= 1:
            result = self._remote_display(path, transform, frame)
            if result is not None:
                self._remember(key, result)
                return result
        result = self._local.display(path, transform, frame)
        if result is None:
            self._last_error = self._local.error_text()
            return None
        self._remember(key, result)
        return result

    def frame_duration_ms(self, path: Path, frame: int) -> int:
        return self._local.frame_duration_ms(path, frame)

    def list_folder(self, folder: Path) -> list[Path]:
        """目录列表也走共享索引：多个窗口看同一目录时只扫一次盘。"""
        if self._remote_ok():
            try:
                result = self._link.call("list", folder=str(folder))
            except ServiceError as exc:
                self._mark_offline(exc)
            else:
                self.offline = False
                return [Path(item) for item in result.get("paths", [])]
        return self._local.list_folder(folder)

    def preload(self, paths: tuple[Path, ...] | list[Path], transform: Transform) -> None:
        """让**服务**预热前后几张，把「按下方向键」变成「命中缓存」。"""
        if not paths:
            return
        if self._remote_ok():
            try:
                self._link.call(
                    "preload",
                    paths=[str(p) for p in paths],
                    rotation=transform.rotation,
                    flip_h=transform.flip_h,
                    flip_v=transform.flip_v,
                )
                return
            except ServiceError as exc:
                self._mark_offline(exc)
        self._local.preload(paths, transform)

    def export(self, path: Path, out: Path, transform: Transform, frame: int) -> None:
        """导出当前帧：服务侧直接写盘（全分辨率 + 显示层变换）。"""
        if self._remote_ok():
            try:
                self._link.call(
                    "export",
                    path=str(path),
                    out=str(out),
                    frame=frame,
                    rotation=transform.rotation,
                    flip_h=transform.flip_h,
                    flip_v=transform.flip_v,
                )
                return
            except ServiceError as exc:
                self._mark_offline(exc)
        self._local.export(path, out, transform, frame)

    def stats(self) -> dict[str, Any] | None:
        """服务与缓存的健康状况；共享关闭时返回 ``None``。"""
        if not self._link.enabled:
            return None
        return self._link.status()

    def error_text(self) -> str:
        """最近一次失败的原因：共享侧与本地侧取更近的那一条。"""
        return self._last_error or self._local.error_text()

    def close(self) -> None:
        self._encoded.clear()
        self._local.close()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _remote_ok(self) -> bool:
        return bool(self._link.enabled)

    def _frame_count(self, path: Path) -> int:
        """已知帧数就直接用，未知时探测一次（动图不能交给服务渲染）。"""
        known = self._frames.get(str(path))
        if known is not None:
            return known
        return self.probe(path).frames

    def _remote_display(
        self, path: Path, transform: Transform, frame: int
    ) -> DisplayImage | None:
        """向服务索要显示数据；失败返回 ``None`` 让调用方降级。"""
        meta = self._show(path, transform, frame)
        if meta is None:
            return None
        uri = _blob_uri(meta)
        if uri is None:
            # 缓存文件被外部清理（或磁盘满）→ 让服务重算一次再读
            meta = self._show(path, transform, frame)
            if meta is None:
                return None
            uri = _blob_uri(meta)
            if uri is None:
                return None
        return DisplayImage(
            src=uri,
            width=int(meta["dw"]),
            height=int(meta["dh"]),
            shared=bool(meta.get("hit")),
        )

    def _show(self, path: Path, transform: Transform, frame: int) -> dict[str, Any] | None:
        try:
            meta = self._link.call(
                "show",
                path=str(path),
                rotation=transform.rotation,
                flip_h=transform.flip_h,
                flip_v=transform.flip_v,
                frame=frame,
            )
        except ServiceError as exc:
            self._mark_offline(exc)
            return None
        self.offline = False
        self._frames[str(path)] = int(meta.get("frames", 1) or 1)
        return meta

    def _remember(self, key: tuple[Any, ...], result: DisplayImage) -> None:
        self._encoded[key] = result
        if len(self._encoded) > 64:            # 防止组合无限膨胀
            self._encoded.clear()
            self._encoded[key] = result

    def _mark_offline(self, exc: ServiceError) -> None:
        """记录一次降级：``offline`` 供诊断，``last_error`` 供用户提示。"""
        self.offline = True
        self._last_error = str(exc)


def _stamp(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _blob_uri(meta: dict[str, Any]) -> str | None:
    """把服务落盘的 blob 读成 data URI（读失败返回 ``None``）。

    先跳过一行 JSON 头再读正文：避免把整个文件读成 bytes 再切一刀（大图省一次拷贝）。
    """
    try:
        with open(meta["blob"], "rb") as handle:
            handle.readline()                  # {"mime":...,"w":...,"h":...}\n
            data = handle.read()
    except (OSError, KeyError, TypeError):
        return None
    if not data:
        return None
    return imaging.bytes_to_data_uri(data, str(meta.get("mime", "image/jpeg")))
