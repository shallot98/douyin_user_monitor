"""Probe and command validation without invoking FFmpeg or FFprobe."""

from __future__ import annotations

import json
import subprocess
import unittest
from unittest.mock import patch

from scripts.cloud_playback import media_tools as media
from scripts.cloud_playback.d2_client import D2HTTPError
from scripts.cloud_playback.media_remote import _lookup, direct_url, validate_job


def probe_document(**video_changes):
    return {"streams": [
        {"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p", "width": 540,
         "height": 960, "avg_frame_rate": "30/1", "nb_frames": "450", **video_changes},
        {"codec_type": "audio", "codec_name": "aac"}],
        "format": {"duration": "15", "bit_rate": "900000", "format_name": "mov,mp4,m4a,3gp,3g2,mj2"}}


class MediaToolsTests(unittest.TestCase):
    def test_actual_probe_fields_are_parsed_without_container_filename_or_tags(self):
        with patch.object(media, "run_media", return_value=json.dumps(probe_document()).encode()) as runner:
            result = media.probe_video("https://source.test/private?token=secret")
        self.assertEqual(result["video_codec"], "h264")
        self.assertEqual(result["audio_codecs"], ("aac",))
        self.assertEqual(result["declared_frames"], 450)
        self.assertEqual(result["frame_rate"], 30)
        self.assertNotIn("filename", runner.call_args.args[0])

    def test_missing_video_or_invalid_probe_is_an_explicit_error(self):
        for raw in (b"not json", b'{"streams": [], "format": {}}',
                    json.dumps(probe_document(width=0)).encode(),
                    json.dumps(probe_document(avg_frame_rate="0/0")).encode()):
            with self.subTest(raw=raw), patch.object(media, "run_media", return_value=raw):
                with self.assertRaises(media.MediaJobError):
                    media.probe_video("https://source.test/private")

    def test_duration_tolerance_is_maximum_of_half_second_and_one_percent(self):
        media.assert_duration(15.4, 15)
        media.assert_duration(1009.0, 1000)
        for actual, expected in ((15.6, 15), (1011.0, 1000), (float("nan"), 15)):
            with self.subTest(actual=actual), self.assertRaises(media.MediaJobError):
                media.assert_duration(actual, expected)

    def test_complete_decode_requires_end_all_frames_and_full_duration(self):
        good = b"frame=450\nout_time_us=15000000\nprogress=end\n"
        with patch.object(media, "run_media", return_value=good) as runner:
            result = media.full_decode("output.mp4", expected_frames=450, expected_duration=15)
        self.assertTrue(result["full_decode_verified"])
        self.assertIn("-xerror", runner.call_args.args[0])
        self.assertIn("explode", runner.call_args.args[0])
        for raw in (good.replace(b"450", b"400"), good.replace(b"15000000", b"10000000"),
                    good.replace(b"end", b"continue"), b"frame=450\nprogress=end\n"):
            with self.subTest(raw=raw), patch.object(media, "run_media", return_value=raw):
                with self.assertRaises(media.MediaJobError):
                    media.full_decode("output.mp4", expected_frames=450, expected_duration=15)

    def test_commands_preserve_original_and_use_one_thread_and_faststart(self):
        with patch.object(media, "run_media") as runner:
            media.prepare_video("private-source", "output.mp4", "transcode")
        command = runner.call_args.args[0]
        for argument in ("-n", "libx264", "aac", "yuv420p", "+faststart"):
            self.assertIn(argument, command)
        self.assertNotIn("-y", command)
        self.assertEqual(command[command.index("-threads") + 1], "1")
        self.assertEqual(command[command.index("-r") + 1], "30")
        with patch.object(media, "run_media") as runner:
            media.prepare_video("private-source", "output.mp4", "remux")
        self.assertIn("copy", runner.call_args.args[0])
        self.assertNotIn("libx264", runner.call_args.args[0])

    def test_media_errors_redact_signed_urls_and_disable_proxy_routing(self):
        private = "https://source.test/video?token=top-secret"
        failed = subprocess.CompletedProcess([], 1, b"", ("[http] " + private + ": failed").encode())
        with patch.object(media.subprocess, "run", return_value=failed) as runner:
            with self.assertRaisesRegex(media.MediaJobError, "private URL") as error:
                media.run_media(("ffmpeg", "-i", private))
        self.assertNotIn("top-secret", str(error.exception))
        self.assertEqual(runner.call_args.args[0][:3], ["nice", "-n", "10"])
        self.assertEqual(runner.call_args.kwargs["env"]["no_proxy"], "*")
        self.assertFalse(any(key.lower() in media.PROXY_VARIABLES for key in runner.call_args.kwargs["env"]))


class NativeLookupTests(unittest.TestCase):
    def test_only_observed_404_code_5_is_absence(self):
        from unittest.mock import Mock
        client = Mock()
        client.post.side_effect = D2HTTPError(404, "file not found", code=5)
        self.assertIsNone(_lookup(client, "/known/path"))
        for status, code in ((404, None), (404, 7), (401, 5), (500, 5)):
            client.post.side_effect = D2HTTPError(status, "failed", code=code)
            with self.subTest(status=status, code=code), self.assertRaises(D2HTTPError):
                _lookup(client, "/known/path")

    def test_direct_link_must_match_source_and_be_unexpired(self):
        from unittest.mock import Mock
        source = {"path": "/vps/抖音视频/user/video.mp4", "content_identity": "cid", "size": 20}
        response = {"path": source["path"], "content_identity": "cid", "file_size": "20",
                    "expire_at": "9999999999", "download_address": "https://source.test/private?token=secret"}
        client = Mock()
        client.post.return_value = response
        self.assertEqual(direct_url(client, source), response["download_address"])
        for change in ({"content_identity": "replacement"}, {"file_size": "21"}, {"expire_at": "1"},
                       {"download_address": "file:///source.mp4"}):
            client.post.return_value = {**response, **change}
            with self.subTest(change=change), self.assertRaises(media.MediaJobError) as error:
                direct_url(client, source)
            self.assertNotIn("token=", str(error.exception))

    def test_paths_cannot_escape_independent_namespace(self):
        source = {"path": "/vps/抖音视频/user/video.mp4", "identity": "id", "content_identity": "cid", "size": 20}
        job = {"source": source, "operation": "transcode", "user_id": "user", "aweme_id": "9001",
               "playback_path": "/vps/抖音视频/_web_playback/user/9001.h264.mp4", "expected_duration": 15}
        self.assertEqual(validate_job(job)["identity"], "id")
        for change in ({"playback_path": source["path"]}, {"user_id": "../user"},
                       {"aweme_id": "../9001"}, {"expected_duration": float("nan")}, {"operation": "download"}):
            with self.subTest(change=change), self.assertRaises(media.MediaJobError):
                validate_job({**job, **change})


if __name__ == "__main__":
    unittest.main()
