"""HTTP 接口层（标准库实现，无第三方依赖）。

路由：

- ``POST /api/procurement-lines``             登记采购行
- ``POST /api/procurement-lines/{id}/release`` 下达采购行
- ``POST /api/procurement-lines/{id}/close``   关闭采购行
- ``POST /api/capacity-windows``              登记供应商产能窗口
- ``GET  /api/capacity-windows``              产能窗口及占用
- ``POST /api/messages``                      供应商回复（幂等入口）
- ``GET  /api/procurement-lines``             全部采购行覆盖视图
- ``GET  /api/procurement-lines/{id}``        覆盖/缺口/责任视图
- ``GET  /api/procurement-lines/{id}/versions`` 承诺版本链
- ``GET  /api/procurement-lines/{id}/verify``    哈希链与状态可重现性校验
- ``GET  /healthz``

消息的 ``message_id`` / ``sender`` 可放在 JSON 体内，也可用请求头
``X-Message-Id`` / ``X-Sender``（头优先）。
"""
from __future__ import annotations

import json
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlsplit

from .service import (
    CommitmentService,
    ConflictError,
    NotFoundError,
    ServiceError,
    ValidationError,
)


def _json_default(obj: Any) -> Any:
    from datetime import date as _date

    if isinstance(obj, _date):
        return obj.isoformat()
    raise TypeError(f"不可序列化的类型：{type(obj)!r}")


class CommitmentHTTPHandler(BaseHTTPRequestHandler):
    server_version = "CommitmentService/1.0"

    # 由 server 注入
    service: CommitmentService

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静日志
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    # ---- 基础收发 ----------------------------------------------------------
    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(value, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        return value

    def _handle_errors(self, fn: Callable[[], Any]) -> None:
        try:
            fn()
        except ValidationError as exc:
            self._send(HTTPStatus.BAD_REQUEST, {"error": "校验失败", "detail": str(exc)})
        except NotFoundError as exc:
            self._send(HTTPStatus.NOT_FOUND, {"error": "不存在", "detail": str(exc)})
        except ConflictError as exc:
            self._send(HTTPStatus.CONFLICT, {"error": "冲突", "detail": str(exc)})
        except ServiceError as exc:
            self._send(exc.status, {"error": "业务错误", "detail": str(exc)})

    # ---- 路由 --------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path

        def route() -> None:
            if path == "/healthz":
                self._send(200, {"status": "ok"})
            elif path == "/api/procurement-lines":
                self._send(200, {"lines": self.service.list_line_views()})
            elif path == "/api/capacity-windows":
                self._send(200, {"windows": self.service.list_capacity_windows()})
            elif m := re.fullmatch(r"/api/procurement-lines/([^/]+)/verify", path):
                self._send(200, self.service.verify_line(m.group(1)))
            elif m := re.fullmatch(r"/api/procurement-lines/([^/]+)/versions", path):
                self._send(200, self.service.get_versions(m.group(1)))
            elif m := re.fullmatch(r"/api/procurement-lines/([^/]+)", path):
                self._send(200, self.service.get_line_view(m.group(1)))
            else:
                self._send(404, {"error": "未知路由", "path": path})

        self._handle_errors(route)

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path

        def route() -> None:
            body = self._read_json()
            if path == "/api/procurement-lines":
                result = self.service.register_line(
                    line_id=body.get("line_id"),
                    material=body.get("material"),
                    quantity=body.get("quantity"),
                    due_date=body.get("due_date"),
                    status=body.get("status", "待确认"),
                )
                self._send(201, result)
            elif m := re.fullmatch(r"/api/procurement-lines/([^/]+)/release", path):
                self._send(200, self.service.release_line(m.group(1)))
            elif m := re.fullmatch(r"/api/procurement-lines/([^/]+)/close", path):
                self._send(200, self.service.close_line(m.group(1)))
            elif path == "/api/capacity-windows":
                result = self.service.register_capacity_window(
                    window_id=body.get("window_id"),
                    supplier=body.get("supplier"),
                    material=body.get("material"),
                    window_date=body.get("window_date"),
                    capacity=body.get("capacity"),
                )
                self._send(201, result)
            elif path == "/api/messages":
                message_id = self.headers.get("X-Message-Id") or body.get("message_id")
                sender = self.headers.get("X-Sender") or body.get("sender")
                payload = body.get("payload")
                if not isinstance(payload, dict):
                    payload = {k: v for k, v in body.items()
                               if k not in ("message_id", "sender")}
                result = self.service.handle_message(message_id, sender, payload)
                # 幂等重放返回 200；新处理返回 201；被业务拒绝仍是 200
                status = 200 if result.get("result_kind") in ("幂等重放",) else (
                    201 if result.get("commitment_id") else 200)
                self._send(status, result)
            else:
                self._send(404, {"error": "未知路由", "path": path})

        self._handle_errors(route)


def create_server(host: str, port: int, service: CommitmentService,
                  verbose: bool = False) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (CommitmentHTTPHandler,),
                   {"service": service})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.verbose = verbose  # type: ignore[attr-defined]
    httpd.daemon_threads = True
    return httpd


def serve_forever_in_thread(httpd: ThreadingHTTPServer) -> threading.Thread:
    t = threading.Thread(target=httpd.serve_forever, name="http-server", daemon=True)
    t.start()
    return t
