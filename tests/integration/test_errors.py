"""错误验收：examples/errors.sql 中每条语句在独立准备的环境中验证错误阶段与错误码。"""
from pathlib import Path

import pytest

from minisql.contracts.errors import MiniSQLError
from minisql.engine.database import open_database

ERRORS_SQL = Path(__file__).resolve().parents[2] / "examples" / "errors.sql"

EXPECTED = [
    ("semantic", "UNKNOWN_COLUMN"),
    ("semantic", "TYPE_MISMATCH"),
    ("semantic", "MISSING_COLUMN"),
    ("syntax", "UNEXPECTED_TOKEN"),
    ("lexical", "INVALID_CHARACTER"),
    ("lexical", "UNCLOSED_STRING"),
]


def _error_statements() -> tuple[str, ...]:
    lines = ERRORS_SQL.read_text(encoding="utf-8").splitlines()
    return tuple(line.strip() for line in lines if line.strip() and not line.startswith("--"))


CASES = tuple(
    (sql, stage, code)
    for sql, (stage, code) in zip(_error_statements(), EXPECTED, strict=True)
)


@pytest.mark.parametrize("sql,stage,code", CASES)
def test_error_scenario(sql, stage, code, tmp_path):
    database = open_database(tmp_path / "db")
    try:
        database.execute("CREATE TABLE student(id INT, name VARCHAR, age INT);")
        database.execute("INSERT INTO student(id,name,age) VALUES (1,'Alice',20);")
        with pytest.raises(MiniSQLError) as error:
            database.execute(sql)
        assert error.value.stage.value == stage
        assert error.value.code == code
        assert error.value.position is not None
    finally:
        database.close()
