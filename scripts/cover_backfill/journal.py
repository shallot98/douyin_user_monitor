"""Atomic JPEG publication, append-only receipts, and verified resume state."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from scripts.cloud_playback.media_tools import full_decode, probe_cover

from .models import SCHEMA_VERSION, BackfillError, cover_relative_path, json_bytes

JPEG_START = b"\xff\xd8"
JPEG_END = b"\xff\xd9"
IMAGE_FIELDS = ("bytes", "sha256", "width", "height")


def jpeg_bytes(path):
    data = path.read_bytes()
    if not data.startswith(JPEG_START) or not data.endswith(JPEG_END):
        raise BackfillError("Cover is not a complete JPEG")
    return data


def inspect_cover(path, *, timeout):
    data = jpeg_bytes(path)
    media = probe_cover(path, timeout=timeout)
    full_decode(path, expected_frames=1, timeout=timeout)
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "width": media["width"], "height": media["height"]}


def receipt_key(receipt):
    if not isinstance(receipt, dict) or receipt.get("schema_version") != SCHEMA_VERSION:
        raise BackfillError("Receipt has an unsupported schema")
    if receipt.get("status") not in ("ready", "failed"):
        raise BackfillError("Receipt has an invalid status")
    cover_relative_path(receipt.get("user_id"), receipt.get("aweme_id"))
    revision = receipt.get("input_revision")
    if not isinstance(revision, str) or len(revision) != hashlib.sha256().digest_size * 2:
        raise BackfillError("Receipt has an invalid input revision")
    return receipt["user_id"], receipt["aweme_id"], revision


def receipt_cover(root, receipt):
    expected = cover_relative_path(receipt.get("user_id"), receipt.get("aweme_id"))
    if receipt.get("cover_path") != expected:
        raise BackfillError("Receipt cover path does not match its identity")
    path = root / expected
    if not path.resolve().is_relative_to(root.resolve()):
        raise BackfillError("Receipt cover path escapes the work directory")
    return path


def verify_receipt(root, receipt, inspect):
    actual = inspect(receipt_cover(root, receipt))
    if any(actual[field] != receipt.get(field) for field in IMAGE_FIELDS):
        raise BackfillError("Cover no longer matches its ready receipt")


def verified_bytes(root, receipt):
    data = jpeg_bytes(receipt_cover(root, receipt))
    if len(data) != receipt.get("bytes") or hashlib.sha256(data).hexdigest() != receipt.get("sha256"):
        raise BackfillError("Cover bytes no longer match their ready receipt")
    if any(type(receipt.get(field)) is not int or receipt[field] <= 0 for field in ("width", "height")):
        raise BackfillError("Ready receipt has invalid image dimensions")
    return data


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_cover(temporary, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    with temporary.open("rb") as stream:
        os.fsync(stream.fileno())
    os.replace(temporary, target)
    sync_directory(target.parent)


def atomic_json(path, value):
    with tempfile.NamedTemporaryFile(prefix=".progress-", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(json_bytes(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
            sync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)


@contextmanager
def producer_lock(root):
    with (root / ".producer.lock").open("a") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackfillError("Another cover worker owns this work directory") from None
        yield


def read_receipts(path, *, offset=0, strict_tail=False):
    if not path.exists() and offset == 0:
        return
    with path.open("rb") as stream:
        _seek_receipt(stream, offset)
        for line in stream:
            if not _complete_line(line, strict_tail):
                return
            try:
                receipt = json.loads(line)
            except (ValueError, UnicodeError):
                raise BackfillError("Receipt journal contains invalid JSON") from None
            receipt_key(receipt)
            yield stream.tell(), receipt


def _complete_line(line, strict_tail):
    if line.endswith(b"\n"):
        return True
    if strict_tail:
        raise BackfillError("Receipt journal ends with an incomplete line; preserve and repair it before resuming")
    return False


def _seek_receipt(stream, offset):
    if type(offset) is not int or offset < 0 or offset > os.fstat(stream.fileno()).st_size:
        raise BackfillError("Receipt cursor is outside the journal")
    if offset:
        stream.seek(offset - 1)
        if stream.read(1) != b"\n":
            raise BackfillError("Receipt cursor must follow a complete newline")
    stream.seek(offset)


def append_receipt(path, receipt):
    receipt_key(receipt)
    with path.open("ab") as stream:
        stream.write(json_bytes(receipt) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
        return stream.tell()
