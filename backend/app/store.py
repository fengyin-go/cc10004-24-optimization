"""持久化数据仓库：内存里放业务表，落盘用一份 JSON 快照。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，但保留了数据库最关键的
两个语义：

* 重启恢复：进程退出后，下次启动从同一个文件恢复，不重新拿示例数据覆盖。
* 事务：``Store.transaction()`` 里的修改要么全部写盘成功，要么一律回滚。

路径默认是 ``backend/data/store.json``，可以用 ``APP_DATA_FILE`` 环境变量覆盖。
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app.seed import SEED_ROWS

SCHEMA_VERSION = 2

# 这些表是业务模块，运营概览和外层只暴露它们。
PUBLIC_MODULES = tuple(SEED_ROWS)

# 这些是支撑版本归属和评估留存的内部表，不作为业务模块展示。
INTERNAL_TABLES = ("datastore_versions", "forecast_rounds", "forecast_evaluations")

DATASET_CONTENT_FIELDS = [
    "数据集编号",
    "数据集名称",
    "来源表名",
    "字段列表",
    "更新策略",
    "数据采样比例",
    "生命周期天数",
    "缓存开关",
]

# 判断数据集定义是否「已确认」的关键字段：只要求这几项脱离样例文案。
DATASET_CONFIRM_FIELDS = ["数据集编号", "数据集名称", "来源表名", "字段列表"]

FORECAST_PLACEHOLDER_MARKER = "趋势预测样例"
UNCONFIRMED = "未确认"
CONFIRMED = "已确认"


class StoreWriteError(RuntimeError):
    """写盘失败。调用方必须保证内存状态已经回滚到事务开始之前。"""


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def default_data_file() -> Path:
    configured = os.environ.get("APP_DATA_FILE")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parent.parent / "data" / "store.json"


def is_placeholder(value: Any) -> bool:
    """存量数据里的样例文案、以及迁移时标出的「未确认」都不能当真实训练证据。"""
    text = str(value or "").strip()
    if not text:
        return True
    if text == UNCONFIRMED:
        return True
    return "样例" in text or "示例" in text or text in {"sample", "demo"}


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _empty_tables() -> dict[str, list[dict[str, Any]]]:
    tables: dict[str, list[dict[str, Any]]] = {
        name: [copy.deepcopy(row) for row in rows] for name, rows in SEED_ROWS.items()
    }
    for name in INTERNAL_TABLES:
        tables[name] = []
    return tables


def dataset_is_confirmed(definition: dict[str, Any]) -> bool:
    """数据集定义是否可用于训练：关键字段都不能还是样例文案。"""
    return all(not is_placeholder(definition.get(field)) for field in DATASET_CONFIRM_FIELDS)


def _seed_datastore_versions(tables: dict[str, list[dict[str, Any]]]) -> None:
    """为存量数据集补一份 v1 版本快照。

    版本快照一旦写入就不再回改，后续改名/改字段只会追加新版本。
    """
    if tables["datastore_versions"]:
        return
    now = now_iso()
    versions: list[dict[str, Any]] = []
    for row in tables["datastore"]:
        row.setdefault("当前版本", 1)
        row.setdefault("更新时间", None)
        content = {field: row.get(field) for field in DATASET_CONTENT_FIELDS}
        row.setdefault("定义状态", CONFIRMED if dataset_is_confirmed(row) else UNCONFIRMED)
        versions.append({
            "id": _next_id(versions),
            "dataset_id": int(row.get("id", 0)),
            "version_no": int(row.get("当前版本", 1)),
            **content,
            "定义状态": row["定义状态"],
            "变更类型": "存量初始化",
            "变更字段": [],
            "变更前": {},
            "变更后": content,
            "创建时间": now,
            "操作人": "system",
        })
    tables["datastore_versions"] = versions


def _migrate_definitions(tables: dict[str, list[dict[str, Any]]], meta: dict[str, Any]) -> bool:
    """迁移存量预测模型：缺项标为未确认，不伪造轮次和评估证据。

    幂等：只有尚未迁移过的旧行（没有 ``定义状态``）才会被处理。
    存量行即使状态是「已生效」，也没有任何可核验的训练轮次与评估记录，因此证据状态
    只能是未确认；生效归属留空，不拿示例文案补位。
    """
    changed = False
    definition_fields = ["模型类型", "训练数据集", "目标字段", "特征字段", "预测步长", "置信区间"]
    now = now_iso()
    for row in tables["forecast"]:
        if "定义状态" in row:
            continue
        missing: list[str] = []
        for field in definition_fields:
            if is_placeholder(row.get(field)):
                row[field] = UNCONFIRMED
                missing.append(field)
        # 存量行的「评估指标」是样例文案，不能当作已有评估结果。
        row["评估指标"] = UNCONFIRMED
        missing.append("评估指标")
        row["定义状态"] = UNCONFIRMED
        row["证据状态"] = UNCONFIRMED
        row["生效轮次编号"] = None
        row["生效轮次记录"] = None
        row["生效评估记录"] = None
        row["生效时间"] = None
        row["评估值"] = None
        row["数据集版本"] = None
        row["缺失项"] = sorted(set(missing))
        row["迁移说明"] = "存量模型没有可核验的训练轮次与评估记录，缺项标记为未确认"
        row["当前轮次编号"] = None
        row["训练轮次总数"] = 0
        # 没有证据的「已生效」是一种自相矛盾的状态：保留原状态供审计，
        # 但降回「待训练」，避免把样例文案当成生效依据展示。
        if row.get("status") == "已生效":
            row["迁移前状态"] = "已生效"
            row["status"] = "待训练"
            row["pending"] = True
            row["迁移说明"] = (
                "存量模型标注为已生效，但没有可核验的训练轮次与评估记录，"
                "已降回待训练并把缺项标记为未确认"
            )
        changed = True
    if changed:
        meta["forecast_migrated_at"] = now
    return changed


def _normalize(tables: dict[str, list[dict[str, Any]]], meta: dict[str, Any]) -> bool:
    changed = False
    for module in PUBLIC_MODULES:
        if not isinstance(tables.get(module), list):
            tables[module] = [copy.deepcopy(row) for row in SEED_ROWS[module]]
            changed = True
    for module in INTERNAL_TABLES:
        if not isinstance(tables.get(module), list):
            tables[module] = []
            changed = True
    if _seed_datastore_versions(tables):
        changed = True
    if _migrate_definitions(tables, meta):
        changed = True
    return changed


class Store:
    def __init__(self, path: str | Path | None = None, *, persist: bool = True) -> None:
        self.path = Path(path) if path is not None else default_data_file()
        self.persist_enabled = persist
        self.fail_next_persist = False
        self._lock = threading.RLock()
        self._tables: dict[str, list[dict[str, Any]]] = {}
        self._meta: dict[str, Any] = {}
        self._load_or_bootstrap()

    # ------------------------------------------------------------------ 读
    def module_names(self) -> list[str]:
        with self._lock:
            return sorted(name for name in self._tables if name not in INTERNAL_TABLES)

    def rows(self, module: str) -> list[dict[str, Any]]:
        with self._lock:
            return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        with self._lock:
            for row in self.rows(module):
                if int(row.get("id", 0)) == entry_id:
                    return row
        return None

    def next_id(self, module: str) -> int:
        with self._lock:
            return _next_id(self.rows(module))

    def meta(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._meta.get(key, default)

    @property
    def schema_version(self) -> int:
        with self._lock:
            return int(self._meta.get("schema_version", SCHEMA_VERSION))

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}

    # ------------------------------------------------------------------ 事务
    @contextmanager
    def transaction(self) -> Iterator["Store"]:
        """整体提交或整体回滚。

        ``yield`` 之后先写盘，写盘成功才算提交；任何异常（包括 ``StoreWriteError``）
        都会把整份内存表恢复成事务前的快照，因此不会出现「评估已保存但模型没生效」
        或反过来的一半状态。
        """
        with self._lock:
            backup_tables = copy.deepcopy(self._tables)
            backup_meta = copy.deepcopy(self._meta)
            try:
                yield self
                self._persist()
            except BaseException:
                self._tables = backup_tables
                self._meta = backup_meta
                raise

    # ------------------------------------------------------------------ 落盘
    def _load_or_bootstrap(self) -> None:
        with self._lock:
            if self.persist_enabled and self.path.exists():
                self._load()
                changed = _normalize(self._tables, self._meta)
                if changed:
                    self._persist()
                return
            self._tables = _empty_tables()
            self._meta = {"schema_version": SCHEMA_VERSION, "created_at": now_iso()}
            _normalize(self._tables, self._meta)
            if self.persist_enabled:
                self._persist()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StoreWriteError(f"数据文件 {self.path} 无法读取：{error}") from error
        if isinstance(raw, dict) and isinstance(raw.get("tables"), dict):
            self._tables = raw["tables"]
            self._meta = dict(raw.get("meta") or {})
        elif isinstance(raw, dict):
            # 兼容最早期的「表名 -> 行数组」文件格式。
            self._tables = {k: v for k, v in raw.items() if isinstance(v, list)}
            self._meta = {}
        else:
            raise StoreWriteError(f"数据文件 {self.path} 结构无法识别")
        self._meta.setdefault("schema_version", SCHEMA_VERSION)

    def _persist(self) -> None:
        if not self.persist_enabled:
            return
        if self.fail_next_persist:
            self.fail_next_persist = False
            raise StoreWriteError("落库失败：存储不可写，本次变更已回滚")
        payload = {
            "schema_version": SCHEMA_VERSION,
            "saved_at": now_iso(),
            "meta": self._meta,
            "tables": self._tables,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(prefix=".store-", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, self.path)
        except BaseException as error:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            if isinstance(error, (OSError, TypeError, ValueError)):
                raise StoreWriteError(f"落库失败：{error}") from error
            raise

    def reload(self) -> None:
        """从磁盘重新加载（测试重启恢复时可直接调用）。"""
        with self._lock:
            self._load()
            _normalize(self._tables, self._meta)


store = Store()


def get_store() -> Store:
    """服务层统一通过这里取仓库。

    读到的是模块当前引用，测试或运维脚本替换 ``app.store.store`` 后立即生效，
    不需要逐个模块重绑。
    """
    return store

