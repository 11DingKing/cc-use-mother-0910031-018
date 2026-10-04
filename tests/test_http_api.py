"""HTTP 接口端到端回归测试。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from commitment_service.http_api import build_server


class Client:
    def __init__(self, base: str) -> None:
        self.base = base

    def request(self, method: str, path: str, payload: dict | None = None):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = build_server("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.api = Client(f"http://127.0.0.1:{cls.port}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self) -> None:
        self.svc = self.server.service  # type: ignore[attr-defined]
        # 每个用例使用独立采购行，服务实例共享也互不影响
        self.line_id = f"PO-{self._testMethodName}"

    def _seed(self) -> None:
        status, _ = self.api.request("POST", "/purchase-lines", {
            "line_id": self.line_id, "material_code": "MAT-A",
            "batches": [
                {"batch_id": "W40", "need_date": "2026-10-05", "qty": 100},
                {"batch_id": "W41", "need_date": "2026-10-12", "qty": 80},
            ],
        })
        self.assertEqual(status, 201)
        for supplier in ("S1", "S2"):
            status, _ = self.api.request("POST", "/capacity-windows", {
                "window_id": f"WIN-{supplier}-{self.line_id}", "supplier_id": supplier,
                "material_code": "MAT-A", "date_from": "2026-10-01",
                "date_to": "2026-10-31", "capacity_qty": 1000,
            })
            self.assertEqual(status, 201)
        status, _ = self.api.request("POST", f"/purchase-lines/{self.line_id}/issue", {})
        self.assertEqual(status, 200)

    def test_full_flow(self) -> None:
        self._seed()
        status, body = self.api.request("POST", "/commitments", {
            "line_id": self.line_id, "supplier_id": "S1", "message_id": "M1", "confirmer": "张三",
            "items": [{"batch_id": "W40", "promised_date": "2026-10-04", "qty": 60}],
            "rejections": [{"batch_id": "W40", "qty": 40, "reason": "原料短缺"}],
        })
        self.assertEqual(status, 201)
        self.assertIn("snapshot_hash", body["version"])

        # 重复邮件：200 + idempotent_replay，不产生新版本
        status, body2 = self.api.request("POST", "/commitments", {
            "line_id": self.line_id, "supplier_id": "S1", "message_id": "M1", "confirmer": "张三",
            "items": [{"batch_id": "W40", "promised_date": "2026-10-04", "qty": 60}],
        })
        self.assertEqual(status, 200)
        self.assertTrue(body2["version"]["idempotent_replay"])
        self.assertEqual(body2["version"]["version_id"], body["version"]["version_id"])

        # 超量确认默认拒绝
        status, body3 = self.api.request("POST", "/commitments", {
            "line_id": self.line_id, "supplier_id": "S2", "message_id": "M2", "confirmer": "王五",
            "items": [{"batch_id": "W40", "promised_date": "2026-10-05", "qty": 50}],
        })
        self.assertEqual(status, 409)
        self.assertEqual(body3["error"], "OVER_COMMITTED")

        # 转单
        status, body4 = self.api.request("POST", "/commitments/transfer", {
            "line_id": self.line_id, "from_supplier_id": "S1", "to_supplier_id": "S2",
            "message_id": "T1", "confirmer": "计划员-赵", "reason": "S1检修",
            "items": [{"batch_id": "W40", "promised_date": "2026-10-08", "qty": 60}],
        })
        self.assertEqual(status, 201)
        self.assertEqual(body4["transfer_out"]["kind"], "transfer_out")
        self.assertEqual(body4["transfer_in"]["kind"], "transfer_in")

        status, view = self.api.request("GET", f"/purchase-lines/{self.line_id}", None)
        self.assertEqual(status, 200)
        w40 = view["batches"][0]
        self.assertEqual(w40["committed_qty"], 60)
        self.assertEqual(w40["gap_qty"], 40)
        self.assertTrue(w40["coverage"][0]["late"])
        causes = {a["cause"] for a in w40["responsibility"]["attribution"]}
        self.assertIn("partial_rejection", causes)
        self.assertEqual(view["summary"]["version_count"], 3)

        # 版本链与单版本快照
        status, chain = self.api.request("GET", f"/purchase-lines/{self.line_id}/versions", None)
        self.assertEqual(status, 200)
        self.assertEqual(len(chain["versions"]), 3)
        vid = chain["versions"][0]["version_id"]
        status, one = self.api.request("GET", f"/versions/{vid}", None)
        self.assertEqual(status, 200)
        self.assertEqual(one["version"]["snapshot"]["version_id"], vid)

        # 消息号反查
        status, lookup = self.api.request("GET", "/commitments/message/M1", None)
        self.assertEqual(status, 200)
        self.assertEqual(lookup["version"]["message_id"], "M1")

    def test_reschedule(self) -> None:
        self._seed()
        self.api.request("POST", "/commitments", {
            "line_id": self.line_id, "supplier_id": "S1", "message_id": "M1", "confirmer": "张三",
            "items": [{"batch_id": "W41", "promised_date": "2026-10-10", "qty": 80}],
        })
        status, body = self.api.request("POST", "/commitments/reschedule", {
            "line_id": self.line_id, "supplier_id": "S1", "message_id": "M2", "confirmer": "张三",
            "items": [{"batch_id": "W41", "promised_date": "2026-10-15"}],
        })
        self.assertEqual(status, 201)
        self.assertEqual(body["version"]["items"][0]["promised_date"], "2026-10-15")
        self.assertEqual(body["version"]["items"][0]["qty"], 80)

    def test_validation_errors(self) -> None:
        status, body = self.api.request("POST", "/commitments", {"line_id": "NOPE"})
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "NOT_FOUND")
        status, body = self.api.request("POST", "/purchase-lines", {"line_id": "X"})
        self.assertEqual(status, 400)
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/commitments",
                                     data=b"{bad", method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req)
            self.fail("应当返回 400")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
            self.assertEqual(json.loads(exc.read())["error"], "INVALID_JSON")


if __name__ == "__main__":
    unittest.main()
