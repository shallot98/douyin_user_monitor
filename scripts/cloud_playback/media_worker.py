"""Repair one job on an independent host; execution on project host hdz is forbidden.

Only derivatives use this host's temporary workdir. Sources are read from native
D2 HTTPS addresses. Existing cloud outputs are never adopted or overwritten.
"""

from __future__ import annotations

import socket
import tempfile
from pathlib import Path

from .d2_upload import upload_file
from .media_remote import assert_source, destinations_missing, direct_url, get_source, validate_job
from .media_tools import (
    MediaJobError,
    assert_duration,
    assert_remux_source,
    full_decode,
    prepare_cover,
    prepare_video,
    probe_cover,
    probe_video,
    validate_playback,
)

PROJECT_HOSTNAME = "hdz"
REMOTE_MATCH_FIELDS = ("width", "height", "duration", "video_codec", "pixel_format", "audio_codecs", "declared_frames")


def _work_directory(workdir):
    if socket.gethostname().split(".", 1)[0] == PROJECT_HOSTNAME:
        raise MediaJobError("Historical media repair is forbidden on project host hdz; use the independent compute host")
    root = Path(workdir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _prepare_outputs(job, address, source, directory):
    expected = job.get("expected_duration") or source["duration"]
    assert_duration(source["duration"], expected)
    playback = None
    cover = None
    if job["operation"] != "cover":
        if job["operation"] == "remux":
            assert_remux_source(source)
        path = directory / "playback.h264.mp4"
        prepare_video(address, path, job["operation"])
        media = probe_video(path, local=True)
        validate_playback(media, source, expected_duration=expected, operation=job["operation"])
        decoded = full_decode(path, expected_frames=media["declared_frames"], expected_duration=expected)
        playback = {"path": path, "media": {**media, **decoded}}
    if job.get("cover_path"):
        path = directory / "cover.jpg"
        prepare_cover(playback["path"] if playback else address, path)
        media = probe_cover(path)
        decoded = full_decode(path, expected_frames=1)
        cover = {"path": path, "media": {**media, **decoded}}
    return playback, cover


def _upload_verified(client, prepared, destination, *, cover=False, uploader=None):
    selected_uploader = upload_file if uploader is None else uploader
    receipt = selected_uploader(client, prepared["path"], destination)
    remote = get_source(client, destination)
    assert_source(remote, receipt)
    address = direct_url(client, remote)
    media = probe_cover(address) if cover else probe_video(address)
    fields = ("width", "height", "video_codec") if cover else REMOTE_MATCH_FIELDS
    if any(media[field] != prepared["media"][field] for field in fields):
        raise MediaJobError("Uploaded media differs from the locally verified derivative")
    decoded = full_decode(
        address, expected_frames=1 if cover else media["declared_frames"],
        expected_duration=None if cover else media["duration"],
    )
    return {"receipt": receipt, "media": {**prepared["media"], **decoded}, "uploaded_media_verified": True}


def process_job(client, job, workdir, *, uploader=None):
    """Return source snapshots and verified receipts; failures never publish state.

    job: source{path,identity,content_identity,size}, user_id, aweme_id,
    operation(transcode/remux/cover), expected_duration(number/null),
    playback_path and optional cover_path in the _web_playback namespace.
    uploader(client, local_path, destination) defaults to direct D2 upload.
    """
    expected_source = validate_job(job)
    root = _work_directory(workdir)
    before = get_source(client, expected_source["path"])
    assert_source(before, expected_source)
    destinations_missing(client, job, create_parents=True)
    address = direct_url(client, before)
    source_media = probe_video(address)
    with tempfile.TemporaryDirectory(prefix=f"{job['aweme_id']}-", dir=root) as temporary:
        playback, cover = _prepare_outputs(job, address, source_media, Path(temporary))
        assert_source(get_source(client, before["path"]), before)
        destinations_missing(client, job)
        playback_result = _upload_verified(client, playback, job["playback_path"], uploader=uploader) if playback else None
        cover_result = _upload_verified(client, cover, job["cover_path"], cover=True, uploader=uploader) if cover else None
        after = get_source(client, before["path"])
        assert_source(after, before)
    return {
        "operation": job["operation"], "user_id": job["user_id"], "aweme_id": job["aweme_id"],
        "source_before": before, "source_after": after, "source_media": source_media,
        "playback": playback_result, "cover": cover_result,
    }
