from copy import deepcopy
from unittest.mock import Mock

import pytest

from scripts.cloud_playback.media_remote import ARCHIVE_ROOT
from scripts.cloud_playback.media_tools import MediaJobError
from scripts.cloud_playback.publish import PublicationError
from scripts.cloud_playback.receipt import verified_publication


@pytest.fixture
def receipt():
    source = {"path": ARCHIVE_ROOT + "/original.mp4", "identity": "original", "content_identity": "source-cid",
              "size": 1000, "update_ts": "1"}
    playback = {"path": ARCHIVE_ROOT + "/_web_playback/user/123.h264.mp4", "identity": "playback",
                "content_identity": "playback-cid", "size": 200, "update_ts": "2"}
    job = {"user_id": "user", "aweme_id": "123", "operation": "transcode", "sec_user_id": "sec",
           "downloaded_at": "revision", "source_relative_path": "original.mp4",
           "previous_fast_play_file": "old.fast.mp4", "previous_cover_file": None,
           "source": {**source, "size": "1000"}, "source_paths": [source["path"]], "expected_size": 1000,
           "playback_path": playback["path"], "cover_path": None}
    result = {"user_id": "user", "aweme_id": "123", "operation": "transcode",
              "source_before": source, "source_after": deepcopy(source), "cover": None,
              "playback": {"receipt": playback, "uploaded_media_verified": True,
                           "media": {"full_decode_verified": True, "video_codec": "h264"}}}
    return {"status": "succeeded", "job": job, "result": result, "finished_at": "finished"}


def client_for(receipt):
    objects = [receipt["result"]["source_before"], receipt["result"]["playback"]["receipt"]]
    by_path = {item["path"]: deepcopy(item) for item in objects}
    client = Mock()
    client.post.side_effect = lambda endpoint, payload: by_path[payload["path"]]
    return client


def test_receipt_rechecks_cloud_metadata_with_provider_string_sizes(receipt):
    client = client_for(receipt)
    before = deepcopy(receipt)
    publication = verified_publication(client, receipt, node="boh")
    assert publication["playback_file"] == "_web_playback/user/123.h264.mp4"
    assert publication["evidence"]["source_unchanged"] is True
    assert publication["remote_verified"] is True
    assert client.post.call_count == 2
    assert all(call.args[0] == "/v6/userfile/get" for call in client.post.call_args_list)
    assert receipt == before


@pytest.mark.parametrize("field,value", [("aweme_id", "other"), ("user_id", "other"), ("operation", "cover")])
def test_worker_result_cannot_be_associated_with_a_different_job(receipt, field, value):
    receipt["result"][field] = value
    client = Mock()
    with pytest.raises(PublicationError, match="different job"):
        verified_publication(client, receipt, node="boh")
    client.post.assert_not_called()


def test_cloud_change_prevents_publication(receipt):
    client = client_for(receipt)
    receipt["result"]["playback"]["receipt"]["content_identity"] = "changed"
    with pytest.raises(MediaJobError, match="changed"):
        verified_publication(client, receipt, node="boh")


def test_source_not_in_audited_paths_is_rejected(receipt):
    receipt["job"]["source_paths"] = [ARCHIVE_ROOT + "/other.mp4"]
    with pytest.raises(PublicationError, match="audited"):
        verified_publication(Mock(), receipt, node="boh")


def test_missing_complete_decode_evidence_prevents_publication(receipt):
    receipt["result"]["playback"]["media"]["full_decode_verified"] = False
    with pytest.raises(PublicationError, match="complete uploaded"):
        verified_publication(client_for(receipt), receipt, node="boh")


def test_recovered_original_receipt_keeps_raw_backup_separate_from_playback(receipt):
    job, result = receipt["job"], receipt["result"]
    original = {"receipt": {**result["source_before"], "content_verified": True, "sha256": "upstream-hash"},
                "media": {"full_decode_verified": True, "video_codec": "hevc"}, "uploaded_media_verified": True}
    result["original"] = original
    job.update(kind="source_recovery", original_path=job["source"]["path"], original_sha256="upstream-hash",
               previous_video_file="old-original.mp4", recovery_source={"selection": "clearest"})
    publication = verified_publication(client_for(receipt), receipt, node="boh")
    assert publication["original_file"] == "original.mp4"
    assert publication["original_media"]["video_codec"] == "hevc"
    assert publication["playback_media"]["video_codec"] == "h264"
    assert publication["evidence"]["original"]["sha256"] == "upstream-hash"
