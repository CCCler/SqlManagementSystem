from pathlib import Path


class FileManager:
    def __init__(self, path: Path) -> None:
        self.path = path

    def read_at(self, offset: int, size: int) -> bytes:
        raise NotImplementedError("成员二：读取磁盘文件")

    def write_at(self, offset: int, data: bytes) -> None:
        raise NotImplementedError("成员二：写入磁盘文件")

    def sync(self) -> None:
        raise NotImplementedError("成员二：同步文件")

    def close(self) -> None:
        raise NotImplementedError("成员二：关闭文件")
