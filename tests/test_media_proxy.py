from __future__ import annotations

import unittest

from douyin_user_monitor.api.media_proxy import (
    build_response_headers,
    build_upstream_headers,
)


class UpstreamHeaderTests(unittest.TestCase):
    def test_keeps_crawler_headers_and_adds_range(self):
        headers = build_upstream_headers({"User-Agent": "ua", "Referer": "https://www.douyin.com/"}, "bytes=0-1023")
        self.assertEqual(headers["User-Agent"], "ua")
        self.assertEqual(headers["Referer"], "https://www.douyin.com/")
        self.assertEqual(headers["Range"], "bytes=0-1023")

    def test_omits_range_when_absent_or_blank(self):
        self.assertNotIn("Range", build_upstream_headers({"User-Agent": "ua"}, None))
        self.assertNotIn("Range", build_upstream_headers({"User-Agent": "ua"}, "   "))

    def test_never_injects_our_own_referer(self):
        headers = build_upstream_headers({}, None)
        self.assertEqual(headers, {"Accept-Encoding": "identity"})
        self.assertNotIn("Referer", headers)


class ResponseHeaderTests(unittest.TestCase):
    def test_passes_through_range_headers_for_seeking(self):
        headers = build_response_headers(
            {
                "Content-Type": "video/mp4",
                "Content-Length": "1024",
                "Content-Range": "bytes 0-1023/63266995",
                "Accept-Ranges": "bytes",
                "ETag": '"abc"',
                "Last-Modified": "Sat, 29 Aug 2026 22:35:19 GMT",
            }
        )
        self.assertEqual(headers["Content-Type"], "video/mp4")
        self.assertEqual(headers["Content-Length"], "1024")
        self.assertEqual(headers["Content-Range"], "bytes 0-1023/63266995")
        self.assertEqual(headers["Accept-Ranges"], "bytes")
        self.assertEqual(headers["ETag"], '"abc"')
        self.assertIn("Last-Modified", headers)

    def test_drops_upstream_cookie_and_cors_headers(self):
        headers = build_response_headers(
            {
                "Content-Type": "video/mp4",
                "Set-Cookie": "sid=secret",
                "Access-Control-Allow-Origin": "*",
                "Content-Disposition": 'attachment; filename="x.mp4"',
            }
        )
        self.assertNotIn("Set-Cookie", headers)
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertNotIn("Content-Disposition", headers)

    def test_drops_content_length_when_upstream_is_compressed(self):
        # httpx 已解压，上游长度与实际写出字节不可比（同 downloader 的 gzip 处理）
        headers = build_response_headers(
            {"Content-Type": "video/mp4", "Content-Length": "999", "Content-Encoding": "gzip"}
        )
        self.assertNotIn("Content-Length", headers)

    def test_keeps_content_length_for_identity_encoding(self):
        headers = build_response_headers(
            {"Content-Type": "video/mp4", "Content-Length": "999", "Content-Encoding": "identity"}
        )
        self.assertEqual(headers["Content-Length"], "999")

    def test_defaults_accept_ranges_and_no_store_cache(self):
        headers = build_response_headers({"Content-Type": "video/mp4"})
        self.assertEqual(headers["Accept-Ranges"], "bytes")
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_private_cache_when_seconds_given(self):
        headers = build_response_headers({"Content-Type": "video/mp4"}, cache_seconds=300)
        self.assertEqual(headers["Cache-Control"], "private, max-age=300")

    def test_empty_upstream_headers_are_safe(self):
        headers = build_response_headers(None)
        self.assertEqual(headers["Accept-Ranges"], "bytes")


if __name__ == "__main__":
    unittest.main()
