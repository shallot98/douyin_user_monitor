import json
from unittest.mock import Mock

from scripts.cloud_playback.run_staged import main


def arguments(path):
    return ["--credentials", "secret.json", "--workspace", str(path), "--pilot-queue", "pilot.json",
            "--queue", "complete.json"]


def test_failed_pilot_prevents_batch_submission(tmp_path):
    execute, recover = Mock(return_value=1), Mock()
    assert main(arguments(tmp_path), execute=execute, recover=recover) == 1
    assert execute.call_count == 1
    recover.assert_not_called()
    assert json.loads((tmp_path / "pipeline.json").read_text())["stage"] == "pilot_failed"


def test_successful_pilot_resumes_full_queue_in_same_receipt_workspace(tmp_path):
    execute = Mock(side_effect=[0, 0])
    assert main(arguments(tmp_path), execute=execute) == 0
    assert execute.call_args_list[1].args[0] == ["--credentials", "secret.json", "--workspace", str(tmp_path),
                                                "--queue", "complete.json"]


def test_source_recovery_failure_is_reported_and_unrelated_batch_still_runs(tmp_path):
    execute, recover = Mock(side_effect=[0, 0]), Mock(return_value=1)
    args = arguments(tmp_path) + ["--recovery-queue", "recovery.json", "--recovery-source-root", "sources"]
    assert main(args, execute=execute, recover=recover) == 1
    assert execute.call_count == 2 and recover.call_count == 1
    assert json.loads((tmp_path / "pipeline.json").read_text())["stage"] == "finished_with_failures"
