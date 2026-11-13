"""采集配置版本接口：维护配置版本，覆盖发布版本、回滚版本、废弃版本等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.version import VersionService

router = APIRouter(prefix="/api/version", tags=["采集配置版本"])

service = VersionService()

LIST_FIELDS = ["版本编号", "配置名称", "版本号", "变更说明", "变更内容", "回滚标识", "发布状态", "发布时间"]
STATUSES = ["草稿", "已发布", "已回滚", "已废弃"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按版本编号检索"),
    status: str | None = Query(default=None, description="草稿、已发布、已回滚、已废弃"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按版本编号与状态过滤采集配置版本列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


# 静态路径必须声明在 /{entry_id} 之前，否则会被动态路由截获
@router.get("/check-items")
def check_items(
    version_no: str | None = Query(default=None, description="按版本编号过滤待核对项"),
) -> dict[str, Any]:
    """配置版本清单的待核对项与审计回写视图：复核结论、归档标记均可回看。"""
    return service.check_items(version_no)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出采集配置版本清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "version", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条配置版本明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"配置版本 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条配置版本，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="配置版本已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条配置版本执行发布版本、回滚版本、废弃版本；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
