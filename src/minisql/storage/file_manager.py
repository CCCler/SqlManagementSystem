"""数据库文件随机读写封装；负责创建文件、保证父目录存在、同步与关闭。"""
from pathlib import Path
import os


class FileManager:
    """以二进制读写模式打开数据库文件，支持随机偏移读写。

    read_at 在文件末尾时可能返回短于 size 的字节，由调用方决定是否视为错误。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self._file = open(self.path, "r+b", buffering=0)
        else:
            self._file = open(self.path, "w+b", buffering=0)

    def read_at(self, offset: int, size: int) -> bytes:
        self._file.seek(offset)
        return self._file.read(size)

    def write_at(self, offset: int, data: bytes) -> None:
        self._file.seek(offset)
        remaining = memoryview(data)
        while remaining:
            written = self._file.write(remaining)
            if written is None or written <= 0:
                raise OSError("数据库文件写入未取得进展")
            remaining = remaining[written:]

    def sync(self) -> None:
        self._file.flush()
        os.fsync(self._file.fileno())

    def close(self) -> None:
        self._file.close()
