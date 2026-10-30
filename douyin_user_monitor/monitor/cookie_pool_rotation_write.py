from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from douyin_user_monitor.monitor.cookie_pool_items import (
    ensure_cookie_pool,
    find_item,
    to_public_item,
)
from douyin_user_monitor.monitor.cookie_rotation import apply_rotation_outcome
from douyin_user_monitor.monitor.timeutils import cn_now


def mutate_pool_state(storage: Any, mutator: Callable[[Dict[str, Any]], None]) -> None:
    # storage 协议保证提供 update_state（锁内原子 RMW）；不做静默降级到无锁 load→save。
    storage.update_state(mutator)


def begin_rotation_use(
    storage: Any,
    *,
    item_id: str,
    next_cursor: int,
) -> Optional[Dict[str, Any]]:
    public: Dict[str, Any] = {}

    def mutator(state: Dict[str, Any]) -> None:
        pool = ensure_cookie_pool(state.setdefault("monitoring", {}))
        item = find_item(pool["items"], item_id)
        if item is None:
            return
        now = cn_now()
        item["use_count"] = int(item.get("use_count") or 0) + 1
        item["last_used_at"] = now
        item["updated_at"] = now
        pool["rotation_cursor"] = next_cursor
        pool["updated_at"] = now
        state["monitoring"]["updated_at"] = now
        public.clear()
        public.update(to_public_item(item))

    mutate_pool_state(storage, mutator)
    return public or None


def write_rotation_outcome(
    storage: Any,
    item_id: str,
    *,
    success: bool,
    error_text: Optional[str],
) -> None:
    if not item_id:
        return

    def mutator(state: Dict[str, Any]) -> None:
        pool = ensure_cookie_pool(state.setdefault("monitoring", {}))
        item = find_item(pool["items"], item_id)
        if item is None:
            return
        apply_rotation_outcome(
            item,
            success=success,
            error_text=error_text,
            now=cn_now(),
        )
        pool["updated_at"] = item["updated_at"]
        state["monitoring"]["updated_at"] = item["updated_at"]

    mutate_pool_state(storage, mutator)
