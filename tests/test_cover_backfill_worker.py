"""Receipt publication, fair orchestration, and verified restart behavior."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from test_cover_backfill_support import JPEG, FakeD2, fake_inspect, make_job

from scripts.cloud_playback.d2_client import D2HTTPError
from scripts.cloud_playback.media_remote import DOWNLOAD_ENDPOINT, GET_ENDPOINT
from scripts.cloud_playback.media_tools import MediaJobError
from scripts.cover_backfill import worker
from scripts.cover_backfill.journal import producer_lock, read_receipts
from scripts.cover_backfill.models import BackfillError
from scripts.cover_backfill.pipeline import Dependencies


class CoverWorkerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "run"
        self.job = make_job()
        self.client = FakeD2(self.job.source_paths)
        self.extract = Mock(side_effect=lambda address, target: target.write_bytes(JPEG))
        self.inspect = Mock(side_effect=fake_inspect)
        self.dependencies = Dependencies(make_client=lambda: self.client, extract=self.extract, inspect=self.inspect)
        self.options = worker.WorkerOptions(workdir=self.root, workers=1)
        host = patch.object(worker.socket, "gethostname", return_value="boh-test")
        host.start()
        self.addCleanup(host.stop)

    def run_jobs(self, jobs=None):
        return worker.run_worker(tuple(jobs or (self.job,)), self.options, self.dependencies)

    def receipts(self):
        return [record for _, record in read_receipts(self.root / "results.jsonl")]

    def test_success_verifies_source_and_publishes_only_jpeg_and_receipt(self):
        progress = self.run_jobs()
        receipt = self.receipts()[0]
        self.assertEqual(progress["succeeded"], 1)
        self.assertEqual(progress["remaining"], 0)
        self.assertTrue(progress["finished"])
        self.assertEqual(receipt["status"], "ready")
        self.assertTrue(receipt["source_verified_after"])
        self.assertEqual((self.root / self.job.cover_path).read_bytes(), JPEG)
        self.assertEqual([endpoint for endpoint, _ in self.client.calls], [GET_ENDPOINT, DOWNLOAD_ENDPOINT, GET_ENDPOINT])
        self.assertEqual(list((self.root / ".tmp").iterdir()), [])
        self.assertNotIn("token=", json.dumps([receipt, progress]))

    def test_explicit_missing_candidate_is_recorded_before_verified_alias(self):
        missing = "/vps/抖音视频/old/9001.mp4"
        self.job = replace(self.job, source_paths=(missing, *self.job.source_paths))
        self.run_jobs()
        receipt = self.receipts()[0]
        self.assertEqual([attempt["status"] for attempt in receipt["attempts"]], ["missing", "selected"])
        self.assertEqual(receipt["attempts"][0]["error"]["provider_code"], 5)
        self.assertEqual(receipt["source"]["path"], self.job.source_paths[1])

    def test_all_missing_candidates_remain_failed(self):
        self.client.objects = {}
        progress = self.run_jobs()
        receipt = self.receipts()[0]
        self.assertEqual(progress["failed"], 1)
        self.assertEqual(receipt["error"]["code"], "sources_missing")
        self.assertEqual(receipt["stage"], "source_lookup")
        self.assertFalse((self.root / self.job.cover_path).exists())
        self.extract.assert_not_called()

    def test_unrecognized_404_does_not_try_other_candidates(self):
        original = self.job.source_paths[0]
        self.client.errors[original] = D2HTTPError(404, "private provider body", code=7)
        self.job = replace(self.job, source_paths=(original, "/vps/抖音视频/alternate/9001.mp4"))
        self.run_jobs()
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.receipts()[0]["error"]["provider_code"], 7)
        self.assertNotIn("private provider body", (self.root / "results.jsonl").read_text())

    def test_source_mutation_during_extraction_prevents_publication(self):
        def extract(address, target):
            target.write_bytes(JPEG)
            self.client.objects[self.job.source_paths[0]]["content_identity"] = "replacement"
        self.extract.side_effect = extract
        self.run_jobs()
        receipt = self.receipts()[0]
        self.assertEqual(receipt["stage"], "verify_source_unchanged")
        self.assertEqual(receipt["error"]["code"], "source_changed")
        self.assertFalse((self.root / self.job.cover_path).exists())

    def test_media_failure_does_not_publish_source_url_or_error_body(self):
        self.extract.side_effect = MediaJobError("ffmpeg failed (1): https://private.test/?token=secret BODY_SECRET")
        self.run_jobs()
        receipt = self.receipts()[0]
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["error"]["exit_code"], 1)
        public = (self.root / "results.jsonl").read_text() + (self.root / "progress.json").read_text()
        self.assertNotIn("secret", public)
        self.assertNotIn("BODY_SECRET", public)

    def test_partial_image_without_ready_receipt_is_regenerated(self):
        target = self.root / self.job.cover_path
        target.parent.mkdir(parents=True)
        target.write_bytes(b"unfinished")
        self.run_jobs()
        self.extract.assert_called_once()
        self.assertEqual(target.read_bytes(), JPEG)
        self.assertEqual(self.receipts()[0]["status"], "ready")

    def test_resume_revalidates_ready_image_and_does_not_append_duplicate(self):
        self.run_jobs()
        original = (self.root / "results.jsonl").read_bytes()
        progress = self.run_jobs()
        self.assertEqual(progress["skipped_verified"], 1)
        self.assertEqual(progress["succeeded"], 0)
        self.assertEqual((self.root / "results.jsonl").read_bytes(), original)
        self.assertEqual(self.inspect.call_count, 2)
        self.extract.assert_called_once()

    def test_corrupted_ready_image_is_reprocessed_with_explicit_resume_error(self):
        self.run_jobs()
        (self.root / self.job.cover_path).write_bytes(b"\xff\xd8modified\xff\xd9")
        progress = self.run_jobs()
        self.assertEqual(progress["succeeded"], 1)
        self.assertEqual(len(self.receipts()), 2)
        self.assertIn("resume_error", self.receipts()[-1])
        self.assertEqual((self.root / self.job.cover_path).read_bytes(), JPEG)

    def test_changed_input_revision_is_not_skipped(self):
        self.run_jobs()
        original_revision = self.job.input_revision
        self.job = replace(self.job, downloaded_at="2026-09-14T13:00:00+08:00")
        self.run_jobs()
        self.assertEqual(self.extract.call_count, 2)
        self.assertNotEqual(self.receipts()[-1]["input_revision"], original_revision)

    def test_crash_after_image_publish_before_receipt_cannot_create_false_resume(self):
        with patch.object(worker, "append_receipt", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.run_jobs()
        self.assertTrue((self.root / self.job.cover_path).exists())
        self.assertEqual(self.receipts(), [])
        self.run_jobs()
        self.assertEqual(self.extract.call_count, 2)
        self.assertEqual(len(self.receipts()), 1)

    def test_incomplete_journal_tail_is_explicit_and_preserved(self):
        self.root.mkdir()
        journal = self.root / "results.jsonl"
        journal.write_bytes(b'{"status":')
        with self.assertRaisesRegex(BackfillError, "incomplete line"):
            self.run_jobs()
        self.assertEqual(journal.read_bytes(), b'{"status":')
        self.extract.assert_not_called()

    def test_duplicate_producer_lock_fails_before_source_access(self):
        self.root.mkdir()
        with producer_lock(self.root), self.assertRaisesRegex(BackfillError, "Another cover worker"):
            self.run_jobs()
        self.assertEqual(self.client.calls, [])

    def test_hdz_is_rejected_before_network_and_directory_creation(self):
        with patch.object(worker.socket, "gethostname", return_value="hdz.local"):
            with self.assertRaisesRegex(BackfillError, "forbidden on project host hdz"):
                self.run_jobs()
        self.assertFalse(self.root.exists())
        self.assertEqual(self.client.calls, [])

    def test_pool_never_exceeds_configured_concurrency(self):
        jobs = tuple(make_job(str(index)) for index in range(6))
        self.client = FakeD2(job.source_paths[0] for job in jobs)
        barrier, lock = threading.Barrier(2), threading.Lock()
        active, peak = 0, 0
        def extract(address, target):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            barrier.wait(timeout=5)
            target.write_bytes(JPEG)
            with lock:
                active -= 1
        self.extract.side_effect = extract
        self.options = replace(self.options, workers=2)
        progress = self.run_jobs(jobs)
        self.assertEqual(progress["succeeded"], len(jobs))
        self.assertEqual(peak, 2)


if __name__ == "__main__":
    unittest.main()
