from minisql.storage.file_manager import FileManager

DEFAULT_PAGE_SIZE = 4096


class DiskPageManager:
    def __init__(self, files: FileManager, page_size: int = DEFAULT_PAGE_SIZE) -> None:
        self.files = files
        self.page_size = page_size

    def allocate_page(self) -> int:
        raise NotImplementedError("成员二：分配数据页")

    def free_page(self, page_id: int) -> None:
        raise NotImplementedError("成员二：回收数据页")

    def read_page(self, page_id: int) -> bytes:
        raise NotImplementedError("成员二：读取完整数据页")

    def write_page(self, page_id: int, data: bytes) -> None:
        raise NotImplementedError("成员二：写入完整数据页")

    def close(self) -> None:
        raise NotImplementedError("成员二：关闭页管理器")
