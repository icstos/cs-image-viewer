"""后台常驻主进程：共享缓存与预加载的唯一主人。

窗口进程（Flet/Flutter，一个窗口一个进程）一启动就会先找它，找不到就把它
拉起来 —— 因此**主进程是常态**，而不再是「第一个窗口兼职当宿主」。
它不创建窗口、不加载 Flutter 引擎，只是一个普通 CPython 进程（约 30MB），
却让所有窗口共享同一份解码结果和磁盘缓存。

两种用法：

1. 自动（推荐）：窗口启动时由 ``cs_image_viewer/infra/procs.py`` 拉起，无窗口、无控制台；
   所有窗口关闭后仍常驻，空闲 `CSIV_MASTER_IDLE` 秒（默认 900）后自行退出。
2. 手动（开发/调试）：
       python -m cs_image_viewer.infra.shared.daemon            # 前台常驻，Ctrl+C 退出
       python -m cs_image_viewer.infra.shared.daemon --detach   # 分离到后台
       python -m cs_image_viewer.infra.shared.daemon --idle 600 # 无窗口连接超过 600 秒就退出
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time

from .. import dispatch, paths, procs
from .client import ServiceHost
from .runtime import Registry

# 预热窗口池维护（双击极速响应的关键）
HOT_POOL_ENV = "CSIV_HOT_POOL"         # 目标空闲预热窗口数；0 关闭
HOT_DEFAULT_POOL = 1                   # 默认维持 1 个隐藏预热窗口
HOT_RELAUNCH_COOLDOWN = 4.0            # 秒：两次拉起的最小间隔（等 idle 标记建立）


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
    try:
        pool = int(os.environ.get(HOT_POOL_ENV, str(HOT_DEFAULT_POOL)) or 0)
    except ValueError:
        pool = HOT_DEFAULT_POOL
    idle_since = time.monotonic()
    last_hot_launch = 0.0
    if pool > 0:
        log.info("预热窗口池已启用（目标 %d 个空闲窗口）", pool)
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
            # 维持预热池：窗口转正后会清空 idle 标记并投递 replenish，这里补位。
            # 冷却时间避免 idle 标记尚未建立时重复拉起多个窗口。
            if pool > 0 and time.monotonic() - last_hot_launch >= HOT_RELAUNCH_COOLDOWN:
                # 两个条件都要独立求值（不能用 or 短路吞掉 take_replenish 的消费动作）
                need_replenish = dispatch.take_replenish()
                no_idle = dispatch.idle_owner() is None
                if need_replenish or no_idle:
                    if procs.launch_hot_window():
                        last_hot_launch = time.monotonic()
                        log.info("已拉起一个预热窗口")
                    else:
                        log.warning("预热窗口拉起失败（打包态可能未配置 exe 路径），"
                                    "退回冷启动；不影响功能")
                        dispatch.take_replenish()   # 清掉标记，避免每轮刷日志
                        time.sleep(9)               # 失败时拉长重试间隔
    except KeyboardInterrupt:
        log.info("收到中断，退出")
    finally:
        host.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m cs_image_viewer.infra.shared.daemon",
        description="ImageViewer 共享缓存主进程（常驻后台）",
    )
    parser.add_argument("--detach", action="store_true", help="分离到后台运行（无控制台窗口）")
    parser.add_argument("--idle", type=float, default=None, metavar="SECONDS",
                        help="没有任何窗口连接超过该秒数后退出（0 = 永不退出；"
                             "默认取环境变量 CSIV_MASTER_IDLE，未设置则永不退出）")
    parser.add_argument("-v", "--verbose", action="store_true", help="日志同时输出到控制台")
    args = parser.parse_args(argv)

    if args.detach:
        from .. import procs

        return 0 if procs.launch_daemon() else 1

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
