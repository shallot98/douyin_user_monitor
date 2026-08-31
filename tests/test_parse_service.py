from __future__ import annotations

import unittest
from typing import Any, Dict
from unittest.mock import AsyncMock, patch

from douyin_user_monitor.monitor.parse_service import ParseService


class FakeCrawler:
    def __init__(self, payload: Dict[str, Any]):
        self.payload = payload
        self.fetch_one_video = AsyncMock(return_value=payload)
        self.get_douyin_headers = AsyncMock(return_value={"headers": {"Cookie": "x=1", "User-Agent": "test"}})


def _video_detail(*, aweme_id: str = "7123456789") -> Dict[str, Any]:
    return {
        "aweme_id": aweme_id,
        "aweme_type": 0,
        "desc": "demo video",
        "create_time": 1710000000,
        "author": {"nickname": "alice", "uid": "u1", "sec_uid": "sec1"},
        "video": {
            "play_addr": {
                "uri": "v2800fgi0000d7meamvog65qpql92v50",
                "url_list": ["https://cdn.example/fallback.mp4"],
                "width": 1080,
                "height": 1920,
            },
            "bit_rate": [
                {
                    "bit_rate": 2_000_000,
                    "gear_name": "normal_1080",
                    "play_addr": {
                        "uri": "v2800fgi0000d7meamvog65qpql92v50",
                        "url_list": ["https://cdn.example/bit.mp4"],
                        "width": 1080,
                        "height": 1920,
                    },
                }
            ],
        },
    }


def _image_detail(*, aweme_id: str = "7987654321") -> Dict[str, Any]:
    return {
        "aweme_id": aweme_id,
        "aweme_type": 68,
        "desc": "demo note",
        "create_time": 1710001111,
        "author": {"nickname": "bob", "uid": "u2", "sec_uid": "sec2"},
        "images": [
            {"url_list": ["https://cdn.example/img1.jpg"]},
            {"url_list": ["https://cdn.example/img2.jpg"]},
        ],
        "video": {
            # music-only shell should not force video original path
            "play_addr": {"uri": "https://sf3-cdn-tos.douyinstatic.com/ies-music/x.mp3", "url_list": []},
        },
    }


class ParseServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_valid_url(self):
        service = ParseService(FakeCrawler({}))
        with self.assertRaises(ValueError) as ctx:
            await service.parse("没有链接的口令纯文本")
        self.assertIn("未找到有效抖音链接", str(ctx.exception))

    async def test_video_original_success(self):
        detail = _video_detail()
        crawler = FakeCrawler({"aweme_detail": detail})
        service = ParseService(crawler)
        with (
            patch(
                "douyin_user_monitor.monitor.parse_service.AwemeIdFetcher.get_aweme_id",
                new=AsyncMock(return_value="7123456789"),
            ),
            patch(
                "douyin_user_monitor.monitor.parse_service.resolve_original_play_url",
                new=AsyncMock(return_value="https://cdn.example/original.mp4"),
            ) as resolve_mock,
        ):
            result = await service.parse("看这个 https://v.douyin.com/AbCdEf/")

        self.assertEqual(result["aweme_id"], "7123456789")
        self.assertFalse(result["is_image_post"])
        self.assertEqual(result["quality_source"], "original")
        self.assertEqual(result["download_url"], "https://cdn.example/original.mp4")
        self.assertEqual(result["original_play_url"], "https://cdn.example/original.mp4")
        self.assertEqual(result["resolved_from"], "https://v.douyin.com/AbCdEf/")
        self.assertIsInstance(result["video_source"], dict)
        self.assertIn("url", result["video_source"])
        self.assertTrue(str(result["video_source"]["url"]).startswith("https://"))
        self.assertIsNone(result["raw"])
        self.assertEqual(result["author"]["nickname"], "alice")
        resolve_mock.assert_awaited()
        crawler.fetch_one_video.assert_awaited_with("7123456789")

    async def test_video_original_fail_falls_back_bit_rate(self):
        detail = _video_detail(aweme_id="7001")
        crawler = FakeCrawler({"aweme_detail": detail})
        service = ParseService(crawler)
        with (
            patch(
                "douyin_user_monitor.monitor.parse_service.AwemeIdFetcher.get_aweme_id",
                new=AsyncMock(return_value="7001"),
            ),
            patch(
                "douyin_user_monitor.monitor.parse_service.resolve_original_play_url",
                new=AsyncMock(return_value=None),
            ),
        ):
            result = await service.parse("https://www.douyin.com/video/7001")

        self.assertEqual(result["aweme_id"], "7001")
        self.assertIsNone(result["original_play_url"])
        self.assertEqual(result["quality_source"], "bit_rate")
        self.assertEqual(result["download_url"], "https://cdn.example/bit.mp4")
        self.assertEqual(result["video_source"]["url"], "https://cdn.example/bit.mp4")
        self.assertEqual(result["video_source"]["quality_source"], "bit_rate")

    async def test_image_post(self):
        detail = _image_detail()
        crawler = FakeCrawler({"aweme_detail": detail})
        service = ParseService(crawler)
        with (
            patch(
                "douyin_user_monitor.monitor.parse_service.AwemeIdFetcher.get_aweme_id",
                new=AsyncMock(return_value="7987654321"),
            ),
            patch(
                "douyin_user_monitor.monitor.parse_service.resolve_original_play_url",
                new=AsyncMock(return_value="https://should-not-be-called"),
            ) as resolve_mock,
        ):
            result = await service.parse("https://www.douyin.com/note/7987654321", include_raw=True)

        self.assertTrue(result["is_image_post"])
        self.assertEqual(result["quality_source"], "image")
        self.assertEqual(result["image_urls"], ["https://cdn.example/img1.jpg", "https://cdn.example/img2.jpg"])
        self.assertEqual(result["download_url"], "https://cdn.example/img1.jpg")
        self.assertIsNone(result["original_play_url"])
        self.assertIsNone(result["video_source"])
        self.assertIsInstance(result["raw"], dict)
        resolve_mock.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
