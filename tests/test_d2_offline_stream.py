"""Bounded streaming and credential-free error checks, without network access."""

from __future__ import annotations

import hashlib
import io
import unittest
import urllib.error
from unittest.mock import Mock, patch

from scripts.cloud_playback.d2_client import D2Error
from scripts.cloud_playback.d2_offline import (
    HASH_CHUNK_SIZE,
    OfflineUploader,
    _hash_stream,
    _open_stream,
    _remote_digest,
)

PRIVATE_URL = "https://download.example.test/file?token=private-bearer"


class OfflineStreamTests(unittest.TestCase):
    def test_hash_reads_bounded_chunks_and_counts_every_byte(self):
        payload = b"a" * (HASH_CHUNK_SIZE * 2 + 17)
        stream = Mock(wraps=io.BytesIO(payload))
        self.assertEqual(_hash_stream(stream, len(payload)), hashlib.sha256(payload).hexdigest())
        self.assertTrue(all(call.args == (HASH_CHUNK_SIZE,) for call in stream.read.call_args_list))

    def test_read_errors_do_not_expose_bearer_urls(self):
        for exception in (urllib.error.URLError(PRIVATE_URL), OSError(PRIVATE_URL),
                          urllib.error.HTTPError(PRIVATE_URL, 503, "failed", {}, None)):
            reader = Mock(side_effect=exception)
            with self.subTest(exception=type(exception).__name__), self.assertRaises(D2Error) as caught:
                _remote_digest(reader, PRIVATE_URL, 10, deadline=100, monotonic=lambda: 0)
            self.assertNotIn("private-bearer", str(caught.exception))

    def test_stream_deadline_is_checked_before_further_reads(self):
        stream = Mock(wraps=io.BytesIO(b"payload"))
        clock = Mock(side_effect=[0, 2])
        with self.assertRaisesRegex(D2Error, "verification timed out"):
            _hash_stream(stream, 7, deadline=1, monotonic=clock)
        self.assertEqual(stream.read.call_count, 1)

    def test_direct_get_disables_proxy_and_requests_identity_encoding(self):
        response = Mock(status=200)
        opener = Mock()
        opener.open.return_value = response
        with patch("scripts.cloud_playback.d2_offline.urllib.request.build_opener", return_value=opener) as builder:
            self.assertIs(_open_stream(PRIVATE_URL), response)
        self.assertEqual(builder.call_args.args[0].proxies, {})
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Accept-encoding"), "identity")

    def test_non_200_stream_is_rejected_and_closed(self):
        response = Mock(status=206)
        opener = Mock()
        opener.open.return_value = response
        with patch("scripts.cloud_playback.d2_offline.urllib.request.build_opener", return_value=opener):
            with self.assertRaisesRegex(D2Error, "HTTP 206"):
                _open_stream(PRIVATE_URL)
        response.close.assert_called_once()

    def test_polling_configuration_is_explicit_and_finite(self):
        for value in (0, -1, float("inf"), float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                OfflineUploader(origin=object(), timeout_seconds=value)


if __name__ == "__main__":
    unittest.main()
