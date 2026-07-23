from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional

from douyin_user_monitor.monitor.cookie_liveness import CookieLivenessService
from douyin_user_monitor.monitor.cookie_pool_items import (
    DEFAULT_COOKIE_NAME,
    CookiePoolStorage,
    CookieRuntime,
    apply_liveness_to_item,
    build_cookie_item,
    clear_cookie_check_fields,
    ensure_cookie_pool,
    find_item,
    liveness_payload_from_result,
    mask_cookie,
    sanitize_public_error,
    to_public_item,
    touch_pool_timestamps,
)
from douyin_user_monitor.monitor.cookie_rotation import (
    pick_round_robin,
    select_rotation_candidates,
)
from douyin_user_monitor.monitor.cookie_pool_rotation_write import (
    begin_rotation_use,
    write_rotation_outcome,
)
from douyin_user_monitor.monitor.storage import utc_now

class CookiePoolService:
    def __init__(
        self,
        *,
        storage: CookiePoolStorage,
        crawler: CookieRuntime,
        liveness_service: CookieLivenessService,
        rotation_enabled: bool = True,
        rotation_skip_expired: bool = True,
    ):
        self._storage = storage
        self._crawler = crawler
        self._liveness = liveness_service
        self._rotation_enabled = bool(rotation_enabled)
        self._rotation_skip_expired = bool(rotation_skip_expired)

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
        touch_pool_timestamps(state, pool, item)

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
            clear_cookie_check_fields(item)
            cookie_changed = True

        touch_pool_timestamps(state, pool, item)
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
                result = await self._liveness.run_check(state["users"], force_probe=True)
        except Exception as exc:  # noqa: BLE001
            now = utc_now()
            safe_err = sanitize_public_error(exc)
            item["last_error"] = safe_err
            item["status"] = "unknown"
            item["reason"] = f"测活失败: {safe_err}"
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

    def rotation_enabled(self) -> bool:
        return self._rotation_enabled

    @asynccontextmanager
    async def rotation_session(self) -> AsyncIterator[Optional[Dict[str, Any]]]:
        """Pick next pool cookie and apply it for the wrapped sync work."""
        if not self._rotation_enabled:
            yield None
            return

        state = await self._load_bootstrapped()
        pool = ensure_cookie_pool(state["monitoring"])
        items = [row for row in pool["items"] if isinstance(row, dict)]
        candidates = select_rotation_candidates(
            items,
            skip_expired=self._rotation_skip_expired,
        )
        if len(candidates) < 2:
            yield None
            return

        cursor = int(pool.get("rotation_cursor") or 0)
        chosen, next_cursor = pick_round_robin(candidates, cursor=cursor)
        if chosen is None:
            yield None
            return

        cookie_text = str(chosen.get("cookie") or "").strip()
        if not cookie_text:
            yield None
            return

        item_id = str(chosen.get("id") or "")
        public_item = begin_rotation_use(
            self._storage,
            item_id=item_id,
            next_cursor=next_cursor,
        )
        if public_item is None:
            yield None
            return

        async with self._crawler.temporary_cookie(cookie_text):
            try:
                yield public_item
            except Exception as exc:  # noqa: BLE001
                write_rotation_outcome(
                    self._storage,
                    item_id,
                    success=False,
                    error_text=sanitize_public_error(exc),
                )
                raise
            else:
                write_rotation_outcome(
                    self._storage,
                    item_id,
                    success=True,
                    error_text=None,
                )


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

