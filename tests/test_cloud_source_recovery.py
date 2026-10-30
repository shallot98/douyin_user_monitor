import hashlib
import io
from copy import deepcopy
from unittest.mock import Mock

import pytest

from scripts.cloud_playback import source_recovery as recovery
from scripts.cloud_playback.d2_client import D2HTTPError
from scripts.cloud_playback.media_remote import ARCHIVE_ROOT, CREATE_ENDPOINT, DOWNLOAD_ENDPOINT, GET_ENDPOINT
from scripts.cloud_playback.media_tools import MediaJobError

SOURCE_BYTES = b"verified upstream original bytes"
MEDIA = {"width": 1080, "height": 1920, "bit_rate": 5000000, "duration": 15.0,
         "video_codec": "hevc", "pixel_format": "yuv420p", "audio_codecs": ["aac"], "is_mp4": True,
         "faststart": True, "frame_rate": 30.0, "declared_frames": 450, "full_decode_verified": True}


@pytest.fixture
def job(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery.socket, "gethostname", lambda: "boh-test")
    path = tmp_path / "original.source.mp4"
    path.write_bytes(SOURCE_BYTES)
    destination = recovery.ORIGINAL_ROOT + "/user/123.source.mp4"
    return {"user_id": "user", "aweme_id": "123", "original_path": destination,
            "source_paths": [destination], "original_local_file": str(path), "expected_size": len(SOURCE_BYTES),
            "original_sha256": hashlib.sha256(SOURCE_BYTES).hexdigest(), "original_media": deepcopy(MEDIA)}


class Cloud:
    def __init__(self):
        self.objects = {ARCHIVE_ROOT + "/old.mp4": {"size": 0}}
        self.uploads = []

    def post(self, endpoint, payload):
        path = payload["path"]
        if endpoint == CREATE_ENDPOINT:
            created = path + "/" + payload["name"]
            self.objects[created] = {"path": created, "dir": True}
            return self.objects[created]
        if path not in self.objects:
            raise D2HTTPError(404, "file not found", code=5)
        source = deepcopy(self.objects[path])
        if endpoint == GET_ENDPOINT:
            return source
        if endpoint == DOWNLOAD_ENDPOINT:
            return {"path": path, "file_size": str(source["size"]), "content_identity": source["content_identity"],
                    "expire_at": "9999999999", "download_address": "https://source.test/original.mp4"}
        raise AssertionError(endpoint)

    def upload(self, client, path, destination):
        assert client is self
        assert destination not in self.objects
        self.uploads.append(destination)
        source = {"path": destination, "identity": "original", "content_identity": "source-cid",
                  "size": path.stat().st_size, "update_ts": "1"}
        self.objects[destination] = source
        return {**source, "status": "uploaded"}


def remote_bytes(monkeypatch, content=SOURCE_BYTES):
    def response(*args, **kwargs):
        stream = io.BytesIO(content)
        stream.status = 200
        return stream
    monkeypatch.setattr(recovery.urllib.request, "build_opener", lambda *args: Mock(open=response))
    monkeypatch.setattr(recovery, "probe_video", lambda location: {**MEDIA, "audio_codecs": ("aac",)})
    monkeypatch.setattr(recovery, "full_decode", lambda *args, **kwargs: {"full_decode_verified": True})


def test_original_is_copied_and_verified_without_overwriting_corrupt_old_archive(job, tmp_path, monkeypatch):
    remote_bytes(monkeypatch)
    cloud = Cloud()
    result = recovery.prepare_source(cloud, job, source_root=tmp_path, uploader=cloud.upload)
    assert cloud.objects[ARCHIVE_ROOT + "/old.mp4"] == {"size": 0}
    assert result["source"]["path"] == job["original_path"]
    assert result["original_artifact"]["receipt"]["sha256"] == job["original_sha256"]
    assert result["original_artifact"]["media"]["video_codec"] == "hevc"
    assert "source" not in job


def test_resume_accepts_existing_original_only_after_complete_hash_verification(job, tmp_path, monkeypatch):
    remote_bytes(monkeypatch)
    cloud = Cloud()
    recovery.prepare_source(cloud, job, source_root=tmp_path, uploader=cloud.upload)
    second = recovery.prepare_source(cloud, job, source_root=tmp_path, uploader=cloud.upload)
    assert len(cloud.uploads) == 1
    assert second["original_artifact"]["receipt"]["status"] == "existing_original_content_verified"


def test_same_size_wrong_cloud_bytes_are_rejected(job, tmp_path, monkeypatch):
    remote_bytes(monkeypatch, content=b"x" * len(SOURCE_BYTES))
    cloud = Cloud()
    with pytest.raises(MediaJobError, match="bytes differ"):
        recovery.prepare_source(cloud, job, source_root=tmp_path, uploader=cloud.upload)


def test_changed_local_original_never_reaches_cloud(job, tmp_path):
    job["original_sha256"] = "0" * 64
    client, upload = Mock(), Mock()
    with pytest.raises(MediaJobError, match="upstream bytes"):
        recovery.prepare_source(client, job, source_root=tmp_path, uploader=upload)
    client.post.assert_not_called()
    upload.assert_not_called()


def test_project_host_and_unscoped_original_paths_are_rejected(job, tmp_path, monkeypatch):
    monkeypatch.setattr(recovery.socket, "gethostname", lambda: "hdz")
    with pytest.raises(MediaJobError, match="forbidden"):
        recovery.checked_local_source(job, tmp_path)
    monkeypatch.setattr(recovery.socket, "gethostname", lambda: "boh-test")
    with pytest.raises(MediaJobError, match="outside"):
        recovery.checked_local_source(job, tmp_path / "other")
    job["original_path"] = ARCHIVE_ROOT + "/old.mp4"
    with pytest.raises(MediaJobError, match="independent"):
        recovery.checked_local_source(job, tmp_path)


def test_remote_decode_failure_does_not_produce_verified_artifact(job, tmp_path, monkeypatch):
    remote_bytes(monkeypatch)
    monkeypatch.setattr(recovery, "full_decode", Mock(side_effect=MediaJobError("decode incomplete")))
    cloud = Cloud()
    with pytest.raises(MediaJobError, match="decode incomplete"):
        recovery.prepare_source(cloud, job, source_root=tmp_path, uploader=cloud.upload)
