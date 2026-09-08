"""阶段一（页层基础设施）单元测试：FileManager、RowCodec、DiskPageManager。"""
import pytest

from minisql.contracts.errors import MiniSQLError
from minisql.contracts.models import ColumnSchema, DataType, TableSchema
from minisql.storage.file_manager import FileManager
from minisql.storage.page import PAGE_SIZE, DiskPageManager
from minisql.storage.record import RowCodec


def _schema() -> TableSchema:
    return TableSchema(
        "t",
        (
            ColumnSchema("a", DataType.INT),
            ColumnSchema("b", DataType.VARCHAR),
        ),
    )


# ---------- FileManager ----------

def test_file_manager_read_write(tmp_path):
    fm = FileManager(tmp_path / "test.db")
    fm.write_at(0, b"hello")
    fm.write_at(5, b"world")
    assert fm.read_at(0, 10) == b"helloworld"
    fm.close()


def test_file_manager_reopen(tmp_path):
    path = tmp_path / "test.db"
    fm = FileManager(path)
    fm.write_at(0, b"data")
    fm.close()

    fm2 = FileManager(path)
    assert fm2.read_at(0, 4) == b"data"
    fm2.close()


def test_file_manager_creates_parent_dir(tmp_path):
    path = tmp_path / "nested" / "dir" / "test.db"
    fm = FileManager(path)
    fm.write_at(0, b"x")
    fm.close()
    assert path.exists()


# ---------- RowCodec ----------

def test_rowcodec_roundtrip_int_varchar():
    codec = RowCodec()
    schema = _schema()
    row = (42, "hello")
    data = codec.encode(schema, row)
    assert codec.decode(schema, data) == row


def test_rowcodec_unicode_roundtrip():
    codec = RowCodec()
    schema = _schema()
    row = (-1, "中文测试")
    data = codec.encode(schema, row)
    assert codec.decode(schema, data) == row


def test_rowcodec_int_boundaries():
    codec = RowCodec()
    schema = _schema()
    for value in (-(2**63), 2**63 - 1):
        row = (value, "")
        assert codec.decode(schema, codec.encode(schema, row)) == row


def test_rowcodec_column_count_mismatch():
    codec = RowCodec()
    with pytest.raises(MiniSQLError) as exc:
        codec.encode(_schema(), (1,))
    assert exc.value.code == "INVALID_RECORD"


def test_rowcodec_type_mismatch():
    codec = RowCodec()
    with pytest.raises(MiniSQLError) as exc:
        codec.encode(_schema(), ("not-int", "x"))
    assert exc.value.code == "TYPE_MISMATCH"


def test_rowcodec_bool_rejected_as_int():
    codec = RowCodec()
    with pytest.raises(MiniSQLError) as exc:
        codec.encode(_schema(), (True, "x"))
    assert exc.value.code == "TYPE_MISMATCH"


def test_rowcodec_int_out_of_range():
    codec = RowCodec()
    with pytest.raises(MiniSQLError) as exc:
        codec.encode(_schema(), (2**63, "x"))
    assert exc.value.code == "INVALID_RECORD"


# ---------- DiskPageManager ----------

def test_page_allocate_read_write(tmp_path):
    pm = DiskPageManager(FileManager(tmp_path / "test.db"))
    page_id = pm.allocate_page()
    assert page_id == 1

    data = bytearray(PAGE_SIZE)
    data[0:4] = b"ABCD"
    pm.write_page(page_id, bytes(data))
    assert pm.read_page(page_id)[0:4] == b"ABCD"
    pm.close()


def test_page_allocate_sequential_ids(tmp_path):
    pm = DiskPageManager(FileManager(tmp_path / "test.db"))
    assert pm.allocate_page() == 1
    assert pm.allocate_page() == 2
    pm.close()


def test_page_free_reuse(tmp_path):
    pm = DiskPageManager(FileManager(tmp_path / "test.db"))
    p1 = pm.allocate_page()  # 1
    pm.allocate_page()       # 2
    pm.free_page(p1)
    assert pm.allocate_page() == 1  # 复用被释放的页
    pm.close()


def test_page_free_rejects_meta_page(tmp_path):
    pm = DiskPageManager(FileManager(tmp_path / "test.db"))
    with pytest.raises(MiniSQLError) as exc:
        pm.free_page(0)
    assert exc.value.code == "INVALID_PAGE"
    pm.close()


def test_page_reopen_restores_allocator(tmp_path):
    path = tmp_path / "test.db"
    pm = DiskPageManager(FileManager(path))
    pm.allocate_page()  # 1
    pm.close()

    pm2 = DiskPageManager(FileManager(path))
    assert pm2.allocate_page() == 2  # next_page_id 已持久化
    pm2.close()


def test_page_free_list_reopen(tmp_path):
    path = tmp_path / "test.db"
    pm = DiskPageManager(FileManager(path))
    pm.allocate_page()  # 1
    pm.allocate_page()  # 2
    pm.free_page(1)     # 空闲链表头 = 1
    pm.close()

    pm2 = DiskPageManager(FileManager(path))
    assert pm2.allocate_page() == 1  # 复用空闲页
    assert pm2.allocate_page() == 3  # 链表空后继续分配新页
    pm2.close()


def test_page_read_unallocated_raises(tmp_path):
    pm = DiskPageManager(FileManager(tmp_path / "test.db"))
    with pytest.raises(MiniSQLError) as exc:
        pm.read_page(999)
    assert exc.value.code == "IO_ERROR"
    pm.close()
