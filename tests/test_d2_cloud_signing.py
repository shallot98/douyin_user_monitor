"""Signing/CID vectors and sanitized transport failures, using synthetic data."""

from __future__ import annotations

import base64
import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from scripts.cloud_playback.d2_cid import block_cid
from scripts.cloud_playback.d2_client import (
    Credentials,
    D2Client,
    D2Error,
    D2HTTPError,
    HttpReply,
    HttpTransport,
    _NoRedirect,
    check_status,
    request_headers,
    response_json,
)


class SigningTests(unittest.TestCase):
    def setUp(self):
        self.credentials = Credentials.from_mapping({"host": "api.example.test", "client_id": "test-client",
                                                     "client_secret": "unit-test-secret"})

    def test_signature_matches_verified_official_sdk_vector(self):
        headers = request_headers(self.credentials, "/v6/userfile/create_upload_task",
                                  b'{"path":"/test.mp4","size":"8"}', timestamp_ns=1767225600123456789)
        self.assertEqual(headers["authorization"],
                         "HL6-HMAC-SHA256 Credential=test-client/2026-01-01//hl6_request, "
                         "SignedHeaders=content-type;host;other-header;x-hl-nonce;x-hl-timestamp, "
                         "Signature=bdb5fc116857941b2ce15a5b040d3c54be16ee7375a83fc302866543e5338df1")
        self.assertEqual(headers["x-hl-nonce"], "dfct6wy4nvv9")
        self.assertEqual(headers["x-hl-timestamp"], "2026-01-01T00:00:00Z")
        self.assertEqual(headers["content-type"], "application/json; charset=utf-8")

    def test_payload_and_access_token_participate_in_signature(self):
        options = {"timestamp_ns": 1767225600123456789}
        original = request_headers(self.credentials, "/v6/userfile/get", b"{}", **options)
        changed_body = request_headers(self.credentials, "/v6/userfile/get", b'{"path":"/other"}', **options)
        token_credentials = Credentials(host="api.example.test", client_id="test-client",
                                        client_secret="unit-test-secret", access_token="test-token")
        changed_token = request_headers(token_credentials, "/v6/userfile/get", b"{}", **options)
        self.assertNotEqual(original["authorization"], changed_body["authorization"])
        self.assertNotEqual(original["authorization"], changed_token["authorization"])
        self.assertIn("/2026-01-01/test-token/hl6_request", changed_token["authorization"])

    def test_credentials_repr_does_not_include_client_secrets(self):
        self.assertNotIn("unit-test-secret", repr(self.credentials))
        self.assertNotIn("test-client", repr(self.credentials))

    def test_host_is_a_host_not_a_path_or_embedded_credential(self):
        for host in ("https://api.example.test", "api.example.test/path", "id:secret@api.example.test"):
            with self.subTest(host=host), self.assertRaises(D2Error):
                Credentials.from_mapping({"host": host, "client_id": "test", "client_secret": "test"})


class CIDTests(unittest.TestCase):
    def test_sha256_raw_cid_vector(self):
        self.assertEqual(block_cid(b"abc"), "bafkreif2pall7dybz7vecqka3zo24irdwabwdi4wc55jznaq75q7eaavvu")

    def test_server_codec_uses_varint_and_sha512_has_full_digest(self):
        encoded = block_cid(b"abc", codec=0x129, hash_type=0x13)[1:].upper()
        decoded = base64.b32decode(encoded + "=" * (-len(encoded) % 8))
        self.assertEqual(decoded[:5], bytes.fromhex("01a9021340"))
        self.assertEqual(decoded[5:].hex(),
                         "ddaf35a193617abacc417349ae20413112e6fa4e89a97ea20a9eeee64b55d39a"
                         "2192992a274fc1a836ba3c23a3feebbd454d4423643ce80e2a9ac94fa54ca49f")

    def test_unknown_hash_is_not_silently_changed(self):
        with self.assertRaisesRegex(D2Error, "unsupported"):
            block_cid(b"abc", hash_type=999)


class TransportTests(unittest.TestCase):
    def test_transport_failure_does_not_print_bearer_url(self):
        transport = HttpTransport()
        with patch.object(transport._opener, "open", side_effect=urllib.error.URLError("https://host/private-bearer")):
            with self.assertRaisesRegex(D2Error, "transport failed") as error:
                transport.request("GET", "https://host/private-bearer")
        self.assertNotIn("private-bearer", str(error.exception))

    def test_http_errors_remain_available_for_protocol_classification(self):
        transport = HttpTransport()
        error = urllib.error.HTTPError("https://host/private-bearer", 401, "unauthorized", {}, io.BytesIO(b"{}"))
        with patch.object(transport._opener, "open", side_effect=error):
            reply = transport.request("GET", "https://host/private-bearer")
        self.assertEqual(reply.status, 401)
        self.assertEqual(json.loads(reply.body), {})

    def test_invalid_json_does_not_expose_response_body(self):
        with self.assertRaisesRegex(D2Error, "invalid JSON") as error:
            response_json(HttpReply(200, b"https://host/private-bearer"), "finalize")
        self.assertNotIn("private-bearer", str(error.exception))

    def test_redirect_does_not_forward_signed_headers_or_task_url(self):
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 307, None, {}, "https://other.example.test"))

    def test_insecure_hosts_only_applies_to_configured_block_host(self):
        transport = HttpTransport(insecure_hosts=("simple-file-proxy.2dland.cn",))
        self.assertIs(transport._opener_for("https://openapi.2dland.cn/v6/userfile/get"), transport._opener)
        self.assertIs(
            transport._opener_for("https://simple-file-proxy.2dland.cn/int/blocks/test"),
            transport._insecure_opener,
        )
        self.assertIsNot(transport._opener, transport._insecure_opener)


class HTTPErrorTests(unittest.TestCase):
    def test_missing_file_has_status_code_and_safe_message(self):
        credentials = Credentials(host="api.example.test", client_id="test", client_secret="test")
        transport = unittest.mock.Mock()
        transport.request.return_value = HttpReply(404, b'{"code":5,"message":"file not found"}')
        client = D2Client(credentials, transport=transport)
        with self.assertRaises(D2HTTPError) as caught:
            client.post("/v6/userfile/get", {"path": "/missing"})
        error = caught.exception
        self.assertIsInstance(error, D2Error)
        self.assertEqual((error.status, error.code, error.message), (404, 5, "file not found"))
        self.assertEqual(str(error), "D2 /v6/userfile/get: HTTP 404")
        self.assertEqual(transport.request.call_count, 1)

    def test_arbitrary_provider_text_is_not_retained(self):
        for payload in ({"code": "private-token", "message": "https://host/private-bearer"},
                        {"code": True, "message": "private-token"}, {"message": ["file not found"]}):
            with self.subTest(payload=payload):
                reply = HttpReply(500, json.dumps(payload).encode())
                with self.assertRaises(D2HTTPError) as caught:
                    check_status(reply, "finalize")
                error = caught.exception
                self.assertEqual((error.status, error.code, error.message), (500, None, None))
                self.assertNotIn("private-", repr(error))
                self.assertNotIn("private-", repr(vars(error)))

    def test_malformed_error_body_preserves_http_status(self):
        for body in (b"not json https://host/private-bearer", b"null", b"[]", b"\xff"):
            with self.subTest(body=body), self.assertRaises(D2HTTPError) as caught:
                check_status(HttpReply(404, body), "/v6/userfile/get")
            self.assertEqual((caught.exception.status, caught.exception.code, caught.exception.message),
                             (404, None, None))

    def test_auth_failure_keeps_original_diagnostic_and_optional_numeric_code(self):
        reply = HttpReply(401, b'{"code":16,"message":"private-token"}')
        with self.assertRaises(D2HTTPError) as caught:
            check_status(reply, "/v6/userfile/get")
        self.assertEqual((caught.exception.status, caught.exception.code, caught.exception.message), (401, 16, None))
        self.assertIn("authentication or authorization failed; token refresh disabled", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
