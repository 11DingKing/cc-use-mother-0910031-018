"""SQLite 存储层。

快照表 ``commitment_snapshots`` 配置了 UPDATE/DELETE 触发器，任何对已落盘
承诺版本的改写都会被数据库直接拒绝；版本之间通过 ``chain_hash`` 串联，
可调用 :meth:`Storage.verify_chain` 校验完整性。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS procurement_lines (
    line_id    TEXT PRIMARY KEY,
    material   TEXT NOT NULL,
    quantity   INTEGER NOT NULL CHECK (quantity > 0),
    due_date   TEXT NOT NULL,
    status     TEXT NOT NULL,
    version    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS capacity_windows (
    window_id   TEXT PRIMARY KEY,
    supplier    TEXT NOT NULL,
    material    TEXT NOT NULL,
    window_date TEXT NOT NULL,
    capacity    INTEGER NOT NULL CHECK (capacity >= 0),
    allocated   INTEGER NOT NULL DEFAULT 0 CHECK (allocated >= 0),
    version     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    UNIQUE (supplier, material, window_date)
);

CREATE TABLE IF NOT EXISTS commitment_snapshots (
    commitment_id   TEXT PRIMARY KEY,
    line_id         TEXT NOT NULL REFERENCES procurement_lines(line_id),
    supplier        TEXT NOT NULL,
    material        TEXT NOT NULL,
    promised_date   TEXT NOT NULL,
    quantity        INTEGER NOT NULL CHECK (quantity >= 0),
    action          TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    status          TEXT NOT NULL,
    confirmed_by    TEXT NOT NULL,
    message_id      TEXT NOT NULL,
    reason          TEXT,
    references_json TEXT NOT NULL DEFAULT '[]',
    transferred_to  TEXT,
    chain_hash      TEXT NOT NULL,
    state_json      TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cmt_line ON commitment_snapshots(line_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cmt_line_seq ON commitment_snapshots(line_id, seq);
CREATE INDEX IF NOT EXISTS idx_cmt_supplier ON commitment_snapshots(supplier, material, promised_date);

CREATE TRIGGER IF NOT EXISTS trg_cmt_no_update
BEFORE UPDATE ON commitment_snapshots
BEGIN
    SELECT RAISE(ABORT, '承诺快照不可变：禁止 UPDATE');
END;

CREATE TRIGGER IF NOT EXISTS trg_cmt_no_delete
BEFORE DELETE ON commitment_snapshots
BEGIN
    SELECT RAISE(ABORT, '承诺快照不可变：禁止 DELETE');
END;

CREATE TABLE IF NOT EXISTS message_ledger (
    message_id     TEXT PRIMARY KEY,
    sender         TEXT NOT NULL,
    body_hash      TEXT NOT NULL,
    result_kind    TEXT NOT NULL,
    response_json  TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
"""

GENESIS = "0" * 64


def utc_now() -> str:
    """UTC 时间戳，微秒精度，按 ISO 字符串排序即按时间排序。"""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def compute_chain_hash(prev_hash: str, payload: dict[str, Any]) -> str:
    """计算承诺版本链哈希。

    哈希载荷只纳入业务字段并以 sort_keys 序列化，保证同一业务事实在
    任何机器、任何 Python 进程中得到相同哈希。
    """
    material = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256()
    digest.update(prev_hash.encode("ascii"))
    digest.update(b"|")
    digest.update(material.encode("utf-8"))
    return digest.hexdigest()


class Storage:
    """同步 SQLite 存储；所有写操作走单把写锁，天然串行。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._path = str(path)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        if self._path != ":memory:":
            self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)
        # SQLite 单连接同时只能有一个事务；写事务本就全局串行，
        # 用锁把"开始事务→提交/回滚"这段临界区保护起来。
        self._tx_lock = threading.RLock()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """``BEGIN IMMEDIATE`` 立即获取写锁。

        SQLite 单库单写：并发消息在本锁 + 数据库写锁上严格串行；
        ``(line_id, seq)`` 唯一索引与消息台账主键再做一层兜底。
        """
        conn = self._conn
        with self._tx_lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    # ---- 采购行 ------------------------------------------------------------
    def insert_line(self, conn: sqlite3.Connection, line_id: str, material: str,
                    quantity: int, due_date: date, status: str) -> None:
        conn.execute(
            "INSERT INTO procurement_lines(line_id, material, quantity, due_date, status, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (line_id, material, quantity, due_date.isoformat(), status, utc_now()),
        )

    def get_line(self, line_id: str) -> sqlite3.Row | None:
        with self._tx_lock:
            return self._conn.execute(
                "SELECT * FROM procurement_lines WHERE line_id=?", (line_id,)
            ).fetchone()

    def get_line_lock(self, conn: sqlite3.Connection, line_id: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM procurement_lines WHERE line_id=?", (line_id,)
        ).fetchone()

    def update_line_status(self, conn: sqlite3.Connection, line_id: str, status: str) -> None:
        conn.execute(
            "UPDATE procurement_lines SET status=?, version=version+1 WHERE line_id=?",
            (status, line_id),
        )

    def list_lines(self) -> list[sqlite3.Row]:
        with self._tx_lock:
            return list(self._conn.execute(
                "SELECT * FROM procurement_lines ORDER BY line_id"))

    # ---- 产能窗口 ----------------------------------------------------------
    def insert_window(self, conn: sqlite3.Connection, window_id: str, supplier: str,
                      material: str, window_date: date, capacity: int) -> None:
        conn.execute(
            "INSERT INTO capacity_windows(window_id, supplier, material, window_date,"
            " capacity, allocated, created_at) VALUES (?,?,?,?,?,0,?)",
            (window_id, supplier, material, window_date.isoformat(), capacity, utc_now()),
        )

    def get_window_lock(self, conn: sqlite3.Connection, window_id: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM capacity_windows WHERE window_id=?", (window_id,)
        ).fetchone()

    def find_window_lock(self, conn: sqlite3.Connection, supplier: str, material: str,
                         window_date: date) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM capacity_windows WHERE supplier=? AND material=? AND window_date=?",
            (supplier, material, window_date.isoformat()),
        ).fetchone()

    def allocate_window(self, conn: sqlite3.Connection, window_id: str, delta: int) -> None:
        """正数占用、负数释放；CHECK 约束保证不会占用为负。"""
        conn.execute(
            "UPDATE capacity_windows SET allocated=allocated+?, version=version+1"
            " WHERE window_id=?",
            (delta, window_id),
        )

    # ---- 承诺快照 ----------------------------------------------------------
    def last_snapshot(self, conn: sqlite3.Connection, line_id: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM commitment_snapshots WHERE line_id=? ORDER BY seq DESC LIMIT 1",
            (line_id,),
        ).fetchone()

    def active_commitments(self, conn: sqlite3.Connection, line_id: str) -> list[sqlite3.Row]:
        return list(conn.execute(
            "SELECT * FROM commitment_snapshots WHERE line_id=? AND status=? ORDER BY seq",
            (line_id, "生效"),
        ))

    def all_snapshots(self, line_id: str) -> list[sqlite3.Row]:
        with self._tx_lock:
            return list(self._conn.execute(
                "SELECT * FROM commitment_snapshots WHERE line_id=? ORDER BY seq",
                (line_id,),
            ))

    def insert_snapshot(self, conn: sqlite3.Connection, *, commitment_id: str, line_id: str,
                        supplier: str, material: str, promised_date: date, quantity: int,
                        action: str, seq: int, status: str, confirmed_by: str,
                        message_id: str, reason: str | None, references: list[str],
                        transferred_to: str | None, chain_hash: str,
                        state: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO commitment_snapshots(commitment_id, line_id, supplier, material,"
            " promised_date, quantity, action, seq, status, confirmed_by, message_id,"
            " reason, references_json, transferred_to, chain_hash, state_json, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (commitment_id, line_id, supplier, material, promised_date.isoformat(), quantity,
             action, seq, status, confirmed_by, message_id, reason,
             json.dumps(sorted(references), ensure_ascii=False), transferred_to,
             chain_hash, json.dumps(state, ensure_ascii=False, sort_keys=True), utc_now()),
        )

    def verify_chain(self, line_id: str) -> dict[str, Any]:
        """重放哈希链，返回链头哈希、版本数与是否完整。"""
        rows = self.all_snapshots(line_id)
        prev = GENESIS
        for row in rows:
            payload = {
                "commitment_id": row["commitment_id"],
                "line_id": row["line_id"],
                "supplier": row["supplier"],
                "material": row["material"],
                "promised_date": row["promised_date"],
                "quantity": row["quantity"],
                "action": row["action"],
                "seq": row["seq"],
                "status": row["status"],
                "confirmed_by": row["confirmed_by"],
                "message_id": row["message_id"],
                "reason": row["reason"],
                "references": json.loads(row["references_json"]),
                "transferred_to": row["transferred_to"],
                "state": json.loads(row["state_json"]),
            }
            expected = compute_chain_hash(prev, payload)
            if expected != row["chain_hash"]:
                return {"line_id": line_id, "intact": False, "versions": len(rows),
                        "broken_seq": row["seq"]}
            prev = expected
        return {"line_id": line_id, "intact": True, "versions": len(rows),
                "head": prev}

    # ---- 消息台账 ----------------------------------------------------------
    def find_message(self, message_id: str) -> sqlite3.Row | None:
        with self._tx_lock:
            return self._conn.execute(
                "SELECT * FROM message_ledger WHERE message_id=?", (message_id,)
            ).fetchone()

    def find_message_lock(self, conn: sqlite3.Connection, message_id: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM message_ledger WHERE message_id=?", (message_id,)
        ).fetchone()

    def record_message(self, conn: sqlite3.Connection, message_id: str, sender: str,
                       body_hash: str, result_kind: str, response: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO message_ledger(message_id, sender, body_hash, result_kind,"
            " response_json, created_at) VALUES (?,?,?,?,?,?)",
            (message_id, sender, body_hash, result_kind,
             json.dumps(response, ensure_ascii=False, sort_keys=True), utc_now()),
        )

    def all_windows(self, supplier: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM capacity_windows"
        args: tuple[Any, ...] = ()
        if supplier:
            sql += " WHERE supplier=?"
            args = (supplier,)
        sql += " ORDER BY supplier, material, window_date"
        with self._tx_lock:
            return list(self._conn.execute(sql, args))
