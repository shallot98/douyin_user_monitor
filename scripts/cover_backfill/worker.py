"""Generate local JPEGs on independent compute; never upload D2 objects."""

from __future__ import annotations

import argparse
import json
import math
import socket
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from functools import partial
from itertools import islice
from pathlib import Path

from scripts.cloud_playback.d2_client import Credentials, D2Client, HttpTransport
from scripts.cloud_playback.media_tools import prepare_cover

from .diagnostics import public_error
from .journal import (
    append_receipt,
    atomic_json,
    inspect_cover,
    producer_lock,
    read_receipts,
    receipt_key,
    verify_receipt,
)
from .models import (
    DEFAULT_MEDIA_TIMEOUT,
    DEFAULT_PAGE_SIZE,
    DEFAULT_WORKERS,
    BackfillError,
    fair_jobs,
    load_manifest,
)
from .pipeline import Dependencies, process_job, utc_now

PROJECT_HOSTNAME = "hdz"
ERROR_SAMPLE_LIMIT = 20


@dataclass(frozen=True, kw_only=True)
class WorkerOptions:
    workdir: Path
    workers: int = DEFAULT_WORKERS
    page_size: int = DEFAULT_PAGE_SIZE


@dataclass(frozen=True, kw_only=True)
class RunContext:
    root: Path
    previous: object
    dependencies: Dependencies


def _run_one(job, context):
    previous = context.previous.get(job.key)
    invalid = None
    if previous is not None and previous["status"] == "ready":
        try:
            verify_receipt(context.root, previous, context.dependencies.inspect)
        except Exception as error:
            invalid = public_error(error)
        else:
            return {"skipped": True, "receipt": previous}
    receipt = process_job(job, context.root, context.dependencies)
    if invalid is not None:
        receipt = {**receipt, "resume_error": invalid}
    return {"skipped": False, "receipt": receipt}


def _submit(executor, jobs, context, *, count):
    return {executor.submit(_run_one, job, context) for job in islice(jobs, count)}


def _save_outcome(root, stats, outcome):
    if outcome["skipped"]:
        return {**stats, "skipped_verified": stats["skipped_verified"] + 1}
    receipt = outcome["receipt"]
    offset = append_receipt(root / "results.jsonl", receipt)
    if receipt["status"] == "ready":
        return {**stats, "succeeded": stats["succeeded"] + 1, "journal_offset": offset,
                "bytes_generated": stats["bytes_generated"] + receipt["bytes"]}
    sample = {"user_id": receipt["user_id"], "aweme_id": receipt["aweme_id"],
              "stage": receipt["stage"], "error": receipt["error"]}
    return {**stats, "failed": stats["failed"] + 1, "journal_offset": offset,
            "error_samples": [*stats["error_samples"], sample][-ERROR_SAMPLE_LIMIT:]}


def _write_progress(root, stats, *, active, finished=False):
    completed = stats["succeeded"] + stats["failed"] + stats["skipped_verified"]
    progress = {**stats, "completed": completed, "remaining": stats["total"] - completed,
                "active": active, "finished": finished, "updated_at": utc_now()}
    atomic_json(root / "progress.json", progress)
    return progress


def _run_pool(jobs, options, context, *, initial_offset):
    stats = {"total": len(jobs), "succeeded": 0, "failed": 0, "skipped_verified": 0,
             "bytes_generated": 0, "journal_offset": initial_offset, "error_samples": [], "started_at": utc_now()}
    ordered = iter(fair_jobs(jobs, options.page_size))
    with ThreadPoolExecutor(max_workers=options.workers, thread_name_prefix="cover") as executor:
        pending = _submit(executor, ordered, context, count=options.workers)
        _write_progress(context.root, stats, active=len(pending))
        while pending:
            completed, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed:
                stats = _save_outcome(context.root, stats, future.result())
            pending.update(_submit(executor, ordered, context, count=len(completed)))
            _write_progress(context.root, stats, active=len(pending))
    return _write_progress(context.root, stats, active=0, finished=True)


def run_worker(jobs, options, dependencies):
    if socket.gethostname().split(".", 1)[0] == PROJECT_HOSTNAME:
        raise BackfillError("Historical cover extraction is forbidden on project host hdz; use independent compute")
    if options.workers <= 0 or options.page_size <= 0:
        raise BackfillError("workers and page_size must be positive")
    root = options.workdir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    with producer_lock(root):
        (root / ".tmp").mkdir(exist_ok=True)
        previous = {}
        offset = 0
        for offset, receipt in read_receipts(root / "results.jsonl", strict_tail=True):
            previous[receipt_key(receipt)] = receipt
        context = RunContext(root=root, previous=previous, dependencies=dependencies)
        return _run_pool(jobs, options, context, initial_offset=offset)


def _positive_integer(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _positive_seconds(value):
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number of seconds")
    return seconds


def _arguments(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--workers", type=_positive_integer, default=DEFAULT_WORKERS)
    parser.add_argument("--page-size", type=_positive_integer, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--media-timeout", type=_positive_seconds, default=DEFAULT_MEDIA_TIMEOUT,
                        help="timeout in seconds for each extraction or JPEG validation subprocess (default: 120)")
    return parser.parse_args(argv)


def _dependencies(credentials, timeout):
    def make_client():
        return D2Client(credentials=credentials, transport=HttpTransport())
    return Dependencies(make_client=make_client, extract=partial(prepare_cover, timeout=timeout),
                        inspect=partial(inspect_cover, timeout=timeout))


def main(argv=None):
    args = _arguments(argv)
    try:
        jobs = load_manifest(args.manifest)
        credentials = Credentials.from_mapping(json.loads(args.credentials.read_bytes()))
        options = WorkerOptions(workdir=args.workdir, workers=args.workers, page_size=args.page_size)
        progress = run_worker(jobs, options, _dependencies(credentials, args.media_timeout))
    except Exception as error:
        print(json.dumps({"status": "failed", "error": public_error(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(progress, ensure_ascii=False))
    return int(progress["failed"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
