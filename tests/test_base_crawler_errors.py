from __future__ import annotations

import unittest

import httpx

from crawlers.base_crawler import BaseCrawler
from crawlers.utils.api_exceptions import (
    APIConnectionError,
    APIRetryExhaustedError,
    APIUnauthorizedError,
)
from douyin_user_monitor.monitor.cookie_rotation import is_auth_like_error


class BaseCrawlerErrorTests(unittest.IsolatedAsyncioTestCase):
    async def _make_crawler(self, handler) -> BaseCrawler:
        crawler = BaseCrawler(max_retries=3, timeout=0)
        await crawler.aclient.aclose()
        crawler.aclient = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(crawler.aclient.aclose)
        return crawler

    async def test_401_raises_typed_error_without_retry(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(401, content=b"unauthorized")

        crawler = await self._make_crawler(handler)
        with self.assertRaises(APIUnauthorizedError):
            await crawler.get_fetch_data("https://example.com/api")
        self.assertEqual(len(calls), 1)

    async def test_403_raises_typed_error_without_retry(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(403, content=b"forbidden")

        crawler = await self._make_crawler(handler)
        with self.assertRaises(APIUnauthorizedError) as ctx:
            await crawler.get_fetch_data("https://example.com/api")
        self.assertEqual(len(calls), 1)
        self.assertTrue(is_auth_like_error(str(ctx.exception)))

    async def test_connect_error_retries_then_succeeds(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            if len(calls) == 1:
                raise httpx.ConnectError("connection refused", request=request)
            return httpx.Response(200, json={"status_code": 0})

        crawler = await self._make_crawler(handler)
        response = await crawler.get_fetch_data("https://example.com/api")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(calls), 2)

    async def test_argus_403_keeps_the_precise_rejection_reason(self):
        for reason in ("Uifid Not Found", "Signature Not Found"):
            with self.subTest(reason=reason):
                body = f"Blocked by ArgusSecurityPlugin {reason}"
                crawler = await self._make_crawler(lambda request: httpx.Response(403, text=body))
                with self.assertRaises(APIUnauthorizedError) as caught:
                    await crawler.get_fetch_data("https://example.com/api")
                self.assertEqual(str(caught.exception), f"HTTP Status Code 403: {body}")
                self.assertTrue(is_auth_like_error(str(caught.exception)))

    async def test_unknown_403_body_does_not_leak_into_public_error(self):
        crawler = await self._make_crawler(lambda request: httpx.Response(403, text="sessionid=private-value"))
        with self.assertRaises(APIUnauthorizedError) as caught:
            await crawler.get_fetch_data("https://example.com/api")
        self.assertEqual(str(caught.exception), "HTTP Status Code 403")

    async def test_connect_error_exhausted_raises_not_none(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            raise httpx.ConnectError("connection refused", request=request)

        crawler = await self._make_crawler(handler)
        with self.assertRaises(APIConnectionError):
            await crawler.get_fetch_data("https://example.com/api")
        self.assertEqual(len(calls), 3)

    async def test_empty_body_exhausted_raises_not_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"")

        crawler = await self._make_crawler(handler)
        with self.assertRaises(APIRetryExhaustedError):
            await crawler.post_fetch_data("https://example.com/api", params={"a": "1"})

    async def test_post_401_raises_typed_error_without_retry(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(401, content=b"unauthorized")

        crawler = await self._make_crawler(handler)
        with self.assertRaises(APIUnauthorizedError):
            await crawler.post_fetch_data("https://example.com/api", params={"a": "1"})
        self.assertEqual(len(calls), 1)

    async def test_401_message_matches_auth_like_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, content=b"unauthorized")

        crawler = await self._make_crawler(handler)
        with self.assertRaises(APIUnauthorizedError) as ctx:
            await crawler.get_fetch_data("https://example.com/api")
        self.assertIn("401", str(ctx.exception))
        self.assertTrue(is_auth_like_error(str(ctx.exception)))


if __name__ == "__main__":
    unittest.main()
