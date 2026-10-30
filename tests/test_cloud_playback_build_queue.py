from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest

from douyin_user_monitor.monitor.archive_path_aliases import ArchivePathAliases
from douyin_user_monitor.monitor.openlist_client import OpenListClient
from douyin_user_monitor.settings import ArchiveSettings
from scripts.cloud_playback.build_queue import SPECIAL_CLASSES, build_queue
from scripts.cloud_playback.media_remote import PLAYBACK_ROOT, validate_job


def work(user_id="user", aweme_id="2", *, nickname="Other", codec="avc1", width=540, height=960,
         size=900000, duration=10, recorded_duration=10000, cover=True, trailing=False):
    original, selected, image = (f"{user_id}/{aweme_id}{suffix}" for suffix in (".mp4", ".fast.mp4", ".jpg"))
    files = [original, selected, *([image] if cover else [])]
    record = {"aweme_id": aweme_id, "media_type": "video", "files": files, "downloaded_at": "revision",
              "video_file": original, "fast_play_file": selected, "cover_file": image if cover else None,
              "total_size_bytes": size * 9, "video_duration_ms": recorded_duration}
    snapshot = {"user_id": user_id, "aweme_id": aweme_id, "nickname": nickname, "sec_user_id": "sec-" + user_id,
                "downloaded_at": "revision", "fingerprint": user_id + aweme_id, "files": list(files),
                "relative_path": selected, "play_index": 1, "cover_index": 2 if cover else None}
    inventory = {**snapshot, "current_play_file": selected,
                 "classification": "hevc_browser_incompatible" if codec == "hvc1" else "h264_candidate"}
    atoms = [{"type": "mdat", "offset": 24}, {"type": "moov", "offset": 800000}] if trailing else [
        {"type": "ftyp", "offset": 0}, {"type": "moov", "offset": 24}]
    probe = {**snapshot, "status": "ok", "file_bytes": size, "duration_seconds": duration, "atoms": atoms,
             "streams": [{"type": "vide", "codec": codec, "width": width, "height": height},
                         {"type": "soun", "codec": "mp4a"}]}
    user = {"id": user_id, "sec_user_id": snapshot["sec_user_id"], "download_records": [record]}
    return {"snapshot": snapshot, "inventory": inventory, "probe": probe, "user": user}


def documents(*works):
    pilot = work("pilot", "1", nickname="贴心的小饱饱", codec="hvc1")
    snapshot = pilot["snapshot"]
    prefix = f"{PLAYBACK_ROOT}/pilot/1"
    pilot_job = {**{name: snapshot[name] for name in (
        "user_id", "aweme_id", "nickname", "sec_user_id", "downloaded_at", "fingerprint")},
        "source_relative_path": snapshot["files"][0], "source_paths": ["/vps/抖音视频/" + snapshot["files"][0]],
        "expected_size": 12345, "expected_duration": 10, "operation": "transcode",
        "playback_path": prefix + ".h264.mp4", "cover_path": prefix + ".cover.jpg",
        "previous_fast_play_file": snapshot["relative_path"], "previous_cover_file": snapshot["files"][2]}
    all_works = [pilot, *works]
    return {"manifest": {"at": "fixed-time", "items": [item["snapshot"] for item in all_works]},
            "inventory": {"videos": [item["inventory"] for item in all_works]},
            "probes": [item["probe"] for item in all_works], "recovered": [],
            "state": {"users": [item["user"] for item in all_works]}, "pilot": {"jobs": [pilot_job]}}


def build(inputs, *, aliases=()):
    config = ArchiveSettings(enabled=True, openlist_base_url="http://localhost", openlist_db_path=Path("unused"),
                             openlist_token="", primary_base="/d2/vps/抖音视频", backup_base="/kuake/抖音视频",
                             timeout_seconds=3, sanitize_primary_dirs=False)
    client = OpenListClient(config)
    registry = ArchivePathAliases(aliases)
    return build_queue(inputs, remote_paths_for=client.remote_paths_for, alias_paths_for=registry.paths_for)


def test_selected_audited_source_is_used_without_changing_originals_or_input(monkeypatch):
    inputs = documents(work(codec="hvc1"))
    original = deepcopy(inputs)
    network = Mock(side_effect=AssertionError("Queue construction must not access network"))
    monkeypatch.setattr("httpx.AsyncClient", network)
    queue, summary = build(inputs)
    job = queue["jobs"][1]
    assert inputs == original
    assert job["source_relative_path"] == "user/2.fast.mp4"
    assert job["expected_size"] == 900000
    assert job["previous_fast_play_file"] == "user/2.fast.mp4"
    assert job["previous_cover_file"] == "user/2.jpg" and job["cover_path"] is None
    assert queue["jobs"][0] == inputs["pilot"]["jobs"][0]
    assert summary["cover_outputs"] == 1
    network.assert_not_called()


@pytest.mark.parametrize(("options", "operation"), [
    ({"codec": "hvc1", "cover": False}, "transcode"),
    ({"width": 721, "height": 1280}, "transcode"),
    ({"width": 1281, "height": 720}, "transcode"),
    ({"size": 2000001}, "transcode"),
    ({"width": 720, "height": 1280, "size": 2000000}, None),
    ({"trailing": True}, "remux"),
    ({"cover": False}, "cover"),
    ({"cover": True}, None),
])
def test_operation_boundaries_preserve_eligible_candidates(options, operation):
    inputs = documents(work(**options))
    queue, summary = build(inputs)
    assert [job["operation"] for job in queue["jobs"][1:]] == ([operation] if operation else [])
    assert summary["browser_playback_verified"] is False
    if operation:
        job = queue["jobs"][1]
        source = {"path": job["source_paths"][0], "size": job["expected_size"],
                  "identity": "source-id", "content_identity": "source-content"}
        validate_job({**job, "source": source})
        assert bool(job["cover_path"]) is (not options.get("cover", True))


def test_positive_record_duration_only_estimates_bitrate_not_expected_duration():
    queue, summary = build(documents(work(duration=None, trailing=True)))
    assert queue["jobs"][1]["operation"] == "remux"
    assert queue["jobs"][1]["expected_duration"] is None
    assert summary["bitrate_basis_counts"] == {"record_video_duration_ms": 1}


@pytest.mark.parametrize("duration", [None, 0, -1000])
def test_unknown_bitrate_never_qualifies_as_lightweight(duration):
    queue, summary = build(documents(work(duration=None, recorded_duration=duration, trailing=True, cover=False)))
    assert len(queue["jobs"]) == 1
    assert summary["deferred_entries"][0]["reason"] == "h264_bitrate_unknown"


def recovered_inputs():
    inputs = documents(work(codec="hvc1", cover=False))
    recovered = {**inputs["probes"][1], "file_bytes": 4567,
                 "verified_remote_path": "/d2/vps/抖音视频/sec_sec-user/old  title.mp4"}
    inputs["recovered"] = [recovered]
    inputs["probes"][1] = {**inputs["probes"][1], "status": "error", "file_bytes": None}
    inputs["inventory"]["videos"][1]["classification"] = "source_lookup_failed"
    alias = {"user_id": "user", "aweme_id": "2", "original_relative_path": "user/2.fast.mp4",
             "verified_remote_path": recovered["verified_remote_path"],
             "source_revision": {"sec_user_id": "sec-user", "downloaded_at": "revision"}}
    return inputs, alias


def test_recovered_probes_override_failed_reads_and_keep_exact_d2_alias_first():
    inputs, alias = recovered_inputs()
    queue, summary = build(inputs, aliases=[alias])
    job = queue["jobs"][1]
    assert job["expected_size"] == 4567 and job["operation"] == "transcode"
    assert job["source_paths"][0] == "/vps/抖音视频/sec_sec-user/old  title.mp4"
    assert all(path.startswith("/vps/抖音视频/") for path in job["source_paths"])
    assert summary["recovered_probe_overrides"] == 1


def test_missing_installed_alias_is_explicitly_stale():
    inputs, _ = recovered_inputs()
    queue, summary = build(inputs)
    assert len(queue["jobs"]) == 1
    assert summary["stale_entries"][0]["reason"] == "recovered_alias_missing_or_changed"


@pytest.mark.parametrize(("field", "value", "reason"), [
    ("sec_user_id", "other-user", "source_identity_changed"),
    ("downloaded_at", "new-revision", "download_revision_changed"),
    ("fast_play_file", "user/2.mp4", "selected_source_changed"),
    ("files", ["user/2.mp4"], "source_path_removed"),
])
def test_current_source_changes_are_reported_without_rewriting_snapshot(field, value, reason):
    inputs = documents(work(codec="hvc1"))
    user = inputs["state"]["users"][1]
    target = user if field == "sec_user_id" else user["download_records"][0]
    target[field] = value
    snapshot = deepcopy(inputs["manifest"])
    queue, summary = build(inputs)
    assert len(queue["jobs"]) == 1 and inputs["manifest"] == snapshot
    assert summary["stale_entries"][0]["reason"] == reason


def test_removed_work_is_stale_and_new_downloads_stay_outside_fixed_inventory():
    inputs = documents(work(codec="hvc1"))
    inputs["state"]["users"][1]["download_records"] = [work("user", "99")["user"]["download_records"][0]]
    queue, summary = build(inputs)
    assert len(queue["jobs"]) == 1
    assert summary["stale_entries"][0]["reason"] == "source_record_missing"


def test_concurrently_added_cover_is_preserved():
    inputs = documents(work(codec="hvc1", cover=False))
    record = inputs["state"]["users"][1]["download_records"][0]
    record["files"].append("user/new-cover.jpg")
    record["cover_file"] = "user/new-cover.jpg"
    queue, _ = build(inputs)
    assert queue["jobs"][1]["cover_path"] is None
    assert queue["jobs"][1]["previous_cover_file"] == "user/new-cover.jpg"


def test_all_special_categories_stay_in_explicit_deferred_report():
    works = [work("user" + str(index), str(index + 2), codec="hvc1") for index in range(len(SPECIAL_CLASSES))]
    inputs = documents(*works)
    for row, category in zip(inputs["inventory"]["videos"][1:], sorted(SPECIAL_CLASSES)):
        row["classification"] = category
    queue, summary = build(inputs)
    assert len(queue["jobs"]) == 1
    assert {entry["reason"] for entry in summary["deferred_entries"]} == SPECIAL_CLASSES


def test_priority_and_output_are_deterministic_under_input_reordering():
    inputs = documents(work("cover", "2", cover=False), work("remux", "3", trailing=True),
                       work("large", "4", size=3000000), work("hevc", "5", codec="hvc1"),
                       work("wen", "6", nickname="文文的武器库", cover=False),
                       work("bao", "7", nickname="贴心的小饱饱", cover=False))
    result = build(inputs)
    assert [job["user_id"] for job in result[0]["jobs"]] == ["pilot", "bao", "wen", "hevc", "large", "remux", "cover"]
    reordered = deepcopy(inputs)
    for rows in (reordered["manifest"]["items"], reordered["inventory"]["videos"],
                 reordered["probes"], reordered["state"]["users"]):
        rows.reverse()
    assert build(reordered) == result


@pytest.mark.parametrize("field", ["fingerprint", "play_index", "relative_path"])
def test_recovered_evidence_must_match_exact_audit_revision(field):
    inputs, alias = recovered_inputs()
    inputs["recovered"][0][field] = "changed"
    with pytest.raises(ValueError, match="does not match snapshot"):
        build(inputs, aliases=[alias])


@pytest.mark.parametrize("change", ["duplicate", "missing"])
def test_ambiguous_or_incomplete_probe_sets_fail_explicitly(change):
    inputs = documents(work())
    inputs["probes"] = inputs["probes"] * 2 if change == "duplicate" else inputs["probes"][:1]
    with pytest.raises(ValueError, match="Duplicate|coverage differ"):
        build(inputs)
