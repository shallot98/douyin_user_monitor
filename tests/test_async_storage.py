from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from douyin_user_monitor.monitor.storage import MonitorStorage, build_default_state

READ_WAIT_SECONDS = 2


def state_named(name: str) -> dict:
    state = build_default_state()
    state["users"] = [{"id": "user-1", "nickname": name}]
    return state


class AsyncStorageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.path.write_text(json.dumps(state_named("before")), encoding="utf-8")
        self.storage = MonitorStorage(self.path)

    async def _wait_for_reader(self, entered: threading.Event):
        ready = await asyncio.to_thread(entered.wait, READ_WAIT_SECONDS)
        self.assertTrue(ready, "snapshot reader did not start")

    def _blocked_reader(self, entered: threading.Event, release: threading.Event):
        read = self.storage._read_state_snapshot

        def blocked():
            snapshot = read()
            entered.set()
            if not release.wait(READ_WAIT_SECONDS):
                raise TimeoutError("test did not release the snapshot reader")
            return snapshot

        return blocked

    async def test_coalesces_cold_reads_without_blocking_event_loop(self):
        entered, release = threading.Event(), threading.Event()
        blocked = self._blocked_reader(entered, release)
        with patch.object(self.storage, "_read_state_snapshot", side_effect=blocked) as read:
            first = asyncio.create_task(self.storage.load_state_async())
            second = asyncio.create_task(self.storage.load_state_async())
            try:
                await self._wait_for_reader(entered)
                await asyncio.sleep(0)
                self.assertFalse(first.done())
                self.assertFalse(second.done())
                self.assertEqual(read.call_count, 1)
            finally:
                release.set()
                snapshots = await asyncio.gather(first, second)
        self.assertIs(snapshots[0], snapshots[1])
        self.assertEqual(snapshots[0]["users"][0]["nickname"], "before")

    async def test_synchronous_write_wins_over_pending_async_snapshot(self):
        entered, release = threading.Event(), threading.Event()
        blocked = self._blocked_reader(entered, release)
        with patch.object(self.storage, "_read_state_snapshot", side_effect=blocked):
            pending = asyncio.create_task(self.storage.load_state_async())
            try:
                await self._wait_for_reader(entered)
                self.storage.save_state(state_named("after"))
            finally:
                release.set()
                result = await pending
        self.assertEqual(result["users"][0]["nickname"], "after")
        self.assertIs(result, self.storage.load_state())
        self.assertEqual(json.loads(self.path.read_text())["users"][0]["nickname"], "after")

    async def test_external_replace_does_not_stamp_old_snapshot_as_fresh(self):
        entered, release = threading.Event(), threading.Event()
        blocked = self._blocked_reader(entered, release)
        writer = MonitorStorage(self.path)
        with patch.object(self.storage, "_read_state_snapshot", side_effect=blocked):
            pending = asyncio.create_task(self.storage.load_state_async())
            try:
                await self._wait_for_reader(entered)
                writer.save_state(state_named("after"))
            finally:
                release.set()
                first = await pending
        self.assertEqual(first["users"][0]["nickname"], "before")
        current = await self.storage.load_state_async()
        self.assertEqual(current["users"][0]["nickname"], "after")

    async def test_opened_file_stamp_survives_replace_during_normalization(self):
        normalize = self.storage._normalize_state
        writer = MonitorStorage(self.path)

        def replace_during_normalization(state):
            writer.save_state(state_named("after"))
            normalize(state)

        with patch.object(self.storage, "_normalize_state", side_effect=replace_during_normalization):
            first = await self.storage.load_state_async()
        self.assertEqual(first["users"][0]["nickname"], "before")
        self.assertEqual(self.storage.load_state()["users"][0]["nickname"], "after")

    async def test_same_mtime_replacement_invalidates_sync_and_async_cache(self):
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.storage.save_state(state_named("before"))
                before = self.path.stat()
                replacement = self.path.with_suffix(".replacement")
                replacement.write_text(json.dumps(state_named("after")), encoding="utf-8")
                os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
                replacement.replace(self.path)
                self.assertEqual(before.st_mtime_ns, self.path.stat().st_mtime_ns)
                self.assertNotEqual(before.st_ino, self.path.stat().st_ino)
                state = await self.storage.load_state_async() if asynchronous else self.storage.load_state()
                self.assertEqual(state["users"][0]["nickname"], "after")

    async def test_own_atomic_save_keeps_cache_fresh_without_rereading(self):
        state = state_named("after")
        self.storage.save_state(state)
        with patch.object(self.storage, "_read_state_snapshot", side_effect=AssertionError("unexpected disk read")):
            self.assertIs(self.storage.load_state(), state)
            self.assertIs(await self.storage.load_state_async(), state)

    async def test_invalid_json_is_not_replaced_or_treated_as_empty_state(self):
        corrupt = '{"users": '
        self.path.write_text(corrupt, encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            await self.storage.load_state_async()
        with self.assertRaises(json.JSONDecodeError):
            self.storage.load_state()
        mutate = Mock()
        with self.assertRaises(json.JSONDecodeError):
            self.storage.update_state(mutate)
        mutate.assert_not_called()
        self.assertEqual(self.path.read_text(encoding="utf-8"), corrupt)

    async def test_io_error_is_propagated_without_writing(self):
        before = self.path.read_bytes()
        with (
            patch.object(self.storage, "_read_state_snapshot", side_effect=PermissionError("read denied")),
            patch.object(self.storage, "_write_json_atomic") as write,
        ):
            with self.assertRaises(PermissionError):
                await self.storage.load_state_async()
            with self.assertRaises(PermissionError):
                self.storage.load_state()
            write.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
