"""承诺服务核心规则回归测试。"""
from __future__ import annotations

import hashlib
import json
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from commitment_service import CommitmentService
from commitment_service.errors import ServiceError

LINE = {
    "line_id": "PO-1",
    "material_code": "MAT-A",
    "unit": "件",
    "batches": [
        {"batch_id": "W40", "need_date": "2026-10-05", "qty": 100},
        {"batch_id": "W41", "need_date": "2026-10-12", "qty": 80},
    ],
}


def make_service() -> CommitmentService:
    svc = CommitmentService()
    svc.create_purchase_line(LINE)
    svc.add_capacity_window(
        {"window_id": "WIN-S1-A", "supplier_id": "S1", "material_code": "MAT-A",
         "date_from": "2026-10-01", "date_to": "2026-10-31", "capacity_qty": 1000}
    )
    svc.add_capacity_window(
        {"window_id": "WIN-S2-A", "supplier_id": "S2", "material_code": "MAT-A",
         "date_from": "2026-10-01", "date_to": "2026-10-31", "capacity_qty": 1000}
    )
    svc.issue_line("PO-1")
    return svc


def canonical(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class RegistrationTest(unittest.TestCase):
    def test_line_and_capacity(self) -> None:
        svc = CommitmentService()
        line = svc.create_purchase_line(LINE)
        self.assertEqual(line.total_qty, 180)
        self.assertEqual(line.status, "草拟")
        with self.assertRaises(ServiceError) as cm:
            svc.submit_commitment({"line_id": "PO-1", "supplier_id": "S1",
                                   "message_id": "M0", "confirmer": "张三",
                                   "items": [{"batch_id": "W40", "promised_date": "2026-10-04", "qty": 10}]})
        self.assertEqual(cm.exception.code, "LINE_NOT_ISSUED")
        with self.assertRaises(ServiceError):
            svc.add_capacity_window(
                {"window_id": "W", "supplier_id": "S", "material_code": "M",
                 "date_from": "2026-10-10", "date_to": "2026-10-01", "capacity_qty": 5})

    def test_supplier_must_have_capacity_window(self) -> None:
        svc = make_service()
        with self.assertRaises(ServiceError) as cm:
            svc.submit_commitment({"line_id": "PO-1", "supplier_id": "SX",
                                   "message_id": "M0", "confirmer": "李四",
                                   "items": [{"batch_id": "W40", "promised_date": "2026-10-04", "qty": 5}]})
        self.assertEqual(cm.exception.code, "SUPPLIER_NOT_REGISTERED")


class CommitmentTest(unittest.TestCase):
    def test_multi_supplier_coverage_and_gap(self) -> None:
        svc = make_service()
        v1, replay = svc.submit_commitment({
            "line_id": "PO-1", "supplier_id": "S1", "message_id": "M1", "confirmer": "张三",
            "items": [{"batch_id": "W40", "promised_date": "2026-10-04", "qty": 60}],
        })
        self.assertFalse(replay)
        svc.submit_commitment({
            "line_id": "PO-1", "supplier_id": "S2", "message_id": "M2", "confirmer": "王五",
            "items": [{"batch_id": "W40", "promised_date": "2026-10-05", "qty": 30}],
        })
        view = svc.get_line_view("PO-1")
        w40, w41 = view["batches"]
        self.assertEqual(w40["committed_qty"], 90)
        self.assertEqual(w40["gap_qty"], 10)
        self.assertEqual(w40["coverage_rate"], 0.9)
        self.assertEqual(w41["gap_qty"], 80)
        self.assertEqual(w41["responsibility"]["attribution"][0]["cause"], "no_response")

    def test_cumulative_over_commit_strict_rejected(self) -> None:
        svc = make_service()
        svc.submit_commitment({"line_id": "PO-1", "supplier_id": "S1", "message_id": "M1",
                               "confirmer": "张三",
                               "items": [{"batch_id": "W40", "promised_date": "2026-10-04", "qty": 60}]})
        svc.submit_commitment({"line_id": "PO-1", "supplier_id": "S2", "message_id": "M2",
                               "confirmer": "王五",
                               "items": [{"batch_id": "W40", "promised_date": "2026-10-05", "qty": 40}]})
        # S1 已占 60，S2 后续邮件把承诺提到 41，累计 101 > 100：拒绝并给出可接受上限
        with self.assertRaises(ServiceError) as cm:
            svc.submit_commitment({"line_id": "PO-1", "supplier_id": "S2", "message_id": "M3",
                                   "confirmer": "王五",
                                   "items": [{"batch_id": "W40", "promised_date": "2026-10-05", "qty": 41}]})
        self.assertEqual(cm.exception.code, "OVER_COMMITTED")
        self.assertEqual(cm.exception.details["over"][0]["max_acceptable"], 40)
        # 超量消息未落账
        self.assertEqual(len(svc.list_versions("PO-1")), 2)

    def test_over_commit_allow_over(self) -> None:
        svc = make_service()
        svc.submit_commitment({"line_id": "PO-1", "supplier_id": "S1", "message_id": "M1",
                               "confirmer": "张三",
                               "items": [{"batch_id": "W40", "promised_date": "2026-10-04", "qty": 60}]})
        v, _ = svc.submit_commitment({"line_id": "PO-1", "supplier_id": "S2", "message_id": "M2",
                                      "confirmer": "王五", "mode": "allow_over",
                                      "items": [{"batch_id": "W40", "promised_date": "2026-10-05", "qty": 55}]})
        self.assertIn("超量", v.remark)
        view = svc.get_line_view("PO-1")
        self.assertEqual(view["batches"][0]["over_qty"], 15)
        self.assertEqual(view["batches"][0]["covered_qty"], 100)

    def test_supplier_new_version_replaces_old_and_immutable_snapshot(self) -> None:
        svc = make_service()
        v1, _ = svc.submit_commitment({"line_id": "PO-1", "supplier_id": "S1", "message_id": "M1",
                                       "confirmer": "张三",
                                       "items": [{"batch_id": "W40", "promised_date": "2026-10-04", "qty": 100}]})
        before = json.dumps(v1.to_dict()["snapshot"], sort_keys=True)
        v2, _ = svc.submit_commitment({"line_id": "PO-1", "supplier_id": "S1", "message_id": "M2",
                                       "confirmer": "张三",
                                       "items": [{"batch_id": "W40", "promised_date": "2026-10-04", "qty": 70},
                                                 {"batch_id": "W41", "promised_date": "2026-10-10", "qty": 10}]})
        self.assertEqual(v1.status, "superseded")
        self.assertEqual(v2.superseded_version_ids, [v1.version_id])
        self.assertEqual(v2.item_changes[0].change_type, "decreased")
        # 旧版本快照未被改写
        self.assertEqual(before, json.dumps(svc.get_version(v1.version_id).to_dict()["snapshot"], sort_keys=True))
        # 哈希可用返回的快照原文复算
        for v in (v1, v2):
            self.assertEqual(
                hashlib.sha256(canonical(v.snapshot).encode()).hexdigest(), v.snapshot_hash
            )
        view = svc.get_line_view("PO-1")
        self.assertEqual(view["summary"]["committed_qty"], 80)

    def test_partial_rejection_attributes_gap(self) -> None:
        svc = make_service()
        svc.submit_commitment({
            "line_id": "PO-1", "supplier_id": "S1", "message_id": "M1", "confirmer": "张三",
            "items": [{"batch_id": "W40", "promised_date": "2026-10-04", "qty": 60}],
            "rejections": [{"batch_id": "W40", "qty": 40, "reason": "原料短缺"}],
        })
        view = svc.get_line_view("PO-1")
        resp = view["batches"][0]["responsibility"]
        self.assertEqual(view["batches"][0]["gap_qty"], 40)
        self.assertEqual(resp["attribution"][0]["cause"], "partial_rejection")
        self.assertEqual(resp["attribution"][0]["supplier_id"], "S1")
        self.assertEqual(resp["attribution"][0]["confirmer"], "张三")
        self.assertEqual(resp["rejections"][0]["reason"], "原料短缺")
        with self.assertRaises(ServiceError):
            # 承诺+拒绝不能超过需求量
            svc.submit_commitment({
                "line_id": "PO-1", "supplier_id": "S2", "message_id": "M9", "confirmer": "王五",
                "items": [{"batch_id": "W40", "promised_date": "2026-10-04", "qty": 50}],
                "rejections": [{"batch_id": "W40", "qty": 60, "reason": "x"}],
            })

    def test_reschedule_creates_version_and_flags_late(self) -> None:
        svc = make_service()
        svc.submit_commitment({"line_id": "PO-1", "supplier_id": "S1", "message_id": "M1",
                               "confirmer": "张三",
                               "items": [{"batch_id": "W40", "promised_date": "2026-10-04", "qty": 100}]})
        v, _ = svc.reschedule_commitment({
            "line_id": "PO-1", "supplier_id": "S1", "message_id": "M2", "confirmer": "张三",
            "items": [{"batch_id": "W40", "promised_date": "2026-10-09"}],
        })
        self.assertEqual(v.items[0].promised_date, date(2026, 10, 9))
        self.assertEqual(v.item_changes[0].change_type, "rescheduled")
        view = svc.get_line_view("PO-1")
        cov = view["batches"][0]["coverage"][0]
        self.assertTrue(cov["late"])
        self.assertEqual(view["batches"][0]["responsibility"]["late_commitments"][0]["version_id"], v.version_id)

    def test_transfer_is_atomic_and_traces_responsibility(self) -> None:
        svc = make_service()
        svc.submit_commitment({"line_id": "PO-1", "supplier_id": "S1", "message_id": "M1",
                               "confirmer": "张三",
                               "items": [{"batch_id": "W40", "promised_date": "2026-10-04", "qty": 100}]})
        out, inn, replay = svc.transfer_commitment({
            "line_id": "PO-1", "from_supplier_id": "S1", "to_supplier_id": "S2",
            "message_id": "T1", "confirmer": "计划员-赵", "reason": "S1产线检修",
            "items": [{"batch_id": "W40", "promised_date": "2026-10-06", "qty": 100}],
        })
        self.assertFalse(replay)
        self.assertEqual(out.kind, "transfer_out")
        self.assertEqual(inn.kind, "transfer_in")
        self.assertEqual(out.related_version_id, inn.version_id)
        self.assertEqual(inn.related_version_id, out.version_id)
        self.assertEqual(out.items, [])
        self.assertEqual(inn.items[0].qty, 100)
        view = svc.get_line_view("PO-1")
        self.assertEqual(view["batches"][0]["committed_qty"], 100)
        self.assertEqual(view["summary"]["active_supplier_count"], 1)
        # 转单重复消息幂等
        out2, inn2, replay2 = svc.transfer_commitment({
            "line_id": "PO-1", "from_supplier_id": "S1", "to_supplier_id": "S2",
            "message_id": "T1", "confirmer": "计划员-赵",
            "items": [{"batch_id": "W40", "promised_date": "2026-10-06", "qty": 100}],
        })
        self.assertTrue(replay2)
        self.assertEqual((out2.version_id, inn2.version_id), (out.version_id, inn.version_id))
        self.assertEqual(len(svc.list_versions("PO-1")), 3)

    def test_transfer_cannot_exceed_held(self) -> None:
        svc = make_service()
        svc.submit_commitment({"line_id": "PO-1", "supplier_id": "S1", "message_id": "M1",
                               "confirmer": "张三",
                               "items": [{"batch_id": "W40", "promised_date": "2026-10-04", "qty": 30}]})
        with self.assertRaises(ServiceError) as cm:
            svc.transfer_commitment({
                "line_id": "PO-1", "from_supplier_id": "S1", "to_supplier_id": "S2",
                "message_id": "T1", "confirmer": "赵",
                "items": [{"batch_id": "W40", "promised_date": "2026-10-06", "qty": 40}],
            })
        self.assertEqual(cm.exception.code, "TRANSFER_EXCEEDS_HELD")
        self.assertEqual(len(svc.list_versions("PO-1")), 1)

    def test_coverage_broken_attribution(self) -> None:
        svc = make_service()
        svc.submit_commitment({"line_id": "PO-1", "supplier_id": "S1", "message_id": "M1",
                               "confirmer": "张三",
                               "items": [{"batch_id": "W40", "promised_date": "2026-10-04", "qty": 100}]})
        # S1 后续邮件把承诺砍到 40 —— 缺口由 S1 造成
        svc.submit_commitment({"line_id": "PO-1", "supplier_id": "S1", "message_id": "M2",
                               "confirmer": "张三",
                               "items": [{"batch_id": "W40", "promised_date": "2026-10-04", "qty": 40}]})
        view = svc.get_line_view("PO-1")
        attr = view["batches"][0]["responsibility"]
        self.assertTrue(attr["ever_fully_covered"])
        self.assertEqual(attr["attribution"][0]["cause"], "coverage_broken")
        self.assertEqual(attr["attribution"][0]["supplier_id"], "S1")
        self.assertEqual(attr["attribution"][0]["version_id"], "PO-1-V002")
        # 版本链展示变更责任
        chain = view["version_chain"]
        self.assertEqual([c["status"] for c in chain], ["superseded", "active"])
        self.assertEqual(chain[1]["item_changes"][0]["change_type"], "decreased")

    def test_partial_transfer_keeps_residual(self) -> None:
        svc = make_service()
        svc.submit_commitment({"line_id": "PO-1", "supplier_id": "S1", "message_id": "M1",
                               "confirmer": "张三",
                               "items": [{"batch_id": "W40", "promised_date": "2026-10-04", "qty": 100}]})
        out, inn, _ = svc.transfer_commitment({
            "line_id": "PO-1", "from_supplier_id": "S1", "to_supplier_id": "S2",
            "message_id": "T1", "confirmer": "赵",
            "items": [{"batch_id": "W40", "promised_date": "2026-10-06", "qty": 30}],
        })
        self.assertEqual([(i.batch_id, i.qty) for i in out.items], [("W40", 70)])
        self.assertEqual([(i.batch_id, i.qty) for i in inn.items], [("W40", 30)])
        view = svc.get_line_view("PO-1")
        self.assertEqual(view["batches"][0]["committed_qty"], 100)
        self.assertEqual(view["batches"][0]["gap_qty"], 0)
        self.assertEqual(view["summary"]["active_supplier_count"], 2)

    def test_capacity_window_enforced(self) -> None:
        svc = make_service()
        with self.assertRaises(ServiceError) as cm:
            svc.submit_commitment({"line_id": "PO-1", "supplier_id": "S1", "message_id": "M1",
                                   "confirmer": "张三",
                                   "items": [{"batch_id": "W40", "promised_date": "2026-11-20", "qty": 10}]})
        self.assertEqual(cm.exception.code, "CAPACITY_VIOLATION")

    def test_duplicate_message_is_idempotent(self) -> None:
        svc = make_service()
        payload = {"line_id": "PO-1", "supplier_id": "S1", "message_id": "DUP1",
                   "confirmer": "张三",
                   "items": [{"batch_id": "W40", "promised_date": "2026-10-04", "qty": 10}]}
        v1, r1 = svc.submit_commitment(payload)
        v2, r2 = svc.submit_commitment(payload)
        self.assertFalse(r1)
        self.assertTrue(r2)
        self.assertEqual(v1.version_id, v2.version_id)
        self.assertEqual(len(svc.list_versions("PO-1")), 1)

    def test_concurrent_duplicate_messages_create_one_commitment(self) -> None:
        svc = make_service()
        barrier = threading.Barrier(8)
        payload = {"line_id": "PO-1", "supplier_id": "S1", "message_id": "CONC1",
                   "confirmer": "张三",
                   "items": [{"batch_id": "W40", "promised_date": "2026-10-04", "qty": 50}]}

        def call() -> tuple[str, bool]:
            barrier.wait()
            v, replay = svc.submit_commitment(payload)
            return v.version_id, replay

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: call(), range(8)))
        version_ids = {vid for vid, _ in results}
        self.assertEqual(len(version_ids), 1)
        self.assertEqual(sum(1 for _, replay in results if not replay), 1)
        self.assertEqual(len(svc.list_versions("PO-1")), 1)

    def test_concurrent_distinct_messages_have_deterministic_cumulative_result(self) -> None:
        # 两家供应商同时回邮各承诺 60，合计 120 > 100：恰好一封成功，一封 OVER_COMMITTED
        outcomes: list[str] = []
        for _ in range(20):
            svc = make_service()
            barrier = threading.Barrier(2)

            def call(supplier: str, msg: str) -> str:
                barrier.wait()
                try:
                    svc.submit_commitment({"line_id": "PO-1", "supplier_id": supplier,
                                           "message_id": msg, "confirmer": supplier,
                                           "items": [{"batch_id": "W40", "promised_date": "2026-10-04", "qty": 60}]})
                    return "ok"
                except ServiceError as exc:
                    return exc.code

            with ThreadPoolExecutor(max_workers=2) as pool:
                r = list(pool.map(lambda args: call(*args), [("S1", "C1"), ("S2", "C2")]))
            self.assertEqual(sorted(r), ["OVER_COMMITTED", "ok"])
            outcomes.extend(r)
        self.assertEqual(outcomes.count("ok"), 20)
        self.assertEqual(outcomes.count("OVER_COMMITTED"), 20)


if __name__ == "__main__":
    unittest.main()
