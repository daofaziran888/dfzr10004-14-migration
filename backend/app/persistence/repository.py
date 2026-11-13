"""审计仓储：把追加事件折叠成可查询的审计记录与配置版本核对视图。

所有状态都来自 ``AppendOnlyLog`` 的事件回放，进程内存里只保留折叠结果，
重启后重新回放即可恢复，复核、归档标记不会蒸发。

事件类型（stream=audit）：

- ``audit.recorded``    登记/迁移一条审计记录（首写为准，重复幂等键直接忽略）
- ``audit.reviewed``    标记复核
- ``audit.archived``    归档
- ``audit.amended``     追加修正，不改历史，只追加新事件
- ``version.check_opened``  配置版本新增待核对项
- ``version.audit_written`` 审计结论回写配置版本
- ``version.archive_written`` 归档标记回写配置版本

折叠规则：同一「记录编号」的多个事件按 seq 顺序叠加；登记事件以首次成功
写入为准，后续登记/迁移事件不覆盖已有值，冲突值仅以 ``audit.amended``
形式留痕，保证可追溯。
"""
from __future__ import annotations

from typing import Any, Callable

from app.persistence.eventlog import AppendOnlyLog, Event, utc_now_iso

# 审计记录需要保留的历史字段（按编号迁移时逐个保真）
AUDIT_FIELDS = [
    "记录编号",
    "操作类型",
    "采集计划",
    "执行人",
    "执行时间",
    "结果摘要",
    "变更内容",
    "复核状态",
]
REQUIRED_FIELDS = ["记录编号", "操作类型", "采集计划"]
STATUS_ORDER = ["已记录", "已复核", "已归档"]
# 允许通过追加修正修改的字段白名单；状态字段不允许直接修正，只能走复核/归档动作
AMENDABLE_FIELDS = [
    "操作类型",
    "采集计划",
    "执行人",
    "执行时间",
    "结果摘要",
    "变更内容",
    "复核状态",
]


class ConflictError(RuntimeError):
    """并发写入冲突：该记录编号已被首次写入占用。"""


def _new_blank_record(record_no: str) -> dict[str, Any]:
    return {
        "记录编号": record_no,
        "操作类型": "",
        "采集计划": "",
        "执行人": "",
        "执行时间": "",
        "结果摘要": "",
        "变更内容": "",
        "复核状态": "",
        "status": STATUS_ORDER[0],
        "pending": True,
        "abnormal": False,
        "id": 0,
        "事件数": 0,
        "迁移来源": "",
        "补齐字段": [],
        "最近事件时间": "",
    }


class AuditRepository:
    """审计记录的权威读写入口；内存状态完全由事件日志折叠而来。"""

    def __init__(
        self,
        log: AppendOnlyLog,
        *,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self._log = log
        self._on_change = on_change
        self._records: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        # 配置版本侧的待核对/回写视图，同样由事件折叠
        self._version_checks: dict[str, list[dict[str, Any]]] = {}
        self._version_written: dict[str, list[dict[str, Any]]] = {}
        self._replay_all()

    @property
    def log_path(self) -> str:
        return self._log.path

    # ================================================================ 折叠
    def _replay_all(self) -> None:
        with self._log.lock:
            for event in self._log.events():
                self._fold(event)

    def _fold(self, event: Event) -> None:
        p = event.payload
        etype = event.type
        if etype == "audit.recorded":
            record_no = str(p.get("记录编号", ""))
            if not record_no:
                return
            if record_no in self._records:
                # 首写为准：重复登记（含迁移重试）不覆盖已有记录
                return
            record = _new_blank_record(record_no)
            record["id"] = int(p.get("id", 0))
            for field in AUDIT_FIELDS:
                record[field] = p.get(field, "") if field != "记录编号" else record_no
            record["迁移来源"] = p.get("迁移来源", "")
            record["补齐字段"] = list(p.get("补齐字段", []))
            record["事件数"] = 1
            record["最近事件时间"] = event.ts
            self._records[record_no] = record
            self._order.append(record_no)
            return

        record_no = str(p.get("记录编号", ""))
        record = self._records.get(record_no)
        if record is None:
            # 没有对应登记事件的状态事件不丢弃，但无法折叠进任何记录
            return

        if etype == "audit.reviewed":
            record["status"] = "已复核"
            record["pending"] = True
            record["复核状态"] = p.get("复核结论", "已复核")
            record["事件数"] = int(record.get("事件数", 0)) + 1
            record["最近事件时间"] = event.ts
        elif etype == "audit.archived":
            record["status"] = "已归档"
            record["pending"] = False
            record["复核状态"] = p.get("复核结论", record.get("复核状态") or "已复核")
            record["事件数"] = int(record.get("事件数", 0)) + 1
            record["最近事件时间"] = event.ts
        elif etype == "audit.amended":
            changes = p.get("changes", {})
            # trace_only 仅用于迁移冲突留痕，不覆盖首次成功写入的字段
            if not p.get("trace_only"):
                for field, value in changes.items():
                    if field in AMENDABLE_FIELDS:
                        record[field] = value
            # 修正只追加事件，不改动 status/pending 等流转标记
            record["事件数"] = int(record.get("事件数", 0)) + 1
            record["最近事件时间"] = event.ts
        elif etype == "version.check_opened":
            target = str(p.get("版本编号", ""))
            self._version_checks.setdefault(target, []).append(
                {
                    "记录编号": record_no,
                    "版本编号": target,
                    "说明": p.get("说明", ""),
                    "时间": event.ts,
                    "事件": event.id,
                }
            )
        elif etype == "version.audit_written":
            target = str(p.get("版本编号", ""))
            record["复核状态"] = p.get("复核结论", record.get("复核状态"))
            self._version_written.setdefault(target, []).append(
                {
                    "记录编号": record_no,
                    "版本编号": target,
                    "复核结论": p.get("复核结论", ""),
                    "结果摘要": p.get("结果摘要", ""),
                    "时间": event.ts,
                    "事件": event.id,
                }
            )
        elif etype == "version.archive_written":
            target = str(p.get("版本编号", ""))
            self._version_written.setdefault(target, []).append(
                {
                    "记录编号": record_no,
                    "版本编号": target,
                    "归档标记": p.get("归档标记", "已归档"),
                    "时间": event.ts,
                    "事件": event.id,
                }
            )

    def _emit(self, event: Event) -> None:
        self._fold(event)
        if self._on_change is not None:
            self._on_change()

    # ================================================================ 查询
    def list_records(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """按记录编号稳定排序返回；过滤/分页口径与旧内存服务保持一致。"""
        with self._log.lock:
            rows = [self._records[no] for no in self._order]
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("记录编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [dict(row) for row in rows[start:start + size]], total

    def get_by_id(self, entry_id: int) -> dict[str, Any] | None:
        with self._log.lock:
            for no in self._order:
                if int(self._records[no].get("id", 0)) == entry_id:
                    return dict(self._records[no])
        return None

    def get_by_record_no(self, record_no: str) -> dict[str, Any] | None:
        with self._log.lock:
            record = self._records.get(record_no)
            return dict(record) if record else None

    def events_for(self, record_no: str) -> list[dict[str, Any]]:
        """返回某条记录的追加事件流，按 seq（写入顺序）稳定排序。"""
        with self._log.lock:
            return [
                {
                    "seq": e.seq,
                    "id": e.id,
                    "ts": e.ts,
                    "type": e.type,
                    "payload": dict(e.payload),
                }
                for e in self._log.events()
                if str(e.payload.get("记录编号", "")) == record_no
            ]

    def all_records(self) -> list[dict[str, Any]]:
        with self._log.lock:
            return [dict(self._records[no]) for no in self._order]

    def next_entry_id(self) -> int:
        with self._log.lock:
            return max((int(r.get("id", 0)) for r in self._records.values()), default=0) + 1

    # ================================================================ 写入
    def record(
        self,
        values: dict[str, Any],
        *,
        idem_key: str = "",
        source: str = "runtime",
        filled_fields: list[str] | None = None,
        entry_id: int | None = None,
        allow_conflict: bool = False,
    ) -> tuple[dict[str, Any] | None, list[str], bool]:
        """登记一条审计记录。

        返回 ``(记录, 缺失字段, 是否与既有首写冲突)``。
        冲突时不覆盖首写数据；调用方可选择追加 ``audit.amended`` 留痕。
        """
        missing = [f for f in REQUIRED_FIELDS if not str(values.get(f) or "").strip()]
        if missing:
            return None, missing, False
        record_no = str(values["记录编号"]).strip()
        with self._log.lock:
            if record_no in self._records:
                if allow_conflict:
                    return dict(self._records[record_no]), [], True
                raise ConflictError(record_no)
            new_id = entry_id or self.next_entry_id()
            payload: dict[str, Any] = {"id": new_id}
            for field in AUDIT_FIELDS:
                payload[field] = record_no if field == "记录编号" else values.get(field, "")
            payload["迁移来源"] = source
            payload["补齐字段"] = filled_fields or []
            event = self._log.append(
                "audit.recorded",
                payload,
                idem_key=idem_key or f"record:{record_no}:{source}",
            )
            self._emit(event)
            return dict(self._records[record_no]), [], False

    def review(
        self,
        entry_id: int,
        *,
        conclusion: str = "已复核",
        target_version: str = "",
        idem_key: str = "",
    ) -> tuple[dict[str, Any] | None, str]:
        record = self.get_by_id(entry_id)
        if record is None:
            return None, f"审计记录 {entry_id} 不存在或已归档"
        if record["status"] != "已记录":
            return None, f"审计记录当前状态为「{record['status']}」，无法标记复核"
        record_no = record["记录编号"]
        with self._log.lock:
            event = self._log.append(
                "audit.reviewed",
                {"记录编号": record_no, "复核结论": conclusion},
                # 运行期动作不使用稳定幂等键：每次真实复核都是独立事件，
                # 避免与迁移补发的同名键碰撞；迁移事件自带 legacy: 前缀幂等键
                idem_key=idem_key,
            )
            self._emit(event)
            if target_version:
                base_key = idem_key or f"review:{record_no}:{event.seq}"
                opened = self._log.append(
                    "version.check_opened",
                    {
                        "记录编号": record_no,
                        "版本编号": target_version,
                        "说明": f"审计记录 {record_no} 复核后待核对",
                    },
                    idem_key=f"{base_key}:check",
                )
                self._emit(opened)
                written = self._log.append(
                    "version.audit_written",
                    {
                        "记录编号": record_no,
                        "版本编号": target_version,
                        "复核结论": conclusion,
                        "结果摘要": record.get("结果摘要", ""),
                    },
                    idem_key=f"{base_key}:write",
                )
                self._emit(written)
            return dict(self._records[record_no]), "审计记录已标记复核"

    def archive(
        self,
        entry_id: int,
        *,
        target_version: str = "",
        idem_key: str = "",
    ) -> tuple[dict[str, Any] | None, str]:
        record = self.get_by_id(entry_id)
        if record is None:
            return None, f"审计记录 {entry_id} 不存在或已归档"
        if record["status"] != "已复核":
            return None, f"审计记录当前状态为「{record['status']}」，仅已复核记录可归档"
        record_no = record["记录编号"]
        with self._log.lock:
            event = self._log.append(
                "audit.archived",
                {"记录编号": record_no, "复核结论": record.get("复核状态") or "已复核"},
                idem_key=idem_key,
            )
            self._emit(event)
            if target_version:
                base_key = idem_key or f"archive:{record_no}:{event.seq}"
                written = self._log.append(
                    "version.archive_written",
                    {
                        "记录编号": record_no,
                        "版本编号": target_version,
                        "归档标记": "已归档",
                    },
                    idem_key=f"{base_key}:write",
                )
                self._emit(written)
            return dict(self._records[record_no]), "审计记录已归档"

    def amend(
        self,
        entry_id: int,
        changes: dict[str, Any],
        *,
        note: str = "",
    ) -> tuple[dict[str, Any] | None, str]:
        """追加修正：历史事件保留不动，只新增一条 amended 事件。"""
        record = self.get_by_id(entry_id)
        if record is None:
            return None, f"审计记录 {entry_id} 不存在或已归档"
        safe_changes = {
            field: changes[field]
            for field in AMENDABLE_FIELDS
            if field in changes and str(changes[field] or "").strip()
        }
        if not safe_changes:
            return None, "没有可修正的字段（仅允许修正业务字段，状态请走复核/归档动作）"
        record_no = record["记录编号"]
        with self._log.lock:
            event = self._log.append(
                "audit.amended",
                {
                    "记录编号": record_no,
                    "changes": safe_changes,
                    "note": note,
                    "时间": utc_now_iso(),
                },
            )
            self._emit(event)
            return dict(self._records[record_no]), "审计记录已追加修正事件"

    def append_amended_event(
        self,
        record_no: str,
        changes: dict[str, Any],
        *,
        note: str,
        idem_key: str,
        trace_only: bool = True,
    ) -> Event | None:
        """供迁移器使用：对首写冲突的旧值追加一条留痕事件（幂等）。

        ``trace_only=True`` 时事件只保留旧值与说明，不折叠覆盖首写字段。
        """
        with self._log.lock:
            if record_no not in self._records:
                return None
            event = self._log.append(
                "audit.amended",
                {
                    "记录编号": record_no,
                    "changes": changes,
                    "note": note,
                    "trace_only": trace_only,
                    "时间": utc_now_iso(),
                },
                idem_key=idem_key,
            )
            self._emit(event)
            return event

    # ================================================================ 版本视图
    def version_check_items(self, version_no: str | None = None) -> dict[str, Any]:
        """配置版本清单的待核对项与审计回写视图。"""
        with self._log.lock:
            targets = [version_no] if version_no else sorted(
                set(self._version_checks) | set(self._version_written)
            )
            items: list[dict[str, Any]] = []
            for target in targets:
                checks = list(self._version_checks.get(target, []))
                written = list(self._version_written.get(target, []))
                handled = {w["记录编号"] for w in written}
                pending = [c for c in checks if c["记录编号"] not in handled]
                items.append(
                    {
                        "版本编号": target,
                        "待核对项": pending,
                        "待核对数量": len(pending),
                        "审计回写": written,
                        "回写数量": len(written),
                    }
                )
            total_pending = sum(item["待核对数量"] for item in items)
            return {"items": items, "待核对总数": total_pending}

    def snapshot(self) -> list[dict[str, Any]]:
        """当前全部审计记录的深拷贝，供同步到兼容层 Store 使用。"""
        return self.all_records()
