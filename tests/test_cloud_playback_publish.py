from copy import deepcopy

import pytest
from record_helpers import seed_state

from douyin_user_monitor.monitor.storage import MonitorStorage
from scripts.cloud_playback.publish import PublicationError, publish_verified


@pytest.fixture
def fixture(tmp_path):
    state = {
        "users": [{"id": "user", "sec_user_id": "sec", "download_records": [{
            "aweme_id": "work", "downloaded_at": "revision", "files": ["old/original.mp4", "old/fast.mp4"],
            "video_file": "old/original.mp4", "fast_play_file": "old/fast.mp4", "total_size_bytes": 123,
        }]}],
        "monitoring": {"desired_running": True},
    }
    publication = {
        "user_id": "user", "aweme_id": "work", "sec_user_id": "sec", "downloaded_at": "revision",
        "source_relative_path": "old/original.mp4", "previous_fast_play_file": "old/fast.mp4",
        "previous_cover_file": None, "playback_file": "_web_playback/user/work.h264.mp4",
        "cover_file": "_web_playback/user/work.cover.jpg", "remote_verified": True,
        "operation": "transcode", "verified_at": "verified", "evidence": {"node": "boh"},
        "playback_media": {"width": 540, "height": 960, "bit_rate": 900000, "duration": 15.0,
                           "video_codec": "h264", "pixel_format": "yuv420p", "audio_codecs": ["aac"],
                           "is_mp4": True, "faststart": True, "full_decode_verified": True},
    }
    storage = MonitorStorage(tmp_path / "state.json")
    seed_state(storage, state)
    return storage, publication


def test_preserves_concurrent_monitoring_and_original_history(fixture):
    storage, publication = fixture
    storage.load_state()
    other_process = MonitorStorage(storage.state_file)

    def monitor_update(state):
        other_process.records.put("user", [*other_process.records.get("user"), {"aweme_id": "new", "files": ["new.mp4"]}])
        state["users"][0]["last_checked_at"] = "newer"
        state["monitoring"]["last_run_at"] = "newer"

    other_process.update_state(monitor_update)
    result = publish_verified(storage, [publication])
    record = result["users"][0]["download_records"][0]
    assert record["video_file"] == "old/original.mp4"
    assert record["files"][:2] == ["old/original.mp4", "old/fast.mp4"]
    assert record["total_size_bytes"] == 123
    assert record["downloaded_at"] == "revision"
    assert result["users"][0]["download_records"][1]["aweme_id"] == "new"
    assert result["users"][0]["last_checked_at"] == "newer"
    assert result["monitoring"]["last_run_at"] == "newer"
    assert record["cloud_playback_repair"]["previous_references"]["fast_play_file"] == "old/fast.mp4"


def test_repeated_publication_is_idempotent(fixture):
    storage, publication = fixture
    first = deepcopy(publish_verified(storage, [publication]))
    second = publish_verified(storage, [publication])
    assert first == second


@pytest.mark.parametrize("field,value", [
    ("sec_user_id", "different"), ("downloaded_at", "different"),
    ("source_relative_path", "missing.mp4"), ("previous_fast_play_file", "different.mp4"),
])
def test_stale_source_does_not_write_state(fixture, field, value):
    storage, publication = fixture
    before = storage.state_file.read_bytes()
    records_before = deepcopy(storage.records.get("user"))
    publication[field] = value
    with pytest.raises(PublicationError):
        publish_verified(storage, [publication])
    assert storage.state_file.read_bytes() == before
    assert MonitorStorage(storage.state_file).records.get("user") == records_before


@pytest.mark.parametrize("field,value", [
    ("video_codec", "hevc"), ("faststart", False), ("full_decode_verified", False),
    ("audio_codecs", ["opus"]), ("pixel_format", "yuv420p10le"),
])
def test_unverified_or_incompatible_media_is_never_published(fixture, field, value):
    storage, publication = fixture
    before = storage.state_file.read_bytes()
    records_before = deepcopy(storage.records.get("user"))
    publication["playback_media"][field] = value
    with pytest.raises(PublicationError):
        publish_verified(storage, [publication])
    assert storage.state_file.read_bytes() == before
    assert MonitorStorage(storage.state_file).records.get("user") == records_before


def test_cover_only_publication_keeps_existing_playback(fixture):
    storage, publication = fixture
    publication["playback_file"] = None
    publication.pop("playback_media")
    state = publish_verified(storage, [publication])
    record = state["users"][0]["download_records"][0]
    assert record["fast_play_file"] == "old/fast.mp4"
    assert record["cover_file"] == "_web_playback/user/work.cover.jpg"


def test_failed_batch_does_not_mutate_cached_state(fixture):
    storage, publication = fixture
    before = deepcopy(storage.load_state())
    records_before = deepcopy(storage.records.get("user"))
    missing = {**publication, "aweme_id": "missing"}
    with pytest.raises(PublicationError):
        publish_verified(storage, [publication, missing])
    assert storage.load_state() == before
    assert storage.records.get("user") == records_before


def test_input_objects_remain_unchanged(fixture):
    storage, publication = fixture
    before = deepcopy(publication)
    publish_verified(storage, [publication])
    assert publication == before


def recovered_original(publication):
    return {**deepcopy(publication), "original_file": "_recovered_originals/user/work.source.mp4",
            "previous_video_file": "old/original.mp4",
            "original_media": {**publication["playback_media"], "video_codec": "hevc", "bit_rate": 5000000},
            "evidence": {**publication["evidence"], "original": {"content_verified": True, "sha256": "verified"}}}


def test_recovered_original_becomes_download_target_and_keeps_old_archive_reference(fixture):
    storage, publication = fixture
    publication = recovered_original(publication)
    result = publish_verified(storage, [publication])
    record = result["users"][0]["download_records"][0]
    assert record["video_file"] == publication["original_file"]
    assert record["files"][:2] == ["old/original.mp4", "old/fast.mp4"]
    assert record["video_source"]["video_codec"] == "hevc"
    assert record["total_size_bytes"] == 123 and record["downloaded_at"] == "revision"
    assert record["cloud_playback_repair"]["previous_references"]["video_file"] == "old/original.mp4"


def test_concurrent_original_replacement_cannot_be_overwritten(fixture):
    storage, publication = fixture
    publication = recovered_original(publication)
    records = deepcopy(storage.records.get("user"))
    records[0]["video_file"] = "new-original.mp4"
    storage.update_state(lambda state: storage.records.put("user", records))
    before = storage.state_file.read_bytes()
    with pytest.raises(PublicationError, match="Concurrent video_file"):
        publish_verified(storage, [publication])
    assert storage.state_file.read_bytes() == before
    assert storage.records.get("user") == records


def test_unverified_original_cannot_change_archive_reference(fixture):
    storage, publication = fixture
    publication = recovered_original(publication)
    publication["evidence"]["original"]["content_verified"] = False
    before = storage.state_file.read_bytes()
    records_before = deepcopy(storage.records.get("user"))
    with pytest.raises(PublicationError, match="Recovered original"):
        publish_verified(storage, [publication])
    assert storage.state_file.read_bytes() == before
    assert storage.records.get("user") == records_before
