import tempfile
import time
import unittest
from pathlib import Path

from douyin_user_monitor.monitor.dashboard_payload import (
    build_recent_activity,
    project_dashboard_users,
    project_public_status,
)
from douyin_user_monitor.monitor.storage import MonitorStorage, build_default_state


class DashboardPayloadTests(unittest.TestCase):
    def test_project_dashboard_users_drops_heavy_fields(self):
        users = [
            {
                "id": "u1",
                "nickname": "Alice",
                "sec_user_id": "sec-1",
                "enabled": True,
                "downloaded_count": 3,
                "downloaded_aweme_ids": ["a", "b", "c"],
                "download_records": [
                    {
                        "aweme_id": "1",
                        "media_type": "video",
                        "downloaded_at": "2026-08-01T10:00:00+08:00",
                        "downloaded_file_count": 1,
                        "extra_blob": "x" * 1000,
                    },
                    {
                        "aweme_id": "2",
                        "media_type": "image",
                        "image_count": 3,
                        "downloaded_at": "2026-08-02T10:00:00+08:00",
                        "downloaded_file_count": 3,
                    },
                    {
                        "aweme_id": "3",
                        "media_type": "video",
                        "downloaded_at": "2026-08-03T10:00:00+08:00",
                        "downloaded_file_count": 1,
                    },
                    {
                        "aweme_id": "4",
                        "media_type": "video",
                        "downloaded_at": "2026-08-04T10:00:00+08:00",
                        "downloaded_file_count": 1,
                    },
                ],
            }
        ]

        payload = project_dashboard_users(users, recent_records_per_user=3, activity_limit=2)
        user = payload["users"][0]
        self.assertNotIn("downloaded_aweme_ids", user)
        self.assertEqual(len(user["download_records"]), 3)
        self.assertEqual(user["download_records"][0]["aweme_id"], "4")
        self.assertNotIn("extra_blob", user["download_records"][-1])
        self.assertEqual(len(payload["recent_activity"]), 2)
        self.assertEqual(payload["recent_activity"][0]["user_nickname"], "Alice")
        self.assertEqual(payload["recent_activity"][0]["aweme_id"], "4")

    def test_project_public_status_masks_cookie_and_drops_samples(self):
        status = project_public_status(
            {
                "is_running": True,
                "cookie_liveness": {
                    "status": "healthy",
                    "signal": "download",
                    "samples": [{"aweme_id": "1"}, {"aweme_id": "2"}],
                    "samples_used": 2,
                    "fresh_count": 1,
                },
                "cookie_pool": {
                    "items": [
                        {
                            "id": "c1",
                            "name": "main",
                            "cookie": "a" * 80,
                            "is_active": True,
                            "status": "healthy",
                        }
                    ],
                    "updated_at": "now",
                    "rotation_cursor": 0,
                },
                "coverage_progress": {
                    "phase": "running",
                    "processed_user_ids": ["u1", "u2", "u3"],
                    "checked_users": 3,
                    "downloaded_items": 1,
                    "errors": [],
                    "priority_user_ids": [],
                },
            }
        )
        self.assertNotIn("samples", status["cookie_liveness"])
        self.assertEqual(status["cookie_liveness"]["status"], "healthy")
        item = status["cookie_pool"]["items"][0]
        self.assertNotIn("cookie", item)
        self.assertIn("cookie_preview", item)
        self.assertEqual(status["coverage_progress"]["processed_user_count"], 3)
        self.assertNotIn("processed_user_ids", status["coverage_progress"])


class StorageCacheTests(unittest.TestCase):
    def test_load_state_reuses_mtime_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            storage = MonitorStorage(path)
            first = storage.load_state()
            first["users"].append({"id": "u1", "nickname": "n", "sec_user_id": "s"})
            # mutate cache object intentionally; next load should return same object if mtime unchanged
            second = storage.load_state()
            self.assertIs(first, second)
            self.assertEqual(len(second["users"]), 1)

            storage.save_state(build_default_state())
            third = storage.load_state()
            self.assertIsNot(second, third)
            self.assertEqual(third["users"], [])


if __name__ == "__main__":
    unittest.main()
