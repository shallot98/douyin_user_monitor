from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from douyin_user_monitor.monitor.storage import MonitorStorage


class CookiePoolMergeOnSaveTests(unittest.TestCase):
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
