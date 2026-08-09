import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

from douyin_user_monitor.api import monitor as monitor_api
from douyin_user_monitor.main import app
from douyin_user_monitor.monitor.user_table import (
    query_monitor_users,
    wants_paginated_users,
)

CHINA_TZ = timezone(timedelta(hours=8))


def build_record(publish_time: str, media_type: str = "video", image_count: int = 0, total_size_bytes: int = 10):
    return {
        "aweme_id": f"aweme-{publish_time}-{media_type}",
        "publish_time": publish_time,
        "downloaded_at": publish_time,
        "media_type": media_type,
        "image_count": image_count,
        "total_size_bytes": total_size_bytes,
    }


def sample_users():
    return [
        {
            "id": "user-1",
            "nickname": "活跃用户",
            "sec_user_id": "sec-1",
            "profile_url": "https://www.douyin.com/user/sec-1",
            "enabled": True,
            "account_status": "normal",
            "account_status_label": "正常",
            "created_at": "2026-01-01T00:00:00+08:00",
            "last_checked_at": "2026-03-27T11:00:00+08:00",
            "downloaded_count": 5,
            "downloaded_aweme_ids": ["1", "2", "3", "4", "5"],
            "history_sync": {"status": "idle"},
            "download_records": [
                build_record("2026-03-26T10:00:00+08:00", "video", total_size_bytes=100),
                build_record("2026-03-20T18:00:00+08:00", "image", image_count=3, total_size_bytes=180),
            ],
        },
        {
            "id": "user-2",
            "nickname": "沉默用户",
            "sec_user_id": "sec-2",
            "enabled": True,
            "account_status": "deleted",
            "account_status_label": "已注销",
            "created_at": "2026-01-02T00:00:00+08:00",
            "last_checked_at": "2026-03-20T11:00:00+08:00",
            "downloaded_count": 2,
            "downloaded_aweme_ids": ["6", "7"],
            "history_sync": {"status": "paused"},
            "download_records": [
                build_record("2026-03-10T09:00:00+08:00", "image", image_count=5, total_size_bytes=200),
            ],
        },
        {
            "id": "user-3",
            "nickname": "无记录用户",
            "sec_user_id": "sec-3",
            "enabled": False,
            "account_status": "banned",
            "account_status_label": "已封禁",
            "created_at": "2026-01-03T00:00:00+08:00",
            "last_checked_at": None,
            "downloaded_count": 0,
            "downloaded_aweme_ids": [],
            "history_sync": {"status": "running"},
            "download_records": [],
        },
        {
            "id": "user-4",
            "nickname": "高下载",
            "sec_user_id": "sec-4",
            "enabled": True,
            "account_status": "normal",
            "account_status_label": "正常",
            "created_at": "2026-01-04T00:00:00+08:00",
            "last_checked_at": "2026-03-27T10:00:00+08:00",
            "downloaded_count": 20,
            "downloaded_aweme_ids": [str(i) for i in range(20)],
            "history_sync": {"status": "completed"},
            "download_records": [
                build_record("2026-03-25T10:00:00+08:00", "video", total_size_bytes=50),
            ],
        },
    ]


class WantsPaginatedTests(unittest.TestCase):
    def test_compat_a_defaults_to_dashboard(self):
        self.assertFalse(wants_paginated_users())
        self.assertFalse(wants_paginated_users(view="dashboard"))

    def test_page_or_table_view_enables_pagination(self):
        self.assertTrue(wants_paginated_users(page=1))
        self.assertTrue(wants_paginated_users(page_size=20))
        self.assertTrue(wants_paginated_users(view="table"))


class QueryMonitorUsersTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 3, 27, 12, 0, tzinfo=CHINA_TZ)
        self.users = sample_users()

    def test_default_sort_downloads_desc(self):
        result = query_monitor_users(self.users, page=1, page_size=20, now=self.now)
        self.assertEqual(result["total"], 4)
        self.assertEqual(result["page"], 1)
        self.assertEqual(result["page_size"], 20)
        ids = [item["id"] for item in result["items"]]
        self.assertEqual(ids[0], "user-4")
        self.assertEqual(result["items"][0]["downloaded_count"], 20)
        self.assertNotIn("is_silent_7d", result["items"][0])

    def test_monitor_enabled_paused(self):
        enabled = query_monitor_users(self.users, monitor="enabled", now=self.now)
        paused = query_monitor_users(self.users, monitor="paused", now=self.now)
        self.assertEqual(enabled["total"], 3)
        self.assertEqual(paused["total"], 1)
        self.assertEqual(paused["items"][0]["id"], "user-3")

    def test_account_abnormal_and_deleted(self):
        abnormal = query_monitor_users(self.users, account="abnormal", now=self.now)
        deleted = query_monitor_users(self.users, account="deleted", now=self.now)
        self.assertEqual(abnormal["total"], 2)
        self.assertEqual({u["id"] for u in abnormal["items"]}, {"user-2", "user-3"})
        self.assertEqual(deleted["total"], 1)
        self.assertEqual(deleted["items"][0]["id"], "user-2")

    def test_activity_filters_disjoint_unknown_and_silent(self):
        silent7 = query_monitor_users(self.users, activity="silent_7d", now=self.now)
        unknown = query_monitor_users(self.users, activity="publish_unknown", now=self.now)
        silent_ids = {u["id"] for u in silent7["items"]}
        unknown_ids = {u["id"] for u in unknown["items"]}
        self.assertIn("user-2", silent_ids)
        self.assertEqual(unknown_ids, {"user-3"})
        self.assertFalse(silent_ids & unknown_ids)

    def test_search_and_pagination(self):
        result = query_monitor_users(
            self.users,
            search="用户",
            sort_by="downloads",
            sort_order="desc",
            page=1,
            page_size=20,
            now=self.now,
        )
        # 活跃用户 / 沉默用户 / 无记录用户
        self.assertEqual(result["total"], 3)

        page1 = query_monitor_users(self.users, page=1, page_size=2, now=self.now)
        page2 = query_monitor_users(self.users, page=2, page_size=2, now=self.now)
        self.assertEqual(page1["total"], 4)
        self.assertEqual(page1["total_pages"], 2)
        self.assertEqual(len(page1["items"]), 2)
        self.assertEqual(len(page2["items"]), 2)
        self.assertEqual(page1["items"][0]["id"], "user-4")
        # cross-page continuity: page1 last downloads >= page2 first
        self.assertGreaterEqual(
            page1["items"][-1]["downloaded_count"],
            page2["items"][0]["downloaded_count"],
        )

    def test_page_clamp_and_page_size_fallback(self):
        result = query_monitor_users(self.users, page=99, page_size=0, now=self.now)
        self.assertEqual(result["page_size"], 20)  # invalid → default
        self.assertEqual(result["page"], 1)
        self.assertEqual(result["total_pages"], 1)

        oversized = query_monitor_users(self.users, page=1, page_size=500, now=self.now)
        self.assertEqual(oversized["page_size"], 100)

    def test_backfill_filter(self):
        active = query_monitor_users(self.users, backfill="active", now=self.now)
        paused = query_monitor_users(self.users, backfill="paused", now=self.now)
        self.assertEqual(active["total"], 1)
        self.assertEqual(active["items"][0]["id"], "user-3")
        self.assertEqual(paused["items"][0]["id"], "user-2")

    def test_history_sync_present_on_row(self):
        result = query_monitor_users(self.users, search="高下载", now=self.now)
        self.assertEqual(result["items"][0]["history_sync"]["status"], "completed")


class UsersRoutePaginationTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.users = sample_users()

    def test_legacy_users_without_page_still_dashboard_shape(self):
        with patch.object(
            monitor_api.monitor_service,
            "list_dashboard_users",
            return_value={"users": [{"id": "x"}], "recent_activity": []},
        ) as mocked:
            # AsyncMock if the method is async
            from unittest.mock import AsyncMock

            mocked.side_effect = None
            monitor_api.monitor_service.list_dashboard_users = AsyncMock(
                return_value={"users": [{"id": "x"}], "recent_activity": []}
            )
            response = self.client.get("/api/monitor/users")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["code"], 200)
        self.assertIn("users", body["data"])
        self.assertIn("recent_activity", body["data"])

    def test_paginated_users_route(self):
        with patch.object(monitor_api.monitor_service, "list_users", return_value=self.users):
            response = self.client.get(
                "/api/monitor/users",
                params={
                    "page": 1,
                    "page_size": 2,
                    "sort_by": "downloads",
                    "sort_order": "desc",
                    "monitor": "enabled",
                },
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        data = body["data"]
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["page_size"], 2)
        self.assertEqual(data["total_pages"], 2)
        self.assertEqual(len(data["items"]), 2)
        self.assertEqual(data["items"][0]["id"], "user-4")
        self.assertEqual(data["applied"]["monitor"], "enabled")

    def test_recent_activity_route_no_profile_hydration(self):
        with patch.object(monitor_api.monitor_service, "list_users", return_value=self.users) as mocked:
            response = self.client.get("/api/monitor/recent_activity")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["code"], 200)
        self.assertIn("recent_activity", body["data"])
        self.assertIsInstance(body["data"]["recent_activity"], list)
        mocked.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
