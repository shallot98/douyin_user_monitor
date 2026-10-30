from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import AsyncIterator, Dict
from unittest.mock import patch

import httpx

from douyin_user_monitor.monitor.downloader import AwemeAssetDownloader


class _BrokenStream(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"partial-bytes"
        raise httpx.ReadError("连接中断")


class _FixedStream(httpx.AsyncByteStream):
    def __init__(self, content: bytes):
        self._content = content

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._content


def _mock_client_patch(transport: httpx.MockTransport):
    real_async_client = httpx.AsyncClient

    def factory(**kwargs):
        kwargs["transport"] = transport
        return real_async_client(**kwargs)

    return patch(
        "douyin_user_monitor.monitor.downloader.httpx.AsyncClient",
        side_effect=factory,
    )


class BuildAwemeFilenameTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.downloader = AwemeAssetDownloader(Path(self.temp_dir.name))

    def test_same_second_same_desc_no_collision(self):
        base: Dict[str, object] = {"desc": "同秒同文案", "create_time": 1_700_000_000}
        name_one = self.downloader._build_aweme_filename({**base, "aweme_id": "111"})
        name_two = self.downloader._build_aweme_filename({**base, "aweme_id": "222"})
        self.assertNotEqual(name_one, name_two)
        self.assertIn("111", name_one)
        self.assertIn("222", name_two)

    def test_missing_create_time_is_reproducible_with_aweme_id(self):
        detail = {"aweme_id": "333", "desc": "无时间"}
        name_one = self.downloader._build_aweme_filename(detail)
        name_two = self.downloader._build_aweme_filename(detail)
        self.assertEqual(name_one, name_two)
        self.assertIn("333", name_one)

    def test_invalid_create_time_uses_fallback_aweme_id(self):
        detail = {"desc": "非法时间", "create_time": "abc"}
        name = self.downloader._build_aweme_filename(detail, "444")
        self.assertEqual(name, self.downloader._build_aweme_filename(detail, "444"))
        self.assertIn("444", name)


class DownloadVideoAtomicTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.downloader = AwemeAssetDownloader(self.root)
        self.video_file = self.root / "20231115_063320_123_demo.mp4"

    def _leftover_parts(self):
        return list(self.root.rglob("*.part"))

    async def test_interrupted_stream_leaves_no_files(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=_BrokenStream())

        with _mock_client_patch(httpx.MockTransport(handler)):
            with self.assertRaises(httpx.ReadError):
                await self.downloader._download_video(
                    "https://cdn/broken.mp4", {}, self.video_file
                )
        self.assertFalse(self.video_file.exists())
        self.assertEqual(self._leftover_parts(), [])

    async def test_content_length_mismatch_fails(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Length": "999"},
                stream=_FixedStream(b"short"),
            )

        with _mock_client_patch(httpx.MockTransport(handler)):
            with self.assertRaises(ValueError):
                await self.downloader._download_video(
                    "https://cdn/truncated.mp4", {}, self.video_file
                )
        self.assertFalse(self.video_file.exists())
        self.assertEqual(self._leftover_parts(), [])

    async def test_success_writes_final_file_without_part(self):
        content = b"video-bytes"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Length": str(len(content))},
                stream=_FixedStream(content),
            )

        with _mock_client_patch(httpx.MockTransport(handler)):
            is_downloaded = await self.downloader._download_video(
                "https://cdn/ok.mp4", {}, self.video_file
            )
        self.assertTrue(is_downloaded)
        self.assertEqual(self.video_file.read_bytes(), content)
        self.assertEqual(self._leftover_parts(), [])


class DownloadImagesAtomicTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.downloader = AwemeAssetDownloader(self.root)
        self.image_folder = self.root / "图片" / "post"

    async def test_content_length_mismatch_fails(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Length": "999"},
                stream=_FixedStream(b"jpg"),
            )

        with _mock_client_patch(httpx.MockTransport(handler)):
            with self.assertRaises(ValueError):
                await self.downloader._download_images(
                    ["https://cdn/1.jpg"], {}, self.image_folder
                )
        self.assertFalse((self.image_folder / "1.jpg").exists())
        self.assertEqual(list(self.root.rglob("*.part")), [])

    async def test_success_writes_images(self):
        content = b"jpg-bytes"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Length": str(len(content))},
                stream=_FixedStream(content),
            )

        with _mock_client_patch(httpx.MockTransport(handler)):
            result = await self.downloader._download_images(
                ["https://cdn/1.jpg", "https://cdn/2.jpg"], {}, self.image_folder
            )
        self.assertEqual(result["downloaded_file_count"], 2)
        self.assertEqual((self.image_folder / "1.jpg").read_bytes(), content)
        self.assertEqual(list(self.root.rglob("*.part")), [])


if __name__ == "__main__":
    unittest.main()
