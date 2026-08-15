# CS Image Viewer

基于 Python Flet 框架的桌面端轻量看图软件，对标 IrfanView 基础交互逻辑，主打流畅、键盘友好，Windows 平台优先。

## 特性

- **文件与目录管理**：打开单张图片 / 打开文件夹自动扫描，支持 JPG/PNG/BMP/GIF/WebP/TIFF 等常见格式，自然排序，循环切换，损坏文件自动跳过。
- **视图与缩放**：无级缩放（10%–1000%）、适应窗口 / 适应宽度 / 1:1 原始像素，鼠标拖动平移带边界限制，窗口缩放自动重新适配。
- **显示层变换**：顺时针 / 逆时针 90° 旋转、水平 / 垂直翻转（不修改原文件），切换图片自动重置。
- **浏览模式**：全屏（F）、幻灯片播放（默认 3 秒，可调）、Esc / 双击退出全屏。
- **快捷键**：严格对齐 IrfanView 经典按键（空格/PageDown 下一张、Backspace/PageUp 上一张、+/= 放大、-/ _ 缩小、F 全屏、R/L 旋转、H/V 翻转、Ctrl+O 打开文件、Ctrl+Shift+O 打开文件夹、Ctrl+0 适应窗口、Ctrl+1 1:1、Delete 删除、Esc 退出）。
- **性能优化**：预加载前后各 1 张图片，切换流畅。
- **UI**：底部状态栏（文件名 · 分辨率+大小 · 页码），顶部极简菜单栏（文件 / 编辑 / 视图）。

## 安装

```bash
pip install -r requirements.txt
```

## 运行

```bash
python main.py
```

或使用 flet CLI：

```bash
flet run main.py
```

## 架构

| 模块 | 职责 |
|------|------|
| `image_manager.py` | 图片管理器：目录扫描、自然排序、索引导航、预加载缓存、元信息读取、删除至回收站 |
| `shortcuts.py` | 快捷键处理器：KeyboardEvent → 动作映射 |
| `state.py` | 可观察状态对象：状态变量驱动 UI 更新 |
| `main_window.py` | 主窗口组件：声明式构建菜单栏 / 图片显示区 / 状态栏，处理交互 |
| `main.py` | 入口 |

## 技术栈

- Python 3.12+
- Flet 0.86.5（声明式组件 + Hooks）
- Pillow（图像解码 / TIFF 转码）
- Send2Trash（安全删除）
