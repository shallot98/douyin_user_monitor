from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence

DOWNLOAD_SOURCE_INCREMENTAL = "incremental"
DOWNLOAD_SOURCE_HISTORY = "history"
MAX_DOWNLOAD_SAMPLES = 50
MIN_DOWNLOAD_LOOKBACK_DAYS = 14
SECONDS_PER_DAY = 86400


def collect_incremental_download_samples(
    users: Sequence[Dict[str, Any]],
    *,
    now: datetime,
    lookback_seconds: int,
    stale_seconds: int,
    limit: int = MAX_DOWNLOAD_SAMPLES,
    parse_iso_datetime: Callable[[str], Optional[datetime]],
) -> List[Dict[str, Any]]:
    cutoff = now - timedelta(seconds=max(0, lookback_seconds))
    rows: List[Dict[str, Any]] = []
    for user in users:
        if not isinstance(user, dict):
            continue
        nickname = str(user.get("nickname") or "")
        user_id = str(user.get("id") or "")
        records = user.get("download_records")
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            source = str(record.get("source") or "").strip()
            if source != DOWNLOAD_SOURCE_INCREMENTAL:
                continue
            downloaded_at = parse_iso_datetime(str(record.get("downloaded_at") or ""))
            publish_at = parse_iso_datetime(str(record.get("publish_time") or ""))
            if downloaded_at is None or publish_at is None:
                continue
            if downloaded_at < cutoff:
                continue
            lag_seconds = int((downloaded_at - publish_at).total_seconds())
            if lag_seconds < 0:
                lag_seconds = 0
            rows.append(
                {
                    "kind": "download",
                    "user_id": user_id,
                    "nickname": nickname,
                    "aweme_id": str(record.get("aweme_id") or ""),
                    "publish_time": publish_at.isoformat(timespec="seconds"),
                    "downloaded_at": downloaded_at.isoformat(timespec="seconds"),
                    "publish_ts": int(publish_at.timestamp()),
                    "downloaded_ts": int(downloaded_at.timestamp()),
                    "lag_seconds": lag_seconds,
                    "is_fresh": lag_seconds <= stale_seconds,
                    "source": DOWNLOAD_SOURCE_INCREMENTAL,
                    "error": None,
                }
            )
    rows.sort(key=lambda item: int(item.get("downloaded_ts") or 0), reverse=True)
    return rows[: max(0, limit)]


def summarize_download_lag(samples):
    lags = sorted(
        int(item["lag_seconds"])
        for item in samples
        if isinstance(item.get("lag_seconds"), (int, float))
    )
    if not lags:
        return {
            "count": 0,
            "fresh_count": 0,
            "lag_p50_seconds": None,
            "lag_p95_seconds": None,
            "lag_max_seconds": None,
        }
    fresh_count = sum(1 for item in samples if bool(item.get("is_fresh")))
    return {
        "count": len(lags),
        "fresh_count": fresh_count,
        "lag_p50_seconds": lags[len(lags) // 2],
        "lag_p95_seconds": lags[max(0, int(round((len(lags) - 1) * 0.95)))],
        "lag_max_seconds": lags[-1],
    }

