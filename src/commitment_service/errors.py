"""服务端错误码与异常。"""
from __future__ import annotations


class ServiceError(Exception):
    """业务错误，携带稳定错误码，HTTP 层映射为状态码。"""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


NOT_FOUND = lambda what: ServiceError("NOT_FOUND", f"{what}不存在", 404)
