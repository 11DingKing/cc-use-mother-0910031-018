"""领域模型：采购行、需求批次、产能窗口、承诺版本（不可变快照）。"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from typing import Any, Literal

LineStatus = Literal["草拟", "待确认", "已下达", "履行中", "已关闭"]
VersionKind = Literal["reply", "transfer_in", "transfer_out"]
VersionStatus = Literal["active", "superseded"]


def _iso(value: date | datetime) -> str:
    return value.isoformat()


@dataclass(frozen=True)
class DemandBatch:
    """一批需求（如某周的物料净需求）。"""

    batch_id: str
    need_date: date
    qty: int

    def to_dict(self) -> dict[str, Any]:
        return {"batch_id": self.batch_id, "need_date": _iso(self.need_date), "qty": self.qty}


@dataclass
class PurchaseLine:
    """采购行：对同一物料的总采购需求，拆成多批需求。"""

    line_id: str
    material_code: str
    unit: str
    batches: list[DemandBatch] = field(default_factory=list)
    description: str = ""
    status: LineStatus = "草拟"
    created_at: datetime = field(default_factory=datetime.utcnow)

    @property
    def total_qty(self) -> int:
        return sum(b.qty for b in self.batches)

    def batch(self, batch_id: str) -> DemandBatch | None:
        return next((b for b in self.batches if b.batch_id == batch_id), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "line_id": self.line_id,
            "material_code": self.material_code,
            "description": self.description,
            "unit": self.unit,
            "total_qty": self.total_qty,
            "status": self.status,
            "created_at": _iso(self.created_at) + "Z",
            "batches": [b.to_dict() for b in self.batches],
        }


@dataclass(frozen=True)
class CapacityWindow:
    """供应商对某物料在日期区间内的供应产能（区间总量）。"""

    window_id: str
    supplier_id: str
    material_code: str
    date_from: date
    date_to: date
    capacity_qty: int

    def covers(self, material_code: str, promised: date) -> bool:
        return (
            self.material_code == material_code
            and self.date_from <= promised <= self.date_to
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id,
            "supplier_id": self.supplier_id,
            "material_code": self.material_code,
            "date_from": _iso(self.date_from),
            "date_to": _iso(self.date_to),
            "capacity_qty": self.capacity_qty,
        }


@dataclass(frozen=True)
class CommitmentItem:
    """供应商针对某需求批次承诺的数量与日期。"""

    batch_id: str
    promised_date: date
    qty: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "promised_date": _iso(self.promised_date),
            "qty": self.qty,
        }


@dataclass(frozen=True)
class Rejection:
    """供应商对某批次部分（或全部）数量的拒绝及其原因。"""

    batch_id: str
    qty: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"batch_id": self.batch_id, "qty": self.qty, "reason": self.reason}


@dataclass(frozen=True)
class ItemChange:
    """本次版本相对该供应商上一版本的逐条变化，用于责任链。"""

    batch_id: str
    change_type: str  # new / increased / decreased / rescheduled / unchanged / transferred_out / transferred_in
    qty_delta: int
    old_date: date | None
    new_date: date | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "change_type": self.change_type,
            "qty_delta": self.qty_delta,
            "old_date": _iso(self.old_date) if self.old_date else None,
            "new_date": _iso(self.new_date) if self.new_date else None,
        }


@dataclass
class CommitmentVersion:
    """
    承诺版本。供应商每封确认邮件产生一个新版本；同一供应商的新版本自动取代
    该供应商在此采购行上的旧版本（旧版本不删除，只置为 superseded）。
    snapshot 为创建时计算的不可变规范快照，snapshot_hash 用于验真。
    """

    version_id: str
    sequence: int
    line_id: str
    supplier_id: str
    message_id: str
    confirmer: str
    created_at: datetime
    kind: VersionKind = "reply"
    status: VersionStatus = "active"
    items: list[CommitmentItem] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)
    item_changes: list[ItemChange] = field(default_factory=list)
    superseded_version_ids: list[str] = field(default_factory=list)
    related_version_id: str | None = None  # 转单时关联的对方版本
    reason: str = ""
    mode: str = "strict"
    remark: str = ""
    snapshot: dict[str, Any] = field(default_factory=dict)
    snapshot_hash: str = ""

    def to_dict(self, include_snapshot: bool = True) -> dict[str, Any]:
        data = {
            "version_id": self.version_id,
            "sequence": self.sequence,
            "line_id": self.line_id,
            "supplier_id": self.supplier_id,
            "message_id": self.message_id,
            "confirmer": self.confirmer,
            "created_at": _iso(self.created_at) + "Z",
            "kind": self.kind,
            "status": self.status,
            "mode": self.mode,
            "remark": self.remark,
            "reason": self.reason,
            "items": [i.to_dict() for i in self.items],
            "rejections": [r.to_dict() for r in self.rejections],
            "item_changes": [c.to_dict() for c in self.item_changes],
            "superseded_version_ids": list(self.superseded_version_ids),
            "related_version_id": self.related_version_id,
        }
        if include_snapshot:
            data["snapshot_hash"] = self.snapshot_hash
            data["snapshot"] = self.snapshot
        return data
