import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from douyin_user_monitor.monitor.history_sync import (
    HISTORY_SYNC_STATUS_PENDING,
    build_history_sync_state,
)
from douyin_user_monitor.monitor.notifier import NoopMonitorNotifier
from douyin_user_monitor.monitor.service import (
    COVERAGE_PHASE_RUNNING,
    MonitorService,
)
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


def _seed_user(service: MonitorService, user_id: str) -> None:
    state = service._storage.load_state()
    state["users"].append(
        {
            "id": user_id,
            "sec_user_id": f"sec-{user_id}",
            "nickname": user_id,
            "enabled": True,
            "downloaded_count": 0,
            "downloaded_aweme_ids": [],
            "download_records": [],
            "history_sync": build_history_sync_state(status=HISTORY_SYNC_STATUS_PENDING),
        }
    )
    service._storage.save_state(state)


class MonitorResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_keeps_desired_running_and_progress(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = _build_service(Path(temp_dir))
            _seed_user(service, "user-1")
            _seed_user(service, "user-2")

            await service.start_monitoring(
                mode="coverage",
                interval_hours=1.0,
                coverage_hours=12.0,
            )
            state = service._storage.load_state()
            state["monitoring"]["coverage_progress"] = {
                "phase": COVERAGE_PHASE_RUNNING,
                "cycle_started_at": state["monitoring"]["updated_at"],
                "idle_until": None,
                "processed_user_ids": ["user-1"],
                "checked_users": 1,
                "downloaded_items": 0,
                "errors": [],
                "priority_user_ids": ["user-2"],
            }
            service._storage.save_state(state)
            service._coverage_priority_user_ids = ["user-2"]

            await service.shutdown()

            monitoring = service._storage.load_state()["monitoring"]
            self.assertFalse(monitoring["is_running"])
            self.assertTrue(monitoring["desired_running"])
            progress = monitoring["coverage_progress"]
            self.assertEqual(progress["phase"], COVERAGE_PHASE_RUNNING)
            self.assertEqual(progress["processed_user_ids"], ["user-1"])
            self.assertEqual(progress["priority_user_ids"], ["user-2"])

    async def test_auto_resume_restarts_loop_from_desired_running(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = _build_service(Path(temp_dir))
            service._was_running = True
            service._mode = "coverage"
            service._interval_hours = 1.0
            service._coverage_hours = 12.0

            with patch.object(service, "start_monitoring", new=AsyncMock()) as start_mock:
                await service.auto_resume()

            start_mock.assert_awaited_once()
            kwargs = start_mock.await_args.kwargs
            self.assertTrue(kwargs.get("resume"))

    async def test_stop_monitoring_clears_desired_running_and_progress(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = _build_service(Path(temp_dir))
            await service.start_monitoring(
                mode="coverage",
                interval_hours=1.0,
                coverage_hours=12.0,
            )
            state = service._storage.load_state()
            state["monitoring"]["coverage_progress"] = {
                "phase": COVERAGE_PHASE_RUNNING,
                "cycle_started_at": "2026-07-19T00:00:00+08:00",
                "processed_user_ids": ["user-1"],
                "checked_users": 1,
                "downloaded_items": 0,
                "errors": [],
                "priority_user_ids": [],
            }
            service._storage.save_state(state)

            await service.stop_monitoring()

            monitoring = service._storage.load_state()["monitoring"]
            self.assertFalse(monitoring["is_running"])
            self.assertFalse(monitoring["desired_running"])
            self.assertIsNone(monitoring.get("coverage_progress"))

    async def test_coverage_cycle_skips_already_processed_users(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = _build_service(Path(temp_dir))
            _seed_user(service, "user-1")
            _seed_user(service, "user-2")
            _seed_user(service, "user-3")

            state = service._storage.load_state()
            state["monitoring"]["coverage_progress"] = {
                "phase": COVERAGE_PHASE_RUNNING,
                "cycle_started_at": state["monitoring"]["updated_at"],
                "idle_until": None,
                "processed_user_ids": ["user-1"],
                "checked_users": 1,
                "downloaded_items": 0,
                "errors": [],
                "priority_user_ids": [],
            }
            service._storage.save_state(state)
            service._coverage_hours = 12.0
            service._mode = "coverage"

            synced_ids: list[str] = []

            async def fake_sync(user, summary):
                synced_ids.append(user["id"])
                summary["downloaded_items"] += 0

            with patch.object(service._sync_service, "sync_one_user", side_effect=fake_sync):
                with patch.object(service, "_sleep_interruptible", new=AsyncMock(return_value=False)):
                    await service._run_coverage_cycle()

            self.assertEqual(synced_ids, ["user-2", "user-3"])
            progress = service._storage.load_state()["monitoring"]["coverage_progress"]
            # 周期结束后进入 idle 或清空
            if progress is not None:
                self.assertIn(progress.get("phase"), {"idle", COVERAGE_PHASE_RUNNING})

    async def test_reset_runtime_reads_desired_running(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir)
            storage = MonitorStorage(path / "monitor_users.json")
            state = storage.load_state()
            state["monitoring"]["desired_running"] = True
            state["monitoring"]["is_running"] = False
            storage.save_state(state)

            service = MonitorService(
                crawler=FakeCrawler(),
                storage=storage,
                download_root=path / "download",
                notifier=NoopMonitorNotifier(),
            )
            self.assertTrue(service._was_running)
            self.assertFalse(service._storage.load_state()["monitoring"]["is_running"])
            self.assertTrue(service._storage.load_state()["monitoring"]["desired_running"])


if __name__ == "__main__":
    unittest.main()
