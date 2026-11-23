"""隔离视图与补写清单：质量门禁异常行的定位、迟到更正与定向补写。

质量门禁原本只统计缺值数量，值班无法定位异常明细。本模块把每次门禁
违规落成隔离行（订单字段 + 错误原因 + 原发布版本 + 当时的数据），
支持后到的更正回执登记与确认、按值班选择生成行级去重的补写清单，
以及与分区回填续办联动的待补领取。

协作边界：管理器只持有隔离行与补写清单的领域状态，真正写宽表行、
记快照修正明细由熔炉引擎根据返回的生效描述（effect）执行。
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from datetime import datetime, timezone
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# 隔离行状态
ENTRY_PENDING = "待核"
ENTRY_CLEARED = "已核清"
ENTRY_BACKFILLED = "已补写"

# 补写字段状态
FIELD_PENDING = "待补"
FIELD_DONE = "已补"
FIELD_BLOCKED = "待核"


@dataclass
class CorrectionReceipt:
    """更正回执：后到的更正，确认前不生效。"""
    receipt_id: int
    receipt_code: str
    value: Any
    received_at: str
    confirmed: bool = False
    confirmed_at: str = ""


@dataclass
class QuarantineEntry:
    """隔离行：一次门禁检查命中的一条订单字段异常。"""
    entry_id: int
    quarantine_code: str
    target_wide_table: str
    row_key: str
    field: str
    check_type: str
    reason: str
    original_version: str
    data_snapshot: dict[str, Any]
    status: str = ENTRY_PENDING
    corrections: list[CorrectionReceipt] = dc_field(default_factory=list)
    review_note: str = ""
    created_at: str = ""

    def confirmed_correction(self) -> CorrectionReceipt | None:
        for receipt in reversed(self.corrections):
            if receipt.confirmed:
                return receipt
        return None

    def to_view(self) -> dict[str, Any]:
        """隔离视图行：错误原因、当时的数据与后到的更正一并保留。"""
        return {
            "entry_id": self.entry_id,
            "quarantine_code": self.quarantine_code,
            "target_wide_table": self.target_wide_table,
            "row_key": self.row_key,
            "field": self.field,
            "check_type": self.check_type,
            "reason": self.reason,
            "original_version": self.original_version,
            "status": self.status,
            "data_snapshot": dict(self.data_snapshot),
            "corrections": [
                {
                    "receipt_id": r.receipt_id,
                    "receipt_code": r.receipt_code,
                    "value": r.value,
                    "received_at": r.received_at,
                    "confirmed": r.confirmed,
                    "confirmed_at": r.confirmed_at,
                }
                for r in self.corrections
            ],
            "review_note": self.review_note,
            "created_at": self.created_at,
        }


@dataclass
class BackfillField:
    """补写字段：清单行内一个待补的订单字段。"""
    field: str
    entry_id: int
    reason: str
    proposed_value: Any
    status: str = FIELD_PENDING
    block_reason: str = ""


@dataclass
class BackfillItem:
    """补写行：同一订单同时命中多个检查也只补成一行。"""
    row_key: str
    fields: list[BackfillField] = dc_field(default_factory=list)
    status: str = FIELD_PENDING

    def refresh_status(self) -> None:
        statuses = {f.status for f in self.fields}
        if statuses and statuses <= {FIELD_DONE}:
            self.status = FIELD_DONE
        elif FIELD_PENDING in statuses:
            self.status = FIELD_PENDING
        else:
            self.status = FIELD_BLOCKED


@dataclass
class QuarantineBackfillList:
    """隔离行补写清单：值班选择隔离行后生成的定向处理清单。"""
    list_id: int
    list_code: str
    target_wide_table: str
    items: list[BackfillItem] = dc_field(default_factory=list)
    claimed_by: str = ""
    created_at: str = ""


class QuarantineManager:
    """隔离视图管理器：隔离行、更正回执与补写清单的领域逻辑。"""

    def __init__(self) -> None:
        self._entries: dict[int, QuarantineEntry] = {}
        self._lists: dict[int, QuarantineBackfillList] = {}
        self._next_entry_id = 1
        self._next_list_id = 1
        self._next_receipt_id = 1

    @property
    def entries(self) -> list[QuarantineEntry]:
        return list(self._entries.values())

    @property
    def backfill_lists(self) -> list[QuarantineBackfillList]:
        return list(self._lists.values())

    # ---- 隔离行登记 ----

    def record_violation(self, target_wide_table: str, row_key: str, field: str,
                         check_type: str, reason: str,
                         data_snapshot: dict[str, Any],
                         original_version: str) -> QuarantineEntry:
        """登记隔离行；同一订单同一字段同一检查的待核行只刷新当时数据，不重复建行。"""
        for entry in self._entries.values():
            if (entry.target_wide_table == target_wide_table
                    and entry.row_key == row_key
                    and entry.field == field
                    and entry.check_type == check_type
                    and entry.status == ENTRY_PENDING):
                entry.data_snapshot = dict(data_snapshot)
                if original_version:
                    entry.original_version = original_version
                return entry
        sid = self._next_entry_id
        self._next_entry_id += 1
        entry = QuarantineEntry(
            entry_id=sid,
            quarantine_code=f"QUAR-{sid:04d}",
            target_wide_table=target_wide_table,
            row_key=row_key,
            field=field,
            check_type=check_type,
            reason=reason,
            original_version=original_version,
            data_snapshot=dict(data_snapshot),
            created_at=_now_iso(),
        )
        self._entries[sid] = entry
        return entry

    def get_entry(self, entry_id: int) -> QuarantineEntry | None:
        return self._entries.get(entry_id)

    def is_cleared(self, target_wide_table: str, row_key: str, field: str,
                   check_type: str) -> bool:
        """该订单字段的异常是否已核清；核清后门禁重跑不再判违规。"""
        return any(
            e.target_wide_table == target_wide_table
            and e.row_key == row_key
            and e.field == field
            and e.check_type == check_type
            and e.status == ENTRY_CLEARED
            for e in self._entries.values()
        )

    # ---- 隔离视图 ----

    def view(self, *, target_wide_table: str | None = None,
             row_key: str | None = None, field: str | None = None,
             reason: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        """按订单、字段或错误原因过滤隔离行，供值班定位异常明细。"""
        rows = list(self._entries.values())
        if target_wide_table:
            rows = [e for e in rows if e.target_wide_table == target_wide_table]
        if row_key:
            rows = [e for e in rows if e.row_key == row_key]
        if field:
            rows = [e for e in rows if e.field == field]
        if reason:
            rows = [e for e in rows if reason in e.reason]
        if status:
            rows = [e for e in rows if e.status == status]
        return [e.to_view() for e in rows]

    # ---- 复核与更正回执 ----

    def review(self, entry_id: int, note: str = "") -> QuarantineEntry | None:
        """复核核清：异常经核查无需补写。"""
        entry = self._entries.get(entry_id)
        if entry is None:
            return None
        entry.status = ENTRY_CLEARED
        entry.review_note = note
        return entry

    def receive_correction(self, entry_id: int, value: Any,
                           receipt_code: str = "") -> CorrectionReceipt | None:
        """登记更正回执（可能迟到）：先保留，确认后才生效。"""
        entry = self._entries.get(entry_id)
        if entry is None:
            return None
        sid = self._next_receipt_id
        self._next_receipt_id += 1
        receipt = CorrectionReceipt(
            receipt_id=sid,
            receipt_code=receipt_code or f"RCPT-{sid:04d}",
            value=value,
            received_at=_now_iso(),
        )
        entry.corrections.append(receipt)
        return receipt

    def confirm_correction(self, entry_id: int,
                           receipt_id: int | None = None) -> dict[str, Any] | None:
        """确认更正回执：返回生效描述，由引擎写宽表行并记快照修正明细。"""
        entry = self._entries.get(entry_id)
        if entry is None:
            return None
        receipt: CorrectionReceipt | None = None
        if receipt_id is not None:
            receipt = next((r for r in entry.corrections if r.receipt_id == receipt_id), None)
        elif entry.corrections:
            receipt = entry.corrections[-1]
        if receipt is None:
            return None
        receipt.confirmed = True
        receipt.confirmed_at = _now_iso()
        entry.status = ENTRY_BACKFILLED
        return {
            "target_wide_table": entry.target_wide_table,
            "row_key": entry.row_key,
            "field": entry.field,
            "value": receipt.value,
            "source": f"更正回执{receipt.receipt_code}",
        }

    # ---- 补写清单 ----

    def build_backfill_list(self, entry_ids: list[int]) -> QuarantineBackfillList:
        """按值班选择生成补写清单：同一订单命中多个检查只补成一行。"""
        items: dict[str, BackfillItem] = {}
        table = ""
        for entry_id in entry_ids:
            entry = self._entries.get(entry_id)
            if entry is None:
                continue
            table = table or entry.target_wide_table
            item = items.setdefault(entry.row_key, BackfillItem(row_key=entry.row_key))
            if any(bf.entry_id == entry_id for bf in item.fields):
                continue
            correction = entry.corrections[-1] if entry.corrections else None
            proposed = correction.value if correction else entry.data_snapshot.get(entry.field)
            item.fields.append(BackfillField(
                field=entry.field,
                entry_id=entry.entry_id,
                reason=entry.reason,
                proposed_value=proposed,
            ))
        sid = self._next_list_id
        self._next_list_id += 1
        qlist = QuarantineBackfillList(
            list_id=sid,
            list_code=f"QBL-{sid:04d}",
            target_wide_table=table,
            items=list(items.values()),
            created_at=_now_iso(),
        )
        self._lists[sid] = qlist
        return qlist

    def get_list(self, list_id: int) -> QuarantineBackfillList | None:
        return self._lists.get(list_id)

    def submit_backfill(self, list_id: int) -> dict[str, Any] | None:
        """提交补写：逐项核对仍可执行的对象，不可执行的给出待核原因。

        有效补写不能覆盖已经确认的新值；已核清的异常不再补写；
        仍缺值的字段保持待补，留给分区回填续办领取。
        """
        qlist = self._lists.get(list_id)
        if qlist is None:
            return None
        applied: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        for item in qlist.items:
            for bf in item.fields:
                if bf.status == FIELD_DONE:
                    continue
                entry = self._entries.get(bf.entry_id)
                if entry is None:
                    bf.status = FIELD_BLOCKED
                    bf.block_reason = "隔离行不存在或已归档"
                    blocked.append({"row_key": item.row_key, "field": bf.field,
                                    "reason": bf.block_reason})
                    continue
                confirmed = entry.confirmed_correction()
                if confirmed is not None and confirmed.value != bf.proposed_value:
                    bf.status = FIELD_BLOCKED
                    bf.block_reason = f"{entry.field}已确认新值「{confirmed.value}」，补写不能覆盖"
                    blocked.append({"row_key": item.row_key, "field": bf.field,
                                    "reason": bf.block_reason})
                    continue
                if entry.status == ENTRY_CLEARED:
                    bf.status = FIELD_BLOCKED
                    bf.block_reason = "异常已核清，无需补写"
                    blocked.append({"row_key": item.row_key, "field": bf.field,
                                    "reason": bf.block_reason})
                    continue
                if entry.status == ENTRY_BACKFILLED:
                    bf.status = FIELD_BLOCKED
                    bf.block_reason = "隔离行已补写，不重复执行"
                    blocked.append({"row_key": item.row_key, "field": bf.field,
                                    "reason": bf.block_reason})
                    continue
                if bf.proposed_value is None or bf.proposed_value == "":
                    bf.status = FIELD_PENDING
                    pending.append({"row_key": item.row_key, "field": bf.field,
                                    "reason": entry.reason})
                    continue
                bf.status = FIELD_DONE
                entry.status = ENTRY_BACKFILLED
                applied.append({
                    "target_wide_table": entry.target_wide_table,
                    "row_key": item.row_key,
                    "field": bf.field,
                    "value": bf.proposed_value,
                    "source": "隔离补写",
                })
            item.refresh_status()
        return {"ok": True, "list_code": qlist.list_code,
                "applied": applied, "pending": pending, "blocked": blocked}

    def claim_continuation(self, list_id: int, task_code: str) -> dict[str, Any] | None:
        """分区回填续办：关联补写清单，从中领取待补订单及字段。"""
        qlist = self._lists.get(list_id)
        if qlist is None:
            return None
        qlist.claimed_by = task_code
        claimed = [
            {"row_key": item.row_key,
             "fields": [bf.field for bf in item.fields if bf.status == FIELD_PENDING]}
            for item in qlist.items
            if any(bf.status == FIELD_PENDING for bf in item.fields)
        ]
        return {"ok": True, "list_code": qlist.list_code,
                "claimed_by": task_code, "claimed": claimed}

    def apply_backfill(self, list_id: int, row_key: str,
                       values: dict[str, Any]) -> dict[str, Any] | None:
        """对领取的待补字段执行补写；补写值同样不得覆盖已确认的新值。"""
        qlist = self._lists.get(list_id)
        if qlist is None:
            return None
        item = next((it for it in qlist.items if it.row_key == row_key), None)
        if item is None:
            return {"ok": False, "message": f"清单中不存在订单 {row_key}"}
        applied: list[dict[str, Any]] = []
        still_pending: list[str] = []
        blocked: list[dict[str, Any]] = []
        for bf in item.fields:
            if bf.status != FIELD_PENDING:
                if bf.status == FIELD_BLOCKED:
                    blocked.append({"row_key": row_key, "field": bf.field,
                                    "reason": bf.block_reason})
                continue
            value = values.get(bf.field)
            if value is None or value == "":
                still_pending.append(bf.field)
                continue
            entry = self._entries.get(bf.entry_id)
            confirmed = entry.confirmed_correction() if entry else None
            if confirmed is not None and confirmed.value != value:
                bf.status = FIELD_BLOCKED
                bf.block_reason = f"{bf.field}已确认新值「{confirmed.value}」，补写不能覆盖"
                blocked.append({"row_key": row_key, "field": bf.field,
                                "reason": bf.block_reason})
                continue
            bf.proposed_value = value
            bf.status = FIELD_DONE
            if entry is not None:
                entry.status = ENTRY_BACKFILLED
            applied.append({
                "target_wide_table": qlist.target_wide_table,
                "row_key": row_key,
                "field": bf.field,
                "value": value,
                "source": "隔离补写",
            })
        item.refresh_status()
        return {"ok": True, "applied": applied,
                "still_pending": still_pending, "blocked": blocked}

    def domain_snapshot(self) -> dict[str, Any]:
        """输出隔离视图数据质量快照。"""
        entries = list(self._entries.values())
        by_status = {
            status: sum(1 for e in entries if e.status == status)
            for status in [ENTRY_PENDING, ENTRY_CLEARED, ENTRY_BACKFILLED]
        }
        return {
            "module": "quarantine_view",
            "entity": "隔离行",
            "count": len(entries),
            "by_status": by_status,
            "backfill_lists": len(self._lists),
            "pending_fields": sum(
                1 for qlist in self._lists.values()
                for item in qlist.items
                for bf in item.fields
                if bf.status == FIELD_PENDING
            ),
        }
