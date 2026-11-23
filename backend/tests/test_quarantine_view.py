"""隔离视图与补写清单测试：质量门禁异常明细的定位、迟到更正与定向补写。

场景主线：订单 7 同时因金额和客户号进入隔离；值班选中后金额更正回执
迟到，客户号仍有缺值，另有一笔已核清的异常也在此次选择里。验证有效
补写不覆盖已确认的新金额、同一订单只补成一行、分区回填续办领取待补
字段，以及重跑门禁后快照里订单 7 只有一条修正明细。
"""
from __future__ import annotations

import pytest

from app.fusion_engine import FusionEngine
from app.quarantine import QuarantineManager


@pytest.fixture
def engine() -> FusionEngine:
    return FusionEngine()


def _build_order_engine() -> FusionEngine:
    """订单宽表 + 金额/客户号两道缺值门禁。"""
    eng = FusionEngine()
    eng.define_wide_table(
        table_code="WIDE-ORDER", table_name="订单宽表",
        field_list=["订单号", "金额", "客户号"], primary_key="订单号",
    )
    eng.create_quality_gate(
        target_wide_table="订单宽表", check_type="null_check",
        threshold_condition="金额", trigger_action="block", block_level="blocking",
    )
    eng.create_quality_gate(
        target_wide_table="订单宽表", check_type="null_check",
        threshold_condition="客户号", trigger_action="block", block_level="blocking",
    )
    return eng


def _order_rows() -> list[dict]:
    return [
        {"订单号": "ORD-006", "金额": 66.0, "客户号": "C006"},
        {"订单号": "ORD-007", "金额": None, "客户号": None},
        {"订单号": "ORD-008", "金额": None, "客户号": "C008"},
    ]


class TestQuarantineView:
    """隔离视图：门禁违规行的明细定位。"""

    def test_gate_records_quarantine_entries_per_field(self) -> None:
        """门禁不再只数缺值：订单 7 因金额和客户号各产生一条隔离行。"""
        eng = _build_order_engine()
        result = eng.run_quality_gate("订单宽表", _order_rows(),
                                      release_version="REL-2026.11")
        assert result["passed"] is False
        assert len(result["quarantine_entries"]) == 3

        view = eng.quarantine_view("订单宽表", row_key="ORD-007")
        assert len(view) == 2
        assert {e["field"] for e in view} == {"金额", "客户号"}
        for entry in view:
            assert entry["status"] == "待核"
            assert entry["original_version"] == "REL-2026.11"
            assert entry["data_snapshot"]["订单号"] == "ORD-007"

    def test_view_filters_by_field_and_reason(self) -> None:
        """按字段和错误原因分别查看。"""
        eng = _build_order_engine()
        eng.run_quality_gate("订单宽表", _order_rows(), release_version="REL-2026.11")

        amount_view = eng.quarantine_view("订单宽表", field="金额")
        assert {e["row_key"] for e in amount_view} == {"ORD-007", "ORD-008"}
        assert all(e["reason"] == "金额缺值" for e in amount_view)

        cust_view = eng.quarantine_view("订单宽表", reason="客户号")
        assert [e["row_key"] for e in cust_view] == ["ORD-007"]

    def test_rerun_gate_dedupes_pending_entries(self) -> None:
        """重跑门禁时同一订单同一字段的待核隔离行不重复建行。"""
        eng = _build_order_engine()
        eng.run_quality_gate("订单宽表", _order_rows(), release_version="REL-2026.11")
        eng.run_quality_gate("订单宽表", _order_rows(), release_version="REL-2026.11")
        assert len(eng.quarantine_view("订单宽表")) == 3

    def test_gate_without_registered_rows_fails(self, engine: FusionEngine) -> None:
        """宽表没有可检查数据时返回失败。"""
        result = engine.run_quality_gate("订单宽表")
        assert result["ok"] is False


class TestCorrectionReceipt:
    """更正回执：迟到的更正先保留，确认后生效。"""

    def test_receive_and_confirm_correction(self) -> None:
        """确认金额更正后写入宽表行，视图保留当时数据与后到的更正。"""
        eng = _build_order_engine()
        eng.run_quality_gate("订单宽表", _order_rows(), release_version="REL-2026.11")
        entry = eng.quarantine_view("订单宽表", row_key="ORD-007", field="金额")[0]

        receipt = eng.receive_quarantine_correction(entry["entry_id"], 880.0)
        assert receipt["ok"] is True
        # 确认前宽表行仍是旧值
        assert eng.wide_table_rows("订单宽表")[1]["金额"] is None

        confirm = eng.confirm_quarantine_correction(entry["entry_id"])
        assert confirm["ok"] is True
        rows = {r["订单号"]: r for r in eng.wide_table_rows("订单宽表")}
        assert rows["ORD-007"]["金额"] == 880.0

        view = eng.quarantine_view("订单宽表", row_key="ORD-007", field="金额")[0]
        assert view["data_snapshot"]["金额"] is None  # 当时的数据保留
        assert view["corrections"][0]["value"] == 880.0  # 后到的更正保留
        assert view["corrections"][0]["confirmed"] is True
        assert view["status"] == "已补写"

    def test_confirm_without_receipt_fails(self) -> None:
        """没有更正回执时确认失败。"""
        eng = _build_order_engine()
        eng.run_quality_gate("订单宽表", _order_rows())
        entry = eng.quarantine_view("订单宽表", row_key="ORD-007", field="金额")[0]
        result = eng.confirm_quarantine_correction(entry["entry_id"])
        assert result["ok"] is False

    def test_correction_on_missing_entry_fails(self, engine: FusionEngine) -> None:
        """隔离行不存在时登记/确认均失败。"""
        assert engine.receive_quarantine_correction(9999, 1.0)["ok"] is False
        assert engine.confirm_quarantine_correction(9999)["ok"] is False


class TestBackfillList:
    """补写清单：行级去重、可执行核对与续办领取。"""

    def test_list_dedupes_same_row(self) -> None:
        """同一行同时命中多个检查也只能被补成一行。"""
        eng = _build_order_engine()
        eng.run_quality_gate("订单宽表", _order_rows())
        entry_ids = [e["entry_id"] for e in eng.quarantine_view("订单宽表")]
        qlist = eng.build_quarantine_backfill_list(entry_ids)
        assert qlist["ok"] is True
        assert len(qlist["items"]) == 2  # ORD-007 两个字段合并成一行
        o7 = next(i for i in qlist["items"] if i["row_key"] == "ORD-007")
        assert {f["field"] for f in o7["fields"]} == {"金额", "客户号"}

    def test_cleared_entry_blocked_on_submit(self) -> None:
        """已核清的异常提交补写时不可执行并给出待核原因。"""
        eng = _build_order_engine()
        eng.run_quality_gate("订单宽表", _order_rows())
        o8 = eng.quarantine_view("订单宽表", row_key="ORD-008")[0]
        eng.review_quarantine_entry(o8["entry_id"], note="业务确认可为空")

        qlist = eng.build_quarantine_backfill_list([o8["entry_id"]])
        submit = eng.submit_quarantine_backfill(qlist["list_id"])
        assert submit["applied"] == []
        assert submit["blocked"][0]["row_key"] == "ORD-008"
        assert "核清" in submit["blocked"][0]["reason"]

    def test_submit_missing_list_fails(self, engine: FusionEngine) -> None:
        """清单不存在时提交失败。"""
        assert engine.submit_quarantine_backfill(9999)["ok"] is False

    def test_continuation_requires_existing_task_and_list(self) -> None:
        """续办必须关联存在的回填任务与补写清单。"""
        eng = _build_order_engine()
        eng.run_quality_gate("订单宽表", _order_rows())
        entry_ids = [e["entry_id"] for e in eng.quarantine_view("订单宽表")]
        qlist = eng.build_quarantine_backfill_list(entry_ids)
        task = eng.create_backfill_task("订单宽表", "20261123")

        assert eng.claim_backfill_continuation(9999, qlist["list_id"])["ok"] is False
        assert eng.claim_backfill_continuation(task.task_id, 9999)["ok"] is False

    def test_apply_backfill_rejects_overwrite_of_confirmed_value(self) -> None:
        """续办补写同样不能覆盖已确认的新值。"""
        eng = _build_order_engine()
        eng.run_quality_gate("订单宽表", _order_rows())
        o7_amount = eng.quarantine_view("订单宽表", row_key="ORD-007", field="金额")[0]
        eng.receive_quarantine_correction(o7_amount["entry_id"], 880.0)
        eng.confirm_quarantine_correction(o7_amount["entry_id"])

        qlist = eng.build_quarantine_backfill_list([o7_amount["entry_id"]])
        result = eng.apply_quarantine_backfill(
            qlist["list_id"], "ORD-007", {"金额": 100.0},
        )
        assert result["ok"] is True
        assert result["applied"] == []
        assert "覆盖" in result["blocked"][0]["reason"]
        rows = {r["订单号"]: r for r in eng.wide_table_rows("订单宽表")}
        assert rows["ORD-007"]["金额"] == 880.0


class TestOrder7Scenario:
    """订单 7 值班处理全流程：查看 -> 确认更正 -> 提交补写 -> 续办 -> 重跑门禁。"""

    def test_order7_quarantine_backfill_end_to_end(self) -> None:
        eng = _build_order_engine()
        snap = eng.create_snapshot("订单宽表", partition_key_value="20261123")
        snap.status_mark = "可用"

        # 跑门禁：订单 7 因金额和客户号同时进入隔离，订单 8 因金额进入隔离
        gate = eng.run_quality_gate("订单宽表", _order_rows(),
                                    release_version="REL-2026.11")
        assert gate["passed"] is False
        assert len(gate["quarantine_entries"]) == 3

        # 值班先按金额和客户号分别查看订单 7
        amount_view = eng.quarantine_view("订单宽表", row_key="ORD-007", field="金额")
        cust_view = eng.quarantine_view("订单宽表", row_key="ORD-007", field="客户号")
        assert len(amount_view) == 1 and len(cust_view) == 1
        assert amount_view[0]["reason"] == "金额缺值"
        assert cust_view[0]["reason"] == "客户号缺值"
        assert amount_view[0]["original_version"] == "REL-2026.11"

        # 另一笔异常（订单 8 金额）经核查业务可接受，被核清
        o8_entry = eng.quarantine_view("订单宽表", row_key="ORD-008")[0]
        eng.review_quarantine_entry(o8_entry["entry_id"], note="业务确认可为空")

        # 值班选中三条隔离行生成补写清单：订单 7 命中两个检查只补成一行
        selection = [
            amount_view[0]["entry_id"],
            cust_view[0]["entry_id"],
            o8_entry["entry_id"],
        ]
        qlist = eng.build_quarantine_backfill_list(selection)
        assert qlist["ok"] is True
        assert len(qlist["items"]) == 2
        o7_item = next(i for i in qlist["items"] if i["row_key"] == "ORD-007")
        assert {f["field"] for f in o7_item["fields"]} == {"金额", "客户号"}

        # 金额更正回执迟到，值班确认后生效到宽表行
        receipt = eng.receive_quarantine_correction(
            amount_view[0]["entry_id"], 880.0, receipt_code="RCPT-A1",
        )
        assert receipt["ok"] is True
        confirm = eng.confirm_quarantine_correction(amount_view[0]["entry_id"])
        assert confirm["ok"] is True
        rows = {r["订单号"]: r for r in eng.wide_table_rows("订单宽表")}
        assert rows["ORD-007"]["金额"] == 880.0

        # 视图保留当时的数据与后到的更正
        view_after = eng.quarantine_view("订单宽表", row_key="ORD-007", field="金额")[0]
        assert view_after["data_snapshot"]["金额"] is None
        assert view_after["corrections"][0]["receipt_code"] == "RCPT-A1"
        assert view_after["corrections"][0]["confirmed"] is True

        # 用原选择提交补写：金额不得覆盖已确认新值，客户号仍待补，已核清不可执行
        submit = eng.submit_quarantine_backfill(qlist["list_id"])
        assert submit["ok"] is True
        assert submit["applied"] == []
        blocked = {(b["row_key"], b["field"]): b["reason"] for b in submit["blocked"]}
        assert "覆盖" in blocked[("ORD-007", "金额")]
        assert "核清" in blocked[("ORD-008", "金额")]
        assert [(p["row_key"], p["field"]) for p in submit["pending"]] == [
            ("ORD-007", "客户号"),
        ]

        # 已确认的新金额没有被补写覆盖
        rows = {r["订单号"]: r for r in eng.wide_table_rows("订单宽表")}
        assert rows["ORD-007"]["金额"] == 880.0

        # 分区回填续办：关联补写清单，领取待补订单及字段
        task = eng.create_backfill_task("订单宽表", "20261123", parallel_partitions=2)
        claim = eng.claim_backfill_continuation(task.task_id, qlist["list_id"])
        assert claim["ok"] is True
        assert claim["claimed"] == [{"row_key": "ORD-007", "fields": ["客户号"]}]
        assert task.quarantine_list_code == qlist["list_code"]

        # 处理剩余问题：客户号补值
        fix = eng.apply_quarantine_backfill(qlist["list_id"], "ORD-007",
                                            {"客户号": "C007"})
        assert fix["ok"] is True
        assert [a["field"] for a in fix["applied"]] == ["客户号"]

        # 再跑门禁：全部通过；已核清的订单 8 金额被放行而不再判违规
        gate2 = eng.run_quality_gate("订单宽表")
        assert gate2["passed"] is True
        assert gate2["violations"] == []
        assert [(s["row_key"], s["field"]) for s in gate2["suppressed"]] == [
            ("ORD-008", "金额"),
        ]

        # 快照里订单 7 只有一条修正明细，金额与客户号并在这条明细里
        details = eng.snapshot_correction_details(snap.partition_id)
        o7_details = [d for d in details if d["row_key"] == "ORD-007"]
        assert len(o7_details) == 1
        assert set(o7_details[0]["fields"]) == {"金额", "客户号"}
        sources = {u["source"] for u in o7_details[0]["updates"]}
        assert "更正回执RCPT-A1" in sources
        assert "隔离补写" in sources


class TestPipelineQuarantineIntegration:
    """融合流水线联动：门禁违规行进入隔离视图。"""

    def test_pipeline_quarantines_violating_rows(self, engine: FusionEngine) -> None:
        """流水线门禁不再只数缺值，违规行带订单键进入隔离视图。"""
        engine.register_source(
            source_code="SRC-QV", source_name="qv_source", source_type="MySQL",
            connection_string="conn://qv",
        )
        job = engine.create_fusion_job(
            target_wide_table="WT_QV", participant_sources=["SRC-QV"], join_key="id",
        )
        engine.create_quality_gate(
            target_wide_table="WT_QV", check_type="null_check",
            threshold_condition="missing_field",
        )
        result = engine.execute_fusion_pipeline(job.job_id)
        assert result["quality_gate"]["passed"] is False
        assert len(result["quality_gate"]["quarantine_entries"]) == 3

        view = engine.quarantine_view("WT_QV")
        assert len(view) == 3
        assert all(e["reason"] == "missing_field缺值" for e in view)
        assert all(e["original_version"] == job.job_code for e in view)

    def test_engine_domain_snapshot_counts_quarantine(self, engine: FusionEngine) -> None:
        """引擎全局快照包含隔离行计数。"""
        engine.register_source(
            source_code="SRC-QC", source_name="qc_source", source_type="MySQL",
            connection_string="conn://qc",
        )
        job = engine.create_fusion_job(
            target_wide_table="WT_QC", participant_sources=["SRC-QC"], join_key="id",
        )
        engine.create_quality_gate(
            target_wide_table="WT_QC", check_type="null_check",
            threshold_condition="missing_field",
        )
        engine.execute_fusion_pipeline(job.job_id)
        snapshot = engine.domain_snapshot()
        assert snapshot["quarantine_entries"] == 3


class TestQuarantineManagerSnapshot:
    """隔离视图管理器自身的数据质量快照。"""

    def test_manager_domain_snapshot(self) -> None:
        mgr = QuarantineManager()
        entry = mgr.record_violation(
            "订单宽表", "ORD-007", "金额", "null_check", "金额缺值",
            {"订单号": "ORD-007", "金额": None}, "REL-2026.11",
        )
        qlist = mgr.build_backfill_list([entry.entry_id])
        snap = mgr.domain_snapshot()
        assert snap["count"] == 1
        assert snap["by_status"]["待核"] == 1
        assert snap["backfill_lists"] == 1
        assert snap["pending_fields"] == 1
