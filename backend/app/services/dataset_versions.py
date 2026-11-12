"""数据集定义版本：追加式快照，旧版本永不被回改。

趋势预测的训练轮次只引用 ``dataset_id + version_no``，而不是引用数据集当前的名字。
这样跨模块改名或更新字段后，旧轮次与旧评估仍能指向训练当时的数据集定义。
"""
from __future__ import annotations

from typing import Any

from app.store import (
    get_store,
    CONFIRMED,
    DATASET_CONTENT_FIELDS,
    UNCONFIRMED,
    dataset_is_confirmed,
    now_iso
)

MODULE = "datastore"
VERSION_MODULE = "datastore_versions"


class DatasetVersionService:
    def versions_of(self, dataset_id: int) -> list[dict[str, Any]]:
        rows = [row for row in get_store().rows(VERSION_MODULE) if int(row["dataset_id"]) == dataset_id]
        return sorted(rows, key=lambda row: int(row["version_no"]))

    def get_version(self, dataset_id: int, version_no: int) -> dict[str, Any] | None:
        for row in get_store().rows(VERSION_MODULE):
            if int(row["dataset_id"]) == dataset_id and int(row["version_no"]) == version_no:
                return row
        return None

    def latest_version(self, dataset_id: int) -> dict[str, Any] | None:
        rows = self.versions_of(dataset_id)
        return rows[-1] if rows else None

    def record_definition(
        self,
        entry: dict[str, Any],
        *,
        change_type: str,
        before: dict[str, Any],
        after: dict[str, Any],
        changed_fields: list[str],
        operator: str = "值班管理员",
    ) -> dict[str, Any]:
        """把当前数据集定义固化成新版本。只在 store.transaction() 内调用。"""
        version_no = int(entry.get("当前版本") or 0) + 1
        confirmed = dataset_is_confirmed(entry)
        record = {
            "id": get_store().next_id(VERSION_MODULE),
            "dataset_id": int(entry["id"]),
            "version_no": version_no,
            **{field: after.get(field) for field in DATASET_CONTENT_FIELDS},
            "定义状态": CONFIRMED if confirmed else UNCONFIRMED,
            "变更类型": change_type,
            "变更字段": changed_fields,
            "变更前": {field: before.get(field) for field in changed_fields},
            "变更后": {field: after.get(field) for field in changed_fields},
            "创建时间": now_iso(),
            "操作人": operator,
        }
        get_store().rows(VERSION_MODULE).append(record)
        entry["当前版本"] = version_no
        entry["更新时间"] = record["创建时间"]
        entry["定义状态"] = record["定义状态"]
        return record


dataset_versions = DatasetVersionService()
