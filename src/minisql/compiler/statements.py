"""共享 SQL 边界扫描；允许未完成输入，不提前触发语法或语义检查。"""
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class StatementScan:
    ends: tuple[int, ...]
    state: Literal["normal", "string", "block_comment"]
    has_pending: bool


def scan_statements(sql: str) -> StatementScan:
    ends = []
    i = 0
    has_content = False
    while i < len(sql):
        if sql.startswith("--", i):
            end = sql.find("\n", i + 2)
            i = len(sql) if end == -1 else end
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            if end == -1:
                return StatementScan(tuple(ends), "block_comment", True)
            i = end + 2
        elif sql[i] == "'":
            has_content = True
            i += 1
            while i < len(sql):
                if sql[i] == "'":
                    i += 1
                    if i < len(sql) and sql[i] == "'":
                        i += 1
                        continue
                    break
                i += 1
            else:
                return StatementScan(tuple(ends), "string", True)
        elif sql[i] == ";":
            i += 1
            ends.append(i)
            has_content = False
        else:
            has_content = has_content or not sql[i].isspace()
            i += 1
    return StatementScan(tuple(ends), "normal", has_content)
