"""Preserve verified upstream source bytes in D2 before generating playback."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import http.client
import json
import re
import socket
import urllib.error
import urllib.request
from functools import partial
from pathlib import Path

from .d2_client import REQUEST_TIMEOUT_SECONDS, Credentials, D2Client, HttpTransport, _NoRedirect
from .d2_upload import upload_file
from .media_remote import ARCHIVE_ROOT, _ensure_directory, _lookup, assert_source, direct_url, get_source
from .media_tools import MediaJobError, full_decode, probe_video
from .media_worker import REMOTE_MATCH_FIELDS, process_job
from .quota import DEFAULT_POLL_SECONDS, UploadWithQuota, wait_for_upload_capacity
from .run_queue import _upload_progress, execute_queue

ORIGINAL_ROOT = ARCHIVE_ROOT + "/_recovered_originals"
HASH_BUFFER_BYTES = 65536


def _digest(stream):
    digest = hashlib.sha256()
    size = 0
    while block := stream.read(HASH_BUFFER_BYTES):
        size += len(block)
        digest.update(block)
    return size, digest.hexdigest()


def checked_local_source(job, source_root):
    if socket.gethostname().split(".", 1)[0] == "hdz":
        raise MediaJobError("Original recovery is forbidden on project host hdz")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", job["user_id"]) or not job["aweme_id"].isdecimal():
        raise MediaJobError("Invalid recovery user/work identity")
    expected_path = f"{ORIGINAL_ROOT}/{job['user_id']}/{job['aweme_id']}.source.mp4"
    if job["original_path"] != expected_path or job["source_paths"] != [expected_path]:
        raise MediaJobError("Recovered original must use its independent D2 namespace")
    path = Path(job["original_local_file"]).resolve()
    if not path.is_relative_to(source_root.resolve()) or not job["original_media"].get("full_decode_verified"):
        raise MediaJobError("Original is outside the audited compute-node source directory or was not decoded")
    with path.open("rb") as source:
        size, digest = _digest(source)
    if size != job["expected_size"] or digest != job["original_sha256"]:
        raise MediaJobError("Retained original no longer matches the verified upstream bytes")
    return path


def verify_original_bytes(address, job):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    request = urllib.request.Request(address, headers={"Accept-Encoding": "identity"})
    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise MediaJobError(f"Recovered original verification returned HTTP {response.status}")
            size, digest = _digest(response)
    except urllib.error.HTTPError as error:
        raise MediaJobError(f"Recovered original verification returned HTTP {error.code}") from None
    except (OSError, http.client.HTTPException) as error:
        raise MediaJobError(f"Recovered original verification failed ({type(error).__name__})") from None
    if size != job["expected_size"] or digest != job["original_sha256"]:
        raise MediaJobError("D2 recovered original bytes differ from the verified upstream source")
    return digest


def verify_original_media(address, job):
    media = probe_video(address)
    expected = job["original_media"]
    for field in REMOTE_MATCH_FIELDS:
        actual, wanted = media[field], expected[field]
        if field == "audio_codecs":
            actual, wanted = tuple(actual), tuple(wanted)
        if actual != wanted:
            raise MediaJobError(f"Recovered original media differs in {field}")
    decoded = full_decode(address, expected_frames=media["declared_frames"], expected_duration=media["duration"])
    return {**expected, **decoded}


def prepare_source(client, job, *, source_root, uploader):
    path = checked_local_source(job, source_root)
    _ensure_directory(client, ARCHIVE_ROOT, "_recovered_originals")
    _ensure_directory(client, ORIGINAL_ROOT, job["user_id"])
    existing = _lookup(client, job["original_path"])
    upload = {"status": "existing_original_requires_content_verification"}
    if existing is None:
        upload = uploader(client, path, job["original_path"])
    cloud = get_source(client, job["original_path"])
    if existing is None:
        assert_source(cloud, upload)
    address = direct_url(client, cloud)
    digest = verify_original_bytes(address, job)
    media = verify_original_media(address, job)
    assert_source(get_source(client, cloud["path"]), cloud)
    if existing is not None:
        upload = {"status": "existing_original_content_verified"}
    artifact = {"receipt": {**upload, **cloud, "sha256": digest, "content_verified": True},
                "media": media, "uploaded_media_verified": True}
    return {**job, "source": cloud, "original_artifact": artifact}


def process_recovered_job(client, job, workdir, *, uploader):
    result = process_job(client, job, workdir, uploader=uploader)
    return {**result, "original": job["original_artifact"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", required=True, type=Path)
    parser.add_argument("--queue", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    args = parser.parse_args(argv)
    credentials = Credentials.from_mapping(json.loads(args.credentials.read_text()))
    client = D2Client(credentials, transport=HttpTransport())
    uploader = UploadWithQuota(upload=upload_file, report=partial(_upload_progress, args.workspace))
    resolve = partial(prepare_source, source_root=args.source_root, uploader=uploader)
    process = partial(process_recovered_job, uploader=uploader)
    before = partial(wait_for_upload_capacity, client, poll_seconds=DEFAULT_POLL_SECONDS)
    jobs = json.loads(args.queue.read_text())["jobs"]
    args.workspace.mkdir(parents=True, exist_ok=True)
    with (args.workspace / "worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        summary = execute_queue(client, jobs, workspace=args.workspace, process=process, resolve=resolve, before_job=before)
    print(json.dumps(summary, ensure_ascii=False))
    return int(bool(summary["failed"]))


if __name__ == "__main__":
    raise SystemExit(main())
