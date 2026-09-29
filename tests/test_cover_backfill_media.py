"""Real tiny-media extraction plus sanitized subprocess timeout verification."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from functools import partial
from pathlib import Path
from unittest.mock import patch

from test_cover_backfill_support import PRIVATE_URL, FakeD2, make_job

from scripts.cloud_playback import media_tools
from scripts.cover_backfill import journal, worker
from scripts.cover_backfill.diagnostics import public_error
from scripts.cover_backfill.pipeline import Dependencies

MEDIA_TEST_TIMEOUT = 5


class CoverRealMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = tempfile.TemporaryDirectory()
        cls.addClassCleanup(fixture.cleanup)
        cls.source = Path(fixture.name) / "tiny.mp4"
        media_tools.run_media((
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
            "-f", "lavfi", "-i", "color=c=blue:s=32x48:r=2", "-frames:v", "2",
            "-c:v", "mpeg4", "-threads", "1", "-f", "mp4", str(cls.source),
        ), timeout=MEDIA_TEST_TIMEOUT)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.job = make_job()
        self.client = FakeD2(self.job.source_paths)
        host = patch.object(worker.socket, "gethostname", return_value="boh-test")
        host.start()
        self.addCleanup(host.stop)

    def run_real(self, extract):
        dependencies = Dependencies(make_client=lambda: self.client, extract=extract,
                                    inspect=partial(journal.inspect_cover, timeout=MEDIA_TEST_TIMEOUT))
        return worker.run_worker((self.job,), worker.WorkerOptions(workdir=self.root), dependencies)

    def test_actual_first_frame_is_decoded_only_as_local_jpeg(self):
        def extract(address, target):
            self.assertEqual(address, PRIVATE_URL)
            media_tools.prepare_cover(self.source, target, timeout=MEDIA_TEST_TIMEOUT)
        with patch.object(journal, "full_decode", wraps=journal.full_decode) as decoder:
            progress = self.run_real(extract)
        self.assertEqual(progress["succeeded"], 1)
        decoded_path = decoder.call_args.args[0]
        self.assertEqual(decoded_path.suffix, ".jpg")
        self.assertEqual(decoder.call_args.kwargs["expected_frames"], 1)
        self.assertEqual(decoder.call_args.kwargs["timeout"], MEDIA_TEST_TIMEOUT)
        media = journal.inspect_cover(self.root / self.job.cover_path, timeout=MEDIA_TEST_TIMEOUT)
        self.assertEqual((media["width"], media["height"]), (32, 48))
        self.assertGreater(media["bytes"], 0)

    def test_jpeg_markers_alone_cannot_publish_undecodable_image(self):
        progress = self.run_real(lambda address, target: target.write_bytes(b"\xff\xd8invalid-image-data\xff\xd9"))
        self.assertEqual(progress["failed"], 1)
        self.assertFalse((self.root / self.job.cover_path).exists())
        receipt = next(journal.read_receipts(self.root / "results.jsonl"))[1]
        self.assertEqual(receipt["stage"], "verify_jpeg")

    def test_first_frame_command_and_timeout_are_explicit(self):
        with patch.object(media_tools, "run_media") as runner:
            media_tools.prepare_cover(PRIVATE_URL, self.root / "cover.jpg", timeout=7)
        command = runner.call_args.args[0]
        self.assertEqual(command[command.index("-frames:v") + 1], "1")
        self.assertEqual(command[command.index("-threads") + 1], "1")
        self.assertEqual(runner.call_args.kwargs["timeout"], 7)
        self.assertNotIn("libx264", command)

    def test_timeout_sanitizes_command_url_output_and_provider_body(self):
        expired = subprocess.TimeoutExpired(["ffmpeg", "-i", PRIVATE_URL], 0.2,
                                            output=b"PRIVATE_BODY", stderr=b"PRIVATE_BODY")
        with patch.object(media_tools.subprocess, "run", side_effect=expired):
            with self.assertRaises(media_tools.MediaJobError) as raised:
                media_tools.prepare_cover(PRIVATE_URL, self.root / "cover.jpg", timeout=0.2)
        diagnostic = public_error(raised.exception)
        self.assertEqual(diagnostic["code"], "media_timeout")
        self.assertEqual(diagnostic["timeout_seconds"], 0.2)
        self.assertNotIn("token=", str(raised.exception))
        self.assertNotIn("PRIVATE_BODY", str(raised.exception))

    def test_real_subprocess_is_killed_at_timeout(self):
        with self.assertRaisesRegex(media_tools.MediaJobError, "timed out"):
            media_tools.run_media((sys.executable, "-c", "import time; time.sleep(3)"), timeout=0.03)


if __name__ == "__main__":
    unittest.main()
