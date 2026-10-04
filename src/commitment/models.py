"""领域模型与常量。

状态与不变量命名与 ``domain/contract.json`` 保持一致。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any

# ---- 采购行状态（契约 states）---------------------------------------------
LINE_DRAFT = "草拟"
LINE_PENDING = "待确认"
LINE_RELEASED = "已下达"
LINE_FULFILLING = "履行中"
LINE_CLOSED = "已关闭"

# ---- 供应商回复动作 --------------------------------------------------------
ACTION_CONFIRM = "确认"
ACTION_REJECT = "拒绝"
ACTION_RESCHEDULE = "改期"
ACTION_TRANSFER = "转单"
ACTION_OVERCONFIRM = "超量确认"

# ---- 承诺版本状态 ----------------------------------------------------------
CMT_ACTIVE = "生效"
CMT_REJECTED = "拒绝"

# ---- 消息处理结果 ----------------------------------------------------------
RESULT_PROCESSED = "已处理"
RESULT_REPLAYED = "幂等重放"
RESULT_PARTIAL = "部分接受"
RESULT_REJECTED = "已拒绝"

# ---- 缺口责任类型 ----------------------------------------------------------
RESP_OPEN = "待供应商确认"
RESP_SHORTFALL = "供应商欠交"
RESP_RESCHEDULE = "供应商改期"
RESP_TRANSFER = "转单导致延期"
RESP_LATE = "承诺迟交"


@dataclass(frozen=True)
class ProcurementLine:
    """采购行：一批需要供应商承诺覆盖的需求。"""

    line_id: str
    material: str
    quantity: int
    due_date: date
    status: str = LINE_PENDING
    version: int = 0

    @classmethod
    def from_row(cls, row: Any) -> "ProcurementLine":
        return cls(
            line_id=row["line_id"],
            material=row["material"],
            quantity=row["quantity"],
            due_date=date.fromisoformat(row["due_date"]),
            status=row["status"],
            version=row["version"],
        )


@dataclass(frozen=True)
class CapacityWindow:
    """供应商在某物料、某日期上的可承诺产能窗口。"""

    window_id: str
    supplier: str
    material: str
    window_date: date
    capacity: int
    allocated: int
    version: int = 0

    @property
    def available(self) -> int:
        return self.capacity - self.allocated

    @classmethod
    def from_row(cls, row: Any) -> "CapacityWindow":
        return cls(
            window_id=row["window_id"],
            supplier=row["supplier"],
            material=row["material"],
            window_date=date.fromisoformat(row["window_date"]),
            capacity=row["capacity"],
            allocated=row["allocated"],
            version=row["version"],
        )


@dataclass(frozen=True)
class Commitment:
    """承诺版本（不可变快照行）。

    ``seq`` 是该版本在采购行版本链上的全局序号；``chain_hash`` 把本行与
    前一版本串联，任何篡改/删除都会破坏链。旧版本永不 UPDATE：拒绝、改期、
    转单关系通过新版本的 ``references`` 指回前序版本，当前生效集由重放
    版本链（:mod:`commitment.engine`）导出。
    """

    commitment_id: str
    line_id: str
    supplier: str
    material: str
    promised_date: date
    quantity: int
    action: str
    seq: int
    status: str
    confirmed_by: str
    message_id: str
    created_at: str
    reason: str | None
    references: tuple[str, ...]
    transferred_to: str | None
    chain_hash: str
    state: dict[str, Any]

    @classmethod
    def from_row(cls, row: Any) -> "Commitment":
        return cls(
            commitment_id=row["commitment_id"],
            line_id=row["line_id"],
            supplier=row["supplier"],
            material=row["material"],
            promised_date=date.fromisoformat(row["promised_date"]),
            quantity=row["quantity"],
            action=row["action"],
            seq=row["seq"],
            status=row["status"],
            confirmed_by=row["confirmed_by"],
            message_id=row["message_id"],
            created_at=row["created_at"],
            reason=row["reason"],
            references=tuple(json.loads(row["references_json"])),
            transferred_to=row["transferred_to"],
            chain_hash=row["chain_hash"],
            state=json.loads(row["state_json"]),
        )


@dataclass(frozen=True)
class MessageReceipt:
    """消息处理台账：重复消息返回同一结果，不产生第二份承诺。"""

    message_id: str
    sender: str
    body_hash: str
    result_kind: str
    response_payload: dict[str, Any]
    created_at: str
    replayed: bool = False
