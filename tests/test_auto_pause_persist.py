import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict

from test_user_account_status import BannedAccountCrawler

from douyin_user_monitor.monitor.history_sync import (
    HISTORY_SYNC_STATUS_COMPLETED,
    build_history_sync_state,
)
from douyin_user_monitor.monitor.notifier import NoopMonitorNotifier
from douyin_user_monitor.monitor.service import MonitorService
from douyin_user_monitor.monitor.storage import MonitorStorage


def _seed_normal_user(storage: MonitorStorage, user_id: str) -> None:
    def _append(state: Dict[str, Any]) -> None:
        state["users"].append(
            {
                "id": user_id,
                "sec_user_id": f"sec-{user_id}",
                "nickname": user_id,
                "enabled": True,
                "avatar_url": "https://example.com/a.png",
                "account_status": "normal",
                "account_status_label": "正常",
                "account_status_reason": None,
                "account_status_updated_at": None,
                "downloaded_count": 0,
                "downloaded_aweme_ids": [],
                "last_aweme_id": None,
                "history_sync": build_history_sync_state(status=HISTORY_SYNC_STATUS_COMPLETED, page_size=50),
            }
        )

    storage.update_state(_append)


class AutoPausePersistTests(unittest.IsolatedAsyncioTestCase):
    async def test_banned_account_auto_paused_on_disk(self):
        """账号转为封禁时，自动停用必须落盘（commit 层持久化路径，而非仅内存快照）。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = MonitorStorage(Path(temp_dir) / "monitor_users.json")
            service = MonitorService(
                crawler=BannedAccountCrawler(),
                storage=storage,
                download_root=Path(temp_dir) / "download",
                notifier=NoopMonitorNotifier(),
            )
            _seed_normal_user(storage, "user-1")

            await service.run_once()

            disk_user = storage.load_state()["users"][0]
            self.assertEqual(disk_user["account_status"], "banned")
            # 关键回归守护：enabled 不在 USER_SYNC_MERGE_FIELDS，自动停用须由 commit 层派生落盘
            self.assertFalse(disk_user["enabled"])

    async def test_nickname_refresh_persisted_on_disk(self):
        """抖音端改名后，sync 抓到的新昵称须落盘（nickname 在合并白名单内）。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = MonitorStorage(Path(temp_dir) / "monitor_users.json")

            class RenamedCrawler(BannedAccountCrawler):
                async def handler_user_profile(self, sec_user_id: str):
                    self.profile_calls += 1
                    return {"user": {"nickname": "新昵称", "avatar_thumb": {"url_list": []}}}

            service = MonitorService(
                crawler=RenamedCrawler(),
                storage=storage,
                download_root=Path(temp_dir) / "download",
                notifier=NoopMonitorNotifier(),
            )
            _seed_normal_user(storage, "user-1")

            await service.run_once()

            disk_user = storage.load_state()["users"][0]
            self.assertEqual(disk_user["nickname"], "新昵称")

    async def test_manual_enabled_normal_account_not_touched(self):
        """正常账号的 enabled 不被 sync 落盘改动（保留 API 侧手动启停的所有权）。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = MonitorStorage(Path(temp_dir) / "monitor_users.json")

            class NormalCrawler(BannedAccountCrawler):
                async def handler_user_profile(self, sec_user_id: str):
                    self.profile_calls += 1
                    return {"user": {"nickname": f"昵称-{sec_user_id}", "avatar_thumb": {"url_list": []}}}

            service = MonitorService(
                crawler=NormalCrawler(),
                storage=storage,
                download_root=Path(temp_dir) / "download",
                notifier=NoopMonitorNotifier(),
            )
            _seed_normal_user(storage, "user-1")

            await service.run_once()

            disk_user = storage.load_state()["users"][0]
            self.assertEqual(disk_user["account_status"], "normal")
            self.assertTrue(disk_user["enabled"])


if __name__ == "__main__":
    unittest.main()
