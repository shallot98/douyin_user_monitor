from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, patch

from douyin_user_monitor.monitor.archive_feed import build_archive_item
from douyin_user_monitor.monitor.downloader import AwemeAssetDownloader
from douyin_user_monitor.monitor.playback_media import PlaybackMedia
from douyin_user_monitor.monitor.playback_prepare import PreparedPlayback
from douyin_user_monitor.monitor.video_source import (
    FAST_PLAY_MIN_SHORT_SIDE,
    extract_cover_url,
    select_fast_play_source,
)


def gear(bit_rate: int, width: int, height: int, url: str, name: str = "") -> Dict[str, Any]:
    return {
        "bit_rate": bit_rate,
        "gear_name": name or f"gear_{height}",
        "is_h265": 0,
        "is_bytevc1": 0,
        "play_addr": {"url_list": [url], "width": width, "height": height},
    }


class SelectFastPlayTests(unittest.TestCase):
    def test_picks_lowest_bitrate_above_watchable_floor(self):
        video = {
            "bit_rate": [
                gear(3_000_000, 1080, 1920, "https://cdn/1080.mp4"),
                gear(900_000, 540, 960, "https://cdn/540.mp4"),
                gear(1_500_000, 720, 1280, "https://cdn/720.mp4"),
            ]
        }
        source = select_fast_play_source(video)
        # 540 短边 >= 480，且码率最低
        self.assertEqual(source.url, "https://cdn/540.mp4")

    def test_ignores_gears_below_floor_when_better_exists(self):
        video = {
            "bit_rate": [
                gear(200_000, 240, 426, "https://cdn/240.mp4"),
                gear(1_500_000, 720, 1280, "https://cdn/720.mp4"),
            ]
        }
        self.assertEqual(select_fast_play_source(video).url, "https://cdn/720.mp4")

    def test_falls_back_to_any_gear_when_all_below_floor(self):
        video = {"bit_rate": [gear(200_000, 240, 426, "https://cdn/240.mp4")]}
        self.assertEqual(select_fast_play_source(video).url, "https://cdn/240.mp4")

    def test_unlabelled_bitrate_is_not_treated_as_smallest(self):
        video = {
            "bit_rate": [
                gear(0, 720, 1280, "https://cdn/unknown.mp4"),
                gear(1_200_000, 720, 1280, "https://cdn/known.mp4"),
            ]
        }
        self.assertEqual(select_fast_play_source(video).url, "https://cdn/known.mp4")

    def test_no_bitrate_list_returns_none(self):
        self.assertIsNone(select_fast_play_source({}))
        self.assertIsNone(select_fast_play_source({"bit_rate": "junk"}))

    def test_floor_constant_is_sane(self):
        self.assertEqual(FAST_PLAY_MIN_SHORT_SIDE, 480)


class ExtractCoverTests(unittest.TestCase):
    def test_prefers_cover_then_origin_then_dynamic(self):
        self.assertEqual(
            extract_cover_url({"video": {"cover": {"url_list": ["https://p/c.jpg"]}}}),
            "https://p/c.jpg",
        )
        self.assertEqual(
            extract_cover_url({"video": {"origin_cover": {"url_list": ["https://p/o.jpg"]}}}),
            "https://p/o.jpg",
        )
        self.assertEqual(
            extract_cover_url({"video": {"dynamic_cover": {"url_list": ["https://p/d.jpg"]}}}),
            "https://p/d.jpg",
        )

    def test_missing_video_block(self):
        self.assertIsNone(extract_cover_url({}))
        self.assertIsNone(extract_cover_url({"video": "junk"}))


def video_detail() -> Dict[str, Any]:
    return {
        "aweme_id": "9001",
        "desc": "demo",
        "create_time": 1_700_000_000,
        "aweme_type": 0,
        "video": {
            "duration": 12000,
            "cover": {"url_list": ["https://p/cover.jpg"]},
            "play_addr": {"uri": "v1", "url_list": ["https://cdn/default.mp4"], "width": 720, "height": 1280},
            "bit_rate": [
                gear(3_000_000, 1080, 1920, "https://cdn/1080.mp4"),
                gear(900_000, 540, 960, "https://cdn/540.mp4"),
            ],
        },
    }


class DownloaderExtrasTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.downloader = AwemeAssetDownloader(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    async def _prepared(self, request):
        target = request.archive_path
        if request.candidate_path is not None:
            target = request.target_path
            target.write_bytes(request.candidate_path.read_bytes())
        return PreparedPlayback(
            path=target, method="verified", created=target != request.archive_path,
            media=PlaybackMedia(
                width=540, height=960, bit_rate=900_000, duration=12,
                video_codec="h264", pixel_format="yuv420p", audio_codecs=("aac",), is_mp4=True, faststart=True,
            ),
        )

    async def _run(self, detail: Dict[str, Any]):
        async def fake_video(url, headers, target, *, client=None):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"V" * 10)
            return True

        async def fake_image(client, url, headers, target):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"I" * 5)

        with (
            patch.object(self.downloader, "_download_video", new=AsyncMock(side_effect=fake_video)) as vmock,
            patch.object(self.downloader, "_download_image_once", new=AsyncMock(side_effect=fake_image)) as imock,
            patch.object(self.downloader, "_prepare_playback", new=AsyncMock(side_effect=self._prepared)),
            patch(
                "douyin_user_monitor.monitor.downloader.resolve_original_play_url",
                new=AsyncMock(return_value=None),
            ),
        ):
            result = await self.downloader.download_aweme_assets(
                aweme_id=str(detail["aweme_id"]),
                sec_user_id="sec",
                user_nickname="u",
                aweme_detail=detail,
                headers={"Cookie": "x=1"},
            )
        return result, vmock, imock

    async def test_video_gets_archive_plus_fast_play_plus_cover(self):
        result, vmock, imock = await self._run(video_detail())
        self.assertEqual(result["media_type"], "video")
        self.assertIn("video_file", result)
        self.assertIn("fast_play_file", result)
        self.assertIn("cover_file", result)
        self.assertTrue(result["fast_play_file"].endswith(".fast.mp4"))
        self.assertTrue(result["cover_file"].endswith(".cover.jpg"))
        # 归档 + 快播 = 两次视频下载；封面一次图片下载
        self.assertEqual(vmock.await_count, 2)
        self.assertEqual(imock.await_count, 1)
        # 三个文件都在 files 里，网页端按下标取用
        self.assertEqual(len(result["files"]), 3)
        self.assertEqual(result["fast_play_source"]["height"], 960)
        for role in ("video_file", "fast_play_file", "cover_file"):
            self.assertTrue((self.root / result[role]).is_file(), role)

    async def test_download_keeps_other_work_media_and_empty_directories(self):
        other_user = self.root / "other-user"
        other_user.mkdir()
        original = other_user / "original.mp4"
        original.write_bytes(b"original retained for verified mover")
        in_progress = other_user / "in-progress"
        in_progress.mkdir()

        result, _, _ = await self._run(video_detail())

        self.assertEqual(original.read_bytes(), b"original retained for verified mover")
        self.assertTrue(in_progress.is_dir())
        self.assertTrue(all((self.root / name).is_file() for name in result["files"]))

    async def test_no_duplicate_when_archive_already_is_fast_gear(self):
        detail = video_detail()
        # 只有一个档位：归档与快播是同一个地址，不该重复存
        detail["video"]["bit_rate"] = [gear(900_000, 540, 960, "https://cdn/540.mp4")]
        result, vmock, _ = await self._run(detail)
        self.assertEqual(result["fast_play_file"], result["video_file"])
        self.assertEqual(vmock.await_count, 1)
        self.assertIn("cover_file", result)
        self.assertEqual(len(result["files"]), 2)
        self.assertEqual(result["downloaded_file_count"], 2)
        self.assertEqual(result["existing_file_count"], 0)
        self.assertEqual(result["total_size_bytes"], 15)

    async def test_cover_failure_surfaces_and_keeps_archive(self):
        async def fake_video(url, headers, target, *, client=None):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"V")
            return True

        with (
            patch.object(self.downloader, "_download_video", new=AsyncMock(side_effect=fake_video)),
            patch.object(self.downloader, "_prepare_playback", new=AsyncMock(side_effect=self._prepared)),
            patch.object(
                self.downloader, "_download_image_once", new=AsyncMock(side_effect=RuntimeError("cover boom"))
            ),
            patch(
                "douyin_user_monitor.monitor.downloader.resolve_original_play_url",
                new=AsyncMock(return_value=None),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "cover boom"):
                await self.downloader.download_aweme_assets(
                    aweme_id="9001", sec_user_id="sec", user_nickname="u", aweme_detail=video_detail(), headers={},
                )
        archive = self.root / "u" / f"{self.downloader._build_aweme_filename(video_detail())}.mp4"
        self.assertEqual(archive.read_bytes(), b"V")
        self.assertEqual(list(self.root.rglob("*.part")), [])

    async def test_image_post_also_stores_small_cover(self):
        detail = {
            "aweme_id": "9002",
            "desc": "note",
            "create_time": 1_700_000_001,
            "aweme_type": 68,
            "images": [{"url_list": ["https://p/1.jpg"]}, {"url_list": ["https://p/2.jpg"]}],
            "video": {"cover": {"url_list": ["https://p/cover.jpg"]}},
        }
        result, _, imock = await self._run(detail)
        self.assertEqual(result["media_type"], "image")
        self.assertIn("cover_file", result)
        self.assertTrue(result["cover_file"].endswith("cover.jpg"))
        # 2 张内容图 + 1 张封面
        self.assertEqual(imock.await_count, 3)
        self.assertEqual(len(result["files"]), 3)
        self.assertTrue(all((self.root / name).is_file() for name in result["files"]))

    async def test_cover_without_url_or_saved_media_fails(self):
        with self.assertRaisesRegex(ValueError, "缺少上游链接或可用媒体源"):
            await self.downloader._download_cover({}, {}, self.root / "cover.jpg", client=None)


class ArchiveItemRoleTests(unittest.TestCase):
    def test_play_index_prefers_fast_play(self):
        record = {
            "aweme_id": "1",
            "media_type": "video",
            "files": ["u/a.mp4", "u/a.fast.mp4", "u/a.cover.jpg"],
            "video_file": "u/a.mp4",
            "fast_play_file": "u/a.fast.mp4",
            "cover_file": "u/a.cover.jpg",
        }
        item = build_archive_item(record)
        self.assertEqual(item["play_index"], 1)
        self.assertEqual(item["archive_index"], 0)
        self.assertEqual(item["cover_index"], 2)
        self.assertTrue(item["has_fast_play"])

    def test_legacy_record_falls_back_to_first_file(self):
        record = {"aweme_id": "1", "media_type": "video", "files": ["u/a.mp4"]}
        item = build_archive_item(record)
        self.assertEqual(item["play_index"], 0)
        self.assertEqual(item["archive_index"], 0)
        self.assertIsNone(item["cover_index"])
        self.assertFalse(item["has_fast_play"])

    def test_image_indices_exclude_cover(self):
        record = {
            "aweme_id": "2",
            "media_type": "image",
            "files": ["u/图片/t/1.jpg", "u/图片/t/2.jpg", "u/图片/t/cover.jpg"],
            "cover_file": "u/图片/t/cover.jpg",
        }
        item = build_archive_item(record)
        self.assertEqual(item["cover_index"], 2)
        self.assertEqual(item["image_indices"], [0, 1])
        self.assertIsNone(item["play_index"])

    def test_legacy_image_record_uses_first_image_as_cover(self):
        record = {"aweme_id": "2", "media_type": "image", "files": ["u/图片/t/1.jpg", "u/图片/t/2.jpg"]}
        item = build_archive_item(record)
        self.assertEqual(item["cover_index"], 0)
        self.assertEqual(item["image_indices"], [0, 1])


if __name__ == "__main__":
    unittest.main()
