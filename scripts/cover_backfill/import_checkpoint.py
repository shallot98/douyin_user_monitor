"""Locked importer cursor, durable per-attempt audit, and exact current-record matching."""

from __future__ import annotations

import fcntl
import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from douyin_user_monitor.monitor.record_store import load_state_with_records

from .journal import atomic_json
from .models import SCHEMA_VERSION, BackfillError

OUTCOMES = ("published", "repaired", "already_available", "skipped", "failed")


class CurrentRecords:
    def __init__(self, path):
        self._path = path
        self._stamp = None
        self._users = []

    def find(self, receipt):
        stat = self._path.stat()
        stamp = stat.st_ino, stat.st_size, stat.st_mtime_ns
        if stamp != self._stamp:
            # 记录写入都在 update_state 锁内、必然改写 JSON，状态文件标识即可当记录的失效键
            state = load_state_with_records(self._path)
            if not isinstance(state, dict) or not isinstance(state.get("users"), list):
                raise BackfillError("Monitor state must contain its current users list")
            self._users, self._stamp = state["users"], stamp
        return current_record(self._users, receipt)


@contextmanager
def checkpoint_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackfillError("Another cover importer owns this checkpoint") from None
        yield


def load_checkpoint(path):
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "next_offset": 0, "batches": {},
                "totals": dict.fromkeys(OUTCOMES, 0)}
    checkpoint = json.loads(path.read_bytes())
    if (not isinstance(checkpoint, dict) or checkpoint.get("schema_version") != SCHEMA_VERSION
            or type(checkpoint.get("next_offset")) is not int or checkpoint["next_offset"] < 0
            or not isinstance(checkpoint.get("batches"), dict)
            or not isinstance(checkpoint.get("totals"), dict)):
        raise BackfillError("Invalid cover importer checkpoint")
    if any(type(checkpoint["totals"].get(key)) is not int or checkpoint["totals"][key] < 0 for key in OUTCOMES):
        raise BackfillError("Invalid cover importer checkpoint totals")
    return checkpoint


def outcome_counts(items):
    return {key: sum(item["status"] == key for item in items) for key in OUTCOMES}


def write_report(checkpoint, report):
    directory = checkpoint.parent / f"{checkpoint.stem}-reports"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{report['batch_sha256'][:16]}-{uuid.uuid4().hex}.json"
    atomic_json(target, {**report, "finished_at": datetime.now(timezone.utc).isoformat()})
    return target


def commit_checkpoint(path, previous, report):
    metadata, counts = report["batch"], report["counts"]
    existing = previous["batches"].get(report["batch_sha256"])
    if existing is None:
        entry = {"report": report["report"], "after_offset": metadata["after_offset"],
                 "next_offset": metadata["next_offset"], "counts": counts}
        next_offset, increments = metadata["next_offset"], counts
    else:
        entry = {**existing, "last_repair_report": report["report"]}
        next_offset = previous["next_offset"]
        increments = {key: counts[key] if key == "repaired" else 0 for key in OUTCOMES}
    updated = {**previous, "next_offset": next_offset,
               "batches": {**previous["batches"], report["batch_sha256"]: entry},
               "totals": {key: previous["totals"][key] + increments[key] for key in OUTCOMES}}
    atomic_json(path, updated)
    return updated


def current_record(users, receipt):
    user = next((user for user in users if str(user.get("id")) == receipt["user_id"]), None)
    if user is None:
        return None, "user_deleted"
    if user.get("sec_user_id") != receipt["sec_user_id"]:
        return None, "sec_user_id_changed"
    records = [record for record in user.get("download_records", [])
               if str(record.get("aweme_id")) == receipt["aweme_id"]]
    if not records:
        return None, "work_deleted"
    records = [record for record in records if record.get("downloaded_at") == receipt["downloaded_at"]]
    if not records:
        return None, "download_revision_changed"
    for record in records:
        if isinstance(record.get("files"), list) and receipt["source_relative_path"] in record["files"]:
            return record, None
    return None, "source_path_removed"
