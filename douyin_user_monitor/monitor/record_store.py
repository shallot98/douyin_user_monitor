"""Per-user `download_records` kept outside the JSON monitor state.

Why: records were ~30MB of a 48MB `monitor_users.json`; every `update_state`
re-read and re-wrote all of it on the event loop and froze the whole service.
Here each user is one SQLite row whose `records` column is the exact list the
JSON used to hold (newest first, capped by `merge_download_records`), so order
and shape are unchanged and a write only touches that user's row.

Contract:
- Reads are served from an in-process cache. Returned lists/dicts are shared —
  never mutate them; build new lists and `put()` them.
- Writes must run inside `MonitorStorage.update_state` mutators, which hold the
  state file lock and therefore serialize record writers across processes.
  Records are committed before the JSON state, so a crash in between leaves a
  record without its id in `downloaded_aweme_ids`; the next sync re-selects the
  work and `put()` overwrites the same row — nothing is lost or duplicated.
- Other processes (cloud playback publisher) are picked up via
  `PRAGMA data_version`, reloading only rows whose `rev` changed.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping

import orjson

RECORD_DB_FILENAME = "monitor_records.db"
BUSY_TIMEOUT_MS = 30000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_records (
    user_id TEXT PRIMARY KEY,
    rev INTEGER NOT NULL,
    records BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""

_EMPTY: List[Dict[str, Any]] = []


def record_db_path(state_file: Path) -> Path:
    return state_file.with_name(RECORD_DB_FILENAME)


def load_state_with_records(state_file: Path) -> Dict[str, Any]:
    """Read-only full view for offline scripts: raw JSON state, users carrying their records."""
    db_path = record_db_path(state_file)
    if not db_path.exists():
        raise FileNotFoundError(f"record store missing next to state file: {db_path}")
    state = orjson.loads(state_file.read_bytes())
    store = RecordStore(db_path)
    try:
        state["users"] = store.attach_all(state.get("users") or [])
    finally:
        store.close()
    return state


class RecordStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # API 线程池（to_thread）与事件循环都会读；连接加锁共享。
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._cache: Dict[str, List[Dict[str, Any]]] = {}
        self._revs: Dict[str, int] = {}
        self._data_version: int | None = None
        # 本进程可见的变更计数，给统计等派生缓存当失效键
        self.revision = 0
        self._refresh()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- reads -------------------------------------------------------------

    def get(self, user_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            self._refresh()
            return self._cache.get(str(user_id), _EMPTY)

    def snapshot(self) -> Dict[str, List[Dict[str, Any]]]:
        with self._lock:
            self._refresh()
            return dict(self._cache)

    def current_revision(self) -> int:
        with self._lock:
            self._refresh()
            return self.revision

    def attach(self, user: Mapping[str, Any]) -> Dict[str, Any]:
        """A shallow view of `user` carrying its records, for the pure record helpers."""
        return {**user, "download_records": self.get(str(user.get("id") or ""))}

    def attach_all(self, users: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        records = self.snapshot()
        return [
            {**user, "download_records": records.get(str(user.get("id") or ""), _EMPTY)}
            for user in users
        ]

    # ---- writes ------------------------------------------------------------

    def put(self, user_id: str, records: List[Dict[str, Any]]) -> None:
        self.put_many({str(user_id): records})

    def put_many(self, mapping: Mapping[str, List[Dict[str, Any]]]) -> None:
        if not mapping:
            return
        rows = [(str(user_id), orjson.dumps(list(records))) for user_id, records in mapping.items()]
        with self._lock:
            with self._transaction() as rev:
                self._conn.executemany(
                    "INSERT INTO user_records(user_id, rev, records) VALUES (?, ?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET rev=excluded.rev, records=excluded.records",
                    [(user_id, rev, blob) for user_id, blob in rows],
                )
            for user_id, records in mapping.items():
                self._cache[str(user_id)] = list(records)
                self._revs[str(user_id)] = rev
            self.revision += 1

    def delete(self, user_id: str) -> None:
        user_id = str(user_id)
        with self._lock:
            with self._transaction():
                self._conn.execute("DELETE FROM user_records WHERE user_id = ?", (user_id,))
            self._cache.pop(user_id, None)
            self._revs.pop(user_id, None)
            self.revision += 1

    # ---- internals ---------------------------------------------------------

    @contextmanager
    def _transaction(self) -> Iterator[int]:
        """BEGIN IMMEDIATE + bump the global rev; yields the rev for rows written."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute("SELECT value FROM meta WHERE key = 'rev'").fetchone()
            rev = (row[0] if row else 0) + 1
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES ('rev', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (rev,),
            )
            yield rev
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def _refresh(self) -> None:
        version = self._conn.execute("PRAGMA data_version").fetchone()[0]
        if version == self._data_version:
            return
        revs = dict(self._conn.execute("SELECT user_id, rev FROM user_records").fetchall())
        stale = [user_id for user_id, rev in revs.items() if self._revs.get(user_id) != rev]
        removed = [user_id for user_id in self._cache if user_id not in revs]
        for user_id in removed:
            self._cache.pop(user_id, None)
            self._revs.pop(user_id, None)
        for start in range(0, len(stale), 500):
            chunk = stale[start:start + 500]
            placeholders = ",".join("?" * len(chunk))
            for user_id, rev, blob in self._conn.execute(
                f"SELECT user_id, rev, records FROM user_records WHERE user_id IN ({placeholders})",
                chunk,
            ):
                self._cache[user_id] = orjson.loads(blob)
                self._revs[user_id] = rev
        if stale or removed:
            self.revision += 1
        self._data_version = version
