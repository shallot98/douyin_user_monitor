import json
import unittest
from collections import Counter
from unittest.mock import patch

from test_history_backfill import CountingNotifier, FakeDownloader

from douyin_user_monitor.monitor.history_sync import (
    HISTORY_SYNC_STATUS_COMPLETED,
    HISTORY_SYNC_STATUS_FAILED,
    HISTORY_SYNC_STATUS_PAUSED,
    HISTORY_SYNC_STATUS_PENDING,
    build_history_sync_state,
    normalize_history_sync_state,
)
from douyin_user_monitor.monitor.service import MonitorService
from douyin_user_monitor.monitor.user_sync import UserSyncService


class RetryCrawler:
    def __init__(self):
        self.failed = {"bad"}
        self.pages = []
        self.details = []
        self.fail_page = False

    async def fetch_user_post_videos(self, sec_user_id, max_cursor, count):
        self.pages.append(max_cursor)
        if self.fail_page:
            raise OSError("page unavailable")
        return {"aweme_list": [{"aweme_id": "bad" if max_cursor == 0 else "good", "create_time": 1}],
                "max_cursor": 10 if max_cursor == 0 else 20, "has_more": max_cursor == 0}

    async def fetch_one_video(self, aweme_id):
        self.details.append(aweme_id)
        if aweme_id in self.failed:
            raise OSError("permanent missing media")
        return {"aweme_detail": {"aweme_id": aweme_id, "create_time": 1}}

    async def get_douyin_headers(self):
        return {"headers": {"User-Agent": "test"}}


def new_user():
    return {"id": "user", "sec_user_id": "sec", "nickname": "user", "downloaded_count": 0,
            "download_records": [], "downloaded_aweme_ids": [],
            "history_sync": build_history_sync_state(status=HISTORY_SYNC_STATUS_PENDING)}


class HistoryRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.crawler = RetryCrawler()
        self.notifier = CountingNotifier()
        self.service = UserSyncService(self.crawler, FakeDownloader(), self.notifier)
        self.user = new_user()

    async def test_failed_work_does_not_block_later_pages_and_retries_after_restart(self):
        self.assertEqual(await self.service.sync_history_backfill_step(self.user), 0)
        self.assertEqual(self.user["history_sync"]["next_cursor"], 10)
        self.assertEqual(await self.service.sync_history_backfill_step(self.user), 1)
        history = self.user["history_sync"]
        self.assertEqual(self.crawler.pages, [0, 10])
        self.assertFalse(history["has_more"])
        self.assertEqual(history["status"], HISTORY_SYNC_STATUS_FAILED)
        self.assertIsNone(history["completed_at"])
        self.assertEqual([item["aweme_id"] for item in history["pending_awemes"]], ["bad"])
        self.assertIn("missing media", history["pending_awemes"][0]["error"])
        self.user = json.loads(json.dumps(self.user))
        normalize_history_sync_state(self.user)
        self.crawler.failed.clear()
        self.assertEqual(await self.service.sync_history_backfill_step(self.user), 1)
        self.assertEqual(self.crawler.pages, [0, 10])
        self.assertEqual(self.user["history_sync"]["status"], HISTORY_SYNC_STATUS_COMPLETED)
        self.assertEqual(self.user["history_sync"]["pending_awemes"], [])
        self.assertEqual(self.user["downloaded_count"], 2)
        self.assertEqual(self.user["history_sync"]["downloaded_items"], 2)
        self.assertEqual(self.user["history_sync"]["processed_pages"], 2)
        self.assertEqual(self.notifier.detected, [])
        self.assertEqual(self.notifier.completed, [])

    async def test_paused_pending_retries_survive_resume_without_reopening_exhausted_pages(self):
        await self.service.sync_history_backfill_step(self.user)
        await self.service.sync_history_backfill_step(self.user)
        self.user["history_sync"]["status"] = HISTORY_SYNC_STATUS_PAUSED
        before = len(self.crawler.details)
        self.assertEqual(await self.service.sync_history_backfill_step(self.user), 0)
        self.assertEqual(len(self.crawler.details), before)
        monitor = object.__new__(MonitorService)
        monitor._activate_history_sync(self.user, reset_completed=False)
        self.assertFalse(self.user["history_sync"]["has_more"])
        self.crawler.failed.clear()
        self.assertEqual(await self.service.sync_history_backfill_step(self.user), 1)
        self.assertEqual(self.crawler.pages, [0, 10])
        self.assertEqual(self.user["history_sync"]["status"], HISTORY_SYNC_STATUS_COMPLETED)

    async def test_retries_are_bounded_fair_and_remove_already_downloaded_work(self):
        ids = [f"failed-{index}" for index in range(7)]
        self.crawler.failed = set(ids)
        self.user["history_sync"].update(status=HISTORY_SYNC_STATUS_FAILED, has_more=False,
                                           pending_awemes=[{"aweme_id": item, "error": "old"} for item in ids])
        self.user["downloaded_aweme_ids"] = [ids[0]]
        self.user["downloaded_count"] = 1
        with patch("douyin_user_monitor.monitor.user_sync.HISTORY_RETRY_LIMIT", 2, create=True):
            for _ in range(3):
                before = len(self.crawler.details)
                self.assertEqual(await self.service.sync_history_backfill_step(self.user), 0)
                self.assertEqual(len(self.crawler.details) - before, 2)
        self.assertEqual(Counter(self.crawler.details), Counter(ids[1:]))
        self.assertEqual(self.crawler.pages, [])
        self.assertNotIn(ids[0], [item["aweme_id"] for item in self.user["history_sync"]["pending_awemes"]])

    async def test_page_failure_does_not_advance_cursor_or_discard_pending_work(self):
        await self.service.sync_history_backfill_step(self.user)
        before = json.loads(json.dumps(self.user["history_sync"]))
        self.crawler.fail_page = True
        with self.assertRaisesRegex(OSError, "page unavailable"):
            await self.service.sync_history_backfill_step(self.user)
        self.assertEqual(self.user["history_sync"], before)

    async def test_legacy_state_has_empty_retry_queue_and_finishes_normally(self):
        self.user["history_sync"].pop("pending_awemes", None)
        self.crawler.failed.clear()
        await self.service.sync_history_backfill_step(self.user)
        await self.service.sync_history_backfill_step(self.user)
        self.assertEqual(self.user["history_sync"].get("pending_awemes"), [])
        self.assertEqual(self.user["history_sync"]["status"], HISTORY_SYNC_STATUS_COMPLETED)


if __name__ == "__main__":
    unittest.main()
