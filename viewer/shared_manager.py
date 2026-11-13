"""窗口侧的图片管理器：**状态在本地，重活交给共享服务**。

这是整个重构的关键接缝。`ImageViewerApp` 一直只认 `ImageManager` 的那套接口，
所以这里用继承而不是重写：导航、变换、删除、导出这些「便宜的逻辑」全部复用，
只把三件「贵的事」接管过来：

1. `get_display()`  —— 解码+编码：命中共享缓存时只是读一个已编码好的文件；
2. `frame_count()/info()/_decodable()` —— 原来要靠本地解码才知道的信息，改问服务；
3. `preload()` —— 让**服务**在后台预热前后两张，而不是每个窗口各自解码。

三条原则：
- **能降级**：服务不可用（没启动、被禁用、崩了）就退回父类，功能一个不少，
  只是慢一点；
- **不重复解码**：静态图一律不在窗口里解码，动图必须本地逐帧（尤其 GIF/WebP
  靠文件句柄 seek，跨进程做只会更慢）；
- **状态不外移**：当前索引、旋转/翻转、帧号都由窗口自己持有，
  所以宿主换了、连接断了，用户看到的东西不会跳。
"""
from __future__ import annotations

from pathlib import Path

from . import imaging
from .image_manager import ImageManager
from .service.client import ServiceLink, default_link
from .service.protocol import ServiceError


class RemoteImageManager(ImageManager):
    """在 `ImageManager` 之上叠一层共享缓存；接口与父类完全一致。"""

    def __init__(self, link: ServiceLink | None = None):
        # 本地解码缓存只留 2 张：动图前后帧 + 降级时的当前图
        super().__init__(cache_size=2)
        self.link = link if link is not None else default_link()
        self.last_shared_hit = False      # 当前这张图是否直接来自共享缓存
        self.service_offline = False      # 服务是否已经不可用（本张图内）
        self._frames: dict[int, int] = {}  # 索引 -> 帧数（服务告知，避免本地解码）
        self._meta: dict | None = None     # 最近一次 show/probe 的元信息
        self._meta_index = -1

    # ------------------------------------------------------------------
    # 显示：唯一的热路径
    # ------------------------------------------------------------------

    def get_display(self) -> tuple[str | None, int, int]:
        if not self.files:
            return None, 0, 0
        key = (self.index, self.current_frame, self.rotation, self.flip_h, self.flip_v)
        cached = self._encoded.get(key)
        if cached is not None:
            return cached                   # 平移/缩放/选区刷新都走这里，零开销

        if self._remote_ok():
            result = self._remote_display(key)
            if result is not None:
                return result
        # 降级：本地解码（动图必须本地逐帧；服务不可用/失败时也走这里）
        self.last_shared_hit = False
        return super().get_display()

    def _remote_ok(self) -> bool:
        """这张图能不能交给服务渲染：静态图可以，动图不行。"""
        if not self.link.enabled or not self.files:
            return False
        return self._frames.get(self.index, 1) <= 1

    def _remote_display(self, key: tuple) -> tuple[str, int, int] | None:
        path = self.files[self.index]
        meta = self._show(path)
        if meta is None:
            return None
        uri = self._blob_uri(meta)
        if uri is None:
            # 缓存文件被外部清理（或磁盘满）→ 让服务重算一次再读
            meta = self._show(path)
            if meta is None:
                return None
            uri = self._blob_uri(meta)
            if uri is None:
                return None
        result = (uri, int(meta["dw"]), int(meta["dh"]))
        self._encoded[key] = result
        if len(self._encoded) > 64:          # 与父类一致：防止组合无限膨胀
            self._encoded.clear()
            self._encoded[key] = result
        return result

    def _show(self, path: Path) -> dict | None:
        """向服务索要显示数据；失败返回 None（调用方降级到本地解码）。"""
        try:
            meta = self.link.call(
                "show",
                path=str(path),
                rotation=self.rotation,
                flip_h=self.flip_h,
                flip_v=self.flip_v,
                frame=self.current_frame,
            )
        except ServiceError as exc:
            self.service_offline = True
            self.last_error = str(exc)
            return None
        self.service_offline = False
        self._meta = meta
        self._meta_index = self.index
        self._frames[self.index] = int(meta.get("frames", 1) or 1)
        self.last_shared_hit = bool(meta.get("hit"))
        return meta

    @staticmethod
    def _blob_uri(meta: dict) -> str | None:
        """把服务落盘的 blob 读成 data URI（读失败返回 None）。

        先跳过一行 JSON 头再读正文：避免把整个文件读成 bytes 再切一刀（大图省一次拷贝）。
        """
        try:
            with open(meta["blob"], "rb") as fh:
                fh.readline()                # {"mime":...,"w":...,"h":...}\n
                data = fh.read()
        except (OSError, KeyError, TypeError):
            return None
        if not data:
            return None
        return imaging.bytes_to_data_uri(data, str(meta.get("mime", "image/jpeg")))

    # ------------------------------------------------------------------
    # 元信息：不再为了「知道多大」而解码
    # ------------------------------------------------------------------

    def info(self) -> dict:
        if not self.files:
            return {"name": "", "w": 0, "h": 0, "size": 0, "pos": 0, "total": 0, "frames": 0}
        meta = self._meta if self._meta_index == self.index else self._probe(self.index)
        if meta is None or not meta.get("ok", True):
            return super().info()            # 服务不可用或坏图：交回父类
        p = self.files[self.index]
        return {
            "name": p.name,
            "w": int(meta.get("w", 0)),
            "h": int(meta.get("h", 0)),
            "size": int(meta.get("size", 0)),
            "pos": self.index + 1,
            "total": len(self.files),
            "frames": self._frames.get(self.index, int(meta.get("frames", 1) or 1)),
        }

    def frame_count(self) -> int:
        if not self.files:
            return 0
        n = self._frames.get(self.index)
        if n is None:
            meta = self._probe(self.index)
            if meta is None:
                return super().frame_count()
            n = int(meta.get("frames", 1) or 1)
        if n > 1:
            return super().frame_count()     # 动图：本地解码出真实帧数（并保留句柄）
        return n

    def _decodable(self, idx: int) -> bool:
        """能不能显示：静态图问服务（不解码），动图仍由父类本地判断。"""
        if idx in self._failed:
            return False
        if self._frames.get(idx, 0) > 1:
            return super()._decodable(idx)
        if not self.link.enabled:
            return super()._decodable(idx)
        meta = self._probe(idx)
        if meta is None:
            return super()._decodable(idx)
        if not meta.get("ok", True):
            self._failed.add(idx)
            self.last_error = str(meta.get("error") or "无法解码")
            return False
        return True

    def _probe(self, idx: int) -> dict | None:
        """读图头（帧数/原始尺寸）；同时把帧数记下来供后续判断。"""
        if not self.files:
            return None
        try:
            meta = self.link.call("probe", path=str(self.files[idx]))
        except ServiceError as exc:
            self.service_offline = True
            self.last_error = str(exc)
            return None
        self.service_offline = False
        frames = int(meta.get("frames", 1) or 1)
        self._frames[idx] = frames
        if idx == self.index:
            self._meta, self._meta_index = meta, idx
        return meta

    # ------------------------------------------------------------------
    # 目录与预加载
    # ------------------------------------------------------------------

    def folder_candidates(self) -> list[Path]:
        """目录扫描也走共享索引：多个窗口看同一目录时只扫一次盘。"""
        if not self.files or not self.link.enabled:
            return super().folder_candidates()
        try:
            result = self.link.call("list", folder=str(self.files[self.index].parent))
        except ServiceError:
            return super().folder_candidates()
        return [Path(p) for p in result.get("paths", [])]

    def preload(self) -> None:
        """预渲染前后各一张：让「按下方向键」变成「命中缓存」。"""
        if len(self.files) <= 1:
            return
        if not self.link.enabled:
            super().preload()
            return
        paths = [
            str(self.files[(self.index + delta) % len(self.files)])
            for delta in (-1, 1)
        ]
        try:
            self.link.call(
                "preload", paths=paths,
                rotation=self.rotation, flip_h=self.flip_h, flip_v=self.flip_v,
            )
        except ServiceError:
            super().preload()

    # ------------------------------------------------------------------
    # 导出 / 诊断
    # ------------------------------------------------------------------

    def save_current_frame(self, out_path: str | Path) -> None:
        """导出当前帧：服务侧直接写盘（全分辨率 + 显示层变换）。"""
        if self.link.enabled and self.files:
            try:
                self.link.call(
                    "export",
                    path=str(self.files[self.index]),
                    out=str(out_path),
                    frame=self.current_frame,
                    rotation=self.rotation,
                    flip_h=self.flip_h,
                    flip_v=self.flip_v,
                )
                return
            except ServiceError:
                pass
        super().save_current_frame(out_path)

    def stats(self) -> dict:
        """服务与缓存的健康状况（菜单「缓存状态」）。"""
        return self.link.status()

    # ------------------------------------------------------------------
    # 状态复位
    # ------------------------------------------------------------------

    def _reset(self) -> None:
        super()._reset()
        self._frames.clear()
        self._meta = None
        self._meta_index = -1
        self.last_shared_hit = False

    def _remap_indices(self, old: int, new: int) -> None:
        """单图打开后补全目录列表时，索引会位移，缓存标记要一起搬家。"""
        super()._remap_indices(old, new)
        if old != new and old in self._frames:
            self._frames[new] = self._frames.pop(old)
        if self._meta_index == old:
            self._meta_index = new
