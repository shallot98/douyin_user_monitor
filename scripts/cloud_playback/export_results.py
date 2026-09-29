"""Export completed metadata receipts; never open any media file."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

DEFAULT_BATCH_SIZE = 20
PROCESS_STATUS_TIMEOUT_SECONDS = 10


def managed_process_status(workspace):
    path = workspace / "process-manager.json"
    if not path.exists():
        return None
    config = json.loads(path.read_text())
    result = subprocess.run(["supervisorctl", "-c", config["config"], "status", config["program"]], capture_output=True,
                            text=True, timeout=PROCESS_STATUS_TIMEOUT_SECONDS, check=False)
    return {"manager": "supervisor", "program": config["program"], "exit_code": result.returncode,
            "status": result.stdout.strip(), "stderr": result.stderr.strip()}


def export_page(workspace, offset, *, limit=DEFAULT_BATCH_SIZE):
    if offset < 0 or limit <= 0:
        raise ValueError("Receipt offset and batch size are invalid")
    path = workspace / "results.jsonl"
    if offset > path.stat().st_size:
        raise ValueError("Remote receipt log is shorter than the acknowledged offset")
    rows = []
    with path.open("rb") as source:
        source.seek(offset)
        for _ in range(limit):
            line = source.readline()
            if not line.endswith(b"\n"):
                break
            rows.append({"offset": source.tell(), "receipt": json.loads(line)})
    return {"offset": offset, "rows": rows,
            "progress": json.loads((workspace / "progress.json").read_text()),
            "pipeline": json.loads((workspace / "pipeline.json").read_text()) if (workspace / "pipeline.json").exists() else None,
            "worker_process": managed_process_status(workspace),
            "worker_finished": (workspace / "finished.json").exists()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--offset", required=True, type=int)
    parser.add_argument("--limit", type=int, default=DEFAULT_BATCH_SIZE)
    args = parser.parse_args(argv)
    print(json.dumps(export_page(args.workspace, args.offset, limit=args.limit), ensure_ascii=False))


if __name__ == "__main__":
    main()
