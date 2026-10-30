"""Check current cloud objects before publishing a remote worker receipt."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

from .media_remote import ARCHIVE_ROOT, assert_source, get_source, source_record
from .publish import PublicationError


def _artifact(client, result, expected_path):
    if result.get("uploaded_media_verified") is not True or not result["media"].get("full_decode_verified"):
        raise PublicationError("Worker did not verify the complete uploaded media")
    receipt = result["receipt"]
    if receipt["path"] != expected_path or not expected_path.startswith(ARCHIVE_ROOT + "/"):
        raise PublicationError("Uploaded artifact path differs from the repair job")
    assert_source(get_source(client, expected_path), receipt)
    return expected_path.removeprefix(ARCHIVE_ROOT + "/")


def verified_publication(client, receipt, *, node):
    if receipt["status"] != "succeeded":
        raise PublicationError("Failed jobs cannot change playback references")
    job, result = receipt["job"], receipt["result"]
    if (job.get("kind") == "source_recovery") != bool(result.get("original")):
        raise PublicationError("Recovered original requires an explicit source-recovery job")
    if any(result[field] != job[field] for field in ("user_id", "aweme_id", "operation")):
        raise PublicationError("Worker result belongs to a different job")
    source = source_record(job["source"])
    assert_source(result["source_before"], source)
    if source["path"] not in job["source_paths"] or source["size"] != job["expected_size"]:
        raise PublicationError("Worker source differs from the audited queue")
    assert_source(result["source_after"], result["source_before"])
    assert_source(get_source(client, result["source_before"]["path"]), result["source_before"])
    publication = {key: deepcopy(job[key]) for key in (
        "user_id", "aweme_id", "sec_user_id", "downloaded_at", "source_relative_path",
        "previous_fast_play_file", "previous_cover_file", "operation",
    )}
    publication.update(remote_verified=True, verified_at=datetime.now(timezone.utc).isoformat(),
                       playback_file=None, cover_file=None)
    evidence = {"node": node, "source": deepcopy(result["source_before"]),
                "source_unchanged": True, "worker_finished_at": receipt["finished_at"]}
    for kind, field in (("playback", "playback_file"), ("cover", "cover_file")):
        artifact = result[kind]
        if artifact is None:
            continue
        publication[field] = _artifact(client, artifact, job[kind + "_path"])
        evidence[kind] = deepcopy(artifact["receipt"])
        if kind == "playback":
            publication["playback_media"] = deepcopy(artifact["media"])
    publication["evidence"] = evidence
    if result.get("original"):
        publication.update(_recovered_original(client, job, result["original"]))
        publication["evidence"] = {**evidence, "original": deepcopy(result["original"]["receipt"]),
                                   "recovery_source": deepcopy(job["recovery_source"])}
    return publication


def _recovered_original(client, job, artifact):
    receipt = artifact["receipt"]
    if receipt.get("content_verified") is not True or receipt.get("sha256") != job["original_sha256"]:
        raise PublicationError("Recovered original bytes were not verified against the upstream source")
    assert_source(receipt, source_record(job["source"]))
    return {"original_file": _artifact(client, artifact, job["original_path"]),
            "original_media": deepcopy(artifact["media"]), "previous_video_file": job["previous_video_file"]}
