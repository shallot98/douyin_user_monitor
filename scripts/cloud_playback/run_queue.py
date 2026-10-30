"""Run a resumable, single-worker cloud repair queue on the independent node."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from collections import Counter
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

from .d2_client import Credentials, D2Client, D2Error, D2HTTPError, HttpTransport
from .d2_offline import OfflineImportError
from .quota import DEFAULT_POLL_SECONDS, UploadWithQuota, wait_for_upload_capacity

SOURCE_FIELDS = ("path", "identity", "content_identity", "size", "update_ts")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def job_key(job):
    return job["user_id"] + "__" + job["aweme_id"]


def resolve_source(client, job):
    """Only a verified D2 not-found advances to the next documented path."""
    missing = []
    for path in job["source_paths"]:
        try:
            source = client.post("/v6/userfile/get", {"path": path})
        except D2HTTPError as exc:
            if exc.status != 404 or exc.code != 5:
                raise
            missing.append(path)
            continue
        if source.get("path") != path or source.get("dir"):
            raise D2Error("Source metadata does not identify the requested video file")
        if int(source["size"]) != job["expected_size"] or not source.get("content_identity"):
            raise D2Error("Source size or content identity differs from the audited object")
        return {**job, "source": {key: source.get(key) for key in SOURCE_FIELDS},
                "missing_source_paths": missing}
    raise D2Error("All documented D2 source paths returned file-not-found")


def _process_one(client, job, *, workspace, process, resolve):
    started = utc_now()
    try:
        resolved = resolve(client, job)
        result = process(client, resolved, workspace / "media" / job_key(job))
    except (D2Error, ValueError, RuntimeError, OSError) as exc:
        error = {"status": "failed", "job": job, "started_at": started, "finished_at": utc_now(),
                 "error_type": type(exc).__name__, "error": str(exc)}
        if isinstance(exc, D2HTTPError):
            error.update(http_status=exc.status, provider_code=exc.code, provider_message=exc.message)
        if isinstance(exc, OfflineImportError):
            error.update(offline_task_identity=exc.task_identity, offline_history=exc.history)
        return error
    return {"status": "succeeded", "job": resolved, "result": result,
            "started_at": started, "finished_at": utc_now()}


def _append_result(workspace, receipt):
    with (workspace / "results.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(receipt, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _previous_results(workspace):
    path = workspace / "results.jsonl"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as source:
        return {job_key(row["job"]): row for row in map(json.loads, source)}


def _progress(workspace, jobs, results, *, current=None, extra=None):
    counts = Counter(row["status"] for row in results.values())
    summary = {"updated_at": utc_now(), "total": len(jobs), "completed": len(results),
               "succeeded": counts["succeeded"], "failed": counts["failed"],
               "pending": len(jobs) - len(results), "current": current}
    if extra:
        summary.update(extra)
    write_json(workspace / "progress.json", summary)
    return summary


def _receipt_path(workspace, job):
    return workspace / "receipts" / (job_key(job) + ".json")


def execute_queue(client, jobs, *, workspace, process, retry_failed=False, before_job=None, resolve=resolve_source):
    keys = [job_key(job) for job in jobs]
    key_set = set(keys)
    if len(keys) != len(key_set):
        raise ValueError("Duplicate work in repair queue")
    (workspace / "receipts").mkdir(parents=True, exist_ok=True)
    (workspace / "results.jsonl").touch(exist_ok=True)
    (workspace / "finished.json").unlink(missing_ok=True)
    results = {key: value for key, value in _previous_results(workspace).items() if key in key_set}
    for job in jobs:
        key = job_key(job)
        path = _receipt_path(workspace, job)
        previous = json.loads(path.read_text()) if path.exists() else results.get(key)
        if previous and previous["job"]["fingerprint"] != job["fingerprint"]:
            raise ValueError("Existing receipt belongs to a different source revision")
        if previous and (previous["status"] == "succeeded" or not retry_failed):
            if key not in results:
                _append_result(workspace, previous)
            results[key] = previous
            continue
        current = {name: job[name] for name in ("user_id", "aweme_id", "operation")}
        current = {**current, "started_at": utc_now()}
        _progress(workspace, jobs, results, current=current)
        if before_job:
            before_job(lambda event: _progress(workspace, jobs, results, current=current, extra=event))
        receipt = _process_one(client, job, workspace=workspace, process=process, resolve=resolve)
        write_json(path, receipt)
        _append_result(workspace, receipt)
        results[key] = receipt
        _progress(workspace, jobs, results)
    summary = _progress(workspace, jobs, results)
    write_json(workspace / "finished.json", summary)
    return summary


def _upload_progress(workspace, event):
    path = workspace / "progress.json"
    current = json.loads(path.read_text())
    write_json(path, {**current, **event, "updated_at": utc_now()})


def _execution_callbacks(client, args):
    from .media_worker import process_job

    if args.origin_config is None:
        from .d2_upload import upload_file

        before = partial(wait_for_upload_capacity, client, poll_seconds=args.quota_poll_seconds)
        uploader = UploadWithQuota(upload=upload_file, report=partial(_upload_progress, args.workspace),
                                   poll_seconds=args.quota_poll_seconds)
        return partial(process_job, uploader=uploader), before
    from .d2_offline import OfflineUploader
    from .origin import MediaOrigin

    config = json.loads(args.origin_config.read_text())
    origin = MediaOrigin(config["base_url"], Path(config["entries"]), Path(config["media_root"]))
    uploader = OfflineUploader(origin=origin)
    return partial(process_job, uploader=uploader), None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", required=True, type=Path)
    parser.add_argument("--queue", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--quota-poll-seconds", type=int, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--origin-config", type=Path, help="Explicitly use the independent HTTPS offline-import origin")
    args = parser.parse_args(argv)
    credentials = Credentials.from_mapping(json.loads(args.credentials.read_text()))
    client = D2Client(credentials, transport=HttpTransport())
    process, before = _execution_callbacks(client, args)
    jobs = json.loads(args.queue.read_text())["jobs"]
    args.workspace.mkdir(parents=True, exist_ok=True)
    with (args.workspace / "worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        summary = execute_queue(client, jobs, workspace=args.workspace,
                                process=process, retry_failed=args.retry_failed, before_job=before)
    print(json.dumps(summary, ensure_ascii=False))
    return int(bool(summary["failed"]))


if __name__ == "__main__":
    raise SystemExit(main())
