from __future__ import annotations

import asyncio
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

from douyin_user_monitor.monitor.cookie_liveness import (
    STATUS_HEALTHY,
    CookieLivenessConfig,
)
from douyin_user_monitor.monitor.cookie_liveness_service import CookieLivenessService
from douyin_user_monitor.monitor.cookie_pool import (
    CookiePoolService,
    to_public_item,
)
from douyin_user_monitor.monitor.cookie_pool_items import mask_cookie
from douyin_user_monitor.monitor.profile_parser import ACCOUNT_STATUS_NORMAL

CHINA_TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 7, 19, 12, 0, 0, tzinfo=CHINA_TZ)
NOW_TS = int(NOW.timestamp())
DAY = 86400


class MemStorage:
    def __init__(self, state: Optional[Dict[str, Any]] = None):
        self.state = state or {"users": [], "monitoring": {}}

    def load_state(self) -> Dict[str, Any]:
        return self.state

    def save_state(self, state: Dict[str, Any]) -> None:
        self.state = state

    def update_state(self, mutator):
        mutator(self.state)
        return self.state


class FakeRuntime:
    def __init__(self, cookie: str = ""):
        self.cookie = cookie
        self.updates: List[str] = []

    async def get_cookie(self) -> str:
        return self.cookie

    async def update_cookie(self, cookie: str) -> None:
        self.cookie = cookie
        self.updates.append(cookie)

    async def clear_cookie(self) -> None:
        self.cookie = ""
        self.updates.append("")

    @asynccontextmanager
    async def temporary_cookie(self, cookie: str):
        old = self.cookie
        self.cookie = cookie
        try:
            yield
        finally:
            self.cookie = old


class FakeCrawler:
    def __init__(self, mapping: Dict[str, Any]):
        self.mapping = mapping

    async def fetch_user_post_videos(self, sec_user_id: str, max_cursor: int, count: int):
        _ = max_cursor, count
        value = self.mapping[sec_user_id]
        if isinstance(value, Exception):
            raise value
        return value


def _users(n: int = 3) -> List[Dict[str, Any]]:
    return [
        {
            "id": f"u{i}",
            "nickname": f"n{i}",
            "enabled": True,
            "account_status": ACCOUNT_STATUS_NORMAL,
            "sec_user_id": f"sec-{i}",
        }
        for i in range(1, n + 1)
    ]


def _liveness(crawler: FakeCrawler) -> CookieLivenessService:
    return CookieLivenessService(
        crawler=crawler,
        config=CookieLivenessConfig(
            enabled=True,
            interval_hours=6,
            stale_days=7,
            sample_user_count=5,
            min_samples=3,
            alert_cooldown_hours=12,
        ),
        now_provider=lambda: NOW,
    )


class MaskCookieTests(unittest.TestCase):
    def test_mask_hides_middle(self):
        raw = "a" * 40 + "sessionid=secret-value-here" + "b" * 20
        preview = mask_cookie(raw)
        self.assertNotIn("secret-value-here", preview)
        self.assertIn("len=", preview)

    def test_public_item_has_no_raw_cookie(self):
        item = {
            "id": "1",
            "name": "x",
            "cookie": "sessionid=super-secret-cookie-value-123456",
            "is_active": True,
            "samples_used": 0,
        }
        public = to_public_item(item)
        self.assertNotIn("cookie", public)
        self.assertIn("cookie_preview", public)
        self.assertNotIn("super-secret-cookie-value-123456", public["cookie_preview"])


class CookiePoolServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_bootstrap_from_crawler_cookie(self):
        storage = MemStorage()
        runtime = FakeRuntime(cookie="ttwid=abc; sessionid=from-yaml")
        service = CookiePoolService(
            storage=storage,
            crawler=runtime,
            liveness_service=_liveness(FakeCrawler({})),
        )
        items = await service.list_items()
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["is_active"])
        self.assertEqual(items[0]["name"], "默认 Cookie")
        self.assertNotIn("sessionid=from-yaml", str(items[0]))

    async def test_add_two_and_delete_active_rejected(self):
        storage = MemStorage()
        runtime = FakeRuntime(cookie="")
        service = CookiePoolService(
            storage=storage,
            crawler=runtime,
            liveness_service=_liveness(FakeCrawler({})),
        )
        first = await service.add_item(cookie="cookie-one-aaaaaaaaaaaaaaaa", name="A")
        second = await service.add_item(cookie="cookie-two-bbbbbbbbbbbbbbbb", name="B")
        self.assertTrue(first["is_active"])
        self.assertFalse(second["is_active"])
        self.assertEqual(runtime.updates, ["cookie-one-aaaaaaaaaaaaaaaa"])

        with self.assertRaises(ValueError):
            await service.delete_item(first["id"])

        deleted = await service.delete_item(second["id"])
        self.assertEqual(deleted["id"], second["id"])
        remaining = await service.list_items()
        self.assertEqual(len(remaining), 1)

    async def test_activate_switches_crawler_cookie(self):
        storage = MemStorage()
        runtime = FakeRuntime(cookie="")
        service = CookiePoolService(
            storage=storage,
            crawler=runtime,
            liveness_service=_liveness(FakeCrawler({})),
        )
        a = await service.add_item(cookie="cookie-aaa-1111111111111111", name="A")
        b = await service.add_item(cookie="cookie-bbb-2222222222222222", name="B")
        result = await service.activate(b["id"])
        self.assertTrue(result["is_active"])
        self.assertEqual(runtime.cookie, "cookie-bbb-2222222222222222")
        items = await service.list_items()
        active_ids = [item["id"] for item in items if item["is_active"]]
        self.assertEqual(active_ids, [b["id"]])
        self.assertNotEqual(active_ids[0], a["id"])

    async def test_check_item_writes_status(self):
        storage = MemStorage(
            {
                "users": _users(3),
                "monitoring": {},
            }
        )
        runtime = FakeRuntime(cookie="")
        crawler = FakeCrawler(
            {
                "sec-1": {"aweme_list": [{"create_time": NOW_TS - DAY}]},
                "sec-2": {"aweme_list": [{"create_time": NOW_TS - 2 * DAY}]},
                "sec-3": {"aweme_list": [{"create_time": NOW_TS - 3 * DAY}]},
            }
        )
        service = CookiePoolService(
            storage=storage,
            crawler=runtime,
            liveness_service=_liveness(crawler),
        )
        item = await service.add_item(cookie="cookie-live-cccccccccccccccc", name="live")
        checked = await service.check_item(item["id"])
        self.assertEqual(checked["status"], STATUS_HEALTHY)
        self.assertIsNotNone(checked["last_check_at"])
        self.assertIn("cookie_liveness", storage.state["monitoring"])
        self.assertEqual(storage.state["monitoring"]["cookie_liveness"]["status"], STATUS_HEALTHY)

    async def test_check_expired_cookie_removes_it_and_switches_active_cookie(self):
        storage = MemStorage({"users": _users(3), "monitoring": {}})
        runtime = FakeRuntime(cookie="")
        crawler = FakeCrawler({
            "sec-1": RuntimeError("auth expired"),
            "sec-2": RuntimeError("auth expired"),
            "sec-3": RuntimeError("auth expired"),
        })
        service = CookiePoolService(
            storage=storage,
            crawler=runtime,
            liveness_service=_liveness(crawler),
        )
        expired = await service.add_item(cookie="cookie-expired-aaaaaaaa", name="expired")
        live = await service.add_item(cookie="cookie-live-bbbbbbbb", name="live")

        result = await service.check_item(expired["id"])

        self.assertEqual(result["status"], "expired")
        items = await service.list_items()
        self.assertEqual([item["id"] for item in items], [live["id"]])
        self.assertTrue(items[0]["is_active"])
        self.assertEqual(runtime.cookie, "cookie-live-bbbbbbbb")


class ExpiredCookieCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.storage = MemStorage({"users": _users(3), "monitoring": {}})
        self.runtime = FakeRuntime()
        self.liveness = _liveness(FakeCrawler({f"sec-{i}": RuntimeError("auth expired") for i in range(1, 4)}))
        self.service = CookiePoolService(storage=self.storage, crawler=self.runtime, liveness_service=self.liveness)
        self.item = await self.service.add_item(cookie="expired-test-cookie", name="expired")

    async def test_only_expired_cookie_stays_removed_after_list_rotation_and_restart(self):
        await self.service.check_item(self.item["id"])
        self.assertEqual(self.runtime.cookie, "")
        self.assertEqual(await self.service.list_items(), [])
        async with self.service.rotation_session() as selected:
            self.assertIsNone(selected)
            self.assertEqual(self.runtime.cookie, "")
        restarted = CookiePoolService(storage=self.storage, crawler=self.runtime, liveness_service=self.liveness)
        self.assertEqual(await restarted.list_items(), [])
        fresh = await restarted.add_item(cookie="replacement-test-cookie")
        self.assertTrue(fresh["is_active"])
        self.assertEqual(self.runtime.cookie, "replacement-test-cookie")

    async def test_failed_clear_keeps_item_and_surfaces_error(self):
        self.runtime.clear_cookie = AsyncMock(side_effect=OSError("configuration not writable"))
        with self.assertRaisesRegex(OSError, "configuration not writable"):
            await self.service.check_item(self.item["id"])
        self.assertEqual([row["id"] for row in await self.service.list_items()], [self.item["id"]])
        self.assertEqual(self.runtime.cookie, "expired-test-cookie")

    async def test_new_cookie_added_while_clear_waits_is_not_lost_or_overwritten(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def clear():
            entered.set()
            await release.wait()
            self.runtime.cookie = ""

        self.runtime.clear_cookie = clear
        check = asyncio.create_task(self.service.check_item(self.item["id"]))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            added = asyncio.create_task(self.service.add_item(cookie="concurrent-new-cookie"))
            await asyncio.sleep(0)
            # The expired entry stays present until its persisted runtime value is cleared.
            self.assertEqual(len(self.storage.state["monitoring"]["cookie_pool"]["items"]), 1)
            release.set()
            await check
            fresh = await added
            self.assertEqual(self.runtime.cookie, "concurrent-new-cookie")
            self.assertEqual([row["id"] for row in await self.service.list_items()], [fresh["id"]])
            self.assertTrue(fresh["is_active"])
        finally:
            release.set()
            await check

    async def test_stale_check_does_not_remove_cookie_replaced_during_probe(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original_check = self.liveness.run_check

        async def delayed(*args, **kwargs):
            result = await original_check(*args, **kwargs)
            entered.set()
            await release.wait()
            return result

        self.liveness.run_check = delayed
        check = asyncio.create_task(self.service.check_item(self.item["id"]))
        await entered.wait()
        await self.service.update_item(self.item["id"], cookie="new-value-after-probe")
        release.set()
        await check
        row = (await self.service.list_items())[0]
        self.assertEqual(row["id"], self.item["id"])
        self.assertIsNone(row["status"])

    async def test_failed_replacement_keeps_the_active_item(self):
        await self.service.add_item(cookie="replacement-test-cookie")
        self.runtime.update_cookie = AsyncMock(side_effect=OSError("replacement write failed"))
        with self.assertRaisesRegex(OSError, "replacement write failed"):
            await self.service.check_item(self.item["id"])
        items = await self.service.list_items()
        self.assertEqual(len(items), 2)
        self.assertEqual([row["id"] for row in items if row["is_active"]], [self.item["id"]])
        self.assertEqual(self.runtime.cookie, "expired-test-cookie")

    async def test_remaining_expired_items_are_not_activated_as_replacements(self):
        other = await self.service.add_item(cookie="also-expired-cookie")
        self.storage.state["monitoring"]["cookie_pool"]["items"][1]["status"] = "expired"
        await self.service.check_item(self.item["id"])
        self.assertEqual(self.runtime.cookie, "")
        items = await self.service.list_items()
        self.assertEqual([row["id"] for row in items], [other["id"]])
        self.assertFalse(items[0]["is_active"])

    async def test_public_cookie_writes_still_reject_empty_values(self):
        with self.assertRaises(ValueError):
            await self.service.add_item(cookie=" ")
        with self.assertRaises(ValueError):
            await self.service.update_item(self.item["id"], cookie=" ")


if __name__ == "__main__":
    unittest.main()
