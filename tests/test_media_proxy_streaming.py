from __future__ import annotations

import asyncio
import gzip
import unittest
from unittest.mock import patch

import httpx

from douyin_user_monitor.api import media_proxy

FIRST_CHUNK_TIMEOUT_SECONDS = 1
UPSTREAM_TIMEOUT_SECONDS = 5


class GatedStream(httpx.AsyncByteStream):
    def __init__(self, first: bytes, remaining: bytes):
        self.first = first
        self.remaining = remaining
        self.release = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        yield self.first
        await self.release.wait()
        yield self.remaining

    async def aclose(self):
        self.closed = True


class MediaProxyStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_small_chunk_is_delivered_before_upstream_continues(self):
        stream = GatedStream(b"MP4-HEADER", b"VIDEO-FRAMES")
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(
                206,
                headers={"Content-Range": "bytes 0-21/22", "Content-Length": "22"},
                stream=stream,
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with patch.object(media_proxy.httpx, "AsyncClient", return_value=client):
            status, headers, body = await media_proxy.open_media_stream(
                "https://media.test/video.mp4",
                headers={"Range": "bytes=0-21"},
                timeout_seconds=UPSTREAM_TIMEOUT_SECONDS,
            )
            try:
                first = await asyncio.wait_for(anext(body), FIRST_CHUNK_TIMEOUT_SECONDS)
                self.assertEqual(first, b"MP4-HEADER")
                self.assertFalse(stream.release.is_set())
                self.assertFalse(stream.closed)
            finally:
                stream.release.set()
            rest = b"".join([chunk async for chunk in body])
        self.assertEqual(rest, b"VIDEO-FRAMES")
        self.assertEqual(status, 206)
        self.assertEqual(headers["content-range"], "bytes 0-21/22")
        self.assertEqual(requests[0].headers["range"], "bytes=0-21")
        self.assertEqual(requests[0].headers["accept-encoding"], "identity")
        self.assertTrue(stream.closed)
        self.assertTrue(client.is_closed)

    async def test_gzip_remains_decoded_and_original_length_is_not_forwarded(self):
        content = b"DECODED VIDEO CONTENT" * 20
        encoded = gzip.compress(content)
        stream = GatedStream(encoded[:10], encoded[10:])
        stream.release.set()
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
            200,
            headers={"Content-Encoding": "gzip", "Content-Length": str(len(encoded))},
            stream=stream,
        )))
        with patch.object(media_proxy.httpx, "AsyncClient", return_value=client):
            _, headers, body = await media_proxy.open_media_stream(
                "https://media.test/video.mp4", headers={}, timeout_seconds=UPSTREAM_TIMEOUT_SECONDS
            )
            actual = b"".join([chunk async for chunk in body])
        self.assertEqual(actual, content)
        self.assertNotIn("Content-Length", media_proxy.build_response_headers(headers))
        self.assertNotIn("Content-Encoding", media_proxy.build_response_headers(headers))
        self.assertTrue(client.is_closed)

    async def test_closing_body_releases_upstream_without_reading_remaining_video(self):
        stream = GatedStream(b"HEADER", b"UNREQUESTED VIDEO")
        client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=stream)
        ))
        with patch.object(media_proxy.httpx, "AsyncClient", return_value=client):
            _, _, body = await media_proxy.open_media_stream(
                "https://media.test/video.mp4", headers={}, timeout_seconds=UPSTREAM_TIMEOUT_SECONDS
            )
            self.assertEqual(await anext(body), b"HEADER")
            await body.aclose()
        self.assertFalse(stream.release.is_set())
        self.assertTrue(stream.closed)
        self.assertTrue(client.is_closed)


if __name__ == "__main__":
    unittest.main()
