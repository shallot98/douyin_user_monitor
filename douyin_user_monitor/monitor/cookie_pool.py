from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from functools import wraps
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

from douyin_user_monitor.monitor.cookie_liveness import STATUS_EXPIRED
from douyin_user_monitor.monitor.cookie_liveness_service import CookieLivenessService
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
    to_public_item,
    touch_pool_timestamps,
)
from douyin_user_monitor.monitor.cookie_pool_rotation_write import (
    begin_rotation_use,
    write_rotation_outcome,
)
from douyin_user_monitor.monitor.cookie_rotation import (
    pick_round_robin,
    select_rotation_candidates,
)
from douyin_user_monitor.monitor.errors import sanitize_public_error
from douyin_user_monitor.monitor.timeutils import cn_now


def _serialized_mutation(method):
    """Keep runtime Cookie changes and their pool commits in one transaction."""
    @wraps(method)
    async def locked(self, *args, **kwargs):
        async with self._mutation_lock:
            return await method(self, *args, **kwargs)
    return locked


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
        self._mutation_lock = asyncio.Lock()

    async def list_items(self) -> List[Dict[str, Any]]:
        state = await self._load_bootstrapped()
        pool = ensure_cookie_pool(state["monitoring"])
        return [to_public_item(item) for item in pool["items"] if isinstance(item, dict)]

    @_serialized_mutation
    async def add_item(self, *, cookie: str, name: Optional[str] = None) -> Dict[str, Any]:
        cookie_text = (cookie or "").strip()
        if not cookie_text:
            raise ValueError("Cookie 不能为空")

        await self._ensure_bootstrapped()
        public: Dict[str, Any] = {}

        def _add(state: Dict[str, Any]) -> None:
            pool = ensure_cookie_pool(state["monitoring"])
            items: List[Dict[str, Any]] = pool["items"]
            auto_name = (name or "").strip() or f"cookie-{len(items) + 1}"
            item = build_cookie_item(cookie=cookie_text, name=auto_name, is_active=len(items) == 0)
            items.append(item)
            touch_pool_timestamps(state, pool, item)
            public.update(to_public_item(item))

        self._storage.update_state(_add)
        if public.get("is_active"):
            await self._crawler.update_cookie(cookie_text)
        return public

    @_serialized_mutation
    async def update_item(
        self,
        item_id: str,
        *,
        name: Optional[str] = None,
        cookie: Optional[str] = None,
    ) -> Dict[str, Any]:
        await self._ensure_bootstrapped()
        active_cookie_changed: List[str] = []

        def _update(state: Dict[str, Any], pool: Dict[str, Any], item: Dict[str, Any]) -> None:
            if name is not None:
                text = name.strip()
                if not text:
                    raise ValueError("名称不能为空")
                item["name"] = text
            if cookie is not None:
                cookie_text = cookie.strip()
                if not cookie_text:
                    raise ValueError("Cookie 不能为空")
                item["cookie"] = cookie_text
                clear_cookie_check_fields(item)
                if item.get("is_active"):
                    active_cookie_changed.append(cookie_text)
            touch_pool_timestamps(state, pool, item)

        public = self._update_pool_item(item_id, _update)
        if active_cookie_changed:
            await self._crawler.update_cookie(active_cookie_changed[0])
        return public

    @_serialized_mutation
    async def delete_item(self, item_id: str) -> Dict[str, Any]:
        await self._ensure_bootstrapped()

        def _delete(state: Dict[str, Any], pool: Dict[str, Any], item: Dict[str, Any]) -> None:
            if item.get("is_active"):
                raise ValueError("不能删除当前启用的 Cookie，请先启用其他条目")
            pool["items"] = [row for row in pool["items"] if str(row.get("id") or "") != item_id]
            pool["updated_at"] = cn_now()
            state["monitoring"]["updated_at"] = pool["updated_at"]

        return self._update_pool_item(item_id, _delete)

    @_serialized_mutation
    async def activate(self, item_id: str) -> Dict[str, Any]:
        await self._ensure_bootstrapped()
        state = self._storage.load_state()
        item = find_item(ensure_cookie_pool(state["monitoring"])["items"], item_id)
        if item is None:
            raise KeyError("Cookie 不存在")

        cookie_text = str(item.get("cookie") or "").strip()
        if not cookie_text:
            raise ValueError("Cookie 内容为空，无法启用")

        await self._crawler.update_cookie(cookie_text)

        def _activate(state: Dict[str, Any], pool: Dict[str, Any], item: Dict[str, Any]) -> None:
            now = cn_now()
            for row in pool["items"]:
                if not isinstance(row, dict):
                    continue
                active = str(row.get("id") or "") == item_id
                row["is_active"] = active
                if active:
                    row["updated_at"] = now
            pool["updated_at"] = now
            state["monitoring"]["updated_at"] = now

        return self._update_pool_item(item_id, _activate)

    async def check_item(self, item_id: str) -> Dict[str, Any]:
        state = await self._load_bootstrapped()
        item = find_item(ensure_cookie_pool(state["monitoring"])["items"], item_id)
        if item is None:
            raise KeyError("Cookie 不存在")

        cookie_text = str(item.get("cookie") or "").strip()
        if not cookie_text:
            raise ValueError("Cookie 内容为空，无法测活")

        try:
            async with self._crawler.temporary_cookie(cookie_text):
                result = await self._liveness.run_check(state["users"], force_probe=True)
        except Exception as exc:  # noqa: BLE001
            return self._commit_check_failure(item_id, sanitize_public_error(exc))
        return await self._commit_check_result(item_id, result, checked_cookie=cookie_text)

    def _commit_check_failure(self, item_id: str, safe_err: str) -> Dict[str, Any]:
        def _fail(state: Dict[str, Any], pool: Dict[str, Any], item: Dict[str, Any]) -> None:
            now = cn_now()
            item["last_error"] = safe_err
            item["status"] = "unknown"
            item["reason"] = f"测活失败: {safe_err}"
            item["last_check_at"] = now
            item["updated_at"] = now
            pool["updated_at"] = now
            state["monitoring"]["updated_at"] = now

        return self._update_pool_item(item_id, _fail)

    @_serialized_mutation
    async def _commit_check_result(
        self, item_id: str, result: Any, *, checked_cookie: str | None = None,
    ) -> Dict[str, Any]:
        current = find_item(ensure_cookie_pool(self._storage.load_state()["monitoring"])["items"], item_id)
        if current is None:
            raise KeyError("Cookie 不存在")
        if checked_cookie is not None and current.get("cookie") != checked_cookie:
            return to_public_item(current)
        if result.status == STATUS_EXPIRED:
            return await self._remove_expired_item(item_id, result)

        def _apply(state: Dict[str, Any], pool: Dict[str, Any], item: Dict[str, Any]) -> None:
            apply_liveness_to_item(item, result)
            if item.get("is_active"):
                prev = state["monitoring"].get("cookie_liveness")
                state["monitoring"]["cookie_liveness"] = liveness_payload_from_result(
                    result,
                    previous=prev if isinstance(prev, dict) else None,
                )
            pool["updated_at"] = item["updated_at"]
            state["monitoring"]["updated_at"] = item["updated_at"]

        return self._update_pool_item(item_id, _apply)

    async def _remove_expired_item(self, item_id: str, result: Any) -> Dict[str, Any]:
        state = self._storage.load_state()
        items = ensure_cookie_pool(state["monitoring"])["items"]
        item = find_item(items, item_id)
        if item is None:
            raise KeyError("Cookie 不存在")
        remaining = [row for row in items if isinstance(row, dict) and str(row.get("id") or "") != item_id]
        candidate = None
        if item.get("is_active"):
            candidate = next((row for row in remaining if row.get("status") != STATUS_EXPIRED), None)
            # Keep the item until persistence succeeds, so failures cannot masquerade as removal.
            if candidate is not None:
                await self._crawler.update_cookie(str(candidate.get("cookie") or "").strip())
            else:
                await self._crawler.clear_cookie()

        removed = {**to_public_item(item), "status": result.status, "reason": result.reason}
        was_active = bool(item.get("is_active"))
        candidate_id = str(candidate.get("id") or "") if candidate is not None else None

        def _remove(locked: Dict[str, Any]) -> None:
            pool = ensure_cookie_pool(locked["monitoring"])
            pool["items"] = [
                row for row in pool["items"] if isinstance(row, dict) and str(row.get("id") or "") != item_id
            ]
            if was_active:
                for row in pool["items"]:
                    row["is_active"] = str(row.get("id") or "") == candidate_id
            now = cn_now()
            pool["updated_at"] = now
            locked["monitoring"]["updated_at"] = now

        self._storage.update_state(_remove)
        return removed

    def _update_pool_item(
        self,
        item_id: str,
        mutate: Callable[[Dict[str, Any], Dict[str, Any], Dict[str, Any]], None],
    ) -> Dict[str, Any]:
        """锁内定位条目并提交纯状态变更；条目不存在时不写盘直接抛错。"""
        public: Dict[str, Any] = {}

        def _apply(state: Dict[str, Any]) -> None:
            pool = ensure_cookie_pool(state["monitoring"])
            item = find_item(pool["items"], item_id)
            if item is None:
                raise KeyError("Cookie 不存在")
            mutate(state, pool, item)
            public.update(to_public_item(item))

        self._storage.update_state(_apply)
        return public

    def rotation_enabled(self) -> bool:
        return self._rotation_enabled

    @asynccontextmanager
    async def rotation_session(self) -> AsyncIterator[Optional[Dict[str, Any]]]:
        """Pick next pool cookie and apply it for the wrapped sync work."""
        if not self._rotation_enabled:
            yield None
            return

        async with AsyncExitStack() as cookie_scope:
            item_id, public_item = await self._enter_rotation(cookie_scope)
            if public_item is None:
                yield None
                return
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

    @_serialized_mutation
    async def _enter_rotation(self, cookie_scope: AsyncExitStack) -> tuple[str, Optional[Dict[str, Any]]]:
        await self._ensure_bootstrapped()
        state = self._storage.load_state()
        pool = ensure_cookie_pool(state["monitoring"])
        items = [row for row in pool["items"] if isinstance(row, dict)]
        candidates = select_rotation_candidates(items, skip_expired=self._rotation_skip_expired)
        chosen, next_cursor = pick_round_robin(candidates, cursor=int(pool.get("rotation_cursor") or 0))
        if chosen is None:
            return "", None
        cookie_text = str(chosen.get("cookie") or "").strip()
        if not cookie_text:
            return "", None
        item_id = str(chosen.get("id") or "")
        public_item = begin_rotation_use(self._storage, item_id=item_id, next_cursor=next_cursor)
        if public_item is not None:
            # Select and enter before removals can clear this Cookie; never hold the pool lock over sync work.
            await cookie_scope.enter_async_context(self._crawler.temporary_cookie(cookie_text))
        return item_id, public_item

    async def _load_bootstrapped(self) -> Dict[str, Any]:
        state = self._storage.load_state()
        if ensure_cookie_pool(state["monitoring"])["items"]:
            return state
        async with self._mutation_lock:
            await self._ensure_bootstrapped()
            return self._storage.load_state()

    async def _ensure_bootstrapped(self) -> None:
        """池为空时用 crawler 当前 Cookie 播种；提交走锁内 RMW 防重复播种。"""
        state = self._storage.load_state()
        if ensure_cookie_pool(state.setdefault("monitoring", {}))["items"]:
            return

        raw = (await self._crawler.get_cookie()).strip()
        if not raw:
            return

        def _seed(locked: Dict[str, Any]) -> None:
            monitoring = locked.setdefault("monitoring", {})
            pool = ensure_cookie_pool(monitoring)
            if pool["items"]:
                return
            pool["items"] = [build_cookie_item(cookie=raw, name=DEFAULT_COOKIE_NAME, is_active=True)]
            pool["updated_at"] = cn_now()
            monitoring["updated_at"] = pool["updated_at"]

        self._storage.update_state(_seed)
