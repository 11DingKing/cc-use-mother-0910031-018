"""``python -m commitment`` 启动 HTTP 服务。

环境变量：
- ``COMMITMENT_DB``：SQLite 文件路径（默认 data/commitment.sqlite3）
- ``COMMITMENT_HOST``：监听地址（默认 0.0.0.0）
- ``COMMITMENT_PORT``：端口（默认 8080）
"""
from __future__ import annotations

import os

from .app import build_service
from .httpapi import create_server


def main() -> None:
    host = os.environ.get("COMMITMENT_HOST", "0.0.0.0")
    port = int(os.environ.get("COMMITMENT_PORT", "8080"))
    service = build_service()
    httpd = create_server(host, port, service, verbose=True)
    print(f"供应商交付承诺服务已启动：http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
