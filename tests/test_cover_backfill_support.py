"""Small dependency fakes shared by cover orchestration tests."""

from __future__ import annotations

import hashlib

from scripts.cloud_playback.d2_client import D2HTTPError
from scripts.cloud_playback.media_remote import DOWNLOAD_ENDPOINT, GET_ENDPOINT
from scripts.cover_backfill.journal import jpeg_bytes
from scripts.cover_backfill.models import parse_job

PRIVATE_URL = "https://private.example.test/video?token=do-not-publish"
JPEG = b"\xff\xd8orchestration-test-bytes\xff\xd9"


def make_job(aweme_id="9001", user_id="user-1", paths=None):
    return parse_job({"user_id": user_id, "aweme_id": aweme_id, "sec_user_id": "sec-" + user_id,
                      "downloaded_at": "2026-09-14T12:00:00+08:00", "source_relative_path": f"old/{aweme_id}.mp4",
                      "source_paths": paths or [f"/vps/抖音视频/{user_id}/{aweme_id}.mp4"]})


def source_record(path):
    return {"path": path, "identity": "source-" + path.rsplit("/", 1)[-1],
            "content_identity": "content-" + path.rsplit("/", 1)[-1], "size": 5000, "update_ts": "123"}


def fake_inspect(path):
    data = jpeg_bytes(path)
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "width": 32, "height": 48}


class FakeD2:
    def __init__(self, paths):
        self.objects = {path: source_record(path) for path in paths}
        self.calls = []
        self.errors = {}

    def post(self, endpoint, payload):
        path = payload["path"]
        self.calls.append((endpoint, path))
        if path in self.errors:
            raise self.errors[path]
        if path not in self.objects:
            raise D2HTTPError(404, "file not found", code=5)
        source = dict(self.objects[path])
        if endpoint == GET_ENDPOINT:
            return source
        if endpoint == DOWNLOAD_ENDPOINT:
            return {"path": path, "content_identity": source["content_identity"], "file_size": source["size"],
                    "expire_at": "9999999999", "download_address": PRIVATE_URL}
        raise AssertionError("A read-only cover worker attempted a D2 write")
