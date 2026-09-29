from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from PIL import Image

from douyin_user_monitor.api import monitor as monitor_api
from douyin_user_monitor.main import API_KEY_ENV, app
from douyin_user_monitor.monitor.cover_store import CoverStore

ARCHIVE_PATH = "/api/monitor/users/user-1/archive"
COVER_PATH = ARCHIVE_PATH + "/7001/cover"
TEST_API_KEY = "cover-test-key"


def archive_user() -> dict:
    return {
        "id": "user-1", "nickname": "用户", "sec_user_id": "sec-1",
        "download_records": [{
            "aweme_id": "7001", "media_type": "video", "downloaded_at": "2026-09-14T12:00:00+08:00",
            "files": ["original.mp4", "cover.jpg", "play.mp4"],
            "video_file": "original.mp4", "cover_file": "cover.jpg", "fast_play_file": "play.mp4",
        }, {
            "aweme_id": "7002", "media_type": "image", "files": ["old-image.jpg"],
        }],
    }


class CoverApiTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = CoverStore(self.root / "covers")
        self.source = self.root / "cover.png"
        Image.new("RGB", (120, 80), "red").save(self.source)
        self.user = archive_user()
        self.cover = self.store.publish("user-1", self.user["download_records"][0], self.source)
        self.url = COVER_PATH + f"?v={self.cover.version}"
        self.client = TestClient(app)
        contexts = ExitStack()
        self.addCleanup(contexts.close)
        contexts.enter_context(patch.dict(os.environ, {API_KEY_ENV: ""}))
        contexts.enter_context(patch.object(monitor_api, "COVER_STORE", self.store))
        contexts.enter_context(patch.object(monitor_api.monitor_service, "list_users_async", return_value=[self.user]))
        self.cloud = contexts.enter_context(patch.object(
            monitor_api.OPENLIST_CLIENT, "resolve_stream_url", new=AsyncMock(side_effect=AssertionError("cloud read")),
        ))
        self.addCleanup(self.cloud.assert_not_awaited)

    def _get_archive(self):
        # Cover contracts are independent of the list route's background media prefetch.
        with patch.object(monitor_api, "_prefetch_archive_urls", new=AsyncMock()) as prefetch:
            response = self.client.get(ARCHIVE_PATH)
        prefetch.assert_awaited_once_with(self.user, response.json()["data"]["items"])
        return response

    def test_archive_exposes_versioned_cover_without_changing_legacy_indices_or_records(self):
        before = deepcopy(self.user)
        response = self._get_archive()
        self.assertEqual(response.status_code, 200)
        items = response.json()["data"]["items"]
        self.assertEqual(items[0]["cover_url"], self.url)
        self.assertEqual((items[0]["cover_index"], items[0]["play_index"], items[0]["archive_index"]), (1, 2, 0))
        self.assertIsNone(items[1]["cover_url"])
        self.assertEqual(items[1]["cover_index"], 0)
        self.assertEqual(self.user, before)

    def test_serves_verified_jpeg_with_private_versioned_cache(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, self.cover.path.read_bytes())
        self.assertEqual(response.headers["content-type"], "image/jpeg")
        self.assertEqual(response.headers["etag"], f'"{self.cover.version}"')
        self.assertIn("private", response.headers["cache-control"])
        self.assertIn("immutable", response.headers["cache-control"])
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_unversioned_url_requires_revalidation(self):
        response = self.client.get(COVER_PATH)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "private, no-cache")

    def test_conditional_request_uses_content_etag(self):
        response = self.client.get(self.url, headers={"If-None-Match": f'"old", W/"{self.cover.version}"'})
        self.assertEqual(response.status_code, 304)
        self.assertEqual(response.content, b"")
        self.assertEqual(response.headers["etag"], f'"{self.cover.version}"')
        response = self.client.get(self.url, headers={"If-None-Match": '"other"'})
        self.assertEqual(response.status_code, 200)

    def test_version_change_never_returns_new_bytes_under_old_url(self):
        Image.new("RGB", (100, 80), "blue").save(self.source)
        latest = self.store.publish("user-1", self.user["download_records"][0], self.source)
        self.assertNotEqual(latest.version, self.cover.version)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        response = self.client.get(COVER_PATH, params={"v": latest.version})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, latest.path.read_bytes())

    def test_revision_mismatch_does_not_publish_or_serve_stale_cover(self):
        self.user["download_records"][0]["downloaded_at"] = "2026-09-15T12:00:00+08:00"
        response = self._get_archive()
        self.assertIsNone(response.json()["data"]["items"][0]["cover_url"])
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_unknown_user_work_and_cover_have_no_access_to_another_record(self):
        paths = ("/api/monitor/users/missing/archive/7001/cover", ARCHIVE_PATH + "/missing/cover",
                 ARCHIVE_PATH + "/7002/cover")
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)
        self.user["id"] = "user-2"
        response = self.client.get("/api/monitor/users/user-2/archive/7001/cover", params={"v": self.cover.version})
        self.assertEqual(response.status_code, 404)

    def test_path_and_version_input_cannot_select_arbitrary_files(self):
        response = self.client.get(COVER_PATH, params={"v": "../../source.png"})
        self.assertEqual(response.status_code, 422)
        response = self.client.get(ARCHIVE_PATH + "/%2E%2E%2Fsource.png/cover")
        self.assertEqual(response.status_code, 404)
        response = self.client.get(COVER_PATH, params={"v": "0" * 64})
        self.assertEqual(response.status_code, 404)

    def test_route_uses_existing_authentication_including_conditional_requests(self):
        os.environ[API_KEY_ENV] = TEST_API_KEY
        response = self.client.get(self.url, headers={"If-None-Match": "*"})
        self.assertEqual(response.status_code, 401)
        response = self.client.get(self.url, headers={"X-API-Key": TEST_API_KEY})
        self.assertEqual(response.status_code, 200)
        response = self.client.get(self.url, params={"v": self.cover.version, "api_key": TEST_API_KEY})
        self.assertEqual(response.status_code, 200)

    def test_corrupt_published_object_returns_explicit_error(self):
        self.cover.path.write_bytes(b"x" * self.cover.size_bytes)
        with self.assertLogs("douyin_user_monitor.api.covers", level="ERROR") as captured:
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["detail"], "封面文件校验失败")
        self.assertIn("CoverIntegrityError", "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
