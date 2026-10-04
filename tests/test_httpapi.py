"""HTTP 接口端到端测试。"""
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

from commitment.httpapi import create_server  # noqa: E402
from commitment.service import CommitmentService  # noqa: E402
from commitment.storage import Storage  # noqa: E402


class HttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = CommitmentService(Storage(":memory:"))
        self.httpd = create_server("127.0.0.1", 0, self.service)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def request(self, method: str, path: str, body: dict | None = None,
                headers: dict | None = None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json",
                                              **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_flow_over_http(self) -> None:
        # 登记 + 下达
        s, r = self.request("POST", "/api/procurement-lines", {
            "line_id": "L1", "material": "MAT-X", "quantity": 100,
            "due_date": "2026-11-30"})
        self.assertEqual(s, 201)
        s, _ = self.request("POST", "/api/procurement-lines/L1/release")
        self.assertEqual(s, 200)

        s, _ = self.request("POST", "/api/capacity-windows", {
            "window_id": "W1", "supplier": "A", "material": "MAT-X",
            "window_date": "2026-11-20", "capacity": 60})
        self.assertEqual(s, 201)
        s, _ = self.request("POST", "/api/capacity-windows", {
            "window_id": "W2", "supplier": "B", "material": "MAT-X",
            "window_date": "2026-11-25", "capacity": 60})
        self.assertEqual(s, 201)

        # 两批确认（message 体内带 message_id/sender）
        s, r = self.request("POST", "/api/messages", {
            "message_id": "mail-1", "sender": "a@e.com",
            "action": "确认", "line_id": "L1", "supplier": "A",
            "quantity": 60, "promised_date": "2026-11-20",
            "confirmed_by": "张三"})
        self.assertEqual(s, 201)
        self.assertEqual(r["accepted_quantity"], 60)

        # 用 X-Message-Id 头 + payload 嵌套
        s, r = self.request("POST", "/api/messages", {
            "payload": {"action": "确认", "line_id": "L1", "supplier": "B",
                        "quantity": 60, "promised_date": "2026-11-25",
                        "confirmed_by": "李四"}},
            headers={"X-Message-Id": "mail-2", "X-Sender": "b@e.com"})
        self.assertEqual(r["accepted_quantity"], 40)
        self.assertEqual(r["result_kind"], "部分接受")

        # 重复邮件：HTTP 200 + 幂等重放，且不新增版本
        s, r = self.request("POST", "/api/messages", {
            "message_id": "mail-1", "sender": "a@e.com",
            "action": "确认", "line_id": "L1", "supplier": "A",
            "quantity": 60, "promised_date": "2026-11-20",
            "confirmed_by": "张三"})
        self.assertEqual(s, 200)
        self.assertEqual(r["result_kind"], "幂等重放")

        # 部分拒绝形成缺口归责
        s, r = self.request("POST", "/api/messages", {
            "message_id": "mail-3", "sender": "a@e.com",
            "action": "拒绝", "line_id": "L1", "supplier": "A",
            "quantity": 15, "confirmed_by": "张三", "reason": "缺料"})
        self.assertEqual(r["cancelled_quantity"], 15)

        s, view = self.request("GET", "/api/procurement-lines/L1")
        self.assertEqual(s, 200)
        self.assertEqual(view["gap"], 15)
        self.assertEqual(view["gap_responsibility"][0]["message_id"], "mail-3")
        self.assertEqual(view["gap_responsibility"][0]["confirmed_by"], "张三")

        s, versions = self.request("GET", "/api/procurement-lines/L1/versions")
        # mail-1 重放不产生新版本：只有 3 个版本
        self.assertEqual(len(versions["versions"]), 3)

        s, verify = self.request("GET", "/api/procurement-lines/L1/verify")
        self.assertTrue(verify["intact"])
        self.assertTrue(verify["state_reproducible"])

    def test_errors_map_to_http_status(self) -> None:
        s, body = self.request("POST", "/api/procurement-lines", {
            "line_id": "L1", "material": "M", "quantity": -1,
            "due_date": "2026-11-30"})
        self.assertEqual(s, 400)
        self.assertIn("校验失败", body["error"])

        s, _ = self.request("GET", "/api/procurement-lines/NOPE")
        self.assertEqual(s, 404)

        self.request("POST", "/api/procurement-lines", {
            "line_id": "L9", "material": "M", "quantity": 1,
            "due_date": "2026-11-30"})
        s, _ = self.request("POST", "/api/messages", {
            "message_id": "x", "sender": "a", "action": "确认",
            "line_id": "L9", "supplier": "A", "quantity": 1,
            "promised_date": "2026-11-30"})
        self.assertEqual(s, 409)  # 未下达

    def test_healthz(self) -> None:
        s, body = self.request("GET", "/healthz")
        self.assertEqual(s, 200)
        self.assertEqual(body["status"], "ok")


if __name__ == "__main__":
    unittest.main()
