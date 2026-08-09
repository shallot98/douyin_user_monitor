"""Dashboard API payload projection — keep first paint small.

Full monitor state is 10MB+ because each user carries complete
`downloaded_aweme_ids` + `download_records`. The monitor panel only needs
summary fields, a few recent records for the activity feed, and a slim
status blob (no raw cookies / full liveness samples).
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence

DASHBOARD_RECENT_RECORDS_PER_USER = 3
DASHBOARD_ACTIVITY_LIMIT = 50
STATUS_LIVENESS_SAMPLE_LIMIT = 0  # panel only shows status/signal/counts


def _record_sort_key(record: Mapping[str, Any]) -> str:
    return str(record.get("downloaded_at") or record.get("publish_time") or "")


def _trim_download_record(record: Mapping[str, Any], *, user_nickname: str | None = None) -> Dict[str, Any]:
    item = {
        "aweme_id": record.get("aweme_id"),
        "media_type": record.get("media_type"),
        "image_count": record.get("image_count"),
        "downloaded_file_count": record.get("downloaded_file_count"),
        "downloaded_at": record.get("downloaded_at"),
        "publish_time": record.get("publish_time"),
        "total_size_bytes": record.get("total_size_bytes"),
    }
    if user_nickname is not None:
        item["user_nickname"] = user_nickname
    return item


def project_dashboard_user(
    user: Mapping[str, Any],
    *,
    recent_records: int = DASHBOARD_RECENT_RECORDS_PER_USER,
) -> Dict[str, Any]:
    """Strip heavy fields while keeping list/row render data."""
    records = user.get("download_records")
    recent: List[Dict[str, Any]] = []
    if isinstance(records, list) and recent_records > 0:
        sorted_records = sorted(
            (row for row in records if isinstance(row, dict)),
            key=_record_sort_key,
            reverse=True,
        )
        recent = [_trim_download_record(row) for row in sorted_records[:recent_records]]

    return {
        "id": user.get("id"),
        "profile_url": user.get("profile_url"),
        "sec_user_id": user.get("sec_user_id"),
        "nickname": user.get("nickname"),
        "enabled": user.get("enabled"),
        "created_at": user.get("created_at"),
        "updated_at": user.get("updated_at"),
        "last_checked_at": user.get("last_checked_at"),
        "last_download_at": user.get("last_download_at"),
        "last_aweme_id": user.get("last_aweme_id"),
        "downloaded_count": user.get("downloaded_count"),
        "last_error": user.get("last_error"),
        "avatar_url": user.get("avatar_url"),
        "history_sync": user.get("history_sync"),
        "account_status": user.get("account_status"),
        "account_status_label": user.get("account_status_label"),
        "account_status_reason": user.get("account_status_reason"),
        "account_status_updated_at": user.get("account_status_updated_at"),
        # Keep a tiny slice for per-user context; global feed is separate.
        "download_records": recent,
    }


def build_recent_activity(
    users: Sequence[Mapping[str, Any]],
    *,
    limit: int = DASHBOARD_ACTIVITY_LIMIT,
) -> List[Dict[str, Any]]:
    feed: List[Dict[str, Any]] = []
    for user in users:
        nickname = str(user.get("nickname") or user.get("sec_user_id") or "-")
        records = user.get("download_records")
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            feed.append(_trim_download_record(record, user_nickname=nickname))
    feed.sort(key=_record_sort_key, reverse=True)
    return feed[: max(0, int(limit))]


def project_dashboard_users(
    users: Sequence[Mapping[str, Any]],
    *,
    recent_records_per_user: int = DASHBOARD_RECENT_RECORDS_PER_USER,
    activity_limit: int = DASHBOARD_ACTIVITY_LIMIT,
) -> Dict[str, Any]:
    projected = [
        project_dashboard_user(user, recent_records=recent_records_per_user)
        for user in users
        if isinstance(user, Mapping)
    ]
    return {
        "users": projected,
        "recent_activity": build_recent_activity(users, limit=activity_limit),
    }


def _project_cookie_pool(pool: Any) -> Any:
    if not isinstance(pool, dict):
        return pool
    items = pool.get("items")
    public_items: List[Dict[str, Any]] = []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            cookie = str(item.get("cookie") or "")
            # Prefer already-public shape; fall back to masking raw cookie.
            if "cookie_preview" in item or "cookie" not in item:
                public_items.append(
                    {
                        "id": item.get("id"),
                        "name": item.get("name"),
                        "cookie_preview": item.get("cookie_preview"),
                        "cookie_length": item.get("cookie_length"),
                        "is_active": bool(item.get("is_active")),
                        "last_check_at": item.get("last_check_at"),
                        "status": item.get("status"),
                        "reason": item.get("reason"),
                        "samples_used": int(item.get("samples_used") or 0),
                        "newest_create_time": item.get("newest_create_time"),
                        "last_error": item.get("last_error"),
                        "use_count": int(item.get("use_count") or 0),
                        "last_used_at": item.get("last_used_at"),
                        "auth_fail_streak": int(item.get("auth_fail_streak") or 0),
                        "created_at": item.get("created_at"),
                        "updated_at": item.get("updated_at"),
                    }
                )
            else:
                length = len(cookie.strip())
                preview = (
                    f"{cookie[:24]}...{cookie[-8:]} (len={length})"
                    if length > 40
                    else (cookie or "(empty)")
                )
                public_items.append(
                    {
                        "id": item.get("id"),
                        "name": item.get("name"),
                        "cookie_preview": preview,
                        "cookie_length": length,
                        "is_active": bool(item.get("is_active")),
                        "last_check_at": item.get("last_check_at"),
                        "status": item.get("status"),
                        "reason": item.get("reason"),
                        "samples_used": int(item.get("samples_used") or 0),
                        "newest_create_time": item.get("newest_create_time"),
                        "last_error": item.get("last_error"),
                        "use_count": int(item.get("use_count") or 0),
                        "last_used_at": item.get("last_used_at"),
                        "auth_fail_streak": int(item.get("auth_fail_streak") or 0),
                        "created_at": item.get("created_at"),
                        "updated_at": item.get("updated_at"),
                    }
                )
    return {
        "items": public_items,
        "updated_at": pool.get("updated_at"),
        "rotation_cursor": pool.get("rotation_cursor"),
    }


def _project_cookie_liveness(live: Any) -> Any:
    if not isinstance(live, dict):
        return live
    samples = live.get("samples")
    sample_count = len(samples) if isinstance(samples, list) else 0
    projected = {
        "last_check_at": live.get("last_check_at"),
        "status": live.get("status"),
        "reason": live.get("reason"),
        "signal": live.get("signal"),
        "fresh_count": live.get("fresh_count"),
        "newest_create_time": live.get("newest_create_time"),
        "samples_used": live.get("samples_used", sample_count),
        "last_alert_at": live.get("last_alert_at"),
        "last_error": live.get("last_error"),
        "alerted": live.get("alerted"),
        "updated_at": live.get("updated_at"),
    }
    if STATUS_LIVENESS_SAMPLE_LIMIT > 0 and isinstance(samples, list):
        projected["samples"] = samples[:STATUS_LIVENESS_SAMPLE_LIMIT]
    return projected


def _project_coverage_progress(progress: Any) -> Any:
    if not isinstance(progress, dict):
        return progress
    processed = progress.get("processed_user_ids")
    processed_count = len(processed) if isinstance(processed, list) else 0
    return {
        "phase": progress.get("phase"),
        "cycle_started_at": progress.get("cycle_started_at"),
        "idle_until": progress.get("idle_until"),
        "processed_user_count": processed_count,
        "checked_users": progress.get("checked_users"),
        "downloaded_items": progress.get("downloaded_items"),
        "errors": progress.get("errors") if isinstance(progress.get("errors"), list) else [],
        "priority_user_ids": progress.get("priority_user_ids")
        if isinstance(progress.get("priority_user_ids"), list)
        else [],
    }


def project_public_status(monitoring: Mapping[str, Any]) -> Dict[str, Any]:
    """Panel-safe status: no raw cookies, no 50-sample liveness dump."""
    status = dict(monitoring)
    if "cookie_liveness" in status:
        status["cookie_liveness"] = _project_cookie_liveness(status.get("cookie_liveness"))
    if "cookie_pool" in status:
        status["cookie_pool"] = _project_cookie_pool(status.get("cookie_pool"))
    if "coverage_progress" in status:
        status["coverage_progress"] = _project_coverage_progress(status.get("coverage_progress"))
    return status
