"""趋势预测接口：训练定义、训练轮次、评估录入与生效发布。

动作口径：

* 提交训练：为模型创建独立轮次，冻结训练定义与数据集版本；
* 完成训练：轮次进入「训练完成」，等待评估；
* 保存评估并生效：评估与生效一个事务，落库失败整体回滚；
* 生效发布：使用已有评估推进生效；
* 标记失效：保留轮次与评估留档。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import (
    ActionResult,
    EntryPayload,
    ForecastEvaluationPayload,
    ForecastRoundPayload,
    PageResult,
)
from app.services.errors import DomainError
from app.services.forecast import ForecastService
from app.store import StoreWriteError

router = APIRouter(prefix="/api/forecast", tags=["趋势预测"])

service = ForecastService()

LIST_FIELDS = ["模型编号", "模型类型", "训练数据集", "目标字段", "特征字段", "预测步长", "置信区间", "评估指标"]
STATUSES = ["草稿", "待训练", "已生效", "已失效"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按模型编号检索"),
    status: str | None = Query(default=None, description="草稿、待训练、已生效、已失效"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按模型编号与状态过滤趋势预测列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


# 注意：静态路径必须放在 /{entry_id} 之前，否则 "export" 会被当成 entry_id。
@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出趋势预测清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "forecast", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条预测模型明细，含训练轮次、评估记录与生效归属。"""
    entry = service.detail(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"预测模型 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/rounds", response_model=dict)
def list_rounds(entry_id: int) -> dict:
    """列出模型的所有训练轮次；每轮冻结的训练定义都在记录里。"""
    if service.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail=f"预测模型 {entry_id} 不存在或已归档")
    rounds = service.list_rounds(entry_id)
    valid = service.valid_round(entry_id)
    return {
        "items": rounds,
        "total": len(rounds),
        "有效轮次编号": valid["轮次编号"] if valid else None,
    }


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条预测模型，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="预测模型已登记", entry=entry)


@router.patch("/{entry_id}", response_model=ActionResult)
def update_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """修改模型训练定义；已产生的轮次与评估不会被倒改。"""
    try:
        entry, message = service.update_entry(entry_id, payload.values)
    except DomainError as error:
        return ActionResult(ok=False, message=str(error))
    except StoreWriteError as error:
        return ActionResult(ok=False, message=f"{error}；训练定义维持原版本")
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/rounds", response_model=ActionResult)
def submit_training(entry_id: int, payload: ForecastRoundPayload) -> ActionResult:
    """提交训练：创建独立轮次并冻结训练定义。"""
    try:
        round_record, message = service.submit_training(entry_id, payload.values)
    except DomainError as error:
        return ActionResult(ok=False, message=str(error))
    except StoreWriteError as error:
        return ActionResult(ok=False, message=f"{error}；未创建训练轮次")
    if round_record is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=round_record)


@router.post("/{entry_id}/rounds/{round_no}/complete", response_model=ActionResult)
def complete_round(entry_id: int, round_no: str) -> ActionResult:
    """把训练轮次标记为完成；完成后才能录入评估。"""
    try:
        round_record, message = service.complete_round(entry_id, round_no)
    except DomainError as error:
        return ActionResult(ok=False, message=str(error))
    except StoreWriteError as error:
        return ActionResult(ok=False, message=f"{error}；轮次状态未改变")
    if round_record is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=round_record)


@router.post("/{entry_id}/evaluations", response_model=ActionResult)
def save_evaluation(entry_id: int, payload: ForecastEvaluationPayload) -> ActionResult:
    """保存评估并推进生效：一个事务内完成，失败时原生效版本保持不变。

    ``publish=false`` 不是公开可选项：保存评估与推进生效必须是一个不可拆的结果。
    """
    if not payload.publish:
        return ActionResult(
            ok=False,
            message="保存评估与推进模型生效必须同时完成，publish=false 不被接受",
        )
    try:
        evaluation, message = service.save_evaluation(
            entry_id, payload.round_no, payload.values, require_publish=True
        )
    except DomainError as error:
        return ActionResult(ok=False, message=str(error))
    except StoreWriteError as error:
        return ActionResult(
            ok=False,
            message=f"{error}；评估未保存，模型保持原来的生效版本",
        )
    if evaluation is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=evaluation)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条预测模型执行既有动作；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    try:
        entry, message = service.run_action(entry_id, action, payload.values)
    except DomainError as error:
        return ActionResult(ok=False, message=str(error))
    except StoreWriteError as error:
        return ActionResult(ok=False, message=f"{error}；模型保持原来的生效版本")
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
