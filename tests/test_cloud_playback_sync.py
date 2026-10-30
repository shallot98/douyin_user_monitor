import json
from unittest.mock import Mock

import pytest

from scripts.cloud_playback.d2_client import D2Error
from scripts.cloud_playback.export_results import export_page
from scripts.cloud_playback.publish import PublicationError
from scripts.cloud_playback.sync_results import sync_once


def row(offset, *, status="succeeded", work="work"):
    receipt = {"status": status, "job": {"user_id": "user", "aweme_id": work}, "error": "worker error"}
    return {"offset": offset, "receipt": receipt}


def page(offset, rows):
    return {"offset": offset, "rows": rows, "progress": {"total": 2}, "worker_finished": False,
            "worker_process": None, "pipeline": None}


def test_export_holds_partial_line_until_worker_finishes_it(tmp_path):
    first, second = row(1)["receipt"], row(2, work="second")["receipt"]
    complete = (json.dumps(first) + "\n").encode()
    partial = json.dumps(second).encode()
    log = tmp_path / "results.jsonl"
    log.write_bytes(complete + partial)
    (tmp_path / "progress.json").write_text('{"pending":1}')
    first_page = export_page(tmp_path, 0)
    assert first_page["rows"] == [{"offset": len(complete), "receipt": first}]
    assert export_page(tmp_path, len(complete))["rows"] == []
    with log.open("ab") as stream:
        stream.write(b"\n")
    assert export_page(tmp_path, len(complete))["rows"][0]["receipt"] == second


def test_sync_requires_verification_and_resumes_acknowledged_offset(tmp_path):
    read = Mock(side_effect=[page(0, [row(123)]), page(123, [])])
    verify = Mock(return_value={"playback_file": "verified.mp4", "verified_at": "now"})
    publish = Mock(return_value=[])
    first = sync_once(tmp_path, read_remote=read, verify=verify, publish=publish)
    second = sync_once(tmp_path, read_remote=read, verify=verify, publish=publish)
    assert first["counts"] == second["counts"] == {"published": 1}
    assert [call.args[0] for call in read.call_args_list] == [0, 123]
    publish.assert_called_once_with([verify.return_value])
    assert (tmp_path / "receipts/user__work.json").exists()


def test_failed_and_stale_receipts_are_visible_without_publishing(tmp_path):
    read = Mock(return_value=page(0, [row(50, status="failed"), row(100, work="stale")]))
    verify = Mock(side_effect=PublicationError("download revision changed"))
    publish = Mock()
    result = sync_once(tmp_path, read_remote=read, verify=verify, publish=publish)
    assert result["counts"] == {"worker_failed": 1, "rejected": 1}
    assert result["offset"] == 100
    publish.assert_not_called()
    assert verify.call_count == 1


def test_cloud_transport_failure_does_not_acknowledge_or_publish(tmp_path):
    read = Mock(return_value=page(0, [row(100)]))
    verify = Mock(side_effect=D2Error("cloud metadata unavailable"))
    publish = Mock()
    with pytest.raises(D2Error, match="unavailable"):
        sync_once(tmp_path, read_remote=read, verify=verify, publish=publish)
    assert not (tmp_path / "checkpoint.json").exists()
    publish.assert_not_called()


def test_offset_mismatch_is_not_silently_skipped(tmp_path):
    verify, publish = Mock(), Mock()
    with pytest.raises(ValueError, match="acknowledged"):
        sync_once(tmp_path, read_remote=Mock(return_value=page(20, [row(30)])), verify=verify, publish=publish)
    verify.assert_not_called()
    publish.assert_not_called()
