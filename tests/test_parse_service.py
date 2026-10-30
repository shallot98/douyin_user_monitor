from __future__ import annotations

import unittest
from pathlib import Path
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



class LocalAwemeIdTests(unittest.TestCase):
    def test_long_links_resolve_without_network(self):
        from douyin_user_monitor.monitor.parse_service import local_aweme_id

        cases = {
            "https://www.iesdouyin.com/share/video/7687999745575033715/?region=CN": "7687999745575033715",
            "https://www.douyin.com/video/7687999745575033715": "7687999745575033715",
            "https://www.douyin.com/note/7600000000000000001?x=1": "7600000000000000001",
            "https://www.douyin.com/user/abc?modal_id=7612345678901234567": "7612345678901234567",
            "https://www.douyin.com/discover?vid=7612345678901234567": "7612345678901234567",
        }
        for url, expected in cases.items():
            self.assertEqual(local_aweme_id(url), expected, url)
        for url in ("https://v.douyin.com/AbCdEf/", "https://evil.example/video/7687999745575033715",
                    "https://www.douyin.com/user/abc", "https://notdouyin.com/video/7687999745575033715"):
            self.assertIsNone(local_aweme_id(url), url)


class ParseCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_parse_of_same_work_is_cached_and_long_link_skips_redirect(self):
        crawler = FakeCrawler({"aweme_detail": _video_detail()})
        service = ParseService(crawler)
        fetch_id = AsyncMock(return_value="7123456789")
        with (
            patch("douyin_user_monitor.monitor.parse_service.AwemeIdFetcher.get_aweme_id", new=fetch_id),
            patch("douyin_user_monitor.monitor.parse_service.resolve_original_play_url",
                  new=AsyncMock(return_value="https://cdn.example/original.mp4")) as resolve_mock,
        ):
            first = await service.parse("https://www.douyin.com/video/7123456789")
            first["download_url"] = "mutated"
            second = await service.parse("https://www.iesdouyin.com/share/video/7123456789/")
            raw = await service.parse("https://www.douyin.com/video/7123456789", include_raw=True)
        fetch_id.assert_not_awaited()
        self.assertEqual(crawler.fetch_one_video.await_count, 2, "缓存命中一次；include_raw 不走缓存")
        self.assertEqual(resolve_mock.await_count, 2)
        self.assertEqual(second["download_url"], "https://cdn.example/original.mp4", "缓存要深拷贝")
        self.assertEqual(second["resolved_from"], "https://www.iesdouyin.com/share/video/7123456789/")
        self.assertIsNotNone(raw["raw"])



class ParseCookiePoolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def _pool(self):
        from douyin_user_monitor.monitor.parse_cookies import ParseCookiePool

        return ParseCookiePool(self.dir)

    def test_round_robin_and_reload(self):
        (self.dir / "a.txt").write_text("ttwid=a; x=1\n", encoding="utf-8")
        (self.dir / "b.txt").write_text("ttwid=b", encoding="utf-8")
        (self.dir / "empty.txt").write_text("   ", encoding="utf-8")
        pool = self._pool()
        self.assertEqual(pool.count(), 2)
        self.assertEqual([pool.take()[1] for _ in range(3)], ["ttwid=a; x=1", "ttwid=b", "ttwid=a; x=1"])
        (self.dir / "c.txt").write_text("ttwid=c", encoding="utf-8")
        self.assertEqual(pool.count(), 3)

    async def test_empty_pool_fails_loudly_without_touching_monitor_cookie(self):
        from douyin_user_monitor.monitor.parse_cookies import ParseCookiesUnavailable

        crawler = FakeCrawler({"aweme_detail": _video_detail()})
        service = ParseService(crawler, cookie_pool=self._pool())
        with self.assertRaises(ParseCookiesUnavailable):
            await service.parse("https://www.douyin.com/video/7123456789")
        crawler.fetch_one_video.assert_not_awaited()

    async def test_parse_uses_dedicated_cookie_and_rotates_on_error(self):
        (self.dir / "a.txt").write_text("ttwid=a", encoding="utf-8")
        (self.dir / "b.txt").write_text("ttwid=b", encoding="utf-8")
        crawler = FakeCrawler({"aweme_detail": _video_detail()})
        crawler.fetch_one_video = AsyncMock(side_effect=[RuntimeError("boom"), {"aweme_detail": _video_detail()}])
        service = ParseService(crawler, cookie_pool=self._pool())
        with patch("douyin_user_monitor.monitor.parse_service.resolve_original_play_url",
                   new=AsyncMock(return_value="https://cdn.example/o.mp4")):
            result = await service.parse("https://www.douyin.com/video/7123456789")
        self.assertEqual(result["download_url"], "https://cdn.example/o.mp4")
        cookies = [call.kwargs.get("cookie") for call in crawler.fetch_one_video.await_args_list]
        self.assertEqual(cookies, ["ttwid=a", "ttwid=b"])
        crawler.get_douyin_headers.assert_awaited_with("ttwid=b")


if __name__ == "__main__":
    unittest.main()
