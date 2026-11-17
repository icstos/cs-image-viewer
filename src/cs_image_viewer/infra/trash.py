"""回收站 —— 删除图片的**唯一**落地方式。

「删了就进回收站」不是锦上添花：看图软件里鼠标一动就可能删错，直接
``os.remove`` 意味着不可挽回。这里把具体实现（``send2trash``）关在门内，
上层只看到 ``delete(path)`` 与一个可能抛出的异常。
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["TrashBin"]


class TrashBin:
    """实现 ``service.ports.FileTrash``：把文件移入系统回收站。"""

    def delete(self, path: Path) -> None:
        """移入回收站；文件被占用 / 不存在时抛 ``OSError`` 或库自身的异常。

        调用方负责先释放自己的句柄（Windows 上「正在被另一个程序使用」会让删除失败），
        并把异常翻译成用户能读的提示 —— 这里不做任何吞异常，吞掉就没有可诊断性了。
        """
        import send2trash

        send2trash.send2trash(str(path))
