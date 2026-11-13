"""采集审计日志接口：维护审计记录，覆盖标记复核、归档记录等动作。

持久化改造后新增入口（旧入口全部保留）：

- ``GET  /api/audit/migration-status``      迁移进度与失败区间
- ``GET  /api/audit/{id}/events``           单条记录的追加事件流（按 seq 排序）
- ``POST /api/audit/{id}/amend``            追加修正（不改历史，只追加事件）
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.persistence.bootstrap import get_migration
from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.audit import AuditService

router = APIRouter(prefix="/api/audit", tags=["采集审计日志"])

service = AuditService()

LIST_FIELDS = ["记录编号", "操作类型", "采集计划", "执行人", "执行时间", "结果摘要", "变更内容", "复核状态"]
STATUSES = ["已记录", "已复核", "已归档"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按记录编号检索"),
    status: str | None = Query(default=None, description="已记录、已复核、已归档"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按记录编号与状态过滤采集审计日志列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/migration-status")
def migration_status() -> dict[str, Any]:
    """迁移状态：总量、已迁移、失败区间、快照文件，便于运维确认可恢复性。"""
    migration = get_migration()
    if migration is None:
        return {"状态": "未初始化", "总量": 0, "已迁移": 0, "待迁移": 0, "失败区间": []}
    return migration.status()


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出采集审计日志清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "audit", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条审计记录明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"审计记录 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/events")
def get_events(entry_id: int) -> dict[str, Any]:
    """读取单条审计记录的追加事件流，按写入顺序（seq）稳定排序。"""
    events, message = service.get_events(entry_id)
    if events is None:
        raise HTTPException(status_code=404, detail=message)
    return {"entry_id": entry_id, "total": len(events), "items": events}


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条审计记录，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段或编号冲突：{'、'.join(missing)}")
    return ActionResult(ok=True, message="审计记录已登记并持久化", entry=entry)


@router.post("/{entry_id}/amend", response_model=ActionResult)
def amend_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """追加修正：历史事件保留，只新增一条 amended 事件并持久化。"""
    note = str(payload.values.pop("note", "") or payload.remark or "")
    entry, message = service.amend_entry(entry_id, payload.values, note=note)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条审计记录执行标记复核、归档记录；不允许的动作会被拦下并说明原因。

    可在 values 中带 ``版本编号`` 指定审计结论回写的配置版本；不带则回写
    到版本清单中的最新版本。
    """
    action = str(payload.values.get("action") or "").strip()
    target_version = str(payload.values.get("版本编号") or "").strip()
    if target_version:
        entry, message = service.run_action_with_version(entry_id, action, target_version)
    else:
        entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
