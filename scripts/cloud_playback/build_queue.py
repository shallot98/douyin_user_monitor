"""Build a deterministic, offline D2 repair queue from reviewed inventory evidence."""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from copy import deepcopy
from pathlib import Path

from douyin_user_monitor.monitor.archive_feed import build_archive_item, find_record, list_archive_files
from douyin_user_monitor.monitor.archive_path_aliases import ARCHIVE_PATH_ALIASES_FILENAME, ArchivePathAliasError
from douyin_user_monitor.monitor.playback_media import (
    FAST_PLAY_MAX_BIT_RATE,
    FAST_PLAY_MAX_LONG_SIDE,
    FAST_PLAY_MAX_SHORT_SIDE,
)

from .media_remote import PLAYBACK_ROOT
from .run_queue import write_json

D2_MOUNT = "/d2"
BITS_PER_BYTE = 8
MILLISECONDS_PER_SECOND = 1000
SPECIAL_CLASSES = frozenset(("empty_file", "audio_only", "encrypted_media", "truncated_container"))
HEVC = frozenset(("hvc1", "hev1"))
H264 = frozenset(("avc1", "avc3"))
PRIORITY_USERS = ("贴心的小饱饱", "文文的武器库")
OPERATION_ORDER = {"transcode": 0, "remux": 1, "cover": 2}


class StaleSource(ValueError):
    """Current records no longer identify the reviewed source."""


def work_key(row):
    return row["user_id"], str(row["aweme_id"])


def _unique(rows):
    indexed = {work_key(row): row for row in rows}
    if len(indexed) != len(rows):
        raise ValueError("Duplicate user/work in queue evidence")
    return indexed


def _evidence(documents):
    snapshots = _unique(documents["manifest"]["items"])
    inventory = _unique(documents["inventory"]["videos"])
    probes = _unique(documents["probes"])
    recovered = _unique(documents["recovered"])
    if not (snapshots.keys() == inventory.keys() == probes.keys()) or not recovered.keys() <= snapshots.keys():
        raise ValueError("Inventory, manifest and probe coverage differ")
    for rows, path_field in ((inventory, "current_play_file"), (probes, "relative_path"), (recovered, "relative_path")):
        for key, row in rows.items():
            snapshot = snapshots[key]
            matches = all(row[field] == snapshot[field] for field in ("fingerprint", "play_index"))
            if not matches or row.get(path_field, snapshot["relative_path"]) != snapshot["relative_path"]:
                raise ValueError(f"Evidence does not match snapshot: {key}")
    return snapshots, inventory, {**probes, **recovered}


def _current_source(snapshot, user, *, source_path=None):
    if user is None:
        raise StaleSource("user_missing")
    if user.get("sec_user_id") != snapshot["sec_user_id"]:
        raise StaleSource("source_identity_changed")
    record = find_record(user, snapshot["aweme_id"])
    if record is None:
        raise StaleSource("source_record_missing")
    if record.get("downloaded_at") != snapshot.get("downloaded_at"):
        raise StaleSource("download_revision_changed")
    files = list_archive_files(user, snapshot["aweme_id"])
    if (source_path or snapshot["relative_path"]) not in files:
        raise StaleSource("source_path_removed")
    current = build_archive_item(record)
    index = current["play_index"]
    if source_path is None and (index is None or files[index] != snapshot["relative_path"]):
        raise StaleSource("selected_source_changed")
    return record, current


def _positive(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value > 0 else None


def _bit_rate(probe, record):
    duration = _positive(probe.get("duration_seconds"))
    basis = "probe_duration"
    if duration is None:
        duration_ms = _positive(record.get("video_duration_ms"))
        duration = duration_ms / MILLISECONDS_PER_SECOND if duration_ms else None
        basis = "record_video_duration_ms"
    return (probe["file_bytes"] * BITS_PER_BYTE / duration, basis) if duration else (None, "unknown")


def _dimensions_action(probe):
    videos = [stream for stream in probe["streams"] if stream["type"] == "vide"]
    dimensions = [(stream.get("width", 0), stream.get("height", 0)) for stream in videos]
    if any(min(width, height) <= 0 for width, height in dimensions):
        return "deferred", "h264_dimensions_unknown"
    if any(min(w, h) > FAST_PLAY_MAX_SHORT_SIDE or max(w, h) > FAST_PLAY_MAX_LONG_SIDE for w, h in dimensions):
        return "transcode", "h264_dimensions_exceed_limits"
    return None


def _index_action(probe, basis):
    moov = next((atom["offset"] for atom in probe["atoms"] if atom["type"] == "moov"), None)
    if moov is None:
        return "deferred", "index_position_unknown", basis
    trailing = any(atom["type"] == "mdat" and atom["offset"] < moov for atom in probe["atoms"])
    return ("remux", "h264_trailing_moov", basis) if trailing else ("unchanged", "h264_candidate", basis)


def _h264_action(probe, record):
    dimensions = _dimensions_action(probe)
    if dimensions:
        return *dimensions, None
    bit_rate, basis = _bit_rate(probe, record)
    if bit_rate is None:
        return "deferred", "h264_bitrate_unknown", basis
    if bit_rate > FAST_PLAY_MAX_BIT_RATE:
        return "transcode", "h264_bitrate_exceeds_limit", basis
    return _index_action(probe, basis)


def _operation(probe, record, missing_cover):
    if probe["status"] != "ok" or not _positive(probe.get("file_bytes")):
        return "deferred", "probe_unresolved", None
    codecs = {stream["codec"] for stream in probe["streams"] if stream["type"] == "vide"}
    if codecs & HEVC:
        return "transcode", "hevc_browser_incompatible", None
    if not codecs or not codecs <= H264:
        return "deferred", "video_codec_requires_review", None
    operation, reason, basis = _h264_action(probe, record)
    if operation == "unchanged" and missing_cover:
        return "cover", "missing_cover_reference", basis
    return operation, reason, basis


def _native_paths(snapshot, probe, user, *, remote_paths_for, alias_paths_for):
    aliases = alias_paths_for(user, snapshot["aweme_id"], snapshot["relative_path"])
    if probe.get("verified_remote_path") and probe["verified_remote_path"] not in aliases:
        raise StaleSource("recovered_alias_missing_or_changed")
    paths = remote_paths_for(snapshot["relative_path"], snapshot["sec_user_id"], verified_paths=aliases)
    native = [path.removeprefix(D2_MOUNT) for path in paths if path.startswith(D2_MOUNT + "/")]
    if not native:
        raise ValueError("Archive configuration produced no D2 source paths")
    return native


def _entry(snapshot, evidence, *, remote_paths_for, alias_paths_for):
    inventory, probe, user = evidence
    brief = {name: snapshot[name] for name in ("user_id", "aweme_id", "nickname")}
    if inventory["classification"] in SPECIAL_CLASSES:
        return {**brief, "status": "deferred", "reason": inventory["classification"]}
    try:
        record, current = _current_source(snapshot, user)
        missing_cover = current["cover_index"] is None
        operation, reason, basis = _operation(probe, record, missing_cover)
        entry = {**brief, "status": operation, "reason": reason, "bitrate_basis": basis}
        if operation in ("deferred", "unchanged"):
            return entry
        paths = _native_paths(snapshot, probe, user, remote_paths_for=remote_paths_for, alias_paths_for=alias_paths_for)
    except (StaleSource, ArchivePathAliasError) as exc:
        return {**brief, "status": "stale", "reason": str(exc)}
    prefix = f"{PLAYBACK_ROOT}/{snapshot['user_id']}/{snapshot['aweme_id']}"
    job = {**brief, **{name: snapshot[name] for name in ("sec_user_id", "downloaded_at", "fingerprint")},
           "source_relative_path": snapshot["relative_path"], "source_paths": paths,
           "expected_size": probe["file_bytes"], "expected_duration": _positive(probe.get("duration_seconds")),
           "operation": operation, "playback_path": None if operation == "cover" else prefix + ".h264.mp4",
           "cover_path": prefix + ".cover.jpg" if missing_cover else None,
           "previous_fast_play_file": record.get("fast_play_file") or None,
           "previous_cover_file": record.get("cover_file") or None}
    return {**entry, "status": "queued", "job": job}


def _priority(entry):
    name = entry["nickname"]
    user_rank = PRIORITY_USERS.index(name) if name in PRIORITY_USERS else len(PRIORITY_USERS)
    return (user_rank, entry["reason"] != "hevc_browser_incompatible",
            OPERATION_ORDER[entry["job"]["operation"]], *work_key(entry))


def _pilot(document, snapshots, users):
    if len(document["jobs"]) != 1:
        raise ValueError("Exactly one reviewed pilot job is required")
    job = document["jobs"][0]
    snapshot = snapshots[work_key(job)]
    if any(job[name] != snapshot[name] for name in ("sec_user_id", "downloaded_at", "fingerprint")):
        raise ValueError("Pilot does not match snapshot source revision")
    if job["source_relative_path"] not in snapshot["files"]:
        raise ValueError("Pilot source is not one of the audited work's original references")
    _current_source(snapshot, users.get(job["user_id"]), source_path=job["source_relative_path"])
    return deepcopy(job)


def build_queue(documents, *, remote_paths_for, alias_paths_for):
    """Return queue and dispositions without changing inputs, state or cloud objects."""
    snapshots, inventory, probes = _evidence(documents)
    users = {user["id"]: user for user in documents["state"]["users"]}
    pilot = _pilot(documents["pilot"], snapshots, users)
    entries = [_entry(snapshot, (inventory[key], probes[key], users.get(key[0])),
                      remote_paths_for=remote_paths_for, alias_paths_for=alias_paths_for)
               for key, snapshot in sorted(snapshots.items()) if key != work_key(pilot)]
    queued = sorted((entry for entry in entries if entry["status"] == "queued"), key=_priority)
    jobs = [pilot, *(entry["job"] for entry in queued)]
    bitrate_bases = Counter(entry["bitrate_basis"] for entry in entries if entry.get("bitrate_basis"))
    summary = {
        "snapshot_at": documents["manifest"]["at"], "inventory_records": len(snapshots),
        "recovered_probe_overrides": len(documents["recovered"]), "jobs": len(jobs),
        "operation_counts": dict(sorted(Counter(job["operation"] for job in jobs).items())),
        "queued_reason_counts": dict(sorted(Counter(entry["reason"] for entry in queued).items())),
        "cover_outputs": sum(bool(job.get("cover_path")) for job in jobs),
        "source_size_bytes": sum(job["expected_size"] for job in jobs),
        "users_with_jobs": len({job["user_id"] for job in jobs}),
        "bitrate_basis_counts": dict(sorted(bitrate_bases.items())),
        "limits": {"short_side": FAST_PLAY_MAX_SHORT_SIDE, "long_side": FAST_PLAY_MAX_LONG_SIDE,
                   "bit_rate": FAST_PLAY_MAX_BIT_RATE},
        "browser_playback_verified": False,
        "assumptions": [
            "Existing H.264 candidates are not all browser-verified; "
            "remote workers must verify actual media and full decode.",
            "Bitrate uses probe bytes and positive probe duration, or positive recorded video_duration_ms; "
            "unknown stays deferred.",
            "expected_duration is the positive probe duration or null, never the metadata duration estimate.",
            "Existing cover references are preserved except the exact reviewed pilot; "
            "missing references request a cover.",
            "D2 candidates use installed exact aliases; "
            "this builder performs no media, network or production-state writes.",
        ],
    }
    for status in ("deferred", "stale", "unchanged"):
        selected = [entry for entry in entries if entry["status"] == status]
        summary[status + "_count"] = len(selected)
        summary[status + "_entries"] = selected
    return {"jobs": jobs}, summary


def main(argv=None):
    from douyin_user_monitor.monitor.archive_path_aliases import ArchivePathAliases
    from douyin_user_monitor.monitor.openlist_client import OpenListClient
    from douyin_user_monitor.settings_loaders import load_settings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory-dir", required=True, type=Path)
    parser.add_argument("--repair-dir", required=True, type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--aliases", type=Path)
    args = parser.parse_args(argv)
    settings = load_settings()
    state_path = args.state or settings.monitor.state_path
    aliases = ArchivePathAliases.from_file(args.aliases or state_path.with_name(ARCHIVE_PATH_ALIASES_FILENAME))
    client = OpenListClient(settings.archive)
    documents = {name: json.loads(path.read_text(encoding="utf-8")) for name, path in (
        ("manifest", args.inventory_dir / "manifest.json"), ("inventory", args.inventory_dir / "video-inventory.json"),
        ("state", state_path), ("pilot", args.repair_dir / "pilot-queue.json"))}
    for name, path in (("probes", args.inventory_dir / "results.jsonl"),
                       ("recovered", args.repair_dir / "recovered-paths-results.jsonl")):
        documents[name] = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    queue, summary = build_queue(documents, remote_paths_for=client.remote_paths_for, alias_paths_for=aliases.paths_for)
    write_json(args.repair_dir / "full-queue.json", queue)
    write_json(args.repair_dir / "queue-summary.json", summary)
    print(json.dumps({k: v for k, v in summary.items() if not k.endswith("_entries")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
