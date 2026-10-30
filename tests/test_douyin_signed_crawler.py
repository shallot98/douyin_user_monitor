from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs

import httpx

from douyin_user_monitor.crawler.inprocess_client import InProcessDouyinClient


class SignedCrawlerTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        with patch("crawlers.douyin.web.utils.TokenManager.gen_real_msToken", return_value="test-token"):
            from crawlers.douyin.web.web_crawler import DouyinWebCrawler

        cls.crawler_class = DouyinWebCrawler

    async def asyncSetUp(self):
        self.requests = []
        self.source_headers = {"Cookie": "sessionid=session-a; UIFID=visitor-a", "User-Agent": "test-agent"}
        self.crawler = self.crawler_class()
        self.crawler.get_douyin_headers = AsyncMock(side_effect=lambda: {
            "headers": dict(self.source_headers), "proxies": {"http://": None, "https://": None},
        })
        transport = httpx.MockTransport(self._respond)
        patcher = patch("crawlers.base_crawler.httpx.AsyncHTTPTransport", return_value=transport)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _respond(self, request):
        self.requests.append(request)
        if request.url.path.endswith("/post/"):
            return httpx.Response(200, json={"status_code": 0, "aweme_list": [{"aweme_id": "work-id"}]})
        return httpx.Response(200, json={"status_code": 0, "aweme_detail": {"aweme_id": "work-id"}})

    def _assert_signed(self, request, visitor):
        query = parse_qs(request.url.query.decode(), keep_blank_values=True)
        self.assertEqual(query["uifid"], [visitor])
        self.assertEqual(request.headers["uifid"], visitor)
        self.assertRegex(query["x-secsdk-web-signature"][0], r"^[0-9a-f]{32}$")
        self.assertEqual(query["x-secsdk-web-signature"], [request.headers["x-secsdk-web-signature"]])
        self.assertEqual(query["timestamp"], [request.headers["x-secsdk-web-expire"]])
        self.assertTrue(query["a_bogus"][0])
        self.assertNotIn("x-secsdk-web-signature", self.source_headers)
        return query

    async def test_post_listing_signs_pagination_and_returns_payload(self):
        payload = await self.crawler.fetch_user_post_videos("target-user", 123456, 18)
        self.assertEqual(payload["aweme_list"], [{"aweme_id": "work-id"}])
        self.assertEqual(len(self.requests), 1)
        query = self._assert_signed(self.requests[0], "visitor-a")
        self.assertEqual(query["sec_user_id"], ["target-user"])
        self.assertEqual(query["max_cursor"], ["123456"])
        self.assertEqual(query["count"], ["18"])

    async def test_work_detail_is_also_signed(self):
        payload = await self.crawler.fetch_one_video("work-id")
        self.assertEqual(payload["aweme_detail"]["aweme_id"], "work-id")
        query = self._assert_signed(self.requests[0], "visitor-a")
        self.assertEqual(query["aweme_id"], ["work-id"])

    async def test_temporary_cookie_signs_with_the_selected_account(self):
        client = object.__new__(InProcessDouyinClient)
        client._crawler = self.crawler
        client._cookie_lock = asyncio.Lock()
        client._headers_bucket = lambda: self.source_headers
        async with client.temporary_cookie("sessionid=session-b; UIFID=visitor-b"):
            await client.fetch_user_post_videos("target-user", 0, 18)
        await client.fetch_user_post_videos("target-user", 0, 18)
        self._assert_signed(self.requests[0], "visitor-b")
        self._assert_signed(self.requests[1], "visitor-a")
        self.assertEqual(self.requests[0].headers["Cookie"], "sessionid=session-b; UIFID=visitor-b")
        self.assertEqual(self.source_headers["Cookie"], "sessionid=session-a; UIFID=visitor-a")

    async def test_missing_visitor_reports_error_without_http_request(self):
        self.source_headers["Cookie"] = "sessionid=session-a"
        with self.assertRaisesRegex(ValueError, "UIFID"):
            await self.crawler.fetch_one_video("work-id")
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
