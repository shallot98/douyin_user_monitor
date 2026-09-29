from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from archive_http_server import ArchiveMediaServer
from fastapi import FastAPI

from douyin_user_monitor.api import monitor as monitor_api
from douyin_user_monitor.monitor.openlist_client import ArchiveUnavailableError, OpenListClient
from douyin_user_monitor.monitor.storage import MonitorStorage, build_default_state
from record_helpers import write_state_file

READ_WAIT_SECONDS = 2
USER_PATH = "/api/monitor/users/user-1"
ORIGINAL_BYTES = b"ORIGINAL-HIGH-QUALITY-VIDEO"
PLAYBACK_BYTES = b"SMALL-PLAYBACK-VIDEO"


def archive_user() -> dict:
    return {
        "id": "user-1",
        "sec_user_id": "sec-1",
        "nickname": "user",
        "avatar_url": "https://example.test/avatar.jpg",
        "download_records": [{
            "aweme_id": "7001",
            "media_type": "video",
            "files": ["cover.jpg", "play.mp4", "original.mp4"],
            "cover_file": "cover.jpg",
            "fast_play_file": "play.mp4",
            "video_file": "original.mp4",
        }],
    }


class ArchiveResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "state.json"
        state = build_default_state()
        state["users"] = [archive_user()]
        write_state_file(self.path, state)
        self.server = ArchiveMediaServer({
            "cover.jpg": b"CLOUD-COVER", "play.mp4": PLAYBACK_BYTES, "original.mp4": ORIGINAL_BYTES,
        })
        self.addCleanup(self.server.close)
        self.metadata_paths = []
        (self.root / "cover.jpg").write_bytes(b"LOCAL-COVER")
        (self.root / "play.mp4").write_bytes(b"LOCAL-PLAYBACK")
        (self.root / "original.mp4").write_bytes(b"LOCAL-ORIGINAL")
        self.settings = replace(
            monitor_api.SETTINGS,
            monitor=replace(monitor_api.SETTINGS.monitor, download_root=self.root),
        )
        self.archive = OpenListClient(replace(self.settings.archive, enabled=True, openlist_token="test-token"))
        for patcher in (
            patch.object(monitor_api, "OPENLIST_CLIENT", self.archive),
            patch.object(self.archive, "_fs_get", side_effect=self._metadata),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.app = FastAPI()
        self.app.include_router(monitor_api.router, prefix="/api/monitor")

        @self.app.get("/probe")
        async def probe():
            return {"responsive": True}

    async def _metadata(self, remote_path, **kwargs):
        self.metadata_paths.append(remote_path)
        name = remote_path.rsplit("/", 1)[-1]
        if name not in self.server.files:
            raise ArchiveUnavailableError("playback not archived")
        return {"raw_url": self.server.url(name)}

    def _client(self):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://testserver"
        )

    async def _assert_route_does_not_block(self, route: str):
        storage = MonitorStorage(self.path)
        entered, release = threading.Event(), threading.Event()
        read = storage._read_state_snapshot

        def blocked():
            entered.set()
            if not release.wait(READ_WAIT_SECONDS):
                raise TimeoutError("test did not release the read")
            return read()

        with (
            patch.object(monitor_api.monitor_service, "_storage", storage),
            patch.object(monitor_api, "SETTINGS", self.settings),
            patch.object(storage, "_read_state_snapshot", side_effect=blocked),
            patch.object(monitor_api, "_download_avatar", new=AsyncMock(return_value=httpx.Response(200, content=b"AVATAR"))),
        ):
            async with self._client() as client:
                request = asyncio.create_task(client.get(USER_PATH + route))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, READ_WAIT_SECONDS))
                    response = await asyncio.wait_for(client.get("/probe"), READ_WAIT_SECONDS)
                    self.assertEqual(response.json(), {"responsive": True})
                    self.assertFalse(request.done())
                finally:
                    release.set()
                    completed = await request
                self.assertEqual(completed.status_code, 200)

    async def test_archive_cover_video_and_avatar_allow_other_requests_during_cold_read(self):
        for route in ("/archive", "/archive/7001/media/0", "/archive/7001/media/1", "/avatar"):
            with self.subTest(route=route):
                await self._assert_route_does_not_block(route)

    async def test_selected_playback_and_original_indices_return_distinct_exact_ranges(self):
        with (
            patch.object(monitor_api.monitor_service, "_storage", MonitorStorage(self.path)),
            patch.object(monitor_api, "SETTINGS", self.settings),
        ):
            async with self._client() as client:
                page = await client.get(USER_PATH + "/archive")
                item = page.json()["data"]["items"][0]
                self.assertEqual((item["cover_index"], item["play_index"], item["archive_index"]), (0, 1, 2))
                self.assertEqual(self.server.requests, [])  # Prefetch resolves metadata, never media bytes.
                self.assertTrue(any(path.endswith("/play.mp4") for path in self.metadata_paths))
                for index, content in ((item["play_index"], PLAYBACK_BYTES), (item["archive_index"], ORIGINAL_BYTES)):
                    response = await client.get(
                        USER_PATH + f"/archive/7001/media/{index}", headers={"Range": "bytes=2-7"}
                    )
                    self.assertEqual(response.status_code, 206)
                    self.assertEqual(response.content, content[2:8])
                    self.assertEqual(response.headers["content-range"], f"bytes 2-7/{len(content)}")
        self.assertEqual(self.server.requests, [("play.mp4", "bytes=2-7"), ("original.mp4", "bytes=2-7")])
        self.assertEqual((self.root / "original.mp4").read_bytes(), b"LOCAL-ORIGINAL")

    async def test_missing_playback_does_not_request_original_instead(self):
        del self.server.files["play.mp4"]
        with (
            patch.object(monitor_api.monitor_service, "_storage", MonitorStorage(self.path)),
            patch.object(monitor_api, "SETTINGS", self.settings),
        ):
            async with self._client() as client:
                response = await client.get(USER_PATH + "/archive/7001/media/1")
        self.assertEqual(response.status_code, 404)
        self.assertTrue(self.metadata_paths)
        self.assertTrue(all(path.endswith("/play.mp4") for path in self.metadata_paths))
        self.assertEqual(self.server.requests, [])
        self.assertEqual((self.root / "original.mp4").read_bytes(), b"LOCAL-ORIGINAL")


if __name__ == "__main__":
    unittest.main()
