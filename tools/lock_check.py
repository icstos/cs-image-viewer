"""单实例锁互斥验证：三个进程同时抢同一把锁，必须只有一个成功。"""
from __future__ import annotations

import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHILD = r"""
import os, sys, time
sys.path.insert(0, __ROOT__)
from viewer.service.runtime import SingletonLock
lock = SingletonLock(__LOCK__)
got = lock.acquire()
print(f"pid={os.getpid()} got={got}", flush=True)
time.sleep(2.5)
"""


def main() -> int:
    lock_path = os.path.join(ROOT, ".tmp-lock", "service.lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    for name in os.listdir(os.path.dirname(lock_path)):
        os.unlink(os.path.join(os.path.dirname(lock_path), name))

    code = CHILD.replace("__ROOT__", repr(ROOT).replace("'", '"')).replace(
        "__LOCK__", repr(lock_path).replace("'", '"')
    )
    procs = [subprocess.Popen([sys.executable, "-c", code]) for _ in range(3)]
    time.sleep(0.2)
    procs.append(subprocess.Popen([sys.executable, "-c", code]))
    for p in procs:
        p.wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
