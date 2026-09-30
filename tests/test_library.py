from __future__ import annotations

import os
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

from fastapi.testclient import TestClient
from record_helpers import seed_state

from douyin_user_monitor.api import monitor as monitor_api
from douyin_user_monitor.main import API_KEY_ENV, app
from douyin_user_monitor.monitor.library import build_library_index, parse_library_query, query_library
from douyin_user_monitor.monitor.storage import MonitorStorage


def work(
    aweme_id: str,
    *,
    publish: Optional[str] = "2026-08-01T12:00:00+08:00",
    score: Any = 2,
    version: int = 3,
    status: str = "ok",
    media_type: str = "video",
    duration: int = 10000,
    category: str = "normal",
    desc: str = "",
    summary: str = "",
    outfit: Optional[List[str]] = None,
    enhancers: Optional[List[str]] = None,
    tags: Optional[Dict[str, List[str]]] = None,
    subs: Optional[Dict[str, int]] = None,
    violation: bool = False,
    borderline: Any = "auto",
) -> Dict[str, Any]:
    if borderline == "auto":
        borderline = {"score": score, "chest": 0, "hip": 0, "figure": 0, "overall": 0, "camera": 0, "motion": 0,
                      "enhancers": enhancers or [], "violation": violation, **(subs or {})}
    return {
        "aweme_id": aweme_id,
        "desc": desc,
        "publish_time": publish,
        "media_type": media_type,
        "video_duration_ms": duration,
        "files": [f"u/{aweme_id}.mp4"],
        "content_filter": {
            "version": version,
            "status": status,
            "verdict": "pass",
            "category": category,
            "confidence": 0.9,
            "tags": {"subject": [], "scene": [], "content": [], "style": [], "features": [], **(tags or {})},
            "appearance": {"age": "成年", "outfit": outfit or [], "exposed_areas": [], "camera_focus": [],
                           "poses": [], "borderline": borderline},
            "summary": summary,
        },
    }


def query(index, **params):
    return query_library(index, parse_library_query({key: str(value) for key, value in params.items()}))


def ids(result) -> List[str]:
    return [entry.aweme_id for entry in result["entries"]]


USERS = [{"id": "u1", "nickname": "Alice"}, {"id": "u2", "nickname": "Bob"}]


class EligibilityTests(unittest.TestCase):
    def test_only_version3_or_5_ok_scored_works_enter_index(self):
        records = {
            "u1": [
                work("ok"),
                work("v5", version=5, publish="2026-07-01T12:00:00+08:00"),
                work("v4", version=4),
                work("zero", score=0),
                work("v2", version=2),
                work("err", status="error"),
                work("nobl", borderline=None),
                work("boolscore", score=True),
                {"aweme_id": "nofilter"},
            ],
            "ghost": [work("deleted-user")],
        }
        index = build_library_index(USERS, records)
        self.assertEqual([entry.aweme_id for entry in index], ["ok", "v5"])
        self.assertEqual(query(index)["eligible_total"], 2)

    def test_user_cache_reuses_unchanged_users_only(self):
        records = {"u1": [work("a")], "u2": [work("b")]}
        cache: Dict[str, Any] = {}
        first = {entry.aweme_id: entry for entry in build_library_index(USERS, records, cache)}
        records = {**records, "u2": [work("b", score=4)]}
        second = {entry.aweme_id: entry for entry in build_library_index(USERS, records, cache)}
        self.assertIs(second["a"], first["a"])
        self.assertIsNot(second["b"], first["b"])
        self.assertEqual(second["b"].score, 4)
        renamed = [{"id": "u1", "nickname": "Alicia"}]
        third = build_library_index(renamed, records, cache)
        self.assertEqual([(entry.aweme_id, entry.nickname) for entry in third], [("a", "Alicia")])
        self.assertEqual(set(cache), {"u1"})


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.index = build_library_index(USERS, {
            "u1": [
                work("a", publish="2026-08-03T01:00:00+08:00", score=5, media_type="video", duration=30000,
                     outfit=["泳装"], tags={"scene": ["海边"], "style": ["写真"]}, desc="海边 Swim",
                     subs={"chest": 3, "camera": 2}, violation=True, enhancers=["湿身"]),
                work("b", publish="2026-08-02T23:30:00+08:00", score=3, media_type="image", duration=0,
                     outfit=["紧身"], tags={"scene": ["室内"], "style": ["写真"]}, category="ad",
                     summary="室内自拍"),
            ],
            "u2": [
                work("c", publish="2026-07-31T10:00:00+08:00", score=1, outfit=["泳装", "紧身"],
                     tags={"scene": ["室内"], "content": ["舞蹈"]}, duration=5000, subs={"chest": 1}),
                work("d", publish=None, score=2, category="screenshot", duration=60000),
            ],
        })

    def test_default_newest_order_and_missing_publish_last(self):
        self.assertEqual(ids(query(self.index)), ["a", "b", "c", "d"])
        self.assertEqual(ids(query(self.index, sort="oldest")), ["d", "c", "b", "a"])

    def test_score_and_duration_sorts(self):
        self.assertEqual(ids(query(self.index, sort="score")), ["a", "b", "d", "c"])
        self.assertEqual(ids(query(self.index, sort="duration")), ["d", "a", "c", "b"])

    def test_sub_score_sorts_rank_by_that_sub_score_then_total(self):
        records = [
            work("low", score=5, subs={"chest": 0}, publish="2026-05-04T10:00:00+08:00"),
            work("mid", score=2, subs={"chest": 2}, publish="2026-05-03T10:00:00+08:00"),
            work("top_old", score=4, subs={"chest": 3}, publish="2026-05-01T10:00:00+08:00"),
            work("top_new", score=4, subs={"chest": 3}, publish="2026-05-02T10:00:00+08:00"),
            work("top_low_total", score=3, subs={"chest": 3}, publish="2026-05-05T10:00:00+08:00"),
        ]
        index = build_library_index(USERS, {"u1": records})

        # 胸部 3 分的排前面；同分按总分；总分也相同维持最新优先
        self.assertEqual(ids(query(index, sort="chest")), ["top_new", "top_old", "top_low_total", "mid", "low"])

    def test_random_sort_stable_per_seed(self):
        many = build_library_index(USERS, {"u1": [work(f"w{i}") for i in range(60)]})
        first = ids(query(many, sort="random", seed=7, page_size=120))
        again = ids(query(many, sort="random", seed=7, page_size=120))
        other = ids(query(many, sort="random", seed=8, page_size=120))
        self.assertEqual(first, again)
        self.assertNotEqual(first, other)
        self.assertEqual(sorted(first), sorted(other))
        self.assertEqual(query(many, sort="random", seed=7)["seed"], 7)
        self.assertIsInstance(query(many, sort="random")["seed"], int)
        self.assertIsNone(query(many)["seed"])

    def test_score_range_and_sub_scores(self):
        self.assertEqual(ids(query(self.index, score_min=3)), ["a", "b"])
        self.assertEqual(ids(query(self.index, score_min=2, score_max=3)), ["b", "d"])
        self.assertEqual(ids(query(self.index, chest_min=1)), ["a", "c"])
        self.assertEqual(ids(query(self.index, chest_min=1, camera_min=2)), ["a"])
        self.assertEqual(ids(query(self.index, score_min=0)), ["a", "b", "c", "d"])

    def test_media_category_users_violation(self):
        self.assertEqual(ids(query(self.index, media_type="image")), ["b"])
        self.assertEqual(ids(query(self.index, category="ad,screenshot")), ["b", "d"])
        self.assertEqual(ids(query(self.index, user_ids="u2")), ["c", "d"])
        self.assertEqual(ids(query(self.index, violation=1)), ["a"])

    def test_or_within_and_across_groups(self):
        self.assertEqual(ids(query(self.index, outfit="泳装")), ["a", "c"])
        self.assertEqual(ids(query(self.index, scene="海边,室内")), ["a", "b", "c"])
        self.assertEqual(ids(query(self.index, outfit="泳装", scene="室内")), ["c"])
        self.assertEqual(ids(query(self.index, style="写真", content="舞蹈")), [])
        self.assertEqual(ids(query(self.index, enhancers="湿身")), ["a"])

    def test_exclude(self):
        self.assertEqual(ids(query(self.index, exclude="outfit:紧身")), ["a", "d"])
        self.assertEqual(ids(query(self.index, exclude="category:ad,scene:海边")), ["c", "d"])

    def test_date_range_uses_china_time(self):
        self.assertEqual(ids(query(self.index, date_from="2026-08-03")), ["a"])
        self.assertEqual(ids(query(self.index, date_from="2026-08-01", date_to="2026-08-02")), ["b"])
        self.assertEqual(ids(query(self.index, date_to="2026-07-31")), ["c"])

    def test_text_search_case_insensitive(self):
        self.assertEqual(ids(query(self.index, q="swim")), ["a"])
        self.assertEqual(ids(query(self.index, q="自拍")), ["b"])
        self.assertEqual(ids(query(self.index, q="bob")), ["c", "d"])

    def test_pagination_page_size_clamp_and_beyond_last(self):
        many = build_library_index(USERS, {"u1": [work(f"w{i:03d}") for i in range(30)]})
        first = query(many, page_size=5)
        self.assertEqual(first["page_size"], 12)
        self.assertEqual((first["total"], first["total_pages"]), (30, 3))
        last = query(many, page=3, page_size=12)
        self.assertEqual(last["page"], 3)
        self.assertEqual(len(last["entries"]), 6)
        beyond = query(many, page=9, page_size=12)
        self.assertEqual((beyond["page"], beyond["total"], beyond["total_pages"]), (9, 30, 3))
        self.assertEqual(beyond["entries"], [])
        self.assertEqual(query(many, page_size=999)["page_size"], 120)

    def test_facets_exclude_own_group(self):
        result = query(self.index, outfit="泳装")
        self.assertEqual(ids(result), ["a", "c"])
        outfit = {item["value"]: item["count"] for item in result["facets"]["outfit"]}
        self.assertEqual(outfit, {"泳装": 2, "紧身": 2})
        scene = {item["value"]: item["count"] for item in result["facets"]["scene"]}
        self.assertEqual(scene, {"海边": 1, "室内": 1})
        users = {item["user_id"]: (item["nickname"], item["count"]) for item in result["facets"]["users"]}
        self.assertEqual(users, {"u1": ("Alice", 1), "u2": ("Bob", 1)})
        self.assertEqual(result["users_total"], 2)
        media = {item["value"]: item["count"] for item in query(self.index, media_type="image")["facets"]["media_type"]}
        self.assertEqual(media, {"video": 3, "image": 1})

    def test_histogram_ignores_score_filter(self):
        result = query(self.index, score_min=4, media_type="video")
        self.assertEqual(ids(result), ["a"])
        self.assertEqual(result["score_histogram"], {"1": 1, "2": 1, "3": 0, "4": 0, "5": 1})

    def test_invalid_params(self):
        for params in (
            {"page": "0"}, {"page": "x"}, {"sort": "hot"}, {"seed": "abc"}, {"score_max": "6"},
            {"score_min": "4", "score_max": "2"}, {"chest_min": "4"}, {"camera_min": "4"},
            {"media_type": "audio"}, {"category": "normal,porn"}, {"exclude": "nope:x"}, {"exclude": "category:typo"},
            {"exclude": "outfit"}, {"violation": "yes"}, {"date_from": "2026/08/01"},
            {"date_from": "2026-08-05", "date_to": "2026-08-01"},
        ):
            with self.subTest(params=params), self.assertRaises(ValueError):
                parse_library_query(params)

    def test_filtering_large_index_is_fast(self):
        records = {"u1": [work(f"w{i}", outfit=["泳装"] if i % 2 else ["紧身"], tags={"scene": ["室内"]},
                              desc=f"作品{i}") for i in range(25000)]}
        index = build_library_index(USERS, records)
        started = time.perf_counter()
        result = query(index, outfit="泳装", scene="室内", q="作品1", sort="score")
        elapsed = time.perf_counter() - started
        self.assertGreater(result["total"], 0)
        self.assertLess(elapsed, 0.5)


class LibraryRouteTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.storage = MonitorStorage(Path(temporary.name) / "state.json")
        self.addCleanup(self.storage.records.close)
        seed_state(self.storage, {"users": [
            {"id": "u1", "nickname": "Alice", "download_records": [work("7001", score=4), work("7002", score=0)]},
            {"id": "u2", "nickname": "Bob", "download_records": [work("7003", media_type="image")]},
        ], "monitoring": {}})
        self.client = TestClient(app)
        contexts = ExitStack()
        self.addCleanup(contexts.close)
        contexts.enter_context(patch.dict(os.environ, {API_KEY_ENV: ""}))
        for name in ("_LIBRARY_INDEX", "_LIBRARY_INDEX_STAMP", "_LIBRARY_INDEX_KEY"):
            contexts.enter_context(patch.object(monitor_api, name, None))
        contexts.enter_context(patch.object(monitor_api, "_LIBRARY_USER_CACHE", {}))
        contexts.enter_context(patch.object(monitor_api.monitor_service, "_storage", self.storage))

    def _get(self, **params):
        response = self.client.get("/api/monitor/library", params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["data"]

    def test_lists_items_with_user_fields(self):
        data = self._get()
        # 同一发布时间按 aweme_id 倒序
        self.assertEqual([item["aweme_id"] for item in data["items"]], ["7003", "7001"])
        by_id = {item["aweme_id"]: item for item in data["items"]}
        self.assertEqual(by_id["7001"]["nickname"], "Alice")
        self.assertEqual(by_id["7001"]["user_id"], "u1")
        self.assertEqual(by_id["7001"]["avatar_url"], "/api/monitor/users/u1/avatar")
        self.assertIn("cover_url", by_id["7001"])
        self.assertEqual(by_id["7001"]["content_filter"]["appearance"]["borderline"]["score"], 4)
        self.assertEqual(data["eligible_total"], 2)
        self.assertEqual(data["users_total"], 2)
        self.assertNotIn("entries", data)

    def test_invalid_param_returns_400(self):
        response = self.client.get("/api/monitor/library", params={"sort": "hot"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("排序", response.json()["detail"])

    def test_index_cached_and_invalidated_on_record_change(self):
        with patch.object(monitor_api, "build_library_index", wraps=monitor_api.build_library_index) as build:
            self.assertEqual(self._get()["total"], 2)
            self.assertEqual(self._get()["total"], 2)
            self.assertEqual(build.call_count, 1)
            self.storage.records.put("u1", [work("7001", score=4), work("7002", score=3)])
            self.assertEqual(self._get()["total"], 3)
            self.assertEqual(build.call_count, 2)

    def test_state_rewrite_without_user_change_keeps_index(self):
        with patch.object(monitor_api, "build_library_index", wraps=monitor_api.build_library_index) as build:
            self._get()
            self.storage.update_state(lambda state: state["monitoring"].update({"is_running": True}))
            self._get()
            self.assertEqual(build.call_count, 1)
            self.storage.update_state(lambda state: state["users"][1].update({"nickname": "Bobby"}))
            data = self._get(user_ids="u2")
            self.assertEqual(build.call_count, 2)
            self.assertEqual(data["items"][0]["nickname"], "Bobby")

    def test_dashboard_page_served_without_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = Path(tmp) / "library.html"
            page.write_text("<html>作品库</html>", encoding="utf-8")
            with patch.object(monitor_api, "LIBRARY_DASHBOARD_PATH", page):
                response = self.client.get("/api/monitor/library/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertIn("作品库", response.text)
        self.assertIn("no-store", response.headers["cache-control"])


if __name__ == "__main__":
    unittest.main()
