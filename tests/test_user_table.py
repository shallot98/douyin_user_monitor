import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
from record_helpers import patch_monitor_users

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

    def test_sort_created_desc_and_asc(self):
        desc = query_monitor_users(
            self.users,
            sort_by="created",
            sort_order="desc",
            page=1,
            page_size=20,
            now=self.now,
        )
        asc = query_monitor_users(
            self.users,
            sort_by="created",
            sort_order="asc",
            page=1,
            page_size=20,
            now=self.now,
        )
        desc_ids = [item["id"] for item in desc["items"]]
        asc_ids = [item["id"] for item in asc["items"]]
        self.assertEqual(desc_ids, ["user-4", "user-3", "user-2", "user-1"])
        self.assertEqual(asc_ids, ["user-1", "user-2", "user-3", "user-4"])
        self.assertEqual(desc["applied"]["sort_by"], "created")
        self.assertEqual(desc["applied"]["sort_order"], "desc")
        self.assertEqual(asc["applied"]["sort_order"], "asc")
        self.assertTrue(all(item.get("created_at") for item in asc["items"]))

    def test_sort_created_missing_timestamp_sinks_last(self):
        users = self.users + [
            {
                "id": "user-5",
                "nickname": "无添加时间",
                "sec_user_id": "sec-5",
                "enabled": True,
                "account_status": "normal",
                "created_at": None,
                "last_checked_at": None,
                "downloaded_count": 0,
                "downloaded_aweme_ids": [],
                "history_sync": {"status": "idle"},
                "download_records": [],
            }
        ]
        for order in ("desc", "asc"):
            result = query_monitor_users(
                users,
                sort_by="created",
                sort_order=order,
                page=1,
                page_size=20,
                now=self.now,
            )
            ids = [item["id"] for item in result["items"]]
            self.assertEqual(ids[-1], "user-5", f"order={order}")

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

    def test_paginated_users_route_derives_counts_from_record_store(self):
        # 拆分后 list_users 不带记录：视频/图集数、最近发布必须来自 RecordStore
        with patch_monitor_users(self.users, method="list_users"):
            response = self.client.get(
                "/api/monitor/users",
                params={"page": 1, "page_size": 20, "search": "user-1"},
            )
        self.assertEqual(response.status_code, 200)
        row = next(item for item in response.json()["data"]["items"] if item["id"] == "user-1")
        self.assertEqual((row["known_video_posts"], row["known_image_posts"]), (1, 1))
        self.assertTrue(row["activity_known"])

    def test_paginated_users_route_sort_by_created(self):
        seen = {}
        for order, expected_first in (("desc", "user-4"), ("asc", "user-1")):
            with patch.object(monitor_api.monitor_service, "list_users", return_value=self.users):
                response = self.client.get(
                    "/api/monitor/users",
                    params={
                        "page": 1,
                        "page_size": 20,
                        "sort_by": "created",
                        "sort_order": order,
                    },
                )
            self.assertEqual(response.status_code, 200)
            data = response.json()["data"]
            self.assertEqual(data["applied"]["sort_by"], "created")
            self.assertEqual(data["applied"]["sort_order"], order)
            self.assertEqual(data["items"][0]["id"], expected_first)
            self.assertTrue(data["items"][0]["created_at"])
            seen[order] = [item["id"] for item in data["items"]]
        self.assertEqual(seen["desc"], list(reversed(seen["asc"])))

    def test_recent_activity_route_no_profile_hydration(self):
        with patch.object(monitor_api.monitor_service, "list_users", return_value=self.users) as mocked:
            response = self.client.get("/api/monitor/recent_activity")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["code"], 200)
        self.assertIn("recent_activity", body["data"])
        self.assertIsInstance(body["data"]["recent_activity"], list)
        mocked.assert_called_once_with()



def scored_record(publish_time: str, index: int, score, *, version: int = 5, status: str = "ok"):
    record = build_record(publish_time)
    record["aweme_id"] = f"{publish_time}-{index}"
    appearance = {} if score is None else {"borderline": {"score": score}}
    record["content_filter"] = {"version": version, "status": status, "appearance": appearance}
    return record


def value_user(user_id: str, publish_time: str, scores, *, extra_unscored: int = 0):
    records = [scored_record(publish_time, index, score) for index, score in enumerate(scores)]
    records += [build_record(f"{publish_time[:10]}T0{index}:00:00+08:00") for index in range(extra_unscored)]
    return {
        "id": user_id,
        "nickname": user_id,
        "sec_user_id": f"sec-{user_id}",
        "enabled": True,
        "account_status": "normal",
        "created_at": "2026-01-01T00:00:00+08:00",
        "downloaded_count": len(records),
        "download_records": records,
    }


class UserValueTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 30, 12, 0, tzinfo=CHINA_TZ)
        recent = "2026-09-25T10:00:00+08:00"
        self.users = [
            # 擦边低：5 条已评分，均分 0.8（无 borderline 分档按 0 分）
            value_user("low", recent, [0, 1, 1, 2, None]),
            # 分数高但 100 天没发 → 长期沉默
            value_user("silent", "2026-06-22T10:00:00+08:00", [3, 4, 3, 3, 4]),
            # 只评了 2 条：待评分，不参与擦边判定
            value_user("pending", recent, [0, 0], extra_unscored=6),
            # 正常：均分 2.6，最近在发
            value_user("good", recent, [3, 3, 2, 3, 2]),
        ]

    def query(self, **kwargs):
        return query_monitor_users(self.users, page=1, page_size=20, now=self.now, **kwargs)

    def ids(self, **kwargs):
        return [item["id"] for item in self.query(**kwargs)["items"]]

    def test_score_metrics_on_rows(self):
        rows = {item["id"]: item for item in self.query()["items"]}
        self.assertEqual(rows["low"]["scored_works"], 5)
        self.assertEqual(rows["low"]["avg_borderline_score"], 0.8)
        self.assertEqual(rows["low"]["high_score_works"], 0)
        self.assertEqual(rows["good"]["high_score_ratio"], 0.6)
        self.assertEqual(rows["pending"]["scored_works"], 2)
        self.assertTrue(rows["pending"]["is_score_pending"])
        self.assertEqual(rows["low"]["low_value_reasons"], ["low_score"])
        self.assertEqual(rows["silent"]["low_value_reasons"], ["long_silent"])
        self.assertFalse(rows["good"]["is_low_value"])

    def test_failed_or_old_version_scores_are_not_counted(self):
        user = value_user("mixed", "2026-09-25T10:00:00+08:00", [3])
        user["download_records"].append(scored_record("2026-09-24T10:00:00+08:00", 9, 0, status="error"))
        user["download_records"].append(scored_record("2026-09-24T10:00:00+08:00", 8, 0, version=4))
        row = query_monitor_users([user], page=1, page_size=20, now=self.now)["items"][0]
        self.assertEqual(row["scored_works"], 1)
        self.assertEqual(row["avg_borderline_score"], 3.0)

    def test_value_filters(self):
        self.assertEqual(sorted(self.ids(value="low_value")), ["low", "silent"])
        self.assertEqual(self.ids(value="low_score"), ["low"])
        self.assertEqual(self.ids(value="long_silent"), ["silent"])
        self.assertEqual(self.ids(value="score_pending"), ["pending"])
        self.assertEqual(self.query(value="bogus")["applied"]["value"], "all")

    def test_silent_windows(self):
        self.assertEqual(self.ids(activity="silent_90d"), ["silent"])
        self.assertEqual(self.ids(activity="silent_180d"), [])
        self.assertEqual(self.ids(activity="silent_60d"), ["silent"])

    def test_summary_counts_value_groups(self):
        from douyin_user_monitor.monitor.statistics import build_user_statistics

        summary = build_user_statistics(self.users, now=self.now)["summary"]
        self.assertEqual(summary["low_value_users"], 2)
        self.assertEqual(summary["low_score_users"], 1)
        self.assertEqual(summary["long_silent_users"], 1)
        self.assertEqual(summary["score_pending_users"], 1)
        self.assertEqual(summary["silent_users_90d"], 1)

    def test_score_sort_puts_pending_last_both_ways(self):
        self.assertEqual(self.ids(sort_by="avg_score", sort_order="asc"), ["low", "good", "silent", "pending"])
        self.assertEqual(self.ids(sort_by="avg_score", sort_order="desc"), ["silent", "good", "low", "pending"])
        self.assertEqual(self.ids(sort_by="high_score_ratio", sort_order="asc")[0], "low")


if __name__ == "__main__":
    unittest.main()
