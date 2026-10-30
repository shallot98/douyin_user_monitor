from pathlib import Path

import pytest
from test_openlist_mover_backup import PRELUDE, run_bash

QUOTA_API = r'''
TARGET_BASE="/d2/target"
BACKUP_CLOUD="${TMP_ROOT}/backup-cloud"
PRIMARY_CALLS="${TMP_ROOT}/primary-calls"
REATTACH_CALLS="${TMP_ROOT}/reattach-calls"
mkdir -p "${BACKUP_CLOUD}"
: > "${PRIMARY_CALLS}"
: > "${REATTACH_CALLS}"
get_move_task_by_id() {
  jq -cn --arg error "${QUOTA_ERROR}" '{id:"quota-1",state:7,progress:0,error:$error}'
}
api_get_json() {
  jq -cn --arg error "${QUOTA_ERROR}" '{code:200,data:[
    {id:"quota-1",state:7,error:$error}, {id:"pending-2",state:1,error:""}
  ]}'
}
attach_existing_undone_move_task() { printf 'reattach\n' >> "${REATTACH_CALLS}"; }
api_post_json() {
  local endpoint="$2" payload="$3" dir name
  dir=$(jq -r '.path // .dst_dir' <<< "${payload}")
  if [[ "${dir}" != "${BACKUP_TARGET_BASE}/acct" ]]; then
    printf '%s %s\n' "${endpoint}" "${dir}" >> "${PRIMARY_CALLS}"
    printf '{"code":500,"message":"unexpected primary request"}'
    return 0
  fi
  case "${endpoint}" in
    /api/fs/mkdir) printf '{"code":200}' ;;
    /api/fs/list)
      find "${BACKUP_CLOUD}" -type f -printf '%f\t%s\n' |
        jq -Rn '[inputs | split("\t") | {name:.[0],size:(.[1]|tonumber)}] | {code:200,data:{content:.,total:length}}'
      ;;
    /api/fs/copy)
      name=$(jq -r '.names[0]' <<< "${payload}")
      [[ "$(jq -r '.src_dir' <<< "${payload}")" == "${SOURCE_MOUNT}/.backup-staging/acct" ]] || return 1
      printf '%s\n' "${name}" >> "${COPY_CALLS_FILE}"
      if [[ "${BACKUP_RESULT}" == confirmed ]]; then
        cp -- "${SOURCE_ROOT}/.backup-staging/acct/${name}" "${BACKUP_CLOUD}/${name}"
      elif [[ "${BACKUP_RESULT}" == failed ]]; then
        printf '{"code":500,"message":"backup rejected"}'
        return 0
      fi
      printf '{"code":200,"data":{"tasks":[{"id":"copy-1"}]}}'
      ;;
    *) printf 'unexpected API: %s\n' "${endpoint}" >&2; return 1 ;;
  esac
}
'''


def quota_script(*, error: str, backup_result: str = "confirmed") -> str:
    return PRELUDE + f"QUOTA_ERROR='{error}'\nBACKUP_RESULT={backup_result}\n" + QUOTA_API


@pytest.mark.parametrize("error", ["capacity limit", "USER_DAILY_UPLOAD_QUOTA_EXCEEDED"])
def test_quota_prevents_reattachment_and_primary_work_but_preserves_backups(tmp_path: Path, error: str) -> None:
    result = run_bash(
        quota_script(error=error)
        + r'''
        printf photo > "${SOURCE_ROOT}/acct/original.jpeg"
        for name in a.fast.mp4 a.cover.jpg cover.jpg partial.mp4.part skipped.mp4 pending.mp4; do
          printf keep > "${SOURCE_ROOT}/acct/${name}"
        done
        touch -d @1 "${SOURCE_ROOT}/acct/"*
        printf recent > "${SOURCE_ROOT}/acct/recent.mp4"
        MIN_AGE_SECONDS=120
        SKIP_CONFLICTING_FILES=true
        mark_file_skipped "${SOURCE_ROOT}/acct/skipped.mp4"
        mark_file_pending "${SOURCE_ROOT}/acct/a.fast.mp4" quota-1
        mark_file_pending "${SOURCE_ROOT}/acct/pending.mp4" pending-2
        MAX_PENDING_TASKS=1
        process_files_batched token "$(date +%s)"
        [[ "${CAPACITY_LIMIT_HIT}" == 1 ]]
        [[ "${PROCESS_ERROR_COUNT}" == 0 ]]
        [[ "$(pending_record_count)" == 1 ]]
        [[ ! -s "${PRIMARY_CALLS}" && ! -s "${REATTACH_CALLS}" ]]
        ''',
        tmp_path,
    )
    assert "仅继续夸克备份" in result.stderr
    local = tmp_path / "source/acct"
    backup = tmp_path / "backup-cloud"
    assert (backup / "a.mp4").read_bytes() == (local / "a.mp4").read_bytes() == b"abcd"
    assert (backup / "original.jpeg").read_bytes() == (local / "original.jpeg").read_bytes() == b"photo"
    assert sorted(path.name for path in backup.iterdir()) == ["a.mp4", "original.jpeg"]
    assert len(list(local.iterdir())) == 9
    assert not (tmp_path / "move-calls").read_text()


@pytest.mark.parametrize("backup_result", ["failed", "unconfirmed"])
def test_quota_backup_unconfirmed_is_staged_and_keeps_bytes(tmp_path: Path, backup_result: str) -> None:
    result = run_bash(
        quota_script(error="USER_DAILY_UPLOAD_QUOTA_EXCEEDED", backup_result=backup_result)
        + r"""
        CAPACITY_LIMIT_HIT=1
        MAX_NEW_FILES_PER_RUN=1
        submit_new_files_batched token "$(date +%s)"
        [[ "${PROCESS_ERROR_COUNT}" == 0 ]]
        [[ ! -s "${PRIMARY_CALLS}" ]]
        # 已暂存的文件下一轮不再消耗额度、不重复 copy
        submit_new_files_batched token "$(date +%s)"
        """,
        tmp_path,
    )
    assert "staged=1, failed=0" in result.stderr
    assert (tmp_path / "source/acct/a.mp4").read_bytes() == b"abcd"
    assert (tmp_path / "source/.backup-staging/acct/a.mp4").samefile(tmp_path / "source/acct/a.mp4")
    assert not list((tmp_path / "backup-cloud").iterdir())
    assert (tmp_path / "copy-calls").read_text().splitlines() == ["a.mp4"]


def test_quota_backup_staging_failure_reports_failure(tmp_path: Path) -> None:
    result = run_bash(
        quota_script(error="USER_DAILY_UPLOAD_QUOTA_EXCEEDED")
        + r"""
        printf x > "${SOURCE_ROOT}/.backup-staging"
        list_source_files() { printf '%s\0' "${SOURCE_ROOT}/acct/a.mp4"; }
        CAPACITY_LIMIT_HIT=1
        submit_new_files_batched token "$(date +%s)"
        [[ "${PROCESS_ERROR_COUNT}" == 1 ]]
        """,
        tmp_path,
    )
    assert "staged=0, failed=1" in result.stderr
    assert (tmp_path / "source/acct/a.mp4").read_bytes() == b"abcd"
    assert not (tmp_path / "copy-calls").read_text()


def test_quota_backup_limit_advances_past_existing_copies(tmp_path: Path) -> None:
    run_bash(
        quota_script(error="capacity limit")
        + r'''
        printf second > "${SOURCE_ROOT}/acct/b.mp4"
        printf photo > "${SOURCE_ROOT}/acct/c.png"
        cp -- "${SOURCE_ROOT}/acct/a.mp4" "${BACKUP_CLOUD}/a.mp4"
        list_source_files() {
          printf '%s\0' "${SOURCE_ROOT}/acct/a.mp4" "${SOURCE_ROOT}/acct/b.mp4" "${SOURCE_ROOT}/acct/c.png"
        }
        CAPACITY_LIMIT_HIT=1
        MAX_NEW_FILES_PER_RUN=1
        submit_new_files_batched token "$(date +%s)"
        [[ "$(cat "${COPY_CALLS_FILE}")" == b.mp4 ]]
        submit_new_files_batched token "$(date +%s)"
        [[ "$(wc -l < "${COPY_CALLS_FILE}")" == 2 ]]
        [[ ! -s "${PRIMARY_CALLS}" ]]
        ''',
        tmp_path,
    )
    for name in ("a.mp4", "b.mp4", "c.png"):
        assert (tmp_path / "backup-cloud" / name).read_bytes() == (tmp_path / "source/acct" / name).read_bytes()
    assert (tmp_path / "copy-calls").read_text().splitlines() == ["b.mp4", "c.png"]


def test_quota_backup_respects_remaining_budget_and_file_pattern(tmp_path: Path) -> None:
    run_bash(
        quota_script(error="capacity limit")
        + r'''
        printf photo > "${SOURCE_ROOT}/acct/original.jpg"
        FILE_PATTERN='*.mp4'
        MAX_NEW_FILES_PER_RUN=1
        backup_local_originals_without_move token "$(date +%s)" 1
        [[ ! -s "${COPY_CALLS_FILE}" ]]
        backup_local_originals_without_move token "$(date +%s)" 0
        [[ "$(cat "${COPY_CALLS_FILE}")" == a.mp4 ]]
        [[ ! -s "${PRIMARY_CALLS}" ]]
        ''',
        tmp_path,
    )
    assert (tmp_path / "source/acct/a.mp4").read_bytes() == b"abcd"
    assert (tmp_path / "source/acct/original.jpg").read_bytes() == b"photo"
    assert sorted(path.name for path in (tmp_path / "backup-cloud").iterdir()) == ["a.mp4"]


def test_quota_during_conflict_fallback_stops_remaining_batches(tmp_path: Path) -> None:
    run_bash(
        PRELUDE
        + r'''
        init_batch_buffers
        batch_add_file first "${SOURCE_ROOT}/acct/a.mp4" /qbt/source/acct /d2/first a.mp4
        batch_add_file first "${SOURCE_ROOT}/acct/b.mp4" /qbt/source/acct /d2/first b.mp4
        batch_add_file second "${SOURCE_ROOT}/acct/c.mp4" /qbt/source/acct /d2/second c.mp4
        ensure_remote_dir() { :; }
        move_remote_files() { printf 'move\n' >> "${MOVE_CALLS_FILE}"; return 2; }
        process_one_file() { printf 'reconcile\n' >> "${COPY_CALLS_FILE}"; printf '%s' "${ACTION_CAPACITY}"; }
        flush_all_batches token
        [[ "${CAPACITY_LIMIT_HIT}" == 1 ]]
        [[ "$(wc -l < "${MOVE_CALLS_FILE}")" == 1 ]]
        [[ "$(wc -l < "${COPY_CALLS_FILE}")" == 1 ]]
        [[ "${BATCH_COUNTS[second]}" == 1 ]]
        ''',
        tmp_path,
    )
    assert (tmp_path / "source/acct/a.mp4").read_bytes() == b"abcd"


def test_legacy_quota_stops_primary_work_and_continues_backup(tmp_path: Path) -> None:
    run_bash(
        quota_script(error="USER_DAILY_UPLOAD_QUOTA_EXCEEDED")
        + r'''
        printf playback > "${SOURCE_ROOT}/acct/a.fast.mp4"
        mark_file_pending "${SOURCE_ROOT}/acct/a.fast.mp4" quota-1
        list_candidate_files() {
          printf '%s\0' "${SOURCE_ROOT}/acct/a.fast.mp4" "${SOURCE_ROOT}/acct/a.mp4"
        }
        MOVE_BATCH_SIZE=1
        MAX_PENDING_TASKS=1
        process_files_legacy token "$(date +%s)"
        [[ "${CAPACITY_LIMIT_HIT}" == 1 ]]
        [[ "${PROCESS_ERROR_COUNT}" == 0 ]]
        [[ ! -s "${PRIMARY_CALLS}" && ! -s "${REATTACH_CALLS}" ]]
        ''',
        tmp_path,
    )
    assert (tmp_path / "source/acct/a.mp4").read_bytes() == b"abcd"
    assert (tmp_path / "backup-cloud/a.mp4").read_bytes() == b"abcd"
    assert not (tmp_path / "move-calls").read_text()
