import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from unittest.mock import AsyncMock, patch

from douyin_user_monitor.monitor.history_sync import (
    HISTORY_SYNC_STATUS_PENDING,
    build_history_sync_state,
)
from douyin_user_monitor.monitor.notifier import NoopMonitorNotifier
from douyin_user_monitor.monitor.service import COVERAGE_PHASE_RUNNING, MonitorService
from douyin_user_monitor.monitor.storage import MonitorStorage


class FakeCrawler:
    """在用户资料拉取的 await 挂起点注入并发写回调，复现覆盖周期竞态窗口。"""

    def __init__(self):
        self.on_profile_fetch: Optional[Callable[[str], None]] = None

    async def get_sec_user_id(self, url: str) -> str:
        await asyncio.sleep(0)
        return url

    async def handler_user_profile(self, sec_user_id: str):
        await asyncio.sleep(0)
        if self.on_profile_fetch is not None:
            self.on_profile_fetch(sec_user_id)
        return {"user": {"nickname": f"昵称-{sec_user_id}", "avatar_thumb": {"url_list": []}}}

    async def fetch_user_post_videos(self, sec_user_id: str, max_cursor: int, count: int):
        _ = sec_user_id, max_cursor, count
        return {"aweme_list": [], "has_more": 0, "max_cursor": 0}

    async def fetch_one_video(self, aweme_id: str):
        return {"aweme_detail": {"aweme_id": aweme_id}}

    async def get_douyin_headers(self):
        return {"headers": {"User-Agent": "test"}}


def _build_service(temp_path: Path, crawler: FakeCrawler) -> MonitorService:
    return MonitorService(
        crawler=crawler,
        storage=MonitorStorage(temp_path / "monitor_users.json"),
        download_root=temp_path / "download",
        notifier=NoopMonitorNotifier(),
    )


def _seed_user(service: MonitorService, user_id: str) -> None:
    def _append(state: Dict[str, Any]) -> None:
        state["users"].append(
            {
                "id": user_id,
                "sec_user_id": f"sec-{user_id}",
                "nickname": user_id,
                "enabled": True,
                "avatar_url": "https://example.com/avatar.png",
                "account_status": "normal",
                "downloaded_count": 0,
                "downloaded_aweme_ids": [],
                "download_records": [],
                "history_sync": build_history_sync_state(status=HISTORY_SYNC_STATUS_PENDING),
            }
        )

    service._storage.update_state(_append)


def _seed_running_coverage(service: MonitorService) -> None:
    def _apply(state: Dict[str, Any]) -> None:
        state["monitoring"]["coverage_progress"] = {
            "phase": COVERAGE_PHASE_RUNNING,
            "cycle_started_at": state["monitoring"]["updated_at"],
            "idle_until": None,
            "processed_user_ids": [],
            "checked_users": 0,
            "downloaded_items": 0,
            "errors": [],
            "priority_user_ids": [],
        }

    service._storage.update_state(_apply)
    service._mode = "coverage"
    service._coverage_hours = 12.0


class CoverageAtomicityTests(unittest.IsolatedAsyncioTestCase):
    async def test_removed_user_not_resurrected_by_coverage_checkpoint(self):
        """覆盖周期内经 API 删除的、尚未处理的用户不能被 checkpoint 复活。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            crawler = FakeCrawler()
            service = _build_service(Path(temp_dir), crawler)
            _seed_user(service, "user-1")
            _seed_user(service, "user-2")
            _seed_user(service, "user-3")
            _seed_running_coverage(service)

            fired = {"done": False}

            def _remove_pending_once(sec_user_id: str) -> None:
                # 处理 user-1 时删除尚未处理的 user-2
                if sec_user_id == "sec-user-1" and not fired["done"]:
                    fired["done"] = True
                    service.remove_user("user-2")

            crawler.on_profile_fetch = _remove_pending_once

            with patch.object(service, "_sleep_interruptible", new=AsyncMock(return_value=False)):
                await service._run_coverage_cycle()

            disk_ids = [u["id"] for u in service._storage.load_state()["users"]]
            self.assertNotIn("user-2", disk_ids)
            self.assertIn("user-1", disk_ids)
            self.assertIn("user-3", disk_ids)

    async def test_disabled_user_not_rolled_back_by_coverage_checkpoint(self):
        """覆盖周期内经 API 禁用的、尚未处理的用户不能被 checkpoint 回滚为启用。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            crawler = FakeCrawler()
            service = _build_service(Path(temp_dir), crawler)
            _seed_user(service, "user-1")
            _seed_user(service, "user-2")
            _seed_running_coverage(service)

            fired = {"done": False}

            def _disable_pending_once(sec_user_id: str) -> None:
                if sec_user_id == "sec-user-1" and not fired["done"]:
                    fired["done"] = True
                    service.set_user_enabled("user-2", False)

            crawler.on_profile_fetch = _disable_pending_once

            with patch.object(service, "_sleep_interruptible", new=AsyncMock(return_value=False)):
                await service._run_coverage_cycle()

            by_id = {u["id"]: u for u in service._storage.load_state()["users"]}
            self.assertFalse(by_id["user-2"]["enabled"])


if __name__ == "__main__":
    unittest.main()
