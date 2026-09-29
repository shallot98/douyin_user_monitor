"""Offline-import protocol tests using synthetic bytes and injected transports."""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from scripts.cloud_playback.d2_client import D2Error, D2HTTPError
from scripts.cloud_playback.d2_offline import ADD_ENDPOINT, LIST_ENDPOINT, OfflineImportError, OfflineUploader
from scripts.cloud_playback.media_remote import DOWNLOAD_ENDPOINT, GET_ENDPOINT

DESTINATION = "/vps/抖音视频/_web_playback/user/9001.h264.mp4"
PAYLOAD = b"synthetic completed derivative"
ORIGIN_URL = "https://origin.example.test/private-origin-token/9001.h264.mp4"
D2_URL = "https://download.example.test/file?signature=private-download-token"


class Clock:
    def __init__(self):
        self.value = 0

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class RetainedOrigin:
    def __init__(self):
        self.copies = {}

    def publish(self, path, basename):
        self.copies[basename] = path.read_bytes()
        return ORIGIN_URL


class Client:
    def __init__(self):
        self.calls = []
        self.added = False
        self.preexisting = False
        self.lists = 0
        self.ready_after = 2
        self.task = {"identity": "task-123", "status": -7, "code": 0, "progress": "17",
                     "bytes_total": str(len(PAYLOAD)), "bytes_processed": "3"}
        self.metadata = {"path": DESTINATION, "identity": "stored-file", "content_identity": "stored-cid",
                         "size": str(len(PAYLOAD)), "update_ts": "123"}
        self.list_pages = None

    def post(self, endpoint, payload):
        self.calls.append((endpoint, payload))
        if endpoint == GET_ENDPOINT:
            if self.preexisting or self.added and self.lists >= self.ready_after:
                return dict(self.metadata)
            raise D2HTTPError(404, "file not found", code=5)
        if endpoint == ADD_ENDPOINT:
            self.added = True
            return dict(self.task)
        if endpoint == LIST_ENDPOINT:
            self.lists += 1
            return next(self.list_pages) if self.list_pages else {"tasks": [dict(self.task)]}
        if endpoint == DOWNLOAD_ENDPOINT:
            return {"path": DESTINATION, "content_identity": self.metadata["content_identity"],
                    "file_size": str(len(PAYLOAD)), "download_address": D2_URL, "expire_at": "9999999999"}
        raise AssertionError(endpoint)


class OfflineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name) / "completed.mp4"
        self.source.write_bytes(PAYLOAD)
        self.client = Client()
        self.origin = RetainedOrigin()
        self.clock = Clock()
        self.reader = Mock(side_effect=lambda address: io.BytesIO(PAYLOAD))

    def uploader(self, **changes):
        settings = {"origin": self.origin, "timeout_seconds": 3, "poll_interval_seconds": 1,
                    "monotonic": self.clock.monotonic, "sleep": self.clock.sleep, "open_stream": self.reader}
        return OfflineUploader(**{**settings, **changes})

    def upload(self, **changes):
        return self.uploader(**changes)(self.client, self.source, DESTINATION)

    def test_only_full_bytes_and_sha256_accept_independent_of_status_numbers(self):
        result = self.upload()
        self.assertEqual(result["status"], "uploaded")
        self.assertEqual(result["transport"], "offline")
        self.assertEqual(result["sha256"], hashlib.sha256(PAYLOAD).hexdigest())
        self.assertTrue(result["content_verified"])
        self.assertEqual(result["offline_task"]["status"], -7)
        self.assertEqual(self.source.read_bytes(), PAYLOAD)
        self.assertEqual(self.origin.copies["9001.h264.mp4"], PAYLOAD)
        adds = [body for endpoint, body in self.client.calls if endpoint == ADD_ENDPOINT]
        self.assertEqual(adds, [{"url": ORIGIN_URL, "save_path": str(Path(DESTINATION).parent)}])
        self.assertFalse(any("parse" in endpoint for endpoint, _ in self.client.calls))
        self.assertNotIn("private-", json.dumps(result))

    def test_received_task_and_progress_do_not_count_as_upload_success(self):
        self.client.ready_after = 100
        self.client.task.update(status=2, progress="100", bytes_processed=str(len(PAYLOAD)))
        with self.assertRaisesRegex(OfflineImportError, "timed out") as error:
            self.upload()
        self.assertEqual(error.exception.task_identity, "task-123")
        self.assertEqual(error.exception.history[-1]["status"], 2)
        self.assertEqual(self.clock.value, 3)
        self.assertEqual(self.origin.copies["9001.h264.mp4"], PAYLOAD)
        self.reader.assert_not_called()

    def test_paginated_list_selects_returned_identity_not_unrelated_task(self):
        self.client.list_pages = iter([
            {"tasks": [{"identity": "unrelated", "code": 7}], "list_info": {"token": "page-2"}},
            {"tasks": [dict(self.client.task)]},
        ])
        result = self.upload()
        self.assertEqual(result["offline_task"]["identity"], "task-123")
        payloads = [body for endpoint, body in self.client.calls if endpoint == LIST_ENDPOINT]
        self.assertEqual(payloads, [{"list_info": {"limit": "100", "token": ""}},
                                    {"list_info": {"limit": "100", "token": "page-2"}}])

    def test_pagination_cycle_is_an_explicit_failure(self):
        self.client.list_pages = iter([{"list_info": {"token": "same"}}, {"list_info": {"token": "same"}}])
        with self.assertRaisesRegex(OfflineImportError, "did not advance"):
            self.upload()

    def test_existing_destination_does_not_publish_or_add(self):
        self.client.preexisting = True
        with self.assertRaisesRegex(OfflineImportError, "already exists"):
            self.upload()
        self.assertFalse(self.client.added)
        self.assertEqual(self.origin.copies, {})

    def test_destination_appearing_during_publication_is_not_overwritten(self):
        publish = self.origin.publish
        def appearing(path, basename):
            self.client.preexisting = True
            return publish(path, basename)
        self.origin.publish = appearing
        with self.assertRaisesRegex(OfflineImportError, "appeared before add"):
            self.upload()
        self.assertFalse(self.client.added)
        self.assertEqual(self.origin.copies["9001.h264.mp4"], PAYLOAD)

    def test_provider_rejection_preserves_code_and_redacts_source_url(self):
        self.client.task = {"code": -12, "message": "unsupported URL: " + ORIGIN_URL}
        with self.assertRaisesRegex(OfflineImportError, "code -12: unsupported URL") as error:
            self.upload()
        self.assertEqual(error.exception.history[0]["code"], -12)
        self.assertNotIn("private-origin-token", str(error.exception))
        self.assertNotIn("private-origin-token", json.dumps(error.exception.history))
        self.assertEqual(self.client.lists, 0)

    def test_empty_add_200_is_not_a_success(self):
        self.client.task = {}
        with self.assertRaisesRegex(OfflineImportError, "lacks a task identity"):
            self.upload()
        self.reader.assert_not_called()

    def test_matching_size_with_wrong_hash_is_rejected(self):
        self.reader.side_effect = lambda address: io.BytesIO(b"x" * len(PAYLOAD))
        with self.assertRaisesRegex(OfflineImportError, "SHA-256 differs") as error:
            self.upload()
        self.assertEqual(error.exception.task_identity, "task-123")
        self.assertEqual(self.origin.copies["9001.h264.mp4"], PAYLOAD)

    def test_short_or_extra_download_bytes_are_rejected(self):
        for content in (PAYLOAD[:-1], PAYLOAD + b"extra"):
            with self.subTest(content=content):
                self.client = Client()
                self.clock.value = 0
                self.reader.side_effect = lambda address, value=content: io.BytesIO(value)
                with self.assertRaisesRegex(OfflineImportError, "byte count"):
                    self.upload()

    def test_source_change_after_publish_prevents_add(self):
        publish = self.origin.publish
        def mutating(path, basename):
            address = publish(path, basename)
            path.write_bytes(PAYLOAD + b"modified")
            return address
        self.origin.publish = mutating
        with self.assertRaisesRegex(D2Error, "Local derivative changed"):
            self.upload()
        self.assertFalse(self.client.added)

    def test_unknown_http_failure_is_never_treated_as_absence(self):
        self.client.post = Mock(side_effect=D2HTTPError(404, "unknown missing", code=7))
        with self.assertRaises(D2HTTPError):
            self.upload()
        self.assertEqual(self.origin.copies, {})

    def test_cloud_object_change_after_content_read_is_rejected(self):
        def read(address):
            self.client.metadata["content_identity"] = "changed-cid"
            return io.BytesIO(PAYLOAD)
        self.reader.side_effect = read
        with self.assertRaisesRegex(OfflineImportError, "changed"):
            self.upload()

    def test_origin_name_and_https_are_validated_before_add(self):
        for address in ("http://origin.test/9001.h264.mp4", "https://origin.test/wrong.mp4"):
            with self.subTest(address=address):
                self.origin.publish = Mock(return_value=address)
                with self.assertRaisesRegex(D2Error, "HTTPS address with the destination basename"):
                    self.upload()
        self.assertFalse(self.client.added)

    def test_poll_error_keeps_last_reported_provider_diagnostic(self):
        self.client.list_pages = iter([{"tasks": [{**self.client.task, "code": 13, "message": "fetch failed " + D2_URL}]}])
        with self.assertRaisesRegex(OfflineImportError, "code 13") as error:
            self.upload()
        self.assertEqual(error.exception.history[-1]["code"], 13)
        self.assertNotIn("private-download-token", json.dumps(error.exception.history))


if __name__ == "__main__":
    unittest.main()
