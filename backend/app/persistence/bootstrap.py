"""启动引导：装配追加式事件日志、审计仓储与旧数据迁移。

顺序：

1. 打开（或首次创建）追加式事件日志，自动回放磁盘事件。
2. 首次启动且日志为空时，把内存里的旧示例数据快照后迁移进事件日志；
   运行期新写入与迁移并行时，由仓储的首写为准规则仲裁。
3. 把折叠后的权威审计记录与版本核对视图同步回兼容层 ``Store``，
   保证现有列表、详情、复核、归档入口和概览统计口径不变。
"""
from __future__ import annotations

import logging
import os
from typing import Any

from app.config import settings
from app.persistence.eventlog import AppendOnlyLog
from app.persistence.migration import AuditMigration
from app.persistence.repository import AuditRepository
from app.store import store

logger = logging.getLogger("audit-persistence")

_LOG_FILENAME = "audit-events.jsonl"

_audit_repo: AuditRepository | None = None
_migration: AuditMigration | None = None


def _default_target_version() -> str:
    """复核/归档回写的默认配置版本：当前版本清单里编号最大的一条。"""
    rows = store.rows("version")
    candidates = [str(row.get("版本编号", "")) for row in rows if row.get("版本编号")]
    return sorted(candidates)[-1] if candidates else ""


def _sync_to_store(repo: AuditRepository) -> None:
    """把权威仓储状态同步到旧 Store，保持既有接口与页面可用。"""
    records = repo.snapshot()
    audit_rows = store.rows("audit")
    audit_rows.clear()
    audit_rows.extend(records)

    view = repo.version_check_items()
    version_rows = store.rows("version")
    for row in version_rows:
        version_no = str(row.get("版本编号", ""))
        item = next((v for v in view["items"] if v["版本编号"] == version_no), None)
        if item is None:
            row.setdefault("待核对数量", 0)
            row.setdefault("审计回写", [])
            continue
        row["待核对数量"] = item["待核对数量"]
        row["审计回写"] = item["审计回写"]


def get_audit_repository() -> AuditRepository:
    """服务层入口：未显式初始化时按默认配置惰性初始化。"""
    global _audit_repo, _migration
    if _audit_repo is None:
        bootstrap()
    assert _audit_repo is not None
    return _audit_repo


def get_migration() -> AuditMigration | None:
    return _migration


def bootstrap(
    *,
    data_dir: str | None = None,
    legacy_rows: list[dict[str, Any]] | None = None,
    fail_at_index: int | None = None,
) -> dict[str, Any]:
    """装配持久化组件并执行一次性迁移；返回迁移状态。

    若已存在指向同一数据目录的仓储（路由导入期惰性创建过），直接复用，
    避免不同调用方持有两个仓储实例导致写入与查询分叉。
    """
    global _audit_repo, _migration
    target_dir = os.path.abspath(data_dir or settings.audit_data_dir)
    os.makedirs(target_dir, exist_ok=True)

    if _audit_repo is None or os.path.dirname(_audit_repo.log_path) != target_dir:
        log = AppendOnlyLog(os.path.join(target_dir, _LOG_FILENAME))
        repo = AuditRepository(log, on_change=lambda: _sync_to_store(repo))
        _audit_repo = repo
    repo = _audit_repo

    if _migration is None or os.path.dirname(_migration.state_path) != target_dir:
        _migration = AuditMigration(target_dir, repo._log, repo)  # noqa: SLF001
    migration = _migration

    if migration.state["status"] in ("pending", "failed", "running"):
        # 先抓取旧内存数据，再做同步——同步会用仓储视图替换 store 的 audit 表
        rows = legacy_rows if legacy_rows is not None else [dict(r) for r in store.rows("audit")]
        # 仅在尚未建快照时快照当前旧数据；运行期写入不影响已保存的原始快照
        if not os.path.exists(migration.snapshot_path):
            migration._write_snapshot(rows)  # noqa: SLF001
        try:
            status = migration.run(
                rows,
                version_resolver=lambda _row: _default_target_version(),
                fail_at_index=fail_at_index,
            )
        except Exception as exc:  # noqa: BLE001 - 迁移失败不阻断服务启动
            logger.exception("审计记录迁移中断，原数据已保留，待恢复后续跑：%s", exc)
            status = migration.status()
        _sync_to_store(repo)
        if status.get("状态") == "failed":
            logger.warning("审计迁移存在失败区间：%s", status.get("失败区间"))
        return status

    _sync_to_store(repo)
    return migration.status()
