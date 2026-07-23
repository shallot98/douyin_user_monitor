from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

STATUS_EXPIRED = "expired"


def select_rotation_candidates(
    items: Sequence[Dict[str, Any]],
    *,
    skip_expired: bool = True,
) -> List[Dict[str, Any]]:
    usable: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        cookie = str(item.get("cookie") or "").strip()
        if not cookie:
            continue
        usable.append(item)

    if not usable:
        return []

    if not skip_expired:
        return usable

    filtered = [
        item
        for item in usable
        if str(item.get("status") or "").strip() != STATUS_EXPIRED
    ]
    # Prefer non-expired whenever any remain (even a single healthy cookie).
    # Fall back to all usable only when every candidate is expired.
    if filtered:
        return filtered
    return usable


def pick_round_robin(
    candidates: Sequence[Dict[str, Any]],
    *,
    cursor: int,
) -> tuple[Optional[Dict[str, Any]], int]:
    if not candidates:
        return None, cursor
    index = cursor % len(candidates)
    chosen = candidates[index]
    next_cursor = (index + 1) % len(candidates)
    return chosen, next_cursor


DEFAULT_AUTH_FAIL_LIMIT = 3
# Strong phrases only — avoid bare "cookie"/"session"/"expired"/"auth"/"login"
# which match network/timeout/TLS noise and false-expire healthy cookies.
_AUTH_MARKERS = (
    "401",
    "403",
    "passport",
    "未登录",
    "鉴权",
    "forbidden",
    "login required",
    "not logged in",
    "auth expired",
    "auth fail",
    "authentication",
    "unauthorized",
    "permission denied",
    "status_code=401",
    "status_code=403",
    "http 401",
    "http 403",
    "session invalid",
    "session expired",
)


def is_auth_like_error(message: str) -> bool:
    text = (message or "").strip().lower()
    if not text:
        return False
    return any(marker in text for marker in _AUTH_MARKERS)


def apply_rotation_outcome(
    item: Dict[str, Any],
    *,
    success: bool,
    error_text: Optional[str],
    now: str,
    auth_fail_limit: int = DEFAULT_AUTH_FAIL_LIMIT,
) -> None:
    """Write per-cookie liveness fields after one rotation-session use."""
    item["updated_at"] = now
    item["last_check_at"] = now
    if success:
        item["status"] = "healthy"
        item["reason"] = "轮换使用成功"
        item["last_error"] = None
        item["auth_fail_streak"] = 0
        return

    safe = (error_text or "unknown error").strip() or "unknown error"
    item["last_error"] = safe
    if not is_auth_like_error(safe):
        # Non-auth errors break the consecutive streak; keep prior status.
        item["auth_fail_streak"] = 0
        if not item.get("reason"):
            item["reason"] = f"轮换使用出错: {safe[:80]}"
        return

    streak = int(item.get("auth_fail_streak") or 0) + 1
    item["auth_fail_streak"] = streak
    limit = max(1, int(auth_fail_limit))
    if streak >= limit:
        item["status"] = STATUS_EXPIRED
        item["reason"] = f"轮换连续鉴权失败 {streak} 次"
        return
    item["status"] = "unknown"
    item["reason"] = f"轮换鉴权失败 {streak}/{limit}"

