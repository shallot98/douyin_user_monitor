import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

from douyin_user_monitor.monitor.cover_store import CoverStore
from douyin_user_monitor.monitor.crawler_protocol import MonitorCrawlerProtocol
from douyin_user_monitor.monitor.history_sync import HISTORY_SYNC_STATUS_COMPLETED, build_history_sync_state
from douyin_user_monitor.monitor.new_work_cover import NewWorkCoverPublisher
from douyin_user_monitor.monitor.notifier import MonitorNotifierProtocol
from douyin_user_monitor.monitor.service import MonitorService
from douyin_user_monitor.monitor.storage import MonitorStorage


class NewDownloadCoverIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.download_root = self.root / "download"
        self.cover_store = CoverStore(self.root / "covers")
        self.storage = MonitorStorage(self.root / "monitor_users.json")
        self.notifier = AsyncMock(spec=MonitorNotifierProtocol)
        self.crawler = AsyncMock(spec=MonitorCrawlerProtocol)
        self.crawler.handler_user_profile.return_value = {"user": {"nickname": "image-user"}}
        self.crawler.get_douyin_headers.return_value = {"headers": {}}
        self.crawler.fetch_user_post_videos.return_value = {
            "aweme_list": [{"aweme_id": "9002", "create_time": 1700000000}], "has_more": 0,
        }
        self.detail = {
            "aweme_id": "9002", "create_time": 1700000000, "desc": "image post", "aweme_type": 68,
            "images": [{"url_list": ["https://media.test/photo"]}],
        }
        self.crawler.fetch_one_video.return_value = {"aweme_detail": self.detail}
        image = BytesIO()
        Image.new("RGB", (120, 240), "blue").save(image, "PNG")
        self.original = image.getvalue()
        state = self.storage.load_state()
        state["users"] = [{
            "id": "user-1", "sec_user_id": "sec-1", "nickname": "image-user", "enabled": True,
            "downloaded_aweme_ids": [], "downloaded_count": 0,
            "history_sync": build_history_sync_state(status=HISTORY_SYNC_STATUS_COMPLETED),
        }]
        self.storage.save_state(state)
        self.service = MonitorService(
            crawler=self.crawler, storage=self.storage, download_root=self.download_root,
            notifier=self.notifier,
            cover_publisher=NewWorkCoverPublisher(self.download_root, self.cover_store).publish,
        )

    async def run_download(self, handler):
        with patch.object(self.service._downloader, "_create_shared_client", side_effect=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
        )):
            return await self.service.run_user_once("user-1")

    async def test_image_post_generates_and_persists_real_cover_before_completion(self):
        async def check_cover(*, user_nickname, record):
            metadata = self.cover_store.lookup("user-1", record)
            self.assertIsNotNone(metadata)
            with Image.open(metadata.path) as image:
                image.load()
                self.assertEqual(image.format, "JPEG")

        self.notifier.notify_download_completed.side_effect = check_cover

        result = await self.run_download(lambda request: httpx.Response(200, content=self.original))

        self.assertEqual(result["errors"], [])
        self.assertEqual(result["downloaded_items"], 1)
        user = self.storage.records.attach(self.storage.load_state()["users"][0])
        record = user["download_records"][0]
        self.assertEqual(user["downloaded_aweme_ids"], ["9002"])
        metadata = CoverStore(self.root / "covers").lookup("user-1", record)
        self.assertEqual(metadata.downloaded_at, record["downloaded_at"])
        self.assertEqual((metadata.width, metadata.height), (120, 240))
        self.assertFalse(metadata.path.is_relative_to(self.download_root))
        self.assertEqual((self.download_root / record["files"][0]).read_bytes(), self.original)
        self.assertTrue((self.download_root / record["cover_file"]).is_file())
        self.notifier.notify_download_completed.assert_awaited_once()

    async def test_corrupt_upstream_cover_does_not_mark_work_downloaded(self):
        self.detail["video"] = {"cover": {"url_list": ["https://media.test/cover"]}}

        def handler(request):
            content = b"<html>not an image</html>" if request.url.path == "/cover" else self.original
            return httpx.Response(200, content=content)

        result = await self.run_download(handler)

        self.assertEqual(result["downloaded_items"], 0)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("cannot identify image", result["errors"][0]["error"])
        user = self.storage.records.attach(self.storage.load_state()["users"][0])
        self.assertEqual(user["downloaded_aweme_ids"], [])
        self.assertEqual(user["download_records"], [])
        self.assertEqual(list((self.root / "covers").rglob("*.jpg")), [])
        self.notifier.notify_download_completed.assert_not_awaited()

    async def test_corrupt_cover_is_downloaded_again_after_upstream_recovers(self):
        self.detail["video"] = {"cover": {"url_list": ["https://media.test/cover"]}}

        def invalid_cover(request):
            content = b"<html>not an image</html>" if request.url.path == "/cover" else self.original
            return httpx.Response(200, content=content)

        first = await self.run_download(invalid_cover)
        self.assertEqual(first["downloaded_items"], 0)
        self.notifier.notify_download_completed.assert_not_awaited()
        retry_requests = []

        def recovered_cover(request):
            retry_requests.append(request.url.path)
            return httpx.Response(200, content=self.original)

        retry = await self.run_download(recovered_cover)

        self.assertEqual(retry["errors"], [])
        self.assertEqual(retry["downloaded_items"], 1)
        self.assertEqual(retry_requests, ["/cover"])
        record = self.storage.records.get("user-1")[0]
        self.assertIsNotNone(self.cover_store.lookup("user-1", record))
        self.assertEqual((self.download_root / record["files"][0]).read_bytes(), self.original)
        self.notifier.notify_download_completed.assert_awaited_once()

    async def test_valid_cover_is_reused_after_store_publication_failure(self):
        self.detail["video"] = {"cover": {"url_list": ["https://media.test/cover"]}}
        with patch.object(self.cover_store, "_publish_index", side_effect=OSError("index cannot be written")):
            first = await self.run_download(lambda request: httpx.Response(200, content=self.original))
        self.assertEqual(first["downloaded_items"], 0)
        self.notifier.notify_download_completed.assert_not_awaited()

        def unexpected_download(request):
            raise AssertionError(f"Verified local assets should be reused: {request.url.path}")

        retry = await self.run_download(unexpected_download)

        self.assertEqual(retry["errors"], [])
        self.assertEqual(retry["downloaded_items"], 1)
        self.notifier.notify_download_completed.assert_awaited_once()

    async def test_invalid_content_image_is_retried_before_generating_cover(self):
        first = await self.run_download(lambda request: httpx.Response(200, content=b"<html>temporary error</html>"))
        self.assertEqual(first["downloaded_items"], 0)
        self.assertEqual(len(first["errors"]), 1)
        self.notifier.notify_download_completed.assert_not_awaited()
        retry_requests = []

        def recovered_image(request):
            retry_requests.append(request.url.path)
            return httpx.Response(200, content=self.original)

        retry = await self.run_download(recovered_image)

        self.assertEqual(retry["errors"], [])
        self.assertEqual(retry["downloaded_items"], 1)
        self.assertEqual(retry_requests, ["/photo"])
        record = self.storage.records.get("user-1")[0]
        self.assertEqual((self.download_root / record["files"][0]).read_bytes(), self.original)
        self.assertIsNotNone(self.cover_store.lookup("user-1", record))
        self.notifier.notify_download_completed.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
