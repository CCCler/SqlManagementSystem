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
    words = []
    trigger_block = False
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
        elif sql[i].isascii() and (sql[i].isalpha() or sql[i] == "_"):
            start = i
            i += 1
            while i < len(sql) and sql[i].isascii() and (sql[i].isalnum() or sql[i] == "_"):
                i += 1
            word = sql[start:i].upper()
            if len(words) < 2:
                words.append(word)
            if words == ["CREATE", "TRIGGER"]:
                if word == "BEGIN":
                    trigger_block = True
                elif word == "END":
                    trigger_block = False
            has_content = True
        elif sql[i] == ";" and trigger_block:
            i += 1
        elif sql[i] == ";":
            i += 1
            ends.append(i)
            has_content = False
            words = []
        else:
            has_content = has_content or not sql[i].isspace()
            i += 1
    return StatementScan(tuple(ends), "normal", has_content)
