"""One-shot move of `download_records` out of monitor_users.json into monitor_records.db.

Run with the monitor service and the playback-repair timer stopped:

    python -m scripts.split_download_records            # migrate (default state path)
    python -m scripts.split_download_records --rollback # put records back into the JSON

Migrate: backup JSON → import every user's records → re-read and byte-compare →
strip records from the JSON. Refuses to run on a half-migrated pair instead of guessing.
Rollback writes the legacy single-file layout again (for the pre-split code); the
DB file is left in place and must be moved away before migrating a second time.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

import orjson

from douyin_user_monitor.monitor.record_store import RecordStore, record_db_path
from douyin_user_monitor.monitor.storage import MonitorStorage
from douyin_user_monitor.monitor.sync_utils import merge_downloaded_ids


class SplitError(RuntimeError):
    pass


def _dump(value) -> bytes:
    return orjson.dumps(value)


def _backup(state_file: Path, tag: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = state_file.with_name(f"{state_file.name}.{tag}.{stamp}.bak")
    shutil.copy2(state_file, target)
    return target


def migrate(state_file: Path) -> dict:
    storage = MonitorStorage(state_file)
    with storage._state_file_lock():  # noqa: SLF001 — 与监控进程同一把锁，防并发写
        state = orjson.loads(state_file.read_bytes())
        users = state.get("users") or []
        legacy = [user for user in users if isinstance(user, dict) and "download_records" in user]
        if not legacy:
            return {"status": "already_split", "users": len(users)}
        if storage.records.snapshot():
            raise SplitError(
                f"{storage.records.db_path} already has rows while the JSON still carries records; "
                "resolve manually (rollback leftovers?) instead of merging two sources"
            )

        backup = _backup(state_file, "pre-split")
        mapping = {}
        for user in legacy:
            records = user.get("download_records")
            if not isinstance(records, list):
                raise SplitError(f"user {user.get('id')!r}: download_records is not a list")
            if records:
                mapping[str(user["id"])] = records
        storage.records.put_many(mapping)

        # 独立连接重新读回逐用户比对，确认落库内容与原 JSON 完全一致
        verify = RecordStore(storage.records.db_path)
        try:
            stored = verify.snapshot()
        finally:
            verify.close()
        if set(stored) != set(mapping):
            raise SplitError("record store user set differs from the JSON after import")
        for user_id, records in mapping.items():
            if _dump(stored[user_id]) != _dump(records):
                raise SplitError(f"user {user_id!r}: stored records differ from the JSON")

        before = state_file.stat().st_size
        for user in legacy:
            records = user.pop("download_records")
            # 去重依据只剩 downloaded_aweme_ids：确保它覆盖所有已有记录
            user["downloaded_aweme_ids"] = merge_downloaded_ids(
                user.get("downloaded_aweme_ids") or [],
                [record.get("aweme_id", "") for record in records if isinstance(record, dict)],
            )
        storage._write_json_atomic(state)  # noqa: SLF001
        return {
            "status": "migrated",
            "users": len(users),
            "users_with_records": len(mapping),
            "records": sum(len(records) for records in mapping.values()),
            "json_bytes_before": before,
            "json_bytes_after": state_file.stat().st_size,
            "db": str(storage.records.db_path),
            "backup": str(backup),
        }


def rollback(state_file: Path) -> dict:
    db_path = record_db_path(state_file)
    if not db_path.exists():
        raise SplitError(f"nothing to roll back: {db_path} does not exist")
    storage = MonitorStorage(state_file)
    with storage._state_file_lock():  # noqa: SLF001
        state = orjson.loads(state_file.read_bytes())
        users = state.get("users") or []
        if any(isinstance(user, dict) and "download_records" in user for user in users):
            raise SplitError("JSON already carries download_records; refusing to overwrite them")
        backup = _backup(state_file, "pre-rollback")
        records = storage.records.snapshot()
        for user in users:
            user["download_records"] = list(records.get(str(user.get("id") or ""), []))
        # 旧代码的原始格式：stdlib 同款 2 空格缩进；绕过新代码的内联记录守卫直接写
        content = orjson.dumps(state, option=orjson.OPT_INDENT_2 | orjson.OPT_NON_STR_KEYS)
        temp_file = state_file.with_suffix(".tmp")
        temp_file.write_bytes(content)
        temp_file.replace(state_file)
    return {"status": "rolled_back", "users": len(users),
            "records": sum(len(rows) for rows in records.values()), "backup": str(backup),
            "note": f"move {db_path} (and -wal/-shm) away before migrating again"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--state", type=Path, help="monitor_users.json (default: configured state path)")
    parser.add_argument("--rollback", action="store_true", help="write records back into the JSON")
    args = parser.parse_args(argv)
    if args.state is None:
        from douyin_user_monitor.settings_loaders import load_settings

        args.state = load_settings().monitor.state_path
    try:
        result = rollback(args.state) if args.rollback else migrate(args.state)
    except SplitError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(orjson.dumps(result, option=orjson.OPT_INDENT_2).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
