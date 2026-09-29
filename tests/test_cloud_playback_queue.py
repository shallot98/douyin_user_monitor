import json
from unittest.mock import Mock

import pytest

from scripts.cloud_playback.d2_client import D2Error, D2HTTPError
from scripts.cloud_playback.d2_offline import OfflineImportError
from scripts.cloud_playback.run_queue import execute_queue, resolve_source


@pytest.fixture
def job():
    return {"user_id": "user", "aweme_id": "work", "operation": "transcode",
            "source_paths": ["/old/video.mp4", "/verified/video.mp4"],
            "expected_size": 123, "fingerprint": "audit-revision"}


def metadata(path="/old/video.mp4"):
    return {"path": path, "size": "123", "identity": "file", "content_identity": "content"}


def test_resolver_accepts_only_verified_missing_as_next_candidate(job):
    client = Mock()
    missing = D2HTTPError(404, "file lookup", code=5, message="file not found")
    client.post.side_effect = [missing, metadata("/verified/video.mp4")]
    result = resolve_source(client, job)
    assert result["source"]["path"] == "/verified/video.mp4"
    assert result["missing_source_paths"] == ["/old/video.mp4"]
    assert "source" not in job


def test_authentication_failure_does_not_try_other_sources(job):
    client = Mock()
    client.post.side_effect = D2HTTPError(403, "file lookup")
    with pytest.raises(D2HTTPError):
        resolve_source(client, job)
    assert client.post.call_count == 1


def test_size_change_never_reaches_media_processing(tmp_path, job):
    client = Mock()
    client.post.return_value = {**metadata(), "size": "124"}
    process = Mock()
    summary = execute_queue(client, [job], workspace=tmp_path, process=process)
    assert summary["failed"] == 1
    process.assert_not_called()


def test_resume_uses_completed_receipt_without_duplicate_upload(tmp_path, job):
    client = Mock()
    client.post.return_value = metadata()
    process = Mock(return_value={"uploaded_media_verified": True})
    first = execute_queue(client, [job], workspace=tmp_path, process=process)
    second = execute_queue(client, [job], workspace=tmp_path, process=process)
    assert first["succeeded"] == second["succeeded"] == 1
    assert process.call_count == 1
    assert len((tmp_path / "results.jsonl").read_text().splitlines()) == 1


def test_failure_is_recorded_and_other_works_continue(tmp_path, job):
    client = Mock()
    client.post.return_value = metadata()
    next_job = {**job, "aweme_id": "next"}
    process = Mock(side_effect=[D2Error("upload failed"), {"uploaded_media_verified": True}])
    summary = execute_queue(client, [job, next_job], workspace=tmp_path, process=process)
    assert summary["failed"] == 1 and summary["succeeded"] == 1 and summary["pending"] == 0
    rows = [json.loads(line) for line in (tmp_path / "results.jsonl").read_text().splitlines()]
    assert rows[0]["status"] == "failed" and rows[0]["error"] == "upload failed"
    assert rows[1]["status"] == "succeeded"


def test_changed_source_revision_does_not_reuse_old_receipt(tmp_path, job):
    client = Mock()
    client.post.return_value = metadata()
    process = Mock(return_value={"uploaded_media_verified": True})
    execute_queue(client, [job], workspace=tmp_path, process=process)
    with pytest.raises(ValueError, match="different source revision"):
        execute_queue(client, [{**job, "fingerprint": "new-revision"}], workspace=tmp_path, process=process)
    assert process.call_count == 1


def test_offline_failure_retains_provider_task_evidence(tmp_path, job):
    client = Mock()
    client.post.return_value = metadata()
    history = ({"identity": "task", "status": 7, "progress": "20"},)
    process = Mock(side_effect=OfflineImportError("import timeout", task_identity="task", history=history))
    execute_queue(client, [job], workspace=tmp_path, process=process)
    row = json.loads((tmp_path / "results.jsonl").read_text())
    assert row["offline_task_identity"] == "task"
    assert row["offline_history"] == list(history)
    assert row["status"] == "failed"


def test_quota_wait_reports_without_processing_media(tmp_path, job):
    client = Mock()
    client.post.return_value = metadata()
    process = Mock(return_value={"uploaded_media_verified": True})

    def before(report):
        report({"status": "waiting_for_upload_quota", "remaining": 0})
        process.assert_not_called()
        progress = json.loads((tmp_path / "progress.json").read_text())
        assert progress["status"] == "waiting_for_upload_quota"
        assert progress["pending"] == 1

    result = execute_queue(client, [job], workspace=tmp_path, process=process, before_job=before)
    assert result["succeeded"] == 1


def test_next_stage_does_not_expose_previous_completion_marker(tmp_path, job):
    client = Mock()
    client.post.return_value = metadata()
    execute_queue(client, [job], workspace=tmp_path, process=Mock(return_value={}))
    assert (tmp_path / "finished.json").exists()

    def process(*args):
        assert not (tmp_path / "finished.json").exists()
        return {}

    result = execute_queue(client, [job, {**job, "aweme_id": "second"}], workspace=tmp_path, process=process)
    assert result["succeeded"] == 2
