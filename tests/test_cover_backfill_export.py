"""Immutable transfer batches preserve receipt cursors and failure evidence."""

from __future__ import annotations

import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_cover_backfill_support import JPEG, fake_inspect, make_job, source_record

from scripts.cover_backfill import export
from scripts.cover_backfill.journal import append_receipt
from scripts.cover_backfill.models import BackfillError, json_bytes


class CoverExportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.journal = self.root / "results.jsonl"

    def add_ready(self, aweme_id):
        job = make_job(aweme_id)
        path = self.root / job.cover_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(JPEG)
        receipt = {**job.payload(), "schema_version": 1, "input_revision": job.input_revision,
                   "status": "ready", "source": source_record(job.source_paths[0]),
                   "cover_path": job.cover_path, **fake_inspect(path)}
        return receipt, append_receipt(self.journal, receipt)

    def add_failure(self, aweme_id):
        job = make_job(aweme_id)
        receipt = {**job.payload(), "schema_version": 1, "input_revision": job.input_revision,
                   "status": "failed", "stage": "source_lookup", "source": None,
                   "attempts": [{"path": job.source_paths[0], "status": "missing"}],
                   "error": {"code": "sources_missing", "message": "All explicit source candidates are absent"}}
        return receipt, append_receipt(self.journal, receipt)

    def members(self, path):
        with tarfile.open(path) as archive:
            return {entry.name: archive.extractfile(entry).read() for entry in archive.getmembers()}

    def test_cursor_batches_include_full_failures_and_do_not_change_producer_files(self):
        first, first_offset = self.add_ready("9001")
        failure, _ = self.add_failure("9002")
        second, final_offset = self.add_ready("9003")
        original = self.journal.read_bytes()
        first_tar, second_tar = self.root / "first.tar", self.root / "second.tar"
        batch = export.export_batch(self.root, first_tar, max_covers=1)
        self.assertEqual(batch["next_offset"], first_offset)
        self.assertEqual(batch["cover_count"], 1)
        first_members = self.members(first_tar)
        self.assertEqual(first_members[first["cover_path"]], JPEG)
        self.assertEqual(first_members["failures.jsonl"], b"")
        batch = export.export_batch(self.root, second_tar, after_offset=first_offset)
        self.assertEqual(batch["next_offset"], final_offset)
        self.assertEqual(batch["scanned_receipts"], 2)
        self.assertEqual(batch["failed_receipts"], 1)
        members = self.members(second_tar)
        self.assertEqual(json.loads(members["failures.jsonl"]), failure)
        self.assertEqual(json.loads(members["receipts.jsonl"]), second)
        self.assertEqual(members[second["cover_path"]], JPEG)
        self.assertEqual(self.journal.read_bytes(), original)
        self.assertTrue((self.root / first["cover_path"]).exists())

    def test_failure_only_batch_keeps_per_item_identity_and_advances_cursor(self):
        failed, offset = self.add_failure("9001")
        target = self.root / "failures.tar"
        batch = export.export_batch(self.root, target)
        self.assertEqual(batch["cover_count"], 0)
        self.assertEqual(batch["next_offset"], offset)
        self.assertEqual(batch["failed_receipts"], 1)
        members = self.members(target)
        self.assertEqual(members["receipts.jsonl"], b"")
        self.assertEqual(json.loads(members["failures.jsonl"]), failed)

    def test_existing_batch_is_immutable(self):
        self.add_ready("9001")
        target = self.root / "immutable.tar"
        export.export_batch(self.root, target)
        original = target.read_bytes()
        with self.assertRaisesRegex(BackfillError, "cannot be overwritten"):
            export.export_batch(self.root, target)
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(list(self.root.glob(".cover-batch-*")), [])

    def test_corruption_prevents_tar_publication(self):
        receipt, _ = self.add_ready("9001")
        (self.root / receipt["cover_path"]).write_bytes(b"\xff\xd8changed\xff\xd9")
        target = self.root / "corrupt.tar"
        with self.assertRaisesRegex(BackfillError, "no longer match"):
            export.export_batch(self.root, target)
        self.assertFalse(target.exists())

    def test_unfinished_concurrent_receipt_does_not_advance_cursor(self):
        _, first_offset = self.add_ready("9001")
        second, second_offset = self.add_ready("9002")
        complete = self.journal.read_bytes()
        self.journal.write_bytes(complete[:-1])
        first_batch = export.export_batch(self.root, self.root / "inflight.tar")
        self.assertEqual(first_batch["next_offset"], first_offset)
        with self.journal.open("ab") as stream:
            stream.write(b"\n")
        next_tar = self.root / "completed.tar"
        next_batch = export.export_batch(self.root, next_tar, after_offset=first_offset)
        self.assertEqual(next_batch["next_offset"], second_offset)
        self.assertEqual(json.loads(self.members(next_tar)["receipts.jsonl"]), second)

    def test_unreceipted_partial_image_is_not_exported(self):
        self.add_failure("9001")
        partial = self.root / "covers/user-1/9002.jpg"
        partial.parent.mkdir(parents=True)
        partial.write_bytes(b"partial")
        target = self.root / "partial.tar"
        export.export_batch(self.root, target)
        self.assertEqual(set(self.members(target)), {"batch.json", "receipts.jsonl", "failures.jsonl"})

    def test_invalid_path_and_nonboundary_cursor_fail_explicitly(self):
        receipt, _ = self.add_ready("9001")
        with self.assertRaisesRegex(BackfillError, "complete newline"):
            export.export_batch(self.root, self.root / "bad-offset.tar", after_offset=2)
        receipt["cover_path"] = "../outside.jpg"
        self.journal.write_bytes(json_bytes(receipt) + b"\n")
        with self.assertRaisesRegex(BackfillError, "does not match"):
            export.export_batch(self.root, self.root / "bad-path.tar")

    def test_archive_failure_leaves_no_partial_public_batch(self):
        self.add_ready("9001")
        target = self.root / "interrupted.tar"
        with patch.object(export, "_write_archive", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                export.export_batch(self.root, target)
        self.assertFalse(target.exists())
        self.assertEqual(list(self.root.glob(".cover-batch-*")), [])

    def test_empty_following_batch_keeps_cursor(self):
        _, offset = self.add_ready("9001")
        batch = export.export_batch(self.root, self.root / "empty.tar", after_offset=offset)
        self.assertEqual(batch["next_offset"], offset)
        self.assertEqual(batch["scanned_receipts"], 0)
        self.assertEqual(batch["cover_count"], 0)


if __name__ == "__main__":
    unittest.main()
