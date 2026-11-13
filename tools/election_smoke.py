"""双进程竞选测试：两个进程同时启动，验证单实例锁与共享缓存协作。

跑法（干净数据目录）：
    IMAGEVIEWER_HOME=/tmp/x py -3.12 tools/election_smoke.py <图片路径>

输出应满足：恰好一个主进程、恰好一个窗口进程，且至少有一方命中共享缓存
（先算的那一方必然是 miss，后算的一方必须 hit）。
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CODE = r"""
import os, sys, time
sys.path.insert(0, "__ROOT__")
from viewer.service.client import default_link
link = default_link()
ok = link.acquire(budget=3.0)
status = link.status()
print(f"[{os.getpid()}] acquire={ok} role={status.get('role')} host={status.get('is_host')}")
time.sleep(1.2)                      # 给对手进程留出抢锁/连上的时间
m = link.call("show", path="__IMAGE__")
print(f"[{os.getpid()}] show hit={m['hit']} {m['dw']}x{m['dh']}")
time.sleep(0.8)
print(f"[{os.getpid()}] final={link.status().get('role')} recoveries={link.recoveries}")
"""


def main() -> int:
    image = sys.argv[1] if len(sys.argv) > 1 else ""
    if not os.path.isfile(image):
        print("请传入一个已存在的图片路径")
        return 2
    code = CODE.replace("__ROOT__", ROOT).replace("__IMAGE__", image.replace("\\", "\\\\"))

    env = os.environ.copy()
    env.setdefault("IMAGEVIEWER_HOME", os.path.join(ROOT, ".tmp-election"))
    procs = [subprocess.Popen([sys.executable, "-c", code], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
             for _ in range(2)]
    outputs = [p.communicate(timeout=60)[0] for p in procs]
    for out in outputs:
        print(out.strip())

    roles = []
    hits = []
    for out in outputs:
        for line in out.splitlines():
            if "role=" in line and "final=" not in line:
                roles.append(line.split("role=")[1].split()[0])
            if "show hit=" in line:
                hits.append("hit=True" in line)
    print("-" * 60)
    print(f"角色：{roles}")
    print(f"命中：{hits}")
    # 先算的一方 miss，另一方必须命中它算好的结果
    masters = sum("主进程" in role for role in roles)
    windows = sum("窗口进程" in role for role in roles)
    ok = masters == 1 and windows == 1 and any(hits)
    print("结果：" + ("通过（恰好一个主进程，另一方命中共享缓存）" if ok else "失败"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
