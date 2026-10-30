"""Validate a cover-only tar and extract verified JPEGs into a temporary directory."""

from __future__ import annotations

import hashlib
import json
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from PIL import Image

from .journal import receipt_key
from .models import IDENTITY_FIELDS, SCHEMA_VERSION, BackfillError, parse_job

MANIFEST_NAMES = {"batch.json", "receipts.jsonl", "failures.jsonl"}
COPY_BLOCK_BYTES = 1024 * 1024
TAR_END_BLOCKS = 2
COUNT_FIELDS = ("after_offset", "next_offset", "scanned_receipts", "failed_receipts",
                "ready_receipts", "cover_count", "cover_bytes")


@dataclass(frozen=True)
class ReadyCover:
    receipt: dict
    source: Path


@dataclass(frozen=True)
class CoverBatch:
    metadata: dict
    ready: tuple[ReadyCover, ...]
    failures: tuple[dict, ...]


def file_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(COPY_BLOCK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def _tar_members(archive):
    members = {}
    for member in archive:
        path = PurePosixPath(member.name)
        if (not member.isfile() or member.sparse is not None or path.is_absolute()
                or ".." in path.parts or str(path) != member.name):
            raise BackfillError("Batch contains a nonregular or unsafe tar member")
        if member.name in members:
            raise BackfillError("Batch contains duplicate tar member names")
        members[member.name] = member
    if not MANIFEST_NAMES.issubset(members):
        raise BackfillError("Batch is missing its metadata, ready receipts, or failure receipts")
    return members


def _read_bytes(archive, member):
    with archive.extractfile(member) as stream:
        data = stream.read()
    if len(data) != member.size:
        raise BackfillError("Tar member is truncated")
    return data


def _read_receipts(data, status):
    receipts = []
    for line in data.splitlines():
        receipt = json.loads(line)
        receipt_key(receipt)
        if receipt["status"] != status:
            raise BackfillError("Receipt is in the wrong batch manifest")
        fields = {key: receipt.get(key) for key in (*IDENTITY_FIELDS, "source_paths", "nickname")}
        job = parse_job(fields)
        if receipt["input_revision"] != job.input_revision:
            raise BackfillError("Receipt input revision does not match its source identity")
        if status == "ready":
            _validate_ready_receipt(receipt, job)
        elif not isinstance(receipt.get("error"), dict) or not receipt["error"]:
            raise BackfillError("Failed receipt does not include its failure reason")
        receipts.append(receipt)
    return tuple(receipts)


def _validate_ready_receipt(receipt, job):
    if receipt.get("cover_path") != job.cover_path:
        raise BackfillError("Ready receipt image path does not match its identity")
    if receipt.get("source_verified_after") is not True:
        raise BackfillError("Ready receipt lacks source verification")
    source = receipt.get("source")
    if not isinstance(source, dict) or source.get("path") not in job.source_paths:
        raise BackfillError("Ready receipt source is outside its declared candidates")
    for key in ("bytes", "width", "height"):
        if type(receipt.get(key)) is not int or receipt[key] <= 0:
            raise BackfillError("Ready receipt has invalid image size or dimensions")


def _validate_batch(metadata, ready, failures):
    if not isinstance(metadata, dict) or metadata.get("schema_version") != SCHEMA_VERSION:
        raise BackfillError("Batch has an unsupported schema")
    if any(type(metadata.get(key)) is not int or metadata[key] < 0 for key in COUNT_FIELDS):
        raise BackfillError("Batch contains invalid counts or cursor offsets")
    if (metadata["ready_receipts"] != len(ready) or metadata["failed_receipts"] != len(failures)
            or metadata["scanned_receipts"] != len(ready) + len(failures)):
        raise BackfillError("Batch receipt counts do not match its manifests")
    after, next_offset = metadata["after_offset"], metadata["next_offset"]
    if next_offset < after or bool(next_offset > after) != bool(metadata["scanned_receipts"]):
        raise BackfillError("Batch cursor does not match its receipt progress")


def _verify_jpeg(path, receipt):
    if path.stat().st_size != receipt["bytes"] or file_digest(path) != receipt.get("sha256"):
        raise BackfillError(f"JPEG hash or byte count does not match its receipt: {receipt['cover_path']}")
    with Image.open(path) as image:
        if image.format != "JPEG" or image.size != (receipt["width"], receipt["height"]):
            raise BackfillError("Image format or dimensions do not match the JPEG receipt")
        image.verify()
    with Image.open(path) as image:
        image.load()


def _extract_images(archive, members, receipts, root):
    images = {}
    for receipt in receipts:
        name = receipt["cover_path"]
        if name not in members:
            raise BackfillError("Ready receipt image is missing from the tar")
        if name not in images:
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(members[name]) as source, target.open("xb") as output:
                for block in iter(lambda: source.read(COPY_BLOCK_BYTES), b""):
                    output.write(block)
            images[name] = target
        _verify_jpeg(images[name], receipt)
    return tuple(ReadyCover(receipt, images[receipt["cover_path"]]) for receipt in receipts)


def _validate_tar_end(path):
    ending_bytes = TAR_END_BLOCKS * tarfile.BLOCKSIZE
    size = path.stat().st_size
    if size < ending_bytes or size % tarfile.BLOCKSIZE:
        raise BackfillError("Batch tar is truncated or not block-aligned")
    with path.open("rb") as stream:
        stream.seek(-ending_bytes, 2)
        if stream.read() != bytes(ending_bytes):
            raise BackfillError("Batch tar is missing its end markers")


def extract_batch(path, root):
    _validate_tar_end(path)
    with tarfile.open(path, "r:") as archive:
        members = _tar_members(archive)
        metadata = json.loads(_read_bytes(archive, members["batch.json"]))
        ready = _read_receipts(_read_bytes(archive, members["receipts.jsonl"]), "ready")
        failures = _read_receipts(_read_bytes(archive, members["failures.jsonl"]), "failed")
        _validate_batch(metadata, ready, failures)
        images = {receipt["cover_path"] for receipt in ready}
        if set(members) != MANIFEST_NAMES | images:
            raise BackfillError("Batch contains unlisted files; only declared JPEGs may be imported")
        if (metadata["cover_count"] != len(images)
                or metadata["cover_bytes"] != sum(members[name].size for name in images)):
            raise BackfillError("Batch image counts or bytes do not match the tar")
        prepared = _extract_images(archive, members, ready, root)
    return CoverBatch(metadata, prepared, failures)
