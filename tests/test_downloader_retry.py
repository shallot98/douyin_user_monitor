from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import AsyncIterator, Callable
from unittest.mock import AsyncMock, patch

import httpx

from douyin_user_monitor.monitor.downloader import (
    MAX_DOWNLOAD_ATTEMPTS,
    AwemeAssetDownloader,
    _content_length,
)


class _FixedStream(httpx.AsyncByteStream):
    def __init__(self, content: bytes):
        self._content = content

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._content


class _CountingHandler:
    """按调用次数决定：前 fail_times 次抛连接错误，之后交给 response_factory。"""

    def __init__(self, fail_times: int, response_factory: Callable[[httpx.Request], httpx.Response]):
        self.calls = 0
        self._fail_times = fail_times
        self._response_factory = response_factory

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise httpx.ConnectError("连接失败")
        return self._response_factory(request)


def _ok_response(content: bytes) -> Callable[[httpx.Request], httpx.Response]:
    def factory(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Length": str(len(content))},
            stream=_FixedStream(content),
        )

    return factory


class DownloadVideoRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.downloader = AwemeAssetDownloader(self.root)
        self.video_file = self.root / "20231115_063320_123_demo.mp4"
        # 避免真实退避睡眠拖慢测试；不改变重试次数语义。
        self._sleep_patch = patch(
            "douyin_user_monitor.monitor.downloader.asyncio.sleep", new=AsyncMock()
        )
        self._sleep_patch.start()
        self.addCleanup(self._sleep_patch.stop)

    def _leftover_parts(self):
        return list(self.root.rglob("*.part"))

    async def test_retries_transient_error_then_succeeds(self):
        content = b"video-bytes"
        handler = _CountingHandler(fail_times=MAX_DOWNLOAD_ATTEMPTS - 1, response_factory=_ok_response(content))
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            is_downloaded = await self.downloader._download_video(
                "https://cdn/ok.mp4", {}, self.video_file, client=client
            )
        self.assertTrue(is_downloaded)
        self.assertEqual(handler.calls, MAX_DOWNLOAD_ATTEMPTS)
        self.assertEqual(self.video_file.read_bytes(), content)
        self.assertEqual(self._leftover_parts(), [])

    async def test_exhausts_retries_raises_and_leaves_no_part(self):
        handler = _CountingHandler(fail_times=999, response_factory=_ok_response(b"never"))
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            with self.assertRaises(httpx.ConnectError):
                await self.downloader._download_video(
                    "https://cdn/broken.mp4", {}, self.video_file, client=client
                )
        self.assertEqual(handler.calls, MAX_DOWNLOAD_ATTEMPTS)
        self.assertFalse(self.video_file.exists())
        self.assertEqual(self._leftover_parts(), [])

    async def test_http_status_error_is_not_retried(self):
        handler = _CountingHandler(fail_times=0, response_factory=lambda request: httpx.Response(403, content=b"no"))
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            with self.assertRaises(httpx.HTTPStatusError):
                await self.downloader._download_video(
                    "https://cdn/forbidden.mp4", {}, self.video_file, client=client
                )
        self.assertEqual(handler.calls, 1)
        self.assertFalse(self.video_file.exists())
        self.assertEqual(self._leftover_parts(), [])


class DownloadImageRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.downloader = AwemeAssetDownloader(self.root)
        self.image_folder = self.root / "图片" / "post"
        self._sleep_patch = patch(
            "douyin_user_monitor.monitor.downloader.asyncio.sleep", new=AsyncMock()
        )
        self._sleep_patch.start()
        self.addCleanup(self._sleep_patch.stop)

    async def test_image_retries_transient_error_then_succeeds(self):
        content = b"jpg-bytes"
        handler = _CountingHandler(fail_times=MAX_DOWNLOAD_ATTEMPTS - 1, response_factory=_ok_response(content))
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            result = await self.downloader._download_images(
                ["https://cdn/1.jpg"], {}, self.image_folder, client=client
            )
        self.assertEqual(result["downloaded_file_count"], 1)
        self.assertEqual(handler.calls, MAX_DOWNLOAD_ATTEMPTS)
        self.assertEqual((self.image_folder / "1.jpg").read_bytes(), content)
        self.assertEqual(list(self.root.rglob("*.part")), [])


class ContentLengthEncodingTests(unittest.TestCase):
    def test_plain_response_uses_content_length(self):
        response = httpx.Response(200, headers={"Content-Length": "100"})
        self.assertEqual(_content_length(response), 100)

    def test_gzip_encoding_skips_length_check(self):
        # gzip 下 Content-Length 是压缩体大小，与解压后写入字节不可比 → 返回 None 跳过校验
        response = httpx.Response(
            200, headers={"Content-Length": "45", "Content-Encoding": "gzip"}
        )
        self.assertIsNone(_content_length(response))

    def test_identity_encoding_still_verified(self):
        response = httpx.Response(
            200, headers={"Content-Length": "100", "Content-Encoding": "identity"}
        )
        self.assertEqual(_content_length(response), 100)


if __name__ == "__main__":
    unittest.main()
