"""采集审计日志业务规则：状态流转、字段校验与筛选口径都收在这里。

数据底座已从进程内存切换为可重启恢复的追加式事件日志（见
``app/persistence``）：本服务只负责入参校验与动作编排，权威状态读写
全部走 ``AuditRepository``。
"""
from __future__ import annotations

from typing import Any

from app.persistence.bootstrap import get_audit_repository
from app.persistence.repository import REQUIRED_FIELDS, STATUS_ORDER, ConflictError

MODULE = "audit"
# 向后兼容旧引用
ACTION_RULES = {"标记复核": "已记录", "归档记录": "已复核"}
NEGATIVE_ACTIONS = []


class AuditService:
    def __init__(self) -> None:
        # 不缓存仓储：路由在应用导入期就实例化服务，而真正的引导在 lifespan
        # 中按 AUDIT_DATA_DIR 执行，这里每次按当前单例取，避免持有过期仓储
        self._repo = None

    @property
    def repo(self):
        if self._repo is None:
            self._repo = get_audit_repository()
        return self._repo

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        return self.repo.list_records(keyword=keyword, status=status, page=page, size=size)

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return self.repo.get_by_id(entry_id)

    def get_events(self, entry_id: int) -> tuple[list[dict[str, Any]] | None, str]:
        entry = self.repo.get_by_id(entry_id)
        if entry is None:
            return None, f"审计记录 {entry_id} 不存在或已归档"
        return self.repo.events_for(str(entry["记录编号"])), ""

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        try:
            entry, missing, _conflict = self.repo.record(values)
            return entry, missing
        except ConflictError as exc:
            # 首写为准：重复编号不新建副本，提示走追加修正
            return None, [f"记录编号 {exc} 已存在，首次写入为准；如需修正请使用追加修正入口"]

    def amend_entry(
        self, entry_id: int, changes: dict[str, Any], *, note: str = ""
    ) -> tuple[dict[str, Any] | None, str]:
        return self.repo.amend(entry_id, changes, note=note)

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于采集审计日志可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        # 复核/归档结论回写到默认配置版本（可由 values.版本编号 覆盖的入口在路由层处理）
        if action == "标记复核":
            return self.repo.review(entry_id)
        return self.repo.archive(entry_id)

    def run_action_with_version(
        self, entry_id: int, action: str, target_version: str
    ) -> tuple[dict[str, Any] | None, str]:
        """带配置版本回写目标的动作入口，供需要明确回写版本时使用。"""
        if action == "标记复核":
            return self.repo.review(entry_id, target_version=target_version)
        if action == "归档记录":
            return self.repo.archive(entry_id, target_version=target_version)
        return self.run_action(entry_id, action)
