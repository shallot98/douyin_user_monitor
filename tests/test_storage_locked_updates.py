import json
import os
from copy import deepcopy

import pytest

from douyin_user_monitor.monitor.storage import MonitorStorage


def test_locked_update_reads_replaced_file_with_identical_timestamp(tmp_path):
    path = tmp_path / "state.json"
    first = MonitorStorage(path)
    first.save_state({"users": [], "monitoring": {"revision": "old"}})
    stamp = path.stat().st_mtime_ns
    second = MonitorStorage(path)
    second.update_state(lambda state: state["users"].append({"id": "new-user"}))
    os.utime(path, ns=(stamp, stamp))

    result = first.update_state(lambda state: state["monitoring"].update(revision="new"))

    assert result["users"][0]["id"] == "new-user"
    assert json.loads(path.read_text())["users"][0]["id"] == "new-user"
    assert result["monitoring"]["revision"] == "new"


def test_failed_mutator_preserves_disk_and_cached_state(tmp_path):
    storage = MonitorStorage(tmp_path / "state.json")
    before = deepcopy(storage.load_state())
    disk_before = storage.state_file.read_bytes()

    def rejected(state):
        state["users"].append({"id": "must-not-survive"})
        raise ValueError("rejected update")

    with pytest.raises(ValueError, match="rejected update"):
        storage.update_state(rejected)

    assert storage.load_state() == before
    assert storage.state_file.read_bytes() == disk_before
