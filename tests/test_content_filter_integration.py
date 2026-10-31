import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

from douyin_user_monitor.monitor.content_filter import CALIBER, RESULT_VERSION, ContentFilter
from douyin_user_monitor.monitor.crawler_protocol import MonitorCrawlerProtocol
from douyin_user_monitor.monitor.history_sync import HISTORY_SYNC_STATUS_COMPLETED, build_history_sync_state
from douyin_user_monitor.monitor.notifier import MonitorNotifierProtocol
from douyin_user_monitor.monitor.service import MonitorService
from douyin_user_monitor.monitor.storage import MonitorStorage
from douyin_user_monitor.settings import ContentFilterSettings

SETTINGS = ContentFilterSettings(
    enabled=True, mode="shadow", api_base="http://model.test/v1", api_key="k", model="vision-test",
    timeout_seconds=5, max_frames=3, reject_categories=("ad", "text_only", "screenshot"),
    min_confidence=0.8, include_history=True,
)


class ContentFilterPipelineTests(unittest.IsolatedAsyncioTestCase):
    """下载 → 记录 → 筛选结论落进 RecordStore；筛选失败也不丢下载记录。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.download_root = self.root / "download"
        self.storage = MonitorStorage(self.root / "monitor_users.json")
        self.crawler = AsyncMock(spec=MonitorCrawlerProtocol)
        self.crawler.handler_user_profile.return_value = {"user": {"nickname": "u"}}
        self.crawler.get_douyin_headers.return_value = {"headers": {}}
        self.crawler.fetch_user_post_videos.return_value = {
            "aweme_list": [{"aweme_id": "9002", "create_time": 1700000000}], "has_more": 0,
        }
        self.crawler.fetch_one_video.return_value = {"aweme_detail": {
            "aweme_id": "9002", "create_time": 1700000000, "desc": "语录", "aweme_type": 68, "is_ads": False,
            "images": [{"url_list": ["https://media.test/photo"]}],
        }}
        image = BytesIO()
        Image.new("RGB", (120, 240), "white").save(image, "PNG")
        self.picture = image.getvalue()
        state = self.storage.load_state()
        state["users"] = [{
            "id": "user-1", "sec_user_id": "sec-1", "nickname": "u", "enabled": True,
            "downloaded_aweme_ids": [], "downloaded_count": 0,
            "history_sync": build_history_sync_state(status=HISTORY_SYNC_STATUS_COMPLETED),
        }]
        self.storage.save_state(state)

    async def _run(self, model_handler):
        requests: list = []

        def counting_handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return model_handler(request)

        content_filter = ContentFilter(SETTINGS, self.download_root,
                                       transport=httpx.MockTransport(counting_handler))
        notifier = AsyncMock(spec=MonitorNotifierProtocol)
        service = MonitorService(crawler=self.crawler, storage=self.storage, download_root=self.download_root,
                                 notifier=notifier, content_filter=content_filter)
        with patch.object(service._downloader, "_create_shared_client", side_effect=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=self.picture)),
        )):
            result = await service.run_user_once("user-1")
        user = self.storage.records.attach(self.storage.load_state()["users"][0])
        return result, user, notifier, requests

    async def test_verdict_persisted_with_record(self):
        reply = json.dumps({"category": "text_only", "ad_type": None, "confidence": 0.97, "reason": "文字卡片",
                            "tags": {"subject": ["无人物"], "scene": [], "content": ["情感语录"], "style": [],
                                     "features": ["文字卡片"]},
                            "appearance": {"age": "无法判断", "outfit": [], "exposed_areas": [], "camera_focus": [],
                                           "poses": [], "borderline": None},
                            "summary": "白底文字语录"})

        result, user, notifier, requests = await self._run(
            lambda request: httpx.Response(200, json={"choices": [{"message": {"content": reply}}]}))

        self.assertEqual(result["errors"], [])
        verdict = user["download_records"][0]["content_filter"]
        self.assertEqual((verdict["status"], verdict["verdict"], verdict["category"]), ("ok", "reject", "text_only"))
        self.assertEqual(verdict["mode"], "shadow")
        self.assertEqual((verdict["version"], verdict["caliber"]), (RESULT_VERSION, CALIBER))
        self.assertEqual(len(requests), 1)
        self.assertEqual(notifier.notify_download_completed.await_count, 1)
        # shadow 模式不动文件
        self.assertTrue(any(self.download_root.rglob("*.jpg")))

    async def test_model_failure_keeps_download_record(self):
        result, user, notifier, requests = await self._run(lambda request: httpx.Response(502, text="bad gateway"))

        self.assertEqual(result["errors"], [])
        self.assertEqual(user["downloaded_aweme_ids"], ["9002"])
        verdict = user["download_records"][0]["content_filter"]
        self.assertEqual(verdict["status"], "error")
        self.assertIn("502", verdict["error"])
        self.assertEqual(notifier.notify_download_completed.await_count, 1)


    async def test_queue_mode_commits_record_before_model_answers(self):
        """有队列时 run_user_once 不等模型：记录先落盘，结论之后由队列补进去。"""
        import asyncio

        from douyin_user_monitor.monitor.content_filter_queue import ContentFilterQueue

        reply = json.dumps({"category": "normal", "ad_type": None, "confidence": 0.9, "reason": "r",
                            "tags": {"subject": [], "scene": [], "content": [], "style": [], "features": []},
                            "appearance": {"age": "无法判断", "outfit": [], "exposed_areas": [], "camera_focus": [],
                                           "poses": [], "borderline": None},
                            "summary": "s"})
        gate = asyncio.Event()

        async def gated_handler(request):
            await gate.wait()
            return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})

        content_filter = ContentFilter(SETTINGS, self.download_root, transport=httpx.MockTransport(gated_handler))
        queue = ContentFilterQueue(content_filter, self.storage, content_filter._local, record_wait_seconds=(0.05,) * 20)
        notifier = AsyncMock(spec=MonitorNotifierProtocol)
        service = MonitorService(crawler=self.crawler, storage=self.storage, download_root=self.download_root,
                                 notifier=notifier, content_filter_queue=queue)
        queue.start()
        self.addAsyncCleanup(queue.stop)
        with patch.object(service._downloader, "_create_shared_client", side_effect=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=self.picture)),
        )):
            result = await asyncio.wait_for(service.run_user_once("user-1"), timeout=10)

        self.assertEqual(result["errors"], [])
        record = self.storage.records.get("user-1")[0]
        self.assertNotIn("content_filter", record)
        self.assertEqual(notifier.notify_download_completed.await_count, 1)

        gate.set()
        await asyncio.wait_for(queue.drain(), timeout=10)
        verdict = self.storage.records.get("user-1")[0]["content_filter"]
        self.assertEqual((verdict["status"], verdict["version"]), ("ok", RESULT_VERSION))


if __name__ == "__main__":
    unittest.main()
