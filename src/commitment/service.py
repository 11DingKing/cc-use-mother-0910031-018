"""承诺登记与消息处理服务。

所有写操作在单个 ``BEGIN IMMEDIATE`` 事务内完成：SQLite 单库单写使
并发消息严格串行；``(line_id, seq)`` 唯一索引与消息台账主键兜底，
即使上层出现重复投递也不可能生成第二份承诺。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date
from typing import Any

from . import engine
from .models import (
    ACTION_CONFIRM,
    ACTION_REJECT,
    ACTION_RESCHEDULE,
    ACTION_TRANSFER,
    LINE_CLOSED,
    LINE_DRAFT,
    LINE_FULFILLING,
    LINE_PENDING,
    LINE_RELEASED,
    RESULT_PARTIAL,
    RESULT_PROCESSED,
    RESULT_REJECTED,
    RESULT_REPLAYED,
)
from .storage import GENESIS, Storage, compute_chain_hash


class ServiceError(Exception):
    """业务错误基类，HTTP 层映射为 4xx。"""

    status = 400


class ValidationError(ServiceError):
    status = 400


class NotFoundError(ServiceError):
    status = 404


class ConflictError(ServiceError):
    status = 409


def _parse_date(value: Any, field: str) -> date:
    if not isinstance(value, str):
        raise ValidationError(f"{field} 必须是 ISO 日期字符串")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{field} 日期格式非法：{value}") from exc


def _parse_qty(value: Any, field: str = "quantity") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationError(f"{field} 必须是正整数")
    return value


def _body_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class CommitmentService:
    def __init__(self, storage: Storage) -> None:
        self.db = storage

    # ---- 登记：采购行 ------------------------------------------------------
    def register_line(self, line_id: str, material: str, quantity: int,
                      due_date: str, status: str = LINE_PENDING) -> dict[str, Any]:
        if not line_id or not isinstance(line_id, str):
            raise ValidationError("line_id 不能为空")
        if not material:
            raise ValidationError("material 不能为空")
        qty = _parse_qty(quantity)
        due = _parse_date(due_date, "due_date")
        if status not in (LINE_DRAFT, LINE_PENDING):
            raise ValidationError("新登记采购行只能是 草拟/待确认")
        try:
            with self.db.transaction() as conn:
                self.db.insert_line(conn, line_id, material, qty, due, status)
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"采购行已存在：{line_id}") from exc
        return {"line_id": line_id, "status": status}

    def release_line(self, line_id: str) -> dict[str, Any]:
        with self.db.transaction() as conn:
            row = self.db.get_line_lock(conn, line_id)
            if row is None:
                raise NotFoundError(f"采购行不存在：{line_id}")
            if row["status"] == LINE_RELEASED:
                return {"line_id": line_id, "status": row["status"], "changed": False}
            if row["status"] != LINE_PENDING:
                raise ConflictError(f"当前状态 {row['status']} 不允许下达")
            self.db.update_line_status(conn, line_id, LINE_RELEASED)
        return {"line_id": line_id, "status": LINE_RELEASED, "changed": True}

    def close_line(self, line_id: str) -> dict[str, Any]:
        with self.db.transaction() as conn:
            row = self.db.get_line_lock(conn, line_id)
            if row is None:
                raise NotFoundError(f"采购行不存在：{line_id}")
            self.db.update_line_status(conn, line_id, LINE_CLOSED)
        return {"line_id": line_id, "status": LINE_CLOSED}

    # ---- 登记：产能窗口 ----------------------------------------------------
    def register_capacity_window(self, window_id: str, supplier: str, material: str,
                                 window_date: str, capacity: int) -> dict[str, Any]:
        for name, value in (("window_id", window_id), ("supplier", supplier),
                            ("material", material)):
            if not value or not isinstance(value, str):
                raise ValidationError(f"{name} 不能为空")
        day = _parse_date(window_date, "window_date")
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 0:
            raise ValidationError("capacity 必须是非负整数")
        try:
            with self.db.transaction() as conn:
                self.db.insert_window(conn, window_id, supplier, material, day, capacity)
        except sqlite3.IntegrityError as exc:
            raise ConflictError(
                f"产能窗口已存在：{supplier}/{material}/{window_date}") from exc
        return {"window_id": window_id, "capacity": capacity, "allocated": 0}

    # ---- 消息处理（幂等入口）----------------------------------------------
    def handle_message(self, message_id: str, sender: str, payload: dict[str, Any]
                       ) -> dict[str, Any]:
        """处理一封供应商回复邮件。

        同一 ``message_id`` 重复投递：
        - 内容一致 → 返回首次结果（``result_kind=幂等重放``），不再产生版本；
        - 内容不一致 → 409，防止消息编号被复用覆盖事实。
        """
        if not message_id or not isinstance(message_id, str):
            raise ValidationError("message_id 不能为空")
        if not sender:
            raise ValidationError("sender 不能为空")
        if not isinstance(payload, dict):
            raise ValidationError("payload 必须是对象")

        digest = _body_hash(payload)

        # 快路径：只读命中台账
        existing = self.db.find_message(message_id)
        if existing is not None:
            return self._replay_receipt(existing, digest)

        try:
            with self.db.transaction() as conn:
                existing = self.db.find_message_lock(conn, message_id)
                if existing is not None:
                    return self._replay_receipt(existing, digest)
                response = self._process(conn, message_id, sender, payload)
                self.db.record_message(conn, message_id, sender, digest,
                                       response["result_kind"], response)
                response["message_id"] = message_id
                return response
        except sqlite3.IntegrityError as exc:
            # 并发下唯一约束兜底：重新读台账做重放
            existing = self.db.find_message(message_id)
            if existing is not None:
                return self._replay_receipt(existing, digest)
            raise ConflictError(f"写入冲突：{exc}") from exc

    def _replay_receipt(self, row: sqlite3.Row, digest: str) -> dict[str, Any]:
        if row["body_hash"] != digest:
            raise ConflictError(
                f"message_id={row['message_id']} 的消息体与首次投递不一致，"
                "禁止用同一编号投递不同内容")
        response = json.loads(row["response_json"])
        response["result_kind"] = RESULT_REPLAYED
        response["message_id"] = row["message_id"]
        return response

    # ---- 内部：单事务处理一条消息 ------------------------------------------
    def _process(self, conn: sqlite3.Connection, message_id: str, sender: str,
                 payload: dict[str, Any]) -> dict[str, Any]:
        action = payload.get("action")
        if action not in (ACTION_CONFIRM, ACTION_REJECT, ACTION_RESCHEDULE,
                          ACTION_TRANSFER):
            raise ValidationError(
                f"action 必须是 {ACTION_CONFIRM}/{ACTION_REJECT}/"
                f"{ACTION_RESCHEDULE}/{ACTION_TRANSFER} 之一")
        line_id = payload.get("line_id")
        supplier = payload.get("supplier")
        if not line_id or not supplier:
            raise ValidationError("line_id 与 supplier 必填")
        qty = _parse_qty(payload.get("quantity"))
        confirmed_by = payload.get("confirmed_by") or sender

        line = self.db.get_line_lock(conn, line_id)
        if line is None:
            raise NotFoundError(f"采购行不存在：{line_id}")
        if line["status"] not in (LINE_RELEASED, LINE_FULFILLING):
            raise ConflictError(
                f"采购行 {line_id} 当前状态 {line['status']}，需先下达才能受理回复")
        if payload.get("material") not in (None, line["material"]):
            raise ValidationError("material 与采购行登记不一致")

        rows = self.db.all_snapshots(line_id)
        state = engine.replay(rows, line["quantity"])
        last = self.db.last_snapshot(conn, line_id)
        seq = (last["seq"] + 1) if last else 1

        if action == ACTION_CONFIRM:
            result = self._do_confirm(conn, state, line, supplier, qty,
                                      payload, confirmed_by, message_id, seq)
        elif action == ACTION_REJECT:
            result = self._do_reject(conn, state, line, supplier, qty,
                                     payload, confirmed_by, message_id, seq)
        elif action == ACTION_RESCHEDULE:
            result = self._do_reschedule(conn, state, line, supplier, qty,
                                         payload, confirmed_by, message_id, seq)
        else:
            result = self._do_transfer(conn, state, line, supplier, qty,
                                       payload, confirmed_by, message_id, seq)

        # 首次出现生效承诺后进入履行中
        committed_after = sum(c["qty"] for c in engine.active_coverings(state))
        if committed_after > 0 and line["status"] == LINE_RELEASED:
            self.db.update_line_status(conn, line_id, LINE_FULFILLING)
        result["line_status"] = (LINE_FULFILLING if committed_after > 0
                                 else line["status"])
        result["message_id"] = message_id
        return result

    def _window(self, conn: sqlite3.Connection, supplier: str, material: str,
                day: date) -> sqlite3.Row:
        row = self.db.find_window_lock(conn, supplier, material, day)
        if row is None:
            raise ValidationError(
                f"供应商 {supplier} 在 {day} 没有 {material} 的产能窗口，"
                "请先登记窗口")
        return row

    def _prev_for(self, conn: sqlite3.Connection, line_id: str, seq: int) -> str:
        if seq == 1:
            return GENESIS
        prev = conn.execute(
            "SELECT chain_hash FROM commitment_snapshots WHERE line_id=? AND seq=?",
            (line_id, seq - 1),
        ).fetchone()
        if prev is None:
            raise ConflictError("版本链断裂：缺少前序版本")
        return prev["chain_hash"]

    # ---- 四种动作 ----------------------------------------------------------
    def _do_confirm(self, conn, state, line, supplier, qty, payload, confirmed_by,
                    message_id, seq) -> dict[str, Any]:
        day = _parse_date(payload.get("promised_date"), "promised_date")
        current_committed = sum(c["qty"] for c in engine.active_coverings(state))
        remaining_demand = line["quantity"] - current_committed

        window = self._window(conn, supplier, line["material"], day)
        room = window["capacity"] - window["allocated"]

        accepted = min(qty, max(0, remaining_demand), max(0, room))
        refused_demand = max(0, qty - max(0, remaining_demand))
        # 产能能挡住的量：需求允许但窗口装不下的部分
        fit_after_demand = min(qty, max(0, remaining_demand))
        refused_capacity = max(0, fit_after_demand - max(0, room))

        if accepted == 0:
            reasons = []
            if remaining_demand <= 0:
                reasons.append("采购行剩余需求已足额，无法再确认")
            if room <= 0:
                reasons.append(f"{day} 产能窗口已满或未登记")
            return {
                "result_kind": RESULT_REJECTED,
                "action": ACTION_CONFIRM,
                "accepted_quantity": 0,
                "refused_quantity": qty,
                "refusals": [{"reason": r, "quantity": qty} for r in reasons],
            }

        self.db.allocate_window(conn, window["window_id"], accepted)
        ev = {"commitment_id": f"{line['line_id']}-V{seq:06d}",
              "supplier": supplier, "promised_date": day.isoformat(),
              "quantity": accepted, "action": ACTION_CONFIRM, "seq": seq,
              "confirmed_by": confirmed_by, "message_id": message_id,
              "reason": payload.get("reason"), "transferred_to": None}
        engine.apply(state, ev)
        commitment_id = self._persist_after_apply(
            conn, line, supplier, day, accepted, ACTION_CONFIRM, seq,
            confirmed_by, message_id, payload.get("reason"), [], None, state, ev)

        refusals = []
        if refused_demand:
            refusals.append({"reason": "超量确认：超过采购行剩余需求",
                             "quantity": refused_demand, "kind": "超量确认"})
        if refused_capacity:
            refusals.append({"reason": "超过供应商产能窗口可用量",
                             "quantity": refused_capacity, "kind": "产能不足"})
        return {
            "result_kind": RESULT_PARTIAL if refusals else RESULT_PROCESSED,
            "action": ACTION_CONFIRM,
            "commitment_id": commitment_id,
            "version_seq": seq,
            "requested_quantity": qty,
            "accepted_quantity": accepted,
            "refused_quantity": qty - accepted,
            "refusals": refusals,
            "promised_date": day.isoformat(),
            "cumulative_committed": current_committed + accepted,
            "required": line["quantity"],
            "remaining_demand_after": remaining_demand - accepted,
        }

    def _do_reject(self, conn, state, line, supplier, qty, payload, confirmed_by,
                   message_id, seq) -> dict[str, Any]:
        active_total = sum(c["qty"] for c in state["coverings"].get(supplier, []))
        movable = min(qty, active_total)
        if movable == 0:
            return {"result_kind": RESULT_REJECTED, "action": ACTION_REJECT,
                    "cancelled_quantity": 0, "ignored_quantity": qty,
                    "refusals": [{"reason": "该供应商当前没有生效承诺可拒绝",
                                  "quantity": qty, "kind": "无此承诺"}]}

        # 预演 FIFO 切片 → 释放涉及窗口产能 → 正式应用事件
        portions = engine.peek_supplier(state, supplier, movable)
        for p in portions:
            w = self.db.find_window_lock(conn, supplier, line["material"],
                                         date.fromisoformat(p["date"]))
            if w is not None:
                self.db.allocate_window(conn, w["window_id"], -p["qty"])
        ev = {"commitment_id": f"{line['line_id']}-V{seq:06d}",
              "supplier": supplier,
              "promised_date": portions[0]["date"],
              "quantity": movable, "action": ACTION_REJECT, "seq": seq,
              "confirmed_by": confirmed_by, "message_id": message_id,
              "reason": payload.get("reason"), "transferred_to": None}
        engine.apply(state, ev)
        references = sorted({self._cid_of(conn, line["line_id"], p["seq"])
                             for p in portions} - {None})
        commitment_id = self._persist_after_apply(
            conn, line, supplier, date.fromisoformat(portions[0]["date"]), movable,
            ACTION_REJECT, seq, confirmed_by, message_id, payload.get("reason"),
            references, None, state, ev)

        return {
            "result_kind": RESULT_PARTIAL if qty > movable else RESULT_PROCESSED,
            "action": ACTION_REJECT,
            "commitment_id": commitment_id,
            "version_seq": seq,
            "requested_quantity": qty,
            "cancelled_quantity": movable,
            "ignored_quantity": qty - movable,
            "released_window_dates": sorted({p["date"] for p in portions}),
        }

    def _do_reschedule(self, conn, state, line, supplier, qty, payload,
                       confirmed_by, message_id, seq) -> dict[str, Any]:
        new_day = _parse_date(payload.get("promised_date"), "promised_date")
        new_iso = new_day.isoformat()
        active_total = sum(c["qty"] for c in state["coverings"].get(supplier, []))
        candidate = min(qty, active_total)
        if candidate == 0:
            return {"result_kind": RESULT_REJECTED, "action": ACTION_RESCHEDULE,
                    "moved_quantity": 0, "ignored_quantity": qty,
                    "refusals": [{"reason": "该供应商当前没有生效承诺可改期",
                                  "quantity": qty, "kind": "无此承诺"}]}

        # 纯函数预演 FIFO 切片（state 不变）
        peek = engine.peek_supplier(state, supplier, candidate)

        # 目标日容量：同日改期净占用 0（先释放再占用）；跨日片段占用新窗口。
        # 按 FIFO 逐个片段判定，遇到第一个跨日放不下的片段即停止（不能跳过）。
        new_window = self.db.find_window_lock(conn, supplier, line["material"], new_day)
        cross_room = (new_window["capacity"] - new_window["allocated"]
                      if new_window is not None else 0)
        movable = 0
        stop = False
        for p in peek:
            if stop:
                break
            if p["date"] == new_iso:
                movable += p["qty"]
            else:
                take = min(p["qty"], cross_room)
                movable += take
                if take < p["qty"]:
                    stop = True
                cross_room -= take
        capacity_blocked = candidate - movable

        if movable == 0:
            return {"result_kind": RESULT_REJECTED, "action": ACTION_RESCHEDULE,
                    "moved_quantity": 0, "ignored_quantity": qty,
                    "refusals": [{"reason": f"改期目标日 {new_day} 产能窗口不足或未登记",
                                  "quantity": qty, "kind": "产能不足"}]}

        # 按 movable 重新预演一次，保证切到的是 FIFO 确定前缀
        portions = engine.peek_supplier(state, supplier, movable)

        # 释放旧窗口、占用新窗口
        for p in portions:
            if p["date"] != new_iso:
                old = self.db.find_window_lock(conn, supplier, line["material"],
                                               date.fromisoformat(p["date"]))
                if old is not None:
                    self.db.allocate_window(conn, old["window_id"], -p["qty"])
        target = self._window(conn, supplier, line["material"], new_day)
        cross_moved = movable - sum(p["qty"] for p in portions if p["date"] == new_iso)
        if cross_moved > 0:
            self.db.allocate_window(conn, target["window_id"], cross_moved)

        ev = {"commitment_id": f"{line['line_id']}-V{seq:06d}",
              "supplier": supplier, "promised_date": new_iso,
              "quantity": movable, "action": ACTION_RESCHEDULE, "seq": seq,
              "confirmed_by": confirmed_by, "message_id": message_id,
              "reason": payload.get("reason"), "transferred_to": None}
        engine.apply(state, ev)
        references = sorted({self._cid_of(conn, line["line_id"], p["seq"])
                             for p in portions} - {None})
        commitment_id = self._persist_after_apply(
            conn, line, supplier, new_day, movable, ACTION_RESCHEDULE, seq,
            confirmed_by, message_id, payload.get("reason"), references, None,
            state, ev)

        refusals = []
        over_active = qty - candidate
        if over_active > 0:
            refusals.append({"reason": "超过该供应商生效承诺量",
                             "quantity": over_active, "kind": "无此承诺"})
        if capacity_blocked:
            refusals.append({"reason": f"改期目标日 {new_day} 产能不足",
                             "quantity": capacity_blocked, "kind": "产能不足"})
        return {
            "result_kind": RESULT_PARTIAL if refusals else RESULT_PROCESSED,
            "action": ACTION_RESCHEDULE,
            "commitment_id": commitment_id,
            "version_seq": seq,
            "requested_quantity": qty,
            "moved_quantity": movable,
            "ignored_quantity": qty - movable,
            "refusals": refusals,
            "from_dates": sorted({p["date"] for p in portions}),
            "to_date": new_iso,
        }

    def _do_transfer(self, conn, state, line, supplier, qty, payload,
                     confirmed_by, message_id, seq) -> dict[str, Any]:
        target = payload.get("transferred_to")
        if not target or not isinstance(target, str):
            raise ValidationError("转单必须提供 transferred_to")
        if target == supplier:
            raise ValidationError("转单目标供应商不能与原供应商相同")
        new_day = _parse_date(payload.get("promised_date"), "promised_date")
        new_iso = new_day.isoformat()

        active_total = sum(c["qty"] for c in state["coverings"].get(supplier, []))
        target_window = self.db.find_window_lock(conn, target, line["material"], new_day)
        room = (target_window["capacity"] - target_window["allocated"]
                if target_window is not None else 0)

        # FIFO 逐片段判定受让方容量（同日净占用对受让方不适用，都是跨供应商占用）
        candidate = min(qty, active_total)
        peek = engine.peek_supplier(state, supplier, candidate)
        movable = 0
        for p in peek:
            take = min(p["qty"], room)
            movable += take
            room -= take
            if take < p["qty"]:
                break

        if movable == 0:
            reasons = []
            if active_total == 0:
                reasons.append(("该供应商当前没有生效承诺可转单", qty, "无此承诺"))
            if target_window is None or room <= 0 or target_window["allocated"] >= target_window["capacity"]:
                reasons.append((f"受让方 {target} 在 {new_day} 产能窗口不足或未登记",
                                qty, "产能不足"))
            return {"result_kind": RESULT_REJECTED, "action": ACTION_TRANSFER,
                    "moved_quantity": 0, "ignored_quantity": qty,
                    "target_supplier": target,
                    "refusals": [{"reason": r, "quantity": q, "kind": k}
                                 for r, q, k in reasons]}

        portions = engine.peek_supplier(state, supplier, movable)

        for p in portions:
            old = self.db.find_window_lock(conn, supplier, line["material"],
                                           date.fromisoformat(p["date"]))
            if old is not None:
                self.db.allocate_window(conn, old["window_id"], -p["qty"])
        self.db.allocate_window(conn, target_window["window_id"], movable)

        ev = {"commitment_id": f"{line['line_id']}-V{seq:06d}",
              "supplier": supplier, "promised_date": new_iso,
              "quantity": movable, "action": ACTION_TRANSFER, "seq": seq,
              "confirmed_by": confirmed_by, "message_id": message_id,
              "reason": payload.get("reason"), "transferred_to": target}
        engine.apply(state, ev)
        references = sorted({self._cid_of(conn, line["line_id"], p["seq"])
                             for p in portions} - {None})
        commitment_id = self._persist_after_apply(
            conn, line, supplier, new_day, movable, ACTION_TRANSFER, seq,
            confirmed_by, message_id, payload.get("reason"), references, target,
            state, ev)

        refusals = []
        blocked = qty - movable
        if blocked:
            kinds = []
            if qty > active_total:
                kinds.append("超过生效承诺量")
            kinds.append("受让方产能不足")
            refusals.append({"reason": "转单未受理：" + "、".join(kinds),
                             "quantity": blocked,
                             "kind": "无承诺/产能不足"})
        return {
            "result_kind": RESULT_PARTIAL if refusals else RESULT_PROCESSED,
            "action": ACTION_TRANSFER,
            "commitment_id": commitment_id,
            "version_seq": seq,
            "requested_quantity": qty,
            "moved_quantity": movable,
            "ignored_quantity": blocked,
            "refusals": refusals,
            "target_supplier": target,
            "to_date": new_day.isoformat(),
        }

    def _cid_of(self, conn: sqlite3.Connection, line_id: str, seq: int) -> str | None:
        row = conn.execute(
            "SELECT commitment_id FROM commitment_snapshots WHERE line_id=? AND seq=?",
            (line_id, seq)).fetchone()
        return row["commitment_id"] if row else None

    def _persist_after_apply(self, conn, line, supplier, day: date, qty: int,
                             action: str, seq: int, confirmed_by: str,
                             message_id: str, reason: str | None,
                             references: list[str], transferred_to: str | None,
                             state: dict[str, Any], ev: dict[str, Any]) -> str:
        """engine.apply/手工变更后统一落快照并重算链哈希。"""
        commitment_id = f"{line['line_id']}-V{seq:06d}"
        frozen = engine.freeze(state)
        prev_hash = self._prev_for(conn, line["line_id"], seq)
        chain_payload = {
            "commitment_id": commitment_id,
            "line_id": line["line_id"],
            "supplier": supplier,
            "material": line["material"],
            "promised_date": day.isoformat(),
            "quantity": qty,
            "action": action,
            "seq": seq,
            "status": "生效",
            "confirmed_by": confirmed_by,
            "message_id": message_id,
            "reason": reason,
            "references": sorted(references),
            "transferred_to": transferred_to,
            "state": frozen,
        }
        chain_hash = compute_chain_hash(prev_hash, chain_payload)
        self.db.insert_snapshot(
            conn, commitment_id=commitment_id, line_id=line["line_id"],
            supplier=supplier, material=line["material"], promised_date=day,
            quantity=qty, action=action, seq=seq, status="生效",
            confirmed_by=confirmed_by, message_id=message_id, reason=reason,
            references=references, transferred_to=transferred_to,
            chain_hash=chain_hash, state=frozen)
        return commitment_id

    # ---- 查询视图 ----------------------------------------------------------
    def get_line_view(self, line_id: str) -> dict[str, Any]:
        line = self.db.get_line(line_id)
        if line is None:
            raise NotFoundError(f"采购行不存在：{line_id}")
        rows = self.db.all_snapshots(line_id)
        state = engine.replay(rows, line["quantity"])
        view = engine.coverage_view(state, line["due_date"])
        return {
            "line_id": line_id,
            "material": line["material"],
            "status": line["status"],
            "version": line["version"],
            **view,
        }

    def list_line_views(self) -> list[dict[str, Any]]:
        return [self.get_line_view(r["line_id"]) for r in self.db.list_lines()]

    def get_versions(self, line_id: str) -> dict[str, Any]:
        line = self.db.get_line(line_id)
        if line is None:
            raise NotFoundError(f"采购行不存在：{line_id}")
        rows = self.db.all_snapshots(line_id)
        return {
            "line_id": line_id,
            "versions": [
                {
                    "seq": r["seq"],
                    "commitment_id": r["commitment_id"],
                    "action": r["action"],
                    "supplier": r["supplier"],
                    "promised_date": r["promised_date"],
                    "quantity": r["quantity"],
                    "confirmed_by": r["confirmed_by"],
                    "message_id": r["message_id"],
                    "reason": r["reason"],
                    "references": json.loads(r["references_json"]),
                    "transferred_to": r["transferred_to"],
                    "chain_hash": r["chain_hash"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ],
        }

    def verify_line(self, line_id: str) -> dict[str, Any]:
        """重放哈希链并重算状态，证明快照未被篡改、状态可重现。"""
        line = self.db.get_line(line_id)
        if line is None:
            raise NotFoundError(f"采购行不存在：{line_id}")
        chain = self.db.verify_chain(line_id)
        rows = self.db.all_snapshots(line_id)
        state = engine.replay(rows, line["quantity"])
        frozen = engine.freeze(state)
        state_match = rows and json.loads(rows[-1]["state_json"]) == frozen
        return {**chain, "state_reproducible": bool(state_match) if rows else True}

    def list_capacity_windows(self, supplier: str | None = None) -> list[dict[str, Any]]:
        rows = self.db.all_windows(supplier)
        return [{"window_id": r["window_id"], "supplier": r["supplier"],
                 "material": r["material"], "window_date": r["window_date"],
                 "capacity": r["capacity"], "allocated": r["allocated"],
                 "available": r["capacity"] - r["allocated"],
                 "version": r["version"]} for r in rows]
