"""采集配置版本业务规则：状态流转、字段校验与筛选口径都收在这里。

审计侧的复核/归档事件会回写到本模块：每条配置版本附带「待核对数量」与
「审计回写」流水，旧的已复核、已归档标记随事件日志持久化，重启不蒸发。
"""
from __future__ import annotations

from typing import Any

from app.persistence.bootstrap import get_audit_repository
from app.store import store

MODULE = "version"
REQUIRED_FIELDS = ["版本编号", "配置名称", "版本号"]
STATUS_ORDER = ["草稿", "已发布", "已回滚", "已废弃"]
ACTION_RULES = {"发布版本": "草稿", "回滚版本": "已发布", "废弃版本": "已回滚"}
NEGATIVE_ACTIONS = ["回滚版本"]


class VersionService:
    def __init__(self) -> None:
        # 审计仓储为惰性单例；未初始化的测试场景下访问才触发引导
        self._audit_repo = None

    @property
    def audit_repo(self):
        if self._audit_repo is None:
            self._audit_repo = get_audit_repository()
        return self._audit_repo

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("版本编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["待核对数量"] = 0
        entry["审计回写"] = []
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"配置版本 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于采集配置版本可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"配置版本已{action}"

    def check_items(self, version_no: str | None = None) -> dict[str, Any]:
        """配置版本清单的待核对项与审计回写视图（来自审计事件日志折叠）。"""
        return self.audit_repo.version_check_items(version_no)
