"""V1→V2 存储格式迁移入口（默认预演，不改动文件）。

用法：
    python -m minisql.cli.migrate <数据库目录>            # 预演：只报告
    python -m minisql.cli.migrate <数据库目录> --apply     # 执行迁移（默认先备份）
    python -m minisql.cli.migrate <数据库目录> --apply --no-backup
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from minisql.contracts.errors import MiniSQLError
from minisql.engine.migrate import MigrationReport, migrate


def _render(report: MigrationReport) -> None:
    if not report.changed:
        print(f"无需迁移：当前格式版本 V{report.source_version}")
        return
    summary = (f"V{report.source_version} → V{report.target_version}，"
               f"用户表 {report.tables} 张、记录 {report.rows} 行")
    if not report.applied:
        print(f"待迁移：{summary}")
        print("这是预演（dry-run），未改动文件。确认后加 --apply 执行。")
        return
    print(f"迁移完成：{summary}")
    if report.backup is not None:
        print(f"原文件备份：{report.backup.name}")


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    parser = argparse.ArgumentParser(
        prog="minisql-migrate", description="MiniSQL 存储格式迁移（V1→V2）")
    parser.add_argument("directory", type=Path, help="包含 minisql.db 的数据库目录")
    parser.add_argument("--apply", action="store_true", help="执行迁移；缺省只预演")
    parser.add_argument("--no-backup", action="store_true", help="迁移前不备份原文件")
    args = parser.parse_args(argv)
    try:
        report = migrate(args.directory, apply=args.apply, backup=not args.no_backup)
    except MiniSQLError as error:
        print(f"迁移失败：{error.code}: {error.reason}", file=sys.stderr)
        return 1
    _render(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
