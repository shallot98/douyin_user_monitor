import fcntl
import hashlib
import io
import json
import sqlite3
import tarfile
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from douyin_user_monitor.monitor.cover_store import CoverStore
from scripts.cover_backfill.export import export_batch
from scripts.cover_backfill.import_site import import_batch
from scripts.cover_backfill.journal import append_receipt
from scripts.cover_backfill.models import BackfillError, Job, json_bytes


class CoverBatchImportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "monitor_users.json"
        self.covers = self.root / "covers"
        self.checkpoint = self.root / "sync-checkpoint.json"
        self.record = {"aweme_id": "123", "downloaded_at": "2026-09-14T12:00:00+08:00",
                       "media_type": "video", "files": ["user/123.mp4"], "video_file": "user/123.mp4"}
        self.user = {"id": "user-1", "sec_user_id": "sec-1", "nickname": "renamed user",
                     "download_records": [self.record], "downloaded_aweme_ids": ["123"]}
        self.save_state([self.user])
        self.state_before = self.state.read_bytes()
        buffer = io.BytesIO()
        Image.new("RGB", (128, 96), "blue").save(buffer, "JPEG")
        self.jpeg = buffer.getvalue()

    def save_state(self, users):
        self.state.write_bytes(json_bytes({"users": users}) + b"\n")

    def receipt(self, aweme_id="123", *, failed=False):
        job = Job(user_id="user-1", aweme_id=aweme_id, sec_user_id="sec-1",
                  downloaded_at=self.record["downloaded_at"], source_relative_path=f"user/{aweme_id}.mp4",
                  source_paths=(f"/vps/抖音视频/user/{aweme_id}.mp4",))
        identity = {**job.payload(), "schema_version": 1, "input_revision": job.input_revision}
        if failed:
            return {**identity, "status": "failed", "stage": "source_lookup", "attempts": [],
                    "error": {"type": "D2HTTPError", "code": "d2_http_error", "http_status": 404}}
        return {**identity, "status": "ready", "source_verified_after": True,
                "source": {"path": job.source_paths[0]}, "cover_path": job.cover_path,
                "sha256": hashlib.sha256(self.jpeg).hexdigest(), "bytes": len(self.jpeg),
                "width": 128, "height": 96}

    def batch(self, receipts=None, *, bodies=None, extra=None, after=0, suffix="batch"):
        receipts = [self.receipt()] if receipts is None else receipts
        ready = [row for row in receipts if row["status"] == "ready"]
        failures = [row for row in receipts if row["status"] == "failed"]
        images = {row["cover_path"]: (bodies or {}).get(row["aweme_id"], self.jpeg) for row in ready}
        metadata = {"schema_version": 1, "after_offset": after, "next_offset": after + len(receipts) * 100,
                    "scanned_receipts": len(receipts), "ready_receipts": len(ready),
                    "failed_receipts": len(failures), "cover_count": len(images),
                    "cover_bytes": sum(map(len, images.values()))}
        payload = {"batch.json": json_bytes(metadata),
                   "receipts.jsonl": b"".join(json_bytes(row) + b"\n" for row in ready),
                   "failures.jsonl": b"".join(json_bytes(row) + b"\n" for row in failures), **images}
        target = self.root / f"{suffix}.tar"
        with tarfile.open(target, "w") as archive:
            for name, data in payload.items():
                member = tarfile.TarInfo(name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
            if extra is not None:
                archive.addfile(extra, io.BytesIO(b""))
        return target

    def run_import(self, batch):
        return import_batch(batch, state_file=self.state, cover_root=self.covers, checkpoint=self.checkpoint)

    def test_real_jpeg_published_with_durable_checkpoint_and_unchanged_state(self):
        result = self.run_import(self.batch())
        self.assertEqual(result["published"], 1)
        self.assertEqual(result["next_offset"], 100)
        metadata = CoverStore(self.covers).lookup("user-1", self.record)
        with Image.open(metadata.path) as image:
            image.load()
            self.assertEqual(image.format, "JPEG")
            self.assertEqual(image.size, (128, 96))
        self.assertEqual(self.state.read_bytes(), self.state_before)
        saved = json.loads(self.checkpoint.read_bytes())
        self.assertEqual(saved["totals"]["published"], 1)
        audit = json.loads(Path(result["report"]).read_bytes())
        self.assertEqual(audit["items"][0]["status"], "published")

    def test_hash_mismatch_rejects_entire_batch_before_publication(self):
        batch = self.batch([self.receipt(), self.receipt("124")], bodies={"124": b"damaged"})
        with self.assertRaisesRegex(BackfillError, "hash or byte count"):
            self.run_import(batch)
        self.assertFalse(self.checkpoint.exists())
        self.assertEqual(list(self.covers.rglob("*.jpg")), [])
        reports = list(self.root.glob("sync-checkpoint-reports/*.json"))
        self.assertEqual(json.loads(reports[0].read_bytes())["status"], "failed")

    def test_invalid_jpeg_with_matching_hash_is_rejected(self):
        raw = b"not an image"
        receipt = {**self.receipt(), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
        with self.assertRaisesRegex(BackfillError, "cannot identify image"):
            self.run_import(self.batch([receipt], bodies={"123": raw}))
        self.assertFalse(self.checkpoint.exists())

    def test_deleted_or_changed_records_are_explicitly_skipped(self):
        cases = [([], "user_deleted"), ([{**self.user, "sec_user_id": "new-sec"}], "sec_user_id_changed"),
                 ([{**self.user, "download_records": []}], "work_deleted"),
                 ([{**self.user, "download_records": [{**self.record, "downloaded_at": "changed"}]}],
                  "download_revision_changed"),
                 ([{**self.user, "download_records": [{**self.record, "files": []}]}], "source_path_removed")]
        batch = self.batch()
        for index, (users, reason) in enumerate(cases):
            with self.subTest(reason=reason):
                self.checkpoint = self.root / f"skip-{index}.json"
                self.save_state(users)
                before = self.state.read_bytes()
                result = self.run_import(batch)
                audit = json.loads(Path(result["report"]).read_bytes())
                self.assertEqual((result["published"], result["skipped"], result["next_offset"]), (0, 1, 100))
                self.assertEqual(audit["items"][0]["reason"], reason)
                self.assertEqual(self.state.read_bytes(), before)

    def test_worker_failure_is_audited_and_acknowledged_without_publication(self):
        result = self.run_import(self.batch([self.receipt(failed=True)]))
        self.assertEqual(result["status"], "imported_with_failures")
        self.assertEqual((result["published"], result["failed"], result["next_offset"]), (0, 1, 100))
        audit = json.loads(Path(result["report"]).read_bytes())
        self.assertEqual(audit["items"][0]["reason"]["http_status"], 404)
        self.assertEqual(audit["items"][0]["stage"], "source_lookup")

    def test_duplicate_batch_does_not_republish_or_increment_counts(self):
        batch = self.batch()
        self.run_import(batch)
        saved = self.checkpoint.read_bytes()
        with patch.object(CoverStore, "publish", side_effect=AssertionError("duplicate publication")):
            result = self.run_import(batch)
        self.assertEqual(result["status"], "already_imported")
        self.assertEqual(result["published"], 0)
        self.assertEqual(result["totals"]["published"], 1)
        self.assertEqual(self.checkpoint.read_bytes(), saved)

    def test_restart_after_partial_publication_reuses_saved_cover(self):
        second = {**self.record, "aweme_id": "124", "files": ["user/124.mp4"]}
        self.save_state([{**self.user, "download_records": [self.record, second]}])
        batch = self.batch([self.receipt(), self.receipt("124")])
        publish = CoverStore.publish

        def fail_second(store, user_id, record, source):
            if record["aweme_id"] == "124":
                raise OSError("disk full")
            return publish(store, user_id, record, source)

        with patch.object(CoverStore, "publish", new=fail_second), self.assertRaisesRegex(BackfillError, "disk full"):
            self.run_import(batch)
        self.assertFalse(self.checkpoint.exists())
        result = self.run_import(batch)
        self.assertEqual((result["published"], result["already_available"], result["next_offset"]), (1, 1, 200))
        self.assertEqual(len(list(self.covers.rglob("*.jpg"))), 2)

    def test_unsafe_or_unlisted_members_and_duplicates_are_rejected(self):
        names = ("../outside.jpg", "/absolute.jpg", "extra.mp4", "covers/user-1/link.jpg", "batch.json")
        for index, name in enumerate(names):
            member = tarfile.TarInfo(name)
            if name.endswith("link.jpg"):
                member.type, member.linkname = tarfile.SYMTYPE, "../../outside.jpg"
            with self.subTest(name=name), self.assertRaises(BackfillError):
                self.run_import(self.batch(extra=member, suffix=f"attack-{index}"))
        self.assertFalse(self.checkpoint.exists())
        self.assertFalse((self.root.parent / "outside.jpg").exists())

    def test_truncated_tar_does_not_advance_cursor(self):
        batch = self.batch()
        batch.write_bytes(batch.read_bytes()[:-1])
        with self.assertRaisesRegex(BackfillError, "truncated"):
            self.run_import(batch)
        self.assertFalse(self.checkpoint.exists())

    def test_checkpoint_lock_rejects_concurrent_import(self):
        lock = self.checkpoint.with_suffix(".json.lock")
        with lock.open("a") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(BackfillError, "Another cover importer"):
                self.run_import(self.batch())
        self.assertFalse(self.checkpoint.exists())

    def test_noncontiguous_batch_preserves_checkpoint(self):
        self.run_import(self.batch())
        saved = self.checkpoint.read_bytes()
        with self.assertRaisesRegex(BackfillError, "after_offset"):
            self.run_import(self.batch(after=200, suffix="gap"))
        self.assertEqual(self.checkpoint.read_bytes(), saved)

    def test_state_change_during_batch_is_rechecked_before_publication(self):
        second = {**self.record, "aweme_id": "124", "files": ["user/124.mp4"]}
        self.save_state([{**self.user, "download_records": [self.record, second]}])
        publish = CoverStore.publish

        def remove_second_after_publish(store, user_id, record, source):
            result = publish(store, user_id, record, source)
            self.save_state([deepcopy(self.user)])
            return result

        with patch.object(CoverStore, "publish", new=remove_second_after_publish):
            result = self.run_import(self.batch([self.receipt(), self.receipt("124")]))
        self.assertEqual((result["published"], result["skipped"]), (1, 1))

    def test_checkpoint_cannot_overwrite_monitor_state(self):
        with self.assertRaisesRegex(BackfillError, "must not overwrite"):
            import_batch(self.batch(), state_file=self.state, cover_root=self.covers, checkpoint=self.state)
        self.assertEqual(self.state.read_bytes(), self.state_before)

    def test_committed_replay_repairs_missing_or_corrupt_output_without_recounting(self):
        batch = self.batch()
        self.run_import(batch)
        for damage in ("missing", "corrupt", "index_missing"):
            with self.subTest(damage=damage):
                metadata = CoverStore(self.covers).lookup("user-1", self.record)
                if damage == "missing":
                    metadata.path.unlink()
                elif damage == "corrupt":
                    raw = metadata.path.read_bytes()
                    metadata.path.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
                else:
                    with sqlite3.connect(self.covers / "index.sqlite3") as connection:
                        connection.execute("DELETE FROM covers WHERE user_id=? AND aweme_id=?", ("user-1", "123"))
                result = self.run_import(batch)
                self.assertEqual((result["published"], result["repaired"], result["next_offset"]), (0, 1, 100))
                self.assertEqual(result["totals"]["published"], 1)
                store = CoverStore(self.covers)
                self.assertTrue(store.read(store.lookup("user-1", self.record)))

    def test_uncommitted_import_repairs_existing_damaged_index(self):
        source = self.root / "existing.jpg"
        source.write_bytes(self.jpeg)
        metadata = CoverStore(self.covers).publish("user-1", self.record, source)
        metadata.path.unlink()
        result = self.run_import(self.batch())
        self.assertEqual((result["published"], result["repaired"]), (0, 1))
        audit = json.loads(Path(result["report"]).read_bytes())
        self.assertEqual(audit["items"][0]["reason"]["type"], "CoverIntegrityError")

    def test_exporter_roundtrip_keeps_failure_details_and_receipt_offset(self):
        workdir = self.root / "worker"
        ready, failed = self.receipt(), self.receipt("124", failed=True)
        image = workdir / ready["cover_path"]
        image.parent.mkdir(parents=True)
        image.write_bytes(self.jpeg)
        journal = workdir / "results.jsonl"
        append_receipt(journal, ready)
        append_receipt(journal, failed)
        batch = self.root / "exported.tar"
        export_batch(workdir, batch)
        result = self.run_import(batch)
        self.assertEqual((result["published"], result["failed"]), (1, 1))
        self.assertEqual(result["next_offset"], journal.stat().st_size)

    def test_tampered_receipt_identity_is_rejected(self):
        receipt = {**self.receipt(), "sec_user_id": "different-source-user"}
        with self.assertRaisesRegex(BackfillError, "input revision"):
            self.run_import(self.batch([receipt]))
        self.assertFalse(self.checkpoint.exists())

    def test_checkpoint_destination_cannot_change_on_replay(self):
        batch = self.batch()
        self.run_import(batch)
        with self.assertRaisesRegex(BackfillError, "different state file or cover store"):
            import_batch(batch, state_file=self.state, cover_root=self.root / "different", checkpoint=self.checkpoint)


if __name__ == "__main__":
    unittest.main()
