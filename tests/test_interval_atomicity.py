import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from douyin_user_monitor.monitor.history_sync import (
    HISTORY_SYNC_STATUS_PENDING,
    build_history_sync_state,
)
from douyin_user_monitor.monitor.notifier import NoopMonitorNotifier
from douyin_user_monitor.monitor.service import MonitorService
from douyin_user_monitor.monitor.storage import MonitorStorage


class FakeCrawler:
    """在用户资料拉取的 await 挂起点注入并发写回调，复现 load→await→save 竞态窗口。"""

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
                "history_sync": build_history_sync_state(status=HISTORY_SYNC_STATUS_PENDING),
            }
        )

    service._storage.update_state(_append)


class IntervalAtomicityTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_once_does_not_resurrect_removed_user(self):
        """run_once 处理某用户期间被 API 删除的另一用户不能被整体 save 复活。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            crawler = FakeCrawler()
            service = _build_service(Path(temp_dir), crawler)
            _seed_user(service, "user-1")
            _seed_user(service, "user-2")
            _seed_user(service, "user-3")

            fired = {"done": False}

            def _remove_pending_once(sec_user_id: str) -> None:
                if sec_user_id == "sec-user-1" and not fired["done"]:
                    fired["done"] = True
                    service.remove_user("user-2")

            crawler.on_profile_fetch = _remove_pending_once

            await service.run_once()

            disk_ids = [u["id"] for u in service._storage.load_state()["users"]]
            self.assertNotIn("user-2", disk_ids)
            self.assertIn("user-1", disk_ids)
            self.assertIn("user-3", disk_ids)

    async def test_run_once_does_not_rollback_disabled_user(self):
        """run_once 处理某用户期间被 API 禁用的另一用户不能被整体 save 回滚为启用。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            crawler = FakeCrawler()
            service = _build_service(Path(temp_dir), crawler)
            _seed_user(service, "user-1")
            _seed_user(service, "user-2")

            fired = {"done": False}

            def _disable_pending_once(sec_user_id: str) -> None:
                if sec_user_id == "sec-user-1" and not fired["done"]:
                    fired["done"] = True
                    service.set_user_enabled("user-2", False)

            crawler.on_profile_fetch = _disable_pending_once

            await service.run_once()

            by_id = {u["id"]: u for u in service._storage.load_state()["users"]}
            self.assertFalse(by_id["user-2"]["enabled"])

    async def test_interval_cycle_does_not_resurrect_removed_user(self):
        """_run_interval_cycle 处理某用户期间被 API 删除的另一用户不能被整体 save 复活。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            crawler = FakeCrawler()
            service = _build_service(Path(temp_dir), crawler)
            _seed_user(service, "user-1")
            _seed_user(service, "user-2")
            _seed_user(service, "user-3")

            fired = {"done": False}

            def _remove_pending_once(sec_user_id: str) -> None:
                if sec_user_id == "sec-user-1" and not fired["done"]:
                    fired["done"] = True
                    service.remove_user("user-2")

            crawler.on_profile_fetch = _remove_pending_once

            await service._run_interval_cycle()

            disk_ids = [u["id"] for u in service._storage.load_state()["users"]]
            self.assertNotIn("user-2", disk_ids)
            self.assertIn("user-1", disk_ids)
            self.assertIn("user-3", disk_ids)


if __name__ == "__main__":
    unittest.main()
