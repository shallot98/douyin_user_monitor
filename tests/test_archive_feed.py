from __future__ import annotations

import unittest
from typing import Any, Dict, List

from douyin_user_monitor.monitor.archive_feed import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    build_archive_item,
    build_archive_page,
    clamp_page,
    clamp_page_size,
    find_record,
    list_archive_files,
    resolve_archive_file,
)


def video_record(*, aweme_id: str = "7001", publish: str = "2026-08-29T15:00:36+08:00") -> Dict[str, Any]:
    return {
        "aweme_id": aweme_id,
        "desc": "视频描述",
        "publish_time": publish,
        "downloaded_at": "2026-08-30T06:32:45.992+08:00",
        "media_type": "video",
        "source": "incremental",
        "image_count": 0,
        "downloaded_file_count": 1,
        "total_size_bytes": 63266995,
        "files": [f"CutE1n/20260829_150036_{aweme_id}_aweme.mp4"],
        "video_duration_ms": 11800,
        "video_source": {"width": 720, "height": 1280, "quality_source": "original"},
    }


def image_record(*, aweme_id: str = "7002", publish: str = "2026-05-20T19:52:45+08:00") -> Dict[str, Any]:
    return {
        "aweme_id": aweme_id,
        "desc": "图文描述",
        "publish_time": publish,
        "downloaded_at": "2026-05-27T20:42:46+08:00",
        "media_type": "image",
        "image_count": 4,
        "total_size_bytes": 263500,
        "files": [f"柒柒🐿️/图片/20260520_195245_标题/{i}.jpg" for i in range(1, 5)],
    }


def user_with(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"id": "u1", "nickname": "CutE1n", "download_records": records}


class ClampTests(unittest.TestCase):
    def test_page_size(self):
        self.assertEqual(clamp_page_size(None), DEFAULT_PAGE_SIZE)
        self.assertEqual(clamp_page_size("x"), DEFAULT_PAGE_SIZE)
        self.assertEqual(clamp_page_size(0), DEFAULT_PAGE_SIZE)
        self.assertEqual(clamp_page_size(50), 50)
        self.assertEqual(clamp_page_size(9999), MAX_PAGE_SIZE)

    def test_page(self):
        self.assertEqual(clamp_page(None), 1)
        self.assertEqual(clamp_page(0), 1)
        self.assertEqual(clamp_page(-3), 1)
        self.assertEqual(clamp_page("4"), 4)


class BuildItemTests(unittest.TestCase):
    def test_video_item_has_no_cover_index(self):
        item = build_archive_item(video_record())
        self.assertEqual(item["media_type"], "video")
        self.assertIsNone(item["cover_index"])
        self.assertEqual(item["duration_ms"], 11800)
        self.assertEqual(item["file_count"], 1)
        self.assertEqual(item["archive_dir"], "CutE1n")
        self.assertEqual(item["share_url"], "https://www.douyin.com/video/7001")
        self.assertEqual(item["video_source"]["height"], 1280)

    def test_image_item_covers_with_first_image(self):
        item = build_archive_item(image_record())
        self.assertEqual(item["media_type"], "image")
        self.assertEqual(item["cover_index"], 0)
        self.assertEqual(item["file_count"], 4)
        self.assertEqual(item["image_count"], 4)
        self.assertEqual(item["archive_dir"], "柒柒🐿️")

    def test_skips_records_without_aweme_id(self):
        self.assertIsNone(build_archive_item({"files": ["a/b.mp4"]}))
        self.assertIsNone(build_archive_item("junk"))

    def test_cover_index_skips_non_image_files(self):
        record = image_record()
        record["files"] = ["dir/notes.txt", "dir/1.jpg"]
        self.assertEqual(build_archive_item(record)["cover_index"], 1)


class BuildPageTests(unittest.TestCase):
    def test_sorts_newest_published_first(self):
        page = build_archive_page(
            user_with([
                image_record(aweme_id="old", publish="2025-01-01T00:00:00+08:00"),
                video_record(aweme_id="new", publish="2026-08-29T15:00:36+08:00"),
                video_record(aweme_id="mid", publish="2026-01-01T00:00:00+08:00"),
            ])
        )
        self.assertEqual([i["aweme_id"] for i in page["items"]], ["new", "mid", "old"])

    def test_records_without_publish_time_sort_last(self):
        no_publish = video_record(aweme_id="nopub")
        no_publish["publish_time"] = None
        page = build_archive_page(user_with([no_publish, video_record(aweme_id="haspub")]))
        self.assertEqual([i["aweme_id"] for i in page["items"]], ["haspub", "nopub"])

    def test_offset_pagination_totals_and_slicing(self):
        records = [video_record(aweme_id=f"a{i}", publish=f"2026-08-{(i % 28) + 1:02d}T10:00:00+08:00") for i in range(45)]
        first = build_archive_page(user_with(records), page=1, page_size=20)
        self.assertEqual(first["total"], 45)
        self.assertEqual(first["total_pages"], 3)
        self.assertEqual(first["page"], 1)
        self.assertEqual(len(first["items"]), 20)

        last = build_archive_page(user_with(records), page=3, page_size=20)
        self.assertEqual(len(last["items"]), 5)
        self.assertEqual(last["page"], 3)

    def test_page_beyond_last_clamps_to_last(self):
        page = build_archive_page(user_with([video_record()]), page=99, page_size=20)
        self.assertEqual(page["page"], 1)
        self.assertEqual(page["total_pages"], 1)

    def test_media_counts(self):
        page = build_archive_page(user_with([video_record(), image_record(), video_record(aweme_id="7003")]))
        self.assertEqual(page["media_counts"], {"video": 2, "image": 1})

    def test_empty_user_is_safe(self):
        page = build_archive_page({"id": "u1"})
        self.assertEqual(page["items"], [])
        self.assertEqual(page["total"], 0)
        self.assertEqual(page["total_pages"], 1)


class ResolveFileTests(unittest.TestCase):
    def setUp(self):
        self.user = user_with([video_record(), image_record()])

    def test_resolves_by_index(self):
        self.assertEqual(
            resolve_archive_file(self.user, "7001", 0),
            "CutE1n/20260829_150036_7001_aweme.mp4",
        )
        self.assertEqual(
            resolve_archive_file(self.user, "7002", 2),
            "柒柒🐿️/图片/20260520_195245_标题/3.jpg",
        )

    def test_unknown_aweme_raises_key_error(self):
        with self.assertRaises(KeyError):
            resolve_archive_file(self.user, "missing", 0)

    def test_out_of_range_index_raises(self):
        with self.assertRaises(IndexError):
            resolve_archive_file(self.user, "7001", 5)
        with self.assertRaises(IndexError):
            resolve_archive_file(self.user, "7001", -1)

    def test_invalid_index_raises(self):
        with self.assertRaises(IndexError):
            resolve_archive_file(self.user, "7001", "abc")

    def test_record_without_files_raises(self):
        user = user_with([{"aweme_id": "7009", "media_type": "video", "files": []}])
        with self.assertRaises(KeyError):
            resolve_archive_file(user, "7009", 0)

    def test_find_and_list_helpers(self):
        self.assertIsNotNone(find_record(self.user, "7001"))
        self.assertIsNone(find_record(self.user, "nope"))
        self.assertIsNone(find_record(self.user, "  "))
        self.assertEqual(len(list_archive_files(self.user, "7002")), 4)
        self.assertEqual(list_archive_files(self.user, "nope"), [])


if __name__ == "__main__":
    unittest.main()
