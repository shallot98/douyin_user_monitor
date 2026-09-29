from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from archive_http_server import ArchiveMediaServer
from fastapi.testclient import TestClient

from douyin_user_monitor.api import monitor as monitor_api
from douyin_user_monitor.api.media_proxy import (
    UnsatisfiableRangeError,
    iter_file_range,
    parse_byte_range,
)
from douyin_user_monitor.main import app
from record_helpers import patch_monitor_users

PAYLOAD = bytes(range(256)) * 40  # 10240 bytes


class ParseByteRangeTests(unittest.TestCase):
    def test_no_range_header(self):
        self.assertIsNone(parse_byte_range(None, 1000))
        self.assertIsNone(parse_byte_range("", 1000))
        self.assertIsNone(parse_byte_range("items=0-10", 1000))

    def test_explicit_range(self):
        self.assertEqual(parse_byte_range("bytes=0-1023", 10000), (0, 1023))
        self.assertEqual(parse_byte_range("bytes=500-999", 10000), (500, 999))

    def test_open_ended_range_clamps_to_file_end(self):
        self.assertEqual(parse_byte_range("bytes=9000-", 10000), (9000, 9999))
        self.assertEqual(parse_byte_range("bytes=0-99999", 10000), (0, 9999))

    def test_suffix_range(self):
        self.assertEqual(parse_byte_range("bytes=-500", 10000), (9500, 9999))
        self.assertEqual(parse_byte_range("bytes=-99999", 10000), (0, 9999))

    def test_multi_range_falls_back_to_full_body(self):
        self.assertIsNone(parse_byte_range("bytes=0-10,20-30", 10000))

    def test_malformed_range_falls_back(self):
        self.assertIsNone(parse_byte_range("bytes=abc-def", 10000))
        self.assertIsNone(parse_byte_range("bytes=-", 10000))

    def test_unsatisfiable_ranges_raise(self):
        with self.assertRaises(UnsatisfiableRangeError):
            parse_byte_range("bytes=10000-10005", 10000)
        with self.assertRaises(UnsatisfiableRangeError):
            parse_byte_range("bytes=800-700", 10000)
        with self.assertRaises(UnsatisfiableRangeError):
            parse_byte_range("bytes=-0", 10000)

    def test_empty_file_has_no_range(self):
        self.assertIsNone(parse_byte_range("bytes=0-10", 0))


class IterFileRangeTests(unittest.TestCase):
    def test_yields_exact_inclusive_slice(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.bin"
            path.write_bytes(PAYLOAD)
            chunks = b"".join(iter_file_range(path, 100, 199, chunk_size=7))
            self.assertEqual(chunks, PAYLOAD[100:200])
            self.assertEqual(len(chunks), 100)

    def test_single_byte(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.bin"
            path.write_bytes(PAYLOAD)
            self.assertEqual(b"".join(iter_file_range(path, 5, 5)), PAYLOAD[5:6])


def local_user() -> dict:
    return {
        "id": "user-1",
        "sec_user_id": "sec-1",
        "nickname": "本地用户",
        "enabled": True,
        "downloaded_count": 1,
        "account_status": "normal",
        "download_records": [
            {
                "aweme_id": "7001",
                "media_type": "video",
                "publish_time": "2026-08-29T15:00:36+08:00",
                "files": ["本地用户/a.mp4"],
            }
        ],
    }


class CloudMediaRangeRouteTests(unittest.TestCase):
    """The route must proxy the provider's exact range bytes, not local files."""

    def setUp(self):
        self.client = TestClient(app)
        self.server = ArchiveMediaServer({"本地用户/a.mp4": PAYLOAD})
        self.addCleanup(self.server.close)

    def _get(self, headers=None):
        with (
            patch_monitor_users([local_user()]),
            patch.object(
                monitor_api.OPENLIST_CLIENT, "resolve_candidate_url",
                new=AsyncMock(return_value=self.server.url("本地用户/a.mp4")),
            ) as resolve,
        ):
            response = self.client.get(
                "/api/monitor/users/user-1/archive/7001/media/0",
                headers=headers or {},
            )
        resolve.assert_awaited_once()
        self.assertEqual(self.server.requests, [("本地用户/a.mp4", (headers or {}).get("Range"))])
        return response

    def test_range_request_returns_206_slice(self):
        response = self._get({"Range": "bytes=0-1023"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, PAYLOAD[:1024])
        self.assertEqual(response.headers["content-range"], f"bytes 0-1023/{len(PAYLOAD)}")
        self.assertEqual(response.headers["content-length"], "1024")
        self.assertEqual(response.headers["accept-ranges"], "bytes")

    def test_mid_file_seek(self):
        response = self._get({"Range": "bytes=4096-8191"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, PAYLOAD[4096:8192])
        self.assertEqual(response.headers["content-range"], f"bytes 4096-8191/{len(PAYLOAD)}")

    def test_open_ended_range(self):
        response = self._get({"Range": "bytes=10000-"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, PAYLOAD[10000:])

    def test_no_range_serves_whole_file_and_advertises_ranges(self):
        response = self._get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, PAYLOAD)
        self.assertEqual(response.headers["accept-ranges"], "bytes")

    def test_unsatisfiable_range_returns_416(self):
        response = self._get({"Range": f"bytes={len(PAYLOAD) + 10}-"})
        self.assertEqual(response.status_code, 416)
        self.assertEqual(response.headers["content-range"], f"bytes */{len(PAYLOAD)}")

    def test_guesses_media_type_from_extension(self):
        response = self._get()
        self.assertEqual(response.headers["content-type"], "video/mp4")


if __name__ == "__main__":
    unittest.main()
