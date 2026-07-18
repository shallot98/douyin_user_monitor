from __future__ import annotations

import uuid
from contextlib import AbstractAsyncContextManager
from typing import Any, Dict, List, Optional, Protocol, Sequence

from douyin_user_monitor.monitor.cookie_liveness import CookieLivenessResult, CookieLivenessService
from douyin_user_monitor.monitor.storage import utc_now

PREVIEW_HEAD = 24
PREVIEW_TAIL = 8
DEFAULT_COOKIE_NAME = "默认 Cookie"
POOL_KEY = "cookie_pool"


class CookieRuntime(Protocol):
    async def get_cookie(self) -> str:
        ...

    async def update_cookie(self, cookie: str) -> None:
        ...

    def temporary_cookie(self, cookie: str) -> AbstractAsyncContextManager[None]:
        ...


class CookiePoolStorage(Protocol):
    def load_state(self) -> Dict[str, Any]:
        ...

    def save_state(self, state: Dict[str, Any]) -> None:
        ...


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
    pool.setdefault("updated_at", utc_now())
    return pool


def build_cookie_item(
    *,
    cookie: str,
    name: str,
    is_active: bool = False,
    item_id: Optional[str] = None,
) -> Dict[str, Any]:
    now = utc_now()
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
    }


def find_item(items: Sequence[Dict[str, Any]], item_id: str) -> Optional[Dict[str, Any]]:
    for item in items:
        if str(item.get("id") or "") == item_id:
            return item
    return None


def apply_liveness_to_item(item: Dict[str, Any], result: CookieLivenessResult) -> None:
    now = utc_now()
    item["last_check_at"] = now
    item["status"] = result.status
    item["reason"] = result.reason
    item["samples_used"] = result.samples_used
    item["newest_create_time"] = result.newest_create_time
    item["last_error"] = None
    item["updated_at"] = now


def liveness_payload_from_result(
    result: CookieLivenessResult,
    *,
    previous: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    prev = previous if isinstance(previous, dict) else {}
    now = utc_now()
    return {
        "last_check_at": now,
        "status": result.status,
        "reason": result.reason,
        "newest_create_time": result.newest_create_time,
        "samples": result.samples,
        "samples_used": result.samples_used,
        "last_alert_at": prev.get("last_alert_at"),
        "last_error": None,
        "alerted": False,
        "updated_at": now,
    }


class CookiePoolService:
    def __init__(
        self,
        *,
        storage: CookiePoolStorage,
        crawler: CookieRuntime,
        liveness_service: CookieLivenessService,
    ):
        self._storage = storage
        self._crawler = crawler
        self._liveness = liveness_service

    async def list_items(self) -> List[Dict[str, Any]]:
        state = await self._load_bootstrapped()
        pool = ensure_cookie_pool(state["monitoring"])
        return [to_public_item(item) for item in pool["items"] if isinstance(item, dict)]

    async def add_item(self, *, cookie: str, name: Optional[str] = None) -> Dict[str, Any]:
        cookie_text = (cookie or "").strip()
        if not cookie_text:
            raise ValueError("Cookie 不能为空")

        state = await self._load_bootstrapped()
        pool = ensure_cookie_pool(state["monitoring"])
        items: List[Dict[str, Any]] = pool["items"]
        auto_name = (name or "").strip() or f"cookie-{len(items) + 1}"
        is_first = len(items) == 0
        item = build_cookie_item(cookie=cookie_text, name=auto_name, is_active=is_first)
        items.append(item)
        self._touch(state, pool, item)

        if is_first:
            await self._crawler.update_cookie(cookie_text)

        self._storage.save_state(state)
        return to_public_item(item)

    async def update_item(
        self,
        item_id: str,
        *,
        name: Optional[str] = None,
        cookie: Optional[str] = None,
    ) -> Dict[str, Any]:
        state = await self._load_bootstrapped()
        pool = ensure_cookie_pool(state["monitoring"])
        item = find_item(pool["items"], item_id)
        if item is None:
            raise KeyError("Cookie 不存在")

        if name is not None:
            text = name.strip()
            if not text:
                raise ValueError("名称不能为空")
            item["name"] = text

        cookie_changed = False
        if cookie is not None:
            cookie_text = cookie.strip()
            if not cookie_text:
                raise ValueError("Cookie 不能为空")
            item["cookie"] = cookie_text
            self._clear_check_fields(item)
            cookie_changed = True

        self._touch(state, pool, item)
        if cookie_changed and item.get("is_active"):
            await self._crawler.update_cookie(str(item["cookie"]))
        self._storage.save_state(state)
        return to_public_item(item)

    async def delete_item(self, item_id: str) -> Dict[str, Any]:
        state = await self._load_bootstrapped()
        pool = ensure_cookie_pool(state["monitoring"])
        item = find_item(pool["items"], item_id)
        if item is None:
            raise KeyError("Cookie 不存在")
        if item.get("is_active"):
            raise ValueError("不能删除当前启用的 Cookie，请先启用其他条目")

        public = to_public_item(item)
        pool["items"] = [row for row in pool["items"] if str(row.get("id") or "") != item_id]
        pool["updated_at"] = utc_now()
        state["monitoring"]["updated_at"] = pool["updated_at"]
        self._storage.save_state(state)
        return public

    async def activate(self, item_id: str) -> Dict[str, Any]:
        state = await self._load_bootstrapped()
        pool = ensure_cookie_pool(state["monitoring"])
        item = find_item(pool["items"], item_id)
        if item is None:
            raise KeyError("Cookie 不存在")

        cookie_text = str(item.get("cookie") or "").strip()
        if not cookie_text:
            raise ValueError("Cookie 内容为空，无法启用")

        await self._crawler.update_cookie(cookie_text)
        now = utc_now()
        for row in pool["items"]:
            if not isinstance(row, dict):
                continue
            active = str(row.get("id") or "") == item_id
            row["is_active"] = active
            if active:
                row["updated_at"] = now
        pool["updated_at"] = now
        state["monitoring"]["updated_at"] = now
        self._storage.save_state(state)
        return to_public_item(item)

    async def check_item(self, item_id: str) -> Dict[str, Any]:
        state = await self._load_bootstrapped()
        pool = ensure_cookie_pool(state["monitoring"])
        item = find_item(pool["items"], item_id)
        if item is None:
            raise KeyError("Cookie 不存在")

        cookie_text = str(item.get("cookie") or "").strip()
        if not cookie_text:
            raise ValueError("Cookie 内容为空，无法测活")

        try:
            async with self._crawler.temporary_cookie(cookie_text):
                result = await self._liveness.run_check(state["users"])
        except Exception as exc:  # noqa: BLE001
            now = utc_now()
            item["last_error"] = str(exc)
            item["status"] = "unknown"
            item["reason"] = f"测活失败: {exc}"
            item["last_check_at"] = now
            item["updated_at"] = now
            pool["updated_at"] = now
            state["monitoring"]["updated_at"] = now
            self._storage.save_state(state)
            return to_public_item(item)

        apply_liveness_to_item(item, result)
        if item.get("is_active"):
            prev = state["monitoring"].get("cookie_liveness")
            state["monitoring"]["cookie_liveness"] = liveness_payload_from_result(
                result,
                previous=prev if isinstance(prev, dict) else None,
            )
        pool["updated_at"] = item["updated_at"]
        state["monitoring"]["updated_at"] = item["updated_at"]
        self._storage.save_state(state)
        return to_public_item(item)

    async def _load_bootstrapped(self) -> Dict[str, Any]:
        state = self._storage.load_state()
        monitoring = state.setdefault("monitoring", {})
        pool = ensure_cookie_pool(monitoring)
        if pool["items"]:
            return state

        raw = (await self._crawler.get_cookie()).strip()
        if not raw:
            return state

        pool["items"] = [build_cookie_item(cookie=raw, name=DEFAULT_COOKIE_NAME, is_active=True)]
        pool["updated_at"] = utc_now()
        monitoring["updated_at"] = pool["updated_at"]
        self._storage.save_state(state)
        return state

    @staticmethod
    def _touch(state: Dict[str, Any], pool: Dict[str, Any], item: Dict[str, Any]) -> None:
        now = utc_now()
        item["updated_at"] = now
        pool["updated_at"] = now
        state["monitoring"]["updated_at"] = now

    @staticmethod
    def _clear_check_fields(item: Dict[str, Any]) -> None:
        item["status"] = None
        item["reason"] = None
        item["last_check_at"] = None
        item["samples_used"] = 0
        item["newest_create_time"] = None
        item["last_error"] = None
