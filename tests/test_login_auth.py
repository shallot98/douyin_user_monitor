import os
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from douyin_user_monitor import auth
from douyin_user_monitor import main as main_module
from douyin_user_monitor.main import app

LOGIN_ENV = {
    auth.LOGIN_USERNAME_ENV: "tester",
    auth.LOGIN_PASSWORD_ENV: "pw-123",
    auth.SESSION_SECRET_ENV: "session-secret",
    auth.API_KEY_ENV: "script-key",
}
HTML = {"Accept": "text/html,application/xhtml+xml"}


class LoginAuthTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, LOGIN_ENV)
        env.start()
        self.addCleanup(env.stop)
        limiter = patch.object(main_module, "LOGIN_LIMITER", auth.LoginRateLimiter())
        limiter.start()
        self.addCleanup(limiter.stop)
        self.client = TestClient(app, base_url="https://testserver")

    def _login(self, username="tester", password="pw-123", remember=True, headers=None):
        return self.client.post(
            "/auth/login",
            json={"username": username, "password": password, "remember": remember},
            headers=headers,
        )

    def test_page_redirects_to_login_when_not_authenticated(self):
        response = self.client.get("/api/monitor/dashboard?tab=x", headers=HTML, follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/login?next=%2Fapi%2Fmonitor%2Fdashboard%3Ftab%3Dx")

    def test_api_returns_401_when_not_authenticated(self):
        self.assertEqual(self.client.get("/api/monitor/statistics").status_code, 401)

    def test_login_sets_persistent_cookie_and_grants_access(self):
        response = self._login()

        self.assertEqual(response.status_code, 200)
        cookie = response.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Max-Age=2592000", cookie)
        self.assertIn("SameSite=lax", cookie)
        self.assertIn("Secure", cookie)
        self.assertEqual(self.client.get("/api/monitor/dashboard", headers=HTML).status_code, 200)

    def test_login_without_remember_sets_session_cookie(self):
        response = self._login(remember=False)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Max-Age", response.headers["set-cookie"])
        expires = int(self.client.cookies[auth.SESSION_COOKIE].split(".")[0])
        self.assertLessEqual(expires - time.time(), auth.SESSION_ONLY_SECONDS)

    def test_wrong_password_rejected_and_rate_limited(self):
        for _ in range(auth.LOGIN_FAIL_LIMIT):
            self.assertEqual(self._login(password="bad").status_code, 401)

        blocked = self._login()
        self.assertEqual(blocked.status_code, 429)
        self.assertIn("Retry-After", blocked.headers)

    def test_rate_limit_is_per_client_ip(self):
        for _ in range(auth.LOGIN_FAIL_LIMIT):
            self._login(password="bad", headers={"CF-Connecting-IP": "1.1.1.1"})

        self.assertEqual(self._login(headers={"CF-Connecting-IP": "2.2.2.2"}).status_code, 200)

    def test_tampered_and_expired_cookies_rejected(self):
        future = int(time.time()) + 60
        valid = auth.make_session_token(future)
        signature = valid.split(".")[1]
        for token in (f"{future + 1}.{signature}", "garbage", auth.make_session_token(int(time.time()) - 1)):
            self.client.cookies.set(auth.SESSION_COOKIE, token)
            self.assertEqual(self.client.get("/api/monitor/statistics").status_code, 401, token)

    def test_password_change_invalidates_existing_sessions(self):
        self._login()
        with patch.dict(os.environ, {auth.LOGIN_PASSWORD_ENV: "new-pw"}):
            self.assertEqual(self.client.get("/api/monitor/statistics").status_code, 401)

    def test_api_key_still_works_without_login(self):
        response = self.client.get("/api/monitor/dashboard", headers={"X-API-Key": "script-key"})

        self.assertEqual(response.status_code, 200)

    def test_logout_clears_session(self):
        self._login()
        self.client.post("/auth/logout")

        self.assertEqual(self.client.get("/api/monitor/statistics").status_code, 401)

    def test_login_page_only_redirects_to_same_site(self):
        self.assertEqual(self.client.get("/login", follow_redirects=False).status_code, 200)
        self._login()
        for raw, expected in (("/api/monitor/dashboard", "/api/monitor/dashboard"),
                              ("//evil.example", "/"), ("https://evil.example", "/"), ("/\\evil", "/")):
            response = self.client.get("/login", params={"next": raw}, follow_redirects=False)
            self.assertEqual(response.headers["location"], expected, raw)

    def test_docs_and_schema_require_auth(self):
        self.assertEqual(self.client.get("/openapi.json").status_code, 401)
        self._login()
        self.assertEqual(self.client.get("/openapi.json").status_code, 200)
        self.assertEqual(self.client.get("/docs").status_code, 200)

    def test_status_reports_login_state(self):
        before = self.client.get("/auth/status").json()["data"]
        self._login()
        after = self.client.get("/auth/status").json()["data"]

        self.assertEqual(before, {"login_enabled": True, "authenticated": False})
        self.assertEqual(after, {"login_enabled": True, "authenticated": True})

    def test_missing_session_secret_fails_loudly(self):
        with patch.dict(os.environ, {auth.SESSION_SECRET_ENV: ""}):
            self.assertEqual(self._login().status_code, 500)


class OpenModeTests(unittest.TestCase):
    def test_everything_open_when_nothing_configured(self):
        cleared = {name: "" for name in LOGIN_ENV}
        with patch.dict(os.environ, cleared):
            client = TestClient(app)
            self.assertEqual(client.get("/api/monitor/dashboard", headers=HTML).status_code, 200)
            self.assertEqual(client.get("/openapi.json").status_code, 200)
            self.assertEqual(client.get("/login", follow_redirects=False).status_code, 302)
            self.assertEqual(client.post("/auth/login", json={"username": "a", "password": "b"}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
