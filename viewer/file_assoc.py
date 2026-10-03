"""Windows 文件关联（“打开方式”）注册工具。

为什么不能直接用 `"LiteView.exe" "%1"`
----------------------------------------------------
`flet build` 产出的 exe 不是普通的 Python 打包程序，而是 Flutter/Dart 外壳 +
内嵌 CPython。它的 Dart 入口会把**任何命令行参数**当成“开发模式”的页面地址::

    // build/flutter/lib/main.dart（flet 构建模板）
    } else if (_args.isNotEmpty && isDesktopPlatform()) {
        pageUrl = _args[0];            // 开发模式：去连接这个 URL
    }

实测 `LiteView.exe D:\\a.jpg`：Python 端一行都不会执行（main.py 没跑），窗口停在
启动页且进程永不退出。也就是说 Windows 标准的 `"app.exe" "%1"` 关联写法在这里
**必然失败**。

解决办法（本模块）
----------------------------------------------------
关联命令指向一个“启动器”，由它把文件路径写进环境变量 `LITEVIEW_OPEN`
（并额外落一份短时效请求文件作为兜底），再以 **无参数**方式启动 exe
（无参数 ⇒ Dart 走生产模式 ⇒ 内嵌 Python 正常启动）。应用侧
`main.py::_resolve_start_path()` 按 env → 请求文件 → 命令行参数 的顺序解析。

默认启动器是 wscript 承载的 `.vbs`：无控制台闪窗、零额外依赖、纯 ASCII 内容；
若系统禁用了 VBScript，可用 `--launcher cmd` 换成 `.cmd`（会有一次控制台闪窗）。

**exe 叫什么名字都可以**：启动器脚本里不出现 exe 名称，exe 全路径只写进同目录的
`app-exe.txt`（vbs 模式 = UTF-16，cmd 模式 = UTF-8 + `chcp 65001`），因此
`图片查看器.exe` 这类含非 ASCII 字符的名字同样可用。

启动器落在 `%LOCALAPPDATA%\\LiteView`（稳定目录）—— 因此 `flet build` 重建
`build/` 之后关联依然有效，无需重新注册；只有**移动或改名了 exe** 才需要重跑
一次 `install`。

注册范围（全部写在 HKCU，**不需要管理员权限**）
----------------------------------------------------
* ProgID `LiteView.Image` + `shell\\open\\command` → 启动器
* 每个扩展名的 `OpenWithProgids` → 出现在右键“打开方式”列表
* `Applications\\<exe>` → 出现在“选择其他应用”列表
* `Capabilities` + `RegisteredApplications` → 出现在系统“默认应用”设置页
* 右键菜单：图片文件 / 文件夹（含文件夹空白处）追加“用 LiteView 打开”

用法::

    py -3.12 -m viewer.file_assoc install              # 注册（自动探测 build/windows）
    py -3.12 -m viewer.file_assoc install --exe "D:\\app\\LiteView.exe"
    py -3.12 -m viewer.file_assoc install --exe "build\\windows\\图片查看器.exe"  # 中文名同样可用
    py -3.12 -m viewer.file_assoc install --set-default # 同时把默认打开方式设为 LiteView
    py -3.12 -m viewer.file_assoc status                # 查看当前关联状态
    py -3.12 -m viewer.file_assoc uninstall             # 全部撤销

注意：Windows 10/11 的默认打开方式由 `UserChoice`（带校验哈希）保护，任何程序都
无法可靠地“静默改默认”。因此 `install` 只是把 LiteView 注册进“打开方式”列表，
用户点一次“始终使用此应用”即可；这正是系统推荐的交互。
"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import os
import sys
from pathlib import Path
from string import Template

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
OPEN_ENV = "LITEVIEW_OPEN"  # 启动器 → 应用 的环境变量通道（与 main.py 保持一致）
LAUNCHER_DIR_NAME = "LiteView"  # %LOCALAPPDATA%\LiteView：启动器与请求文件的稳定落点
REQUEST_FILE = "open-request.txt"  # 启动器 → 应用 的兜底请求文件（短时效）
PROGID = "LiteView.Image"
FRIENDLY_NAME = "LiteView 看图"
TYPE_NAME = "LiteView 图片"
APP_DESCRIPTION = "轻量、键盘友好的看图工具（IrfanView 风格）"
CAPABILITIES_KEY = r"Software\LiteView\Capabilities"
CLASSES = r"Software\Classes"

LAUNCHER_VBS = "liteview-open.vbs"
LAUNCHER_CMD = "liteview-open.cmd"
EXE_CONFIG = "app-exe.txt"  # 记录 exe 全路径：vbs 模式用 UTF-16，cmd 模式用系统 ANSI

# 打包产物可能的文件名（pyproject 的 [tool.flet.windows].artifact 决定）
EXE_CANDIDATES = ("LiteView.exe", "cs-image-viewer.exe", "liteview.exe")

# 兜底扩展名列表（正常路径下从 viewer.image_manager.IMAGE_EXTS 读取，保持单一来源）
_FALLBACK_EXTS = (
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".bmp", ".gif", ".webp",
    ".tif", ".tiff", ".psd", ".svg", ".heic", ".heif",
    ".cr2", ".cr3", ".nef", ".nrw", ".arw", ".srf", ".dng", ".raf", ".orf",
    ".rw2", ".pef", ".srw", ".erf", ".3fr", ".mef", ".mrw", ".iiq", ".kdc",
    ".raw", ".x3f",
)


# ---------------------------------------------------------------------------
# 启动器模板
#   * 内容保持纯 ASCII，且**不嵌入 exe 名称**（exe 路径走 app-exe.txt），
#     因此 exe 名可以是中文等非 ASCII 字符；
#   * 用 string.Template 而非 str.format：模板里含 Dart 花括号代码片段。
# ---------------------------------------------------------------------------
VBS_TEMPLATE = """' LiteView -- "Open with" launcher (auto-generated, do not edit)
'
' Why this file exists: the exe built by `flet build` is a Flutter/Dart shell
' whose Dart entrypoint treats ANY command-line argument as a "developer mode"
' page URL:
'     if (_args.isNotEmpty && isDesktopPlatform()) { pageUrl = _args[0]; }
' So `<exe> "D:\\a.jpg"` never starts the embedded Python program (the window
' just hangs on the boot screen). Windows file associations can only forward the
' selected file on the command line, so this launcher hands it over through the
' $open_env environment variable (plus a short-lived request file as a fallback)
' and starts the exe with NO arguments -- no arguments means production mode,
' which is the only mode where Python actually runs.
'
' The application path is read from $exe_config instead of being embedded below:
' that keeps this script pure ASCII, so the exe may carry any name -- including
' non-ASCII ones such as Chinese. TristateTrue (-1) makes WSH decode that file
' as UTF-16, which round-trips any Windows path losslessly.
'
' This file lives in a stable folder (%LOCALAPPDATA%\\$launcher_dir) rather than
' inside the build output, so rebuilding the app does not break the association.
' Refresh the recorded path with:
'     py -3.12 -m viewer.file_assoc install
Option Explicit

Dim fso, sh, env, target, here, cfgPath, exePath, reqDir, reqFile, stream

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")

target = ""
If WScript.Arguments.Count >= 1 Then target = WScript.Arguments(0)

here    = fso.GetParentFolderName(WScript.ScriptFullName)
cfgPath = fso.BuildPath(here, "$exe_config")

exePath = ""
If fso.FileExists(cfgPath) Then
    On Error Resume Next
    ' 1 = for reading, False = do not create, -1 = TristateTrue (UTF-16)
    exePath = Trim(fso.OpenTextFile(cfgPath, 1, False, -1).ReadAll())
    On Error GoTo 0
End If

If exePath = "" Or Not fso.FileExists(exePath) Then
    MsgBox "LiteView: the application executable was not found." & vbCrLf & vbCrLf & _
           "Re-run the file association setup, then try again:" & vbCrLf & _
           "    py -3.12 -m viewer.file_assoc install", 48, "LiteView"
    WScript.Quit 1
End If

If target <> "" Then
    ' Channel 1: environment variable. Children inherit this process env block.
    Set env = sh.Environment("PROCESS")
    env("$open_env") = target

    ' Channel 2: request file, consumed (and deleted) by the app on startup.
    ' Kept as a safety net in case mutating the process env block is blocked.
    reqDir = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\\$launcher_dir"
    If Not fso.FolderExists(reqDir) Then fso.CreateFolder(reqDir)
    reqFile = fso.BuildPath(reqDir, "$request_file")
    ' True = overwrite, True = Unicode (UTF-16LE with BOM), which the app reads back
    Set stream = fso.CreateTextFile(reqFile, True, True)
    stream.Write target
    stream.Close
End If

' 1 = normal window, False = do not wait for the app to exit.
' Chr(34) is used instead of doubled quote characters: it survives any
' encoding round-trip and still handles paths containing spaces.
sh.Run Chr(34) & exePath & Chr(34), 1, False
"""

CMD_TEMPLATE = """@echo off
rem LiteView -- "Open with" launcher (auto-generated, do not edit).
rem See $vbs for why the file path is forwarded through $open_env instead of a
rem command-line argument. `set` + `start` is plain Win32 env inheritance, so no
rem request file is needed here; the only downside of this variant is the brief
rem console window it flashes.
rem
rem The application path lives in $exe_config and this script stays pure ASCII,
rem so any exe name works -- `chcp 65001` makes cmd decode the UTF-8 config
rem below correctly (verified on Windows 10/11).
setlocal
chcp 65001 >nul
set "$open_env=%~1"

set "EXE="
if exist "%~dp0$exe_config" < "%~dp0$exe_config" set /p "EXE="
rem A stale config (app moved or deleted) is treated as "not configured".
if defined EXE if not exist "%EXE%" set "EXE="
if not defined EXE for %%f in ("%~dp0*.exe") do if not defined EXE set "EXE=%%~ff"
if not defined EXE (
    echo LiteView: the application executable was not found.
    echo Re-run: py -3.12 -m viewer.file_assoc install
    exit /b 1
)

start "" "%EXE%"
"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def image_extensions() -> list[str]:
    """图片扩展名列表（优先复用应用内的权威定义）。"""
    try:
        from .image_manager import IMAGE_EXTS  # 单一来源，避免两处列表漂移

        return sorted(IMAGE_EXTS)
    except Exception:
        return sorted(_FALLBACK_EXTS)


def _declared_artifact_names(repo: Path) -> list[str]:
    """从 pyproject.toml 读出 flet 声明的产物名（可能含中文等非 ASCII 字符）。

    对应 `[tool.flet.windows].artifact` / `[tool.flet].artifact`，与
    `flet_cli` 决定 exe 文件名时的优先级一致。
    """
    try:
        import tomllib

        data = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    except Exception:
        return []
    flet = data.get("tool", {}).get("flet", {})
    if not isinstance(flet, dict):
        return []
    windows = flet.get("windows", {})
    names = [
        (windows.get("artifact") if isinstance(windows, dict) else None),
        flet.get("artifact"),
    ]
    return [n for n in names if isinstance(n, str) and n]


def resolve_exe(explicit: str | None = None) -> Path:
    """定位打包好的 exe。

    依次尝试：pyproject 声明的产物名 → 常见英文名 → 目录里唯一的 exe。
    第三条是给“产物名完全自定义（含中文）”准备的兜底。
    """
    if explicit:
        exe = Path(explicit).expanduser()
        if not exe.is_file():
            raise SystemExit(f"找不到可执行文件：{exe}")
        return exe.resolve()

    repo = Path(__file__).resolve().parent.parent
    roots = [repo / "build" / "windows", repo / "dist", repo / "build"]
    tried: list[Path] = []

    def _probe(name: str) -> Path | None:
        # pyproject 里的 artifact 可能写成 "LiteView"（不带扩展名），两种都试。
        variants = [name] if name.lower().endswith(".exe") else [f"{name}.exe", name]
        for root in roots:
            for variant in variants:
                cand = root / variant
                tried.append(cand)
                if cand.is_file():
                    return cand.resolve()
        return None

    for name in _declared_artifact_names(repo):
        if (found := _probe(name)) is not None:
            return found
    for name in EXE_CANDIDATES:
        if (found := _probe(name)) is not None:
            return found

    # 兜底：产物目录里只有一个 exe 就用它；多个则取最新并给出提示。
    for root in roots:
        if not root.is_dir():
            continue
        found = [p for p in root.glob("*.exe") if p.is_file()]
        if len(found) == 1:
            return found[0].resolve()
        if found:
            newest = max(found, key=lambda p: p.stat().st_mtime)
            print(
                f"提示：{root} 下有 {len(found)} 个 exe，已选用最新的 {newest.name}；"
                "如需指定请用 --exe。"
            )
            return newest.resolve()

    raise SystemExit(
        "未找到打包产物，请用 --exe 指定 exe 的完整路径。已尝试：\n  "
        + "\n  ".join(str(p) for p in tried)
    )


def _check_windows() -> None:
    if os.name != "nt":
        raise SystemExit("文件关联仅适用于 Windows。")


def _system_exe(name: str) -> str:
    return str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / name)


# ---------------------------------------------------------------------------
# 启动器生成
# ---------------------------------------------------------------------------
def default_launcher_dir() -> Path:
    """启动器默认落点：%LOCALAPPDATA%\\LiteView（与打包产物解耦，重建不失效）。"""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or "."
    return Path(base) / LAUNCHER_DIR_NAME


def write_launcher(launcher_dir: Path, exe: Path, mode: str = "vbs") -> Path:
    """在启动器目录写好启动器与 exe 路径配置，返回启动器路径。

    exe 全路径**只**写进配置文件，脚本内容不含任何 exe 名称 —— 脚本因此永远是
    纯 ASCII，exe 叫 `LiteView.exe` 还是 `图片查看器.exe` 都不影响编码安全。
    """
    exe = Path(exe).resolve()
    launcher_dir.mkdir(parents=True, exist_ok=True)

    # 配置文件编码跟随读取方：
    #   vbs → UTF-16（WSH 的 TristateTrue 直读，任意路径无损）
    #   cmd → UTF-8（脚本里已 chcp 65001，`set /p` 按 65001 解码）
    (launcher_dir / EXE_CONFIG).write_bytes(
        str(exe).encode("utf-8" if mode == "cmd" else "utf-16")
    )

    if mode == "cmd":
        launcher = launcher_dir / LAUNCHER_CMD
        text = Template(CMD_TEMPLATE).substitute(
            vbs=LAUNCHER_VBS, exe_config=EXE_CONFIG, open_env=OPEN_ENV
        )
    else:
        launcher = launcher_dir / LAUNCHER_VBS
        text = Template(VBS_TEMPLATE).substitute(
            exe_config=EXE_CONFIG,
            open_env=OPEN_ENV,
            request_file=REQUEST_FILE,
            launcher_dir=LAUNCHER_DIR_NAME,
        )

    # write_bytes 避免 Windows 文本模式的换行转换：write_text 会把 \n 变成
    # CRCRLF，破坏 VBScript 的续行符（`_` 行尾）。脚本必须是纯 ASCII —— 这是
    # 上面“不嵌入 exe 名称”设计的直接结果，encode 失败即说明模板被改坏了。
    launcher.write_bytes(text.replace("\n", "\r\n").encode("ascii"))
    return launcher


def read_exe_config(launcher_dir: Path | None = None) -> str:
    """读回启动器配置里记录的 exe 全路径（缺失/损坏返回空串）。

    vbs 模式写的是 UTF-16、cmd 模式写的是 UTF-8，用 BOM 区分，避免瞎猜编码。
    """
    cfg = (launcher_dir or default_launcher_dir()) / EXE_CONFIG
    try:
        raw = cfg.read_bytes()
    except OSError:
        return ""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = raw.decode("utf-16", errors="replace")
    else:
        text = raw.decode("utf-8-sig", errors="replace")
    text = text.strip().strip('"')
    if not text or Path(text).suffix.lower() != ".exe":
        return ""
    return text


def launcher_command(launcher: Path, mode: str, arg: str = "%1") -> str:
    """构造写进注册表的 shell open command。

    `arg` 是要交给启动器的文件参数占位符：普通文件用 `%1`，文件夹空白处用 `%V`。
    """
    if mode == "cmd":
        # cmd /c 的引号规则：最外层再包一层引号，让 cmd 正确剥离。
        return f'"{_system_exe("cmd.exe")}" /c ""{launcher}" "{arg}""'
    return f'"{_system_exe("wscript.exe")}" //nologo "{launcher}" "{arg}"'


# ---------------------------------------------------------------------------
# 注册表读写
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def _key(path: str, writable: bool = True):
    import winreg

    access = winreg.KEY_READ | (winreg.KEY_WRITE if writable else 0)
    handle = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path, 0, access)
    try:
        yield handle
    finally:
        winreg.CloseKey(handle)


def _set(key, name: str | None, value: str, kind: int | None = None) -> None:
    import winreg

    if kind is None:
        kind = winreg.REG_SZ
    if kind == winreg.REG_NONE:
        value = b""  # REG_NONE 只接受二进制数据
    winreg.SetValueEx(key, name, 0, kind, value)


def _read(path: str, name: str | None = None) -> str | None:
    """读取注册表值；不存在返回 None。

    注意 REG_NONE（OpenWithProgids 等“只靠键名表态”的值就是这种类型）的数据为
    None，需要归一化成空串，否则存在性判断会把已注册的条目误判为缺失。
    """
    import winreg

    try:
        with _key(path, writable=False) as k:
            value, kind = winreg.QueryValueEx(k, name)
    except OSError:
        return None
    if kind == winreg.REG_NONE or value is None:
        return ""
    return value


def _delete_tree(path: str) -> bool:
    """递归删除 HKCU 下的键（winreg 只能删空键，需自底向上）。"""
    import winreg

    def _walk(sub: str) -> None:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub, 0, winreg.KEY_READ) as k:
                names = []
                i = 0
                while True:
                    try:
                        names.append(winreg.EnumKey(k, i))
                        i += 1
                    except OSError:
                        break
        except OSError:
            return
        for name in names:
            _walk(f"{sub}\\{name}")
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
        except OSError:
            pass

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ):
            pass
    except OSError:
        return False
    _walk(path)
    return True


def _delete_value(path: str, name: str | None = None) -> None:
    import winreg

    try:
        with _key(path) as k:
            winreg.DeleteValue(k, name)
    except OSError:
        pass


def _is_our_command(command: str | None) -> bool:
    """命令行是否指向本工具生成的启动器（据此认出自己写过的注册表项）。"""
    lowered = (command or "").lower()
    return LAUNCHER_VBS in lowered or LAUNCHER_CMD in lowered


def _our_app_keys() -> list[tuple[str, str]]:
    """扫描 HKCU\\Software\\Classes\\Applications，返回 (键名, 启动命令) 。

    以“命令指向本启动器”为判据，而不是靠固定文件名 —— 这样 exe 改名（含中文）
    之后依然能被正确识别和清理。
    """
    import winreg

    root = rf"{CLASSES}\Applications"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, root, 0, winreg.KEY_READ) as k:
            names: list[str] = []
            i = 0
            while True:
                try:
                    names.append(winreg.EnumKey(k, i))
                    i += 1
                except OSError:
                    break
    except OSError:
        return []

    found: list[tuple[str, str]] = []
    for name in names:
        command = _read(rf"{root}\{name}\shell\open\command")
        if _is_our_command(command):
            found.append((name, command or ""))
    return found


def _registered_exe_names() -> list[str]:
    """本工具可能写进 `Applications\\` 的键名：候选名 + 配置里记录的实际名。"""
    names = list(EXE_CANDIDATES)
    configured = read_exe_config()
    if configured:
        name = Path(configured).name
        if name not in names:
            names.append(name)
    for name, _ in _our_app_keys():
        if name not in names:
            names.append(name)
    return names


def _notify_shell() -> None:
    """通知资源管理器刷新关联缓存（失败不影响注册结果）。"""
    try:
        SHCNE_ASSOCCHANGED, SHCNF_IDLIST = 0x08000000, 0x0000
        ctypes.windll.shell32.SHChangeNotify(
            SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# install / uninstall / status
# ---------------------------------------------------------------------------
def install(
    exe: Path,
    launcher_mode: str = "vbs",
    launcher_dir: Path | None = None,
    icon: str | None = None,
    face_menu: bool = True,
    set_default: bool = False,
    verbose: bool = True,
) -> None:
    """把 exe 注册成图片文件的“打开方式”候选，并建立文件关联。"""
    import winreg

    exe = exe.resolve()
    dist = launcher_dir or default_launcher_dir()
    launcher = write_launcher(dist, exe, launcher_mode)
    command = launcher_command(launcher, launcher_mode)
    icon_ref = f'"{icon}",0' if icon else f'"{exe}",0'
    exts = image_extensions()

    # 1) ProgID：真正的关联目标（“打开方式”列表里显示的就是它的类型名+图标）
    with _key(rf"{CLASSES}\{PROGID}") as k:
        _set(k, None, TYPE_NAME)
        _set(k, "DefaultIcon", icon_ref)
        with _key(rf"{CLASSES}\{PROGID}\shell\open") as ok:
            _set(ok, "FriendlyAppName", FRIENDLY_NAME)
        with _key(rf"{CLASSES}\{PROGID}\shell\open\command") as ck:
            _set(ck, None, command)

    # 2) Applications\<exe>：出现在“选择其他应用”列表
    app_root = rf"{CLASSES}\Applications\{exe.name}"
    with _key(app_root) as k:
        _set(k, "FriendlyAppName", FRIENDLY_NAME)
        _set(k, "DefaultIcon", icon_ref)
    with _key(rf"{app_root}\shell\open") as k:
        _set(k, "FriendlyAppName", FRIENDLY_NAME)
    with _key(rf"{app_root}\shell\open\command") as k:
        _set(k, None, command)
    with _key(rf"{app_root}\SupportedTypes") as k:
        for ext in exts:
            _set(k, ext, "", winreg.REG_NONE)

    # 3) 每个扩展名的 OpenWithProgids（叠加，不覆盖用户已有选择）
    for ext in exts:
        with _key(rf"{CLASSES}\{ext}\OpenWithProgids") as k:
            _set(k, PROGID, "", winreg.REG_NONE)

    # 4) Capabilities + RegisteredApplications：出现在系统“默认应用”设置页
    with _key(CAPABILITIES_KEY) as k:
        _set(k, "ApplicationName", FRIENDLY_NAME)
        _set(k, "ApplicationDescription", APP_DESCRIPTION)
        _set(k, "ApplicationIcon", icon_ref)
    with _key(rf"{CAPABILITIES_KEY}\FileAssociations") as k:
        for ext in exts:
            _set(k, ext, PROGID)
    with _key(r"Software\RegisteredApplications") as k:
        _set(k, FRIENDLY_NAME, CAPABILITIES_KEY)

    # 5) 右键菜单：图片 / 文件夹 / 文件夹空白处
    if face_menu:
        for verb_path, title, arg in (
            (rf"{CLASSES}\SystemFileAssociations\image\shell\LiteViewOpen", "用 LiteView 打开", "%1"),
            (rf"{CLASSES}\Directory\shell\LiteViewOpen", "用 LiteView 打开", "%1"),
            (rf"{CLASSES}\Directory\Background\shell\LiteViewOpen", "在此处用 LiteView 打开", "%V"),
        ):
            with _key(verb_path) as k:
                _set(k, None, title)
                _set(k, "Icon", icon_ref)
            with _key(rf"{verb_path}\command") as k:
                _set(k, None, launcher_command(launcher, launcher_mode, arg))

    # 6) 可选：尝试直接设为默认（受 UserChoice 保护，通常需要用户再点一次）
    if set_default:
        for ext in exts:
            with _key(rf"{CLASSES}\{ext}") as k:
                _set(k, None, PROGID)

    _notify_shell()

    if verbose:
        print(f"已注册：{FRIENDLY_NAME}")
        print(f"  应用      {exe}")
        print(f"  启动器    {launcher}  （{launcher_mode}）")
        print(f"  关联类型  {len(exts)} 种图片格式")
        print()
        print("已加入：右键“打开方式”列表、右键“用 LiteView 打开”菜单、系统默认应用列表。")
        print("设为默认：右键任意图片 → 打开方式 → 选择其他应用 → LiteView 看图 →")
        print("         勾选“始终使用此应用”。Windows 10/11 不允许程序静默改默认，需手动点一次。")


def uninstall(remove_launcher: bool = True, verbose: bool = True) -> None:
    """撤销本工具写入的全部注册表项（不影响其它程序与用户默认设置）。"""
    import winreg

    removed: list[str] = []

    if _delete_tree(rf"{CLASSES}\{PROGID}"):
        removed.append(rf"HKCU\{CLASSES}\{PROGID}")
    if _delete_tree(rf"Software\LiteView"):
        removed.append(r"HKCU\Software\LiteView")

    # Applications\<exe>：候选名 + 配置里记录的实际名 + 扫描“命令指向本启动器”的键
    # （exe 可能被改名成中文，不能只靠固定候选名）
    for name in _registered_exe_names():
        if _delete_tree(rf"{CLASSES}\Applications\{name}"):
            removed.append(rf"HKCU\{CLASSES}\Applications\{name}")

    # RegisteredApplications 里我们自己写的那一条
    _delete_value(r"Software\RegisteredApplications", FRIENDLY_NAME)

    # 扩展名：只删自己加的值，不动用户其它配置
    for ext in image_extensions():
        _delete_value(rf"{CLASSES}\{ext}\OpenWithProgids", PROGID)
        if _read(rf"{CLASSES}\{ext}") == PROGID:
            with _key(rf"{CLASSES}\{ext}") as k:
                winreg.DeleteValue(k, None)

    for verb_path in (
        rf"{CLASSES}\SystemFileAssociations\image\shell\LiteViewOpen",
        rf"{CLASSES}\Directory\shell\LiteViewOpen",
        rf"{CLASSES}\Directory\Background\shell\LiteViewOpen",
    ):
        if _delete_tree(verb_path):
            removed.append(rf"HKCU\{verb_path}")

    if remove_launcher:
        repo = Path(__file__).resolve().parent.parent
        # 稳定目录 + 旧版本曾写过的产物目录，一并清理
        for d in {default_launcher_dir(), repo / "build" / "windows", repo / "dist"}:
            for name in (LAUNCHER_VBS, LAUNCHER_CMD, EXE_CONFIG):
                f = d / name
                if f.is_file():
                    f.unlink()
                    removed.append(str(f))
            if d == default_launcher_dir():
                try:
                    d.rmdir()  # 只在已空时删掉
                except OSError:
                    pass

    _notify_shell()
    if verbose:
        if removed:
            print("已撤销：")
            for item in removed:
                print(f"  {item}")
        else:
            print("没有发现本工具写入的关联项（可能未注册过）。")


def status_text() -> str:
    """返回当前关联状态，供命令行和图形界面共同使用。"""
    exts = image_extensions()
    linked = [e for e in exts if _read(rf"{CLASSES}\{e}\OpenWithProgids", PROGID) is not None]
    registered = _read(rf"Software\RegisteredApplications", FRIENDLY_NAME)
    # 不依赖固定文件名：按“命令指向本启动器”反查 AppKey，中文 exe 名也能认出来
    ours = _our_app_keys()

    lines = [
        f"ProgID        {PROGID}  {_read(rf'{CLASSES}\\{PROGID}') or '（未注册）'}",
        f"注册表 AppKey {ours[0][0] if ours else '（未注册）'}",
    ]
    if ours:
        lines.append(f"启动命令      {ours[0][1]}")
    lines.append(f"默认应用条目  {registered or '（未注册）'}")
    target = read_exe_config()
    lines.extend([
        f"启动器目录    {default_launcher_dir()}",
        f"exe 指向      {target or '（未记录）'}",
    ])
    if target and not Path(target).is_file():
        lines.append("              ⚠ 该 exe 已不存在，请重新执行 install")
    lines.append(f"OpenWithProgids 覆盖 {len(linked)}/{len(exts)} 种格式")
    if linked and len(linked) != len(exts):
        missing = sorted(set(exts) - set(linked))
        lines.append(f"  缺失：{' '.join(missing)}")
    lines.append(
        f"右键菜单      {'已添加' if _read(rf'{CLASSES}\\SystemFileAssociations\\image\\shell\\LiteViewOpen') else '未添加'}"
    )
    return "\n".join(lines)


def status() -> None:
    """打印当前关联状态。"""
    print(status_text())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="liteview-assoc",
        description="LiteView 看图 —— Windows“打开方式”文件关联工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法::")[-1],
    )
    sub = p.add_subparsers(dest="action")

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "--exe",
            help="打包产物 exe 的路径（默认按 pyproject 的 artifact 名自动探测 build/windows）",
        )

    inst = sub.add_parser("install", help="注册文件关联（默认动作）")
    add_common(inst)
    inst.add_argument(
        "--launcher",
        choices=("vbs", "cmd"),
        default="vbs",
        help="启动器类型：vbs = 无控制台闪窗（默认）；cmd = 兼容 VBScript 被禁用的情况",
    )
    inst.add_argument(
        "--launcher-dir",
        help=f"启动器落地目录（默认 {default_launcher_dir()}，与打包产物解耦）",
    )
    inst.add_argument("--icon", help='图标文件路径（默认用 exe 自带图标，即 "exe",0）')
    inst.add_argument("--no-face-menu", action="store_true", help="不添加右键“用 LiteView 打开”菜单")
    inst.add_argument(
        "--set-default",
        action="store_true",
        help="同时尝试把默认打开方式设为 LiteView（受系统 UserChoice 保护，可能无效）",
    )

    uni = sub.add_parser("uninstall", help="撤销全部关联")
    uni.add_argument("--keep-launcher", action="store_true", help="保留启动器文件，只清注册表")

    sub.add_parser("status", help="查看当前关联状态")
    return p


def main(argv: list[str] | None = None) -> int:
    _check_windows()
    args = build_parser().parse_args(argv)
    action = args.action or "install"

    if action == "uninstall":
        uninstall(remove_launcher=not args.keep_launcher)
    elif action == "status":
        status()
    else:
        install(
            resolve_exe(getattr(args, "exe", None)),
            launcher_mode=getattr(args, "launcher", "vbs"),
            launcher_dir=Path(args.launcher_dir).resolve() if args.launcher_dir else None,
            icon=getattr(args, "icon", None),
            face_menu=not getattr(args, "no_face_menu", False),
            set_default=getattr(args, "set_default", False),
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
