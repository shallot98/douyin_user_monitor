import json
from pathlib import Path

import pytest
from test_openlist_mover_backup import PRELUDE, run_bash


def create_pending(tmp_path: Path, task_ids: list[str]) -> dict[Path, bytes]:
    runtime = tmp_path / "runtime"
    source = tmp_path / "source/acct"
    runtime.mkdir()
    source.mkdir(parents=True)
    originals = {}
    rows = []
    for index, task_id in enumerate(task_ids):
        path = source / f"original-{index}.mp4"
        originals[path] = f"original-{index}".encode()
        path.write_bytes(originals[path])
        rows.append(f"{path}\t{task_id}\t1\t0\t1\t0\n")
    (runtime / "pending").write_text("".join(rows))
    return originals


def retirement_script(*, limit: int = 0, processed: int = 0) -> str:
    return PRELUDE + r'''
    api_get_json() {
      local name="${2##*/}"
      printf '%s\n' "${name}" >> "${TMP_ROOT}/get-calls"
      cat "${TMP_ROOT}/${name}.json"
    }
    api_post_json() {
      printf '%s\n' "$2" >> "${TMP_ROOT}/write-calls"
      return 1
    }
    ''' + f'''
    retire_capacity_failed_pending token {limit} {processed}
    printf 'errors=%s\\n' "${{PROCESS_ERROR_COUNT}}"
    '''


def write_tasks(tmp_path: Path, endpoint: str, tasks: list[dict]) -> None:
    (tmp_path / f"{endpoint}.json").write_text(json.dumps({"code": 200, "data": tasks}))


def assert_originals_kept(tmp_path: Path, originals: dict[Path, bytes]) -> None:
    assert all(path.read_bytes() == content for path, content in originals.items())
    assert not (tmp_path / "write-calls").exists()
    assert not (tmp_path / "copy-calls").read_bytes()
    assert not (tmp_path / "move-calls").read_bytes()
    assert not list((tmp_path / "runtime").glob("pending.quota*"))


def pending_ids(tmp_path: Path) -> list[str]:
    return [row.split("\t")[1] for row in (tmp_path / "runtime/pending").read_text().splitlines()]


def test_retires_all_confirmed_quota_failures_and_keeps_other_tasks(tmp_path: Path) -> None:
    originals = create_pending(tmp_path, ["quota-1", "active", "quota-2", "other", "unknown", "quota-1"])
    write_tasks(tmp_path, "undone", [
        {"id": "active", "state": 1, "error": "USER_DAILY_UPLOAD_QUOTA_EXCEEDED"},
        {"id": "quota-1", "state": 7, "error": "capacity limit"},
    ])
    write_tasks(tmp_path, "done", [
        {"id": "quota-2", "state": 7, "error": "upload: USER_DAILY_UPLOAD_QUOTA_EXCEEDED"},
        {"id": "other", "state": 7, "error": "network failure"},
    ])

    result = run_bash(retirement_script(), tmp_path)

    assert pending_ids(tmp_path) == ["active", "other", "unknown"]
    assert "历史配额失败记录: 3" in result.stderr
    assert result.stdout.strip() == "errors=0"
    assert_originals_kept(tmp_path, originals)


def test_retirement_respects_remaining_record_budget(tmp_path: Path) -> None:
    originals = create_pending(tmp_path, ["quota-1"] * 4 + ["active"])
    write_tasks(tmp_path, "undone", [{"id": "active", "state": 1}])
    write_tasks(tmp_path, "done", [{"id": "quota-1", "state": 7, "error": "capacity limit"}])

    result = run_bash(retirement_script(limit=3, processed=1), tmp_path)

    assert pending_ids(tmp_path) == ["quota-1", "quota-1", "active"]
    assert "历史配额失败记录: 2" in result.stderr
    assert_originals_kept(tmp_path, originals)


def test_exhausted_reconcile_budget_does_not_query_or_modify_pending(tmp_path: Path) -> None:
    originals = create_pending(tmp_path, ["quota-1", "active"])
    before = (tmp_path / "runtime/pending").read_bytes()

    result = run_bash(retirement_script(limit=1, processed=1), tmp_path)

    assert result.stdout.strip() == "errors=0"
    assert not (tmp_path / "get-calls").exists()
    assert (tmp_path / "runtime/pending").read_bytes() == before
    assert_originals_kept(tmp_path, originals)


@pytest.mark.parametrize("endpoint,response", [
    ("undone", '{"code":500,"message":"unavailable"}'),
    ("done", '{"code":200,"data":null}'),
    ("done", 'not-json'),
])
def test_task_api_error_keeps_pending_bytes_and_sources(tmp_path: Path, endpoint: str, response: str) -> None:
    originals = create_pending(tmp_path, ["quota-1", "active"])
    before = (tmp_path / "runtime/pending").read_bytes()
    write_tasks(tmp_path, "undone", [{"id": "quota-1", "state": 7, "error": "capacity limit"}])
    write_tasks(tmp_path, "done", [])
    (tmp_path / f"{endpoint}.json").write_text(response)

    result = run_bash(retirement_script(), tmp_path)

    assert result.stdout.strip() == "errors=1"
    assert "保留未确认的 pending" in result.stderr
    assert (tmp_path / "runtime/pending").read_bytes() == before
    assert_originals_kept(tmp_path, originals)


def test_no_failed_task_ids_keeps_pending_unchanged(tmp_path: Path) -> None:
    originals = create_pending(tmp_path, ["unknown", "active"])
    before = (tmp_path / "runtime/pending").read_bytes()
    write_tasks(tmp_path, "undone", [{"id": "active", "state": 1}])
    write_tasks(tmp_path, "done", [])

    result = run_bash(retirement_script(), tmp_path)

    assert result.stdout.strip() == "errors=0"
    assert (tmp_path / "runtime/pending").read_bytes() == before
    assert_originals_kept(tmp_path, originals)
