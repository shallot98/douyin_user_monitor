from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import patch

import httpx
from fastapi import HTTPException

from douyin_user_monitor.api import media_proxy
from douyin_user_monitor.api import monitor as monitor_api
from douyin_user_monitor.monitor.openlist_client import ArchiveUnavailableError, OpenListClient


class ArchiveFailoverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.archive = OpenListClient(replace(
            monitor_api.SETTINGS.archive, enabled=True, openlist_token="test-token",
            primary_base="/primary", backup_base="/backup", sanitize_primary_dirs=False,
        ))
        self.metadata_paths = []
        self.media_requests = []
        self.clients = []
        self.status = 403
        self.expire_once = False
        self.metadata_missing = False
        self.real_client = httpx.AsyncClient

    async def _metadata(self, path, **kwargs):
        self.metadata_paths.append(path)
        if self.metadata_missing:
            raise ArchiveUnavailableError("object not found")
        host = "primary.test" if path.startswith("/primary/") else "backup.test"
        return {"raw_url": f"https://{host}/video.mp4?v={len(self.metadata_paths)}"}

    def _response(self, request):
        self.media_requests.append(request)
        if request.url.host == "primary.test" and not (self.expire_once and len(self.media_requests) > 1):
            return httpx.Response(self.status, headers={"Content-Range": "bytes */12"}, content=b"unavailable")
        return httpx.Response(206, headers={
            "Content-Type": "video/mp4", "Content-Range": "bytes 2-7/12", "Content-Length": "6",
        }, content=b"234567")

    def _client(self, **kwargs):
        client = self.real_client(transport=httpx.MockTransport(self._response), **kwargs)
        self.clients.append(client)
        return client

    async def _open(self):
        with (
            patch.object(monitor_api, "OPENLIST_CLIENT", self.archive),
            patch.object(self.archive, "_fs_get", side_effect=self._metadata),
            patch.object(media_proxy.httpx, "AsyncClient", side_effect=self._client),
        ):
            status, headers, body = await monitor_api._open_archived_cloud_stream(
                "user/video.mp4", "sec", "bytes=2-7", verified_paths=(),
            )
            content = b"".join([chunk async for chunk in body])
        self.assertTrue(all(client.is_closed for client in self.clients))
        return status, headers, content

    async def test_primary_media_errors_try_backup_and_preserve_range(self):
        for status in (403, 404):
            with self.subTest(status=status):
                self.status = status
                self.metadata_paths.clear()
                self.media_requests.clear()
                self.archive.invalidate("user/video.mp4")
                result, headers, content = await self._open()
                self.assertEqual((result, content), (206, b"234567"))
                self.assertEqual(headers["content-range"], "bytes 2-7/12")
                self.assertEqual(self.metadata_paths, [
                    "/primary/user/video.mp4", "/primary/user/video.mp4", "/backup/user/video.mp4",
                ])
                self.assertTrue(all(request.headers["range"] == "bytes=2-7" for request in self.media_requests))

    async def test_expired_primary_url_refreshes_without_opening_backup(self):
        self.expire_once = True
        result, _, content = await self._open()
        self.assertEqual((result, content), (206, b"234567"))
        self.assertEqual(self.metadata_paths, ["/primary/user/video.mp4"] * 2)
        self.assertNotEqual(self.media_requests[0].url, self.media_requests[1].url)

    async def test_missing_all_metadata_is_404_without_media_requests(self):
        self.metadata_missing = True
        with self.assertRaises(HTTPException) as raised:
            await self._open()
        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(self.media_requests, [])

    async def test_all_media_failures_are_502_and_close_clients(self):
        with patch.object(self, "_response", return_value=httpx.Response(403)):
            with self.assertRaises(HTTPException) as raised:
                await self._open()
        self.assertEqual(raised.exception.status_code, 502)
        self.assertIn("/backup/user/video.mp4", self.metadata_paths)
        self.assertTrue(all(client.is_closed for client in self.clients))

    async def test_body_failure_does_not_splice_backup_bytes(self):
        class BrokenBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"first chunk"
                raise httpx.ReadError("connection lost during body")

        with patch.object(self, "_response", return_value=httpx.Response(200, stream=BrokenBody())):
            with self.assertRaisesRegex(httpx.ReadError, "connection lost"):
                await self._open()
        self.assertEqual(self.metadata_paths, ["/primary/user/video.mp4"])
        self.assertTrue(all(client.is_closed for client in self.clients))

    async def test_closing_body_early_releases_the_upstream_client(self):
        self.expire_once = True
        with (
            patch.object(monitor_api, "OPENLIST_CLIENT", self.archive),
            patch.object(self.archive, "_fs_get", side_effect=self._metadata),
            patch.object(media_proxy.httpx, "AsyncClient", side_effect=self._client),
        ):
            _, _, body = await monitor_api._open_archived_cloud_stream(
                "user/video.mp4", "sec", "bytes=2-7", verified_paths=(),
            )
            self.assertEqual(await anext(body), b"234567")
            await body.aclose()
        self.assertTrue(all(client.is_closed for client in self.clients))

    async def test_unsatisfiable_range_preserves_416_without_trying_backup(self):
        self.status = 416
        result, headers, _ = await self._open()
        self.assertEqual(result, 416)
        self.assertEqual(headers["content-range"], "bytes */12")
        self.assertEqual(self.metadata_paths, ["/primary/user/video.mp4"])


class DirectoryOnlyArchivePathsTests(unittest.IsolatedAsyncioTestCase):
    async def test_directory_only_mover_paths_keep_original_filename(self):
        for clean_primary in (True, False):
            with self.subTest(sanitize_primary_dirs=clean_primary):
                archive = OpenListClient(replace(
                    monitor_api.SETTINGS.archive, enabled=True, openlist_token="test-token",
                    primary_base="/primary", backup_base="/backup", sanitize_primary_dirs=clean_primary,
                ))
                existing = "/primary/用户/子目录/123_庆祝🎉.mp4"

                async def metadata(path, **kwargs):
                    if path != existing:
                        raise ArchiveUnavailableError("object not found")
                    return {"raw_url": "https://primary.test/original-name.mp4"}

                with patch.object(archive, "_fs_get", side_effect=metadata):
                    resolved = await archive.resolve_stream_url("用户🐱/子目录🍬/123_庆祝🎉.mp4")
                self.assertEqual(resolved, "https://primary.test/original-name.mp4")
                candidates = archive.remote_paths_for("用户🐱/子目录🍬/123_庆祝🎉.mp4")
                self.assertEqual(len(candidates), len(set(candidates)))
                self.assertIn(existing, candidates)
