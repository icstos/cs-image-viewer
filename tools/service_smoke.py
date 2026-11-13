"""共享缓存服务的无界面冒烟测试（不需要打开任何窗口）。

跑法：
    py -3.12 tools/service_smoke.py

覆盖的是重构后的关键承诺：
1. 首个请求未命中 -> 解码+编码+落盘；第二次请求命中且快得多；
2. **另一个进程**（用 socket 客户端模拟）直接命中同一份缓存 —— 共享成立；
3. 目录列表、图头探测、预加载都工作，预加载确实把 blob 提前写好；
4. `RemoteImageManager` 的对外接口与本地 `ImageManager` 一致（能出图、能翻页、
   能旋转、能读元信息），旋转走解码缓存不重新解码；
5. 宿主消失后客户端能接管（服务迁移），共享缓存继续可用；
6. 关闭共享（CSIV_SHARED=0）时完整降级到本地解码。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

# 必须在导入 viewer 之前设定独立的数据目录，避免污染真实用户缓存
_TMP_HOME = Path(tempfile.mkdtemp(prefix="csiv-smoke-home-"))
os.environ["IMAGEVIEWER_HOME"] = str(_TMP_HOME)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from viewer.service.client import ServiceClient, ServiceHost, ServiceLink  # noqa: E402
from viewer.service.runtime import Registry  # noqa: E402
from viewer.shared_manager import RemoteImageManager  # noqa: E402

PASSED: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "OK  " if condition else "FAIL"
    print(f"[{mark}] {label}{(' — ' + detail) if detail else ''}")
    if not condition:
        raise AssertionError(label)
    PASSED.append(label)


def make_images(folder: Path) -> dict[str, Path]:
    """造几张有代表性的测试图：大照片、带透明的 PNG、动图。"""
    folder.mkdir(parents=True, exist_ok=True)
    big = folder / "big.jpg"
    im = Image.new("RGB", (3200, 2400))
    pixels = im.load()
    for y in range(0, 2400, 4):
        for x in range(0, 3200, 4):
            color = ((x * 7) % 256, (y * 5) % 256, ((x + y) * 3) % 256)
            for dy in range(4):
                for dx in range(4):
                    pixels[x + dx, y + dy] = color
    im.save(big, quality=92)

    alpha = folder / "alpha.png"
    rgba = Image.new("RGBA", (640, 480), (0, 0, 0, 0))
    for i in range(0, 640, 20):
        for j in range(0, 480, 20):
            rgba.paste((i % 256, j % 256, 200, 255), (i, j, i + 20, j + 20))
    rgba.save(alpha)

    anim = folder / "anim.gif"
    frames = [
        Image.new("RGB", (320, 240), (30 * i % 256, 90, 160)) for i in range(6)
    ]
    frames[0].save(anim, save_all=True, append_images=frames[1:], duration=90, loop=0)
    return {"big": big, "alpha": alpha, "anim": anim}


def main() -> int:
    print(f"临时数据目录：{_TMP_HOME}")
    images = make_images(_TMP_HOME / "pics")
    big, alpha, anim = images["big"], images["alpha"], images["anim"]

    # ---------- 1. 成为宿主 ----------
    host = ServiceHost.try_start()
    check("成为宿主（抢到单实例锁）", host is not None)
    assert host is not None
    check("第二个宿主被拒绝（单实例）", ServiceHost.try_start() is None)
    registry = Registry()
    info = registry.read()
    check("注册表已写入端口与令牌", bool(info and info.get("port") and info.get("token")),
          f"port={info.get('port') if info else '-'}")

    # ---------- 2. 首个请求未命中 ----------
    t0 = time.perf_counter()
    first = host.service.op_show(str(big))
    cold_ms = (time.perf_counter() - t0) * 1000
    check("首次渲染未命中缓存", first["hit"] is False, f"{cold_ms:.0f}ms")
    check("blob 已落盘", Path(first["blob"]).is_file(),
          f"{Path(first['blob']).stat().st_size // 1024}KB")
    check("原图尺寸正确", (first["w"], first["h"]) == (3200, 2400),
          f"{first['w']}x{first['h']}")

    t0 = time.perf_counter()
    warm = host.service.op_show(str(big))
    warm_ms = (time.perf_counter() - t0) * 1000
    check("第二次命中缓存", warm["hit"] is True, f"{warm_ms:.1f}ms")
    check("命中比解码快一个数量级", warm_ms * 5 < cold_ms,
          f"{cold_ms:.0f}ms -> {warm_ms:.1f}ms")
    check("同一个 blob 文件", warm["blob"] == first["blob"])

    # ---------- 3. 另一个进程（独立 socket）共享同一份缓存 ----------
    client = ServiceClient(info["port"], info["token"])
    t0 = time.perf_counter()
    shared = client.call("show", path=str(big))
    shared_ms = (time.perf_counter() - t0) * 1000
    check("第二个客户端命中同一份缓存", shared["hit"] is True, f"{shared_ms:.1f}ms 往返")
    check("跨进程命中 < 30ms", shared_ms < 30, f"{shared_ms:.1f}ms")
    check("客户端知道宿主 pid", client.pid == host.service.op_stats()["pid"])

    # ---------- 4. 目录列表 / 图头探测 ----------
    t0 = time.perf_counter()
    listing = client.call("list", folder=str(big.parent))
    list_ms = (time.perf_counter() - t0) * 1000
    check("列出目录图片", len(listing["paths"]) == 3, f"{len(listing['paths'])} 张, {list_ms:.1f}ms")
    t0 = time.perf_counter()
    again = client.call("list", folder=str(big.parent))
    check("目录索引第二次走缓存", again["cached"] is True,
          f"{(time.perf_counter() - t0) * 1000:.2f}ms")

    probed = client.call("probe", path=str(anim))
    check("探测动图帧数", probed["frames"] == 6, f"frames={probed['frames']}")
    bad = Path(_TMP_HOME / "pics" / "broken.jpg")
    bad.write_bytes(b"not an image")
    bad_probe = client.call("probe", path=str(bad))
    check("坏图被识别而不是让服务崩掉", bad_probe["ok"] is False, bad_probe.get("error", ""))

    # ---------- 5. 预加载 ----------
    queued = client.call("preload", paths=[str(alpha)])
    check("预加载请求被接受", queued["queued"] == 1, str(queued))
    deadline = time.time() + 10
    done = 0
    while time.time() < deadline:
        done = client.call("stats")["counters"].get("preload_done", 0)
        if done >= 1:
            break
        time.sleep(0.05)
    check("预加载在后台完成", done >= 1, f"preload_done={done}")
    alpha_show = client.call("show", path=str(alpha))
    check("预加载把图提前渲染好", alpha_show["hit"] is True,
          f"hit={alpha_show['hit']}, {alpha_show['dw']}x{alpha_show['dh']}")

    # ---------- 6. RemoteImageManager 端到端 ----------
    link = ServiceLink()
    check("ServiceLink 连上宿主", link.acquire(budget=1.0) is True)
    manager = RemoteImageManager(link)
    check("打开单张图片", manager.open_path(big) is True)
    check("补全同目录列表", manager.expand_folder() is True, f"{len(manager.files)} 张")
    index_big = manager.index

    uri, w, h = manager.get_display()
    check("拿到 data URI", bool(uri and uri.startswith("data:image/")), f"{w}x{h}")
    check("尺寸与渲染结果一致", (w, h) == (3200, 2400))
    check("标记为共享缓存命中", manager.last_shared_hit is True)
    info_text = manager.info()
    check("元信息来自服务（未本地解码）",
          info_text["w"] == 3200 and info_text["size"] > 0 and info_text["total"] == 3,
          f"{info_text}")

    decoded_before = link.call("stats")["decoded"]
    manager.rotate_cw()
    t0 = time.perf_counter()
    uri_rot, rw, rh = manager.get_display()
    rot_ms = (time.perf_counter() - t0) * 1000
    decoded_after = link.call("stats")["decoded"]
    check("旋转后重新渲染", uri_rot != uri and (rw, rh) == (2400, 3200),
          f"{rw}x{rh}, {rot_ms:.0f}ms")
    check("旋转复用解码缓存（没有重新解码像素）",
          decoded_after["hits"] > decoded_before["hits"],
          f"decoded hits {decoded_before['hits']} -> {decoded_after['hits']}, {rot_ms:.0f}ms")

    manager.reset_transform()
    check("翻到下一张", manager.navigate(1) is True)
    nxt, _, _ = manager.get_display()
    check("下一张能显示", bool(nxt))
    manager.index = index_big

    anim_manager = RemoteImageManager(link)
    anim_manager.open_path(anim)
    check("动图帧数来自服务", anim_manager.frame_count() == 6, str(anim_manager.frame_count()))
    anim_uri, aw, ah = anim_manager.get_display()
    check("动图首帧可显示（本地解码路径）", bool(anim_uri) and (aw, ah) == (320, 240))

    # ---------- 7. 导出 ----------
    out = _TMP_HOME / "export.png"
    manager.reset_transform()
    manager.open_path(alpha)
    manager.save_current_frame(out)
    check("导出当前帧", out.is_file() and out.stat().st_size > 0,
          f"{out.stat().st_size // 1024}KB")

    # ---------- 8. 宿主消失 -> 客户端接管（服务迁移） ----------
    host.stop()
    client.close()
    survivor = RemoteImageManager(link)          # 全新窗口：没有任何本地显示缓存
    survivor.open_path(big)
    t0 = time.perf_counter()
    migrated = survivor.get_display()
    migrate_ms = (time.perf_counter() - t0) * 1000
    check("宿主退出后新窗口仍能出图（已接管）", bool(migrated[0]), f"{migrate_ms:.0f}ms")
    check("确实发生了接管", link.recoveries >= 1, f"recoveries={link.recoveries}")
    stats_after = link.status()
    check("接管后角色为宿主", stats_after.get("is_host") is True, str(stats_after.get("role")))
    check("接管后仍是命中（磁盘缓存在）", survivor.last_shared_hit is True)

    # ---------- 9. 关闭共享 -> 完全降级 ----------
    local_manager = RemoteImageManager(ServiceLink(enabled=False))
    local_manager.open_path(big)
    local_uri, lw, lh = local_manager.get_display()
    check("关闭共享后本地解码可用", bool(local_uri) and (lw, lh) == (3200, 2400))
    check("本地模式不标记缓存命中", local_manager.last_shared_hit is False)

    link.close()
    print(f"\n全部通过：{len(PASSED)} 项")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    raise SystemExit(code)
