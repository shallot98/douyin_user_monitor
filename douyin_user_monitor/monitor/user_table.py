"""Paginated / filterable monitor user table (statistics + ops list).

Design: .trellis/tasks/08-09-unify-monitor-list-in-statistics/design.md
Compat: callers without page/page_size keep using list_dashboard_users.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Dict, List, Mapping, Sequence

from douyin_user_monitor.monitor.profile_parser import ACCOUNT_STATUS_NORMAL
from douyin_user_monitor.monitor.statistics import (
    SILENT_7D_DAYS,
    SILENT_30D_DAYS,
    _build_user_row,
    _public_list_user,
)
from douyin_user_monitor.monitor.timeutils import CHINA_TZ

ALLOWED_PAGE_SIZES = (20, 50, 100)
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

MONITOR_VALUES = frozenset({"all", "enabled", "paused"})
ACCOUNT_VALUES = frozenset({"all", "normal", "deleted", "banned", "abnormal"})
ACTIVITY_VALUES = frozenset(
    {
        "all",
        "active_7d",
        "active_30d",
        "silent_7d",
        "silent_30d",
        "publish_unknown",
    }
)
BACKFILL_VALUES = frozenset({"all", "idle", "active", "paused", "completed", "failed"})
SORT_BY_VALUES = frozenset(
    {
        "downloads",
        "last_publish",
        "last_check",
        "last_download",
        "nickname",
        "created",
    }
)
SORT_ORDER_VALUES = frozenset({"asc", "desc"})


def _clamp_page_size(raw: Any) -> int:
    """UI offers 20/50/100; API accepts 1..MAX and clamps. Invalid → default."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_PAGE_SIZE
    if value < 1:
        return DEFAULT_PAGE_SIZE
    if value > MAX_PAGE_SIZE:
        return MAX_PAGE_SIZE
    return value


def _clamp_page(raw: Any, total_pages: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 1
    if value < 1:
        return 1
    return min(value, max(1, total_pages))


def _norm_enum(raw: Any, allowed: frozenset[str], default: str) -> str:
    text = str(raw or "").strip().lower()
    return text if text in allowed else default


def _match_search(row: Mapping[str, Any], keyword: str) -> bool:
    if not keyword:
        return True
    haystacks = (
        row.get("nickname"),
        row.get("sec_user_id"),
        row.get("id"),
        row.get("profile_url"),
    )
    return any(keyword in str(value or "").lower() for value in haystacks)


def _match_monitor(row: Mapping[str, Any], monitor: str) -> bool:
    if monitor == "all":
        return True
    enabled = bool(row.get("enabled", True))
    if monitor == "enabled":
        return enabled
    if monitor == "paused":
        return not enabled
    return True


def _match_account(row: Mapping[str, Any], account: str) -> bool:
    if account == "all":
        return True
    status = str(row.get("account_status") or ACCOUNT_STATUS_NORMAL).strip() or ACCOUNT_STATUS_NORMAL
    if account == "abnormal":
        return status != ACCOUNT_STATUS_NORMAL
    if account == "normal":
        return status == ACCOUNT_STATUS_NORMAL
    return status == account


def _match_activity(row: Mapping[str, Any], activity: str) -> bool:
    if activity == "all":
        return True
    if activity == "publish_unknown":
        return not bool(row.get("activity_known"))
    if activity == "active_7d":
        return bool(row.get("has_publish_in_7d"))
    if activity == "active_30d":
        return bool(row.get("has_publish_in_30d"))
    if activity == "silent_7d":
        return bool(row.get("is_silent_7d"))
    if activity == "silent_30d":
        return bool(row.get("is_silent_30d"))
    return True


def _backfill_status(row: Mapping[str, Any]) -> str:
    sync = row.get("history_sync")
    if not isinstance(sync, Mapping):
        return "idle"
    status = str(sync.get("status") or "idle").strip().lower() or "idle"
    if status in {"pending", "running"}:
        return "active"
    if status in {"idle", "active", "paused", "completed", "failed"}:
        return status
    return "idle"


def _match_backfill(row: Mapping[str, Any], backfill: str) -> bool:
    if backfill == "all":
        return True
    return _backfill_status(row) == backfill


def _timestamp_or_none(raw: Any) -> float | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CHINA_TZ)
    return parsed.timestamp()


def _sort_key(row: Mapping[str, Any], sort_by: str, sort_order: str) -> tuple:
    reverse = sort_order == "desc"
    uid = str(row.get("id") or "")

    def _num(value: Any, *, missing: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return missing

    def _ts(field: str) -> float:
        stamp = _timestamp_or_none(row.get(field))
        if stamp is None:
            return float("inf")  # nulls always last in ascending key space
        return -stamp if reverse else stamp

    if sort_by == "downloads":
        primary: Any = _num(row.get("total_downloaded_works"), missing=0.0)
        primary = -primary if reverse else primary
        return (0, primary, uid)
    if sort_by == "last_publish":
        return (0, _ts("last_publish_at"), uid)
    if sort_by == "last_check":
        return (0, _ts("last_checked_at"), uid)
    if sort_by == "last_download":
        return (0, _ts("last_download_at"), uid)
    if sort_by == "created":
        return (0, _ts("created_at"), uid)
    if sort_by == "nickname":
        name = str(row.get("nickname") or row.get("sec_user_id") or "").lower()
        name_key = tuple((-ord(ch) if reverse else ord(ch)) for ch in name)
        return (0, name_key, uid)

    primary = _num(row.get("total_downloaded_works"), missing=0.0)
    primary = -primary if reverse else primary
    return (0, primary, uid)


def _project_table_row(raw_user: Mapping[str, Any], built: Dict[str, Any]) -> Dict[str, Any]:
    item = _public_list_user(built)
    item["downloaded_count"] = built.get("total_downloaded_works")
    item["created_at"] = raw_user.get("created_at")
    item["updated_at"] = raw_user.get("updated_at")
    item["last_error"] = built.get("last_error")
    item["last_aweme_id"] = raw_user.get("last_aweme_id")
    item["history_sync"] = raw_user.get("history_sync")
    # keep internal flags out of wire format (already stripped by _public_list_user)
    return item


def build_table_rows(
    users: Sequence[Mapping[str, Any]],
    *,
    now: datetime | None = None,
) -> List[Dict[str, Any]]:
    current = now.astimezone(CHINA_TZ) if now else datetime.now(CHINA_TZ)
    rows: List[Dict[str, Any]] = []
    for user in users:
        if not isinstance(user, Mapping):
            continue
        built = _build_user_row(dict(user), current)
        # flags needed for filtering before public projection
        row = dict(built)
        row["history_sync"] = user.get("history_sync")
        row["created_at"] = user.get("created_at")
        row["updated_at"] = user.get("updated_at")
        row["last_aweme_id"] = user.get("last_aweme_id")
        row["_raw"] = user
        rows.append(row)
    return rows


def query_monitor_users(
    users: Sequence[Mapping[str, Any]],
    *,
    search: str = "",
    monitor: str = "all",
    account: str = "all",
    activity: str = "all",
    backfill: str = "all",
    sort_by: str = "downloads",
    sort_order: str = "desc",
    page: Any = 1,
    page_size: Any = DEFAULT_PAGE_SIZE,
    now: datetime | None = None,
) -> Dict[str, Any]:
    """Filter + sort + paginate monitor users for the unified table."""
    monitor_v = _norm_enum(monitor, MONITOR_VALUES, "all")
    account_v = _norm_enum(account, ACCOUNT_VALUES, "all")
    activity_v = _norm_enum(activity, ACTIVITY_VALUES, "all")
    backfill_v = _norm_enum(backfill, BACKFILL_VALUES, "all")
    sort_by_v = _norm_enum(sort_by, SORT_BY_VALUES, "downloads")
    sort_order_v = _norm_enum(sort_order, SORT_ORDER_VALUES, "desc")
    # nickname default order is asc when unspecified noise — still honor sort_order
    page_size_v = _clamp_page_size(page_size)
    keyword = str(search or "").strip().lower()

    built_rows = build_table_rows(users, now=now)
    filtered = [
        row
        for row in built_rows
        if _match_search(row, keyword)
        and _match_monitor(row, monitor_v)
        and _match_account(row, account_v)
        and _match_activity(row, activity_v)
        and _match_backfill(row, backfill_v)
    ]

    # stable sort via transformed key (numeric desc = negate; null timestamps sink)
    filtered.sort(key=lambda row: _sort_key(row, sort_by_v, sort_order_v))

    total = len(filtered)
    total_pages = max(1, int(math.ceil(total / page_size_v))) if total else 1
    page_v = _clamp_page(page, total_pages)
    start = (page_v - 1) * page_size_v
    end = start + page_size_v
    page_rows = filtered[start:end]

    items = [
        _project_table_row(row.get("_raw") or row, row)
        for row in page_rows
    ]

    return {
        "items": items,
        "page": page_v,
        "page_size": page_size_v,
        "total": total,
        "total_pages": total_pages,
        "applied": {
            "search": str(search or "").strip(),
            "monitor": monitor_v,
            "account": account_v,
            "activity": activity_v,
            "backfill": backfill_v,
            "sort_by": sort_by_v,
            "sort_order": sort_order_v,
        },
        "windows": {
            "silent_7d_days": SILENT_7D_DAYS,
            "silent_30d_days": SILENT_30D_DAYS,
        },
    }


def wants_paginated_users(
    page: Any | None = None,
    page_size: Any | None = None,
    view: str | None = None,
) -> bool:
    """Compat A: paginate only when page/page_size/view=table is present."""
    view_text = str(view or "").strip().lower()
    if view_text == "table":
        return True
    if view_text == "dashboard":
        return False
    if page is not None and str(page).strip() != "":
        return True
    if page_size is not None and str(page_size).strip() != "":
        return True
    return False
