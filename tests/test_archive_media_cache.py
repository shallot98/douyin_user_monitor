from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from douyin_user_monitor.api.archive_media_cache import ArchiveMediaCache


class _Opener:
    """记录回源次数的假上游；body 关闭情况也记下来，确认不泄漏连接。"""

    def __init__(self, payload: bytes, *, status: int = 200, declared: int | None = None, gate=None):
        self.payload = payload
        self.status = status
        self.declared = len(payload) if declared is None else declared
        self.gate = gate
        self.calls = 0
        self.closed = 0

    async def __call__(self):
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        owner = self

        async def body():
            try:
                for start in range(0, len(owner.payload), 4):
                    yield owner.payload[start:start + 4]
            finally:
                owner.closed += 1

        return self.status, {"Content-Length": str(self.declared)}, body()


class ArchiveMediaCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "cache"
        self.cache = ArchiveMediaCache(self.root, max_file_bytes=64, max_total_bytes=100)

    async def test_fetches_once_then_serves_from_disk(self):
        opener = _Opener(b"VIDEO-BYTES")
        first = await self.cache.get_or_fetch("k", ".mp4", opener)
        second = await self.cache.get_or_fetch("k", ".mp4", opener)
        self.assertEqual(first, second)
        self.assertEqual(first.read_bytes(), b"VIDEO-BYTES")
        self.assertEqual(first.suffix, ".mp4")
        self.assertEqual((opener.calls, opener.closed), (1, 1))

    async def test_concurrent_requests_share_one_upstream_fetch(self):
        gate = asyncio.Event()
        opener = _Opener(b"SHARED", gate=gate)
        pending = [asyncio.create_task(self.cache.get_or_fetch("k", ".mp4", opener)) for _ in range(3)]
        await asyncio.sleep(0)
        gate.set()
        paths = await asyncio.gather(*pending)
        self.assertEqual(len(set(paths)), 1)
        self.assertEqual(opener.calls, 1)

    async def test_cancelled_waiter_does_not_cancel_shared_fetch(self):
        gate = asyncio.Event()
        opener = _Opener(b"SURVIVES", gate=gate)
        abandoned = asyncio.create_task(self.cache.get_or_fetch("k", ".mp4", opener))
        await asyncio.sleep(0)
        abandoned.cancel()
        survivor = asyncio.create_task(self.cache.get_or_fetch("k", ".mp4", opener))
        await asyncio.sleep(0)
        gate.set()
        path = await survivor
        self.assertEqual(path.read_bytes(), b"SURVIVES")
        self.assertEqual(opener.calls, 1)

    async def test_declared_oversize_is_skipped_and_remembered(self):
        opener = _Opener(b"x" * 65)
        self.assertIsNone(await self.cache.get_or_fetch("k", ".mp4", opener))
        self.assertIsNone(await self.cache.get_or_fetch("k", ".mp4", opener))
        self.assertEqual((opener.calls, opener.closed), (1, 1))
        self.assertEqual(list(self.root.glob("*")) if self.root.exists() else [], [])

    async def test_undeclared_oversize_stops_reading_and_leaves_no_file(self):
        opener = _Opener(b"x" * 80, declared=10)
        self.assertIsNone(await self.cache.get_or_fetch("k", ".mp4", opener))
        self.assertEqual(opener.closed, 1)
        self.assertEqual(list(self.root.iterdir()), [])

    async def test_non_200_is_not_cached_and_remembered(self):
        opener = _Opener(b"PART", status=206)
        self.assertIsNone(await self.cache.get_or_fetch("k", ".mp4", opener))
        self.assertIsNone(await self.cache.get_or_fetch("k", ".mp4", opener))
        self.assertEqual((opener.calls, opener.closed), (1, 1))

    async def test_truncated_body_raises_and_is_retried_later(self):
        truncated = _Opener(b"SHORT", declared=20)
        with self.assertRaises(OSError):
            await self.cache.get_or_fetch("k", ".mp4", truncated)
        self.assertEqual(list(self.root.iterdir()), [])
        healthy = _Opener(b"FULL")
        path = await self.cache.get_or_fetch("k", ".mp4", healthy)
        self.assertEqual(path.read_bytes(), b"FULL")

    async def test_evicts_least_recently_used_over_total_budget(self):
        old = await self.cache.get_or_fetch("old", ".mp4", _Opener(b"a" * 40))
        mid = await self.cache.get_or_fetch("mid", ".mp4", _Opener(b"b" * 40))
        os.utime(old, (1, 1))
        os.utime(mid, (2, 2))
        self.cache.lookup("old", ".mp4")  # 访问后 old 变成最近使用
        newest = await self.cache.get_or_fetch("new", ".mp4", _Opener(b"c" * 40))
        self.assertTrue(old.exists())
        self.assertFalse(mid.exists())
        self.assertTrue(newest.exists())

    async def test_low_disk_space_clears_cache_and_skips_fetch(self):
        kept = await self.cache.get_or_fetch("old", ".mp4", _Opener(b"OLD"))
        opener = _Opener(b"NEW")
        full_disk = shutil._ntuple_diskusage(total=100, used=100, free=0)
        low = ArchiveMediaCache(self.root, max_file_bytes=64, max_total_bytes=100, min_free_bytes=1)
        with patch("douyin_user_monitor.api.archive_media_cache.shutil.disk_usage", return_value=full_disk):
            self.assertIsNone(await low.get_or_fetch("new", ".mp4", opener))
        self.assertEqual(opener.calls, 0)
        self.assertFalse(kept.exists())
        # 空间恢复后照常缓存，不因为之前跳过而被记成不可缓存
        self.assertEqual((await low.get_or_fetch("new", ".mp4", opener)).read_bytes(), b"NEW")


if __name__ == "__main__":
    unittest.main()
