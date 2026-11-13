# ImageViewer

基于 Python Flet 的桌面端轻量看图软件，对标 IrfanView 基础交互：流畅、键盘友好、Windows 优先。

## 运行

依赖与打包配置统一在 `pyproject.toml`（已不再使用 `requirements.txt`）。

```bash
py -3.12 -m pip install -e .              # 基础依赖
py -3.12 -m pip install -e ".[formats]"   # 基础 + 全部特殊格式（HEIC/RAW/SVG/APNG）
py -3.12 main.py                # 启动空窗口
py -3.12 main.py <图片或文件夹>  # 启动并直接打开
imageviewer                     # 安装后可直接用控制台脚本启动
imageviewer-assoc               # “打开方式”文件关联注册工具（见下）
```

可选依赖分组：

| 分组 | 内容 | 支持的格式 |
| --- | --- | --- |
| `heif` | pillow-heif | HEIC / HEIF |
| `raw` | rawpy | 相机 RAW（CR2 / NEF / ARW / DNG 等） |
| `svg` | resvg | SVG |
| `apng` | apng | APNG 逐帧 |
| `formats` | 以上全部 | — |
| `build` | flet-cli | 仅打包用 |

## 打包

```bash
py -3.12 -m pip install ".[build]"
flet build windows --python-version 3.12      # 产物在 build/windows/
```

必须显式带 `--python-version 3.12`：flet-cli 的默认打包 Python 版本会随版本升级
（1.0.4 起默认 3.14），版本一变就会清空 `build/` 并重装全部依赖项，
而 `rawpy` / `resvg` / `pillow-heif` 这些二进制包是按 3.12 装的。

若 `build/flutter/build/windows/.../Release` 已存在且构建被 safe-delete 钩子拦下，
先把该目录移走/删除再构建（Flutter 需要重建整个 Release 包）。

`flet build` 读取 `pyproject.toml`：`[project.dependencies]` 是运行时依赖，
`[tool.flet.windows.dependencies]` 额外把特殊格式库一起打进桌面包
（`flet build` 不会读取 `optional-dependencies`）。注意同一目录下若存在
`requirements.txt`，会**优先于** `pyproject.toml` 被读取，因此不要恢复该文件。

## 启动行为（不闪白屏）

`pyproject.toml` 中有三项直接决定「打开时的观感」，改动前先读这里：

| 配置 | 作用 |
| --- | --- |
| `[tool.flet.app] hide_window_on_start = true` | 客户端窗口创建后**不显示**，等 Python 布置好界面再亮出来 |
| `[tool.flet.boot_screen.flet]` | 启动屏底色/转圈全设成深色，任何提前显示的帧都不会是白色 |
| `[tool.flet.compile] app = true` | 打包时把 `main.py` / `viewer/*.py` 预编译成 `.pyc`，内嵌解释器启动更快 |

`main.py` 的启动顺序是「先布置、后显示」：构建 UI 树 → 一次性下发窗口尺寸/最小尺寸/
主题/底色（窗口仍隐藏）→ 居中 → 把标题栏切成深色 → 最后显示。
用户第一眼看到的就是完整界面，不会出现「左上角白窗 → 跳位 → 变暗」的过程。

调试用环境变量：`CSIV_TIMING=1` 打印各阶段耗时，`CSIV_SMOKE=1` 启动数秒后自动退出。
打包版没有控制台，启动期的未捕获异常会写进 `%LOCALAPPDATA%\ImageViewer\logs\crash.log`。

## 多窗口与共享缓存（进程模型）

Flet 1.0 没有多窗口 API，**一个窗口必然是一个 Flutter 进程**，所以窗口本身轻不了；
但「解码 + 变换 + 编码 + 预加载」这摊重活可以整体挪到一个**独立的常驻主进程**里：

```
主进程（python.exe，无窗口、不加载 Flutter，约 27MB）
   ├── 单实例锁 + 服务端（仅 127.0.0.1 + 随机令牌）
   ├── 共享缓存：磁盘 blob（L2）+ 内存解码结果（LRU）
   └── 预加载队列
        ↑ 只发请求（取显示数据 / 目录列表 / 预加载）
   ├── 窗口进程 #1（Flet，约 175MB，体积由 Flutter 引擎决定）
   ├── 窗口进程 #2
   └── 窗口进程 #N
```

- **窗口只做客户端**：启动时先找主进程；找不到就把它拉起来，然后再连上去。
  主进程是常态，「谁先启动谁兼职当服务」只是兜底。
- **打包态也能有独立主进程**：flet 产物里其实已经带齐了 Python 运行时
  （`python312.dll` + `Lib/` + `DLLs/` + `site-packages/`），只缺一个启动器。
  因此随包放一份 `assets/runtime/python.exe`（约 100KB），首次运行时复制到应用目录，
  之后就用**同一套运行时**跑主进程 —— 不额外加载 Flutter 引擎。
- **兜底**：应用装在只读目录、或产物里缺启动器时，复制失败 → 自动退回
  「第一个窗口兼任宿主」，功能完全一致，只是主进程不独立（状态栏会显示
  「主进程（本窗口兼任）」）。
- **常驻与回收**：所有窗口关闭后主进程仍在（缓存保持热），空闲 `CSIV_MASTER_IDLE`
  秒后退出，默认 900；设为 `0` 表示永不退出。
- **服务迁移**：主进程被结束后，下一次请求会让窗口自动重新拉起一个并接管。
  服务**不保存会话状态**（键 = 路径+修改时间+大小+显示参数），交接对用户透明。
- **两级共享缓存**：
  - 磁盘 L2（`cache/blobs/`，默认上限 512MB，`CSIV_CACHE_MB` 可调）：键 = 文件身份
    （路径 + 修改时间 + 大小）+ 显示参数（旋转/翻转/尺寸预算），原文件被改过自动失效，
    进程重启后依然有效；
  - 内存解码缓存（默认 3 张 / 40MP）：旋转、翻转、换尺寸只重新编码，不重新解码。
- **预加载**：窗口显示某张图后，主进程在后台预热上一张/下一张，按方向键时通常直接命中。
- **动图留在窗口本地**：GIF/WebP/APNG 依赖文件句柄 seek，逐帧跨进程通信只会更慢。

状态栏中段出现「缓存」= 这一帧直接读自共享缓存，本窗口**没有解码**；
「视图 → 共享缓存状态…」显示角色（主进程/窗口）、服务端 pid、命中率、缓存体积与预加载进度。

```bash
py -3.12 main.py a.jpg          # 自动拉起主进程 + 窗口1（窗口1 是客户端）
py -3.12 main.py b.png          # 再开一个窗口进程，直接接入同一个主进程
# 程序内：文件 → 在新窗口打开（Ctrl+N）；双击不同图片效果相同

py -3.12 -m viewer.daemon -v        # 手动前台跑主进程（开发/调试）
py -3.12 -m viewer.daemon --detach  # 分离到后台
```

环境变量：`CSIV_SHARED=0` 关闭共享（退回单进程本地解码，便于对比性能）；
`CSIV_MASTER_IDLE` 主进程空闲退出秒数；`IMAGEVIEWER_HOME` 覆盖数据目录（测试/便携）。

### 自检（都不需要开窗口）

```bash
py -3.12 tools/service_smoke.py         # 命中/共享/预加载/迁移/降级，39 项断言
py -3.12 tools/election_smoke.py <图片>  # 两进程同时启动：恰好一个主进程，另一方命中
py -3.12 tools/lock_check.py            # 单实例锁互斥（4 进程抢同一把锁）
```

## 文件关联（右键“打开方式” / 设为默认看图程序）

```bash
flet build windows --python-version 3.12  # 产物在 build/windows/
py -3.12 -m viewer.file_assoc install     # 注册“打开方式”+ 右键菜单（免管理员）
py -3.12 -m viewer.file_assoc status      # 查看当前关联状态
py -3.12 -m viewer.file_assoc uninstall   # 全部撤销

# 产物名与 pyproject 声明的 artifact 不一致时（例如 build 时加了 --artifact），显式指定：
py -3.12 -m viewer.file_assoc install --exe "build\windows\图片查看器.exe"
```

全部写在 `HKCU`，不需要管理员权限。注册内容：34 种图片格式的 `OpenWithProgids`、
`Applications\<exe>`、系统“默认应用”条目（`Capabilities`），以及图片 / 文件夹 /
文件夹空白处的“用 ImageViewer 打开”右键菜单。

注册后：右键任意图片 → 打开方式 → 选择其他应用 → **ImageViewer** →
勾选“始终使用此应用”，之后双击即可直接打开。（Windows 10/11 的默认程序由带校验哈希的
`UserChoice` 保护，任何程序都无法静默改默认，必须用户点这一次；`--set-default` 只能
在系统尚未记录 `UserChoice` 时生效。）

### 为什么关联命令不能写成 `"ImageViewer.exe" "%1"`

`flet build` 的产物是 Flutter/Dart 外壳 + 内嵌 CPython，它的 Dart 入口会把**任何命令行参数**
当成“开发模式”的页面地址（见 `build/flutter/lib/main.dart`）：

```dart
} else if (_args.isNotEmpty && isDesktopPlatform()) {
    pageUrl = _args[0];        // 开发模式：模板会去连接这个 URL
}
```

实测 `ImageViewer.exe D:\a.jpg`：`main.py` 一行都不会执行，窗口停在启动页且进程永不退出。
这是 Flet 打包版的已知行为，与 Python 代码无关。

因此关联命令指向一个**启动器**，由它把路径写进 `IMAGEVIEWER_OPEN` 环境变量
（另加一份短时效请求文件作兜底），再以**无参数**方式启动 exe —— 无参数 ⇒ 走生产模式 ⇒
Python 才会真正启动。`main.py` 按 `env → 请求文件 → argv` 顺序解析启动路径，所以
`py -3.12 main.py <图片>` 的开发用法完全不受影响。

启动器落在 `%LOCALAPPDATA%\ImageViewer\`（不在 `build/` 内），exe 路径记录在同目录的
`app-exe.txt`：**重建 `build/` 不会让关联失效**，只有 exe 被移动或换名才需要重跑一次
`install`（`flet build` 后跑一次即可）。启动器默认是 wscript 承载的 `.vbs`（无控制台闪窗）；
若系统禁用了 VBScript，用 `--launcher cmd` 换成 `.cmd` 版本（会有一次控制台闪窗）。

### exe 文件名（含中文）都能正常工作

启动器脚本里**不出现 exe 名称**，exe 全路径只写进 `app-exe.txt`，因此脚本本身永远是纯 ASCII，
`artifact = "图片查看器"` 这类含非 ASCII 字符的产物名同样可用：

| 启动器 | `app-exe.txt` 编码 | 读取方式 |
| --- | --- | --- |
| `imageviewer-open.vbs`（默认） | UTF-16（带 BOM） | WSH `OpenTextFile(..., TristateTrue)` 直读，任意路径无损 |
| `imageviewer-open.cmd` | UTF-8（无 BOM） | 脚本内先 `chcp 65001`，再 `< app-exe.txt set /p` 解码 |

同理，`install` 不依赖固定文件名：产物探测顺序是「`pyproject.toml` 里 `[tool.flet.windows].artifact`
声明的名字 → 常见英文名 → `build/windows` 下唯一的 exe」，`status` / `uninstall` 则以
“命令是否指向本启动器”来认出自己写过的注册表项。

## 快捷键（对齐 IrfanView 经典按键）

| 按键 | 功能 |
| --- | --- |
| 空格 / PageDown / ↑ / ← | 下一张 |
| Backspace / PageUp / ↓ / → | 上一张 |
| + / = | 放大（10% ~ 1000%） |
| - / _ | 缩小 |
| Ctrl+滚轮 | 以鼠标位置为锚点缩放；滚轮（未放大时）翻页、（放大后）滚动浏览图片 |
| F | 全屏切换（双击退出） |
| R / L | 顺时针 / 逆时针旋转 90° |
| H / V | 水平 / 垂直翻转 |
| S / P | 幻灯片 开始/停止 / 暂停/继续 |
| A | 动图播放 / 暂停 / 继续 |
| [ / ] | 动图上一帧 / 下一帧（自动暂停） |
| Ctrl+E | 导出当前帧为 PNG |
| Ctrl+O | 打开图片 |
| Ctrl+Shift+O | 打开文件夹 |
| Ctrl+N | 在新窗口打开（共享缓存，可多选） |
| Ctrl+0 | 适应窗口 |
| Ctrl+1 | 1:1 原始大小 |
| Delete | 删除当前图片（移入回收站，二次确认） |
| Esc | 退出程序（删除确认弹窗打开时只关闭弹窗） |

## 特性

- 支持 JPG/PNG/BMP/GIF/WebP/TIFF/PSD 及相机 RAW（CR2/NEF/ARW/DNG 等）、SVG、HEIC/HEIF，目录内按文件名自然排序，首尾循环
- 打开单张图片（双击文件、`打开方式`、命令行传路径）时**先出图再补目录**：首帧延迟与目录里有多少文件、是否在慢速网络盘上无关，同一张图显示后后台再补全同目录列表，随后即可用上一张/下一张浏览（不重新解码、不闪屏）；目录不可读时退化为只显示这一张
- 动图逐帧：GIF / 动态 WebP / APNG 自动播放，A 暂停/继续，[ / ] 逐帧预览（自动暂停），Ctrl+E 导出当前帧（含旋转/翻转）为 PNG，状态栏显示帧进度
- 损坏文件自动跳过并提示；旋转/翻转仅作用于显示层，不修改原文件
- 特殊格式依赖可选安装：`pip install ".[formats]"`（或按需 `.[heif]` / `.[raw]` / `.[svg]` / `.[apng]`），缺失时对应格式会提示安装
- 图片切换时异步解码 + 预加载前后各 1 张，切换丝滑不卡 UI；多窗口共享解码结果（见「多窗口与共享缓存」）
- 图片区左右两侧悬浮半透明圆形箭头（←›）：悬停增亮、点击切换上一张/下一张，全屏同样可用；只有一张图时自动隐藏（没有可切换的目标就不该出现按钮）
- 适应窗口 / 适应宽度 / 1:1 一键切换；左键拖拽框选区域、在选区内单击可将该区域放大到填满视野；右键拖拽或滚轮滚动浏览（带边界限制）
- 滚轮交互：图片完全可见时向上/向下翻页；图片放大溢出视口时向上/向下滚动浏览（到边界自动停止）；按住 Ctrl 滚轮则以鼠标所在位置为锚点缩放
- 窗口尺寸变化自动重算并居中；幻灯片默认 3 秒间隔，可调 1/2/3/5/10 秒
- 底部状态栏：文件名 / 分辨率+大小 / 页码+实时缩放百分比；全屏隐藏全部 UI
- 界面统一使用随包分发的本地字体 `assets/fonts/AlibabaPuHuiTi-3-55-Regular.otf`（阿里普惠体）：不依赖目标机器装了什么中文字体，菜单、状态栏、弹窗、提示的文字风格一致；字体路径相对 `assets/` 声明，开发运行与 `flet build` 产物同一套写法

## 结构

```
pyproject.toml        依赖与打包配置（PEP 621 + [tool.flet]）
main.py               入口（角色分发：窗口 / 主进程；启动路径：env / 请求文件 / argv）
assets/fonts/         随包字体（AlibabaPuHuiTi-3-55-Regular.otf，全局界面字体）
assets/runtime/       随包解释器启动器（python.exe，约 100KB，用于拉起无窗口主进程）
tools/                无界面自检脚本（服务冒烟 / 双进程竞选 / 文件锁）
viewer/               应用包
  app.py               主界面组件（菜单栏、图片区、状态栏、事件分发）
  image_manager.py     图片管理器（扫描/排序/解码缓存/变换/编码），也是降级路径
  imaging.py           解码/变换/编码的纯函数层（本地与服务共用，保证结果逐字节一致）
  shared_manager.py    共享缓存版管理器（状态本地、显示走服务、动图与故障本地解码）
  daemon.py            主进程入口（python -m viewer.daemon；无窗口、不加载 Flutter）
  shortcuts.py         快捷键表与按键匹配
  file_assoc.py        Windows 文件关联（“打开方式”）注册工具
  service/             共享服务子包
    paths.py             用户级目录（缓存/注册表/日志）
    runtime.py           单实例锁、服务注册表、进程存活探测、计数器
    protocol.py          线协议（长度前缀 JSON，令牌鉴权）
    cache.py             BlobCache（磁盘 L2）/ DecodedCache（内存）/ DirIndex（目录索引）
    core.py              ImageService：show / probe / list / preload / export / stats
    server.py            loopback TCP 服务端（一连接一线程）
    client.py            ServiceClient / DirectBackend / ServiceHost / ServiceLink（含接管）
    spawn.py             拉起主进程（含随包运行时落地）与窗口子进程
    spawn.py             拉起新窗口与守护进程
```

## 说明

- Flet 0.86.x 已知怪癖（已在代码中规避）：`theme_mode` 与窗口尺寸须在 `render()` 后设置；`page.width/height` 初始为陈旧值，以 resize 事件为准；Column 中 expand 子控件会吞掉后续兄弟空间、Stack 的 bottom 定位失效，故布局全部使用显式高度；组合键事件的 ctrl/shift 标志上报不可靠（实测恒为 False），修饰键状态需用 `KeyboardListener` 的 on_key_down/on_key_up 跟踪（+系统键查询兜底），点击图片区时重聚焦；`use_state` 更新做浅比较——纯视图状态（适应窗口/1:1/缩放）变化时镜像状态可能全部相同而**不触发重绘**，需在 sync_ui 末尾用递增计数 `set_render_tick(lambda t: t+1)` 强制渲染。
