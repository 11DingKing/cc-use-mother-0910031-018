"""承诺登记与判定核心服务。

线程安全：所有写操作在同一把锁内完成“校验 -> 落账”，并发邮件回复按到达
顺序串行化，得到确定结果；message_id 唯一索引保证重复消息不会产生第二份承诺。
"""
from __future__ import annotations

import hashlib
import json
import threading
from collections import defaultdict
from datetime import date, datetime
from typing import Callable, Iterable

from .errors import NOT_FOUND, ServiceError
from .models import (
    CapacityWindow,
    CommitmentItem,
    CommitmentVersion,
    DemandBatch,
    ItemChange,
    PurchaseLine,
    Rejection,
)


def parse_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ServiceError("INVALID_DATE", f"{field}必须是 YYYY-MM-DD 日期")


def _positive_int(value, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ServiceError("INVALID_QTY", f"{field}必须是正整数")
    return value


class CommitmentService:
    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._lock = threading.RLock()
        self._clock = clock or datetime.utcnow
        self._lines: dict[str, PurchaseLine] = {}
        self._windows: list[CapacityWindow] = []
        self._versions: dict[str, CommitmentVersion] = {}
        self._line_versions: dict[str, list[str]] = defaultdict(list)
        self._line_seq: dict[str, int] = defaultdict(int)
        self._message_index: dict[tuple[str, str], str] = {}  # (line_id, message_id) -> version_id

    # ------------------------------------------------------------------ 基础登记

    def create_purchase_line(self, payload: dict) -> PurchaseLine:
        line_id = str(payload.get("line_id") or "").strip()
        material = str(payload.get("material_code") or "").strip()
        unit = str(payload.get("unit") or "件").strip()
        if not line_id or not material:
            raise ServiceError("INVALID_LINE", "line_id 与 material_code 必填")
        batches: list[DemandBatch] = []
        seen: set[str] = set()
        for row in payload.get("batches", []):
            bid = str(row.get("batch_id") or "").strip()
            if not bid or bid in seen:
                raise ServiceError("INVALID_BATCH", f"批次编号非法或重复：{bid!r}")
            seen.add(bid)
            batches.append(
                DemandBatch(
                    batch_id=bid,
                    need_date=parse_date(row.get("need_date"), "need_date"),
                    qty=_positive_int(row.get("qty"), "qty"),
                )
            )
        if not batches:
            raise ServiceError("INVALID_BATCH", "至少登记一批需求")
        with self._lock:
            if line_id in self._lines:
                raise ServiceError("LINE_EXISTS", f"采购行 {line_id} 已存在", 409)
            line = PurchaseLine(
                line_id=line_id,
                material_code=material,
                unit=unit,
                batches=batches,
                description=str(payload.get("description") or ""),
            )
            self._lines[line_id] = line
            return line

    def _get_line(self, line_id: str) -> PurchaseLine:
        line = self._lines.get(line_id)
        if line is None:
            raise NOT_FOUND(f"采购行 {line_id}")
        return line

    def set_line_status(self, line_id: str, status: str) -> PurchaseLine:
        with self._lock:
            line = self._get_line(line_id)
            line.status = status  # type: ignore[assignment]
            return line

    def issue_line(self, line_id: str) -> PurchaseLine:
        return self.set_line_status(line_id, "已下达")

    def close_line(self, line_id: str) -> PurchaseLine:
        return self.set_line_status(line_id, "已关闭")

    def add_capacity_window(self, payload: dict) -> CapacityWindow:
        window = CapacityWindow(
            window_id=str(payload.get("window_id") or "").strip(),
            supplier_id=str(payload.get("supplier_id") or "").strip(),
            material_code=str(payload.get("material_code") or "").strip(),
            date_from=parse_date(payload.get("date_from"), "date_from"),
            date_to=parse_date(payload.get("date_to"), "date_to"),
            capacity_qty=_positive_int(payload.get("capacity_qty"), "capacity_qty"),
        )
        if not window.window_id or not window.supplier_id or not window.material_code:
            raise ServiceError("INVALID_WINDOW", "window_id/supplier_id/material_code 必填")
        if window.date_from > window.date_to:
            raise ServiceError("INVALID_WINDOW", "date_from 不能晚于 date_to")
        with self._lock:
            if any(w.window_id == window.window_id for w in self._windows):
                raise ServiceError("WINDOW_EXISTS", f"产能窗口 {window.window_id} 已存在", 409)
            self._windows.append(window)
            return window

    def list_windows(self, supplier_id: str | None = None, material_code: str | None = None) -> list[CapacityWindow]:
        with self._lock:
            return [
                w
                for w in self._windows
                if (supplier_id is None or w.supplier_id == supplier_id)
                and (material_code is None or w.material_code == material_code)
            ]

    # ------------------------------------------------------------------ 查询辅助

    def _active_version(self, line_id: str, supplier_id: str) -> CommitmentVersion | None:
        for vid in reversed(self._line_versions[line_id]):
            v = self._versions[vid]
            if v.supplier_id == supplier_id and v.status == "active":
                return v
        return None

    def _active_versions(self, line_id: str) -> list[CommitmentVersion]:
        return [self._versions[vid] for vid in self._line_versions[line_id] if self._versions[vid].status == "active"]

    @staticmethod
    def _items_by_batch(items: Iterable[CommitmentItem]) -> dict[str, int]:
        totals: dict[str, int] = defaultdict(int)
        for it in items:
            totals[it.batch_id] += it.qty
        return totals

    def _batch_totals(self, line: PurchaseLine) -> dict[str, int]:
        """当前各批次被所有有效版本覆盖的总承诺量。"""
        totals = {b.batch_id: 0 for b in line.batches}
        for v in self._active_versions(line.line_id):
            for it in v.items:
                totals[it.batch_id] = totals.get(it.batch_id, 0) + it.qty
        return totals

    # ------------------------------------------------------------------ 校验

    def _supplier_windows(self, supplier_id: str, material_code: str) -> list[CapacityWindow]:
        return [w for w in self._windows if w.supplier_id == supplier_id and w.material_code == material_code]

    def _check_windows(
        self, supplier_id: str, material_code: str, items: list[CommitmentItem]
    ) -> list[dict]:
        """每个承诺日期必须落在某产能窗口内，且每个窗口承载量不超产能。"""
        windows = self._supplier_windows(supplier_id, material_code)
        problems: list[dict] = []
        for it in items:
            if not any(w.covers(material_code, it.promised_date) for w in windows):
                problems.append(
                    {"batch_id": it.batch_id, "reason": "promised_date 不在任何产能窗口内",
                     "promised_date": it.promised_date.isoformat()}
                )
        for w in windows:
            load = sum(it.qty for it in items if w.covers(material_code, it.promised_date))
            if load > w.capacity_qty:
                problems.append(
                    {"window_id": w.window_id, "reason": "超出窗口产能",
                     "load": load, "capacity_qty": w.capacity_qty,
                     "excess": load - w.capacity_qty}
                )
        return problems

    def _parse_commitment_payload(self, line: PurchaseLine, payload: dict) -> tuple[
        str, str, str, list[CommitmentItem], list[Rejection], str
    ]:
        supplier_id = str(payload.get("supplier_id") or "").strip()
        message_id = str(payload.get("message_id") or "").strip()
        confirmer = str(payload.get("confirmer") or "").strip()
        if not supplier_id or not message_id or not confirmer:
            raise ServiceError("INVALID_COMMITMENT", "supplier_id/message_id/confirmer 必填")
        if not self._supplier_windows(supplier_id, line.material_code):
            raise ServiceError(
                "SUPPLIER_NOT_REGISTERED",
                f"供应商 {supplier_id} 未登记物料 {line.material_code} 的产能窗口",
                422,
            )
        batch_map = {b.batch_id: b for b in line.batches}
        items: list[CommitmentItem] = []
        seen: set[str] = set()
        for row in payload.get("items", []):
            bid = str(row.get("batch_id") or "").strip()
            if bid not in batch_map:
                raise ServiceError("UNKNOWN_BATCH", f"批次 {bid} 不属于采购行 {line.line_id}")
            if bid in seen:
                raise ServiceError("INVALID_ITEMS", f"批次 {bid} 的承诺条目重复")
            seen.add(bid)
            qty = _positive_int(row.get("qty"), "qty")
            items.append(CommitmentItem(batch_id=bid, promised_date=parse_date(row.get("promised_date"), "promised_date"), qty=qty))
        rejections: list[Rejection] = []
        rej_seen: set[str] = set()
        for row in payload.get("rejections", []):
            bid = str(row.get("batch_id") or "").strip()
            if bid not in batch_map:
                raise ServiceError("UNKNOWN_BATCH", f"拒绝批次 {bid} 不属于采购行 {line.line_id}")
            if bid in rej_seen:
                raise ServiceError("INVALID_ITEMS", f"批次 {bid} 的拒绝条目重复")
            rej_seen.add(bid)
            qty = _positive_int(row.get("qty"), "rejection.qty")
            reason = str(row.get("reason") or "未说明").strip()
            rejections.append(Rejection(batch_id=bid, qty=qty, reason=reason))
        if not items and not rejections:
            raise ServiceError("EMPTY_MESSAGE", "承诺与拒绝不能同时为空")
        # 同一供应商对同一批次的承诺量+拒绝量不能超过需求量
        item_totals = self._items_by_batch(items)
        for r in rejections:
            if item_totals.get(r.batch_id, 0) + r.qty > batch_map[r.batch_id].qty:
                raise ServiceError(
                    "INVALID_REJECTION",
                    f"供应商 {supplier_id} 对批次 {r.batch_id} 的承诺+拒绝数量超过需求量",
                )
        return supplier_id, message_id, confirmer, items, rejections, str(payload.get("remark") or "")

    def _diff_items(
        self, prev: list[CommitmentItem], items: list[CommitmentItem]
    ) -> list[ItemChange]:
        prev_map = {it.batch_id: it for it in prev}
        new_map = {it.batch_id: it for it in items}
        changes: list[ItemChange] = []
        for bid in sorted(set(prev_map) | set(new_map)):
            old = prev_map.get(bid)
            new = new_map.get(bid)
            if old is None and new is not None:
                changes.append(ItemChange(bid, "new", new.qty, None, new.promised_date))
            elif old is not None and new is None:
                changes.append(ItemChange(bid, "withdrawn", -old.qty, old.promised_date, None))
            elif old is not None and new is not None:
                delta = new.qty - old.qty
                if new.promised_date != old.promised_date and delta == 0:
                    ctype = "rescheduled"
                elif delta > 0:
                    ctype = "increased"
                elif delta < 0:
                    ctype = "decreased"
                else:
                    ctype = "unchanged"
                changes.append(ItemChange(bid, ctype, delta, old.promised_date, new.promised_date))
        return changes

    # ------------------------------------------------------------------ 快照

    @staticmethod
    def _canonical(payload: dict) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _build_snapshot(
        self, line: PurchaseLine, version: CommitmentVersion, state_after: dict
    ) -> tuple[dict, str]:
        snap = {
            "version_id": version.version_id,
            "sequence": version.sequence,
            "line_id": version.line_id,
            "material_code": line.material_code,
            "supplier_id": version.supplier_id,
            "message_id": version.message_id,
            "confirmer": version.confirmer,
            "kind": version.kind,
            "created_at": version.created_at.isoformat() + "Z",
            "items": [it.to_dict() for it in version.items],
            "rejections": [r.to_dict() for r in version.rejections],
            "superseded_version_ids": list(version.superseded_version_ids),
            "related_version_id": version.related_version_id,
            "state_after": state_after,
        }
        digest = hashlib.sha256(self._canonical(snap).encode("utf-8")).hexdigest()
        return snap, digest

    def _state_after(self, line: PurchaseLine) -> dict:
        totals = self._batch_totals(line)
        state: dict[str, dict] = {}
        for b in line.batches:
            committed = totals.get(b.batch_id, 0)
            state[b.batch_id] = {
                "demand": b.qty,
                "committed": committed,
                "gap": max(0, b.qty - committed),
                "over": max(0, committed - b.qty),
            }
        return state

    def _next_version(self, line_id: str, supplier_id: str, message_id: str, confirmer: str) -> CommitmentVersion:
        self._line_seq[line_id] += 1
        seq = self._line_seq[line_id]
        return CommitmentVersion(
            version_id=f"{line_id}-V{seq:03d}",
            sequence=seq,
            line_id=line_id,
            supplier_id=supplier_id,
            message_id=message_id,
            confirmer=confirmer,
            created_at=self._clock(),
        )

    def _store(self, line: PurchaseLine, version: CommitmentVersion, state_after: dict) -> None:
        snap, digest = self._build_snapshot(line, version, state_after)
        version.snapshot = snap
        version.snapshot_hash = digest
        self._versions[version.version_id] = version
        self._line_versions[line.line_id].append(version.version_id)
        self._message_index[(line.line_id, version.message_id)] = version.version_id

    # ------------------------------------------------------------------ 提交承诺

    def submit_commitment(self, payload: dict) -> tuple[CommitmentVersion, bool]:
        """登记一封供应商确认邮件。返回 (版本, 是否幂等重放)。"""
        line_id = str(payload.get("line_id") or "").strip()
        mode = str(payload.get("mode") or "strict")
        if mode not in ("strict", "allow_over"):
            raise ServiceError("INVALID_MODE", "mode 仅支持 strict / allow_over")
        with self._lock:
            line = self._get_line(line_id)
            message_id = str(payload.get("message_id") or "").strip()
            if message_id and (line_id, message_id) in self._message_index:
                # 重复消息（含并发同时到达）：直接返回首份承诺，不再建版本
                return self._versions[self._message_index[(line_id, message_id)]], True
            if line.status == "草拟":
                raise ServiceError("LINE_NOT_ISSUED", "采购计划尚未下达，不能接收承诺", 409)
            if line.status == "已关闭":
                raise ServiceError("LINE_CLOSED", "采购行已关闭", 409)

            supplier_id, message_id, confirmer, items, rejections, remark = self._parse_commitment_payload(line, payload)
            if (line_id, message_id) in self._message_index:  # 双检，防并发
                return self._versions[self._message_index[(line_id, message_id)]], True

            # 产能窗口校验
            window_problems = self._check_windows(supplier_id, line.material_code, items)
            if window_problems:
                err = ServiceError("CAPACITY_VIOLATION", "承诺日期或数量违反产能窗口", 422)
                err.details = {"problems": window_problems}  # type: ignore[attr-defined]
                raise err

            # 累计数量校验：本供应商新版本替换其旧版本
            prev = self._active_version(line_id, supplier_id)
            prev_totals = self._items_by_batch(prev.items) if prev else {}
            new_totals = self._items_by_batch(items)
            current = self._batch_totals(line)
            over_rows: list[dict] = []
            for b in line.batches:
                others = current.get(b.batch_id, 0) - prev_totals.get(b.batch_id, 0)
                projected = others + new_totals.get(b.batch_id, 0)
                if projected > b.qty:
                    over_rows.append(
                        {"batch_id": b.batch_id, "demand": b.qty, "projected": projected,
                         "excess": projected - b.qty, "max_acceptable": max(0, b.qty - others)}
                    )
            if over_rows and mode == "strict":
                err = ServiceError("OVER_COMMITTED", "累计承诺量超过需求量", 409)
                err.details = {"over": over_rows}  # type: ignore[attr-defined]
                raise err

            version = self._next_version(line_id, supplier_id, message_id, confirmer)
            version.items = items
            version.rejections = rejections
            version.mode = mode
            version.remark = remark or ("超量确认已登记" if over_rows else "")
            version.item_changes = self._diff_items(prev.items if prev else [], items)
            if prev is not None:
                prev.status = "superseded"
                version.superseded_version_ids.append(prev.version_id)

            self._message_index[(line_id, message_id)] = version.version_id  # 先占位再落账
            state_after = self._projected_state(line, current, prev_totals, new_totals)
            self._store(line, version, state_after)
            if line.status == "已下达":
                line.status = "履行中"
            return version, False

    @staticmethod
    def _projected_state(line: PurchaseLine, current: dict, prev_totals: dict, new_totals: dict) -> dict:
        state: dict[str, dict] = {}
        for b in line.batches:
            projected = current.get(b.batch_id, 0) - prev_totals.get(b.batch_id, 0) + new_totals.get(b.batch_id, 0)
            state[b.batch_id] = {
                "demand": b.qty,
                "committed": projected,
                "gap": max(0, b.qty - projected),
                "over": max(0, projected - b.qty),
            }
        return state

    # ------------------------------------------------------------------ 改期

    def reschedule_commitment(self, payload: dict) -> tuple[CommitmentVersion, bool]:
        """供应商对既有承诺改期（数量默认沿用当前有效版本）。"""
        line_id = str(payload.get("line_id") or "").strip()
        supplier_id = str(payload.get("supplier_id") or "").strip()
        message_id = str(payload.get("message_id") or "").strip()
        confirmer = str(payload.get("confirmer") or "").strip()
        if not message_id or not confirmer:
            raise ServiceError("INVALID_COMMITMENT", "message_id/confirmer 必填")
        with self._lock:
            line = self._get_line(line_id)
            prev = self._active_version(line_id, supplier_id)
            if prev is None:
                raise ServiceError("NO_ACTIVE_COMMITMENT", f"供应商 {supplier_id} 尚无有效承诺，无法改期", 409)
            overrides: dict[str, tuple[date, int | None]] = {}
            for row in payload.get("items", []):
                bid = str(row.get("batch_id") or "").strip()
                if bid not in {b.batch_id: b for b in line.batches}:
                    raise ServiceError("UNKNOWN_BATCH", f"批次 {bid} 不属于采购行 {line.line_id}")
                qty = row.get("qty")
                overrides[bid] = (
                    parse_date(row.get("promised_date"), "promised_date"),
                    _positive_int(qty, "qty") if qty is not None else None,
                )
            if not overrides:
                raise ServiceError("INVALID_ITEMS", "改期至少包含一个批次")
            items: list[CommitmentItem] = []
            prev_map = {it.batch_id: it for it in prev.items}
            for bid, (new_date, qty) in overrides.items():
                old = prev_map.get(bid)
                if old is None:
                    raise ServiceError("NO_ACTIVE_COMMITMENT", f"批次 {bid} 上无该供应商的有效承诺", 409)
                items.append(CommitmentItem(bid, new_date, qty or old.qty))
            forwarded = {
                "line_id": line_id,
                "supplier_id": supplier_id,
                "message_id": payload.get("message_id"),
                "confirmer": payload.get("confirmer"),
                "mode": payload.get("mode", "strict"),
                "remark": payload.get("remark", "供应商改期"),
                "items": [it.to_dict() for it in items],
                "rejections": [r.to_dict() for r in prev.rejections],
            }
            return self.submit_commitment(forwarded)

    # ------------------------------------------------------------------ 转单

    def transfer_commitment(self, payload: dict) -> tuple[CommitmentVersion, CommitmentVersion, bool]:
        """把原供应商的（部分）承诺转给另一供应商，原子生成转出/转入两条版本。"""
        line_id = str(payload.get("line_id") or "").strip()
        from_supplier = str(payload.get("from_supplier_id") or "").strip()
        to_supplier = str(payload.get("to_supplier_id") or "").strip()
        message_id = str(payload.get("message_id") or "").strip()
        confirmer = str(payload.get("confirmer") or "").strip()
        reason = str(payload.get("reason") or "供应商转单").strip()
        if not message_id or not confirmer:
            raise ServiceError("INVALID_TRANSFER", "message_id/confirmer 必填")
        with self._lock:
            line = self._get_line(line_id)
            if (line_id, message_id) in self._message_index:
                out = self._versions[self._message_index[(line_id, message_id)]]
                related = self._versions[out.related_version_id] if out.related_version_id else out
                return out, related, True
            if line.status not in ("已下达", "履行中"):
                raise ServiceError("LINE_NOT_ISSUED", "采购计划未处于可转单状态", 409)
            if from_supplier == to_supplier:
                raise ServiceError("INVALID_TRANSFER", "转入与转出供应商不能相同")
            from_prev = self._active_version(line_id, from_supplier)
            if from_prev is None:
                raise ServiceError("NO_ACTIVE_COMMITMENT", f"供应商 {from_supplier} 无有效承诺可转出", 409)
            if not self._supplier_windows(to_supplier, line.material_code):
                raise ServiceError("SUPPLIER_NOT_REGISTERED", f"承接供应商 {to_supplier} 未登记产能窗口", 422)

            batch_map = {b.batch_id: b for b in line.batches}
            transfers: dict[str, tuple[int, date]] = {}
            for row in payload.get("items", []):
                bid = str(row.get("batch_id") or "").strip()
                if bid not in batch_map:
                    raise ServiceError("UNKNOWN_BATCH", f"批次 {bid} 不属于采购行 {line.line_id}")
                if bid in transfers:
                    raise ServiceError("INVALID_ITEMS", f"批次 {bid} 转单条目重复")
                transfers[bid] = (
                    _positive_int(row.get("qty"), "qty"),
                    parse_date(row.get("promised_date"), "promised_date"),
                )
            if not transfers:
                raise ServiceError("INVALID_TRANSFER", "转单至少包含一个批次")

            from_rows = self._items_by_batch(from_prev.items)
            for bid, (qty, _d) in transfers.items():
                if qty > from_rows.get(bid, 0):
                    raise ServiceError(
                        "TRANSFER_EXCEEDS_HELD",
                        f"批次 {bid} 转出量 {qty} 超过 {from_supplier} 持有承诺 {from_rows.get(bid, 0)}",
                        409,
                    )

            # 转出后剩余承诺
            remaining_rows: dict[str, int] = dict(from_rows)
            for bid, (qty, _d) in transfers.items():
                remaining_rows[bid] = remaining_rows[bid] - qty
            # 用一条代表性日期重建剩余条目（沿用原条目，按数量扣减）
            date_pick = {it.batch_id: it.promised_date for it in from_prev.items}
            remaining_items = [
                CommitmentItem(bid, date_pick[bid], qty)
                for bid, qty in sorted(remaining_rows.items())
                if qty > 0
            ]
            # 转入方新承诺 = 其当前承诺 + 转入量
            to_prev = self._active_version(line_id, to_supplier)
            to_rows = self._items_by_batch(to_prev.items) if to_prev else {}
            to_date_pick = {it.batch_id: it.promised_date for it in (to_prev.items if to_prev else [])}
            merged_rows: dict[str, int] = {bid: qty for bid, qty in to_rows.items() if qty > 0}
            merged_dates: dict[str, date] = dict(to_date_pick)
            for bid, (qty, promised) in sorted(transfers.items()):
                merged_rows[bid] = merged_rows.get(bid, 0) + qty
                merged_dates[bid] = promised  # 转入指令刷新该批次承诺日期
            in_items: list[CommitmentItem] = [
                CommitmentItem(bid, merged_dates[bid], qty) for bid, qty in sorted(merged_rows.items())
            ]

            # 窗口校验
            for supplier, items in ((from_supplier, remaining_items), (to_supplier, in_items)):
                problems = self._check_windows(supplier, line.material_code, items)
                if problems:
                    err = ServiceError("CAPACITY_VIOLATION", f"供应商 {supplier} 转单后违反产能窗口", 422)
                    err.details = {"supplier_id": supplier, "problems": problems}  # type: ignore[attr-defined]
                    raise err

            # 转单守恒：转出量 = 转入量，转单前后累计承诺总量不变，
            # 因此不再做超量判定（既存超量是此前 allow_over 明确登记的，不阻塞转单）。
            # 仅保留双方各自的产能窗口校验（上方）。

            out_version = self._next_version(line_id, from_supplier, message_id, confirmer)
            out_version.kind = "transfer_out"
            out_version.reason = reason
            out_version.items = remaining_items
            out_version.rejections = list(from_prev.rejections)
            out_version.item_changes = [
                ItemChange(bid, "transferred_out", -qty, date_pick.get(bid), d)
                for bid, (qty, d) in sorted(transfers.items())
            ] + [
                ItemChange(bid, "unchanged", 0, date_pick.get(bid), date_pick.get(bid))
                for bid, qty in sorted(remaining_rows.items())
                if bid not in transfers
            ]
            from_prev.status = "superseded"
            out_version.superseded_version_ids.append(from_prev.version_id)

            in_message = f"{message_id}#transfer-in:{to_supplier}"
            in_version = self._next_version(line_id, to_supplier, in_message,
                                            str(payload.get("to_confirmer") or f"{to_supplier} 系统承接"))
            in_version.kind = "transfer_in"
            in_version.reason = reason
            in_version.items = in_items
            in_version.rejections = list(to_prev.rejections) if to_prev else []
            in_version.item_changes = [
                ItemChange(bid, "transferred_in", qty, to_date_pick.get(bid), d)
                for bid, (qty, d) in sorted(transfers.items())
            ]
            if to_prev is not None:
                to_prev.status = "superseded"
                in_version.superseded_version_ids.append(to_prev.version_id)
            in_version.related_version_id = out_version.version_id
            out_version.related_version_id = in_version.version_id

            # 落账（两条版本同一把锁，外部观察到的永远是成对结果）
            self._message_index[(line_id, message_id)] = out_version.version_id
            self._message_index[(line_id, in_message)] = in_version.version_id
            self._versions[out_version.version_id] = out_version
            self._line_versions[line_id].append(out_version.version_id)
            self._versions[in_version.version_id] = in_version
            self._line_versions[line_id].append(in_version.version_id)
            state_after = self._state_after(line)
            for v in (out_version, in_version):
                snap, digest = self._build_snapshot(line, v, state_after)
                v.snapshot, v.snapshot_hash = snap, digest
            if line.status == "已下达":
                line.status = "履行中"
            return out_version, in_version, False

    # ------------------------------------------------------------------ 视图与归因

    def get_version(self, version_id: str) -> CommitmentVersion:
        with self._lock:
            v = self._versions.get(version_id)
            if v is None:
                raise NOT_FOUND(f"承诺版本 {version_id}")
            return v

    def lookup_message(self, message_id: str, line_id: str | None = None) -> CommitmentVersion:
        with self._lock:
            if line_id is not None:
                vid = self._message_index.get((line_id, message_id))
                if vid is None:
                    raise NOT_FOUND(f"采购行 {line_id} 的消息 {message_id}")
                return self._versions[vid]
            matches = [vid for (lid, mid), vid in self._message_index.items() if mid == message_id]
            if not matches:
                raise NOT_FOUND(f"消息 {message_id}")
            if len(matches) > 1:
                err = ServiceError("AMBIGUOUS_MESSAGE", f"消息号 {message_id} 命中多个采购行，请指定 line_id", 409)
                err.details = {"version_ids": matches}  # type: ignore[attr-defined]
                raise err
            return self._versions[matches[0]]

    def list_versions(self, line_id: str) -> list[CommitmentVersion]:
        with self._lock:
            self._get_line(line_id)
            return [self._versions[vid] for vid in self._line_versions[line_id]]

    def _responsibility(self, line: PurchaseLine) -> dict[str, dict]:
        """按版本顺序回放每批需求，定位造成当前缺口的责任方。"""
        batches = line.batches
        # 每供应商在每版本生效后对该批次的持有量（版本链语义：新版本替换该供应商旧版本）
        held: dict[tuple[str, str], int] = defaultdict(int)
        current_rejections: dict[tuple[str, str], Rejection] = {}
        latest_version_of: dict[str, CommitmentVersion] = {}
        decisive: dict[str, dict] = {}
        ever_covered = {b.batch_id: False for b in batches}
        participated: dict[str, set[str]] = defaultdict(set)

        for vid in self._line_versions[line.line_id]:
            v = self._versions[vid]
            rows = self._items_by_batch(v.items)
            for b in batches:
                bid = b.batch_id
                before = sum(held[(s, bid)] for s in latest_version_of)
                held[(v.supplier_id, bid)] = rows.get(bid, 0)
                after = sum(held[(s, bid)] for s in (set(latest_version_of) | {v.supplier_id}))
                if rows.get(bid, 0) > 0 or any(r.batch_id == bid for r in v.rejections):
                    participated[bid].add(v.supplier_id)
                if after >= b.qty:
                    ever_covered[bid] = True
                cur_r = next((r for r in v.rejections if r.batch_id == bid), None)
                if cur_r is not None:
                    current_rejections[(v.supplier_id, bid)] = cur_r
                else:
                    current_rejections.pop((v.supplier_id, bid), None)
                if before >= b.qty > after:
                    decisive[bid] = {
                        "supplier_id": v.supplier_id,
                        "version_id": v.version_id,
                        "message_id": v.message_id,
                        "confirmer": v.confirmer,
                        "changed_at": v.created_at.isoformat() + "Z",
                        "reason": "转出承诺导致缺口" if v.kind == "transfer_out" else "数量下调/撤回导致缺口",
                        "kind": v.kind,
                    }
            latest_version_of[v.supplier_id] = v

        result: dict[str, dict] = {}
        totals = self._batch_totals(line)
        for b in line.batches:
            bid = b.batch_id
            committed = totals.get(bid, 0)
            rejected = [
                {
                    "supplier_id": s,
                    "qty": r.qty,
                    "reason": r.reason,
                    "version_id": latest_version_of[s].version_id,
                    "message_id": latest_version_of[s].message_id,
                    "confirmer": latest_version_of[s].confirmer,
                }
                for (s, b2), r in current_rejections.items()
                if b2 == bid
            ]
            late = [
                {
                    "supplier_id": v.supplier_id,
                    "version_id": v.version_id,
                    "promised_date": it.promised_date.isoformat(),
                    "qty": it.qty,
                    "confirmer": v.confirmer,
                }
                for v in self._active_versions(line.line_id)
                for it in v.items
                if it.batch_id == bid and it.promised_date > b.need_date
            ]
            attribution: list[dict]
            if committed >= b.qty:
                attribution = []
            elif rejected:
                attribution = [{**r, "cause": "partial_rejection"} for r in rejected]
            elif bid in decisive:
                attribution = [{**decisive[bid], "cause": "coverage_broken"}]
            elif not participated[bid]:
                attribution = [{"cause": "no_response", "reason": "无供应商响应"}]
            else:
                attribution = [
                    {
                        "cause": "never_fully_covered",
                        "reason": "从未足额确认",
                        "supplier_id": s,
                        "version_id": latest_version_of[s].version_id,
                        "confirmer": latest_version_of[s].confirmer,
                    }
                    for s in sorted(participated[bid])
                ]
            result[bid] = {
                "ever_fully_covered": ever_covered[bid],
                "rejections": rejected,
                "late_commitments": late,
                "attribution": attribution,
            }
        return result

    def get_line_view(self, line_id: str) -> dict:
        """展示每批需求的承诺覆盖、缺口与变更责任。"""
        with self._lock:
            line = self._get_line(line_id)
            totals = self._batch_totals(line)
            responsibility = self._responsibility(line)
            batches_out = []
            for b in line.batches:
                committed = totals.get(b.batch_id, 0)
                coverage = []
                for v in self._active_versions(line.line_id):
                    for it in v.items:
                        if it.batch_id == b.batch_id:
                            coverage.append(
                                {
                                    "supplier_id": v.supplier_id,
                                    "qty": it.qty,
                                    "promised_date": it.promised_date.isoformat(),
                                    "late": it.promised_date > b.need_date,
                                    "version_id": v.version_id,
                                    "message_id": v.message_id,
                                    "confirmer": v.confirmer,
                                    "confirmed_at": v.created_at.isoformat() + "Z",
                                }
                            )
                batches_out.append(
                    {
                        **b.to_dict(),
                        "committed_qty": committed,
                        "covered_qty": min(committed, b.qty),
                        "gap_qty": max(0, b.qty - committed),
                        "over_qty": max(0, committed - b.qty),
                        "coverage_rate": round(min(committed, b.qty) / b.qty, 4),
                        "coverage": coverage,
                        "responsibility": responsibility[b.batch_id],
                    }
                )
            return {
                "line": line.to_dict(),
                "summary": {
                    "demand_qty": line.total_qty,
                    "committed_qty": sum(x["committed_qty"] for x in batches_out),
                    "gap_qty": sum(x["gap_qty"] for x in batches_out),
                    "over_qty": sum(x["over_qty"] for x in batches_out),
                    "active_supplier_count": len({
                        v.supplier_id
                        for v in self._active_versions(line.line_id)
                        if any(it.qty > 0 for it in v.items)
                    }),
                    "version_count": len(self._line_versions[line.line_id]),
                },
                "batches": batches_out,
                "version_chain": [
                    {
                        "version_id": self._versions[vid].version_id,
                        "sequence": self._versions[vid].sequence,
                        "supplier_id": self._versions[vid].supplier_id,
                        "kind": self._versions[vid].kind,
                        "status": self._versions[vid].status,
                        "message_id": self._versions[vid].message_id,
                        "confirmer": self._versions[vid].confirmer,
                        "created_at": self._versions[vid].created_at.isoformat() + "Z",
                        "superseded_version_ids": list(self._versions[vid].superseded_version_ids),
                        "related_version_id": self._versions[vid].related_version_id,
                        "item_changes": [c.to_dict() for c in self._versions[vid].item_changes],
                    }
                    for vid in self._line_versions[line.line_id]
                ],
            }
