import asyncio
import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx
from PIL import Image

from douyin_user_monitor.monitor.content_filter import ContentFilterError
from douyin_user_monitor.monitor.content_filter_media import ArchiveMediaLoader

HAS_FFMPEG = shutil.which("ffmpeg") is not None


def jpeg(color="white", size=(900, 1600)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "JPEG")
    return buffer.getvalue()


class FakeOpenList:
    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    async def resolve_stream_url(self, relative, *, sec_user_id=None, verified_paths=()):
        self.calls.append(relative)
        return self.mapping[relative]


class FakeAliases:
    def paths_for(self, user, aweme_id, relative):
        return ()


class FakeCovers:
    def __init__(self, path=None):
        self.path = path

    def lookup(self, user_id, record):
        return SimpleNamespace(path=self.path) if self.path else None


class ArchiveMediaLoaderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.download_root = self.root / "download"
        self.download_root.mkdir()
        self.user = {"id": "u1", "sec_user_id": "sec"}

    def _loader(self, openlist, covers=None, transport=None):
        return ArchiveMediaLoader(self.download_root, openlist, FakeAliases(), covers or FakeCovers(),
                                  transport=transport)

    def test_remote_image_post_downloads_and_shrinks(self):
        pictures = {"https://cdn.test/1.jpg": jpeg("red"), "https://cdn.test/2.jpg": jpeg("blue")}
        openlist = FakeOpenList({"n/图片/w/1.jpg": "https://cdn.test/1.jpg", "n/图片/w/2.jpg": "https://cdn.test/2.jpg"})
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=pictures[str(request.url)]))
        record = {"aweme_id": "1", "media_type": "image", "cover_file": "n/图片/w/cover.jpg",
                  "files": ["n/图片/w/1.jpg", "n/图片/w/2.jpg", "n/图片/w/cover.jpg"]}

        media = asyncio.run(self._loader(openlist, transport=transport).prepare(self.user, record, 6))

        self.assertEqual(media.source, "archive")
        self.assertEqual(len(media.images), 2)
        self.assertNotIn("n/图片/w/cover.jpg", openlist.calls)
        with Image.open(io.BytesIO(media.images[0])) as image:
            self.assertEqual(min(image.size), 512)

    def test_remote_image_http_error_raises(self):
        openlist = FakeOpenList({"n/1.jpg": "https://cdn.test/1.jpg"})
        transport = httpx.MockTransport(lambda request: httpx.Response(403))
        record = {"aweme_id": "1", "media_type": "image", "files": ["n/1.jpg"]}

        with self.assertRaises(ContentFilterError):
            asyncio.run(self._loader(openlist, transport=transport).prepare(self.user, record, 6))

    @unittest.skipUnless(HAS_FFMPEG, "需要 ffmpeg")
    def test_remote_video_prefers_fast_play_and_uses_cover_store(self):
        clip = self.root / "remote.fast.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=320x568:d=6:r=25",
                        "-c:v", "libx264", "-g", "25", "-pix_fmt", "yuv420p", "-y", str(clip)], check=True)
        cover = self.root / "cover.jpg"
        cover.write_bytes(jpeg("black", (320, 568)))
        # ffmpeg 读 URL 与读本地路径同一套逻辑，这里用本地路径代替网盘直链
        openlist = FakeOpenList({"n/a.fast.mp4": str(clip)})
        record = {"aweme_id": "1", "media_type": "video", "video_file": "n/a.mp4", "fast_play_file": "n/a.fast.mp4",
                  "files": ["n/a.mp4", "n/a.fast.mp4"], "video_duration_ms": 6000}

        media = asyncio.run(self._loader(openlist, FakeCovers(cover)).prepare(self.user, record, 3))

        self.assertEqual(openlist.calls, ["n/a.fast.mp4"])
        self.assertEqual(media.source, "archive")
        self.assertTrue(media.cover_used)
        self.assertIsNone(media.motion)
        self.assertEqual(len(media.images), 4)  # 封面 + 3 张关键帧

    @unittest.skipUnless(HAS_FFMPEG, "需要 ffmpeg")
    def test_local_file_used_when_still_present(self):
        (self.download_root / "n").mkdir()
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=320x568:d=4:r=25",
                        "-c:v", "libx264", "-g", "25", "-pix_fmt", "yuv420p", "-y",
                        str(self.download_root / "n" / "a.mp4")], check=True)
        openlist = FakeOpenList({})
        record = {"aweme_id": "1", "media_type": "video", "video_file": "n/a.mp4", "files": ["n/a.mp4"],
                  "video_duration_ms": 4000}

        media = asyncio.run(self._loader(openlist).prepare(self.user, record, 2))

        self.assertEqual(openlist.calls, [])
        self.assertEqual(media.source, "local")
        self.assertIsNotNone(media.motion)


if __name__ == "__main__":
    unittest.main()
