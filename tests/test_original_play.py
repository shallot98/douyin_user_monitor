from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, patch

import httpx

from douyin_user_monitor.monitor.downloader import AwemeAssetDownloader
from douyin_user_monitor.monitor.original_play import (
    build_original_play_url,
    extract_video_id,
    looks_like_source_cdn,
    resolve_original_play_url,
)
from douyin_user_monitor.monitor.playback_media import PlaybackMedia
from douyin_user_monitor.monitor.playback_prepare import PreparedPlayback
from douyin_user_monitor.monitor.video_source import QUALITY_BIT_RATE, QUALITY_ORIGINAL


class OriginalPlayHelpersTests(unittest.TestCase):
    def test_extract_video_id_from_play_addr(self):
        detail = {
            "video": {
                "play_addr": {"uri": "v2800fgi0000d7meamvog65qpql92v50", "url_list": ["https://cdn/x.mp4"]},
            }
        }
        self.assertEqual(extract_video_id(detail), "v2800fgi0000d7meamvog65qpql92v50")

    def test_extract_video_id_from_bit_rate_when_top_missing(self):
        detail = {
            "video": {
                "bit_rate": [
                    {"play_addr": {"uri": "v1e00frombitrateabc12345", "url_list": ["https://cdn/a.mp4"]}},
                ]
            }
        }
        self.assertEqual(extract_video_id(detail), "v1e00frombitrateabc12345")

    def test_extract_video_id_rejects_music_url(self):
        detail = {
            "video": {
                "play_addr": {
                    "uri": "https://sf6-cdn-tos.douyinstatic.com/obj/ies-music/7570574461940337418.mp3",
                    "url_list": ["https://cdn/cover.jpg"],
                }
            }
        }
        self.assertIsNone(extract_video_id(detail))

    def test_build_original_play_url_uses_default_ratio(self):
        url = build_original_play_url("v2800abc")
        self.assertIn("video_id=v2800abc", url)
        self.assertIn("ratio=default", url)
        self.assertIn("/aweme/v1/play/", url)

    def test_looks_like_source_cdn(self):
        self.assertTrue(
            looks_like_source_cdn(
                "https://v3-dy-o.zjcdn.com/tos-cn-v-0015c000-ce/oUEAEo/video.mp4"
            )
        )
        self.assertFalse(looks_like_source_cdn("https://cdn.example/tos-cn-ve-0015/x.mp4"))


class ResolveOriginalPlayTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolve_follows_redirect_to_cdn(self):
        play_url = build_original_play_url("v1e00testid0000000001")
        cdn_url = "https://v3-dy-o.zjcdn.com/tos-cn-v-0015/source.mp4"

        def handler(request: httpx.Request) -> httpx.Response:
            if "aweme/v1/play" in str(request.url):
                return httpx.Response(302, headers={"Location": cdn_url})
            if str(request.url).startswith("https://v3-dy-o.zjcdn.com/"):
                return httpx.Response(
                    200,
                    headers={"content-type": "video/mp4", "content-length": "11"},
                    content=b"video-bytes",
                )
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            result = await resolve_original_play_url(
                "v1e00testid0000000001",
                {"User-Agent": "test"},
                client=client,
            )
        self.assertEqual(result, cdn_url)
        self.assertIn("ratio=default", play_url)

    async def test_resolve_returns_none_on_http_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, content=b"forbidden")

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            result = await resolve_original_play_url("v1e00badid0000000001", {}, client=client)
        self.assertIsNone(result)

    async def test_resolve_returns_none_for_transcode_cdn(self):
        cdn_url = "https://v5-dy.zjcdn.com/tos-cn-ve-15c000-ce/transcode.mp4"

        def handler(request: httpx.Request) -> httpx.Response:
            if "aweme/v1/play" in str(request.url):
                return httpx.Response(302, headers={"Location": cdn_url})
            return httpx.Response(
                200,
                headers={"content-type": "video/mp4", "content-length": "100"},
                content=b"x" * 100,
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            result = await resolve_original_play_url("v1e00transcode0000001", {}, client=client)
        self.assertIsNone(result)

    async def test_resolve_returns_none_for_invalid_video_id(self):
        result = await resolve_original_play_url(
            "https://sf6-cdn-tos.douyinstatic.com/obj/ies-music/x.mp3",
            {},
        )
        self.assertIsNone(result)


class DownloaderOriginalPreferenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.downloader = AwemeAssetDownloader(
            Path(self.temp_dir.name), playback_preparer=self._prepared, cover_preparer=self._cover
        )

    async def _prepared(self, request):
        target = request.target_path if request.candidate_path is not None else request.archive_path
        return PreparedPlayback(
            path=target, method="verified", created=target != request.archive_path,
            media=PlaybackMedia(
                width=540, height=960, bit_rate=900_000, duration=1,
                video_codec="h264", pixel_format="yuv420p", audio_codecs=(), is_mp4=True, faststart=True,
            ),
        )

    async def _cover(self, source, target):
        target.write_bytes(b"cover")

    def _detail(self) -> Dict[str, Any]:
        return {
            "aweme_id": "123",
            "desc": "demo",
            "create_time": 1_700_000_000,
            "aweme_type": 0,
            "video": {
                "play_addr": {
                    "uri": "v1e00originalid000001",
                    "url_list": ["https://cdn/default.mp4"],
                    "width": 720,
                    "height": 1280,
                },
                "bit_rate": [
                    {
                        "bit_rate": 3_000_000,
                        "gear_name": "normal_1080",
                        "is_h265": 0,
                        "play_addr": {
                            "url_list": ["https://cdn/1080.mp4"],
                            "width": 1080,
                            "height": 1920,
                        },
                    }
                ],
            },
        }

    async def test_image_post_skips_original_play(self):
        detail = {
            "aweme_id": "img1",
            "desc": "note",
            "create_time": 1_700_000_000,
            "aweme_type": 68,
            "images": [{"url_list": ["https://cdn/1.jpg"]}],
            "video": {
                "play_addr": {
                    "uri": "https://sf6-cdn-tos.douyinstatic.com/obj/ies-music/x.mp3",
                    "url_list": ["https://cdn/cover.jpg"],
                    "width": 720,
                    "height": 720,
                }
            },
        }
        with patch(
            "douyin_user_monitor.monitor.downloader.resolve_original_play_url",
            new=AsyncMock(return_value="https://should-not-call"),
        ) as resolve_mock:
            with patch.object(
                self.downloader,
                "_download_images",
                new=AsyncMock(
                    return_value={
                        "files": [Path(self.temp_dir.name) / "1.jpg"],
                        "downloaded_file_count": 1,
                        "existing_file_count": 0,
                    }
                ),
            ):
                result = await self.downloader.download_aweme_assets(
                    aweme_id="img1",
                    sec_user_id="sec",
                    user_nickname="user",
                    aweme_detail=detail,
                    headers={},
                )
        self.assertEqual(result["media_type"], "image")
        resolve_mock.assert_not_called()

    async def test_prefers_original_play_url(self):
        original_url = "https://v3-dy-o.zjcdn.com/tos-cn-v-x/source.mp4"
        with patch(
            "douyin_user_monitor.monitor.downloader.resolve_original_play_url",
            new=AsyncMock(return_value=original_url),
        ):
            with patch.object(
                self.downloader,
                "_download_video",
                new=AsyncMock(return_value=True),
            ) as download_mock:
                result = await self.downloader.download_aweme_assets(
                    aweme_id="123",
                    sec_user_id="sec",
                    user_nickname="user",
                    aweme_detail=self._detail(),
                    headers={"Cookie": "x=1"},
                )
        self.assertEqual(result["video_source"]["quality_source"], QUALITY_ORIGINAL)
        self.assertEqual(result["video_source"]["gear_name"], "original")
        download_mock.assert_awaited()
        # 归档走原画：现在同一作品还会再下一个网页播放用的「快播」版本，
        # 所以原画是第一次调用，快播是第二次。
        self.assertEqual(download_mock.await_args_list[0].args[0], original_url)
        self.assertGreaterEqual(len(download_mock.await_args_list), 2)
        self.assertNotEqual(download_mock.await_args_list[1].args[0], original_url)
        self.assertEqual(result["video_source"]["gear_name"], "original")
        self.assertIn("fast_play_file", result)

    async def test_falls_back_to_bit_rate_when_original_fails(self):
        with patch(
            "douyin_user_monitor.monitor.downloader.resolve_original_play_url",
            new=AsyncMock(return_value=None),
        ):
            with patch.object(
                self.downloader,
                "_download_video",
                new=AsyncMock(return_value=True),
            ) as download_mock:
                result = await self.downloader.download_aweme_assets(
                    aweme_id="123",
                    sec_user_id="sec",
                    user_nickname="user",
                    aweme_detail=self._detail(),
                    headers={},
                )
        self.assertEqual(result["video_source"]["quality_source"], QUALITY_BIT_RATE)
        self.assertEqual(result["video_source"]["gear_name"], "normal_1080")
        self.assertEqual(download_mock.await_args.args[0], "https://cdn/1080.mp4")


if __name__ == "__main__":
    unittest.main()
