from __future__ import annotations

import uuid
from contextlib import AbstractAsyncContextManager
from typing import Any, Callable, Dict, Optional, Protocol, Sequence

from douyin_user_monitor.monitor.cookie_liveness import CookieLivenessResult
from douyin_user_monitor.monitor.timeutils import cn_now


class CookieRuntime(Protocol):
    async def get_cookie(self) -> str:
        ...

    async def update_cookie(self, cookie: str) -> None:
        ...

    async def clear_cookie(self) -> None:
        ...

    def temporary_cookie(self, cookie: str) -> AbstractAsyncContextManager[None]:
        ...


class CookiePoolStorage(Protocol):
    def load_state(self) -> Dict[str, Any]:
        ...

    def save_state(self, state: Dict[str, Any]) -> None:
        ...

    def update_state(self, mutator: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
        ...


PREVIEW_HEAD = 24
PREVIEW_TAIL = 8
DEFAULT_COOKIE_NAME = "默认 Cookie"
POOL_KEY = "cookie_pool"


def mask_cookie(cookie: str) -> str:
    text = (cookie or "").strip()
    length = len(text)
    if length == 0:
        return "(empty)"
    if length <= PREVIEW_HEAD + PREVIEW_TAIL:
        return f"{text[: max(1, length // 3)]}... (len={length})"
    return f"{text[:PREVIEW_HEAD]}...{text[-PREVIEW_TAIL:]} (len={length})"


def to_public_item(item: Dict[str, Any]) -> Dict[str, Any]:
    cookie = str(item.get("cookie") or "")
    return {
        "id": item.get("id"),
        "name": item.get("name"),
        "cookie_preview": mask_cookie(cookie),
        "cookie_length": len(cookie.strip()),
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


def ensure_cookie_pool(monitoring: Dict[str, Any]) -> Dict[str, Any]:
    pool = monitoring.get(POOL_KEY)
    if not isinstance(pool, dict):
        pool = {}
        monitoring[POOL_KEY] = pool
    if not isinstance(pool.get("items"), list):
        pool["items"] = []
    if not isinstance(pool.get("rotation_cursor"), int):
        pool["rotation_cursor"] = 0
    pool.setdefault("updated_at", cn_now())
    return pool


def build_cookie_item(
    *,
    cookie: str,
    name: str,
    is_active: bool = False,
    item_id: Optional[str] = None,
) -> Dict[str, Any]:
    now = cn_now()
    return {
        "id": item_id or str(uuid.uuid4()),
        "name": name.strip() or DEFAULT_COOKIE_NAME,
        "cookie": cookie.strip(),
        "is_active": is_active,
        "created_at": now,
        "updated_at": now,
        "last_check_at": None,
        "status": None,
        "reason": None,
        "samples_used": 0,
        "newest_create_time": None,
        "last_error": None,
        "use_count": 0,
        "last_used_at": None,
        "auth_fail_streak": 0,
    }


def find_item(items: Sequence[Dict[str, Any]], item_id: str) -> Optional[Dict[str, Any]]:
    for item in items:
        if str(item.get("id") or "") == item_id:
            return item
    return None


def apply_liveness_to_item(item: Dict[str, Any], result: CookieLivenessResult) -> None:
    now = cn_now()
    item["last_check_at"] = now
    item["status"] = result.status
    item["reason"] = result.reason
    item["samples_used"] = result.samples_used
    item["newest_create_time"] = result.newest_create_time
    item["last_error"] = None
    item["updated_at"] = now
    if getattr(result, "signal", None) is not None:
        item["last_signal"] = result.signal


def liveness_payload_from_result(
    result: CookieLivenessResult,
    *,
    previous: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    prev = previous if isinstance(previous, dict) else {}
    now = cn_now()
    return {
        "last_check_at": now,
        "status": result.status,
        "reason": result.reason,
        "signal": getattr(result, "signal", None),
        "fresh_count": getattr(result, "fresh_count", 0),
        "newest_create_time": result.newest_create_time,
        "samples": result.samples,
        "samples_used": result.samples_used,
        "last_alert_at": prev.get("last_alert_at"),
        "last_error": None,
        "alerted": False,
        "updated_at": now,
    }


def touch_pool_timestamps(state: Dict[str, Any], pool: Dict[str, Any], item: Dict[str, Any]) -> None:
    now = cn_now()
    item["updated_at"] = now
    pool["updated_at"] = now
    state["monitoring"]["updated_at"] = now


def clear_cookie_check_fields(item: Dict[str, Any]) -> None:
    item["status"] = None
    item["reason"] = None
    item["last_check_at"] = None
    item["samples_used"] = 0
    item["newest_create_time"] = None
    item["last_error"] = None

