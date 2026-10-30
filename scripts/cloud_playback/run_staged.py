"""Run the reviewed D2 pilot successfully before processing the complete queue."""

import argparse
from pathlib import Path

from .run_queue import main as run_queue
from .run_queue import utc_now, write_json
from .source_recovery import main as run_recovery


def _stage(workspace, name):
    workspace.mkdir(parents=True, exist_ok=True)
    write_json(workspace / "pipeline.json", {"stage": name, "updated_at": utc_now()})


def _run(args, *, execute, recover):
    common = ["--credentials", args.credentials, "--workspace", args.workspace]
    workspace = Path(args.workspace)
    _stage(workspace, "pilot")
    pilot_status = execute([*common, "--queue", args.pilot_queue])
    if pilot_status:
        _stage(workspace, "pilot_failed")
        return pilot_status
    recovery_status = 0
    if args.recovery_queue:
        _stage(workspace, "source_recovery")
        recovery_status = recover([*common, "--queue", args.recovery_queue, "--source-root", args.recovery_source_root])
    _stage(workspace, "batch")
    batch_status = execute([*common, "--queue", args.queue])
    status = int(bool(batch_status or recovery_status))
    _stage(workspace, "finished_with_failures" if status else "finished")
    return status


def main(argv=None, *, execute=run_queue, recover=run_recovery):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", required=True)
    parser.add_argument("--pilot-queue", required=True)
    parser.add_argument("--queue", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--recovery-queue")
    parser.add_argument("--recovery-source-root")
    args = parser.parse_args(argv)
    if bool(args.recovery_queue) != bool(args.recovery_source_root):
        parser.error("Recovery queue and source root must be provided together")
    return _run(args, execute=execute, recover=recover)


if __name__ == "__main__":
    raise SystemExit(main())
