"""Synthetic protocol tests; these never contact D2 or upload real media."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from scripts.cloud_playback.d2_client import Credentials, D2Client, D2Error, HttpReply, json_body
from scripts.cloud_playback.d2_upload import CREATE_UPLOAD_ENDPOINT, GET_FILE_ENDPOINT, UploadTask, upload_file

DESTINATION = "/vps/抖音视频/_web_playback/user/测试 视频.h264.mp4"
CID_ABC = "bafkreif2pall7dybz7vecqka3zo24irdwabwdi4wc55jznaq75q7eaavvu"
CID_DE = "bafkreievtjc5ittpz5mdmhwqarubkvx6kajj6iij5al55qeyyagj4xjfpa"
API = "https://api.example.test"
UPLOAD = "https://upload.example.test/blocks/test-task"


def task_response(**changes):
    return {"upload_address": "https://upload.example.test/blocks", "task": "test-task",
            "block_size": "3", "block_codec": "85", "block_hash_type": "18", "block_version": 1, **changes}


def file_response(**changes):
    return {"identity": "test-file", "path": DESTINATION, "size": 8,
            "content_identity": "test-content", **changes}


def finalize_response(**changes):
    return {"path": DESTINATION, "size": 8, "content_identity": "test-content", **changes}


class ScriptedTransport:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return next(self.responses)


def client_for(responses):
    credentials = Credentials(host="api.example.test", client_id="test-client", client_secret="unit-test-secret")
    return D2Client(credentials, transport=ScriptedTransport(responses))


def reply(value, status=200):
    return HttpReply(status, json_body(value))


class UploadProtocolTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.source = Path(self.directory.name) / "completed.mp4"
        self.source.write_bytes(b"abcabcde")

    def test_cids_keep_order_duplicate_blocks_and_partial_tail(self):
        client = client_for([reply(task_response()), reply(False), reply(None, 201), reply(True),
                             reply(False), reply(None, 201), reply(finalize_response(), 201), reply(file_response())])
        result = upload_file(client, self.source, DESTINATION)
        self.assertEqual(result, {"status": "uploaded", **file_response(), "blocks_total": 3,
                                  "blocks_uploaded": 2, "blocks_reused": 1})
        calls = client.transport.calls
        self.assertEqual([(call["method"], call["url"]) for call in calls], [
            ("POST", API + CREATE_UPLOAD_ENDPOINT), ("GET", UPLOAD + "/" + CID_ABC),
            ("POST", UPLOAD + "/" + CID_ABC), ("GET", UPLOAD + "/" + CID_ABC),
            ("GET", UPLOAD + "/" + CID_DE), ("POST", UPLOAD + "/" + CID_DE), ("POST", UPLOAD),
            ("POST", API + GET_FILE_ENDPOINT),
        ])
        self.assertEqual(json.loads(calls[0]["body"]), {"path": DESTINATION, "size": "8"})
        self.assertEqual(calls[2]["body"], b"abc")
        self.assertEqual(calls[5]["body"], b"de")
        self.assertEqual(json.loads(calls[6]["body"]), [CID_ABC, CID_ABC, CID_DE])
        self.assertEqual(json.loads(calls[7]["body"]), {"path": DESTINATION})
        self.assertTrue(all("authorization" not in call["headers"] for call in calls[1:-1]))
        self.assertEqual(self.source.read_bytes(), b"abcabcde")

    def test_reused_blocks_still_have_to_finalize(self):
        client = client_for([reply(task_response()), reply(True), reply(True), reply(True),
                             reply(finalize_response(size="8")), reply(file_response(size="8"))])
        result = upload_file(client, self.source, DESTINATION)
        self.assertEqual(result["blocks_uploaded"], 0)
        self.assertEqual(result["blocks_reused"], 3)
        self.assertEqual(client.transport.calls[-2]["url"], UPLOAD)

    def test_provider_task_colons_are_preserved_for_blocks_and_finalization(self):
        self.source.write_bytes(b"abc")
        client = client_for([
            reply(task_response(task="bucket::session:0::opaque")), reply(False),
            reply(None, 201), reply(finalize_response(size=3), 201), reply(file_response(size=3)),
        ])
        result = upload_file(client, self.source, DESTINATION)
        task_url = "https://upload.example.test/blocks/bucket::session:0::opaque"
        self.assertEqual(result["status"], "uploaded")
        self.assertEqual([(call["method"], call["url"]) for call in client.transport.calls[1:-1]], [
            ("GET", task_url + "/" + CID_ABC),
            ("POST", task_url + "/" + CID_ABC),
            ("POST", task_url),
        ])

    def test_finalize_rejects_invalid_path_size_or_content_identity(self):
        for metadata in (finalize_response(path=DESTINATION + "(1)"), finalize_response(size=7),
                         finalize_response(content_identity=""), finalize_response(size=True)):
            with self.subTest(metadata=metadata):
                client = client_for([reply(task_response()), reply(True), reply(True), reply(True), reply(metadata)])
                with self.assertRaises(D2Error):
                    upload_file(client, self.source, DESTINATION)

    def test_finalize_acknowledgement_without_identity_reads_verified_metadata(self):
        client = client_for([reply(task_response()), reply(True), reply(True), reply(True),
                             reply(finalize_response()), reply(file_response())])
        result = upload_file(client, self.source, DESTINATION)
        self.assertEqual(result["identity"], "test-file")
        self.assertEqual(client.transport.calls[-1]["url"], API + GET_FILE_ENDPOINT)

    def test_finalized_metadata_content_identity_must_match_acknowledgement(self):
        client = client_for([reply(task_response()), reply(True), reply(True), reply(True),
                             reply(finalize_response()), reply(file_response(content_identity="other-content"))])
        with self.assertRaisesRegex(D2Error, "content identity"):
            upload_file(client, self.source, DESTINATION)

    def test_metadata_lookup_rejects_invalid_path_size_or_identity(self):
        for metadata in (file_response(path=DESTINATION + "(1)"), file_response(size=7),
                         file_response(identity=""), file_response(content_identity="")):
            with self.subTest(metadata=metadata):
                client = client_for([reply(task_response()), reply(True), reply(True), reply(True),
                                     reply(finalize_response()), reply(metadata)])
                with self.assertRaises(D2Error):
                    upload_file(client, self.source, DESTINATION)

    def test_already_created_is_not_proof_of_matching_contents(self):
        client = client_for([reply({"created": True}), reply(file_response(size="8"))])
        with self.assertRaisesRegex(D2Error, "matching metadata does not verify source contents"):
            upload_file(client, self.source, DESTINATION)
        self.assertEqual(len(client.transport.calls), 2)
        self.assertEqual(json.loads(client.transport.calls[1]["body"]), {"path": DESTINATION})

    def test_presence_requires_actual_boolean_not_truthy_data(self):
        for value in ("false", "true", 1, {}, None):
            with self.subTest(value=value):
                client = client_for([reply(task_response()), reply(value)])
                with self.assertRaisesRegex(D2Error, "JSON boolean"):
                    upload_file(client, self.source, DESTINATION)
                self.assertEqual(len(client.transport.calls), 2)

    def test_upload_failure_does_not_finalize_or_report_success(self):
        client = client_for([reply(task_response()), reply(False), reply({"message": "private-url"}, 500)])
        with self.assertRaisesRegex(D2Error, "block upload: HTTP 500") as error:
            upload_file(client, self.source, DESTINATION)
        self.assertNotIn("private-url", str(error.exception))
        self.assertEqual(len(client.transport.calls), 3)

    def test_authentication_failure_is_explicit_and_not_refreshed(self):
        client = client_for([reply({"message": "private-token"}, 401)])
        with self.assertRaisesRegex(D2Error, "token refresh disabled") as error:
            upload_file(client, self.source, DESTINATION)
        self.assertNotIn("private-token", str(error.exception))
        self.assertEqual(len(client.transport.calls), 1)

    def test_source_mutation_prevents_finalization(self):
        client = client_for([reply(task_response()), reply(True), reply(True), reply(True)])
        request = client.transport.request

        def mutate_source(method, url, **kwargs):
            if method == "GET":
                state = self.source.stat()
                os.utime(self.source, ns=(state.st_atime_ns, state.st_mtime_ns + 1))
            return request(method, url, **kwargs)

        client.transport.request = mutate_source
        with self.assertRaisesRegex(D2Error, "Source file changed"):
            upload_file(client, self.source, DESTINATION)
        self.assertEqual(len(client.transport.calls), 4)

    def test_invalid_source_or_destination_sends_no_requests(self):
        client = client_for([])
        for destination in ("relative.mp4", "/parent/../original.mp4", "/folder/"):
            with self.subTest(destination=destination), self.assertRaises(D2Error):
                upload_file(client, self.source, destination)
        self.source.write_bytes(b"")
        with self.assertRaisesRegex(D2Error, "empty"):
            upload_file(client, self.source, DESTINATION)
        self.assertEqual(client.transport.calls, [])


class UploadTaskTests(unittest.TestCase):
    def test_zero_or_omitted_codec_and_hash_use_documented_defaults(self):
        task = UploadTask.from_response(task_response(block_codec="0", block_hash_type="0"))
        self.assertEqual((task.codec, task.hash_type, task.block_size), (0x55, 0x12, 3))
        task = UploadTask.from_response({"upload_address": "https://upload.example.test/blocks", "task": "test-task",
                                         "block_size": "4194304"})
        self.assertEqual((task.codec, task.hash_type), (0x55, 0x12))

    def test_explicit_unknown_hash_or_version_is_rejected(self):
        for changes in ({"block_hash_type": "9999"}, {"block_version": 2}, {"block_size": "0"},
                        {"block_size": True}, {"block_size": "-1"}):
            with self.subTest(changes=changes), self.assertRaises(D2Error):
                UploadTask.from_response(task_response(**changes))

    def test_task_bearer_is_not_exposed_in_repr(self):
        task = UploadTask.from_response(task_response(task="private-task-bearer"))
        self.assertNotIn("private-task-bearer", repr(task))


if __name__ == "__main__":
    unittest.main()
