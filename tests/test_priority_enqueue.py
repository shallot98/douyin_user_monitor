import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from douyin_user_monitor.monitor.history_sync import (
    HISTORY_SYNC_STATUS_PENDING,
    build_history_sync_state,
)
from douyin_user_monitor.monitor.notifier import NoopMonitorNotifier
from douyin_user_monitor.monitor.service import MonitorService
from douyin_user_monitor.monitor.storage import MonitorStorage


class FakeCrawler:
    async def handler_user_profile(self, sec_user_id: str):
        return {"user": {"nickname": f"昵称-{sec_user_id}", "avatar_thumb": {"url_list": []}}}

    async def get_sec_user_id(self, url: str) -> str:
        return url

    async def fetch_user_post_videos(self, sec_user_id: str, max_cursor: int, count: int):
        _ = sec_user_id, max_cursor, count
        return {"aweme_list": [], "has_more": 0, "max_cursor": 0}

    async def fetch_one_video(self, aweme_id: str):
        return {"aweme_detail": {"aweme_id": aweme_id}}

    async def get_douyin_headers(self):
        return {"headers": {"User-Agent": "test"}}


def _build_service(temp_path: Path) -> MonitorService:
    return MonitorService(
        crawler=FakeCrawler(),
        storage=MonitorStorage(temp_path / "monitor_users.json"),
        download_root=temp_path / "download",
        notifier=NoopMonitorNotifier(),
    )


def _seed_user(service: MonitorService, user_id: str, nickname: str) -> dict:
    state = service._storage.load_state()
    user = {
        "id": user_id,
        "sec_user_id": f"sec-{user_id}",
        "nickname": nickname,
        "enabled": True,
        "downloaded_count": 0,
        "downloaded_aweme_ids": [],
        "download_records": [],
        "history_sync": build_history_sync_state(status=HISTORY_SYNC_STATUS_PENDING),
    }
    state["users"].append(user)
    service._storage.save_state(state)
    return user


class PriorityEnqueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_enqueue_works_for_interval_and_coverage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = _build_service(Path(temp_dir))
            service._loop_task = asyncio.create_task(asyncio.sleep(3600))
            try:
                service._mode = "interval"
                service._enqueue_coverage_priority_user("user-a")
                self.assertEqual(service._coverage_priority_user_ids, ["user-a"])
                self.assertTrue(service._wake_event.is_set())

                service._mode = "coverage"
                service._enqueue_coverage_priority_user("user-b")
                self.assertEqual(service._coverage_priority_user_ids, ["user-a", "user-b"])
            finally:
                service._loop_task.cancel()
                try:
                    await service._loop_task
                except asyncio.CancelledError:
                    pass

    async def test_sleep_interruptible_returns_early_when_priority_enqueued(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = _build_service(Path(temp_dir))
            service._loop_task = asyncio.create_task(asyncio.sleep(3600))
            try:
                started = asyncio.get_running_loop().time()

                async def enqueue_soon():
                    await asyncio.sleep(0.05)
                    service._enqueue_coverage_priority_user("user-new")

                asyncio.create_task(enqueue_soon())
                woken = await service._sleep_interruptible(5.0)
                elapsed = asyncio.get_running_loop().time() - started

                self.assertTrue(woken)
                self.assertLess(elapsed, 1.0)
                self.assertEqual(service._coverage_priority_user_ids, ["user-new"])
            finally:
                service._loop_task.cancel()
                try:
                    await service._loop_task
                except asyncio.CancelledError:
                    pass

    async def test_insert_priority_user_to_front_of_pending(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = _build_service(Path(temp_dir))
            existing = _seed_user(service, "user-old", "旧用户")
            new_user = _seed_user(service, "user-new", "新用户")
            state = service._storage.load_state()
            pending = [existing, new_user]
            service._coverage_priority_user_ids = ["user-new"]

            service._insert_priority_users_into_pending(state, pending, processed_user_ids=set())

            self.assertEqual([user["id"] for user in pending], ["user-new", "user-old"])
            self.assertEqual(service._coverage_priority_user_ids, [])

    async def test_drain_priority_users_runs_sync_one_user(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = _build_service(Path(temp_dir))
            _seed_user(service, "user-new", "新用户")
            service._coverage_priority_user_ids = ["user-new"]
            service._active_coverage_state = None

            with patch.object(
                service._sync_service,
                "sync_one_user",
                new=AsyncMock(),
            ) as sync_mock:
                await service._drain_priority_users()

            self.assertEqual(sync_mock.await_count, 1)
            synced_user = sync_mock.await_args.args[0]
            self.assertEqual(synced_user["id"], "user-new")
            self.assertEqual(service._coverage_priority_user_ids, [])

    async def test_drain_skips_while_coverage_cycle_active(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = _build_service(Path(temp_dir))
            _seed_user(service, "user-new", "新用户")
            service._coverage_priority_user_ids = ["user-new"]
            service._active_coverage_state = {"users": []}

            with patch.object(
                service._sync_service,
                "sync_one_user",
                new=AsyncMock(),
            ) as sync_mock:
                await service._drain_priority_users()

            sync_mock.assert_not_awaited()
            self.assertEqual(service._coverage_priority_user_ids, ["user-new"])

    async def test_add_user_enqueues_priority_when_monitoring_running(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = _build_service(Path(temp_dir))
            service._loop_task = asyncio.create_task(asyncio.sleep(3600))
            try:
                result = await service.add_user_with_status("sec-added")
                user_id = result["user"]["id"]
                self.assertEqual(result["status"], "created")
                self.assertIn(user_id, service._coverage_priority_user_ids)
                self.assertTrue(service._wake_event.is_set())
            finally:
                service._loop_task.cancel()
                try:
                    await service._loop_task
                except asyncio.CancelledError:
                    pass


if __name__ == "__main__":
    unittest.main()
