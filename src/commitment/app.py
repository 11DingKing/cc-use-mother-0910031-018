"""应用装配：按环境变量/参数构造存储与服务。"""
from __future__ import annotations

import os
from pathlib import Path

from .service import CommitmentService
from .storage import Storage

DEFAULT_DB_PATH = "data/commitment.sqlite3"


def build_service(db_path: str | Path | None = None) -> CommitmentService:
    path = Path(db_path or os.environ.get("COMMITMENT_DB", DEFAULT_DB_PATH))
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    storage = Storage(path)
    return CommitmentService(storage)
