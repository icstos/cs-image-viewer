# ImageViewer

基于 Python 3.12 + Flet 1.1 + 本地组件库 **cs-ui**（Python 包名 `ui`）的桌面端轻量看图软件，
对标 IrfanView 基础交互：流畅、键盘友好、Windows 优先。

代码按 **MVVM + 分层依赖倒置** 组织成五层（`models` / `infra` / `service` / `viewmodel` /
`view`），依赖单向 `View → ViewModel → Service → Infra`，各层均可依赖 `models`。
界面层只 `import ui`，不直接依赖 `flet`。详见「分层架构」。

## 运行

依赖与打包配置统一在 `pyproject.toml`（已不再使用 `requirements.txt`）。

界面层依赖本地组件库 **cs-ui**（导入名 `ui`，版本 0.2.0）。首次开发前先装它，
再装本项目；`flet` 会随依赖带入。打包时索引上还没有 0.2.0，需另备本地 wheel，见「打包」。

```bash
py -3.12 -m pip install -e ../cs-ui       # 或指向你本机的 cs-ui 目录
py -3.12 -m pip install -e .              # 基础依赖
py -3.12 -m pip install -e ".[formats]"   # 基础 + 全部特殊格式（HEIC/RAW/SVG/APNG）
py -3.12 main.py                # 启动空窗口
py -3.12 main.py <图片或文件夹>  # 启动并直接打开
imageviewer                     # 安装后可直接用控制台脚本启动
imageviewer-assoc status        # “打开方式”文件关联工具（见下）
imageviewer-daemon              # 只跑无窗口的共享缓存主进程（开发/调试）
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

### 先准备 cs-ui 0.2.0（必须）

`[project.dependencies]` 要求 `cs-ui>=0.2.0`，但包索引目前只发布到 `0.1.1`
（开发机上是 editable 安装的 0.2.0）。**直接 `flet build` 会因解析不到版本而失败**。
打包前先构建本地 wheel，并通过 `PIP_FIND_LINKS` 交给 flet 调用的 pip：

```bash
py -3.12 -m pip wheel --no-deps -w ../cs-ui/dist ../cs-ui   # 产出 cs_ui-0.2.0-py3-none-any.whl
export PIP_FIND_LINKS=/d/CSTOS/projects/cs-ui/dist            # Git Bash；PowerShell 用 $env:PIP_FIND_LINKS="D:\CSTOS\projects\cs-ui\dist"
flet build windows --python-version 3.12                     # 产物在 build/windows/
```

确认解析到的是本地版本：

```bash
PIP_FIND_LINKS=/d/CSTOS/projects/cs-ui/dist py -3.12 -m pip install --dry-run --ignore-installed --no-deps "cs-ui>=0.2.0"
# Would install cs-ui-0.2.0
```

wheel 必须含 `ui/core/geometry.py`、`ui/display/image_viewport.py`、
`ui/navigation/menu_bar.py` 等本次新增组件；若 cs-ui 已发布 0.2.0 到可访问索引，
这一节可整段跳过。

### 常规打包

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
| `[tool.flet.compile] app = true` | 打包时把 `main.py` / `src/cs_image_viewer/**/*.py` 预编译成 `.pyc`，内嵌解释器启动更快 |

启动屏 `bgcolor` 必须与 `src/cs_image_viewer/view/design.py` 的 `C_SURFACE` 一致（当前 `#141518`，
由 `ui.core.palette.build_scheme()` 推导）。静态 TOML 无法 import 运行时色板，
由 `tools/layered_smoke.py` 的「启动屏底色 = 窗口底色」断言守住，换品牌色时同步改。

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

py -3.12 -m cs_image_viewer.infra.shared.daemon -v        # 手动前台跑主进程（开发/调试）
py -3.12 -m cs_image_viewer.infra.shared.daemon --detach  # 分离到后台
```

环境变量：`CSIV_SHARED=0` 关闭共享（退回单进程本地解码，便于对比性能）；
`CSIV_MASTER_IDLE` 主进程空闲退出秒数；`IMAGEVIEWER_HOME` 覆盖数据目录（测试/便携）。

### 自检（都不需要开窗口）

```bash
py -3.12 tools/layered_smoke.py         # 分层约束 + 各层行为 + 装配一致性，151 项断言
py -3.12 tools/service_smoke.py         # 命中/共享/预加载/迁移/降级，40 项断言
py -3.12 tools/election_smoke.py <图片>  # 两进程同时启动：恰好一个主进程，另一方命中
py -3.12 tools/lock_check.py            # 单实例锁互斥（4 进程抢同一把锁）
```

带窗口的启动冒烟：`CSIV_SMOKE=1 CSIV_TIMING=1 py -3.12 main.py <图片>`，
数秒后自动退出，输出各阶段耗时、窗口尺寸与服务角色。

真机截图 + 定量验收（会短暂开窗口并自动关闭，PNG 存 `output/ui_visual/`）：

```bash
py -3.12 tools/ui_visual_check.py <图片目录>   # 多图：灰块 / 溢出 / 两枚翻页箭头
py -3.12 tools/ui_visual_check.py               # 空窗口对照
```

它数 ErrorWidget 灰块占比、Flutter 溢出黄纹像素、左右箭头带的亮图标像素，并用
「顶栏 / 底栏的面板底色是否在位」判定抓帧是否有效，把「构造期通过 ≠ 渲染期通过」
这类只在真机暴露的问题变成可断言的数字。

两个真机坑已固化在脚本里，改它之前先读这两段注释：

- **抓帧要按整窗开位图再裁客户区**：`PrintWindow` 会把标题栏一起画进 DC，按客户区
  开位图等于把整窗画进偏小的画布 —— 底部状态栏整条被裁掉，看起来像布局挤爆了；
- **窗口不随 python 退出**：开发模式下窗口属于 `flet.exe` 客户端进程，脚本必须
  显式关窗（必要时结束那个客户端），否则下一次运行会抓到断线重连的白色引导页。

## 文件关联（右键“打开方式” / 设为默认看图程序）

```bash
flet build windows --python-version 3.12  # 产物在 build/windows/
py -3.12 -m cs_image_viewer.infra.association install     # 注册“打开方式”+ 右键菜单（免管理员）
py -3.12 -m cs_image_viewer.infra.association status      # 查看当前关联状态
py -3.12 -m cs_image_viewer.infra.association uninstall   # 全部撤销
imageviewer-assoc status                              # 安装后等价的控制台脚本

# 产物名与 pyproject 声明的 artifact 不一致时（例如 build 时加了 --artifact），显式指定：
py -3.12 -m cs_image_viewer.infra.association install --exe "build\windows\图片查看器.exe"
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

## 分层架构（MVVM + 依赖倒置）

依赖只朝一个方向走，各层都能站在 `models` 上：

```
        ┌──────────────────────────────────────────────────────────┐
        │ models   纯数据：几何规则 / 状态枚举 / 动作表 / 格式白名单 │
        └──────────────────────────────────────────────────────────┘
              ▲            ▲              ▲              ▲
   ┌──────────┴───┐ ┌──────┴─────┐ ┌──────┴──────┐ ┌─────┴──────┐
   │ infra        │ │ service    │ │ viewmodel   │ │ view       │
   │ 技术实现     │◀│ 业务编排   │◀│ 界面状态     │◀│ 声明式渲染 │
   │ PIL/缓存/注册表│ │ 只认端口协议│ │ 纯 Python 类 │ │ 只 import ui│
   └──────────────┘ └────────────┘ └─────────────┘ └────────────┘
```

`View → ViewModel → Service → Infra`，禁止跨层与反向依赖，由
`tools/layered_smoke.py` 用 `ast` 还原相对导入后逐条断言（含「包内不出现
`import flet`」「models 只依赖标准库」）。

| 层 | 目录 | 职责 | 关键约定 |
| --- | --- | --- | --- |
| models | `src/cs_image_viewer/models/` | `geometry`（倍率/平移/锚点缩放/框选/滚轮意图/按钮定位）、`media`（变换与探测结果）、`actions`（动作枚举 + 间隔表）、`view_state`（`ViewerState` 冻结 dataclass）、`diagnostics`、`formats`（格式白名单**单一事实源**） | 零 IO、零 UI；`__init__` 不 import 任何子层，因此「不开窗口也能断言几何」 |
| infra | `src/cs_image_viewer/infra/` | `imaging`（解码/变换/编码纯函数）、`catalog`（目录扫描）、`display/local|shared`（显示数据来源）、`shared/*`（常驻主进程 + 线协议 + 磁盘/内存缓存）、`paths`、`trash`、`association`、`platform`、`windows`（DWM/看门狗）、`procs`、`concurrency` | 只封装技术细节，不含业务判断；对外暴露的是能力，不是库 |
| service | `src/cs_image_viewer/service/` | `ViewerService`（装配 + `load_current` / `open_in_new_windows` / `install_association` / `service_report`）、`Gallery`（打开/扫描/翻页/变换/删除）、`ports.py`（`DisplayProvider` / `FileTrash` / `AssociationStore` / `PlatformInfo` / `ProcessLauncher`，全部 `runtime_checkable`）、`diagnostics` | 只依赖抽象：换回收站实现、换显示来源都在 `main.py` 一行之内 |
| viewmodel | `src/cs_image_viewer/viewmodel/` | `ViewerViewModel`（`state` / `subscribe` / `dispatch` / 手势与缩放 / 播放循环 / 对话框请求 / 导出 / 删除 / 文件关联）、`formatting`（状态栏与诊断文案）、`ports.py`（`BackgroundRunner` / `TaskScheduler` / `ViewerFacade`） | 纯 Python 类，**不认识任何控件**；后台循环由状态差异驱动启停（`_reconcile_loops`） |
| view | `src/cs_image_viewer/view/` | `app.py`（根组件：装配 + 事件分发 + effect）、`design`（设计令牌唯一投影）、`kit` / `glass` / `viewport` / `canvas` / `bars` / `dialogs` / `menu` / `keymap` / `scheduler` | 只 `import ui`；`UI = f(state)`，控件树冻结，副作用一律由 `use_effect` 声明 |

三条把「分层」从口号变成可执行规则的设计：

1. **需要视图协助的输入写成状态**。选文件 / 选目录 / 多选 / 存盘都会落成一个
   `RequestKind` 状态，视图观察到就弹对话框，结果回填 `submit_pick` / `submit_save` /
   `cancel_request`。viewmodel 因此不必知道「对话框长什么样」，也不必持有控件。
2. **副作用由状态派生**。全屏、提示、弹窗、关窗都是 `use_effect(deps=[状态字段])`：
   按钮只翻状态，真正设置窗口属性的是 effect，于是无论谁改了状态（菜单、快捷键、
   双击）界面都会跟上。
3. **组合根只有 `main.py`**。它认识全部具体实现（`SharedDisplay` / `TrashBin` /
   `WindowsAssociation` / `SystemPlatform` / `WindowLauncher` / `AsyncioRunner` /
   `PageTaskScheduler`），其余文件都只依赖抽象。

`view` 层仍然只 `import ui`，源码里不出现 `import flet`。`ui` 是 cs-ui 的懒加载门面：
先解析 `ui` 子模块与组件注册表，未命中才回退到 `flet` 的公开名。

为本项目抽象、已沉淀到 cs-ui 的公共能力（其它项目可直接用）：

| cs-ui 能力 | 用途 |
| --- | --- |
| `ui.core.tokens` / `ui.core.palette` | 设计令牌与配色引擎（`build_scheme` 出整套 M3 色板）；`src/cs_image_viewer/view/design.py` 的唯一色值输入 |
| `ui.MenuBar` / `MenuAction` / `MenuSection` | 数据驱动的桌面下拉菜单栏，快捷键用 `ui.Kbd` 键帽呈现；`shortcut_map()` / `validate_sections()` 可查询可断言 |
| `ui.StatusBar(center=...)` | 三段式状态栏（在原两段式上扩展，旧用法向后兼容） |

**刻意留在项目里的**：视图几何（`models/geometry.py`）、图片视口（`view/viewport.py`）
与玻璃翻页按钮（`view/glass.py`）。三者都曾进过 cs-ui，2026-11-14 全部撤回。

判断标准是「这是规则，还是取舍」：

- 倍率怎么算、平移怎么夹、滚轮怎么分派 —— 形式上通用，但**取舍是看图软件的**：
  适应窗口优先于适应宽度、贴边锚点时边界让位于锚点、翻页阈值取「图片全可见」。
  升格成公共组件，等于要求别的产品接受这些默认值；
- 四层视口（选区压在图上、箭头压在选区上、切图免闪）同样是看图的层级取舍；
- 玻璃按钮更是纯皮肤。

cs-ui 只留真正跨产品一致的部分：设计令牌、配色引擎、菜单栏、状态栏、对话框、表单控件。
这条边界的好处是**改规则不用惊动任何人** —— 本项目的坐标约定就是本项目的。

`view/glass.py` 用纯工厂函数而不是 `@ui.control` 子类，是同一种克制：子类多一个
字段就会打乱 `IconButton` 的 `__init__` 参数顺序，位置参数被悄悄吃掉，直到挂树才报
英文断言。工厂没有隐藏的签名顺序。

给测试与 Agent 的复算入口（都可离线跑、可断言）：

```bash
py -3.12 tools/layered_smoke.py
py -3.12 -c "from cs_image_viewer.view import design; print(design.snapshot())"      # 色表 / 布局 / 字号快照
py -3.12 -c "from cs_image_viewer.view import menu; print(menu.shortcut_table())"    # 快捷键表
py -3.12 -c "from cs_image_viewer.models import geometry as G; \
             print(G.compute_scale(G.ViewTransform(), G.ViewSize.of(1600,1000), G.ViewSize.of(900,560)))"  # 0.56
```

`ui-mcp` 已收录上表中的 cs-ui 组件，支持中文别名检索。在 cs-ui 侧新增或改动组件后，
需同步 `ui/_registry.py`（`python scripts/gen_ui_registry.py` 生成）、
`ui/core/catalog.py`、`ui_mcp/aliases.py` 与相应子包 `__init__.py`，
再执行 `python -m ui_mcp build` 与 `python scripts/check_all.py`。

`models/geometry.py` 与 `view/*` 不在 `ui-mcp` 索引里 —— 它们只服务本项目，
查阅靠模块文档与 `tools/layered_smoke.py` 的断言。

### src 布局

包位于 `src/cs_image_viewer/`，`main.py` 与 `assets/` 留在仓库根（flet build 的入口与
资源约定）。`pyproject.toml` 用 `package-dir = { "" = ".", cs_image_viewer = "src/cs_image_viewer" }`
把两者分别映射：**不能**写成 `{ "" = "src" }`，那样根 `main.py` 会被静默漏出
wheel / editable 映射，控制台脚本 `imageviewer = "main:run"` 随即失效。
配套三条约束，都由 `tools/layered_smoke.py` 守住：

1. `main.py` 顶部把 `src/` 插入 `sys.path` —— 打包态不会执行 `pip install -e .`，
   包被编译到 `<app>/src/cs_image_viewer/`，没有这段引导就是 `ModuleNotFoundError`；
2. 指向仓库根 / 资源目录的路径只走 `cs_image_viewer/_paths.py`，不在调用处按
   `__file__` 数层（src 迁移后层数必然变，且错了不报错）；
3. 子进程（daemon）不继承父进程的 `sys.path`，`infra/procs.py` 显式把 `src/`
   注入子进程命令。

## 结构

```
pyproject.toml        依赖与打包配置（PEP 621 + [tool.flet] + src 布局映射）
main.py               唯一组合根（装配五层；角色分发：窗口 / 主进程；启动路径：env / 请求文件 / argv）
                      顶部含 src 布局引导：把 src/ 插入 sys.path，打包后仍能 import 包
assets/fonts/         随包字体（AlibabaPuHuiTi-3-55-Regular.otf，全局界面字体）
assets/runtime/       随包解释器启动器（python.exe，约 100KB，用于拉起无窗口主进程）
tools/                自检脚本（前四个不开窗口）
  layered_smoke.py      分层约束（ast 检查依赖方向）+ 各层行为 + 装配一致性
  service_smoke.py      共享缓存：命中 / 共享 / 预加载 / 迁移 / 降级
  election_smoke.py     双进程竞选
  lock_check.py         单实例锁互斥
  ui_visual_check.py    真机截图（PrintWindow）+ 灰块 / 溢出 / 翻页箭头 / 抓帧有效性
src/
  cs_image_viewer/     src 布局项目包（__init__.py + _paths.py 路径唯一出口）
    _paths.py           仓库根 / 资源目录的唯一出口（禁止按 __file__ 数层）
    models/            ── 层 1：纯数据，所有层可依赖
      geometry.py          视图几何（纯计算，零 UI 依赖，可脱离窗口复算）
      media.py             Transform / ImageProbe / DisplayImage / ImageInfo
      actions.py           Action 枚举 + 快捷键专属集 + 幻灯片间隔表
      view_state.py        ViewerState / Notice / DialogRequest / RequestKind
      diagnostics.py       BlobStats / DecodeStats / ServiceReport
      formats.py           格式白名单（唯一事实源）
    infra/               ── 层 2：技术实现，不含业务判断
      imaging.py           解码 / 变换 / 编码纯函数（本地与服务共用，结果逐字节一致）
      catalog.py           目录扫描与自然排序
      display/local.py     本地显示：探测 / 出图 / 列表 / 预加载 / 导出 / 缓存统计
      display/shared.py    共享显示：静态图走服务、动图本地、服务失败降级
      shared/              常驻主进程：runtime / protocol / blobs / service / server / client / daemon
      paths.py             用户级目录（缓存/注册表/日志）
      trash.py             回收站
      association.py       Windows 文件关联（`imageviewer-assoc`）
      platform.py          平台查询（Windows / Ctrl 状态 / 可执行文件）
      windows.py           窗口外观（DWM 暗色标题栏 / 居中 / 显示看门狗）
      procs.py             拉起主进程与窗口子进程
      concurrency.py       线程卸载（AsyncioRunner）
    service/             ── 层 3：业务编排，只依赖端口协议
      ports.py             DisplayProvider / FileTrash / AssociationStore / PlatformInfo / ProcessLauncher
      gallery.py           画册用例：打开 / 扫描 / 翻页 / 变换 / 逐帧 / 删除
      viewer.py            ViewerService：装配 + load_current + 多窗口 + 关联 + 诊断
      diagnostics.py       服务报告解析
    viewmodel/           ── 层 4：界面状态与交互意图，不认识控件
      viewer_vm.py         ViewerViewModel：state / dispatch / 手势 / 播放循环 / 请求 / 导出 / 删除
      formatting.py        状态栏与诊断文案（纯函数）
      ports.py             BackgroundRunner / TaskScheduler / ViewerFacade
    view/                ── 层 5：声明式渲染，只 import ui
      app.py               根组件：装配 + 事件分发 + effect（订阅/尺寸/全屏/提示/弹窗/关窗）
      design.py            设计令牌投影（品牌色 → 色表 / 尺寸 / 字号，snapshot 可导出）
      kit.py                UI 词汇：文本 / 按钮 / 空态 / 提示
      glass.py             玻璃翻页按钮（压在图片上的浮层，纯工厂，无隐藏签名）
      viewport.py          图片视口四层渲染：背景 / 图像 / 框选 / 浮层
      canvas.py            画布装配：视口 + 浮层 + 手势
      bars.py              菜单栏 / 三段式状态栏
      menu.py              菜单纯数据 + 快捷键表 + 分发校验
      keymap.py            按键组合 → Action
      dialogs.py           确认框 / 诊断框构件
      scheduler.py         页面任务调度（TaskScheduler 实现）
```

## 说明

- Flet 1.1（含 0.86 以来）的控件行为与规避方式：`theme_mode` 与窗口尺寸须在 `render()` 后设置；`page.width/height` 是**逻辑客户区**尺寸（初始可能是陈旧值，以 resize 事件为准）；Column 中 expand 子控件会吞掉后续兄弟空间、Stack 的 bottom 定位失效，故布局全部使用显式高度；组合键事件的 ctrl/shift 标志上报不可靠（实测恒为 False），修饰键状态需用 `KeyboardListener` 的 on_key_down/on_key_up 跟踪（+系统键查询兜底），点击图片区时重聚焦。
- 冻结控件树：不要在事件回调里对已挂载控件调用 `update()`，状态变化一律经 `use_state` / `use_effect` 触发重渲染；需要「可查询」的界面信息（快捷键、色表、几何）放在纯数据层，而不是从控件树里反读。
- `IconButton` 一律用 `icon=` 关键字构造：`@ft.control` 生成的 `__init__` 会把子类新增或重声明的字段排在父类字段之前，位置参数会被别的字段吃掉、`icon` 变 `None`，直到控件挂树才由渲染端抛 `IconButton must have either icon or a visible content specified`。本项目的 `view/glass.py` 因此**不做 `IconButton` 子类**，改用纯工厂函数（没有隐藏的签名顺序）；`tools/layered_smoke.py` 断言两枚翻页按钮都带图标。
- **构造期通过 ≠ 渲染期通过**：条件渲染的控件（如只有多张图才出现的翻页箭头）必须用能触发该分支的真实场景冒烟，单图 / 空窗口会漏掉。
- **开发模式下窗口不属于 python 进程**：`py main.py` 时窗口由 `flet.exe` 客户端进程创建（pid 与解释器不同），而打包版里 Python 内嵌在 Flutter 进程内。因此 `infra/windows.py` 的外观函数先按 pid 找窗口、找不到再按窗口标题回落 —— 只按 pid 找会让暗色标题栏与显示看门狗在开发期静默空转（表现：深色界面顶着一条白标题栏）。这条由 `layered_smoke.py` 的「窗口外观函数可接受窗口标题」+「main.py 把窗口标题交给窗口外观」守住。
- `ui.MenuBar` 的下拉面板位置由 `MenuStyle.alignment` + `SubmenuButton.alignment_offset` 两个量共同决定：Flutter 的 `alignment` 只是**对齐点**（`Alignment.withinRect`），不会把面板内嵌到锚点矩形里，所以顶栏栏目必须撑满栏高并下沉 `MENU_DROP`（= 栏高）才会落在栏下方；嵌套子菜单用 `TOP_RIGHT` 从父项右侧展开。这两个数由 cs-ui 的 `scripts/_menu_bar_logic.py` 断言，改之前先看该文件的说明。
