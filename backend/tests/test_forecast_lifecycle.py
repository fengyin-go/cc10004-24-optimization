"""趋势预测训练定义 / 评估留存 / 冲突裁决的行为测试。

覆盖：
1. 训练轮次冻结数据集版本与五项定义；
2. 数据集改名后旧轮次、旧评估不被倒改；
3. 重启（新 Store 读同一份文件）后仍能说明评估来自哪一版训练；
4. 并发完成按有效轮次接纳，迟到结果不能顶掉已确认的新结果；
5. 评估与生效同一事务，落库失败整体回滚；
6. 存量迁移把缺项标为未确认，不伪造轮次/评估。
"""
from __future__ import annotations

import copy
import json

import pytest

from app.services.datastore import DatastoreService
from app.services.errors import DomainError
from app.services.forecast import ForecastService
from app.store import Store, StoreWriteError


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """每个用例独立的数据文件，并把全局 store 换成隔离实例。"""
    import app.store as store_module

    isolated = Store(tmp_path / "store.json")
    monkeypatch.setattr(store_module, "store", isolated)
    yield isolated, DatastoreService(), ForecastService()
    isolated.persist_enabled = False


def _entry(values: dict) -> dict:
    payload = {
        "模型编号": "FORE-T01",
        "模型类型": "时序回归",
        **values,
    }
    return payload


def _prepare_dataset(datastores: DatastoreService, isolated: Store) -> dict:
    """把 DATA-0001 改成已确认定义并生成新版本。"""
    entry = datastores.get_entry(1)
    assert entry is not None
    ok, message = datastores.update_entry(1, {
        "数据集编号": "DATA-0001",
        "数据集名称": "订单销量数据集",
        "来源表名": "dwd_order_sales",
        "字段列表": "ds,y,sales,region",
        "更新策略": "每日增量",
    })
    assert ok is not None and "v2" in message
    assert datastores.run_action(1, "生效发布")[0]["status"] == "已生效"
    return entry


def _create_model(forecasts: ForecastService) -> dict:
    entry, missing = forecasts.create_entry(_entry({
        "训练数据集": "DATA-0001",
        "目标字段": "sales",
        "特征字段": "region,weekday",
        "预测步长": "7",
        "置信区间": "0.95",
    }))
    assert missing == []
    return entry


def test_training_round_freezes_definition_and_dataset_version(workspace):
    isolated, datastores, forecasts = workspace
    _prepare_dataset(datastores, isolated)
    model = _create_model(forecasts)

    round_record, message = forecasts.submit_training(model["id"])
    assert "FORE-T01-R001" in message
    assert round_record["dataset_version"] == 2
    assert round_record["数据集版本"] == "v2"
    assert round_record["目标字段"] == "sales"
    assert round_record["置信区间"] == "0.95"
    assert round_record["数据集快照"]["数据集名称"] == "订单销量数据集"

    forecasts.complete_round(model["id"], round_record["轮次编号"])
    evaluation, message = forecasts.save_evaluation(
        model["id"], round_record["轮次编号"],
        {"评估指标": "MAE", "评估值": 12.5, "结论": "误差可接受"},
    )
    assert evaluation["评估指标"] == "MAE"
    assert "并已推进模型生效" in message
    detail = forecasts.detail(model["id"])
    assert detail["status"] == "已生效"
    assert detail["生效轮次"]["轮次编号"] == "FORE-T01-R001"
    assert detail["生效轮次"]["数据集版本"] == "v2"
    assert detail["生效评估"]["评估值"] == 12.5


def test_dataset_rename_does_not_rewrite_history(workspace):
    isolated, datastores, forecasts = workspace
    _prepare_dataset(datastores, isolated)
    model = _create_model(forecasts)
    round_record, _ = forecasts.submit_training(model["id"])
    forecasts.complete_round(model["id"], round_record["轮次编号"])
    forecasts.save_evaluation(
        model["id"], round_record["轮次编号"], {"评估指标": "MAE", "评估值": 9.0}
    )
    old_round = copy.deepcopy(round_record)
    old_detail = copy.deepcopy(forecasts.detail(model["id"]))

    # 数据集跨模块改名 + 更新字段 -> 追加 v3。
    _, message = datastores.update_entry(1, {
        "数据集名称": "订单销量数据集（新口径）",
        "字段列表": "ds,y,sales,region,channel",
    })
    assert "v3" in message

    new_round = forecasts.get_round("FORE-T01-R001")
    assert new_round["数据集快照"] == old_round["数据集快照"]
    assert new_round["dataset_version"] == 2
    detail = forecasts.detail(model["id"])
    assert detail["生效评估记录"] == old_detail["生效评估记录"]
    assert detail["生效轮次记录"]["数据集快照"] == old_detail["生效轮次记录"]["数据集快照"]

    # 再次训练才会带上 v3，并且是独立轮次。
    second, _ = forecasts.submit_training(model["id"])
    assert second["轮次编号"] == "FORE-T01-R002"
    assert second["dataset_version"] == 3


def test_restart_recovers_round_ownership(workspace, tmp_path):
    isolated, datastores, forecasts = workspace
    _prepare_dataset(datastores, isolated)
    model = _create_model(forecasts)
    round_record, _ = forecasts.submit_training(model["id"])
    forecasts.complete_round(model["id"], round_record["轮次编号"])
    forecasts.save_evaluation(
        model["id"], round_record["轮次编号"], {"评估指标": "MAPE", "评估值": 0.08}
    )

    # 模拟重启：新实例读同一份文件。
    reopened = Store(isolated.path)
    reopened_model = reopened.find("forecast", model["id"])
    assert reopened_model["status"] == "已生效"
    assert reopened_model["生效轮次编号"] == "FORE-T01-R001"
    assert reopened_model["生效评估记录"]["评估指标"] == "MAPE"
    assert reopened_model["生效轮次记录"]["数据集版本"] == "v2"
    # 迁移后的缺失项不会被重新生成轮次。
    assert reopened.rows("forecast_rounds")[0]["轮次编号"] == "FORE-T01-R001"


def test_concurrent_rounds_use_effective_round_and_reject_late_results(workspace):
    isolated, datastores, forecasts = workspace
    _prepare_dataset(datastores, isolated)
    model = _create_model(forecasts)

    first, _ = forecasts.submit_training(model["id"])
    second, _ = forecasts.submit_training(model["id"])
    forecasts.complete_round(model["id"], first["轮次编号"])
    forecasts.complete_round(model["id"], second["轮次编号"])

    # 先落地的反而是较晚轮次 -> 接纳为生效版本。
    _, message = forecasts.save_evaluation(
        model["id"], second["轮次编号"], {"评估指标": "MAE", "评估值": 7.0}
    )
    assert "已推进模型生效" in message
    # 迟到的低序号轮次不能顶掉用户已确认的新结果。
    with pytest.raises(DomainError) as error:
        forecasts.save_evaluation(
            model["id"], first["轮次编号"], {"评估指标": "MAE", "评估值": 5.0}
        )
    assert "迟到结果" in str(error.value)
    # 评估与生效在同一事务，被拒绝时没有留下评估记录。
    assert not any(
        row["轮次编号"] == first["轮次编号"] for row in isolated.rows("forecast_evaluations")
    )
    detail = forecasts.detail(model["id"])
    assert detail["生效轮次"]["轮次编号"] == second["轮次编号"]
    assert detail["生效评估"]["评估值"] == 7.0
    rejected = forecasts.get_round(first["轮次编号"])
    # 拒绝标记与评估+生效在同一事务原子提交，不会出现半条评估。
    assert rejected["接纳结果"] == "已拒绝"
    assert "迟到结果" in rejected["裁决说明"]


def test_zero_sum_when_older_round_completes_after_newer(workspace):
    """较晚轮次后完成也算有效轮次；先完成的不再具备生效资格。"""
    isolated, datastores, forecasts = workspace
    _prepare_dataset(datastores, isolated)
    model = _create_model(forecasts)
    first, _ = forecasts.submit_training(model["id"])
    second, _ = forecasts.submit_training(model["id"])
    forecasts.complete_round(model["id"], second["轮次编号"])
    forecasts.complete_round(model["id"], first["轮次编号"])

    with pytest.raises(DomainError) as error:
        forecasts.save_evaluation(
            model["id"], first["轮次编号"], {"评估指标": "MAE", "评估值": 4.0}
        )
    assert "有效轮次" in str(error.value)
    assert forecasts.detail(model["id"])["status"] != "已生效"


def test_persist_failure_rolls_back_evaluation_and_keeps_previous_version(workspace):
    isolated, datastores, forecasts = workspace
    _prepare_dataset(datastores, isolated)
    model = _create_model(forecasts)

    # 第一轮正常生效。
    round_one, _ = forecasts.submit_training(model["id"])
    forecasts.complete_round(model["id"], round_one["轮次编号"])
    forecasts.save_evaluation(
        model["id"], round_one["轮次编号"], {"评估指标": "MAE", "评估值": 9.0}
    )
    active_before = forecasts.detail(model["id"])["生效评估记录"]

    # 第二轮在落库时失败。
    round_two, _ = forecasts.submit_training(model["id"])
    forecasts.complete_round(model["id"], round_two["轮次编号"])
    isolated.fail_next_persist = True
    with pytest.raises(StoreWriteError):
        forecasts.save_evaluation(
            model["id"], round_two["轮次编号"], {"评估指标": "MAE", "评估值": 6.0}
        )

    detail = forecasts.detail(model["id"])
    assert detail["生效轮次"]["轮次编号"] == round_one["轮次编号"]
    assert detail["生效评估记录"] == active_before
    # 第二轮也不应该留下半条评估。
    assert not any(
        row["轮次编号"] == round_two["轮次编号"] for row in isolated.rows("forecast_evaluations")
    )
    # 内存与磁盘保持一致：重新读盘后依旧还是旧生效版本。
    reopened = Store(isolated.path)
    assert reopened.find("forecast", model["id"])["生效轮次编号"] == round_one["轮次编号"]


def test_legacy_models_are_marked_unconfirmed_without_fake_evidence(workspace):
    isolated, datastores, forecasts = workspace
    legacy = forecasts.get_entry(3)
    assert legacy["定义状态"] == "未确认"
    assert legacy["证据状态"] == "未确认"
    assert legacy["生效轮次编号"] is None
    assert legacy["生效轮次记录"] is None
    assert legacy["评估指标"] == "未确认"
    assert "训练数据集" in legacy["缺失项"]

    # 存量「已生效」模型没有证据，不能直接再次发布。
    with pytest.raises(DomainError):
        forecasts.publish(3)
    assert isolated.rows("forecast_rounds") == []
    assert isolated.rows("forecast_evaluations") == []

    # 未确认的数据集不能作为训练输入。
    with pytest.raises(DomainError) as error:
        forecasts.submit_training(3, {
            "训练数据集": "DATA-0001",
            "目标字段": "sales",
            "特征字段": "region",
            "预测步长": "7",
            "置信区间": "0.95",
            "模型类型": "时序回归",
        })
    assert "未确认" in str(error.value)


def test_placeholder_text_is_not_accepted_as_evidence(workspace):
    isolated, datastores, forecasts = workspace
    _prepare_dataset(datastores, isolated)
    model = _create_model(forecasts)
    round_record, _ = forecasts.submit_training(model["id"])
    forecasts.complete_round(model["id"], round_record["轮次编号"])

    with pytest.raises(DomainError):
        forecasts.save_evaluation(
            model["id"], round_record["轮次编号"],
            {"评估指标": "趋势预测样例1", "评估值": 3.0},
        )
    with pytest.raises(DomainError):
        forecasts.save_evaluation(
            model["id"], round_record["轮次编号"],
            {"评估指标": "MAE", "评估值": "趋势预测样例1"},
        )


def test_snapshot_file_contains_round_and_evaluation(workspace):
    isolated, datastores, forecasts = workspace
    _prepare_dataset(datastores, isolated)
    model = _create_model(forecasts)
    round_record, _ = forecasts.submit_training(model["id"])
    forecasts.complete_round(model["id"], round_record["轮次编号"])
    forecasts.save_evaluation(
        model["id"], round_record["轮次编号"], {"评估指标": "RMSE", "评估值": 4.2}
    )
    snapshot = json.loads(isolated.path.read_text(encoding="utf-8"))
    assert snapshot["schema_version"] == 2
    assert snapshot["tables"]["forecast_rounds"][0]["轮次编号"] == "FORE-T01-R001"
    assert snapshot["tables"]["forecast_evaluations"][0]["评估指标"] == "RMSE"
    assert "生效轮次编号" in snapshot["tables"]["forecast"][3]


def test_legacy_active_model_is_downgraded_and_keeps_audit_trail(workspace):
    isolated, datastores, forecasts = workspace
    legacy = forecasts.get_entry(3)
    assert legacy["status"] == "待训练"
    assert legacy["迁移前状态"] == "已生效"
    assert "降回待训练" in legacy["迁移说明"]
    assert legacy["评估指标"] == "未确认"
    # 迁移是幂等的：不会重复生成缺失项或再改一次状态。
    reopened = Store(isolated.path)
    again = reopened.find("forecast", 3)
    assert again["status"] == "待训练"
    assert again["缺失项"] == legacy["缺失项"]


def test_dataset_version_append_is_additive(workspace):
    isolated, datastores, forecasts = workspace
    _prepare_dataset(datastores, isolated)
    versions = datastores.versions(1)
    assert [v["version_no"] for v in versions] == [1, 2]
    original = copy.deepcopy(versions[0])
    _, message = datastores.update_entry(1, {"字段列表": "ds,y,sales,region,channel"})
    assert "v3" in message
    versions = datastores.versions(1)
    assert [v["version_no"] for v in versions] == [1, 2, 3]
    assert versions[0]["数据集名称"] == original["数据集名称"]
    assert versions[0]["字段列表"] == original["字段列表"]
    assert versions[2]["变更前"]["字段列表"] == "ds,y,sales,region"
    assert versions[2]["变更后"]["字段列表"] == "ds,y,sales,region,channel"


def test_unconfirmed_dataset_cannot_be_published(workspace):
    isolated, datastores, forecasts = workspace
    with pytest.raises(DomainError):
        datastores.run_action(2, "生效发布")
    assert datastores.get_entry(2)["status"] == "待审核"


def test_active_definition_edit_requires_retraining(workspace):
    isolated, datastores, forecasts = workspace
    _prepare_dataset(datastores, isolated)
    model = _create_model(forecasts)
    round_record, _ = forecasts.submit_training(model["id"])
    forecasts.complete_round(model["id"], round_record["轮次编号"])
    forecasts.save_evaluation(
        model["id"], round_record["轮次编号"], {"评估指标": "MAE", "评估值": 6.0}
    )
    # 改定义后，生效归属保留，但证据状态需要重新训练来确认。
    _, message = forecasts.update_entry(model["id"], {
        "训练数据集": "DATA-0001",
        "目标字段": "sales",
        "特征字段": "region,channel",
        "预测步长": "14",
        "置信区间": "0.9",
        "模型类型": "时序回归",
    })
    detail = forecasts.detail(model["id"])
    assert detail["生效轮次"]["轮次编号"] == round_record["轮次编号"]
    assert detail["生效轮次"]["预测步长"] == "7"  # 旧轮次保持不变
    assert detail["证据状态"] == "未确认"
    assert "重新训练" in message
