"""Publish verified cloud receipts using metadata-only SSH skill transfers."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import shlex
import subprocess
from collections import Counter
from contextlib import ExitStack
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from .d2_client import Credentials, D2Client, D2Error, HttpTransport
from .media_tools import MediaJobError
from .publish import PublicationError, publish_verified
from .receipt import verified_publication
from .run_queue import job_key, utc_now, write_json

SSH_TIMEOUT_SECONDS = 30
PROCESS_TIMEOUT_SECONDS = 45
WEBSITE_TIMEOUT_SECONDS = 30
SSH_SKILL_EXECUTE = Path("/root/.codex/skills/ssh-skill/scripts/ssh_execute.py")
SSH_PYTHON = "/root/.local/bin/python"


@dataclass(frozen=True)
class RemoteReader:
    node: str
    code_root: str
    workspace: str
    python: str = "/opt/mamba/bin/python3"

    def __call__(self, offset):
        command = "cd " + shlex.quote(self.code_root) + " && " + shlex.join([
            self.python, "-m", "scripts.cloud_playback.export_results",
            "--workspace", self.workspace, "--offset", str(offset),
        ])
        result = subprocess.run(
            [SSH_PYTHON, str(SSH_SKILL_EXECUTE), self.node, command, "--timeout", str(SSH_TIMEOUT_SECONDS)],
            capture_output=True, text=True, timeout=PROCESS_TIMEOUT_SECONDS, check=True,
        )
        response = json.loads(result.stdout)
        if response.get("success") is not True or response.get("exit_code") != 0:
            raise RuntimeError("SSH receipt export failed: " + str(response.get("stderr", "")))
        return json.loads(response["stdout"])


def _publish_one(receipt, *, verify, publish):
    if receipt["status"] == "failed":
        return {"status": "worker_failed", "error": receipt["error"]}
    if receipt["status"] != "succeeded":
        raise ValueError("Remote receipt has an unknown status")
    try:
        publication = verify(receipt)
        website = publish([publication])
    except (PublicationError, MediaJobError) as error:
        return {"status": "rejected", "error_type": type(error).__name__, "error": str(error)}
    return {"status": "published", "playback_file": publication.get("playback_file"),
            "cover_file": publication.get("cover_file"), "verified_at": publication["verified_at"],
            "website": website}


def sync_once(workspace, *, read_remote, verify, publish):
    workspace.mkdir(parents=True, exist_ok=True)
    checkpoint = workspace / "checkpoint.json"
    state = json.loads(checkpoint.read_text()) if checkpoint.exists() else {"offset": 0, "counts": {}}
    page = read_remote(state["offset"])
    if page["offset"] != state["offset"]:
        raise ValueError("Remote export does not begin at the acknowledged receipt offset")
    receipts = workspace / "receipts"
    receipts.mkdir(exist_ok=True)
    counts = Counter(state["counts"])
    for row in page["rows"]:
        if row["offset"] <= state["offset"]:
            raise ValueError("Remote receipt offset did not advance")
        receipt = row["receipt"]
        outcome = _publish_one(receipt, verify=verify, publish=publish)
        result = {**outcome, "updated_at": utc_now(), "receipt": receipt}
        write_json(receipts / (job_key(receipt["job"]) + ".json"), result)
        counts[outcome["status"]] += 1
        state = {"offset": row["offset"], "counts": dict(counts)}
        write_json(checkpoint, state)
    summary = {**state, "updated_at": utc_now(), "synchronizer_status": "ok",
               "worker": page["progress"], "worker_finished": page["worker_finished"],
               "worker_process": page["worker_process"],
               "pipeline": page["pipeline"],
               "received_this_poll": len(page["rows"])}
    write_json(workspace / "progress.json", summary)
    return summary


def _configured(config, stack):
    import httpx

    from douyin_user_monitor.monitor.openlist_client import OpenListClient
    from douyin_user_monitor.monitor.storage import MonitorStorage
    from douyin_user_monitor.settings_loaders import load_settings

    from .directory_refresh import PublicationDirectoryRefresher
    from .website_check import verify_published

    credentials = Credentials.from_mapping(json.loads(Path(config["credentials"]).read_text()))
    client = D2Client(credentials, transport=HttpTransport())
    storage = MonitorStorage(Path(config["state_file"]))
    reader = RemoteReader(**config["remote"])
    web = stack.enter_context(httpx.Client(base_url=config["website_url"], timeout=WEBSITE_TIMEOUT_SECONDS, trust_env=False))
    archive = load_settings().archive
    openlist = OpenListClient(archive)
    loop = asyncio.new_event_loop()
    stack.callback(loop.close)
    stack.callback(lambda: loop.run_until_complete(loop.shutdown_asyncgens()))
    stack.callback(lambda: loop.run_until_complete(openlist.close()))
    refresh = PublicationDirectoryRefresher(primary_base=archive.primary_base,
                                            refresh_directory=openlist.refresh_directory)

    def publish(publications):
        loop.run_until_complete(refresh(publications))
        state = publish_verified(storage, publications)
        return verify_published(state, publications, client=web)

    return reader, partial(verified_publication, client, node=reader.node), publish


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text())
    workspace = Path(config["workspace"])
    workspace.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack, (workspace / "synchronizer.lock").open("a") as lock:
        reader, verify, publish = _configured(config, stack)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            summary = sync_once(workspace, read_remote=reader, verify=verify, publish=publish)
        except (D2Error, OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            write_json(workspace / "progress.json", {"updated_at": utc_now(), "synchronizer_status": "failed",
                       "error_type": type(error).__name__, "error": str(error)})
            raise
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
