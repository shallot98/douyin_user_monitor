from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from douyin_user_monitor.monitor.storage import MonitorStorage


class CookiePoolMergeOnSaveTests(unittest.TestCase):
    def test_save_keeps_newer_cookie_pool_from_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            storage = MonitorStorage(path)
            base = storage.load_state()
            base["monitoring"]["cookie_pool"] = {
                "updated_at": "2026-07-23T01:00:00+08:00",
                "items": [{"id": "old", "name": "old", "cookie": "a", "is_active": True}],
            }
            storage.save_state(base)

            # Concurrent API wrote a newer pool to disk
            on_disk = json.loads(path.read_text(encoding="utf-8"))
            on_disk["monitoring"]["cookie_pool"] = {
                "updated_at": "2026-07-23T02:00:00+08:00",
                "items": [
                    {"id": "a", "name": "默认", "cookie": "old", "is_active": False},
                    {"id": "b", "name": "新号", "cookie": "new", "is_active": True},
                ],
            }
            path.write_text(json.dumps(on_disk, ensure_ascii=False, indent=2), encoding="utf-8")

            # Stale coverage snapshot without pool items
            stale = storage.load_state()
            stale["monitoring"]["cookie_pool"] = {
                "updated_at": "2026-07-23T00:30:00+08:00",
                "items": [],
            }
            stale["monitoring"]["last_run_at"] = "2026-07-23T02:05:00+08:00"
            storage.save_state(stale)

            final = json.loads(path.read_text(encoding="utf-8"))
            pool = final["monitoring"]["cookie_pool"]
            self.assertEqual(len(pool["items"]), 2)
            self.assertEqual(pool["updated_at"], "2026-07-23T02:00:00+08:00")
            self.assertEqual(final["monitoring"]["last_run_at"], "2026-07-23T02:05:00+08:00")



    def test_same_timestamp_prefers_higher_use_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            storage = MonitorStorage(path)
            ts = "2026-07-23T02:00:00.000+08:00"
            base = storage.load_state()
            base["monitoring"]["cookie_pool"] = {
                "updated_at": ts,
                "rotation_cursor": 0,
                "items": [
                    {"id": "a", "name": "A", "cookie": "c1", "use_count": 1, "is_active": True},
                ],
            }
            storage.save_state(base)

            on_disk = json.loads(path.read_text(encoding="utf-8"))
            on_disk["monitoring"]["cookie_pool"] = {
                "updated_at": ts,
                "rotation_cursor": 1,
                "items": [
                    {"id": "a", "name": "A", "cookie": "c1", "use_count": 5, "is_active": True},
                ],
            }
            path.write_text(json.dumps(on_disk, ensure_ascii=False, indent=2), encoding="utf-8")

            stale = storage.load_state()
            stale["monitoring"]["cookie_pool"] = {
                "updated_at": ts,
                "rotation_cursor": 0,
                "items": [
                    {"id": "a", "name": "A", "cookie": "c1", "use_count": 1, "is_active": True},
                ],
            }
            storage.save_state(stale)

            final = json.loads(path.read_text(encoding="utf-8"))
            pool = final["monitoring"]["cookie_pool"]
            self.assertEqual(pool["items"][0]["use_count"], 5)
            self.assertEqual(pool["rotation_cursor"], 1)


    def test_update_state_is_atomic_rmw(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            storage = MonitorStorage(path)
            base = storage.load_state()
            base["monitoring"]["cookie_pool"] = {
                "updated_at": "2026-07-23T01:00:00+08:00",
                "items": [{"id": "a", "use_count": 0, "cookie": "c"}],
            }
            storage.save_state(base)

            def mutator(state):
                pool = state["monitoring"]["cookie_pool"]
                pool["items"][0]["use_count"] = 3
                pool["updated_at"] = "2026-07-23T03:00:00+08:00"

            storage.update_state(mutator)
            final = storage.load_state()
            self.assertEqual(final["monitoring"]["cookie_pool"]["items"][0]["use_count"], 3)


if __name__ == "__main__":
    unittest.main()
