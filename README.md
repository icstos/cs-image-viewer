# LiteView 看图

基于 Python Flet 的桌面端轻量看图软件，对标 IrfanView 基础交互：流畅、键盘友好、Windows 优先。

## 运行

依赖与打包配置统一在 `pyproject.toml`（已不再使用 `requirements.txt`）。

```bash
py -3.12 -m pip install -e .              # 基础依赖
py -3.12 -m pip install -e ".[formats]"   # 基础 + 全部特殊格式（HEIC/RAW/SVG/APNG）
py -3.12 main.py                # 启动空窗口
py -3.12 main.py <图片或文件夹>  # 启动并直接打开
liteview                        # 安装后可直接用控制台脚本启动
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
flet build windows            # 产物在 build/windows/
```

`flet build` 读取 `pyproject.toml`：`[project.dependencies]` 是运行时依赖，
`[tool.flet.windows.dependencies]` 额外把特殊格式库一起打进桌面包
（`flet build` 不会读取 `optional-dependencies`）。注意同一目录下若存在
`requirements.txt`，会**优先于** `pyproject.toml` 被读取，因此不要恢复该文件。

## 快捷键（对齐 IrfanView 经典按键）

| 按键 | 功能 |
| --- | --- |
| 空格 / PageDown / ↑ / ← | 下一张 |
| Backspace / PageUp / ↓ / → | 上一张 |
| + / = | 放大（10% ~ 1000%） |
| - / _ | 缩小 |
| Ctrl+滚轮 | 以鼠标位置为锚点缩放；滚轮（未放大时）翻页、（放大后）滚动浏览图片 |
| F | 全屏切换（Esc / 双击退出） |
| R / L | 顺时针 / 逆时针旋转 90° |
| H / V | 水平 / 垂直翻转 |
| S / P | 幻灯片 开始/停止 / 暂停/继续 |
| A | 动图播放 / 暂停 / 继续 |
| [ / ] | 动图上一帧 / 下一帧（自动暂停） |
| Ctrl+E | 导出当前帧为 PNG |
| Ctrl+O | 打开图片 |
| Ctrl+Shift+O | 打开文件夹 |
| Ctrl+0 | 适应窗口 |
| Ctrl+1 | 1:1 原始大小 |
| Delete | 删除当前图片（移入回收站，二次确认） |
| Esc | 退出全屏 / 取消选区 / 关闭弹窗 |

## 特性

- 支持 JPG/PNG/BMP/GIF/WebP/TIFF/PSD 及相机 RAW（CR2/NEF/ARW/DNG 等）、SVG、HEIC/HEIF，目录内按文件名自然排序，首尾循环
- 动图逐帧：GIF / 动态 WebP / APNG 自动播放，A 暂停/继续，[ / ] 逐帧预览（自动暂停），Ctrl+E 导出当前帧（含旋转/翻转）为 PNG，状态栏显示帧进度
- 损坏文件自动跳过并提示；旋转/翻转仅作用于显示层，不修改原文件
- 特殊格式依赖可选安装：`pip install ".[formats]"`（或按需 `.[heif]` / `.[raw]` / `.[svg]` / `.[apng]`），缺失时对应格式会提示安装
- 图片切换时异步解码 + 预加载前后各 1 张，切换丝滑不卡 UI
- 适应窗口 / 适应宽度 / 1:1 一键切换；左键拖拽框选区域、在选区内单击可将该区域放大到填满视野；右键拖拽或滚轮滚动浏览（带边界限制）
- 滚轮交互：图片完全可见时向上/向下翻页；图片放大溢出视口时向上/向下滚动浏览（到边界自动停止）；按住 Ctrl 滚轮则以鼠标所在位置为锚点缩放
- 窗口尺寸变化自动重算并居中；幻灯片默认 3 秒间隔，可调 1/2/3/5/10 秒
- 底部状态栏：文件名 / 分辨率+大小 / 页码+实时缩放百分比；全屏隐藏全部 UI

## 结构

```
pyproject.toml        依赖与打包配置（PEP 621 + [tool.flet]）
main.py               入口（支持命令行传入路径）
viewer/               应用包
  app.py               主界面组件（菜单栏、图片区、状态栏、事件分发）
  image_manager.py     图片管理器（扫描/排序/解码缓存/变换/编码）
  shortcuts.py         快捷键表与按键匹配
```

## 说明

- Flet 0.86.x 已知怪癖（已在代码中规避）：`theme_mode` 与窗口尺寸须在 `render()` 后设置；`page.width/height` 初始为陈旧值，以 resize 事件为准；Column 中 expand 子控件会吞掉后续兄弟空间、Stack 的 bottom 定位失效，故布局全部使用显式高度；组合键事件的 ctrl/shift 标志上报不可靠（实测恒为 False），修饰键状态需用 `KeyboardListener` 的 on_key_down/on_key_up 跟踪（+系统键查询兜底），点击图片区时重聚焦；`use_state` 更新做浅比较——纯视图状态（适应窗口/1:1/缩放）变化时镜像状态可能全部相同而**不触发重绘**，需在 sync_ui 末尾用递增计数 `set_render_tick(lambda t: t+1)` 强制渲染。
