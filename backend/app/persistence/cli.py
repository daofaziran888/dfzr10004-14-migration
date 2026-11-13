#!/usr/bin/env python3
"""审计记录迁移命令行工具。

用途：
  python -m app.persistence.cli status                 查看迁移状态与失败区间
  python -m app.persistence.cli migrate [--data-dir D] 执行/续跑迁移（默认读内存示例数据）
  python -m app.persistence.cli migrate --legacy FILE  从旧数据库导出的 JSON 文件迁移
  python -m app.persistence.cli replay                 仅回放事件日志并打印折叠结果

迁移失败后重新执行 migrate 会从检查点的失败区间继续；旧数据快照保存在
数据目录的 legacy-snapshot.json，回退时原环境记录仍可读取。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from app.config import settings
from app.persistence.bootstrap import bootstrap
from app.persistence.eventlog import AppendOnlyLog
from app.persistence.migration import AuditMigration
from app.persistence.repository import AuditRepository
from app.store import store


def _load_legacy(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict) and "rows" in data:
        return list(data["rows"])
    if isinstance(data, list):
        return list(data)
    raise SystemExit("旧数据文件格式应为记录数组，或包含 rows 字段的对象")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="审计记录追加式持久化迁移工具")
    parser.add_argument("command", choices=["status", "migrate", "replay"])
    parser.add_argument("--data-dir", default=settings.audit_data_dir, help="事件日志与检查点目录")
    parser.add_argument("--legacy", default="", help="旧数据库/旧环境导出的 JSON 文件")
    args = parser.parse_args(argv)

    data_dir = os.path.abspath(args.data_dir)
    os.makedirs(data_dir, exist_ok=True)

    if args.command == "replay":
        log = AppendOnlyLog(os.path.join(data_dir, "audit-events.jsonl"))
        repo = AuditRepository(log)
        records, total = repo.list_records(page=1, size=100000)
        print(json.dumps({"total": total, "items": records}, ensure_ascii=False, indent=2))
        return 0

    if args.command == "status":
        log = AppendOnlyLog(os.path.join(data_dir, "audit-events.jsonl"))
        repo = AuditRepository(log)
        migration = AuditMigration(data_dir, log, repo)
        print(json.dumps(migration.status(), ensure_ascii=False, indent=2))
        return 0

    legacy_rows = _load_legacy(args.legacy) if args.legacy else store.rows("audit")
    status = bootstrap(data_dir=data_dir, legacy_rows=[dict(r) for r in legacy_rows])
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0 if status.get("状态") == "completed" else 2


if __name__ == "__main__":
    sys.exit(main())
