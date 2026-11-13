"""图片管理器：文件扫描、自然排序、解码缓存、动图逐帧与显示层变换。

纯逻辑模块，不依赖 Flet，便于复用与单元测试。

本类同时承担两个角色：
- 窗口进程里的**本地解码器**（动图逐帧必须在本地，见 `shared_manager`）；
- 共享服务不可用时的**降级路径**。
真正的解码/变换/编码细节在 `viewer.imaging`（纯函数层），这里只负责
「打开哪些文件、缓存多少、当前显示哪一帧」这类有状态的事情。
"""
from __future__ import annotations

import io
import os
import re
from collections import OrderedDict
from pathlib import Path

from PIL import Image, ImageOps

from . import imaging
from .imaging import IMAGE_EXTS  # noqa: F401 —— 由本模块对外导出，兼容旧引用

# 依赖文件句柄进行多帧 seek 的格式（其余格式完全解码后可释放句柄）
_SEEK_FORMATS = imaging.SEEK_FORMATS

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
        """打开单张图片：**只加载这一张**，不碰目录。

        首帧延迟与目录里有多少文件、目录是否在慢速网络盘上完全无关；
        同目录的其余图片由 `expand_folder()` 在显示之后异步补上，
        补上后上一张/下一张即可用，当前这张不会闪、也不会重新解码。
        """
        p = Path(path)
        if not p.is_file() or p.suffix.lower() not in IMAGE_EXTS:
            return False
        self.files = [p]
        self.index = 0
        self._reset()
        return True

    def expand_folder(self) -> bool:
        """同步版补全（供脚本/测试用）：扫描目录并立即应用。

        界面不要直接调它 —— 扫描可能很慢，会卡住事件循环；
        app 侧走 `folder_candidates()` + `apply_folder_listing()` 两段式。
        """
        return self.apply_folder_listing(self.folder_candidates())

    def folder_candidates(self) -> list[Path]:
        """（后台线程）读出当前图片所在目录的图片列表，**不修改任何状态**。

        拆出这一步是为了让「慢的 I/O」只发生在后台线程里：扫描结果拿回主线程后
        再决定要不要应用，避免扫描途中用户已切到别的文件、列表却被旧结果覆盖。
        """
        if not self.files:
            return []
        return self._list_images(self.files[self.index].parent)

    def apply_folder_listing(self, listing: list[Path]) -> bool:
        """（主线程）把扫描结果应用到「单文件列表」上；返回是否真的扩展了。

        - 已经在浏览目录（列表 > 1 张）→ 什么都不做，False；
        - 目录里也只有这一张 → 保持单文件，False；
        - 成功 → 当前文件仍是同一张图，索引与已解码/已编码缓存**一起搬家**，
          所以既不会重新解码，也不会让画面闪一下。
        """
        if len(self.files) != 1 or len(listing) <= 1:
            return False
        current = self.files[self.index]
        new_index = self._locate(current, listing)
        if new_index < 0:                    # 目录里居然没有它（权限/竞态）：保持原状
            return False
        self._remap_indices(self.index, new_index)
        self.files = listing
        self.index = new_index
        return True

    def scan_folder(self, folder: str | Path) -> int:
        """扫描目录内所有图片，按文件名自然排序；返回图片总数。"""
        self.files = self._list_images(Path(folder))
        self.index = 0
        self._reset()
        return len(self.files)

    @staticmethod
    def _list_images(folder: Path) -> list[Path]:
        """列出目录内的图片文件（自然排序）；目录不可读时返回空列表。

        用 `os.scandir` 而非 `iterdir`：文件类型来自目录项自带的元数据，
        不必对每个文件再 stat 一次 —— 大目录与网络盘上省下的就是成百上千次系统调用。
        """
        try:
            with os.scandir(folder) as it:
                files = [
                    Path(entry.path) for entry in it
                    if entry.is_file()
                    and os.path.splitext(entry.name)[1].lower() in IMAGE_EXTS
                ]
        except OSError:
            return []
        return sorted(files, key=lambda p: natural_key(p.stem))

    @staticmethod
    def _locate(target: Path, files: list[Path]) -> int:
        """在列表中定位目标文件的下标；找不到返回 -1。

        先用原始文件名比一遍（C 层字符串比较，不产生临时对象），
        再退回大小写不敏感比较 —— 常见的「文件名一致」在这里是 O(n) 次无分配比较。
        """
        name = target.name
        for i, f in enumerate(files):
            if f.name == name:
                return i
        lowered = name.lower()               # Windows 文件名大小写不敏感
        for i, f in enumerate(files):
            if f.name.lower() == lowered:
                return i
        return -1

    def _remap_indices(self, old: int, new: int) -> None:
        """把以 old 为下标的解码/编码缓存搬到 new（列表补全后索引位移）。"""
        if old == new:
            return
        self._opened = OrderedDict(
            (new if idx == old else idx, im) for idx, im in self._opened.items()
        )
        self._encoded = {
            (new if key[0] == old else key[0], *key[1:]): value
            for key, value in self._encoded.items()
        }
        if old in self._failed:
            self._failed.discard(old)
            self._failed.add(new)

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
        rendered = imaging.apply_transform(
            imaging.limit_size(im), self.rotation, self.flip_h, self.flip_v
        )
        result = (imaging.to_data_uri(rendered), *rendered.size)
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
            im = imaging.load_image(self.files[idx])
            if self.files[idx].suffix.lower() == ".png":
                self._extract_apng(idx)          # APNG：Pillow 只读首帧，需单独解码
            # 单帧且非 seek 依赖格式：完全解码后释放文件句柄，避免占用文件
            n = imaging.frame_count(im)
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
            return imaging.normalize_mode(self._apng_frames[frame])
        im = self._open(idx)
        if im is None:
            return None
        try:
            im.seek(frame)
            cur = im.copy()                      # 复制当前帧，避免后续 seek 干扰
        except Exception:
            return None
        cur = ImageOps.exif_transpose(cur)       # 按 EXIF 方向摆正照片
        return imaging.normalize_mode(cur)

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

    # ---------- 兼容旧接口（内部委托到 imaging 纯函数层） ----------

    @staticmethod
    def _normalize_mode(im: Image.Image) -> Image.Image:
        """统一色彩模式（委托 imaging，保留旧名给子类/测试使用）。"""
        return imaging.normalize_mode(im)

    def _apply_transform(self, im: Image.Image) -> Image.Image:
        """显示层旋转/翻转（委托 imaging）。"""
        return imaging.apply_transform(im, self.rotation, self.flip_h, self.flip_v)
