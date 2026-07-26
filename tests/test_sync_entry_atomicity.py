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
    """在资料拉取 / 作品页拉取的 await 挂起点分别注入并发写回调。"""

    def __init__(self):
        self.on_profile_fetch: Optional[Callable[[str], None]] = None
        self.on_posts_fetch: Optional[Callable[[str], None]] = None

    async def get_sec_user_id(self, url: str) -> str:
        await asyncio.sleep(0)
        return url

    async def handler_user_profile(self, sec_user_id: str):
        await asyncio.sleep(0)
        if self.on_profile_fetch is not None:
            self.on_profile_fetch(sec_user_id)
        return {"user": {"nickname": f"昵称-{sec_user_id}", "avatar_thumb": {"url_list": []}}}

    async def fetch_user_post_videos(self, sec_user_id: str, max_cursor: int, count: int):
        _ = max_cursor, count
        await asyncio.sleep(0)
        if self.on_posts_fetch is not None:
            self.on_posts_fetch(sec_user_id)
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


class RunUserOnceAtomicityTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_user_once_does_not_resurrect_concurrently_removed_user(self):
        """run_user_once 同步 user-1 期间被 API 删除的 user-2 不复活，未触及的 user-3 保留。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            crawler = FakeCrawler()
            service = _build_service(Path(temp_dir), crawler)
            _seed_user(service, "user-1")
            _seed_user(service, "user-2")
            _seed_user(service, "user-3")

            fired = {"done": False}

            def _remove_once(sec_user_id: str) -> None:
                if sec_user_id == "sec-user-1" and not fired["done"]:
                    fired["done"] = True
                    service.remove_user("user-2")

            crawler.on_profile_fetch = _remove_once

            await service.run_user_once("user-1")

            disk_ids = [u["id"] for u in service._storage.load_state()["users"]]
            self.assertNotIn("user-2", disk_ids)
            self.assertIn("user-1", disk_ids)
            self.assertIn("user-3", disk_ids)


class RunUserHistoryBackfillAtomicityTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_backfill_does_not_rollback_concurrently_disabled_user(self):
        """run_user_history_backfill_once 回填 user-1 期间被 API 禁用的 user-2 不被回滚为启用。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            crawler = FakeCrawler()
            service = _build_service(Path(temp_dir), crawler)
            _seed_user(service, "user-1")
            _seed_user(service, "user-2")
            _seed_user(service, "user-3")

            fired = {"done": False}

            def _disable_once(sec_user_id: str) -> None:
                # 历史回填走 fetch_user_post_videos 的 await 点
                if sec_user_id == "sec-user-1" and not fired["done"]:
                    fired["done"] = True
                    service.set_user_enabled("user-2", False)

            crawler.on_posts_fetch = _disable_once

            await service.run_user_history_backfill_once("user-1")

            by_id = {u["id"]: u for u in service._storage.load_state()["users"]}
            self.assertIn("user-3", by_id)
            self.assertFalse(by_id["user-2"]["enabled"])


class DrainPriorityUsersAtomicityTests(unittest.IsolatedAsyncioTestCase):
    async def test_drain_does_not_resurrect_concurrently_removed_user(self):
        """_drain_priority_users 处理优先用户 user-1 期间被删除的 user-2 不复活，user-3 保留。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            crawler = FakeCrawler()
            service = _build_service(Path(temp_dir), crawler)
            _seed_user(service, "user-1")
            _seed_user(service, "user-2")
            _seed_user(service, "user-3")

            # _enqueue_coverage_priority_user 仅在监控循环运行时入队；用哨兵任务模拟运行态
            sentinel = asyncio.create_task(asyncio.sleep(3600))
            service._loop_task = sentinel
            try:
                service._enqueue_coverage_priority_user("user-1")
                self.assertEqual(service._coverage_priority_user_ids, ["user-1"])

                fired = {"done": False}

                def _remove_once(sec_user_id: str) -> None:
                    if sec_user_id == "sec-user-1" and not fired["done"]:
                        fired["done"] = True
                        service.remove_user("user-2")

                crawler.on_profile_fetch = _remove_once

                await service._drain_priority_users()
            finally:
                sentinel.cancel()
                try:
                    await sentinel
                except asyncio.CancelledError:
                    pass

            disk_ids = [u["id"] for u in service._storage.load_state()["users"]]
            self.assertNotIn("user-2", disk_ids)
            self.assertIn("user-1", disk_ids)
            self.assertIn("user-3", disk_ids)


if __name__ == "__main__":
    unittest.main()
