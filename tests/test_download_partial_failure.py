import unittest
from typing import Any, Dict, List

from douyin_user_monitor.monitor.history_sync import (
    HISTORY_SYNC_STATUS_COMPLETED,
    build_history_sync_state,
)
from douyin_user_monitor.monitor.notifier import MonitorNotifierProtocol
from douyin_user_monitor.monitor.user_sync import UserSyncService


class TwoPostCrawler:
    """返回两个新作品；a-bad 的详情正常但下载会失败。"""

    async def handler_user_profile(self, sec_user_id: str):
        return {"user": {"nickname": f"昵称-{sec_user_id}"}}

    async def get_sec_user_id(self, url: str) -> str:
        return url

    async def fetch_user_post_videos(self, sec_user_id: str, max_cursor: int, count: int):
        _ = sec_user_id, count
        if max_cursor == 0:
            return {
                "aweme_list": [
                    {"aweme_id": "a-good", "desc": "ok"},
                    {"aweme_id": "a-bad", "desc": "fail"},
                ],
                "has_more": 0,
                "max_cursor": 0,
            }
        return {"aweme_list": [], "has_more": 0, "max_cursor": max_cursor}

    async def fetch_one_video(self, aweme_id: str):
        return {"aweme_detail": {"aweme_id": aweme_id}}

    async def get_douyin_headers(self):
        return {"headers": {}}


class FlakyDownloader:
    def __init__(self):
        self.attempted: List[str] = []

    async def download_aweme_assets(self, *, aweme_id: str, **_kwargs) -> Dict[str, Any]:
        self.attempted.append(aweme_id)
        if aweme_id == "a-bad":
            raise RuntimeError("模拟下载中断")
        return {
            "media_type": "video",
            "files": [],
            "downloaded_file_count": 1,
            "existing_file_count": 0,
            "image_count": 0,
            "total_size_bytes": 0,
        }


class RecordingNotifier(MonitorNotifierProtocol):
    def __init__(self):
        self.completed: List[str] = []

    async def notify_new_aweme_detected(self, *, user_nickname: str, aweme_detail: Dict[str, Any]) -> None:
        _ = user_nickname, aweme_detail

    async def notify_download_completed(self, *, user_nickname: str, record: Dict[str, Any]) -> None:
        _ = user_nickname
        self.completed.append(str(record.get("aweme_id", "")))

    async def notify_account_status_changed(self, **_kwargs) -> None:
        return None


def _make_user() -> Dict[str, Any]:
    return {
        "id": "user-1",
        "sec_user_id": "sec-1",
        "nickname": "测试用户",
        "downloaded_count": 0,
        "downloaded_aweme_ids": [],
        "download_records": [],
        "last_aweme_id": None,
        "history_sync": build_history_sync_state(status=HISTORY_SYNC_STATUS_COMPLETED, page_size=50),
    }


class DownloadPartialFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_post_persisted_when_sibling_fails(self):
        downloader = FlakyDownloader()
        notifier = RecordingNotifier()
        service = UserSyncService(crawler=TwoPostCrawler(), downloader=downloader, notifier=notifier)
        user = _make_user()
        summary = {"checked_users": 1, "downloaded_items": 0, "errors": []}

        await service.sync_one_user(user, summary)

        # 成功作品落盘、失败作品未落盘（下轮才会重试，不会因整批 raise 而丢失已完成的）
        self.assertIn("a-good", user["downloaded_aweme_ids"])
        self.assertNotIn("a-bad", user["downloaded_aweme_ids"])
        # 单作品失败不让整个用户同步失败
        self.assertIsNone(user["last_error"])
        # 失败作品记入 summary
        self.assertEqual(len(summary["errors"]), 1)
        self.assertIn("a-bad", summary["errors"][0]["error"])
        # 只对成功作品推送完成通知
        self.assertEqual(notifier.completed, ["a-good"])

    async def test_second_sync_does_not_reprocess_completed_post(self):
        downloader = FlakyDownloader()
        notifier = RecordingNotifier()
        service = UserSyncService(crawler=TwoPostCrawler(), downloader=downloader, notifier=notifier)
        user = _make_user()
        summary = {"checked_users": 1, "downloaded_items": 0, "errors": []}

        await service.sync_one_user(user, summary)
        # 第二轮：a-good 已在 downloaded_aweme_ids 中，不应再次尝试下载/通知
        downloader.attempted.clear()
        notifier.completed.clear()
        summary2 = {"checked_users": 1, "downloaded_items": 0, "errors": []}
        await service.sync_one_user(user, summary2)

        self.assertNotIn("a-good", downloader.attempted)
        self.assertNotIn("a-good", notifier.completed)


if __name__ == "__main__":
    unittest.main()
