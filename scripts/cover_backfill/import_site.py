"""Import verified backfill JPEGs without changing download history or cloud media paths."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from douyin_user_monitor.monitor.cover_store import CoverIntegrityError, CoverStore

from .import_archive import extract_batch, file_digest
from .import_checkpoint import (
    CurrentRecords,
    checkpoint_lock,
    commit_checkpoint,
    load_checkpoint,
    outcome_counts,
    write_report,
)
from .models import IDENTITY_FIELDS, SCHEMA_VERSION, BackfillError


@dataclass(frozen=True)
class ImportPaths:
    batch: Path
    state: Path
    covers: Path
    checkpoint: Path


class PublicationError(BackfillError):
    def __init__(self, items, error):
        self.items = tuple(items)
        super().__init__(f"{type(error).__name__}: {error}")


def _outcome(receipt, status, *, reason=None, version=None):
    return {**{key: receipt[key] for key in IDENTITY_FIELDS},
            "input_revision": receipt["input_revision"], "status": status,
            "reason": reason, "version": version}


def _existing_cover(store, user_id, record):
    try:
        existing = store.lookup(user_id, record)
        if existing is not None:
            store.read(existing)
        return existing, None
    except (CoverIntegrityError, FileNotFoundError) as error:
        return None, _error(error)


def _import_ready(item, records, store, *, replay):
    receipt = item.receipt
    record, reason = records.find(receipt)
    if record is None:
        return _outcome(receipt, "skipped", reason=reason)
    existing, damage = _existing_cover(store, receipt["user_id"], record)
    if existing is not None:
        return _outcome(receipt, "already_available", version=existing.version)
    metadata = store.publish(receipt["user_id"], record, item.source)
    store.read(metadata)
    if damage is not None or replay:
        reason = damage or {"code": "checkpoint_cover_missing", "message": "Previously imported cover is absent"}
        return _outcome(receipt, "repaired", reason=reason, version=metadata.version)
    return _outcome(receipt, "published", version=metadata.version)


def _error(error):
    return {"type": type(error).__name__, "message": str(error)}


def _publish_items(batch, paths, *, replay):
    records, store = CurrentRecords(paths.state), CoverStore(paths.covers)
    items = []
    for item in batch.ready:
        try:
            items.append(_import_ready(item, records, store, replay=replay))
        except Exception as error:
            items.append(_outcome(item.receipt, "failed", reason=_error(error)))
            raise PublicationError(items, error) from error
    for receipt in batch.failures:
        items.append({**_outcome(receipt, "failed", reason=receipt["error"]),
                      "stage": receipt.get("stage"), "attempts": receipt.get("attempts")})
    return items


def _summary(report, checkpoint):
    counts = report["counts"]
    if report.get("replay"):
        counts = {**outcome_counts(()), "repaired": counts["repaired"]}
    return {"status": report["status"], "next_offset": checkpoint["next_offset"],
            **counts, "totals": checkpoint["totals"], "report": report["report"]}


def _import_locked(paths):
    previous = load_checkpoint(paths.checkpoint)
    destination = {"state_file": str(paths.state), "cover_root": str(paths.covers)}
    if previous.get("destination", destination) != destination:
        raise BackfillError("Importer checkpoint belongs to a different state file or cover store")
    previous = {**previous, "destination": destination}
    digest = file_digest(paths.batch)
    replay = digest in previous["batches"]
    report = {"schema_version": SCHEMA_VERSION, "batch_sha256": digest, "replay": replay, "items": []}
    try:
        with TemporaryDirectory(prefix="cover-import-", dir=paths.checkpoint.parent) as directory:
            batch = extract_batch(paths.batch, Path(directory))
            report["batch"] = batch.metadata
            if not replay and batch.metadata["after_offset"] != previous["next_offset"]:
                raise BackfillError("Batch after_offset does not match the importer checkpoint")
            report["items"] = _publish_items(batch, paths, replay=replay)
        report["counts"] = outcome_counts(report["items"])
        if replay and not report["counts"]["repaired"]:
            return {"status": "already_imported", "next_offset": previous["next_offset"],
                    **outcome_counts(()), "totals": previous["totals"],
                    "report": previous["batches"][digest]["report"]}
        report["status"] = "imported_with_failures" if report["counts"]["failed"] else "imported"
        if replay:
            report["status"] = "repaired"
        if not batch.metadata["scanned_receipts"]:
            report["status"] = "no_new_receipts"
        report["report"] = str(write_report(paths.checkpoint, report))
        current = commit_checkpoint(paths.checkpoint, previous, report) if batch.metadata["scanned_receipts"] else previous
        return _summary(report, current)
    except Exception as error:
        items = error.items if isinstance(error, PublicationError) else report["items"]
        failed = {**report, "status": "failed", "items": items, "counts": outcome_counts(items),
                  "error": _error(error)}
        audit = write_report(paths.checkpoint, failed)
        raise BackfillError(f"Cover batch import failed: {type(error).__name__}: {error}; audit={audit}") from error


def import_batch(batch, *, state_file, cover_root, checkpoint):
    paths = ImportPaths(Path(batch).resolve(), Path(state_file).resolve(),
                        Path(cover_root).resolve(), Path(checkpoint).resolve())
    if paths.state == paths.checkpoint or paths.batch == paths.checkpoint:
        raise BackfillError("Importer checkpoint must not overwrite an input file")
    with checkpoint_lock(paths.checkpoint):
        return _import_locked(paths)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=Path, required=True)
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--cover-root", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        summary = import_batch(args.batch, state_file=args.state_file, cover_root=args.cover_root,
                               checkpoint=args.checkpoint)
    except Exception as error:
        print(json.dumps({"status": "failed", "type": type(error).__name__, "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
