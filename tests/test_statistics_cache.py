from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from douyin_user_monitor.api import monitor as monitor_api
from douyin_user_monitor.main import API_KEY_ENV, app
from douyin_user_monitor.monitor.storage import MonitorStorage


class StatisticsCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "state.json"
        self.storage = MonitorStorage(self.path)
        self._write_users(1)
        self.client = TestClient(app)
        contexts = ExitStack()
        self.addCleanup(contexts.close)
        contexts.enter_context(patch.dict(os.environ, {API_KEY_ENV: ""}))
        contexts.enter_context(patch.object(monitor_api, "_STATISTICS_CACHE", None))
        contexts.enter_context(patch.object(monitor_api.monitor_service, "_storage", self.storage))
        # Read uncached disk content to exercise the route's cache independently of storage's cache.
        self.read_users = contexts.enter_context(patch.object(
            monitor_api.monitor_service, "list_users",
            side_effect=lambda: json.loads(self.path.read_text())["users"],
        ))

    def _write_users(self, count):
        self.path.write_text(json.dumps({
            "users": [{"id": f"user-{index}", "nickname": "test", "enabled": True} for index in range(count)],
            "monitoring": {},
        }))

    def _total_users(self):
        response = self.client.get("/api/monitor/statistics")
        self.assertEqual(response.status_code, 200)
        return response.json()["data"]["summary"]["total_users"]

    def test_unchanged_state_reuses_aggregate(self):
        self.assertEqual(self._total_users(), 1)
        self.assertEqual(self._total_users(), 1)
        self.read_users.assert_called_once_with()

    def test_atomic_replacement_with_equal_mtime_invalidates_aggregate(self):
        self.assertEqual(self._total_users(), 1)
        previous = self.path.stat()
        replacement = self.path.with_suffix(".replacement")
        payload = json.loads(self.path.read_text())
        payload["users"].append({"id": "user-new", "nickname": "new", "enabled": True})
        replacement.write_text(json.dumps(payload))
        os.utime(replacement, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        replacement.replace(self.path)
        self.assertEqual(self.path.stat().st_mtime_ns, previous.st_mtime_ns)
        self.assertNotEqual(self.path.stat().st_ino, previous.st_ino)

        self.assertEqual(self._total_users(), 2)
        self.assertEqual(self.read_users.call_count, 2)


if __name__ == "__main__":
    unittest.main()
