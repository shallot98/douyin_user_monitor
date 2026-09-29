"""Create an immutable tar of ready JPEGs after a receipt byte cursor."""

from __future__ import annotations

import argparse
import io
import json
import os
import tarfile
import tempfile
from pathlib import Path

from .diagnostics import public_error
from .journal import read_receipts, sync_directory, verified_bytes
from .models import DEFAULT_BATCH_COVERS, SCHEMA_VERSION, BackfillError, json_bytes
from .pipeline import utc_now

ARCHIVE_FILE_MODE = 0o644


def select_receipts(journal, *, after_offset, max_covers):
    if type(max_covers) is not int or max_covers <= 0:
        raise BackfillError("max_covers must be positive")
    selected, failures = [], []
    next_offset, scanned = after_offset, 0
    for next_offset, receipt in read_receipts(journal, offset=after_offset):
        scanned += 1
        if receipt["status"] == "failed":
            failures.append(receipt)
            continue
        selected.append(receipt)
        if len(selected) == max_covers:
            break
    return selected, failures, {"after_offset": after_offset, "next_offset": next_offset,
                               "scanned_receipts": scanned, "failed_receipts": len(failures)}


def _add_bytes(archive, name, data):
    entry = tarfile.TarInfo(name)
    entry.size = len(data)
    entry.mode = ARCHIVE_FILE_MODE
    archive.addfile(entry, io.BytesIO(data))


def _write_archive(stream, images, payload):
    batch, receipts, failures = payload
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as archive:
        _add_bytes(archive, "batch.json", json_bytes(batch) + b"\n")
        _add_bytes(archive, "receipts.jsonl", b"".join(json_bytes(receipt) + b"\n" for receipt in receipts))
        _add_bytes(archive, "failures.jsonl", b"".join(json_bytes(receipt) + b"\n" for receipt in failures))
        for name, data in images.items():
            _add_bytes(archive, name, data)


def _publish_batch(output, images, payload):
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=".cover-batch-", dir=output.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            _write_archive(stream, images, payload)
            stream.flush()
            os.fsync(stream.fileno())
            os.link(temporary, output)
            sync_directory(output.parent)
        finally:
            temporary.unlink(missing_ok=True)


def export_batch(root, output, *, after_offset=0, max_covers=DEFAULT_BATCH_COVERS):
    root, output = Path(root).resolve(), Path(output).absolute()
    receipts, failures, cursor = select_receipts(root / "results.jsonl", after_offset=after_offset, max_covers=max_covers)
    images = {receipt["cover_path"]: verified_bytes(root, receipt) for receipt in receipts}
    batch = {"schema_version": SCHEMA_VERSION, **cursor, "created_at": utc_now(),
             "ready_receipts": len(receipts), "cover_count": len(images), "cover_bytes": sum(map(len, images.values()))}
    try:
        _publish_batch(output, images, (batch, receipts, failures))
    except FileExistsError:
        raise BackfillError("Batch output already exists; immutable batches cannot be overwritten") from None
    return batch


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--after-offset", type=int, default=0)
    parser.add_argument("--max-covers", type=int, default=DEFAULT_BATCH_COVERS)
    args = parser.parse_args(argv)
    try:
        batch = export_batch(args.workdir, args.output, after_offset=args.after_offset, max_covers=args.max_covers)
    except Exception as error:
        print(json.dumps({"status": "failed", "error": public_error(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(batch, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
