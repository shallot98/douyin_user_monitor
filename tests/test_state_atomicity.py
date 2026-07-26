import asyncio
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from douyin_user_monitor.monitor.cookie_pool import CookiePoolService
from douyin_user_monitor.monitor.history_sync import (
    HISTORY_SYNC_STATUS_PENDING,
    build_history_sync_state,
)
from douyin_user_monitor.monitor.notifier import NoopMonitorNotifier
from douyin_user_monitor.monitor.service import MonitorService
from douyin_user_monitor.monitor.storage import MonitorStorage
from douyin_user_monitor.monitor.user_lookup import find_user_by_sec_uid


class FakeCrawler:
    """在 await 挂起点注入并发写回调，复现 load→await→save 竞态窗口。"""

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


class FakeRuntime:
    def __init__(self, cookie: str = ""):
        self.cookie = cookie
        self.updates: List[str] = []

    async def get_cookie(self) -> str:
        await asyncio.sleep(0)
        return self.cookie

    async def update_cookie(self, cookie: str) -> None:
        self.cookie = cookie
        self.updates.append(cookie)

    @asynccontextmanager
    async def temporary_cookie(self, cookie: str):
        old = self.cookie
        self.cookie = cookie
        try:
            yield
        finally:
            self.cookie = old


def _build_service(temp_path: Path, crawler: FakeCrawler) -> MonitorService:
    return MonitorService(
        crawler=crawler,
        storage=MonitorStorage(temp_path / "monitor_users.json"),
        download_root=temp_path / "download",
        notifier=NoopMonitorNotifier(),
    )


def _seed_user(storage: MonitorStorage, user_id: str, *, nickname: Optional[str] = None) -> None:
    def _append(state: Dict[str, Any]) -> None:
        state["users"].append(
            {
                "id": user_id,
                "sec_user_id": f"sec-{user_id}",
                "nickname": user_id if nickname is None else nickname,
                "enabled": True,
                "avatar_url": "https://example.com/avatar.png",
                "account_status": "normal",
                "downloaded_count": 0,
                "downloaded_aweme_ids": [],
                "download_records": [],
                "history_sync": build_history_sync_state(status=HISTORY_SYNC_STATUS_PENDING),
            }
        )

    storage.update_state(_append)


class AddUserAtomicityTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_write_during_profile_fetch_not_lost(self):
        """add 的网络阶段期间另一路径落盘的修改必须保留。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            crawler = FakeCrawler()
            service = _build_service(Path(temp_dir), crawler)
            _seed_user(service._storage, "user-1")

            crawler.on_profile_fetch = lambda _sec: service.set_user_enabled("user-1", False)
            result = await service.add_user_with_status("sec-new-user")

            self.assertEqual(result["status"], "created")
            disk = service._storage.load_state()
            existing = [u for u in disk["users"] if u["id"] == "user-1"]
            self.assertEqual(len(existing), 1)
            self.assertFalse(existing[0]["enabled"])
            self.assertIsNotNone(find_user_by_sec_uid(disk["users"], "sec-new-user"))

    async def test_concurrent_add_same_user_creates_single_entry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            crawler = FakeCrawler()
            service = _build_service(Path(temp_dir), crawler)

            first, second = await asyncio.gather(
                service.add_user_with_status("sec-race"),
                service.add_user_with_status("sec-race"),
            )

            statuses = sorted([first["status"], second["status"]])
            self.assertEqual(statuses, ["created", "exists"])
            disk = service._storage.load_state()
            matched = [u for u in disk["users"] if u["sec_user_id"] == "sec-race"]
            self.assertEqual(len(matched), 1)


class RemoveUserAtomicityTests(unittest.IsolatedAsyncioTestCase):
    async def test_removed_user_not_resurrected_by_profile_hydration(self):
        """资料补全期间被删除的用户不能因补全落盘而复活。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            crawler = FakeCrawler()
            service = _build_service(Path(temp_dir), crawler)
            _seed_user(service._storage, "user-1", nickname="")
            _seed_user(service._storage, "user-2")

            crawler.on_profile_fetch = lambda _sec: service.remove_user("user-2")
            users = await service.list_users_with_profile()

            self.assertNotIn("user-2", [u["id"] for u in users])
            disk = service._storage.load_state()
            disk_ids = [u["id"] for u in disk["users"]]
            self.assertNotIn("user-2", disk_ids)
            hydrated = next(u for u in disk["users"] if u["id"] == "user-1")
            self.assertEqual(hydrated["nickname"], "昵称-sec-user-1")

    async def test_remove_missing_user_raises_without_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = _build_service(Path(temp_dir), FakeCrawler())
            _seed_user(service._storage, "user-1")
            before = service._storage.state_file.read_text(encoding="utf-8")

            with self.assertRaises(ValueError):
                service.remove_user("no-such-user")

            after = service._storage.state_file.read_text(encoding="utf-8")
            self.assertEqual(before, after)


class CookiePoolAtomicityTests(unittest.IsolatedAsyncioTestCase):
    def _build_pool(self, temp_path: Path) -> tuple[CookiePoolService, MonitorStorage]:
        storage = MonitorStorage(temp_path / "monitor_users.json")
        runtime = FakeRuntime(cookie="")
        service = CookiePoolService(
            storage=storage,
            crawler=runtime,
            liveness_service=None,
        )
        return service, storage

    async def test_interleaved_add_keeps_both_items(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service, storage = self._build_pool(Path(temp_dir))

            await asyncio.gather(
                service.add_item(cookie="cookie-aaa-1111111111111111", name="A"),
                service.add_item(cookie="cookie-bbb-2222222222222222", name="B"),
            )

            items = storage.load_state()["monitoring"]["cookie_pool"]["items"]
            self.assertEqual(len(items), 2)
            self.assertEqual(sum(1 for row in items if row.get("is_active")), 1)

    async def test_update_does_not_clobber_concurrent_rotation_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service, storage = self._build_pool(Path(temp_dir))
            a = await service.add_item(cookie="cookie-aaa-1111111111111111", name="A")
            b = await service.add_item(cookie="cookie-bbb-2222222222222222", name="B")

            def _rotation_bump(state: Dict[str, Any]) -> None:
                for row in state["monitoring"]["cookie_pool"]["items"]:
                    if row["id"] == b["id"]:
                        row["use_count"] = 5
                        row["status"] = "healthy"

            storage.update_state(_rotation_bump)
            await service.update_item(a["id"], name="A-renamed")

            items = storage.load_state()["monitoring"]["cookie_pool"]["items"]
            by_id = {row["id"]: row for row in items}
            self.assertEqual(by_id[a["id"]]["name"], "A-renamed")
            self.assertEqual(by_id[b["id"]]["use_count"], 5)
            self.assertEqual(by_id[b["id"]]["status"], "healthy")

    async def test_update_missing_item_raises_without_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service, storage = self._build_pool(Path(temp_dir))
            await service.add_item(cookie="cookie-aaa-1111111111111111", name="A")
            before = storage.state_file.read_text(encoding="utf-8")

            with self.assertRaises(KeyError):
                await service.update_item("no-such-id", name="X")

            after = storage.state_file.read_text(encoding="utf-8")
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
