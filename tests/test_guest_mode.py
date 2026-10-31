"""游客模式：只能随机刷作品库里的视频，其余接口一律拒绝。"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from record_helpers import seed_state
from test_library import work

from douyin_user_monitor import auth
from douyin_user_monitor.api import monitor as monitor_api
from douyin_user_monitor.main import app
from douyin_user_monitor.monitor.storage import MonitorStorage

LOGIN_ENV = {
    auth.LOGIN_USERNAME_ENV: "tester",
    auth.LOGIN_PASSWORD_ENV: "pw-123",
    auth.SESSION_SECRET_ENV: "session-secret",
    auth.API_KEY_ENV: "script-key",
    auth.GUEST_ENABLED_ENV: "1",
}
JSON = {"Accept": "application/json"}
HTML = {"Accept": "text/html,application/xhtml+xml"}


class GuestModeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.storage = MonitorStorage(Path(temporary.name) / "state.json")
        self.addCleanup(self.storage.records.close)
        seed_state(self.storage, {"users": [
            {"id": "u1", "nickname": "Alice", "download_records": [
                work("7001", score=4, desc="secret desc"), work("7002", score=0),
            ]},
            {"id": "u2", "nickname": "Bob", "download_records": [
                work("7003", media_type="image"), work("7004", score=2, publish="2026-07-01T12:00:00+08:00"),
            ]},
        ], "monitoring": {}})
        contexts = ExitStack()
        self.addCleanup(contexts.close)
        contexts.enter_context(patch.dict(os.environ, LOGIN_ENV))
        for name in ("_LIBRARY_INDEX", "_LIBRARY_INDEX_STAMP", "_LIBRARY_INDEX_KEY", "_GUEST_WORKS"):
            contexts.enter_context(patch.object(monitor_api, name, None))
        contexts.enter_context(patch.object(monitor_api, "_LIBRARY_USER_CACHE", {}))
        contexts.enter_context(patch.object(monitor_api.monitor_service, "_storage", self.storage))
        self.client = TestClient(app, base_url="https://testserver")

    def _enter_as_guest(self):
        response = self.client.post("/auth/guest")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(auth.GUEST_COOKIE, self.client.cookies)
        return response

    # ---- 入口 ----

    def test_guest_entry_disabled_by_default(self):
        with patch.dict(os.environ, {auth.GUEST_ENABLED_ENV: ""}):
            self.assertEqual(self.client.post("/auth/guest").status_code, 404)
            self.assertFalse(self.client.get("/auth/status").json()["data"]["guest_enabled"])

    def test_guest_cookie_and_status(self):
        response = self._enter_as_guest()
        self.assertEqual(response.json()["data"]["next"], "/api/monitor/swipe/dashboard")
        cookie = response.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        status = self.client.get("/auth/status").json()["data"]
        self.assertEqual(status["role"], "guest")
        self.assertFalse(status["authenticated"])

    def test_guest_token_is_not_an_admin_session(self):
        self._enter_as_guest()
        token = self.client.cookies[auth.GUEST_COOKIE]
        self.client.cookies.clear()
        self.client.cookies.set(auth.SESSION_COOKIE, token)
        self.assertEqual(self.client.get("/api/monitor/statistics").status_code, 401)

    def test_guest_sessions_die_when_guest_mode_turned_off(self):
        self._enter_as_guest()
        with patch.dict(os.environ, {auth.GUEST_ENABLED_ENV: "0"}):
            self.assertEqual(self.client.get("/api/monitor/library", headers=JSON).status_code, 401)

    def test_admin_wins_over_guest_cookie(self):
        self._enter_as_guest()
        login = self.client.post("/auth/login", json={"username": "tester", "password": "pw-123"})
        self.assertEqual(login.status_code, 200)
        self.assertEqual(self.client.get("/auth/status").json()["data"]["role"], "admin")
        page = self.client.get("/api/monitor/swipe/dashboard", headers=HTML)
        self.assertNotIn('class="sw-guest"', page.text)

    # ---- 权限：白名单以外全部拒绝 ----

    def test_every_non_guest_route_is_forbidden(self):
        self._enter_as_guest()
        checked = 0
        for route in app.routes:
            if not isinstance(route, APIRoute) or not route.path.startswith("/api/monitor"):
                continue
            path = re.sub(r"\{[^}]+\}", "1", route.path)
            for method in route.methods:
                if (method, route.path) in auth.GUEST_ROUTES:
                    continue
                response = self.client.request(method, path, headers=JSON, json={})
                self.assertEqual(response.status_code, 403, f"{method} {route.path} -> {response.status_code}")
                checked += 1
        # 写操作（加/删/改用户、设置、Cookie 池、启停监控等）都在里面
        self.assertGreater(checked, 30)

    def test_guest_html_pages_redirect_to_login(self):
        self._enter_as_guest()
        response = self.client.get("/api/monitor/dashboard", headers=HTML, follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["location"].startswith("/login?next="))

    def test_docs_forbidden_for_guest(self):
        self._enter_as_guest()
        self.assertIn(self.client.get("/openapi.json").status_code, (401, 403))

    # ---- 允许的部分 ----

    def test_swipe_page_marked_as_guest(self):
        self._enter_as_guest()
        page = self.client.get("/api/monitor/swipe/dashboard", headers=HTML)
        self.assertEqual(page.status_code, 200)
        self.assertIn('<body class="sw-guest">', page.text)

    def test_library_ignores_filters_and_returns_minimal_videos(self):
        self._enter_as_guest()
        response = self.client.get(
            "/api/monitor/library",
            params={"media_type": "image", "score_min": 4, "user_ids": "u1", "sort": "newest", "page_size": 120},
        )
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()["data"]
        self.assertEqual(sorted(item["aweme_id"] for item in data["items"]), ["7001", "7004"])
        self.assertIsNotNone(data["seed"], "游客永远是随机排序")
        self.assertEqual(data["page_size"], monitor_api.GUEST_PAGE_SIZE)
        self.assertNotIn("facets", data)
        item = data["items"][0]
        self.assertEqual(set(item), set(monitor_api.GUEST_ITEM_FIELDS))
        self.assertNotIn("secret desc", response.text)
        self.assertNotIn("Alice", response.text)

    def test_playback_urls_only_for_library_videos(self):
        self._enter_as_guest()
        url = "/api/monitor/users/u1/archive/playback-urls"
        self.assertEqual(self.client.post(url, json={"aweme_ids": ["7002"]}).status_code, 404)
        self.assertEqual(
            self.client.post("/api/monitor/users/u2/archive/playback-urls", json={"aweme_ids": ["7003"]}).status_code,
            404,
        )
        with patch.object(monitor_api, "_direct_work_urls", AsyncMock(return_value=("https://cdn.test/v", None))):
            response = self.client.post(url, json={"aweme_ids": ["7001"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"]["urls"], {"7001": "https://cdn.test/v"})

    def test_media_limited_to_play_and_cover_files(self):
        self._enter_as_guest()
        self.assertEqual(self.client.get("/api/monitor/users/u1/archive/7001/media/5").status_code, 404)
        self.assertEqual(self.client.get("/api/monitor/users/u1/archive/7002/media/0").status_code, 404)
        self.assertEqual(self.client.get("/api/monitor/users/u2/archive/7003/media/0").status_code, 404)
        self.assertEqual(self.client.get("/api/monitor/users/u1/archive/7002/cover").status_code, 404)


if __name__ == "__main__":
    unittest.main()
