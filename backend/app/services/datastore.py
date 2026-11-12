"""数据集管理业务规则：状态流转、字段校验、版本追加与筛选口径都收在这里。

和趋势预测的联动约束：

* 训练轮次按 ``dataset_id + version_no`` 冻结数据集定义，改名/改字段只追加新版本；
* 数据集定义里的样例文案不会被视为「已确认」，未确认定义会在训练入口被拦下。
"""
from __future__ import annotations

from typing import Any

from app.services.dataset_versions import dataset_versions
from app.services.errors import DomainError
from app.store import (
    DATASET_CONTENT_FIELDS,
    UNCONFIRMED,
    dataset_is_confirmed,
    get_store,
    now_iso,
)

MODULE = "datastore"
REQUIRED_FIELDS = ["数据集编号", "数据集名称", "来源表名"]
STATUS_ORDER = ["草稿", "待审核", "已生效", "已废弃"]
ACTION_RULES = {"提交审核": "待审核", "生效发布": "已生效", "废弃下线": "已废弃"}
NEGATIVE_ACTIONS = []


class DatastoreService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = get_store().rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("数据集编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return get_store().find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with get_store().transaction():
            entry = {
                "id": get_store().next_id(MODULE),
                "status": STATUS_ORDER[0],
                "pending": True,
                "abnormal": False,
            }
            for field in DATASET_CONTENT_FIELDS:
                entry[field] = values.get(field)
            entry["创建时间"] = now_iso()
            dataset_versions.record_definition(
                entry,
                change_type="新建",
                before={},
                after={field: entry.get(field) for field in DATASET_CONTENT_FIELDS},
                changed_fields=list(DATASET_CONTENT_FIELDS),
            )
            get_store().rows(MODULE).append(entry)
        return entry, []

    def update_entry(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """修改数据集定义：只追加版本，不动历史版本与已冻结的训练轮次。"""
        entry = get_store().find(MODULE, entry_id)
        if entry is None:
            return None, f"数据集定义 {entry_id} 不存在或已归档"
        with get_store().transaction():
            before = {field: entry.get(field) for field in DATASET_CONTENT_FIELDS}
            for field in DATASET_CONTENT_FIELDS:
                if field in values:
                    entry[field] = values.get(field)
            after = {field: entry.get(field) for field in DATASET_CONTENT_FIELDS}
            changed_fields = [field for field in DATASET_CONTENT_FIELDS if before[field] != after[field]]
            if not changed_fields:
                return entry, "数据集定义没有发生变化，未生成新版本"
            dataset_versions.record_definition(
                entry,
                change_type="修改",
                before=before,
                after=after,
                changed_fields=changed_fields,
            )
            entry["pending"] = True
            new_version = int(entry["当前版本"])
        field_text = "、".join(changed_fields)
        return entry, f"数据集定义已更新为 v{new_version}，变更字段：{field_text}"

    def versions(self, entry_id: int) -> list[dict[str, Any]]:
        return dataset_versions.versions_of(entry_id)

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = get_store().find(MODULE, entry_id)
        if entry is None:
            return None, f"数据集定义 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据集管理可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        if target == "已生效" and not dataset_is_confirmed(entry):
            raise DomainError(
                "数据集定义仍含未确认字段（样例文案），不能生效发布；请先补全定义"
            )
        with get_store().transaction():
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            if entry.get("定义状态") == UNCONFIRMED and dataset_is_confirmed(entry):
                entry["定义状态"] = "已确认"
        return entry, f"数据集定义已{action}"


datastore_service = DatastoreService()
