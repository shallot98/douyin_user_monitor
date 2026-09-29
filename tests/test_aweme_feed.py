from __future__ import annotations

import unittest
from typing import Any, Dict, List
from unittest.mock import AsyncMock

from douyin_user_monitor.monitor.aweme_feed import (
    DEFAULT_FEED_COUNT,
    MAX_FEED_COUNT,
    AwemeFeedService,
    PlayUrlCache,
    build_feed_page,
    clamp_cursor,
    clamp_feed_count,
    extract_cover_url,
)


def _video_aweme(*, aweme_id: str = "7001", create_time: int = 1_710_000_000) -> Dict[str, Any]:
    return {
        "aweme_id": aweme_id,
        "aweme_type": 0,
        "desc": "video desc",
        "create_time": create_time,
        "statistics": {"digg_count": 12345, "comment_count": 7, "collect_count": 3, "share_count": 1},
        "video": {
            "duration": 11800,
            "cover": {"url_list": ["https://p3.example/cover.jpg"]},
            "origin_cover": {"url_list": ["https://p3.example/origin.jpg"]},
            "play_addr": {
                "uri": "v0300fg10000abc",
                "url_list": ["https://v5.example/fallback.mp4"],
                "width": 720,
                "height": 1280,
            },
            "bit_rate": [
                {
                    "bit_rate": 2_000_000,
                    "gear_name": "normal_1080",
                    "play_addr": {
                        "uri": "v0300fg10000abc",
                        "url_list": ["https://v5.example/bit.mp4"],
                        "width": 1080,
                        "height": 1920,
                    },
                }
            ],
        },
    }


def _image_aweme(*, aweme_id: str = "7002", create_time: int = 1_710_009_999) -> Dict[str, Any]:
    return {
        "aweme_id": aweme_id,
        "aweme_type": 68,
        "desc": "note desc",
        "create_time": create_time,
        "statistics": {"digg_count": 20},
        "images": [
            {"url_list": ["https://p3.example/img1.jpg"]},
            {"url_list": ["https://p3.example/img2.jpg"]},
            {"url_list": ["https://p3.example/img3.jpg"]},
        ],
    }


class ClampTests(unittest.TestCase):
    def test_feed_count_defaults_and_caps(self):
        self.assertEqual(clamp_feed_count(None), DEFAULT_FEED_COUNT)
        self.assertEqual(clamp_feed_count("abc"), DEFAULT_FEED_COUNT)
        self.assertEqual(clamp_feed_count(0), DEFAULT_FEED_COUNT)
        self.assertEqual(clamp_feed_count(-5), DEFAULT_FEED_COUNT)
        self.assertEqual(clamp_feed_count(5), 5)
        self.assertEqual(clamp_feed_count(999), MAX_FEED_COUNT)

    def test_cursor_normalizes_to_non_negative_int(self):
        self.assertEqual(clamp_cursor(None), 0)
        self.assertEqual(clamp_cursor("x"), 0)
        self.assertEqual(clamp_cursor(-1), 0)
        self.assertEqual(clamp_cursor("1787611006000"), 1787611006000)


class CoverExtractionTests(unittest.TestCase):
    def test_prefers_video_cover(self):
        self.assertEqual(extract_cover_url(_video_aweme()), "https://p3.example/cover.jpg")

    def test_falls_back_to_first_image_for_image_post(self):
        self.assertEqual(extract_cover_url(_image_aweme()), "https://p3.example/img1.jpg")

    def test_returns_none_without_any_media(self):
        self.assertIsNone(extract_cover_url({"aweme_id": "1"}))


class BuildFeedPageTests(unittest.TestCase):
    def test_normalizes_video_and_image_items(self):
        page = {
            "aweme_list": [_video_aweme(), _image_aweme()],
            "has_more": 1,
            "max_cursor": 1787611006000,
        }
        result = build_feed_page(page, downloaded_ids=["7002"])

        # 按 create_time 倒序：图文 create_time 更大，应排前面
        self.assertEqual([item["aweme_id"] for item in result["items"]], ["7002", "7001"])
        self.assertTrue(result["has_more"])
        self.assertEqual(result["next_cursor"], 1787611006000)

        image_item, video_item = result["items"]
        self.assertEqual(image_item["media_type"], "image")
        self.assertEqual(image_item["image_count"], 3)
        self.assertEqual(len(image_item["image_urls"]), 3)
        self.assertEqual(image_item["duration_ms"], 0)
        self.assertTrue(image_item["downloaded"])

        self.assertEqual(video_item["media_type"], "video")
        self.assertEqual(video_item["image_urls"], [])
        self.assertEqual(video_item["duration_ms"], 11800)
        self.assertFalse(video_item["downloaded"])
        self.assertEqual(video_item["statistics"]["digg_count"], 12345)
        self.assertEqual(video_item["share_url"], "https://www.douyin.com/video/7001")
        self.assertIsNotNone(video_item["publish_time"])

    def test_has_more_false_when_cursor_missing(self):
        page = {"aweme_list": [_video_aweme()], "has_more": 1, "max_cursor": 0}
        result = build_feed_page(page, downloaded_ids=[])
        self.assertFalse(result["has_more"])
        self.assertIsNone(result["next_cursor"])

    def test_skips_items_without_aweme_id_and_non_dicts(self):
        page = {"aweme_list": [{"desc": "no id"}, "junk", _video_aweme()], "has_more": 0}
        result = build_feed_page(page, downloaded_ids=[])
        self.assertEqual([item["aweme_id"] for item in result["items"]], ["7001"])

    def test_empty_page_is_safe(self):
        result = build_feed_page({}, downloaded_ids=[])
        self.assertEqual(result["items"], [])
        self.assertFalse(result["has_more"])


class PlayUrlCacheTests(unittest.TestCase):
    def test_expires_after_ttl(self):
        now = {"value": 0.0}
        cache = PlayUrlCache(ttl_seconds=10.0, clock=lambda: now["value"])
        cache.put("a", "https://v/a.mp4")
        self.assertEqual(cache.get("a"), "https://v/a.mp4")
        now["value"] = 10.0
        self.assertIsNone(cache.get("a"))

    def test_evicts_oldest_beyond_max_entries(self):
        cache = PlayUrlCache(ttl_seconds=100.0, max_entries=2, clock=lambda: 0.0)
        cache.put("a", "https://v/a.mp4")
        cache.put("b", "https://v/b.mp4")
        cache.put("c", "https://v/c.mp4")
        self.assertIsNone(cache.get("a"))
        self.assertEqual(cache.get("b"), "https://v/b.mp4")
        self.assertEqual(cache.get("c"), "https://v/c.mp4")

    def test_ignores_blank_keys_and_values(self):
        cache = PlayUrlCache(clock=lambda: 0.0)
        cache.put("", "https://v/a.mp4")
        cache.put("a", "  ")
        self.assertIsNone(cache.get(""))
        self.assertIsNone(cache.get("a"))


class FakeFeedCrawler:
    def __init__(self, page: Dict[str, Any], detail_payload: Dict[str, Any] | None = None):
        self.fetch_user_post_videos = AsyncMock(return_value=page)
        self.fetch_one_video = AsyncMock(return_value=detail_payload or {})
        self.calls: List[Any] = []


class AwemeFeedServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_clamps_arguments_and_marks_downloaded(self):
        page = {"aweme_list": [_video_aweme()], "has_more": 1, "max_cursor": 42}
        crawler = FakeFeedCrawler(page)
        service = AwemeFeedService(crawler)

        result = await service.list_user_awemes("sec-1", cursor=-9, count=999, downloaded_ids=["7001"])

        crawler.fetch_user_post_videos.assert_awaited_once_with("sec-1", 0, MAX_FEED_COUNT)
        self.assertTrue(result["items"][0]["downloaded"])

    async def test_list_caches_play_url_so_playback_needs_no_extra_request(self):
        page = {"aweme_list": [_video_aweme()], "has_more": 0}
        crawler = FakeFeedCrawler(page)
        service = AwemeFeedService(crawler)

        await service.list_user_awemes("sec-1")
        url = await service.resolve_play_url("7001")

        self.assertEqual(url, "https://v5.example/bit.mp4")
        crawler.fetch_one_video.assert_not_awaited()

    async def test_resolve_falls_back_to_detail_fetch_on_cache_miss(self):
        crawler = FakeFeedCrawler(
            {"aweme_list": [], "has_more": 0},
            detail_payload={"aweme_detail": _video_aweme(aweme_id="7009")},
        )
        service = AwemeFeedService(crawler)

        url = await service.resolve_play_url("7009")

        self.assertEqual(url, "https://v5.example/bit.mp4")
        crawler.fetch_one_video.assert_awaited_once_with("7009")

    async def test_image_posts_are_not_cached_as_playable(self):
        page = {"aweme_list": [_image_aweme()], "has_more": 0}
        crawler = FakeFeedCrawler(page, detail_payload={"aweme_detail": _image_aweme()})
        service = AwemeFeedService(crawler)
        await service.list_user_awemes("sec-1")

        with self.assertRaises(ValueError) as ctx:
            await service.resolve_play_url("7002")
        self.assertIn("图文", str(ctx.exception))

    async def test_resolve_requires_aweme_id(self):
        service = AwemeFeedService(FakeFeedCrawler({"aweme_list": []}))
        with self.assertRaises(ValueError):
            await service.resolve_play_url("  ")

    async def test_list_rejects_invalid_page_shape(self):
        crawler = FakeFeedCrawler({})
        crawler.fetch_user_post_videos = AsyncMock(return_value="not-a-dict")
        service = AwemeFeedService(crawler)
        with self.assertRaises(ValueError):
            await service.list_user_awemes("sec-1")


if __name__ == "__main__":
    unittest.main()
