"""HTTP 接口：基于标准库 http.server 的 JSON API，零三方依赖。

路由：
  POST /purchase-lines                 登记采购行（含分批需求）
  POST /purchase-lines/{id}/issue      下达采购计划
  POST /purchase-lines/{id}/close      关闭采购行
  POST /capacity-windows               登记供应商产能窗口
  POST /commitments                    供应商确认/部分拒绝/超量确认（mode=allow_over）
  POST /commitments/reschedule         改期
  POST /commitments/transfer           转单（原子生成转出/转入版本）
  GET  /commitments/message/{message_id}  按邮件消息号查询（幂等核对）
  GET  /purchase-lines/{id}            覆盖、缺口、责任视图
  GET  /purchase-lines/{id}/versions   承诺版本链（含不可变快照）
  GET  /versions/{version_id}          单个版本快照
  GET  /health
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .errors import ServiceError
from .service import CommitmentService


def _version_brief(v, idempotent_replay: bool | None = None) -> dict:
    data = v.to_dict()
    if idempotent_replay is not None:
        data["idempotent_replay"] = idempotent_replay
    return data


def create_handler(service: CommitmentService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "CommitmentService/1.0"

        def log_message(self, fmt: str, *args) -> None:  # 静默，测试环境不打日志
            return

        def _send(self, status: int, payload: dict | list) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise ServiceError("INVALID_JSON", "请求体不是合法 JSON")
            if not isinstance(payload, dict):
                raise ServiceError("INVALID_JSON", "请求体必须是 JSON 对象")
            return payload

        def _error(self, exc: ServiceError) -> None:
            payload: dict = {"error": exc.code, "message": exc.message}
            details = getattr(exc, "details", None)
            if details:
                payload["details"] = details
            self._send(exc.status, payload)

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/") or "/"
            try:
                if path == "/health":
                    self._send(200, {"status": "ok"})
                    return
                parts = [p for p in path.split("/") if p]
                if len(parts) == 3 and parts[0] == "purchase-lines" and parts[2] == "versions":
                    versions = service.list_versions(parts[1])
                    self._send(200, {"versions": [v.to_dict() for v in versions]})
                    return
                if len(parts) == 3 and parts[:2] == ["commitments", "message"]:
                    query = parse_qs(urlparse(self.path).query)
                    line_id = query.get("line_id", [None])[0]
                    self._send(200, {"version": service.lookup_message(parts[2], line_id).to_dict()})
                    return
                if len(parts) == 2 and parts[0] == "versions":
                    self._send(200, {"version": service.get_version(parts[1]).to_dict()})
                    return
                if len(parts) == 2 and parts[0] == "purchase-lines":
                    self._send(200, service.get_line_view(parts[1]))
                    return
                self._send(404, {"error": "NOT_FOUND", "message": f"未知路径：{path}"})
            except ServiceError as exc:
                self._error(exc)

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/") or "/"
            try:
                payload = self._read_json()
                parts = [p for p in path.split("/") if p]
                if parts == ["purchase-lines"]:
                    line = service.create_purchase_line(payload)
                    self._send(201, {"line": line.to_dict()})
                elif len(parts) == 3 and parts[0] == "purchase-lines" and parts[2] == "issue":
                    line = service.issue_line(parts[1])
                    self._send(200, {"line": line.to_dict()})
                elif len(parts) == 3 and parts[0] == "purchase-lines" and parts[2] == "close":
                    line = service.close_line(parts[1])
                    self._send(200, {"line": line.to_dict()})
                elif parts == ["capacity-windows"]:
                    window = service.add_capacity_window(payload)
                    self._send(201, {"window": window.to_dict()})
                elif parts == ["commitments"]:
                    v, replayed = service.submit_commitment(payload)
                    self._send(200 if replayed else 201, {"version": _version_brief(v, replayed)})
                elif parts == ["commitments", "reschedule"]:
                    v, replayed = service.reschedule_commitment(payload)
                    self._send(200 if replayed else 201, {"version": _version_brief(v, replayed)})
                elif parts == ["commitments", "transfer"]:
                    out, inn, replayed = service.transfer_commitment(payload)
                    self._send(
                        200 if replayed else 201,
                        {"transfer_out": _version_brief(out, replayed),
                         "transfer_in": _version_brief(inn, replayed)},
                    )
                else:
                    self._send(404, {"error": "NOT_FOUND", "message": f"未知路径：{path}"})
            except ServiceError as exc:
                self._error(exc)

    return Handler


def build_server(host: str = "127.0.0.1", port: int = 8080, service: CommitmentService | None = None) -> ThreadingHTTPServer:
    service = service or CommitmentService()
    server = ThreadingHTTPServer((host, port), create_handler(service))
    server.service = service  # type: ignore[attr-defined]
    return server


def serve(host: str = "0.0.0.0", port: int = 8080) -> None:  # pragma: no cover
    server = build_server(host, port)
    print(f"供应商交付承诺服务监听 http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":  # pragma: no cover
    import os

    serve(port=int(os.environ.get("PORT", "8080")))
