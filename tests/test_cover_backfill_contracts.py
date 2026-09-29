"""Manifest boundaries and newest-page-first fairness."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from test_cover_backfill_support import make_job

from scripts.cover_backfill.models import BackfillError, fair_jobs, load_manifest, parse_job


class CoverContractTests(unittest.TestCase):
    def test_manifest_order_is_preserved_per_user_and_pages_rotate(self):
        jobs = [make_job(str(index), user) for user in ("a", "b", "c") for index in (7, 9, 1)]
        result = [(job.user_id, job.aweme_id) for job in fair_jobs(jobs, 2)]
        self.assertEqual(result, [(user, aweme) for user in ("a", "b", "c") for aweme in ("7", "9")]
                         + [(user, "1") for user in ("a", "b", "c")])

    def test_revision_preserves_exact_identity_values(self):
        job = make_job()
        updated = parse_job({**job.payload(), "source_relative_path": "renamed/9001.mp4"})
        self.assertNotEqual(job.input_revision, updated.input_revision)
        self.assertEqual(job.input_revision, parse_job(dict(reversed(list(job.payload().items())))).input_revision)
        self.assertEqual(job.input_revision, parse_job({**job.payload(), "nickname": None}).input_revision)

    def test_invalid_external_fields_fail_before_any_source_access(self):
        job = make_job().payload()
        invalid = ({"user_id": "../escape"}, {"aweme_id": "../9001"}, {"sec_user_id": ""},
                   {"source_paths": []}, {"source_paths": ["https://private.test/video"]},
                   {"source_paths": ["/vps/抖音视频/../other/9001.mp4"]}, {"downloaded_at": 123},
                   {"nickname": []}, {"unexpected": "value"})
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(BackfillError):
                parse_job({**job, **changes})

    def test_duplicate_manifest_targets_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps({"jobs": [make_job().payload()] * 2}))
            with self.assertRaisesRegex(BackfillError, "repeats"):
                load_manifest(path)


if __name__ == "__main__":
    unittest.main()
