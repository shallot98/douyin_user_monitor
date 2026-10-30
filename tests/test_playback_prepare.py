from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from douyin_user_monitor.monitor.downloader import AwemeAssetDownloader
from douyin_user_monitor.monitor.playback_media import has_faststart
from douyin_user_monitor.monitor.playback_prepare import PlaybackRequest, prepare_cover, prepare_playback
from douyin_user_monitor.monitor.video_source import build_original_video_source


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def media_command(arguments: list[str]) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *arguments],
        check=True, capture_output=True, timeout=10,
    )


def make_video(target: Path, *, compatible: bool, faststart: bool = False) -> None:
    encoding = (
        ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac"]
        if compatible else ["-c:v", "mpeg4", "-q:v", "4", "-c:a", "pcm_s16le", "-f", "matroska"]
    )
    media_command([
        "-threads", "1", "-filter_threads", "1", "-f", "lavfi", "-i", "testsrc2=size=96x64:rate=12",
        "-f", "lavfi", "-i", "anullsrc=sample_rate=8000:channel_layout=mono", "-t", "0.5",
        *encoding, "-threads", "1", *(["-movflags", "+faststart"] if faststart else []), str(target),
    ])


class MediaFixtures(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_temp = tempfile.TemporaryDirectory(prefix="playback-media-fixtures-")
        cls.addClassCleanup(cls.fixture_temp.cleanup)
        cls.fixtures = Path(cls.fixture_temp.name)
        make_video(cls.fixtures / "ready.mp4", compatible=True, faststart=True)
        make_video(cls.fixtures / "late.mp4", compatible=True)
        make_video(cls.fixtures / "original.mkv", compatible=False)
        media_command([
            "-threads", "1", "-filter_threads", "1", "-i", str(cls.fixtures / "ready.mp4"),
            "-frames:v", "1", "-c:v", "mjpeg", "-threads", "1", str(cls.fixtures / "cover.jpg"),
        ])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="playback-media-case-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.target = self.root / "video.fast.mp4"

    def source(self, kind: str, name: str = "original.mp4") -> Path:
        target = self.root / name
        shutil.copyfile(self.fixtures / kind, target)
        return target


class PlaybackPreparationTests(MediaFixtures):
    async def test_reuses_ready_original_without_creating_a_second_file(self):
        original = self.source("ready.mp4")
        before = digest(original)
        result = await prepare_playback(PlaybackRequest(archive_path=original, target_path=self.target))
        self.assertEqual(result.path, original)
        self.assertEqual(result.method, "reuse")
        self.assertFalse(result.created)
        self.assertFalse(self.target.exists())
        self.assertEqual(digest(original), before)

    async def test_remux_moves_index_only_in_separate_playback_file(self):
        original = self.source("late.mp4")
        before = digest(original)
        self.assertFalse(has_faststart(original))
        result = await prepare_playback(PlaybackRequest(archive_path=original, target_path=self.target))
        self.assertEqual(result.method, "remux")
        self.assertTrue(result.media.ready)
        self.assertTrue(has_faststart(self.target))
        self.assertEqual(digest(original), before)
        self.assertFalse(has_faststart(original))

    async def test_transcode_preserves_original_and_produces_h264_aac_faststart(self):
        original = self.source("original.mkv")
        before = digest(original)
        result = await prepare_playback(PlaybackRequest(archive_path=original, target_path=self.target))
        self.assertEqual(result.method, "transcode")
        self.assertEqual(result.media.video_codec, "h264")
        self.assertEqual(result.media.audio_codecs, ("aac",))
        self.assertTrue(result.media.ready)
        self.assertLessEqual(result.media.width, 96)
        self.assertLessEqual(result.media.height, 64)
        self.assertEqual(digest(original), before)

    async def test_supplied_ready_candidate_is_published_without_reencoding(self):
        original = self.source("original.mkv")
        candidate = self.source("ready.mp4", "candidate.source.part")
        before, candidate_hash = digest(original), digest(candidate)
        result = await prepare_playback(PlaybackRequest(
            archive_path=original, target_path=self.target, candidate_path=candidate,
        ))
        self.assertEqual(result.method, "download")
        self.assertFalse(candidate.exists())
        self.assertEqual(digest(self.target), candidate_hash)
        self.assertEqual(digest(original), before)

    async def test_failed_encoder_preserves_original_and_previous_target(self):
        original = self.source("original.mkv")
        shutil.copyfile(original, self.target)
        before = digest(original)

        async def fail(arguments):
            Path(arguments[-1]).write_bytes(b"incomplete encoding")
            raise RuntimeError("encoder interrupted")

        with patch("douyin_user_monitor.monitor.playback_prepare.run_media_tool", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "encoder interrupted"):
                await prepare_playback(PlaybackRequest(archive_path=original, target_path=self.target))
        self.assertEqual(digest(original), before)
        self.assertEqual(digest(self.target), before)
        self.assertEqual(list(self.root.glob("*.part")), [])

    async def test_invalid_candidate_is_an_explicit_error(self):
        original = self.source("ready.mp4")
        before = digest(original)
        candidate = self.root / "invalid.source.part"
        candidate.write_bytes(b"not a video")
        with self.assertRaisesRegex(RuntimeError, "ffprobe failed"):
            await prepare_playback(PlaybackRequest(
                archive_path=original, target_path=self.target, candidate_path=candidate,
            ))
        self.assertFalse(self.target.exists())
        self.assertEqual(digest(original), before)

    async def test_rejects_original_as_output(self):
        original = self.source("ready.mp4")
        before = digest(original)
        with self.assertRaisesRegex(ValueError, "must not replace"):
            await prepare_playback(PlaybackRequest(archive_path=original, target_path=original))
        self.assertEqual(digest(original), before)

    async def test_cover_is_a_real_jpeg_and_keeps_source_unchanged(self):
        original = self.source("ready.mp4")
        before = digest(original)
        cover = self.root / "cover.jpg"
        await prepare_cover(original, cover)
        probe = subprocess.run([
            "ffprobe", "-v", "error", "-show_entries", "stream=codec_name,width,height",
            "-of", "json", str(cover),
        ], check=True, capture_output=True, text=True, timeout=10)
        stream = json.loads(probe.stdout)["streams"][0]
        self.assertEqual(stream["codec_name"], "mjpeg")
        self.assertGreater(stream["width"], 0)
        self.assertEqual(digest(original), before)
        self.assertEqual(list(self.root.glob("*.part")), [])


class PlaybackDownloadFlowTests(MediaFixtures):
    def detail(self, candidate_url: str, *, cover: bool) -> dict:
        return {
            "aweme_id": "9001", "desc": "preserve original", "create_time": 1700000000,
            "video": {
                "cover": {"url_list": ["https://media.test/cover"]} if cover else {},
                "bit_rate": [{
                    "is_h265": 0, "is_bytevc1": 0, "bit_rate": 200000,
                    "play_addr": {"width": 96, "height": 64, "url_list": [candidate_url]},
                }],
            },
        }

    async def download(self, downloader, detail, handler):
        with (
            patch.object(downloader, "_resolve_video_source", new=AsyncMock(
                return_value=build_original_video_source("https://media.test/archive"),
            )),
            patch.object(downloader, "_create_shared_client", side_effect=lambda: httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
            )),
        ):
            return await downloader.download_aweme_assets(
                aweme_id="9001", sec_user_id="sec", user_nickname="user", aweme_detail=detail, headers={},
            )

    async def test_reuse_downloads_once_and_generates_missing_cover(self):
        downloader = AwemeAssetDownloader(self.root)
        raw = (self.fixtures / "ready.mp4").read_bytes()
        calls = []

        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(200, content=raw)

        result = await self.download(downloader, self.detail("https://media.test/archive", cover=False), handler)
        self.assertEqual(calls, ["/archive"])
        self.assertEqual(result["video_file"], result["fast_play_file"])
        self.assertEqual(len(result["files"]), 2)
        self.assertEqual(result["downloaded_file_count"], 2)
        original, cover = self.root / result["video_file"], self.root / result["cover_file"]
        self.assertEqual(original.read_bytes(), raw)
        self.assertEqual(result["total_size_bytes"], len(raw) + cover.stat().st_size)

    async def test_cover_retry_keeps_original_and_reuses_completed_playback(self):
        downloader = AwemeAssetDownloader(self.root)
        original = (self.fixtures / "original.mkv").read_bytes()
        detail = self.detail("https://media.test/candidate", cover=True)
        calls = []

        def handler(request):
            path = request.url.path
            calls.append(path)
            if path == "/cover" and calls.count(path) == 1:
                return httpx.Response(503)
            content = original if path == "/archive" else (self.fixtures / (
                "ready.mp4" if path == "/candidate" else "cover.jpg"
            )).read_bytes()
            return httpx.Response(200, content=content)

        with self.assertRaises(httpx.HTTPStatusError):
            await self.download(downloader, detail, handler)
        result = await self.download(downloader, detail, handler)
        self.assertEqual(calls, ["/archive", "/candidate", "/cover", "/cover"])
        self.assertEqual((self.root / result["video_file"]).read_bytes(), original)
        self.assertEqual(result["existing_file_count"], 2)
        self.assertEqual(result["downloaded_file_count"], 1)
        self.assertEqual(len(result["files"]), 3)
        self.assertEqual(list(self.root.rglob("*.part")), [])

    async def test_rejected_archive_cache_is_retried_after_upstream_recovers(self):
        downloader = AwemeAssetDownloader(self.root)
        detail = self.detail("https://media.test/archive", cover=False)
        with self.assertRaisesRegex(RuntimeError, "ffprobe failed"):
            await self.download(downloader, detail, lambda request: httpx.Response(200, content=b"<html>error</html>"))
        original = next((self.root / "user").glob("*.mp4"))
        self.assertEqual(original.read_bytes(), b"<html>error</html>")
        expected = (self.fixtures / "ready.mp4").read_bytes()
        calls = []

        def recovered(request):
            calls.append(request.url.path)
            return httpx.Response(200, content=expected)

        result = await self.download(downloader, detail, recovered)

        self.assertEqual(calls, ["/archive"])
        self.assertEqual(original.read_bytes(), expected)
        self.assertTrue((self.root / result["cover_file"]).is_file())
        self.assertEqual(result["downloaded_file_count"], 2)
        self.assertEqual(list(self.root.rglob("*.part")), [])

    async def test_rejected_candidate_does_not_redownload_valid_archive(self):
        downloader = AwemeAssetDownloader(self.root)
        detail = self.detail("https://media.test/candidate", cover=False)
        expected = (self.fixtures / "ready.mp4").read_bytes()
        calls = []

        def invalid_candidate(request):
            calls.append(request.url.path)
            body = expected if request.url.path == "/archive" else b"<html>error</html>"
            return httpx.Response(200, content=body)

        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, "ffprobe failed"):
                await self.download(downloader, detail, invalid_candidate)

        self.assertEqual(calls, ["/archive", "/candidate", "/candidate"])
        original = next((self.root / "user").glob("*.mp4"))
        self.assertEqual(original.read_bytes(), expected)
        self.assertEqual(list(self.root.rglob("*.part")), [])

    async def test_invalid_retry_preserves_rejected_cache_and_cleans_temporary_copy(self):
        downloader = AwemeAssetDownloader(self.root)
        detail = self.detail("https://media.test/archive", cover=False)
        initial_body = b"<html>first failure</html>"
        with self.assertRaisesRegex(RuntimeError, "ffprobe failed"):
            await self.download(downloader, detail, lambda request: httpx.Response(200, content=initial_body))
        original = next((self.root / "user").glob("*.mp4"))

        with self.assertRaisesRegex(RuntimeError, "ffprobe failed"):
            await self.download(downloader, detail, lambda request: httpx.Response(200, content=b"another failure"))

        self.assertEqual(original.read_bytes(), initial_body)
        self.assertEqual(list(self.root.rglob("*.part")), [])
