"""文案排版 —— 状态、数字、诊断文本的**唯一**拼装处。

为什么把它从数据层与渲染层里分出来：

* 数据层（``service``）只给事实（宽高、字节数、页码），不该知道「×」还是「x」、
  该不该省略单位；
* 渲染层（``view``）只摆控件，不该知道「3 / 12 · 42% 适应」这段话怎么拼；
* 于是「用户看到的每个字」都能在没有窗口的环境里被断言 —— 文案改动不必截图比对。

排版原则：**信息密度按阅读顺序递减**。文件名放最左（最重要），规格居中（看一眼
就走），页码与倍率放最右（持续变化、只给个位置感）。空窗口不写「0 / 0」这种
没有信息量的字。
"""

from __future__ import annotations

from ..models import ImageInfo, ServiceReport, StatusSnapshot, ViewSize, ViewTransform
from ..models.geometry import compute_scale

__all__ = ["format_service_report", "format_size", "format_status", "format_zoom"]


def format_size(num_bytes: int) -> str:
    """字节数 → ``"1.2 MB"`` / ``"860 B"``。"""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{num_bytes} B"


def format_zoom(view: ViewTransform, image: ViewSize, viewport: ViewSize) -> str:
    """倍率文案：``"42% 适应"`` / ``"100%"`` / ``"230%"``。

    显示名与 :attr:`ViewTransform.mode` 一一对应 —— 界面与诊断读的是同一份规则，
    不会出现「看到的倍率」和「写出来的倍率」各算一遍。
    """
    scale = compute_scale(view, image, viewport) * 100
    if view.mode == "fit":
        return f"{scale:.0f}% 适应"
    if view.mode == "fit_width":
        return f"{scale:.0f}% 适宽"
    if view.mode == "actual":
        return "100%"
    return f"{scale:.0f}%"


def format_status(
    info: ImageInfo,
    view: ViewTransform,
    viewport: ViewSize,
    *,
    frame: int = 0,
    image_size: ViewSize | None = None,
    shared: bool = False,
) -> StatusSnapshot:
    """把元信息与视图状态算成三段式状态栏文案（纯函数）。

    Args:
        info: 当前图片元信息（``service`` 给的事实）。
        view: 当前视图变换。
        viewport: 可视区尺寸。
        frame: 动图当前帧（0 基）。
        image_size: 渲染尺寸；默认取 ``info`` 的原始尺寸。
        shared: 这一帧是否直接来自共享缓存（状态栏的「缓存」标记）。
    """
    if not info.has_image:
        return StatusSnapshot()
    size = image_size if image_size is not None and image_size.valid else ViewSize.of(
        info.width, info.height
    )
    detail = f"{info.width}×{info.height} · {format_size(info.size_bytes)}"
    if info.frames > 1:
        detail += f" · 动图 {frame + 1}/{info.frames}帧"
    if shared:
        detail += " · 缓存"
    position = f"{info.index} / {info.total} · {format_zoom(view, size, viewport)}"
    return StatusSnapshot(info.name, detail, position)


def format_service_report(report: ServiceReport) -> str:
    """把共享缓存体检排成弹窗里的多行文本（纯函数）。

    信息密度按「先看角色、再看容量、最后看命中」排；末行留一句人话解释状态栏
    的「缓存」标记，让诊断结果自己说明该怎么读。
    """
    if report.note:
        return report.note
    blobs = report.blobs
    cache_line = (
        f"磁盘缓存：{blobs.count} 个 · {format_size(blobs.bytes)}"
        f"（上限 {format_size(blobs.limit_bytes)}）"
    )
    lines = [
        f"角色：{report.role}",
        f"服务进程：pid {report.pid or '-'}",
        f"连接：{report.backend}",
        "",
        cache_line,
        f"命中 {blobs.hits} 次 · 未命中 {blobs.misses} 次",
    ]
    rate = blobs.hit_rate
    if rate >= 0:
        lines.append(f"命中率：{rate}%")
    if report.decoded.entries:
        lines.append(f"解码缓存：{report.decoded.entries} 张 · {report.decoded.megapixels} MP")
    lines += [
        f"预加载：完成 {report.preload_done} · 排队 {report.pending}",
    ]
    if report.error:
        lines += ["", f"错误：{report.error}"]
    lines += ["", "状态栏出现「缓存」= 这一帧直接读自共享缓存，本窗口没有解码。"]
    return "\n".join(lines)
