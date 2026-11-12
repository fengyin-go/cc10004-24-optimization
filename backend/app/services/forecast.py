"""趋势预测业务规则：训练定义、训练轮次、评估录入与生效裁决。

关键约束（都收在服务层，路由层只做参数搬运）：

1. 每次训练产生一个独立轮次，轮次开始时冻结五项训练定义：
   数据集（dataset_id + version_no + 数据定义快照）、目标字段、特征字段、
   预测步长、置信区间。之后跨模块改名/改字段，都不会倒改这个轮次。
2. 轮次完成后才能录入评估；评估记录落在轮次上，重启后仍能回答
   「这条生效模型是哪一版训练、用哪一版数据集产生的评估指标」。
3. 「保存评估 + 推进生效」在同一个 ``store.transaction()`` 里完成；落库失败
   整体回滚，模型保持原来的生效版本，评估也不会留下半条记录。
4. 多个轮次同时完成时按「有效轮次」接纳：序号最大的已完成轮次才有效；
   迟到的低序号结果不能顶掉用户已经确认的新结果。
5. 存量迁移：缺项标为未确认，不补样例文案、不伪造轮次和评估。
"""
from __future__ import annotations

from typing import Any

from app.services.dataset_versions import dataset_versions
from app.services.errors import DomainError
from app.store import (
    CONFIRMED,
    DATASET_CONTENT_FIELDS,
    UNCONFIRMED,
    dataset_is_confirmed,
    get_store,
    is_placeholder,
    now_iso,
)

MODULE = "forecast"
ROUND_MODULE = "forecast_rounds"
EVALUATION_MODULE = "forecast_evaluations"
DATASET_MODULE = "datastore"

REQUIRED_FIELDS = [
    "模型编号",
    "模型类型",
    "训练数据集",
    "目标字段",
    "特征字段",
    "预测步长",
    "置信区间",
]
DEFINITION_FIELDS = ["训练数据集", "目标字段", "特征字段", "预测步长", "置信区间"]
STATUS_ORDER = ["草稿", "待训练", "已生效", "已失效"]

ACTION_SUBMIT_TRAINING = "提交训练"
ACTION_SAVE_EVALUATION = "保存评估"
ACTION_PUBLISH = "生效发布"
ACTION_INVALIDATE = "标记失效"

ACCEPTED = "已接纳"
REJECTED = "已拒绝"
SUPERSEDED = "已作废"
ROUND_RUNNING = "训练中"
ROUND_COMPLETED = "训练完成"
ROUND_SUPERSEDED = "已作废"
ROUND_REJECTED = "已拒绝"

METRIC_FIELDS = ["评估指标", "指标名称", "metric"]
METRIC_VALUE_FIELDS = ["评估值", "指标值", "value"]

LEGACY_PUBLISH_ERROR = (
    "该模型缺少可核验的训练轮次与评估记录（存量迁移标记为未确认），"
    "不能直接生效发布；请重新提交训练并保存评估后再发布"
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_definition(values: dict[str, Any], *, allow_missing: bool) -> tuple[dict[str, Any], list[str]]:
    """抽取并校验训练定义；样例文案与「未确认」一律不算有效输入。"""
    definition: dict[str, Any] = {}
    missing: list[str] = []
    for field in DEFINITION_FIELDS:
        value = _text(values.get(field))
        if not value or is_placeholder(value):
            missing.append(field)
            continue
        definition[field] = value
    if allow_missing:
        for field in missing:
            definition.setdefault(field, UNCONFIRMED)
    return definition, missing


def _metric(values: dict[str, Any]) -> tuple[str | None, Any]:
    name = None
    for field in METRIC_FIELDS:
        candidate = _text(values.get(field))
        if candidate and not is_placeholder(candidate):
            name = candidate
            break
    value = None
    for field in METRIC_VALUE_FIELDS:
        if values.get(field) not in (None, ""):
            value = values.get(field)
            break
    return name, value


def _round_summary(round_record: dict[str, Any] | None) -> dict[str, Any] | None:
    if not round_record:
        return None
    return {
        "轮次编号": round_record.get("轮次编号"),
        "轮次序号": round_record.get("轮次序号"),
        "轮次状态": round_record.get("轮次状态"),
        "接纳结果": round_record.get("接纳结果"),
        "裁决说明": round_record.get("裁决说明"),
        "数据来源": round_record.get("数据来源"),
        "数据集版本": round_record.get("数据集版本"),
        "数据集快照": round_record.get("数据集快照"),
        "模型类型": round_record.get("模型类型"),
        "目标字段": round_record.get("目标字段"),
        "特征字段": round_record.get("特征字段"),
        "预测步长": round_record.get("预测步长"),
        "置信区间": round_record.get("置信区间"),
        "训练开始时间": round_record.get("训练开始时间"),
        "训练完成时间": round_record.get("训练完成时间"),
        "生效时间": round_record.get("生效时间"),
    }


def _evaluation_summary(evaluation: dict[str, Any] | None) -> dict[str, Any] | None:
    if not evaluation:
        return None
    return {
        "评估编号": evaluation.get("评估编号"),
        "轮次编号": evaluation.get("轮次编号"),
        "评估指标": evaluation.get("评估指标"),
        "评估值": evaluation.get("评估值"),
        "数据集版本": evaluation.get("数据集版本"),
        "目标字段": evaluation.get("目标字段"),
        "特征字段": evaluation.get("特征字段"),
        "预测步长": evaluation.get("预测步长"),
        "置信区间": evaluation.get("置信区间"),
        "结论": evaluation.get("结论"),
        "录入时间": evaluation.get("录入时间"),
        "录入人": evaluation.get("录入人"),
    }


def _definition_values(entry: dict[str, Any]) -> dict[str, Any]:
    return {field: entry.get(field) for field in DEFINITION_FIELDS}


def _definition_snapshot(round_record: dict[str, Any]) -> dict[str, Any]:
    return {field: round_record.get(field) for field in DEFINITION_FIELDS} | {
        "数据集版本": round_record.get("数据集版本"),
    }


class ForecastService:
    # ------------------------------------------------------------------ 读
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
            rows = [row for row in rows if keyword in str(row.get("模型编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return get_store().find(MODULE, entry_id)

    def list_rounds(self, entry_id: int) -> list[dict[str, Any]]:
        rows = [row for row in get_store().rows(ROUND_MODULE) if int(row["model_id"]) == entry_id]
        return sorted(rows, key=lambda row: int(row["轮次序号"]))

    def list_evaluations(self, entry_id: int) -> list[dict[str, Any]]:
        rounds = {row["轮次编号"] for row in self.list_rounds(entry_id)}
        return [row for row in get_store().rows(EVALUATION_MODULE) if row.get("轮次编号") in rounds]

    def get_round(self, round_no: str) -> dict[str, Any] | None:
        for row in get_store().rows(ROUND_MODULE):
            if row.get("轮次编号") == round_no:
                return row
        return None

    def get_evaluation(self, evaluation_id: str) -> dict[str, Any] | None:
        for row in get_store().rows(EVALUATION_MODULE):
            if row.get("评估编号") == evaluation_id:
                return row
        return None

    def active_round(self, entry_id: int) -> dict[str, Any] | None:
        entry = get_store().find(MODULE, entry_id)
        round_no = (entry or {}).get("生效轮次编号")
        return self.get_round(str(round_no)) if round_no else None

    def valid_round(self, entry_id: int) -> dict[str, Any] | None:
        """当前「有效轮次」：序号最大的已完成轮次（按有效轮次接纳的口径）。"""
        completed = [
            row for row in self.list_rounds(entry_id)
            if row.get("轮次状态") == ROUND_COMPLETED
        ]
        if not completed:
            return None
        return max(completed, key=lambda row: int(row["轮次序号"]))

    def detail(self, entry_id: int) -> dict[str, Any] | None:
        """模型明细分层返回：定义、轮次、评估、当前生效归属，重启后口径不变。"""
        entry = get_store().find(MODULE, entry_id)
        if entry is None:
            return None
        result = dict(entry)
        result["训练定义"] = {
            field: entry.get(field) for field in ["模型类型", *DEFINITION_FIELDS]
        }
        result["轮次列表"] = self.list_rounds(entry_id)
        result["评估列表"] = self.list_evaluations(entry_id)
        active_round = self.active_round(entry_id)
        result["生效轮次"] = _round_summary(active_round)
        result["生效评估"] = _evaluation_summary(
            self.get_evaluation(str(entry.get("生效评估编号") or ""))
        )
        valid = self.valid_round(entry_id)
        result["有效轮次编号"] = valid["轮次编号"] if valid else None
        return result

    # ------------------------------------------------------------------ 新建 / 改定义
    def create_entry(
        self, values: dict[str, Any], *, operator: str = "值班管理员"
    ) -> tuple[dict[str, Any] | None, list[str]]:
        """登记模型：必填字段缺失时拒绝；草稿的定义缺项显式标成「未确认」。"""
        missing = [field for field in REQUIRED_FIELDS if not _text(values.get(field))]
        if missing:
            return None, missing

        model_type = _text(values.get("模型类型"))
        if not model_type or is_placeholder(model_type):
            return None, ["模型类型"]
        definition, missing_definition = _normalize_definition(values, allow_missing=True)

        with get_store().transaction():
            entry = {
                "id": get_store().next_id(MODULE),
                "status": STATUS_ORDER[0],
                "pending": True,
                "abnormal": False,
                "模型编号": _text(values.get("模型编号")),
                "模型类型": model_type,
                **definition,
                "评估指标": UNCONFIRMED,
                "评估值": None,
                "定义状态": UNCONFIRMED if missing_definition else CONFIRMED,
                "证据状态": UNCONFIRMED,
                "缺失项": missing_definition,
                "迁移说明": None,
                "生效轮次编号": None,
                "生效轮次记录": None,
                "生效评估编号": None,
                "生效评估记录": None,
                "生效时间": None,
                "当前轮次编号": None,
                "训练轮次总数": 0,
                "数据集版本": None,
                "创建时间": now_iso(),
                "创建人": operator,
            }
            get_store().rows(MODULE).append(entry)
        return entry, []

    def update_entry(
        self, entry_id: int, values: dict[str, Any], *, operator: str = "值班管理员"
    ) -> tuple[dict[str, Any] | None, str]:
        """修改训练定义。历史轮次与历史评估一律保持原样，只影响之后的训练。"""
        entry = get_store().find(MODULE, entry_id)
        if entry is None:
            return None, f"预测模型 {entry_id} 不存在或已归档"
        definition, missing = _normalize_definition(values, allow_missing=False)
        if missing:
            raise DomainError(f"训练定义缺少有效字段（或仍是未确认值）：{'、'.join(missing)}")
        model_type = _text(values.get("模型类型"))
        if model_type and is_placeholder(model_type):
            raise DomainError("模型类型仍是样例文案，不能作为训练定义")
        with get_store().transaction():
            changes: list[str] = []
            if model_type and model_type != entry.get("模型类型"):
                entry["模型类型"] = model_type
                changes.append("模型类型")
            for field in DEFINITION_FIELDS:
                if entry.get(field) != definition[field]:
                    entry[field] = definition[field]
                    changes.append(field)
            if not changes:
                return entry, "训练定义没有发生变化"
            entry["定义状态"] = CONFIRMED
            entry["缺失项"] = []
            # 训练定义变了，旧生效轮次的证据不再覆盖新定义，必须重新训练。
            # 但生效归属保持原样：历史轮次、评估和数据集版本都不倒改。
            if entry.get("生效轮次编号"):
                entry["证据状态"] = UNCONFIRMED
                entry["pending"] = True
            if entry.get("status") == STATUS_ORDER[-1]:
                entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
        return entry, (
            f"训练定义已更新，变更字段：{'、'.join(changes)}；历史轮次与评估保持原样，"
            + ("需要重新训练后再发布" if entry.get("生效轮次编号") else "可提交新训练")
        )

    # ------------------------------------------------------------------ 训练轮次
    def _resolve_dataset(self, dataset_ref: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """把「训练数据集」解析成 dataset_id + 最新版本快照，并校验可用于训练。"""
        dataset_id: int | None = None
        for row in get_store().rows(DATASET_MODULE):
            if _text(row.get("数据集编号")) == dataset_ref:
                dataset_id = int(row["id"])
                break
        if dataset_id is None:
            raise DomainError(
                f"训练数据集「{dataset_ref}」在数据集管理里不存在，请先登记数据集定义"
            )
        version = dataset_versions.latest_version(dataset_id)
        if version is None:
            raise DomainError(f"训练数据集「{dataset_ref}」没有可用版本快照，不能作为训练输入")
        if version.get("定义状态") != CONFIRMED or not dataset_is_confirmed(version):
            raise DomainError(
                f"训练数据集「{dataset_ref}」v{version['version_no']} 仍含未确认字段（样例文案），"
                "不能作为训练输入；请先在数据集管理里补全定义生成新版本"
            )
        dataset = get_store().find(DATASET_MODULE, dataset_id)
        if dataset is not None and dataset.get("status") != "已生效":
            raise DomainError(
                f"训练数据集「{dataset_ref}」当前状态为「{dataset.get('status')}」，"
                "生效发布后才能作为训练输入"
            )
        snapshot = {field: version.get(field) for field in DATASET_CONTENT_FIELDS}
        return version, {
            "dataset_id": dataset_id,
            "dataset_version": int(version["version_no"]),
            "数据集快照": snapshot,
        }

    def _next_round_seq(self, entry_id: int) -> int:
        return max((int(row["轮次序号"]) for row in self.list_rounds(entry_id)), default=0) + 1

    def _new_round_no(self, entry: dict[str, Any], seq: int) -> str:
        return f"{entry.get('模型编号') or entry['id']}-R{seq:03d}"

    def submit_training(
        self, entry_id: int, values: dict[str, Any] | None = None, *, operator: str = "值班管理员"
    ) -> tuple[dict[str, Any] | None, str]:
        """提交训练：每次都是独立轮次，轮次开始时冻结训练定义与数据集版本。"""
        values = dict(values or {})
        entry = get_store().find(MODULE, entry_id)
        if entry is None:
            return None, f"预测模型 {entry_id} 不存在或已归档"
        if entry.get("status") == STATUS_ORDER[-1]:
            raise DomainError("已失效的模型不能继续提交训练；请先重新确认训练定义后再训练")

        # 本次提交可以覆盖模型当前定义；没传的沿用当前值。
        requested = {**_definition_values(entry)}
        requested.update({key: value for key, value in values.items() if key in DEFINITION_FIELDS})
        definition, missing = _normalize_definition(requested, allow_missing=False)
        if missing:
            raise DomainError(
                f"训练定义缺少有效字段（或仍是未确认值）：{'、'.join(missing)}；请补全后再提交训练"
            )

        with get_store().transaction():
            version, frozen = self._resolve_dataset(definition["训练数据集"])
            seq = self._next_round_seq(entry_id)
            round_no = self._new_round_no(entry, seq)
            now = now_iso()
            round_record = {
                "id": get_store().next_id(ROUND_MODULE),
                "轮次编号": round_no,
                "轮次序号": seq,
                "model_id": int(entry["id"]),
                "模型编号": entry.get("模型编号"),
                "模型类型": entry.get("模型类型"),
                **definition,
                "dataset_id": frozen["dataset_id"],
                "dataset_version": frozen["dataset_version"],
                "数据集版本": f"v{frozen['dataset_version']}",
                "数据集快照": frozen["数据集快照"],
                "轮次状态": ROUND_RUNNING,
                "接纳结果": None,
                "裁决说明": "训练轮次已创建，等待完成训练后录入评估",
                "数据来源": "本地训练定义（未调用外部机器学习服务）",
                "训练开始时间": now,
                "训练完成时间": None,
                "生效时间": None,
                "评估编号": None,
                "操作人": operator,
            }
            for field in DEFINITION_FIELDS:
                entry[field] = definition[field]
            entry["数据集版本"] = round_record["数据集版本"]
            entry["定义状态"] = CONFIRMED
            entry["证据状态"] = UNCONFIRMED
            entry["缺失项"] = []
            entry["当前轮次编号"] = round_no
            entry["训练轮次总数"] = seq
            if entry.get("status") == STATUS_ORDER[0]:
                entry["status"] = "待训练"
            entry["pending"] = True
            get_store().rows(ROUND_MODULE).append(round_record)
        return round_record, (
            f"已创建训练轮次 {round_no}（第 {seq} 轮），"
            f"冻结数据集 v{frozen['dataset_version']} 与五项训练定义"
        )

    def complete_round(
        self, entry_id: int, round_no: str
    ) -> tuple[dict[str, Any] | None, str]:
        """训练完成。完成不自动生效，避免跳过评估录入。"""
        entry = get_store().find(MODULE, entry_id)
        if entry is None:
            return None, f"预测模型 {entry_id} 不存在或已归档"
        round_record = self._owned_round(entry_id, round_no)
        if round_record["轮次状态"] in (ROUND_SUPERSEDED, ROUND_REJECTED):
            raise DomainError(f"轮次 {round_no} 已{round_record['轮次状态']}，不能再次完成")
        if round_record["轮次状态"] == ROUND_COMPLETED:
            return round_record, f"轮次 {round_no} 已经完成，可以录入评估"
        with get_store().transaction():
            round_record["轮次状态"] = ROUND_COMPLETED
            round_record["训练完成时间"] = now_iso()
            round_record["裁决说明"] = "训练轮次已完成，等待评估录入"
        return round_record, f"轮次 {round_no} 已完成，可以录入评估"

    # ------------------------------------------------------------------ 评估 + 生效
    def save_evaluation(
        self,
        entry_id: int,
        round_no: str,
        values: dict[str, Any],
        *,
        operator: str = "值班管理员",
        require_publish: bool = True,
    ) -> tuple[dict[str, Any] | None, str]:
        """保存评估并推进模型生效；两者是一个不可拆的结果。

        评估写入、模型生效、被取代轮次的标记都在同一个 ``store.transaction()`` 里，
        落库失败整体回滚：评估不会留下半条记录，模型保持原来的生效版本。

        ``require_publish=False`` 仅供内部留档路径使用；对外入口一律走原子路径。
        """
        entry = get_store().find(MODULE, entry_id)
        if entry is None:
            return None, f"预测模型 {entry_id} 不存在或已归档"
        round_record = self._owned_round(entry_id, round_no)

        metric_name, metric_value = _metric(values)
        if not metric_name:
            raise DomainError("评估指标名称缺失或仍是样例文案，无法保存评估")
        if metric_value in (None, ""):
            raise DomainError("评估值缺失，无法保存评估")
        if is_placeholder(metric_value):
            raise DomainError("评估值仍是样例文案，不能作为评估证据")

        if not require_publish:
            if round_record["轮次状态"] in (ROUND_SUPERSEDED, ROUND_REJECTED):
                raise DomainError(f"轮次 {round_no} 已{round_record['轮次状态']}，评估不能落到该轮次上")
            if round_record["轮次状态"] != ROUND_COMPLETED:
                raise DomainError(f"轮次 {round_no} 仍在训练中，完成训练后才能录入评估")

        # 单事务：评估落库 + 模型生效 + 被取代轮次标记，一起提交或一起回滚。
        # 生效裁决也在事务内，避免「检查通过后、写盘前」被另一个请求插入。
        with get_store().transaction():
            if require_publish:
                reason = self._rejection_reason(entry, round_record)
                if reason is not None:
                    # 只落拒绝标记，不写评估；这一小步单独提交，方便事后审计。
                    round_record["接纳结果"] = REJECTED
                    round_record["裁决说明"] = reason
                    round_record["轮次状态"] = ROUND_REJECTED
                    rejection = reason
                else:
                    rejection = None
            else:
                rejection = None

            if rejection is None:
                existing = self._evaluation_of_round(round_no)
                if existing is not None:
                    existing.update({
                        "评估指标": metric_name,
                        "评估值": metric_value,
                        "结论": _text(values.get("结论")) or existing.get("结论"),
                        "录入时间": now_iso(),
                        "录入人": operator,
                    })
                    evaluation = existing
                    message = f"轮次 {round_no} 的评估已更新"
                else:
                    evaluation = {
                        "id": get_store().next_id(EVALUATION_MODULE),
                        "评估编号": self._next_evaluation_no(round_no),
                        "轮次编号": round_no,
                        "模型编号": entry.get("模型编号"),
                        "评估指标": metric_name,
                        "评估值": metric_value,
                        "结论": _text(values.get("结论")) or "评估已录入",
                        "数据集版本": round_record.get("数据集版本"),
                        "数据集快照": round_record.get("数据集快照"),
                        "目标字段": round_record.get("目标字段"),
                        "特征字段": round_record.get("特征字段"),
                        "预测步长": round_record.get("预测步长"),
                        "置信区间": round_record.get("置信区间"),
                        "录入时间": now_iso(),
                        "录入人": operator,
                    }
                    get_store().rows(EVALUATION_MODULE).append(evaluation)
                    round_record["评估编号"] = evaluation["评估编号"]
                    message = f"轮次 {round_no} 的评估已保存"

                entry["评估指标"] = evaluation["评估指标"]
                entry["评估值"] = evaluation["评估值"]

                if require_publish:
                    self._apply_publish(entry, round_record, evaluation)
                    message += "，并已推进模型生效"
                else:
                    entry["pending"] = entry.get("当前轮次编号") == round_no
        if rejection is not None:
            raise DomainError(rejection)
        return evaluation, message

    def _rejection_reason(self, entry: dict[str, Any], round_record: dict[str, Any]) -> str | None:
        """判断轮次能否成为生效依据；返回 None 表示可以接纳。"""
        if round_record["轮次状态"] == ROUND_SUPERSEDED:
            return f"轮次 {round_record['轮次编号']} 已作废，不能作为生效依据"
        if round_record["轮次状态"] == ROUND_REJECTED:
            return f"轮次 {round_record['轮次编号']} 已被拒绝过，不能作为生效依据"
        if round_record["轮次状态"] != ROUND_COMPLETED:
            return f"轮次 {round_record['轮次编号']} 仍在训练中，完成训练后才能保存评估并生效"

        seq = int(round_record["轮次序号"])

        # 已经确认生效的更新轮次优先：这时迟到结果的特征最明确。
        effective_no = entry.get("生效轮次编号")
        if effective_no:
            effective_round = self.get_round(str(effective_no))
            if effective_round is not None and int(effective_round["轮次序号"]) > seq:
                return (
                    f"迟到结果：轮次序号 {seq} 早于已生效轮次 "
                    f"{int(effective_round['轮次序号'])}，不能顶掉用户已经确认的新结果"
                )

        valid = self.valid_round(int(entry["id"]))
        if valid is not None and valid["轮次编号"] != round_record["轮次编号"]:
            valid_seq = int(valid["轮次序号"])
            if valid_seq > seq:
                return (
                    f"轮次 {round_record['轮次编号']}（第 {seq} 轮）不是当前有效轮次："
                    f"同时完成的更晚轮次 {valid['轮次编号']}（第 {valid_seq} 轮）已就绪，"
                    "按有效轮次接纳"
                )
            if valid_seq == seq:
                return f"轮次序号 {seq} 已被 {valid['轮次编号']} 占用，不能重复接纳"
        return None

    def publish(
        self,
        entry_id: int,
        *,
        round_no: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """推进生效：只接受已完成、已有评估、且属于当前有效轮次的记录。"""
        entry = get_store().find(MODULE, entry_id)
        if entry is None:
            return None, f"预测模型 {entry_id} 不存在或已归档"
        if entry.get("status") == STATUS_ORDER[-1]:
            raise DomainError("已失效的模型不能直接生效发布；请重新训练并保存评估")

        round_no = round_no or entry.get("当前轮次编号")
        if not round_no:
            raise DomainError(LEGACY_PUBLISH_ERROR)
        round_record = self._owned_round(entry_id, str(round_no))

        evaluation = self._evaluation_of_round(str(round_no))
        if evaluation is None:
            raise DomainError(
                f"轮次 {round_no} 还没有评估记录；保存评估后才能推进生效，"
                "或直接使用「保存评估并生效」一步完成"
            )
        with get_store().transaction():
            reason = self._rejection_reason(entry, round_record)
            if reason is not None:
                round_record["接纳结果"] = REJECTED
                round_record["裁决说明"] = reason
                round_record["轮次状态"] = ROUND_REJECTED
                rejection = reason
            else:
                rejection = None
                self._apply_publish(entry, round_record, evaluation)
        if rejection is not None:
            raise DomainError(rejection)
        return entry, f"轮次 {round_no} 已成为模型生效版本（评估 {evaluation['评估编号']}）"

    def _apply_publish(
        self,
        entry: dict[str, Any],
        round_record: dict[str, Any],
        evaluation: dict[str, Any],
    ) -> None:
        """把轮次接纳为生效依据。调用方必须已在事务内，且已通过 _rejection_reason 检查。"""
        current_no = entry.get("生效轮次编号")
        current_round = self.get_round(str(current_no)) if current_no else None
        if (
            current_round is not None
            and current_round["轮次编号"] != round_record["轮次编号"]
            and current_round.get("轮次状态") == ROUND_COMPLETED
        ):
            # 更晚的轮次覆盖旧生效轮次；旧轮次只标为被取代，历史评估保留可查。
            current_round["接纳结果"] = SUPERSEDED
            current_round["轮次状态"] = ROUND_SUPERSEDED
            current_round["裁决说明"] = (
                f"被更晚的有效轮次 {round_record['轮次编号']} 取代，历史评估保留可查"
            )

        now = now_iso()
        round_record["轮次状态"] = ROUND_COMPLETED
        round_record["接纳结果"] = ACCEPTED
        round_record["裁决说明"] = f"轮次 {round_record['轮次编号']} 按有效轮次接纳为模型生效版本"
        round_record["生效时间"] = now
        round_record["评估编号"] = evaluation["评估编号"]

        entry["status"] = "已生效"
        entry["pending"] = False
        entry["abnormal"] = False
        entry["生效轮次编号"] = round_record["轮次编号"]
        entry["生效轮次记录"] = _round_summary(round_record)
        entry["生效评估编号"] = evaluation["评估编号"]
        entry["生效评估记录"] = _evaluation_summary(evaluation)
        entry["生效时间"] = now
        entry["评估指标"] = evaluation["评估指标"]
        entry["评估值"] = evaluation["评估值"]
        entry["证据状态"] = CONFIRMED
        entry["数据集版本"] = round_record.get("数据集版本")
        entry["当前轮次编号"] = round_record["轮次编号"]
        entry["训练轮次总数"] = max(
            int(entry.get("训练轮次总数") or 0), int(round_record["轮次序号"])
        )
        # 模型上的定义跟随当前生效轮次，确保「生效版本是哪一版训练」一目了然。
        for field in DEFINITION_FIELDS:
            entry[field] = round_record.get(field)

    def invalidate(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        entry = get_store().find(MODULE, entry_id)
        if entry is None:
            return None, f"预测模型 {entry_id} 不存在或已归档"
        with get_store().transaction():
            entry["status"] = "已失效"
            entry["pending"] = False
            entry["abnormal"] = True
        return entry, "预测模型已标记失效；生效轮次与评估记录继续留档"

    def run_action(
        self, entry_id: int, action: str, values: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any] | None, str]:
        """兼容既有动作入口：提交训练 / 保存评估 / 生效发布 / 标记失效。"""
        values = dict(values or {})
        if action == ACTION_SUBMIT_TRAINING:
            return self.submit_training(entry_id, values)
        if action == ACTION_PUBLISH:
            return self.publish(entry_id, round_no=_text(values.get("round_no")) or None)
        if action == ACTION_INVALIDATE:
            return self.invalidate(entry_id)
        if action == ACTION_SAVE_EVALUATION:
            round_no = _text(values.get("round_no") or values.get("轮次编号"))
            if not round_no:
                raise DomainError("保存评估必须指明训练轮次 round_no")
            return self.save_evaluation(
                entry_id,
                round_no,
                values,
                require_publish=bool(values.get("publish", True)),
            )
        raise DomainError(f"动作「{action}」不属于趋势预测可执行范围")

    # ------------------------------------------------------------------ 内部工具
    def _owned_round(self, entry_id: int, round_no: str) -> dict[str, Any]:
        for row in get_store().rows(ROUND_MODULE):
            if row.get("轮次编号") == round_no and int(row.get("model_id", 0)) == entry_id:
                return row
        raise DomainError(f"轮次 {round_no} 不属于预测模型 {entry_id}，或不存在")

    def _next_evaluation_no(self, round_no: str) -> str:
        prefix = f"{round_no}-E"
        used = {
            str(row.get("评估编号", "")) for row in get_store().rows(EVALUATION_MODULE)
        }
        seq = 1
        while f"{prefix}{seq:03d}" in used:
            seq += 1
        return f"{prefix}{seq:03d}"

    def _evaluation_of_round(self, round_no: str) -> dict[str, Any] | None:
        for row in get_store().rows(EVALUATION_MODULE):
            if row.get("轮次编号") == round_no:
                return row
        return None


forecast_service = ForecastService()
