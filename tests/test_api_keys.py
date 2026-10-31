"""网页自助管理的 API Key：增删、权限档、使用计数。"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from douyin_user_monitor import auth
from douyin_user_monitor.api import monitor as monitor_api
from douyin_user_monitor.main import app
from douyin_user_monitor.monitor import api_keys
from douyin_user_monitor.monitor.api_keys import SYSTEM_KEY_ID, ApiKeyStore

LOGIN_ENV = {
    auth.LOGIN_USERNAME_ENV: "tester",
    auth.LOGIN_PASSWORD_ENV: "pw-123",
    auth.SESSION_SECRET_ENV: "session-secret",
    auth.API_KEY_ENV: "system-key",
}
VIDEO = {"aweme_id": "7001", "is_image_post": False, "desc": "", "download_url": "https://cdn.test/v.mp4"}


class ApiKeyStoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = ApiKeyStore(Path(tmp.name) / "keys.db")
        self.addCleanup(self.store.close)

    def test_create_match_count_delete(self):
        record, key = self.store.create("插件", "parse")
        self.assertTrue(key.startswith("dk_"))
        self.assertTrue(key.startswith(record["prefix"]))
        self.assertNotIn(key, str(self.store.list()), "明文 Key 不能出现在列表里")
        self.assertEqual(self.store.match(key), (record["id"], "parse"))
        self.assertIsNone(self.store.match(key + "x"))
        self.store.record_use(record["id"], "1.2.3.4")
        self.store.record_use(record["id"], "5.6.7.8")
        saved = self.store.get(record["id"])
        self.assertEqual(saved["use_count"], 2)
        self.assertEqual(saved["last_ip"], "5.6.7.8")
        self.store.delete(record["id"])
        self.assertIsNone(self.store.match(key))

    def test_system_row_cannot_be_deleted_and_validation(self):
        self.assertEqual(self.store.list()[0]["id"], SYSTEM_KEY_ID)
        with self.assertRaises(ValueError):
            self.store.delete(SYSTEM_KEY_ID)
        with self.assertRaises(ValueError):
            self.store.create("", "parse")
        with self.assertRaises(ValueError):
            self.store.create("x", "admin")
        with self.assertRaises(KeyError):
            self.store.delete("missing")

    def test_counts_survive_reopen(self):
        record, key = self.store.create("快捷指令", "full")
        self.store.record_use(record["id"], "ip")
        reopened = ApiKeyStore(self.store._path)  # noqa: SLF001
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.match(key), (record["id"], "full"))
        self.assertEqual(reopened.get(record["id"])["use_count"], 1)


class ApiKeyRouteTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, LOGIN_ENV)
        env.start()
        self.addCleanup(env.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = ApiKeyStore(Path(tmp.name) / "keys.db")
        self.addCleanup(store.close)
        self.store = store
        for target in ((api_keys, "_STORE"), (monitor_api, "API_KEY_STORE")):
            patcher = patch.object(*target, store)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(app, base_url="https://testserver")

    def _admin(self):
        response = self.client.post("/auth/login", json={"username": "tester", "password": "pw-123"})
        self.assertEqual(response.status_code, 200)

    def _create(self, scope="parse", name="插件"):
        response = self.client.post("/api/monitor/api-keys", json={"name": name, "scope": scope})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["data"]

    def test_admin_creates_lists_and_deletes(self):
        self._admin()
        created = self._create()
        self.assertTrue(created["key"].startswith("dk_"))
        items = self.client.get("/api/monitor/api-keys").json()["data"]["items"]
        self.assertEqual([item["id"] for item in items], [SYSTEM_KEY_ID, created["id"]])
        self.assertNotIn(created["key"], str(items))
        self.assertEqual(self.client.delete(f"/api/monitor/api-keys/{created['id']}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/monitor/api-keys/{SYSTEM_KEY_ID}").status_code, 400)
        self.assertEqual(self.client.delete("/api/monitor/api-keys/nope").status_code, 404)

    def test_parse_key_only_reaches_parse_routes_and_is_counted(self):
        self._admin()
        created = self._create("parse")
        self.client.cookies.clear()
        with patch.object(monitor_api.PARSE_SERVICE, "parse", AsyncMock(return_value=VIDEO)):
            ok = self.client.get("/api/monitor/parse/plugin", params={"url": "x", "api_key": created["key"]})
            ok_header = self.client.get("/api/monitor/parse", params={"text": "x"}, headers={"X-API-Key": created["key"]})
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(ok_header.status_code, 200)
        for method, path in (("GET", "/api/monitor/statistics"), ("POST", "/api/monitor/users/add_by_url"),
                             ("GET", "/api/monitor/api-keys"), ("DELETE", f"/api/monitor/users/{created['id']}")):
            response = self.client.request(method, path, headers={"X-API-Key": created["key"]}, json={})
            self.assertEqual(response.status_code, 403, f"{method} {path}")
        # 两次成功 + 四次被拒都算使用
        self.assertEqual(self.store.get(created["id"])["use_count"], 6)

    def test_full_key_is_admin_but_cannot_manage_keys(self):
        self._admin()
        created = self._create("full")
        self.client.cookies.clear()
        headers = {"X-API-Key": created["key"]}
        self.assertEqual(self.client.get("/api/monitor/statistics", headers=headers).status_code, 200)
        self.assertEqual(self.client.get("/api/monitor/api-keys", headers=headers).status_code, 403)
        self.assertEqual(
            self.client.post("/api/monitor/api-keys", headers=headers, json={"name": "x"}).status_code, 403
        )

    def test_system_key_usage_counted_and_can_manage(self):
        headers = {"X-API-Key": "system-key"}
        self.assertEqual(self.client.get("/api/monitor/api-keys", headers=headers).status_code, 200)
        self.client.get("/api/monitor/statistics", headers=headers)
        self.assertEqual(self.store.get(SYSTEM_KEY_ID)["use_count"], 2)

    def test_deleted_key_stops_working(self):
        self._admin()
        created = self._create("full")
        self.client.delete(f"/api/monitor/api-keys/{created['id']}")
        self.client.cookies.clear()
        response = self.client.get("/api/monitor/statistics", headers={"X-API-Key": created["key"]})
        self.assertEqual(response.status_code, 401)

    def test_session_login_not_counted(self):
        self._admin()
        self.client.get("/api/monitor/statistics")
        self.assertEqual(self.store.get(SYSTEM_KEY_ID)["use_count"], 0)


if __name__ == "__main__":
    unittest.main()
