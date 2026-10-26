"""SQLite connection management and schema.

Why SQLite: zero setup, a single file, transactional, and plenty fast for one service
instance (hundreds of small writes per second). Round 2 keeps the same interfaces and adds
SQS / CloudWatch Logs for multi-task durability.

- WAL mode lets readers (/stats, /dlq) run while a write is in progress.
- synchronous=FULL (default) fsyncs each commit, so "persisted" means "on disk" before we
  acknowledge a call. NORMAL is available for throughput experiments.
- sqlite3 is blocking, so queries run off the event loop on ONE dedicated thread that owns
  the connection. SQLite allows a single writer at a time anyway, so one thread is the
  honest model, and it avoids what a shared thread pool did under load: a dozen worker
  threads contending for a lock and the GIL, starving the event loop until pipeline
  attempts timed out and tripped the breaker (found in the first campaign-window run).
"""

import asyncio
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS call_log (
    event_id         TEXT PRIMARY KEY,
    campaign_id      TEXT NOT NULL,
    borrower_bucket  TEXT NOT NULL,
    product_type     TEXT,
    event_timestamp  TEXT NOT NULL,
    received_at      TEXT NOT NULL,
    finished_at      TEXT NOT NULL,
    outcome          TEXT NOT NULL,
    retry_count      INTEGER NOT NULL,
    call_duration_s  INTEGER NOT NULL,
    latency_ms       INTEGER NOT NULL,
    status           TEXT NOT NULL,
    dlq_reason       TEXT
);
CREATE INDEX IF NOT EXISTS idx_call_log_outcome ON call_log (outcome);

CREATE TABLE IF NOT EXISTS dead_letters (
    event_id         TEXT PRIMARY KEY,
    payload_json     TEXT NOT NULL,
    reason           TEXT NOT NULL,
    attempts         INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT,
    first_failed_at  TEXT NOT NULL,
    last_failed_at   TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',
    replay_count     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_dead_letters_status ON dead_letters (status);
"""


class Database:
    def __init__(self, path: str, synchronous: str = "FULL") -> None:
        self.path = path
        self._synchronous = synchronous
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sqlite")
        self._conn: sqlite3.Connection | None = None

    def open(self) -> None:
        """Open the connection on the DB thread (blocking; call once at startup)."""
        self._executor.submit(self._open).result()

    def _open(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA synchronous={self._synchronous}")
        conn.execute("PRAGMA busy_timeout=5000")
        self._migrate(conn)
        self._conn = conn

    def _migrate(self, conn: sqlite3.Connection) -> None:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < SCHEMA_VERSION:
            with conn:
                conn.executescript(SCHEMA)
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def close(self) -> None:
        self._executor.submit(self._close).result()
        self._executor.shutdown(wait=True)

    def _close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    async def run[T](self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Run `fn(conn)` on the DB thread; queries execute one at a time, in order."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._call, fn)

    def _call[T](self, fn: Callable[[sqlite3.Connection], T]) -> T:
        if self._conn is None:
            raise RuntimeError("database is not open")
        return fn(self._conn)

    async def is_writable(self) -> bool:
        """Readiness check: can we take the write lock right now?"""

        def probe(conn: sqlite3.Connection) -> bool:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
            return True

        try:
            return await self.run(probe)
        except (sqlite3.Error, RuntimeError):
            return False
