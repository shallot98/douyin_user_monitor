"""Native D2 metadata and destination checks for isolated media repair jobs."""

from __future__ import annotations

import math
import re
import time
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from .d2_client import D2HTTPError
from .media_tools import MediaJobError

ARCHIVE_ROOT = "/vps/抖音视频"
PLAYBACK_ROOT = ARCHIVE_ROOT + "/_web_playback"
GET_ENDPOINT = "/v6/userfile/get"
CREATE_ENDPOINT = "/v6/userfile/create"
DOWNLOAD_ENDPOINT = "/v6/userfile/get_direct_download_address"
SOURCE_FIELDS = ("path", "identity", "content_identity", "size")


def _positive_size(value):
    try:
        size = int(value)
    except (ValueError, TypeError):
        raise MediaJobError("D2 file size must be a positive integer") from None
    if isinstance(value, bool) or size <= 0 or str(size) != str(value):
        raise MediaJobError("D2 file size must be a positive integer")
    return size


def source_record(data):
    if not isinstance(data, dict):
        raise MediaJobError("D2 metadata must be an object")
    for field in ("path", "identity", "content_identity"):
        if not isinstance(data.get(field), str) or not data[field]:
            raise MediaJobError(f"D2 metadata is missing {field}")
    return {**{field: data[field] for field in SOURCE_FIELDS if field != "size"},
            "size": _positive_size(data.get("size")), "update_ts": data.get("update_ts")}


def get_source(client, path):
    record = source_record(client.post(GET_ENDPOINT, {"path": path}))
    if record["path"] != path:
        raise MediaJobError("D2 returned a different source path")
    return record


def assert_source(actual, expected):
    if any(actual[field] != expected[field] for field in SOURCE_FIELDS):
        raise MediaJobError("D2 source identity, content identity, path or size changed")
    if expected.get("update_ts") is not None and actual["update_ts"] != expected["update_ts"]:
        raise MediaJobError("D2 source update timestamp changed")


def validate_job(job):
    if job.get("operation") not in ("transcode", "remux", "cover"):
        raise MediaJobError("Unknown media operation")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", job.get("user_id", "")):
        raise MediaJobError("Invalid user_id")
    if not re.fullmatch(r"[0-9]+", job.get("aweme_id", "")):
        raise MediaJobError("Invalid aweme_id")
    source = source_record(job["source"])
    path = source["path"]
    if not path.startswith(ARCHIVE_ROOT + "/") or str(PurePosixPath(path)) != path or ".." in path.split("/"):
        raise MediaJobError("Source must be a canonical native D2 archive path")
    prefix = f"{PLAYBACK_ROOT}/{job['user_id']}/{job['aweme_id']}"
    expected_paths = {"playback_path": prefix + ".h264.mp4", "cover_path": prefix + ".cover.jpg"}
    for field, expected in expected_paths.items():
        if job.get(field) is not None and job[field] != expected:
            raise MediaJobError(f"{field} must use the independent playback namespace")
        if job.get(field) == path:
            raise MediaJobError("Output must never replace the source")
    required = "cover_path" if job["operation"] == "cover" else "playback_path"
    if not job.get(required):
        raise MediaJobError(f"Operation requires {required}")
    expected = job.get("expected_duration")
    if expected is not None and (not isinstance(expected, (int, float)) or isinstance(expected, bool)
                                 or not math.isfinite(expected) or expected <= 0):
        raise MediaJobError("expected_duration must be positive or null")
    return source


def direct_url(client, source):
    data = client.post(DOWNLOAD_ENDPOINT, {"path": source["path"]})
    if (not isinstance(data, dict) or data.get("path") != source["path"]
            or data.get("content_identity") != source["content_identity"]
            or _positive_size(data.get("file_size")) != source["size"]):
        raise MediaJobError("D2 download metadata differs from the verified source")
    address = data.get("download_address")
    try:
        parsed = urlsplit(address)
        expires = int(data["expire_at"])
    except (TypeError, ValueError, KeyError):
        raise MediaJobError("D2 returned invalid private download metadata") from None
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or expires <= time.time():
        raise MediaJobError("D2 returned an invalid or expired HTTPS download address")
    return address


def _lookup(client, path):
    try:
        metadata = client.post(GET_ENDPOINT, {"path": path})
    except D2HTTPError as error:
        if error.status == 404 and error.code == 5:
            return None
        raise
    if not isinstance(metadata, dict) or metadata.get("path") != path:
        raise MediaJobError("D2 returned invalid object metadata")
    return metadata


def _ensure_directory(client, parent, name):
    path = f"{parent}/{name}"
    metadata = _lookup(client, path)
    if metadata is None:
        client.post(CREATE_ENDPOINT, {"path": parent, "name": name})
        metadata = _lookup(client, path)
    if not isinstance(metadata, dict) or metadata.get("path") != path or metadata.get("dir") is not True:
        raise MediaJobError("D2 playback directory verification failed")
    return path


def destinations_missing(client, job, *, create_parents=False):
    if create_parents:
        _ensure_directory(client, ARCHIVE_ROOT, "_web_playback")
        _ensure_directory(client, PLAYBACK_ROOT, job["user_id"])
    targets = [job["cover_path"]] if job["operation"] == "cover" else [job["playback_path"], job.get("cover_path")]
    for target in targets:
        if target and _lookup(client, target) is not None:
            raise MediaJobError("D2 playback or cover destination already exists; overwrite refused")
