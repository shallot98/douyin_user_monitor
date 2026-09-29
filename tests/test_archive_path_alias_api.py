from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, call, patch

from fastapi.testclient import TestClient

from douyin_user_monitor.api import monitor as monitor_api
from douyin_user_monitor.main import app
from douyin_user_monitor.monitor.archive_path_aliases import ArchivePathAliases

ORIGINAL_PATH = "八月/新文件名.mp4"
VERIFIED_PATH = "/d2/vps/抖音视频/sec_sec-1/旧文件名  空格.mp4"
DOWNLOADED_AT = "2026-04-10T02:06:08+08:00"
MEDIA_ROUTE = "/api/monitor/users/alias-user/archive/7001/media/0"


def alias_user():
    return {
        "id": "alias-user",
        "sec_user_id": "sec-1",
        "download_records": [{"aweme_id": "7001", "downloaded_at": DOWNLOADED_AT, "files": [ORIGINAL_PATH]}],
    }


def aliases():
    return ArchivePathAliases.from_document({
        "schema_version": 1,
        "entries": [{
            "user_id": "alias-user",
            "aweme_id": "7001",
            "original_relative_path": ORIGINAL_PATH,
            "verified_remote_path": VERIFIED_PATH,
            "source_revision": {"sec_user_id": "sec-1", "downloaded_at": DOWNLOADED_AT},
        }],
    })


class ArchiveAliasApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    @staticmethod
    async def _body():
        yield b"BYTES"

    def test_route_resolves_and_invalidates_the_same_exact_verified_path(self):
        upstream = (206, {"Content-Type": "video/mp4", "Content-Range": "bytes 0-4/100"}, self._body())
        opener = AsyncMock(side_effect=[monitor_api.MediaUpstreamError("expired"), upstream])
        with (
            patch.object(monitor_api.monitor_service, "list_users_async", return_value=[alias_user()]),
            patch.object(monitor_api, "ARCHIVE_PATH_ALIASES", aliases()),
            patch.object(monitor_api.OPENLIST_CLIENT, "resolve_candidate_url", new=AsyncMock(return_value="https://d2.example/video")) as resolve,
            patch.object(monitor_api, "open_media_stream", new=opener),
        ):
            response = self.client.get(MEDIA_ROUTE, headers={"Range": "bytes=0-4"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, b"BYTES")
        self.assertEqual(resolve.await_args_list, [
            call(VERIFIED_PATH, refresh=False), call(VERIFIED_PATH, refresh=True),
        ])
        self.assertEqual(opener.await_args.kwargs["headers"]["Range"], "bytes=0-4")

    def test_changed_source_is_a_visible_conflict_without_cloud_reads(self):
        user = alias_user()
        user["download_records"][0]["downloaded_at"] = "2026-09-13T16:00:00+08:00"
        with (
            patch.object(monitor_api.monitor_service, "list_users_async", return_value=[user]),
            patch.object(monitor_api, "ARCHIVE_PATH_ALIASES", aliases()),
            patch.object(monitor_api.OPENLIST_CLIENT, "resolve_candidate_url", new=AsyncMock()) as resolve,
        ):
            response = self.client.get(MEDIA_ROUTE)
        self.assertEqual(response.status_code, 409)
        self.assertIn("download_revision_changed", response.text)
        resolve.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
