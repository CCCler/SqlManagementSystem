"""全局测试夹具：真实存储（真实文件，非内存替身）。"""
from pathlib import Path

import pytest

from tests.fixtures.real_storage import open_real_store


@pytest.fixture
def real_store(tmp_path):
    """在临时目录打开真实存储，测试结束后关闭。"""
    store = open_real_store(tmp_path / "minisql.db")
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def db_path(tmp_path) -> Path:
    """真实数据库文件路径（目录内 minisql.db）；需要多实例重开时用。"""
    return tmp_path / "minisql.db"
