"""Test helpers for the split layout: JSON state + RecordStore (monitor_records.db)."""

from __future__ import annotations

import tempfile
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator
from unittest.mock import patch

from douyin_user_monitor.monitor.record_store import RecordStore
from douyin_user_monitor.monitor.storage import MonitorStorage


def split_records(state: Dict[str, Any]) -> Dict[str, list]:
    """Pop inline `download_records` from a legacy-shaped state; returns {user_id: records}."""
    records = {}
    for user in state.get("users") or []:
        if "download_records" in user:
            rows = user.pop("download_records")
            if rows:
                records[str(user["id"])] = rows
    return records


def seed_state(storage: MonitorStorage, state: Dict[str, Any]) -> Dict[str, Any]:
    """save_state for fixtures written in the legacy shape (users carrying download_records)."""
    state = deepcopy(state)
    storage.records.put_many(split_records(state))
    storage.save_state(state)
    return state


def write_state_file(path: Path, state: Dict[str, Any]) -> None:
    """Write a fixture state file (+ its record DB) the way the monitor would."""
    storage = MonitorStorage(path)
    try:
        seed_state(storage, state)
    finally:
        storage.records.close()


def load_user_with_records(storage: MonitorStorage, index: int = 0) -> Dict[str, Any]:
    return storage.records.attach(storage.load_state()["users"][index])


@contextmanager
def patch_monitor_users(users: Iterable[Dict[str, Any]], *, method: str = "list_users_async") -> Iterator[RecordStore]:
    """Serve legacy-shaped fixture users to the API the way the split layout does.

    The patched `monitor_service.<method>` returns record-less (JSON-shaped) views, and
    `api.monitor._records()` returns a real RecordStore holding the fixtures' records.
    The store is re-synced from the live dicts on every access, so tests that mutate a
    fixture user after patching (ids, records) still see the change, as before the split.
    """
    from douyin_user_monitor.api import monitor as monitor_api

    users = list(users)

    def views():
        return [{key: value for key, value in user.items() if key != "download_records"} for user in users]

    with tempfile.TemporaryDirectory() as tmp:
        store = RecordStore(Path(tmp) / "monitor_records.db")

        def synced_store() -> RecordStore:
            current = {str(user["id"]) for user in users}
            for user_id in store.snapshot():
                if user_id not in current:
                    store.delete(user_id)
            store.put_many({str(user["id"]): list(user.get("download_records") or []) for user in users})
            return store

        if method == "list_users_async":
            async def listed():
                return views()
        else:
            def listed():
                return views()
        try:
            with (
                patch.object(monitor_api.monitor_service, method, side_effect=listed),
                patch.object(monitor_api, "_records", side_effect=synced_store),
            ):
                yield store
        finally:
            store.close()
