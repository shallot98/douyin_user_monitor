from __future__ import annotations

import unittest
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from douyin_user_monitor.monitor.cookie_liveness import CookieLivenessConfig
from douyin_user_monitor.monitor.cookie_liveness_service import CookieLivenessService
from douyin_user_monitor.monitor.cookie_pool import CookiePoolService
from douyin_user_monitor.monitor.cookie_rotation import (
    apply_rotation_outcome,
    is_auth_like_error,
    pick_round_robin,
    select_rotation_candidates,
)
from douyin_user_monitor.monitor.errors import sanitize_public_error


class SelectCandidatesTests(unittest.TestCase):
    def test_skips_expired_when_others_exist(self):
        items = [
            {"id": "a", "cookie": "c1", "status": "expired"},
            {"id": "b", "cookie": "c2", "status": "healthy"},
            {"id": "c", "cookie": "c3", "status": None},
        ]
        selected = select_rotation_candidates(items, skip_expired=True)
        self.assertEqual([x["id"] for x in selected], ["b", "c"])

    def test_falls_back_when_all_expired(self):
        items = [
            {"id": "a", "cookie": "c1", "status": "expired"},
            {"id": "b", "cookie": "c2", "status": "expired"},
        ]
        selected = select_rotation_candidates(items, skip_expired=True)
        self.assertEqual(len(selected), 2)

    def test_keeps_only_healthy_when_single_non_expired(self):
        items = [
            {"id": "a", "cookie": "c1", "status": "expired"},
            {"id": "b", "cookie": "c2", "status": "healthy"},
        ]
        selected = select_rotation_candidates(items, skip_expired=True)
        self.assertEqual([x["id"] for x in selected], ["b"])

    def test_round_robin_advances(self):
        cands = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        first, cur = pick_round_robin(cands, cursor=0)
        second, cur2 = pick_round_robin(cands, cursor=cur)
        third, cur3 = pick_round_robin(cands, cursor=cur2)
        self.assertEqual(first["id"], "a")
        self.assertEqual(second["id"], "b")
        self.assertEqual(third["id"], "c")
        self.assertEqual(cur3, 0)


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
    def __init__(self):
        self.cookie = ""
        self.seen: List[str] = []

    async def get_cookie(self) -> str:
        return self.cookie

    async def update_cookie(self, cookie: str) -> None:
        self.cookie = cookie

    @asynccontextmanager
    async def temporary_cookie(self, cookie: str):
        self.seen.append(cookie)
        old = self.cookie
        self.cookie = cookie
        try:
            yield
        finally:
            self.cookie = old


class FakeProbe:
    async def fetch_user_post_videos(self, sec_user_id, max_cursor, count):
        return {"aweme_list": [{"create_time": 1}]}



class RotationOutcomeTests(unittest.TestCase):
    def test_auth_marker_detection(self):
        self.assertTrue(is_auth_like_error("login required"))
        self.assertTrue(is_auth_like_error("auth expired"))
        self.assertTrue(is_auth_like_error("HTTP 403 forbidden"))
        self.assertFalse(is_auth_like_error("timeout after 30s"))
        # Wide substrings that previously false-expired healthy cookies
        self.assertFalse(is_auth_like_error("connection reset, Set-Cookie missing"))
        self.assertFalse(is_auth_like_error("session read timeout"))
        self.assertFalse(is_auth_like_error("cert expired"))
        self.assertFalse(is_auth_like_error("failed to parse cookie jar"))

    def test_numeric_id_not_treated_as_auth(self):
        # Bare 401/403 inside IDs or timestamps must not trigger auth failure.
        self.assertFalse(
            is_auth_like_error("获取作品详情失败: aweme_id=7401234567 请求超时")
        )
        self.assertFalse(is_auth_like_error("获取作品详情失败: aweme_id=7403987654"))
        self.assertFalse(is_auth_like_error("create_time=1734014013 解析失败"))

    def test_real_typed_auth_errors_detected(self):
        # Text produced by base_crawler for 401/403 typed errors.
        self.assertTrue(is_auth_like_error("HTTP Status Code 401"))
        self.assertTrue(is_auth_like_error("HTTP Status Code 403"))

    def test_success_resets_streak_and_marks_healthy(self):
        item = {"status": "expired", "auth_fail_streak": 2, "last_error": "x"}
        apply_rotation_outcome(item, success=True, error_text=None, now="t1")
        self.assertEqual(item["status"], "healthy")
        self.assertEqual(item["auth_fail_streak"], 0)
        self.assertIsNone(item["last_error"])
        self.assertEqual(item["reason"], "轮换使用成功")

    def test_auth_failures_escalate_to_expired(self):
        item = {"status": "healthy", "auth_fail_streak": 0}
        apply_rotation_outcome(
            item, success=False, error_text="login required", now="t1", auth_fail_limit=3
        )
        self.assertEqual(item["status"], "unknown")
        self.assertEqual(item["auth_fail_streak"], 1)
        apply_rotation_outcome(
            item, success=False, error_text="auth expired", now="t2", auth_fail_limit=3
        )
        apply_rotation_outcome(
            item, success=False, error_text="session invalid", now="t3", auth_fail_limit=3
        )
        self.assertEqual(item["status"], "expired")
        self.assertEqual(item["auth_fail_streak"], 3)

    def test_non_auth_error_keeps_status(self):
        item = {"status": "healthy", "auth_fail_streak": 2, "reason": "ok"}
        apply_rotation_outcome(item, success=False, error_text="timeout after 30s", now="t1")
        self.assertEqual(item["status"], "healthy")
        self.assertEqual(item["auth_fail_streak"], 0)
        self.assertIn("timeout", item["last_error"])

    def test_non_auth_error_resets_auth_streak(self):
        item = {"status": "healthy", "auth_fail_streak": 1}
        apply_rotation_outcome(item, success=False, error_text="login required", now="t1")
        self.assertEqual(item["auth_fail_streak"], 2)
        self.assertEqual(item["status"], "unknown")
        apply_rotation_outcome(item, success=False, error_text="timeout after 30s", now="t2")
        self.assertEqual(item["auth_fail_streak"], 0)
        # Non-auth keeps prior status (unknown), only clears consecutive streak.
        self.assertEqual(item["status"], "unknown")


class RotationSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_rotates_across_two_cookies(self):
        storage = MemStorage()
        runtime = FakeRuntime()
        liveness = CookieLivenessService(
            crawler=FakeProbe(),
            config=CookieLivenessConfig(
                enabled=True,
                interval_hours=6,
                stale_days=7,
                sample_user_count=3,
                min_samples=1,
                alert_cooldown_hours=12,
            ),
        )
        service = CookiePoolService(
            storage=storage,
            crawler=runtime,
            liveness_service=liveness,
            rotation_enabled=True,
            rotation_skip_expired=True,
        )
        await service.add_item(cookie="cookie-aaa-1111111111111111", name="A")
        await service.add_item(cookie="cookie-bbb-2222222222222222", name="B")

        used = []
        for _ in range(4):
            async with service.rotation_session() as selected:
                self.assertIsNotNone(selected)
                used.append(selected["name"])
                self.assertIn(runtime.cookie, ("cookie-aaa-1111111111111111", "cookie-bbb-2222222222222222"))

        self.assertEqual(used, ["A", "B", "A", "B"])
        items = await service.list_items()
        counts = {item["name"]: item["use_count"] for item in items}
        self.assertEqual(counts["A"], 2)
        self.assertEqual(counts["B"], 2)

    async def test_no_rotation_with_single_cookie(self):
        storage = MemStorage()
        runtime = FakeRuntime()
        liveness = CookieLivenessService(
            crawler=FakeProbe(),
            config=CookieLivenessConfig(
                enabled=True,
                interval_hours=6,
                stale_days=7,
                sample_user_count=3,
                min_samples=1,
                alert_cooldown_hours=12,
            ),
        )
        service = CookiePoolService(
            storage=storage,
            crawler=runtime,
            liveness_service=liveness,
            rotation_enabled=True,
        )
        await service.add_item(cookie="cookie-only-111111111111111", name="only")
        async with service.rotation_session() as selected:
            self.assertIsNone(selected)

    def _two_cookie_service(self):
        storage = MemStorage()
        runtime = FakeRuntime()
        liveness = CookieLivenessService(
            crawler=FakeProbe(),
            config=CookieLivenessConfig(
                enabled=True,
                interval_hours=6,
                stale_days=7,
                sample_user_count=3,
                min_samples=1,
                alert_cooldown_hours=12,
            ),
        )
        service = CookiePoolService(
            storage=storage,
            crawler=runtime,
            liveness_service=liveness,
            rotation_enabled=True,
            rotation_skip_expired=True,
        )
        return service

    async def test_rotation_success_marks_cookie_healthy(self):
        service = self._two_cookie_service()
        await service.add_item(cookie="cookie-aaa-1111111111111111", name="A")
        await service.add_item(cookie="cookie-bbb-2222222222222222", name="B")
        async with service.rotation_session() as selected:
            self.assertIsNotNone(selected)
        items = await service.list_items()
        used = [item for item in items if item["use_count"] > 0]
        self.assertEqual(len(used), 1)
        self.assertEqual(used[0]["status"], "healthy")
        self.assertEqual(used[0]["reason"], "轮换使用成功")
        self.assertEqual(used[0]["auth_fail_streak"], 0)

    async def test_rotation_auth_failures_mark_expired(self):
        storage = MemStorage()
        runtime = FakeRuntime()
        liveness = CookieLivenessService(
            crawler=FakeProbe(),
            config=CookieLivenessConfig(
                enabled=True,
                interval_hours=6,
                stale_days=7,
                sample_user_count=3,
                min_samples=1,
                alert_cooldown_hours=12,
            ),
        )
        # Keep expired cookies in rotation so both can accumulate fail streaks.
        service = CookiePoolService(
            storage=storage,
            crawler=runtime,
            liveness_service=liveness,
            rotation_enabled=True,
            rotation_skip_expired=False,
        )
        await service.add_item(cookie="cookie-aaa-1111111111111111", name="A")
        await service.add_item(cookie="cookie-bbb-2222222222222222", name="B")
        for _ in range(6):
            with self.assertRaises(RuntimeError):
                async with service.rotation_session() as selected:
                    self.assertIsNotNone(selected)
                    raise RuntimeError("login required")
        items = await service.list_items()
        self.assertTrue(all(item["status"] == "expired" for item in items))
        self.assertTrue(all(item["auth_fail_streak"] >= 3 for item in items))


class SanitizeErrorTests(unittest.TestCase):
    def test_truncates_long_error(self):
        msg = "x" * 500
        out = sanitize_public_error(RuntimeError(msg), limit=50)
        self.assertEqual(len(out), 53)
        self.assertTrue(out.endswith("..."))


if __name__ == "__main__":
    unittest.main()
