"""监控下载：登录态只拉作品列表，详情/原画/下载文件用过期 Cookie，取不到再回落登录态。"""

import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

from test_download_partial_failure import FlakyDownloader, RecordingNotifier, _make_user

from douyin_user_monitor.monitor.parse_cookies import ParseCookiePool
from douyin_user_monitor.monitor.user_sync import UserSyncService

LOGIN = "sessionid=login"


class RecordingCrawler:
    """记录每次请求带的是哪个 Cookie（None = 全局登录态）。"""

    def __init__(self, guest_missing=()):
        self.calls: List[tuple] = []
        self.guest_missing = set(guest_missing)

    async def handler_user_profile(self, sec_user_id: str):
        return {"user": {"nickname": "n"}}

    async def fetch_user_post_videos(self, sec_user_id: str, max_cursor: int, count: int):
        self.calls.append(("list", None))
        return {"aweme_list": [{"aweme_id": "a1"}, {"aweme_id": "a2"}], "has_more": 0, "max_cursor": 0}

    async def fetch_one_video(self, aweme_id: str, cookie: Optional[str] = None):
        self.calls.append(("detail", aweme_id, cookie))
        if cookie and aweme_id in self.guest_missing:
            return {"status_code": 0}
        return {"aweme_detail": {"aweme_id": aweme_id}}

    async def get_douyin_headers(self, cookie: Optional[str] = None):
        return {"headers": {"Cookie": cookie or LOGIN}}


class HeaderRecordingDownloader(FlakyDownloader):
    def __init__(self):
        super().__init__(fail_on=None)
        self.headers: Dict[str, str] = {}

    async def download_aweme_assets(self, *, aweme_id: str, headers: Dict[str, str], **kwargs) -> Dict[str, Any]:
        self.headers[aweme_id] = headers.get("Cookie")
        return await super().download_aweme_assets(aweme_id=aweme_id, **kwargs)


class DetailCookieTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def _service(self, crawler, downloader, *, cookies=("ttwid=g1", "ttwid=g2")):
        for index, cookie in enumerate(cookies):
            (self.dir / f"{index}.txt").write_text(cookie, encoding="utf-8")
        return UserSyncService(
            crawler=crawler, downloader=downloader, notifier=RecordingNotifier(),
            detail_cookie_pool=ParseCookiePool(self.dir),
        )

    async def _sync(self, service):
        user = _make_user()
        summary = {"checked_users": 1, "downloaded_items": 0, "errors": []}
        await service.sync_one_user(user, summary)
        return user, summary

    async def test_login_cookie_only_lists_posts(self):
        crawler, downloader = RecordingCrawler(), HeaderRecordingDownloader()
        user, summary = await self._sync(self._service(crawler, downloader))
        self.assertEqual(summary["errors"], [])
        self.assertEqual(sorted(user["downloaded_aweme_ids"]), ["a1", "a2"])
        details = [call for call in crawler.calls if call[0] == "detail"]
        self.assertTrue(all(call[2] in {"ttwid=g1", "ttwid=g2"} for call in details), details)
        self.assertEqual(len(details), 2)
        # 下载（原画跳转 + CDN）带的是取详情用的那条过期 Cookie，不是登录态
        self.assertEqual(set(downloader.headers.values()), {"ttwid=g1", "ttwid=g2"})

    async def test_falls_back_to_login_when_expired_cookie_gets_nothing(self):
        crawler, downloader = RecordingCrawler(guest_missing={"a1"}), HeaderRecordingDownloader()
        with self.assertLogs("uvicorn.error", level="WARNING") as logs:
            user, summary = await self._sync(self._service(crawler, downloader))
        self.assertEqual(summary["errors"], [])
        self.assertIn("a1", user["downloaded_aweme_ids"])
        a1_calls = [call[2] for call in crawler.calls if call[0] == "detail" and call[1] == "a1"]
        self.assertEqual(len(a1_calls), 2)
        self.assertIsNone(a1_calls[1], "第二次用登录态")
        self.assertEqual(downloader.headers["a1"], LOGIN)
        self.assertTrue(any("改用登录态" in line for line in logs.output))

    async def test_empty_pool_uses_login_with_warning(self):
        crawler, downloader = RecordingCrawler(), HeaderRecordingDownloader()
        with self.assertLogs("uvicorn.error", level="WARNING"):
            user, summary = await self._sync(self._service(crawler, downloader, cookies=()))
        self.assertEqual(summary["errors"], [])
        self.assertEqual(set(downloader.headers.values()), {LOGIN})

    async def test_without_pool_behaves_as_before(self):
        crawler, downloader = RecordingCrawler(), HeaderRecordingDownloader()
        service = UserSyncService(crawler=crawler, downloader=downloader, notifier=RecordingNotifier())
        user, summary = await self._sync(service)
        self.assertEqual(summary["errors"], [])
        self.assertTrue(all(call[2] is None for call in crawler.calls if call[0] == "detail"))


if __name__ == "__main__":
    unittest.main()
