"""新作品标注后台队列：下载流程不等模型，结论之后锁内补进记录。"""

import asyncio
import tempfile
import unittest
from pathlib import Path

from douyin_user_monitor.monitor.content_filter import RESULT_VERSION
from douyin_user_monitor.monitor.content_filter_queue import ContentFilterQueue
from douyin_user_monitor.monitor.storage import MonitorStorage


class FakeFilter:
    def __init__(self, gate=None):
        self.calls = []
        self.gate = gate

    def applies_to(self, record):
        return record.get("source") != "skip"

    async def evaluate(self, user, detail, record, loader):
        self.calls.append((user["id"], record["aweme_id"], detail.get("desc")))
        if self.gate is not None:
            await self.gate.wait()
        return {"version": RESULT_VERSION, "status": "ok", "verdict": "keep"}


class ContentFilterQueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.storage = MonitorStorage(Path(tmp.name) / "monitor_users.json")
        state = self.storage.load_state()
        state["users"] = [{"id": "u1", "sec_user_id": "s1", "nickname": "n"}]
        self.storage.save_state(state)

    def _put_records(self, records):
        self.storage.update_state(lambda _state: self.storage.records.put("u1", records))

    def _record(self, aweme_id="a1", **extra):
        return {"aweme_id": aweme_id, "downloaded_at": "2026-10-31T00:00:00+08:00", "media_type": "video", **extra}

    async def _run(self, queue):
        queue.start()
        self.addAsyncCleanup(queue.stop)
        await asyncio.wait_for(queue.drain(), timeout=5)

    async def test_result_is_patched_into_committed_record(self):
        self._put_records([self._record()])
        fake = FakeFilter()
        queue = ContentFilterQueue(fake, self.storage, loader=None, record_wait_seconds=())
        self.assertTrue(queue.submit({"id": "u1"}, self._record(), {"desc": "标题", "aweme_id": "a1", "video": {}}))
        await self._run(queue)
        self.assertEqual(fake.calls, [("u1", "a1", "标题")])
        self.assertEqual(self.storage.records.get("u1")[0]["content_filter"]["status"], "ok")

    async def test_waits_for_record_committed_after_submit(self):
        """下载流程先入队、整轮同步结束才提交记录。"""
        fake = FakeFilter()
        queue = ContentFilterQueue(fake, self.storage, loader=None, record_wait_seconds=(0.05, 0.05, 0.05))
        queue.submit({"id": "u1"}, self._record(), {"desc": "x"})
        queue.start()
        self.addAsyncCleanup(queue.stop)
        await asyncio.sleep(0.02)
        self._put_records([self._record()])
        await asyncio.wait_for(queue.drain(), timeout=5)
        self.assertEqual(len(fake.calls), 1)
        self.assertIn("content_filter", self.storage.records.get("u1")[0])

    async def test_does_not_overwrite_newer_result(self):
        """补打任务先写了当前版本结论：队列跳过，不覆盖。"""
        existing = {"version": RESULT_VERSION, "status": "ok", "verdict": "reject"}
        self._put_records([self._record(content_filter=existing)])
        fake = FakeFilter()
        queue = ContentFilterQueue(fake, self.storage, loader=None, record_wait_seconds=())
        queue.submit({"id": "u1"}, self._record(), {})
        await self._run(queue)
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.storage.records.get("u1")[0]["content_filter"]["verdict"], "reject")

    async def test_result_written_concurrently_during_evaluation_wins(self):
        self._put_records([self._record()])
        gate = asyncio.Event()
        fake = FakeFilter(gate)
        queue = ContentFilterQueue(fake, self.storage, loader=None, record_wait_seconds=())
        queue.submit({"id": "u1"}, self._record(), {})
        queue.start()
        self.addAsyncCleanup(queue.stop)
        while not fake.calls:
            await asyncio.sleep(0.01)
        newer = {"version": RESULT_VERSION, "status": "ok", "verdict": "reject"}
        self._put_records([self._record(content_filter=newer)])
        gate.set()
        await asyncio.wait_for(queue.drain(), timeout=5)
        self.assertEqual(self.storage.records.get("u1")[0]["content_filter"]["verdict"], "reject")

    async def test_duplicates_and_non_applicable_are_not_queued(self):
        fake = FakeFilter()
        queue = ContentFilterQueue(fake, self.storage, loader=None, record_wait_seconds=())
        self.assertTrue(queue.submit({"id": "u1"}, self._record(), {}))
        self.assertFalse(queue.submit({"id": "u1"}, self._record(), {}))
        self.assertFalse(queue.submit({"id": "u1"}, self._record("a2", source="skip"), {}))
        self.assertEqual(queue.pending, 1)

    async def test_full_queue_drops_instead_of_blocking(self):
        queue = ContentFilterQueue(FakeFilter(), self.storage, loader=None, maxsize=1, record_wait_seconds=())
        self.assertTrue(queue.submit({"id": "u1"}, self._record("a1"), {}))
        self.assertFalse(queue.submit({"id": "u1"}, self._record("a2"), {}))

    async def test_enqueue_unlabeled_only_recent_and_missing(self):
        old = {**self._record("old"), "downloaded_at": "2026-09-01T00:00:00+08:00"}
        done = self._record("done", content_filter={"version": RESULT_VERSION, "status": "ok"})
        stale = self._record("stale", content_filter={"version": RESULT_VERSION - 1, "status": "ok"})
        self._put_records([self._record("new"), old, done, stale])
        queue = ContentFilterQueue(FakeFilter(), self.storage, loader=None, record_wait_seconds=())
        added = queue.enqueue_unlabeled(self.storage.load_state()["users"], since="2026-10-28T00:00:00+08:00")
        self.assertEqual(added, 2)


if __name__ == "__main__":
    unittest.main()
