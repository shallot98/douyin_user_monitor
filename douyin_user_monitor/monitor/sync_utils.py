from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Set

from douyin_user_monitor.monitor.timeutils import CHINA_TZ, safe_int

MAX_DOWNLOAD_RECORDS_PER_USER = 300


def format_publish_time(create_time: Any) -> str | None:
    timestamp = safe_int(create_time)
    if timestamp <= 0:
        return None
    return datetime.fromtimestamp(timestamp, tz=CHINA_TZ).isoformat(timespec="seconds")


def merge_downloaded_ids(existing_ids: List[str], new_ids: List[str]) -> List[str]:
    merged: List[str] = []
    seen: Set[str] = set()
    for aweme_id in [*existing_ids, *new_ids]:
        aweme_id_str = str(aweme_id).strip()
        if not aweme_id_str or aweme_id_str in seen:
            continue
        seen.add(aweme_id_str)
        merged.append(aweme_id_str)
    return merged


def merge_download_records(
    existing_records: List[Dict[str, Any]],
    new_records: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    seen_aweme_ids: Set[str] = set()
    for record in [*reversed(new_records), *existing_records]:
        if not isinstance(record, dict):
            continue
        aweme_id = str(record.get("aweme_id", "")).strip()
        if not aweme_id or aweme_id in seen_aweme_ids:
            continue
        seen_aweme_ids.add(aweme_id)
        merged.append(record)
        if len(merged) >= MAX_DOWNLOAD_RECORDS_PER_USER:
            break
    return merged


def merge_synced_downloads(disk_user: Dict[str, Any], snapshot: Dict[str, Any]) -> None:
    """Commit append-only downloads without rolling back concurrent record repairs."""
    if not any(key in snapshot for key in ("download_records", "downloaded_aweme_ids", "downloaded_count")):
        return
    existing_records = disk_user.get("download_records", [])
    existing_ids = merge_downloaded_ids(
        disk_user.get("downloaded_aweme_ids", []),
        [record.get("aweme_id", "") for record in existing_records if isinstance(record, dict)],
    )
    known_ids = set(existing_ids)
    new_records = [
        record for record in snapshot.get("download_records", [])
        if isinstance(record, dict) and str(record.get("aweme_id", "")).strip() not in known_ids
    ]
    snapshot_ids = merge_downloaded_ids(
        snapshot.get("downloaded_aweme_ids", []),
        [record.get("aweme_id", "") for record in new_records],
    )
    merged_ids = merge_downloaded_ids(existing_ids, snapshot_ids)
    # Snapshot records are already newest-first; the download helper reverses its new batch.
    disk_user["download_records"] = merge_download_records(existing_records, list(reversed(new_records)))
    disk_user["downloaded_aweme_ids"] = merged_ids
    disk_user["downloaded_count"] = max(
        safe_int(disk_user.get("downloaded_count")) + len(set(snapshot_ids) - known_ids),
        safe_int(snapshot.get("downloaded_count")),
        len(merged_ids),
    )
