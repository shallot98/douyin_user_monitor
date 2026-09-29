from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from PIL import Image, UnidentifiedImageError

from douyin_user_monitor.monitor.cover_store import CoverIntegrityError, CoverStore

RECORD = {
    "aweme_id": "7001", "downloaded_at": "2026-09-14T12:00:00+08:00",
    "files": ["原目录/7001.mp4"], "cover_file": None,
}


class CoverStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = CoverStore(self.root / "covers")
        self.source = self.root / "source.png"
        Image.new("RGB", (1280, 720), "red").save(self.source)

    def test_normalizes_before_publication_and_preserves_record(self):
        record = deepcopy(RECORD)
        cover = self.store.publish("user-1", record, self.source)
        self.assertEqual(record, RECORD)
        self.assertEqual((cover.width, cover.height), (640, 360))
        self.assertEqual(cover.size_bytes, cover.path.stat().st_size)
        self.assertEqual(cover.version, hashlib.sha256(cover.path.read_bytes()).hexdigest())
        self.assertEqual(cover.path, self.store.root / "objects" / "user-1" / "7001" / f"{cover.version}.jpg")
        self.assertEqual(self.store.lookup("user-1", record), cover)
        self.assertEqual(self.store.read(cover), cover.path.read_bytes())
        with Image.open(cover.path) as image:
            image.load()
            self.assertEqual((image.format, image.mode), ("JPEG", "RGB"))
        self.assertEqual(list(self.root.glob("*.json")), [])

    def test_small_transparent_image_is_not_upscaled(self):
        Image.new("RGBA", (17, 31), (0, 0, 0, 0)).save(self.source)
        cover = self.store.publish("user-1", RECORD, self.source)
        self.assertEqual((cover.width, cover.height), (17, 31))
        with Image.open(cover.path) as image:
            self.assertEqual(image.getpixel((0, 0)), (255, 255, 255))

    def test_exif_orientation_is_applied_and_removed(self):
        source = self.root / "rotated.jpg"
        exif = Image.Exif()
        exif[274] = 6
        Image.new("RGB", (100, 200), "red").save(source, exif=exif)
        cover = self.store.publish("user-1", RECORD, source)
        self.assertEqual((cover.width, cover.height), (200, 100))
        with Image.open(cover.path) as image:
            self.assertNotIn(274, image.getexif())

    def test_corrupt_source_has_no_ready_index(self):
        self.source.write_bytes(b"not an image")
        with self.assertRaises(UnidentifiedImageError):
            self.store.publish("user-1", RECORD, self.source)
        self.assertIsNone(self.store.lookup("user-1", RECORD))
        self.assertEqual(list(self.store.root.rglob("*.jpg")), [])

    def test_truncated_source_has_no_ready_index(self):
        source = self.root / "truncated.jpg"
        Image.new("RGB", (120, 80), "red").save(source)
        source.write_bytes(source.read_bytes()[:-20])
        with self.assertRaises(OSError):
            self.store.publish("user-1", RECORD, source)
        self.assertIsNone(self.store.lookup("user-1", RECORD))

    def test_object_failure_does_not_publish_and_cleans_temporary_file(self):
        with patch("douyin_user_monitor.monitor.cover_store.os.replace", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.store.publish("user-1", RECORD, self.source)
        self.assertIsNone(self.store.lookup("user-1", RECORD))
        self.assertEqual(list(self.store.root.rglob("*.tmp")), [])
        self.assertEqual(list(self.store.root.rglob("*.jpg")), [])

    def test_index_failure_leaves_object_unpublished(self):
        with patch.object(self.store, "_publish_index", side_effect=sqlite3.OperationalError("read only")):
            with self.assertRaisesRegex(sqlite3.OperationalError, "read only"):
                self.store.publish("user-1", RECORD, self.source)
        self.assertIsNone(self.store.lookup("user-1", RECORD))
        objects = list(self.store.root.rglob("*.jpg"))
        self.assertEqual(len(objects), 1)
        with Image.open(objects[0]) as image:
            image.load()

    def test_failed_replacement_preserves_previous_ready_cover(self):
        original = self.store.publish("user-1", RECORD, self.source)
        Image.new("RGB", (100, 100), "blue").save(self.source)
        with patch.object(self.store, "_publish_index", side_effect=sqlite3.OperationalError("read only")):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.publish("user-1", RECORD, self.source)
        self.assertEqual(self.store.lookup("user-1", RECORD), original)
        self.assertEqual(hashlib.sha256(self.store.read(original)).hexdigest(), original.version)

    def test_revision_mismatch_and_late_old_publication_do_not_replace_new_revision(self):
        latest_record = {**RECORD, "downloaded_at": "2026-09-15T12:00:00+08:00"}
        latest = self.store.publish("user-1", latest_record, self.source)
        self.assertIsNone(self.store.lookup("user-1", RECORD))
        Image.new("RGB", (100, 100), "blue").save(self.source)
        older = self.store.publish("user-1", RECORD, self.source)
        self.assertNotEqual(latest.version, older.version)
        self.assertEqual(self.store.lookup("user-1", latest_record), latest)
        self.assertEqual(self.store.lookup("user-1", RECORD), older)

    def test_playback_repair_fields_do_not_change_the_record_revision(self):
        cover = self.store.publish("user-1", RECORD, self.source)
        repaired = {**RECORD, "files": [*RECORD["files"], "cover.jpg"], "cover_file": "cover.jpg"}
        self.assertEqual(self.store.lookup("user-1", repaired), cover)

    def test_legacy_revision_without_timestamp_is_distinct_from_new_revision(self):
        record = {"aweme_id": "7001"}
        cover = self.store.publish("user-1", record, self.source)
        self.assertIsNone(cover.downloaded_at)
        self.assertEqual(self.store.lookup("user-1", record), cover)
        self.assertIsNone(self.store.lookup("user-1", RECORD))

    def test_user_and_work_identities_are_isolated(self):
        first = self.store.publish("user-1", RECORD, self.source)
        second = self.store.publish("user-2", RECORD, self.source)
        other = {**RECORD, "aweme_id": "7002"}
        self.assertIsNone(self.store.lookup("user-1", other))
        self.assertNotEqual(first.path, second.path)
        self.assertEqual(self.store.lookup_many("user-1", (RECORD, other)), {"7001": first})

    def test_path_traversal_and_invalid_revision_are_rejected(self):
        cases = (("../outside", RECORD), ("user-1", {**RECORD, "aweme_id": "../outside"}),
                 ("user-1", {**RECORD, "downloaded_at": 12}), ("", RECORD))
        for user_id, record in cases:
            with self.subTest(user_id=user_id, record=record), self.assertRaises(ValueError):
                self.store.publish(user_id, record, self.source)
        self.assertEqual(list(self.root.rglob("*.jpg")), [])

    def test_symlink_cannot_escape_object_root(self):
        outside = self.root / "outside"
        outside.mkdir()
        objects = self.store.root / "objects"
        objects.mkdir()
        (objects / "user-1").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(CoverIntegrityError):
            self.store.publish("user-1", RECORD, self.source)
        self.assertEqual(list(outside.iterdir()), [])

    def test_missing_or_corrupt_published_file_fails_explicitly(self):
        cover = self.store.publish("user-1", RECORD, self.source)
        cover.path.write_bytes(b"x" * cover.size_bytes)
        with self.assertRaises(CoverIntegrityError):
            self.store.read(cover)
        cover.path.unlink()
        with self.assertRaises(CoverIntegrityError):
            self.store.lookup("user-1", RECORD)

    def test_concurrent_publication_is_idempotent(self):
        def publish(_):
            return self.store.publish("user-1", RECORD, self.source)

        with ThreadPoolExecutor(max_workers=4) as executor:
            covers = list(executor.map(publish, range(12)))
        self.assertEqual(len(set(covers)), 1)
        self.assertEqual(len(list(self.store.root.rglob("*.jpg"))), 1)
        self.assertEqual(list(self.store.root.rglob("*.tmp")), [])
        with sqlite3.connect(self.store.db_path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM covers").fetchone()[0], 1)
        self.assertEqual(self.store.lookup("user-1", RECORD), covers[0])


if __name__ == "__main__":
    unittest.main()
