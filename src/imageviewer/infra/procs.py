"""拉起进程：后台主进程（共享缓存服务）与窗口子进程。

进程模型
--------
    后台主进程（无窗口、不加载 Flutter，约 30MB）
      ├── 窗口 #1   只发请求：取显示数据 / 目录列表 / 预加载
      ├── 窗口 #2
      └── 窗口 #N

为什么必须这样分：Flet 1.0 没有多窗口 API，**一个窗口就是一个 Flutter 进程**，
所以窗口本身没法更轻；但「解码 + 变换 + 编码 + 预加载」这一摊重活可以整体挪到
主进程里。主进程只是一个普通 CPython 进程（不创建窗口、不加载 Flutter 引擎），
内存大约是 Flutter 进程的 1/5，却能让所有窗口共享同一份解码结果与磁盘缓存。
这正是「主进程 + 多个窗口进程」，而不是「每开一张图就起一个各干各的独立进程」。

打包态的难点：产物里没有 python.exe（Python 被嵌进 Flutter 壳里）。
但 flet 的产物目录其实已经带齐了运行时（python312.dll + Lib + DLLs +
site-packages），只缺一个启动器，因此随包放一份 python.exe（约 100KB），
首次运行时复制到应用目录即可 —— 之后就能用**同一个运行时**拉起真正的无窗口主进程。
复制或启动失败时（例如装在只读目录），自动退回「第一个窗口兼职宿主」的老方案，
功能完全不受影响，只是主进程不再独立。

子进程一律**分离启动**（不继承控制台、不阻塞父进程、父进程退出后继续存活）：
关掉所有窗口后主进程仍在（空闲一段时间后自行退出，见 `CSIV_MASTER_IDLE`）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

OPEN_ENV = "IMAGEVIEWER_OPEN"          # 与 association.py / main.py 保持一致
ROLE_ENV = "IMAGEVIEWER_ROLE"          # 进程角色：window / daemon
IDLE_ENV = "CSIV_MASTER_IDLE"          # 主进程空闲多少秒后退出（0 = 永不退出）
DEFAULT_IDLE = 900.0                   # 15 分钟：所有窗口关掉后回收那 30MB

# 随包携带的解释器启动器（打包态用它拉起无窗口主进程）
RUNTIME_ASSET = ("assets", "runtime", "python.exe")

# Windows 进程创建标志
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000   # 脱离父进程所在的 Job（否则可能被连带杀掉）

_runtime_cache: dict[str, str | None] = {}


# ---------------------------------------------------------------------------
# 运行时探测
# ---------------------------------------------------------------------------


def app_root() -> Path:
    """应用根目录：打包态是 exe 所在目录；开发态是仓库根目录。"""
    if python_exe():
        return main_script().parent
    return Path(sys.executable).resolve().parent


def main_script() -> Path:
    """应用代码目录（开发态 = 仓库根；打包态 = `<root>/app`）。

    路径统一由 ``_paths.repo_root()`` 给出：src 布局下本文件多了一层
    ``src/``，按 ``__file__`` 数层会静默指到 ``src/`` 而非仓库根。
    """
    from .._paths import repo_root

    return repo_root()


def python_exe() -> str | None:
    """开发态的解释器路径；打包态返回 None（此时只能用应用自带的运行时）。"""
    exe = Path(sys.executable)
    if exe.name.lower().startswith("python") and exe.is_file():
        return str(exe)
    return None


def runtime_python() -> str | None:
    """可以跑「无窗口主进程」的解释器路径；没有则返回 None。

    开发态直接用当前解释器；打包态用应用目录里的 python.exe（首次从随包资源
    落地一份）。结果按应用根目录缓存，避免每次开窗口都去探测一次。
    """
    key = str(app_root())
    if key in _runtime_cache:
        return _runtime_cache[key]
    found = _resolve_runtime(app_root())
    _runtime_cache[key] = found
    return found


def _resolve_runtime(root: Path) -> str | None:
    py = python_exe()
    if py:
        return py
    target = root / "python.exe"
    if target.is_file() and _runtime_ok(target):
        return str(target)
    return _provision_runtime(root, target)


def _provision_runtime(root: Path, target: Path) -> str | None:
    """把随包的 python.exe 复制到应用目录（幂等）；失败返回 None。

    python.exe 是**启动器**而不是运行时本身：它和同一目录的 python312.dll、
    Lib/、DLLs/ 组成一个完整的解释器 —— 这些 flet 打包时早就带上了。
    """
    src = main_script().joinpath(*RUNTIME_ASSET)
    if not src.is_file():
        return None
    tmp = target.with_name(target.name + ".part")
    try:
        shutil.copyfile(src, tmp)
        if os.name != "nt":
            os.chmod(tmp, 0o755)
        os.replace(tmp, target)          # 原子替换：并发启动的窗口不会读到半个文件
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return None
    return str(target) if _runtime_ok(target) else None


def _runtime_ok(py: Path) -> bool:
    """真跑一次 python -c 确认它可用（DLL 版本不匹配时这里就会失败）。"""
    flags = _CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        done = subprocess.run(                     # noqa: S603 —— 参数完全由本程序构造
            [str(py), "-E", "-s", "-c", "import sys"],
            cwd=str(py.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            creationflags=flags,
            check=False,                           # 可用性看「有没有抛异常」，不看返回码
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------


def _base_command() -> list[str]:
    py = python_exe()
    if py:
        return [py, str(main_script() / "main.py")]
    return [str(sys.executable)]


def _popen(
    command: list[str],
    env: dict[str, str],
    hidden: bool,
    cwd: str | None = None,
    breakaway: bool = False,
) -> bool:
    """分离启动一个子进程；返回是否成功拉起。

    `breakaway=True`（只给常驻主进程用）：从父进程所在的 Job 里脱离，
    这样即使父进程是终端/IDE 拉起的、乃至父进程整体被结束，主进程也不会被
    连坐；Job 不允许 breakaway 时（少数场景会返回 ERROR_ACCESS_DENIED）
    自动退回不带该标志的方式，绝不能因此起不来。
    """
    flags = 0
    if os.name == "nt":
        flags = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
        if hidden:
            flags |= _CREATE_NO_WINDOW
        if breakaway:
            flags |= _CREATE_BREAKAWAY_FROM_JOB

    def _spawn(creationflags: int) -> bool:
        try:
            subprocess.Popen(                       # noqa: S603 —— 命令完全由本程序构造
                command,
                env=env,
                cwd=cwd or str(main_script()),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                creationflags=creationflags,
            )
            return True
        except OSError:
            return False

    if _spawn(flags):
        return True
    if breakaway and os.name == "nt":
        return _spawn(flags & ~_CREATE_BREAKAWAY_FROM_JOB)
    return False


def launch_window(path: str | os.PathLike | None = None) -> bool:
    """新开一个看图窗口（独立进程，连回主进程共享缓存）。"""
    env = os.environ.copy()
    env.pop(ROLE_ENV, None)
    env.pop(OPEN_ENV, None)                         # 不把上一次的打开请求泄漏给新窗口
    if python_exe():
        command = _base_command()
        if path:
            command.append(str(path))
        return _popen(command, env, hidden=False)
    if path:                                        # 打包态：只能走环境变量通道
        env[OPEN_ENV] = str(path)
    return _popen(_base_command(), env, hidden=False)


def daemon_command(py: str) -> tuple[list[str], str]:
    """构造「无窗口主进程」的启动命令，返回 (argv, cwd)。

    开发态：用当前解释器跑一段引导代码把 ``src/`` 加进 sys.path 再启动 daemon
    （cwd = 仓库根）。子进程不继承父进程的 sys.path，所以不能用 ``-m``。
    打包态：用应用目录里的 python.exe 跑一段引导代码 —— 产物里的模块是 .pyc，
    路径要显式塞进 sys.path（app/ 是应用代码，site-packages/ 是第三方依赖）。
    用 `-E -s` 隔离外部 PYTHONPATH / 用户 site，保证加载的就是本包内的东西。
    """
    if python_exe():
        # src 布局：子进程不继承父进程在 main.py 里插入的 sys.path，直接用
        # `-m imageviewer...` 在未做 editable install 的开发态会找不到包。
        # 显式把 src/ 加进子进程路径，功能与安装态完全一致。
        src_dir = main_script() / "src"
        code = (
            "import sys;"
            f"sys.path.insert(0, {str(src_dir)!r});"
            "from imageviewer.infra.shared.daemon import main;"
            "raise SystemExit(main())"
        )
        return [py, "-c", code], str(main_script())
    root = Path(py).resolve().parent
    app_dir = main_script()
    site = root / "site-packages"
    code = (
        "import sys;"
        f"sys.path.insert(0, {str(app_dir)!r});"
        f"sys.path.insert(1, {str(site)!r});"
        "from imageviewer.infra.shared.daemon import main;"
        "raise SystemExit(main())"
    )
    return [py, "-E", "-s", "-c", code], str(app_dir)


def launch_daemon(idle: float | None = None) -> bool:
    """拉起**后台常驻主进程**：无窗口、不加载 Flutter，只跑共享缓存服务。

    可以随便重复调用（服务内部靠单实例锁去重，多余进程会立刻退出）。
    """
    py = runtime_python()
    if py is None:
        return False
    argv, cwd = daemon_command(py)
    env = os.environ.copy()
    env[ROLE_ENV] = "daemon"
    env.pop(OPEN_ENV, None)                         # 主进程不需要打开任何文件
    env.setdefault(IDLE_ENV, str(int(DEFAULT_IDLE if idle is None else idle)))
    return _popen(argv, env, hidden=True, cwd=cwd, breakaway=True)


def service_available() -> bool:
    """能否由一个**独立的无窗口进程**提供共享服务（开发态与打包态都为真）。"""
    return runtime_python() is not None


class WindowLauncher:
    """实现 ``service.ports.ProcessLauncher``：一个路径一个新窗口进程。

    返回值是「真正拉起来了几个」：批量打开时中间某个失败不该让整批回执变成
    一句含糊的「失败」，成功几个就报几个。
    """

    def launch(self, paths: Sequence[Path]) -> int:
        return sum(1 for path in paths if launch_window(path))
