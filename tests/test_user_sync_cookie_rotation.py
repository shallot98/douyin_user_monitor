import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from douyin_user_monitor.monitor.cookie_pool import CookiePoolService
from douyin_user_monitor.monitor.cookie_pool_items import CookieRuntime
from douyin_user_monitor.monitor.crawler_protocol import MonitorCrawlerProtocol
from douyin_user_monitor.monitor.history_sync import (
    HISTORY_SYNC_STATUS_COMPLETED,
    HISTORY_SYNC_STATUS_PENDING,
    build_history_sync_state,
)
from douyin_user_monitor.monitor.notifier import NoopMonitorNotifier
from douyin_user_monitor.monitor.service import MonitorService
from douyin_user_monitor.monitor.storage import MonitorStorage
from douyin_user_monitor.monitor.user_sync import UserSyncService

POSTS_FORBIDDEN = "获取用户作品列表失败: HTTP Status Code 403"
EMPTY_PAGE = {"aweme_list": [], "has_more": 0, "max_cursor": 0}


def make_user(user_id="user-1", *, history_status=HISTORY_SYNC_STATUS_COMPLETED):
    return {
        "id": user_id,
        "sec_user_id": f"sec-{user_id}",
        "nickname": user_id,
        "enabled": True,
        "downloaded_count": 0,
        "downloaded_aweme_ids": [],
        "history_sync": build_history_sync_state(status=history_status),
    }


class UserSyncCookieRotationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.storage = MonitorStorage(self.root / "state.json")
        self.runtime = AsyncMock(spec=CookieRuntime)
        self.runtime.get_cookie.return_value = ""
        self.pool = CookiePoolService(
            storage=self.storage, crawler=self.runtime, liveness_service=None,
        )
        await self.pool.add_item(cookie="test-cookie-a", name="A")
        await self.pool.add_item(cookie="test-cookie-b", name="B")
        self.crawler = AsyncMock(spec=MonitorCrawlerProtocol)
        self.crawler.handler_user_profile.return_value = {"user": {"nickname": "test-user"}}
        self.crawler.fetch_user_post_videos.return_value = EMPTY_PAGE
        self.crawler.get_douyin_headers.return_value = {"headers": {}}
        self.downloader = AsyncMock()
        self.publisher = AsyncMock()
        self.sync = UserSyncService(
            crawler=self.crawler, downloader=self.downloader, notifier=NoopMonitorNotifier(),
            cookie_rotation_session=self.pool.rotation_session,
            cover_publisher=self.publisher,
        )

    async def test_posts_403_records_cookie_failure_and_user_summary(self):
        self.crawler.fetch_user_post_videos.side_effect = RuntimeError(POSTS_FORBIDDEN)
        user = make_user()

        summary = await self.sync.sync_users([user], gaps=[])

        first, second = await self.pool.list_items()
        self.assertEqual(first["status"], "unknown")
        self.assertEqual(first["auth_fail_streak"], 1)
        self.assertEqual(first["last_error"], POSTS_FORBIDDEN)
        self.assertEqual(second["use_count"], 0)
        self.assertEqual(user["last_error"], POSTS_FORBIDDEN)
        self.assertTrue(user["last_checked_at"])
        self.assertTrue(user["updated_at"])
        self.assertEqual(summary["checked_users"], 1)
        self.assertEqual(summary["downloaded_items"], 0)
        self.assertEqual(len(summary["errors"]), 1)
        self.assertEqual(summary["errors"][0]["error"], POSTS_FORBIDDEN)
        self.downloader.download_aweme_assets.assert_not_awaited()

    async def test_failed_user_does_not_stop_following_users(self):
        self.crawler.fetch_user_post_videos.side_effect = [RuntimeError(POSTS_FORBIDDEN), EMPTY_PAGE]
        users = [make_user("failed"), make_user("succeeded")]

        summary = await self.sync.sync_users(users, gaps=[])

        first, second = await self.pool.list_items()
        self.assertEqual(summary["checked_users"], 2)
        self.assertEqual(len(summary["errors"]), 1)
        self.assertEqual(users[0]["last_error"], POSTS_FORBIDDEN)
        self.assertIsNone(users[1]["last_error"])
        self.assertEqual(first["auth_fail_streak"], 1)
        self.assertEqual(second["status"], "healthy")
        self.assertEqual(second["auth_fail_streak"], 0)

    async def test_all_cookies_accumulate_failures_with_skip_expired_enabled(self):
        self.crawler.fetch_user_post_videos.side_effect = RuntimeError(POSTS_FORBIDDEN)

        summary = await self.sync.sync_users([make_user(f"user-{i}") for i in range(6)], gaps=[])

        items = await self.pool.list_items()
        self.assertEqual(len(summary["errors"]), 6)
        self.assertEqual([item["use_count"] for item in items], [3, 3])
        self.assertEqual([item["auth_fail_streak"] for item in items], [3, 3])
        self.assertEqual([item["status"] for item in items], ["expired", "expired"])

    async def test_success_clears_prior_auth_failure(self):
        self.crawler.fetch_user_post_videos.side_effect = [
            RuntimeError(POSTS_FORBIDDEN), EMPTY_PAGE, EMPTY_PAGE,
        ]

        await self.sync.sync_users([make_user(f"user-{i}") for i in range(3)], gaps=[])

        first, _ = await self.pool.list_items()
        self.assertEqual(first["use_count"], 2)
        self.assertEqual(first["status"], "healthy")
        self.assertEqual(first["auth_fail_streak"], 0)
        self.assertIsNone(first["last_error"])

    async def test_timeout_preserves_status_without_reporting_rotation_success(self):
        self.crawler.fetch_user_post_videos.side_effect = TimeoutError("upstream timed out")
        user = make_user()

        summary = await self.sync.sync_users([user], gaps=[])

        first, _ = await self.pool.list_items()
        self.assertIsNone(first["status"])
        self.assertEqual(first["auth_fail_streak"], 0)
        self.assertEqual(first["last_error"], "upstream timed out")
        self.assertEqual(user["last_error"], "upstream timed out")
        self.assertEqual(len(summary["errors"]), 1)

    async def test_history_403_keeps_completed_incremental_download_count(self):
        self.crawler.fetch_user_post_videos.side_effect = [
            {"aweme_list": [{"aweme_id": "new-post", "create_time": 1}]},
            RuntimeError(POSTS_FORBIDDEN),
        ]
        self.crawler.fetch_one_video.return_value = {
            "aweme_detail": {"aweme_id": "new-post", "create_time": 1},
        }
        self.downloader.download_aweme_assets.return_value = {
            "media_type": "video", "files": ["new-post.mp4"], "cover_file": "new-post.cover.jpg",
        }
        user = make_user(history_status=HISTORY_SYNC_STATUS_PENDING)

        summary = await self.sync.sync_users([user], gaps=[])

        first, _ = await self.pool.list_items()
        self.assertEqual(first["auth_fail_streak"], 1)
        self.assertEqual(user["last_error"], POSTS_FORBIDDEN)
        self.assertEqual(len(summary["errors"]), 1)
        self.assertEqual(summary["downloaded_items"], 1)
        self.assertEqual(user["downloaded_count"], 1)
        self.assertEqual(user["downloaded_aweme_ids"], ["new-post"])
        self.assertEqual(user["download_records"][0]["cover_file"], "new-post.cover.jpg")
        self.publisher.assert_awaited_once()

    async def test_manual_history_403_updates_cookie_before_persisting_user_error(self):
        user = make_user(history_status=HISTORY_SYNC_STATUS_PENDING)
        self.storage.update_state(lambda state: state["users"].append(user))
        service = MonitorService(
            crawler=self.crawler, storage=self.storage, download_root=self.root / "download",
            cookie_pool_service=self.pool,
        )
        self.crawler.fetch_user_post_videos.side_effect = RuntimeError(POSTS_FORBIDDEN)

        summary = await service.run_user_history_backfill_once(user["id"])

        first, _ = await self.pool.list_items()
        self.assertEqual(first["auth_fail_streak"], 1)
        self.assertEqual(first["last_error"], POSTS_FORBIDDEN)
        self.assertEqual(len(summary["errors"]), 1)
        saved_user = self.storage.load_state()["users"][0]
        self.assertEqual(saved_user["last_error"], POSTS_FORBIDDEN)
        self.assertEqual(saved_user["history_sync"]["next_cursor"], 0)


if __name__ == "__main__":
    unittest.main()
