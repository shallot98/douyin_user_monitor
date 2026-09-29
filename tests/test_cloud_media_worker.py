"""Worker orchestration tests: no subprocesses, networks or real media."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.cloud_playback import media_worker as worker
from scripts.cloud_playback.d2_client import D2HTTPError
from scripts.cloud_playback.media_remote import ARCHIVE_ROOT, CREATE_ENDPOINT, DOWNLOAD_ENDPOINT, GET_ENDPOINT
from scripts.cloud_playback.media_tools import MediaJobError

SOURCE = {"path": ARCHIVE_ROOT + "/original/9001.mp4", "identity": "source-id",
          "content_identity": "source-cid", "size": 5000, "update_ts": "123"}
PREFIX = ARCHIVE_ROOT + "/_web_playback/user-1/9001"
JOB = {"source": SOURCE, "user_id": "user-1", "aweme_id": "9001", "operation": "transcode",
       "expected_duration": 15.0, "playback_path": PREFIX + ".h264.mp4", "cover_path": PREFIX + ".cover.jpg"}
OUTPUT = {"width": 540, "height": 960, "bit_rate": 900_000, "duration": 15.0,
          "video_codec": "h264", "pixel_format": "yuv420p", "audio_codecs": ("aac",),
          "is_mp4": True, "faststart": True, "frame_rate": 30.0, "declared_frames": 450}
ORIGINAL = {**OUTPUT, "width": 1080, "height": 1920, "video_codec": "hevc", "faststart": False}
COVER = {"width": 360, "height": 640, "video_codec": "mjpeg"}
PRIVATE_ADDRESS = "https://media.example.test/"


class FakeD2:
    def __init__(self):
        self.objects = {SOURCE["path"]: dict(SOURCE)}
        self.calls = []
        self.uploads = []

    def post(self, endpoint, payload):
        self.calls.append((endpoint, copy.deepcopy(payload)))
        path = payload["path"]
        if endpoint == CREATE_ENDPOINT:
            created = path + "/" + payload["name"]
            self.objects[created] = {"path": created, "dir": True}
            return dict(self.objects[created])
        if path not in self.objects:
            raise D2HTTPError(404, "D2 get: HTTP 404", code=5, message="file not found")
        metadata = dict(self.objects[path])
        if endpoint == GET_ENDPOINT:
            return metadata
        if endpoint == DOWNLOAD_ENDPOINT:
            return {"path": path, "file_size": str(metadata["size"]),
                    "content_identity": metadata["content_identity"], "expire_at": "9999999999",
                    "download_address": PRIVATE_ADDRESS + metadata["identity"] + "?token=do-not-log"}
        raise AssertionError(endpoint)

    def upload(self, client, local_path, destination):
        assert client is self
        assert destination not in self.objects
        result = {"identity": "uploaded-" + Path(destination).name, "path": destination,
                  "size": local_path.stat().st_size, "content_identity": "cid-" + Path(destination).name,
                  "status": "uploaded", "blocks_total": 1, "blocks_uploaded": 1, "blocks_reused": 0}
        self.uploads.append(destination)
        self.objects[destination] = {**result, "update_ts": "456"}
        return result


class WorkerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.client = FakeD2()
        self.job = copy.deepcopy(JOB)
        self.output = dict(OUTPUT)
        self.source_media = dict(ORIGINAL)
        self.probe = self.mock("probe_video", side_effect=self.probe_video)
        self.mock("probe_cover", return_value=dict(COVER))
        self.encoder = self.mock("prepare_video", side_effect=lambda location, target, operation: target.write_bytes(b"mp4"))
        self.cover_encoder = self.mock("prepare_cover", side_effect=lambda location, target: target.write_bytes(b"jpeg"))
        self.decoder = self.mock("full_decode", side_effect=self.decode)
        self.uploader = self.mock("upload_file", side_effect=self.client.upload)
        self.hostname = patch.object(worker.socket, "gethostname", return_value="boh-test").start()
        self.addCleanup(patch.stopall)

    def mock(self, name, **kwargs):
        patcher = patch.object(worker, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def probe_video(self, location, *, local=False):
        return dict(self.source_media if "source-id" in str(location) else self.output)

    def decode(self, location, *, expected_frames, expected_duration=None):
        return {"full_decode_verified": True, "decoded_frames": expected_frames,
                "decoded_duration": expected_duration or 0.04}

    def run_job(self):
        return worker.process_job(self.client, self.job, self.directory)

    def test_transcode_and_cover_require_remote_decode_and_keep_original(self):
        result = self.run_job()
        self.assertEqual(result["source_before"], SOURCE)
        self.assertEqual(result["source_after"], SOURCE)
        self.assertEqual(self.client.objects[SOURCE["path"]], SOURCE)
        self.assertEqual(self.client.uploads, [JOB["playback_path"], JOB["cover_path"]])
        self.assertEqual(self.decoder.call_count, 4)
        self.assertTrue(result["playback"]["media"]["full_decode_verified"])
        self.assertTrue(result["playback"]["uploaded_media_verified"])
        self.assertTrue(result["cover"]["uploaded_media_verified"])
        self.assertNotIn("token=", json.dumps(result))
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_remux_reuses_codec_and_cover_only_does_not_upload_video(self):
        self.job.update(operation="remux", cover_path=None)
        self.source_media = {**OUTPUT, "faststart": False}
        result = self.run_job()
        self.assertEqual(self.encoder.call_args.args[2], "remux")
        self.assertIsNone(result["cover"])
        self.job.update(operation="cover", cover_path=JOB["cover_path"])
        result = self.run_job()
        self.assertIsNone(result["playback"])
        self.assertEqual(self.client.uploads, [JOB["playback_path"], JOB["cover_path"]])

    def test_null_duration_uses_real_source_probe(self):
        self.job["expected_duration"] = None
        result = self.run_job()
        self.assertEqual(result["source_media"]["duration"], 15.0)

    def test_explicit_uploader_is_used_without_calling_direct_upload(self):
        result = worker.process_job(self.client, self.job, self.directory, uploader=self.client.upload)
        self.assertTrue(result["playback"]["uploaded_media_verified"])
        self.assertTrue(result["cover"]["uploaded_media_verified"])
        self.assertEqual(self.decoder.call_count, 4)
        self.uploader.assert_not_called()

    def test_project_host_is_rejected_before_cloud_or_media_access(self):
        self.hostname.return_value = "hdz"
        with self.assertRaisesRegex(MediaJobError, "forbidden on project host"):
            self.run_job()
        self.assertEqual(self.client.calls, [])
        self.encoder.assert_not_called()

    def test_source_change_before_processing_is_rejected(self):
        for field, value in (("identity", "replacement"), ("content_identity", "different"), ("size", 5001)):
            with self.subTest(field=field):
                self.client.objects[SOURCE["path"]] = {**SOURCE, field: value}
                with self.assertRaisesRegex(MediaJobError, "changed"):
                    self.run_job()
        self.encoder.assert_not_called()
        self.uploader.assert_not_called()

    def test_source_change_during_encoding_prevents_upload(self):
        def prepare(location, target, operation):
            target.write_bytes(b"mp4")
            self.client.objects[SOURCE["path"]]["content_identity"] = "replaced"
        self.encoder.side_effect = prepare
        with self.assertRaisesRegex(MediaJobError, "changed"):
            self.run_job()
        self.uploader.assert_not_called()

    def test_existing_target_is_never_overwritten(self):
        old = {"path": JOB["playback_path"], "identity": "old-output"}
        self.client.objects[JOB["playback_path"]] = old
        with self.assertRaisesRegex(MediaJobError, "already exists"):
            self.run_job()
        self.assertEqual(self.client.objects[JOB["playback_path"]], old)
        self.encoder.assert_not_called()
        self.uploader.assert_not_called()

    def test_wrong_codec_missing_audio_or_truncation_prevents_upload(self):
        for changes in ({"video_codec": "hevc"}, {"audio_codecs": ()}, {"duration": 11.0},
                        {"pixel_format": "yuv420p10le"}, {"faststart": False}, {"declared_frames": None}):
            with self.subTest(changes=changes):
                self.output = {**OUTPUT, **changes}
                with self.assertRaises(MediaJobError):
                    self.run_job()
        self.uploader.assert_not_called()

    def test_complete_decode_failure_prevents_upload(self):
        self.decoder.side_effect = MediaJobError("decoder failed on final frame")
        with self.assertRaisesRegex(MediaJobError, "final frame"):
            self.run_job()
        self.uploader.assert_not_called()

    def test_remote_decode_failure_does_not_report_success_or_delete_upload(self):
        def decode(location, **kwargs):
            if str(location).startswith(PRIVATE_ADDRESS):
                raise MediaJobError("uploaded decoder failed")
            return self.decode(location, **kwargs)
        self.decoder.side_effect = decode
        with self.assertRaisesRegex(MediaJobError, "uploaded decoder failed"):
            self.run_job()
        self.assertEqual(self.client.uploads, [JOB["playback_path"]])
        self.assertEqual(self.client.objects[SOURCE["path"]], SOURCE)

    def test_directory_create_uses_parent_and_child_and_preserves_existing_dirs(self):
        self.run_job()
        creates = [payload for endpoint, payload in self.client.calls if endpoint == CREATE_ENDPOINT]
        self.assertEqual(creates, [{"path": ARCHIVE_ROOT, "name": "_web_playback"},
                                  {"path": ARCHIVE_ROOT + "/_web_playback", "name": "user-1"}])


if __name__ == "__main__":
    unittest.main()
