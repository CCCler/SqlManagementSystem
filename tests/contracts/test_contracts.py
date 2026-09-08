import ast
import importlib
import pkgutil
from pathlib import Path

import pytest
import minisql
from minisql.cli.main import main
from minisql.compiler.compiler import SQLCompiler
from minisql.contracts.ast import Identifier, Literal, BinaryExpr
from minisql.contracts.errors import ErrorStage, MiniSQLError
from minisql.contracts.models import ColumnSchema, DataType, SourcePosition, TableSchema
from minisql.contracts.plans import Delete, Filter, SeqScan
from tests.fakes.memory import MemoryCatalog, MemoryStorage


def schema():
    return TableSchema("student", (ColumnSchema("id", DataType.INT),))


def test_catalog_is_case_insensitive_and_rejects_duplicates():
    catalog = MemoryCatalog((schema(),))
    assert catalog.get_table("STUDENT") == schema()
    assert catalog.get_table("missing") is None
    with pytest.raises(MiniSQLError) as error:
        catalog.register_table(schema())
    assert error.value.code == "DUPLICATE_TABLE"


def test_memory_storage_preserves_identity_and_deleted_records():
    storage = MemoryStorage()
    table = storage.create_table(schema())
    first = storage.insert(table, (1,))
    second = storage.insert(table, (2,))
    storage.delete(table, first)
    records = list(storage.scan(table))
    assert [(r.record_id, r.row) for r in records] == [(second, (2,))]
    with pytest.raises(MiniSQLError):
        storage.delete(table, first)


def test_error_keeps_position_and_expectations():
    error = MiniSQLError(ErrorStage.SYNTAX, "UNEXPECTED_TOKEN", "unexpected ;",
                         SourcePosition(3, 9), ("IDENTIFIER", "CONST"))
    assert "3:9" in str(error)
    assert error.expected == ("IDENTIFIER", "CONST")
    assert error.stage is ErrorStage.SYNTAX


def test_delete_plan_can_keep_record_source():
    position = SourcePosition(1, 1)
    predicate = BinaryExpr("=", Identifier("id", position),
                           Literal(1, DataType.INT, position), position)
    plan = Delete(schema(), Filter(predicate, SeqScan(schema())))
    assert isinstance(plan.source.source, SeqScan)


def test_scaffold_never_reports_compile_success():
    with pytest.raises(NotImplementedError):
        SQLCompiler().compile("SELECT * FROM student;", MemoryCatalog())


def test_cli_help_and_interactive_eof(capsys, tmp_path):
    with pytest.raises(SystemExit) as result:
        main(["--help"])
    assert result.value.code == 0
    assert "--data-dir" in capsys.readouterr().out
    # 存储已实现：交互模式在 stdin 不可读时按 EOF 正常退出
    assert main(["--data-dir", str(tmp_path / "db")]) == 0


def test_cli_file_mode_reports_unimplemented_compiler(capsys, tmp_path):
    sql_file = tmp_path / "demo.sql"
    sql_file.write_text("CREATE TABLE t(id INT);", encoding="utf-8")
    # 编译器仍为成员一占位：CLI 如实报告尚未实现并返回退出码 2
    assert main(["--data-dir", str(tmp_path / "db"), "--file", str(sql_file)]) == 2
    assert "尚未实现" in capsys.readouterr().err


def test_all_modules_import():
    for module in pkgutil.walk_packages(minisql.__path__, minisql.__name__ + "."):
        if module.name != "minisql.__main__":
            importlib.import_module(module.name)


def test_module_dependency_boundaries():
    root = Path(minisql.__file__).parent
    allowed = {
        "contracts": {"contracts"},
        "compiler": {"compiler", "contracts"},
        "storage": {"storage", "contracts"},
    }
    for group, permitted in allowed.items():
        for path in (root / group).glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                imports = []
                if isinstance(node, ast.Import):
                    imports = [item.name for item in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports = [node.module]
                for name in imports:
                    if name.startswith("minisql."):
                        assert name.split(".")[1] in permitted, f"{path}: {name}"


def test_catalog_bootstrap_id_does_not_consume_first_user_id():
    from minisql.engine.catalog import SYSTEM_CATALOG
    storage = MemoryStorage()
    assert storage.create_table(SYSTEM_CATALOG).table_id == 0
    assert storage.create_table(schema()).table_id == 1
