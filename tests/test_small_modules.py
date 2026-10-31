"""此前没有直接测试的小模块：schedule、media_urls、cover_image、sync_utils、timeutils、telegram_notifier。"""

import json
import re
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import httpx
from PIL import Image

from douyin_user_monitor.monitor import schedule
from douyin_user_monitor.monitor.cover_image import COVER_MAX_SIDE, normalize_cover
from douyin_user_monitor.monitor.media_urls import extract_image_urls, is_image_post
from douyin_user_monitor.monitor.sync_utils import (
    MAX_DOWNLOAD_RECORDS_PER_USER,
    format_publish_time,
    merge_download_records,
    merge_downloaded_ids,
)
from douyin_user_monitor.monitor.telegram_notifier import TelegramNotifier
from douyin_user_monitor.monitor.timeutils import cn_now, safe_int


class ScheduleTests(unittest.TestCase):
    def test_validate_options_rejects_unknown_mode_and_too_small_values(self):
        schedule.validate_options(mode="coverage", interval_hours=1, coverage_hours=12)
        for kwargs in ({"mode": "x", "interval_hours": 1, "coverage_hours": 12},
                       {"mode": "interval", "interval_hours": 0, "coverage_hours": 12},
                       {"mode": "coverage", "interval_hours": 1, "coverage_hours": 0.01}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                schedule.validate_options(**kwargs)

    def test_coverage_delay_bounds(self):
        self.assertEqual(schedule.choose_coverage_delay(remaining_seconds=0, future_gap_count=5), 0.0)
        self.assertEqual(schedule.choose_coverage_delay(remaining_seconds=60, future_gap_count=0), 60)
        for _ in range(200):
            delay = schedule.choose_coverage_delay(remaining_seconds=100, future_gap_count=9)
            self.assertTrue(0 <= delay <= 100)

    def test_coverage_delay_mean_spreads_remaining_time(self):
        with patch.object(schedule.random, "betavariate", return_value=0.1):
            self.assertAlmostEqual(schedule.choose_coverage_delay(remaining_seconds=100, future_gap_count=9), 10)


class MediaUrlsTests(unittest.TestCase):
    def test_collects_both_image_sources_dedup_in_order(self):
        detail = {
            "images": [{"url_list": ["https://a/1", "https://a/1b"]}, {"url_list": []}, "bad"],
            "image_post_info": {"images": [{"display_image": {"url_list": ["https://a/1"]}},
                                           {"display_image": {"url_list": ["https://a/2"]}}]},
        }
        self.assertEqual(extract_image_urls(detail), ["https://a/1", "https://a/2"])

    def test_is_image_post_signals(self):
        self.assertTrue(is_image_post({"images": [{"url_list": ["https://a/1"]}]}))
        self.assertTrue(is_image_post({"aweme_type": "68"}))
        self.assertTrue(is_image_post({"video": {"play_addr": {"uri": "https://x/ies-music/a"}}}))
        self.assertTrue(is_image_post({"video": {"play_addr": {"uri": "a.mp3"}}}))
        self.assertFalse(is_image_post({"aweme_type": 0, "video": {"play_addr": {"uri": "v0200fg"}}}))
        self.assertFalse(is_image_post({"aweme_type": "not-a-number", "image_post_info": "bad"}))


class CoverImageTests(unittest.TestCase):
    def _write(self, image: Image.Image, fmt: str) -> Path:
        tmp = tempfile.NamedTemporaryFile(suffix=f".{fmt.lower()}", delete=False)
        self.addCleanup(Path(tmp.name).unlink)
        image.save(tmp, fmt)
        tmp.close()
        return Path(tmp.name)

    def test_large_image_is_shrunk_to_jpeg(self):
        cover = normalize_cover(self._write(Image.new("RGB", (2000, 1000), "red"), "PNG"))
        self.assertEqual(max(cover.width, cover.height), COVER_MAX_SIDE)
        self.assertTrue(cover.data.startswith(b"\xff\xd8"))
        with Image.open(BytesIO(cover.data)) as decoded:
            self.assertEqual(decoded.format, "JPEG")

    def test_transparency_is_flattened_on_white(self):
        rgba = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
        cover = normalize_cover(self._write(rgba, "PNG"))
        with Image.open(BytesIO(cover.data)) as decoded:
            r, g, b = decoded.convert("RGB").getpixel((5, 5))
        self.assertTrue(min(r, g, b) > 240)

    def test_corrupt_file_raises(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        self.addCleanup(Path(tmp.name).unlink)
        tmp.write(b"\xff\xd8not really a jpeg")
        tmp.close()
        with self.assertRaises(Exception):
            normalize_cover(Path(tmp.name))


class SyncUtilsTests(unittest.TestCase):
    def test_format_publish_time(self):
        self.assertEqual(format_publish_time(1700000000), "2023-11-15T06:13:20+08:00")
        self.assertIsNone(format_publish_time(0))
        self.assertIsNone(format_publish_time("bad"))

    def test_merge_downloaded_ids_keeps_order_and_drops_blanks(self):
        self.assertEqual(merge_downloaded_ids(["1", " 2 "], ["2", "", "3"]), ["1", "2", "3"])

    def test_merge_download_records_newest_first_and_capped(self):
        existing = [{"aweme_id": "2"}, {"aweme_id": "1"}]
        merged = merge_download_records(existing, [{"aweme_id": "3"}, {"aweme_id": "4"}, {"aweme_id": "2", "x": 1}])
        self.assertEqual([r["aweme_id"] for r in merged], ["2", "4", "3", "1"])
        self.assertEqual(merged[0], {"aweme_id": "2", "x": 1})
        many = [{"aweme_id": str(i)} for i in range(MAX_DOWNLOAD_RECORDS_PER_USER + 10)]
        self.assertEqual(len(merge_download_records(many, [])), MAX_DOWNLOAD_RECORDS_PER_USER)


class TimeutilsTests(unittest.TestCase):
    def test_cn_now_is_china_offset_with_millis(self):
        self.assertRegex(cn_now(), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}\+08:00$")

    def test_safe_int(self):
        self.assertEqual(safe_int("7"), 7)
        self.assertEqual(safe_int(None), 0)
        self.assertEqual(safe_int("x", default=-1), -1)


class TelegramNotifierTests(unittest.IsolatedAsyncioTestCase):
    def _notifier(self, handler):
        notifier = TelegramNotifier(bot_token="T0K", chat_id="42", api_base="https://tg.test/", timeout_seconds=5)
        notifier._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: SLF001
        self.addAsyncCleanup(notifier.aclose)
        return notifier

    async def test_download_completed_message_is_posted(self):
        sent = []

        def handler(request):
            sent.append((str(request.url), json.loads(request.content)))
            return httpx.Response(200, json={"ok": True})

        await self._notifier(handler).notify_download_completed(user_nickname="小美", record={
            "media_type": "image", "image_count": 3, "desc": "标题", "total_size_bytes": 3 * 1024 * 1024,
            "downloaded_at": "2026-10-31T01:00:04.163+08:00",
        })
        url, body = sent[0]
        self.assertEqual(url, "https://tg.test/botT0K/sendMessage")
        self.assertEqual(body["chat_id"], "42")
        self.assertIn("图片集 (3张)", body["text"])
        self.assertIn("3.00 MB", body["text"])
        self.assertIn("2026-10-31 01:00:04", body["text"])

    async def test_detected_message_counts_images_and_formats_time(self):
        sent = []
        notifier = self._notifier(lambda request: sent.append(json.loads(request.content)) or
                                  httpx.Response(200, json={"ok": True}))
        await notifier.notify_new_aweme_detected(user_nickname="u", aweme_detail={
            "aweme_id": "9", "desc": "", "create_time": 1700000000, "images": [{}, {}],
        })
        text = sent[0]["text"]
        self.assertIn("图片集 (2张)", text)
        self.assertIn("📝 描述: -", text)
        self.assertIn("2023-11-15 06:13:20", text)

    async def test_status_change_message(self):
        sent = []
        notifier = self._notifier(lambda request: sent.append(json.loads(request.content)) or
                                  httpx.Response(200, json={"ok": True}))
        await notifier.notify_account_status_changed(user_nickname="u", old_status="normal", new_status="banned",
                                                     reason="违规")
        self.assertTrue(re.search(r"🚫 账号状态变更.*正常 → 已封禁.*原因: 违规", sent[0]["text"], re.S))

    async def test_api_failure_raises_with_description(self):
        notifier = self._notifier(lambda request: httpx.Response(400, json={"ok": False, "description": "chat not found"}))
        with self.assertRaisesRegex(RuntimeError, "chat not found"):
            await notifier.notify_new_aweme_detected(user_nickname="u", aweme_detail={})

    async def test_non_json_reply_raises(self):
        notifier = self._notifier(lambda request: httpx.Response(502, text="<html>bad gateway</html>"))
        with self.assertRaisesRegex(RuntimeError, "非 JSON"):
            await notifier.notify_new_aweme_detected(user_nickname="u", aweme_detail={})


if __name__ == "__main__":
    unittest.main()
