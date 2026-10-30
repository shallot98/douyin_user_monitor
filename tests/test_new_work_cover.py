import asyncio
import tempfile
import threading
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

from douyin_user_monitor.monitor.new_work_cover import NewWorkCoverPublisher

PUBLISH_WAIT_SECONDS = 2


class NewWorkCoverPublisherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.download_root = self.root / "download"
        self.source = self.download_root / "user" / "work.cover.jpg"
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(b"cover bytes delegated to the image store")
        self.user = {"id": "user-1", "nickname": "new name"}
        self.record = {
            "aweme_id": "work-1",
            "downloaded_at": "2026-09-14T12:00:00+08:00",
            "cover_file": "user/work.cover.jpg",
        }
        self.store = Mock()
        self.publisher = NewWorkCoverPublisher(self.download_root, self.store)

    async def test_publishes_existing_source_without_mutating_download_record(self):
        before = deepcopy((self.user, self.record))

        await self.publisher.publish(self.user, self.record)

        self.store.publish.assert_called_once_with("user-1", self.record, self.source)
        self.assertEqual((self.user, self.record), before)
        self.assertTrue(self.source.is_file())

    async def test_slow_image_store_does_not_block_event_loop(self):
        release = threading.Event()

        def publish(*_args):
            if not release.wait(PUBLISH_WAIT_SECONDS):
                raise TimeoutError("image publication blocked the event loop")

        self.store.publish.side_effect = publish
        asyncio.get_running_loop().call_soon(release.set)

        await self.publisher.publish(self.user, self.record)

        self.store.publish.assert_called_once()

    async def test_missing_cover_role_is_an_explicit_failure(self):
        record = {key: value for key, value in self.record.items() if key != "cover_file"}
        with self.assertRaisesRegex(ValueError, "缺少封面文件"):
            await self.publisher.publish(self.user, record)
        self.store.publish.assert_not_called()

    async def test_missing_file_does_not_publish_metadata(self):
        self.source.unlink()
        with self.assertRaisesRegex(FileNotFoundError, "封面文件不存在"):
            await self.publisher.publish(self.user, self.record)
        self.store.publish.assert_not_called()

    async def test_directory_cannot_be_published_as_an_image(self):
        record = {**self.record, "cover_file": "user"}
        with self.assertRaises(FileNotFoundError):
            await self.publisher.publish(self.user, record)
        self.store.publish.assert_not_called()

    async def test_path_outside_download_root_is_rejected(self):
        external = self.root / "other.cover.jpg"
        external.write_bytes(b"external file")
        for name in (str(external), "../other.cover.jpg"):
            with self.subTest(path=name), self.assertRaisesRegex(ValueError, "不在下载目录内"):
                await self.publisher.publish(self.user, {**self.record, "cover_file": name})
        self.store.publish.assert_not_called()

    async def test_symlink_cannot_escape_download_root(self):
        external = self.root / "other.cover.jpg"
        external.write_bytes(b"external file")
        self.source.unlink()
        self.source.symlink_to(external)

        with self.assertRaisesRegex(ValueError, "不在下载目录内"):
            await self.publisher.publish(self.user, self.record)

        self.store.publish.assert_not_called()

    async def test_store_failure_surfaces_and_keeps_source_available(self):
        self.store.publish.side_effect = OSError("cover image cannot be decoded")

        with self.assertRaisesRegex(OSError, "cannot be decoded"):
            await self.publisher.publish(self.user, self.record)

        self.assertTrue(self.source.is_file())


if __name__ == "__main__":
    unittest.main()
