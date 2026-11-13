"""后台常驻主进程：共享缓存与预加载的唯一主人。

窗口进程（Flet/Flutter，一个窗口一个进程）一启动就会先找它，找不到就把它
拉起来 —— 因此**主进程是常态**，而不再是「第一个窗口兼职当宿主」。
它不创建窗口、不加载 Flutter 引擎，只是一个普通 CPython 进程（约 30MB），
却让所有窗口共享同一份解码结果和磁盘缓存。

两种用法：

1. 自动（推荐）：窗口启动时由 `viewer/service/spawn.py` 拉起，无窗口、无控制台；
   所有窗口关闭后仍常驻，空闲 `CSIV_MASTER_IDLE` 秒（默认 900）后自行退出。
2. 手动（开发/调试）：
       python -m viewer.daemon                  # 前台常驻，Ctrl+C 退出
       python -m viewer.daemon --detach         # 分离到后台
       python -m viewer.daemon --idle 600       # 无窗口连接超过 600 秒就退出
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time

from .service import paths
from .service.client import ServiceHost
from .service.runtime import Registry


def _setup_logging(verbose: bool) -> None:
    paths.ensure(paths.log_dir())
    log_file = paths.log_dir() / "service.log"
    handlers: list[logging.Handler] = [logging.FileHandler(log_file, encoding="utf-8")]
    if verbose:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def serve(idle_timeout: float = 0.0, verbose: bool = False) -> int:
    """抢锁并跑主进程；已有主进程时直接返回 0（幂等，可以随便重复执行）。"""
    registry = Registry()
    host = ServiceHost.try_start(registry)
    if host is None:
        info = registry.read()
        if info:
            logging.info("主进程已在运行：pid=%s port=%s", info.get("pid"), info.get("port"))
        else:
            logging.warning("无法获得单实例锁，主进程未启动")
        return 0

    log = logging.getLogger("csiv.daemon")
    log.info("主进程已启动 pid=%s port=%s 缓存=%s 空闲退出=%s秒",
             os.getpid(), host.server.port, host.service.blobs.root, idle_timeout or "永不")
    idle_since = time.monotonic()
    try:
        while True:
            time.sleep(1.0)
            if host.server.clients > 0:
                idle_since = time.monotonic()
            elif idle_timeout and time.monotonic() - idle_since > idle_timeout:
                log.info("已空闲 %.0f 秒，主进程退出", idle_timeout)
                break
            if int(time.monotonic() - idle_since) % 300 == 0:
                stats = host.service.op_stats()
                log.debug("缓存：%s", stats["blobs"])
    except KeyboardInterrupt:
        log.info("收到中断，退出")
    finally:
        host.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m viewer.daemon", description="ImageViewer 共享缓存主进程（常驻后台）"
    )
    parser.add_argument("--detach", action="store_true", help="分离到后台运行（无控制台窗口）")
    parser.add_argument("--idle", type=float, default=None, metavar="SECONDS",
                        help="没有任何窗口连接超过该秒数后退出（0 = 永不退出；"
                             "默认取环境变量 CSIV_MASTER_IDLE，未设置则永不退出）")
    parser.add_argument("-v", "--verbose", action="store_true", help="日志同时输出到控制台")
    args = parser.parse_args(argv)

    if args.detach:
        from .service import spawn

        return 0 if spawn.launch_daemon() else 1

    idle = args.idle
    if idle is None:
        try:
            idle = float(os.environ.get("CSIV_MASTER_IDLE", "0") or 0)
        except ValueError:
            idle = 0.0

    _setup_logging(args.verbose)
    return serve(idle_timeout=idle, verbose=args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
