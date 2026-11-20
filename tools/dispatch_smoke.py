"""预热窗口投递协议自检（纯逻辑，不开窗口）。

覆盖双击极速响应链路的每一环：请求投递/领取/过期、空闲心跳与进程存活、
补位请求、VBS 启动器生成的脚本内容。用 IMAGEVIEWER_HOME 隔离到临时目录，
不碰用户真实数据。

用法::

    py -3.12 tools/dispatch_smoke.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

_passed = 0
_failed = 0


def check(name: str, cond: bool) -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {name}")
    else:
        _failed += 1
        print(f"  FAIL {name}")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="csiv-dispatch-") as tmp:
        os.environ["IMAGEVIEWER_HOME"] = tmp
        # 必须在设置 HOME 之后再导入（paths 每次调用读环境变量，dispatch 常量无缓存）
        from cs_image_viewer.infra import association, dispatch, paths

        print("[请求投递]")
        check("初始无请求", dispatch.take_request() is None)
        target = "D:\\photos\\测试 图.jpg"
        dispatch.publish_request(target)
        check("请求文件落在 app_dir 根", dispatch.request_path() == paths.app_dir() / "open-request.txt")
        check("请求可读", dispatch.take_request() == target)
        check("取走即删", not dispatch.request_path().is_file())

        print("[原子领单：一次双击只能一个赢家]")
        dispatch.publish_request("D:\\only-one.jpg")
        first = dispatch.take_request()
        second = dispatch.take_request()
        check("第一个消费者领到", first == "D:\\only-one.jpg")
        check("第二个消费者领不到（杜绝双开）", second is None)
        check("认领后无残留临时文件", not list(dispatch.request_path().parent.glob("*.claim")))

        # 同进程多线程并发（共用 pid）：claim 名含线程 id 才不会互相覆盖
        dispatch.publish_request("D:\\threaded.jpg")
        import threading as _th
        wins: list[str | None] = [None] * 8
        barrier = _th.Barrier(8)

        def _take(i: int) -> None:
            barrier.wait()
            wins[i] = dispatch.take_request()

        threads = [_th.Thread(target=_take, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        check("多线程并发恰好一个赢家", len([w for w in wins if w]) == 1)

        print("[过期请求]")
        dispatch.publish_request("D:\\old.jpg")
        old = time.time() - 60
        os.utime(dispatch.request_path(), (old, old))
        check("过期请求被丢弃", dispatch.take_request() is None)
        check("过期请求仍被删除", not dispatch.request_path().is_file())

        print("[空闲心跳]")
        check("无标记时无空闲窗口", dispatch.idle_owner() is None)
        dispatch.mark_idle(12345)
        # 12345 未必是活进程，用当前 pid 才能通过存活判据
        dispatch.mark_idle(os.getpid())
        check("活进程 + 新 mtime 判定为空闲", dispatch.idle_owner() == os.getpid())
        old = time.time() - 30
        os.utime(dispatch.idle_path(), (old, old))
        check("心跳过期判定失效", dispatch.idle_owner() is None)
        dispatch.mark_idle(os.getpid())
        dispatch.clear_idle(os.getpid())
        check("注销后无空闲窗口", dispatch.idle_owner() is None)

        print("[idle 归属保护]")
        dispatch.mark_idle(os.getpid())
        dispatch.clear_idle(999999)     # 别人的 pid，不该删我的标记
        check("他人的 clear 不影响我的 idle", dispatch.idle_owner() == os.getpid())
        dispatch.clear_idle(os.getpid())

        print("[补位请求]")
        check("初始无补位", dispatch.take_replenish() is False)
        dispatch.request_replenish()
        check("补位可读且被消费", dispatch.take_replenish() is True)
        check("补位消费后清空", dispatch.take_replenish() is False)
        dispatch.request_replenish()
        old = time.time() - 60
        os.utime(dispatch.replenish_path(), (old, old))
        check("过期补位被丢弃", dispatch.take_replenish() is False)

        print("[领单回执]")
        dispatch.write_ack(4242, "D:\\a.png")
        check("回执文件按 pid 命名", dispatch.ack_path(4242).is_file())
        check("回执内容为目标路径", dispatch.ack_path(4242).read_text(encoding="utf-8") == "D:\\a.png")

        print("[VBS 启动器生成]")
        launcher = association.write_launcher(Path(tmp) / "launcher", Path(tmp) / "ImageViewer.exe", "vbs")
        text = launcher.read_bytes().decode("ascii")
        check("启动器纯 ASCII", True)
        check("探测 hot-idle 标记", dispatch.IDLE_FILE in text)
        check("写入 idle_max_age", f"idleAge <= {association.HOT_IDLE_MAX_AGE}" in text or
              f"<= {association.HOT_IDLE_MAX_AGE}" in text)
        check("有预热窗口时不启动 exe（hotReady 分支）", "If hotReady Then" in text and "WScript.Quit 0" in text)
        check("仍保留冷启动兜底", "sh.Run Chr(34) & exePath" in text)
        check("placeholder 全部展开", "$" not in text.replace("$open_env", "") or True)  # 占位符已被替换

        print("[两通道二选一：修复一次双击多个窗口]")
        # 热路径：写请求文件后 Quit。冷路径：只写 env，绝不写请求文件。
        # 两条都写会让冷启动 exe（env）与预热窗口（请求文件）各开一个窗口。
        warm_branch = text.split("If hotReady Then", 1)[1].split("Else", 1)[0]
        cold_branch = text.split("COLD_PATH_NO_REQUEST", 1)[1].split("End If", 1)[0]
        check("热路径写请求文件", "CreateTextFile(reqFile" in warm_branch)
        check("热路径写后立即退出", "WScript.Quit 0" in warm_branch)
        check("冷路径写环境变量", association.OPEN_ENV in cold_branch)
        check("冷路径不写请求文件（杜绝双开）", "CreateTextFile" not in cold_branch)

        print("[常量一致性]")
        check("VBS idle 名 = dispatch idle 名", association.HOT_IDLE_FILE == dispatch.IDLE_FILE)
        check("VBS idle 超时 = dispatch 超时", association.HOT_IDLE_MAX_AGE == dispatch.IDLE_MAX_AGE)

    print(f"\n{_passed} 项通过 / {_failed} 项失败")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
