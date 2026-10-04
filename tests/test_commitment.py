"""承诺服务领域行为测试。"""
from __future__ import annotations

import sqlite3
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from commitment.models import LINE_RELEASED  # noqa: E402
from commitment.service import (  # noqa: E402
    CommitmentService,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from commitment.storage import Storage  # noqa: E402


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Storage(":memory:")
        self.svc = CommitmentService(self.db)

    # 便捷构造：下达一条 100 件、到期 2026-11-30 的采购行
    def seed_line(self, line_id: str = "L1", qty: int = 100,
                  due: str = "2026-11-30") -> None:
        self.svc.register_line(line_id, "MAT-X", qty, due)
        self.svc.release_line(line_id)

    def window(self, wid: str, supplier: str, day: str, cap: int,
               material: str = "MAT-X") -> None:
        self.svc.register_capacity_window(wid, supplier, material, day, cap)

    def msg(self, mid: str, supplier: str, action: str, qty: int,
            day: str | None = None, by: str | None = None,
            target: str | None = None, reason: str | None = None,
            line_id: str = "L1") -> dict:
        payload: dict = {"action": action, "line_id": line_id,
                         "supplier": supplier, "quantity": qty,
                         "confirmed_by": by or f"{supplier}-计划员"}
        if day:
            payload["promised_date"] = day
        if target:
            payload["transferred_to"] = target
        if reason:
            payload["reason"] = reason
        return self.svc.handle_message(mid, f"{supplier}@example.com", payload)


class RegistrationTest(ServiceTestBase):
    def test_register_and_release(self) -> None:
        r = self.svc.register_line("L1", "MAT-X", 100, "2026-11-30")
        self.assertEqual(r["status"], "待确认")
        self.assertEqual(self.svc.release_line("L1")["status"], "已下达")
        # 重复下达幂等
        self.assertFalse(self.svc.release_line("L1")["changed"])

    def test_register_validation(self) -> None:
        with self.assertRaises(ValidationError):
            self.svc.register_line("", "M", 10, "2026-11-30")
        with self.assertRaises(ValidationError):
            self.svc.register_line("L", "M", 0, "2026-11-30")
        with self.assertRaises(ValidationError):
            self.svc.register_line("L", "M", 10, "bad-date")
        with self.assertRaises(ConflictError):
            self.svc.register_line("L1", "MAT-X", 10, "2026-11-30")
            self.svc.register_line("L1", "MAT-X", 10, "2026-11-30")

    def test_message_before_release_rejected(self) -> None:
        self.svc.register_line("L1", "MAT-X", 10, "2026-11-30")
        self.window("W1", "A", "2026-11-20", 10)
        with self.assertRaises(ConflictError):
            self.msg("m1", "A", "确认", 10, "2026-11-20")

    def test_missing_line_and_window(self) -> None:
        with self.assertRaises(NotFoundError):
            self.svc.get_line_view("NOPE")
        self.seed_line()
        # 行存在但窗口未登记 → 校验错误
        with self.assertRaises(ValidationError):
            self.svc.handle_message("m1", "a@e.com", {
                "action": "确认", "line_id": "L1", "supplier": "A",
                "quantity": 10, "promised_date": "2026-11-20"})


class ConfirmationTest(ServiceTestBase):
    def test_multi_supplier_fifo_coverage(self) -> None:
        self.seed_line()
        self.window("W1", "A", "2026-11-20", 60)
        self.window("W2", "B", "2026-11-25", 60)
        r1 = self.msg("m1", "A", "确认", 60, "2026-11-20", by="张三")
        r2 = self.msg("m2", "B", "确认", 60, "2026-11-25", by="李四")
        self.assertEqual(r1["accepted_quantity"], 60)
        self.assertEqual(r2["accepted_quantity"], 40)  # 只剩 40 需求
        self.assertEqual(r2["result_kind"], "部分接受")
        self.assertEqual(r2["refusals"][0]["kind"], "超量确认")
        view = self.svc.get_line_view("L1")
        self.assertTrue(view["covered"])
        self.assertEqual(view["gap"], 0)

    def test_over_confirm_when_fully_covered_is_refused(self) -> None:
        self.seed_line()
        self.window("W1", "A", "2026-11-20", 100)
        self.msg("m1", "A", "确认", 100, "2026-11-20")
        r = self.msg("m2", "A", "确认", 50, "2026-11-20")
        self.assertEqual(r["result_kind"], "已拒绝")
        self.assertEqual(r["accepted_quantity"], 0)

    def test_capacity_window_caps_acceptance(self) -> None:
        self.seed_line()
        self.window("W1", "A", "2026-11-20", 30)
        r = self.msg("m1", "A", "确认", 80, "2026-11-20")
        self.assertEqual(r["accepted_quantity"], 30)
        self.assertEqual(r["refusals"][0]["kind"], "产能不足")
        # 窗口已满，再来一封仍然 0
        r2 = self.msg("m2", "A", "确认", 10, "2026-11-20")
        self.assertEqual(r2["accepted_quantity"], 0)


class RejectionTest(ServiceTestBase):
    def test_partial_reject_causes_traced_gap(self) -> None:
        self.seed_line()
        self.window("W1", "A", "2026-11-20", 60)
        self.window("W2", "B", "2026-11-25", 60)
        self.msg("m1", "A", "确认", 60, "2026-11-20", by="张三")
        self.msg("m2", "B", "确认", 40, "2026-11-25", by="李四")
        r = self.msg("m3", "A", "拒绝", 20, reason="原料断供", by="张三")
        self.assertEqual(r["cancelled_quantity"], 20)
        view = self.svc.get_line_view("L1")
        self.assertEqual(view["gap"], 20)
        self.assertEqual(len(view["gap_responsibility"]), 1)
        blame = view["gap_responsibility"][0]
        self.assertEqual(blame["supplier"], "A")
        self.assertEqual(blame["confirmed_by"], "张三")
        self.assertEqual(blame["message_id"], "m3")
        self.assertEqual(blame["reason"], "原料断供")
        # 窗口产能被释放
        win = [w for w in self.svc.list_capacity_windows() if w["window_id"] == "W1"][0]
        self.assertEqual(win["allocated"], 40)

    def test_reject_more_than_active_is_partial(self) -> None:
        self.seed_line()
        self.window("W1", "A", "2026-11-20", 60)
        self.msg("m1", "A", "确认", 60, "2026-11-20")
        r = self.msg("m2", "A", "拒绝", 80)
        self.assertEqual(r["result_kind"], "部分接受")
        self.assertEqual(r["cancelled_quantity"], 60)
        self.assertEqual(r["ignored_quantity"], 20)

    def test_refilled_gap_clears_blame(self) -> None:
        self.seed_line()
        self.window("W1", "A", "2026-11-20", 60)
        self.window("W2", "B", "2026-11-25", 200)
        self.msg("m1", "A", "确认", 60, "2026-11-20")
        self.msg("m2", "A", "拒绝", 60)
        self.assertEqual(self.svc.get_line_view("L1")["gap"], 100)
        # 缺口按 FIFO 填补：40 待确认 + 60 欠交，补 100 即全部消除
        self.msg("m3", "B", "确认", 100, "2026-11-25")
        view = self.svc.get_line_view("L1")
        self.assertTrue(view["covered"])
        self.assertEqual(view["gap_responsibility"], [])


class RescheduleTest(ServiceTestBase):
    def test_reschedule_moves_without_gap_but_traces_lateness(self) -> None:
        self.seed_line(due="2026-11-30")
        self.window("W1", "A", "2026-11-20", 60)
        self.window("W2", "A", "2026-12-10", 60)
        self.window("W3", "B", "2026-11-25", 40)
        self.msg("m1", "A", "确认", 60, "2026-11-20", by="张三")
        self.msg("m0", "B", "确认", 40, "2026-11-25", by="李四")  # 先铺满
        r = self.msg("m2", "A", "改期", 30, "2026-12-10", by="张三", reason="检修")
        self.assertEqual(r["moved_quantity"], 30)
        view = self.svc.get_line_view("L1")
        self.assertEqual(view["gap"], 0)            # 总量仍覆盖
        self.assertEqual(view["on_time_gap"], 30)   # 但 30 件迟交
        self.assertEqual(view["schedule_responsibility"][0]["type"], "供应商改期")
        self.assertEqual(view["schedule_responsibility"][0]["message_id"], "m2")

    def test_reschedule_blocked_by_target_capacity(self) -> None:
        self.seed_line()
        self.window("W1", "A", "2026-11-20", 100)
        self.window("W2", "A", "2026-12-10", 10)
        self.msg("m1", "A", "确认", 100, "2026-11-20")
        r = self.msg("m2", "A", "改期", 50, "2026-12-10")
        self.assertEqual(r["result_kind"], "部分接受")
        self.assertEqual(r["moved_quantity"], 10)
        self.assertEqual(r["ignored_quantity"], 40)
        view = self.svc.get_line_view("L1")
        self.assertEqual(view["committed"], 100)  # 没动的部分仍在


class TransferTest(ServiceTestBase):
    def test_transfer_moves_supplier_and_date(self) -> None:
        self.seed_line(due="2026-11-30")
        self.window("WA", "A", "2026-11-20", 60)
        self.window("WB", "B", "2026-11-25", 40)
        self.window("WC", "C", "2026-12-05", 60)
        self.msg("m1", "A", "确认", 60, "2026-11-20", by="张三")
        self.msg("m0", "B", "确认", 40, "2026-11-25", by="李四")  # 先铺满
        r = self.msg("m2", "A", "转单", 20, "2026-12-05", target="C", by="王五")
        self.assertEqual(r["moved_quantity"], 20)
        view = self.svc.get_line_view("L1")
        suppliers = {c["supplier"] for c in view["active_commitments"]}
        self.assertEqual(suppliers, {"A", "B", "C"})
        self.assertEqual(view["on_time_gap"], 20)
        self.assertEqual(view["schedule_responsibility"][0]["type"], "转单导致延期")
        # 原窗口释放、新窗口占用
        alloc = {w["window_id"]: w["allocated"]
                 for w in self.svc.list_capacity_windows()}
        self.assertEqual(alloc["WA"], 40)
        self.assertEqual(alloc["WC"], 20)

    def test_transfer_without_target_capacity_refused(self) -> None:
        self.seed_line()
        self.window("WA", "A", "2026-11-20", 60)
        self.window("WC", "C", "2026-11-25", 10)
        self.msg("m1", "A", "确认", 60, "2026-11-20")
        r = self.msg("m2", "A", "转单", 30, "2026-11-25", target="C")
        self.assertEqual(r["result_kind"], "部分接受")
        self.assertEqual(r["moved_quantity"], 10)
        self.assertEqual(r["ignored_quantity"], 20)

    def test_transfer_to_self_rejected(self) -> None:
        self.seed_line()
        self.window("WA", "A", "2026-11-20", 10)
        self.msg("m1", "A", "确认", 10, "2026-11-20")
        with self.assertRaises(ValidationError):
            self.msg("m2", "A", "转单", 5, "2026-11-25", target="A")


class IdempotencyTest(ServiceTestBase):
    def test_duplicate_message_replays_without_new_version(self) -> None:
        self.seed_line()
        self.window("W1", "A", "2026-11-20", 60)
        payload = {"action": "确认", "line_id": "L1", "supplier": "A",
                   "quantity": 60, "promised_date": "2026-11-20",
                   "confirmed_by": "张三"}
        r1 = self.svc.handle_message("SAME", "a@e.com", payload)
        r2 = self.svc.handle_message("SAME", "a@e.com", dict(payload))
        self.assertEqual(r1["commitment_id"], r2["commitment_id"])
        self.assertEqual(r2["result_kind"], "幂等重放")
        versions = self.svc.get_versions("L1")["versions"]
        self.assertEqual(len(versions), 1)

    def test_same_message_id_different_body_conflicts(self) -> None:
        self.seed_line()
        self.window("W1", "A", "2026-11-20", 60)
        self.svc.handle_message("SAME", "a@e.com", {
            "action": "确认", "line_id": "L1", "supplier": "A",
            "quantity": 10, "promised_date": "2026-11-20"})
        with self.assertRaises(ConflictError):
            self.svc.handle_message("SAME", "a@e.com", {
                "action": "确认", "line_id": "L1", "supplier": "A",
                "quantity": 20, "promised_date": "2026-11-20"})


class ImmutabilityTest(ServiceTestBase):
    def test_snapshots_cannot_be_updated_or_deleted(self) -> None:
        self.seed_line()
        self.window("W1", "A", "2026-11-20", 10)
        self.msg("m1", "A", "确认", 10, "2026-11-20")
        conn = self.db._conn
        # 触发器 RAISE(ABORT) 在 Python 绑定中表现为 IntegrityError
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE commitment_snapshots SET quantity=9 WHERE seq=1")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM commitment_snapshots WHERE seq=1")

    def test_chain_verifies_and_state_reproduces(self) -> None:
        self.seed_line()
        self.window("W1", "A", "2026-11-20", 100)
        self.window("W2", "A", "2026-12-10", 100)
        self.window("W3", "C", "2026-12-05", 100)
        self.msg("m1", "A", "确认", 60, "2026-11-20")
        self.msg("m2", "A", "拒绝", 10)
        self.msg("m3", "A", "改期", 20, "2026-12-10")
        self.msg("m4", "A", "转单", 20, "2026-12-05", target="C")
        report = self.svc.verify_line("L1")
        self.assertTrue(report["intact"])
        self.assertTrue(report["state_reproducible"])
        self.assertEqual(report["versions"], 4)

    def test_tampering_breaks_chain(self) -> None:
        # 触发器阻止常规改写；这里验证校验逻辑本身能发现被篡改的哈希
        from commitment.storage import GENESIS, compute_chain_hash
        self.seed_line()
        self.window("W1", "A", "2026-11-20", 10)
        self.msg("m1", "A", "确认", 10, "2026-11-20")
        bogus = compute_chain_hash(GENESIS, {"x": 1})
        self.assertNotEqual(
            bogus, self.db.all_snapshots("L1")[0]["chain_hash"])


class ConcurrencyTest(ServiceTestBase):
    def test_parallel_confirms_never_overshoot(self) -> None:
        self.seed_line(qty=100)
        self.window("W1", "S", "2026-11-20", 1000)
        accepted: list[int] = []
        errors: list[str] = []
        lock = threading.Lock()

        def worker(i: int) -> None:
            try:
                r = self.msg(f"m{i}", "S", "确认", 20, "2026-11-20")
                with lock:
                    accepted.append(r["accepted_quantity"])
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(repr(exc))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(sum(accepted), 100)
        view = self.svc.get_line_view("L1")
        self.assertEqual(view["committed"], 100)
        self.assertEqual(view["gap"], 0)
        self.assertTrue(self.svc.verify_line("L1")["intact"])

    def test_parallel_duplicate_single_message_produces_one_version(self) -> None:
        self.seed_line(qty=100)
        self.window("W1", "S", "2026-11-20", 1000)
        cids: list[str | None] = []
        lock = threading.Lock()

        def worker() -> None:
            r = self.svc.handle_message("DUP", "s@e.com", {
                "action": "确认", "line_id": "L1", "supplier": "S",
                "quantity": 15, "promised_date": "2026-11-20"})
            with lock:
                cids.append(r.get("commitment_id"))

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len({c for c in cids if c}), 1)
        self.assertEqual(len(self.svc.get_versions("L1")["versions"]), 1)


class ScenarioTest(ServiceTestBase):
    """端到端场景：复现题述'多供应商分批确认 + 覆盖旧邮件 + 短缺归责'。"""

    def test_production_shortage_accountability_scenario(self) -> None:
        self.seed_line(qty=1000, due="2026-11-30")
        self.window("wa1", "甲厂", "2026-11-20", 600)
        self.window("wa2", "甲厂", "2026-12-15", 600)
        self.window("wb1", "乙厂", "2026-11-25", 600)

        # 第一批：两厂共确认 900
        self.msg("mail-1", "甲厂", "确认", 600, "2026-11-20", by="甲-王")
        self.msg("mail-2", "乙厂", "确认", 300, "2026-11-25", by="乙-李")
        view = self.svc.get_line_view("L1")
        self.assertEqual(view["gap"], 100)
        self.assertEqual(view["gap_responsibility"][0]["type"], "待供应商确认")

        # 甲厂新邮件：200 件改期到 12 月（旧承诺不丢，形成新版本指向旧版本）
        self.msg("mail-3", "甲厂", "改期", 200, "2026-12-15", by="甲-王",
                 reason="电泳线故障")
        # 乙厂补确认 100，同时又拒绝 50
        self.msg("mail-4", "乙厂", "确认", 100, "2026-11-25", by="乙-李")
        self.msg("mail-5", "乙厂", "拒绝", 50, by="乙-李", reason="质检不合格")

        view = self.svc.get_line_view("L1")
        # 总量缺口恰好是乙厂拒绝的 50
        self.assertEqual(view["gap"], 50)
        self.assertEqual(view["gap_responsibility"], [{
            "type": "供应商欠交", "supplier": "乙厂",
            "confirmed_by": "乙-李", "message_id": "mail-5",
            "reason": "质检不合格", "quantity": 50}])
        # 200 件按期缺口归因于甲厂改期
        on_time = view["schedule_responsibility"]
        self.assertEqual([r["quantity"] for r in on_time], [200])
        self.assertEqual(on_time[0]["message_id"], "mail-3")

        # 版本链完整、引用关系可追：改期版本引用确认版本
        versions = self.svc.get_versions("L1")["versions"]
        self.assertEqual([v["seq"] for v in versions], [1, 2, 3, 4, 5])
        self.assertTrue(versions[2]["references"])  # 改期引用了 V1
        self.assertTrue(self.svc.verify_line("L1")["intact"])


if __name__ == "__main__":
    unittest.main()
