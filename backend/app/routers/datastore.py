"""数据集管理接口：维护数据集定义，版本追加、生效发布与版本追溯。

改名或更新字段只会生成新版本，旧版本快照不动；趋势预测的训练轮次按
``dataset_id + version_no`` 引用数据集，因此旧评估不会因为定义改名而失真。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, DatasetUpdatePayload, EntryPayload, PageResult
from app.services.datastore import DatastoreService
from app.services.errors import DomainError
from app.store import StoreWriteError

router = APIRouter(prefix="/api/datastore", tags=["数据集管理"])

service = DatastoreService()

LIST_FIELDS = ["数据集编号", "数据集名称", "来源表名", "字段列表", "更新策略", "数据采样比例", "生命周期天数", "缓存开关"]
STATUSES = ["草稿", "待审核", "已生效", "已废弃"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按数据集编号检索"),
    status: str | None = Query(default=None, description="草稿、待审核、已生效、已废弃"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按数据集编号与状态过滤数据集管理列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出数据集管理清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "datastore", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条数据集定义明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"数据集定义 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/versions", response_model=dict)
def list_versions(entry_id: int) -> dict:
    """列出数据集的历史版本快照；旧版本只读，不会被后续改名回改。"""
    if service.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail=f"数据集定义 {entry_id} 不存在或已归档")
    versions = service.versions(entry_id)
    return {"items": versions, "total": len(versions)}


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条数据集定义，缺字段时说明原因而不是静默丢弃。"""
    try:
        entry, missing = service.create_entry(payload.values)
    except StoreWriteError as error:
        return ActionResult(ok=False, message=f"{error}；数据集定义未登记")
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="数据集定义已登记（生成 v1 版本快照）", entry=entry)


@router.patch("/{entry_id}", response_model=ActionResult)
def update_entry(entry_id: int, payload: DatasetUpdatePayload) -> ActionResult:
    """改名或更新字段：只追加新版本，历史版本与历史训练不受影响。"""
    try:
        entry, message = service.update_entry(entry_id, payload.values)
    except StoreWriteError as error:
        return ActionResult(ok=False, message=f"{error}；数据集定义维持原版本")
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条数据集定义执行提交审核、生效发布、废弃下线；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    try:
        entry, message = service.run_action(entry_id, action)
    except DomainError as error:
        return ActionResult(ok=False, message=str(error))
    except StoreWriteError as error:
        return ActionResult(ok=False, message=f"{error}；数据集状态未改变")
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
