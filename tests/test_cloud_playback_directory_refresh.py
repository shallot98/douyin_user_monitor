"""Metadata refresh must expose new directories before publishing their files."""

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, call, patch

import httpx

from douyin_user_monitor.monitor.openlist_client import ArchiveUnavailableError, OpenListClient
from douyin_user_monitor.settings import ArchiveSettings
from scripts.cloud_playback.directory_refresh import PublicationDirectoryRefresher, publication_directories

BASE = "/d2/vps/videos"
PUBLICATION = {"original_file": "_recovered_originals/user/1.source.mp4",
               "playback_file": "_web_playback/user/1.h264.mp4", "cover_file": "_web_playback/user/1.cover.jpg"}
DIRECTORIES = [BASE, BASE + "/_recovered_originals", BASE + "/_web_playback",
               BASE + "/_recovered_originals/user", BASE + "/_web_playback/user"]


class PublicationRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_refreshes_ancestors_before_new_user_directories(self):
        refresh = AsyncMock()
        refresher = PublicationDirectoryRefresher(primary_base=BASE, refresh_directory=refresh)
        await refresher([PUBLICATION])
        self.assertEqual(refresh.await_args_list, [call(path) for path in DIRECTORIES])

    async def test_shared_ancestors_are_refreshed_once_per_receipt_batch(self):
        refresh = AsyncMock()
        refresher = PublicationDirectoryRefresher(primary_base=BASE, refresh_directory=refresh)
        await refresher([PUBLICATION])
        await refresher([PUBLICATION, {"cover_file": "_web_playback/another/2.cover.jpg"}])
        self.assertEqual(refresh.await_args_list, [call(path) for path in [*DIRECTORIES, BASE + "/_web_playback/another"]])
        next_batch = PublicationDirectoryRefresher(primary_base=BASE, refresh_directory=refresh)
        await next_batch([PUBLICATION])
        self.assertEqual(refresh.await_args_list[-len(DIRECTORIES):], [call(path) for path in DIRECTORIES])

    async def test_refresh_failure_stops_before_dependent_directories(self):
        refresh = AsyncMock(side_effect=[None, ArchiveUnavailableError("refresh failed")])
        refresher = PublicationDirectoryRefresher(primary_base=BASE, refresh_directory=refresh)
        with self.assertRaisesRegex(ArchiveUnavailableError, "refresh failed"):
            await refresher([PUBLICATION])
        self.assertEqual(refresh.await_args_list, [call(path) for path in DIRECTORIES[:2]])
        refresh.side_effect = None
        await refresher([PUBLICATION])
        self.assertEqual(refresh.await_args_list[2:], [call(path) for path in DIRECTORIES[1:]])

    def test_empty_publication_does_not_refresh_unrelated_directories(self):
        self.assertEqual(publication_directories([{"playback_file": None}], BASE), [])


class OpenListRefreshTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        config = ArchiveSettings(enabled=True, openlist_base_url="http://openlist.test", openlist_db_path=Path("unused"),
                                 openlist_token="unit-test-token", primary_base=BASE, backup_base="/backup",
                                 timeout_seconds=10, sanitize_primary_dirs=False)
        self.archive = OpenListClient(config)
        patcher = patch("douyin_user_monitor.monitor.openlist_client.httpx.AsyncClient")
        factory = patcher.start()
        self.addCleanup(patcher.stop)
        self.http = AsyncMock()
        self.http.is_closed = False
        factory.return_value = self.http
        self.addAsyncCleanup(self.archive.close)

    async def test_close_releases_the_persistent_client(self):
        self.http.post.return_value = httpx.Response(200, json={"code": 200})
        await self.archive.refresh_directory(BASE)
        await self.archive.close()
        self.http.aclose.assert_awaited_once()
        self.assertIsNone(self.archive._http_client)

    async def test_uses_forced_metadata_listing_with_bounded_response(self):
        self.http.post.return_value = httpx.Response(200, json={"code": 200, "data": {"total": 1000}})
        await self.archive.refresh_directory(BASE)
        self.http.post.assert_awaited_once_with(
            "http://openlist.test/api/fs/list", headers={"Authorization": "unit-test-token", "Content-Type": "application/json"},
            json={"path": BASE, "password": "", "page": 1, "per_page": 1, "refresh": True},
        )

    async def test_refresh_http_provider_and_protocol_errors_are_explicit(self):
        responses = [httpx.Response(503), httpx.Response(200, json={"code": 500, "message": "private-provider-text"}),
                     httpx.Response(200, json=[]), httpx.Response(200, content=b"invalid json")]
        for response in responses:
            with self.subTest(status=response.status_code):
                self.http.post.return_value = response
                with self.assertRaises(ArchiveUnavailableError) as error:
                    await self.archive.refresh_directory(BASE)
                self.assertNotIn("private-provider-text", str(error.exception))

    async def test_network_failure_is_not_reported_as_a_refreshed_directory(self):
        self.http.post.side_effect = httpx.ReadTimeout("private-request-details")
        with self.assertRaisesRegex(ArchiveUnavailableError, "ReadTimeout") as error:
            await self.archive.refresh_directory(BASE)
        self.assertNotIn("private-request-details", str(error.exception))


if __name__ == "__main__":
    unittest.main()
