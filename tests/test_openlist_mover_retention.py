from pathlib import Path

import pytest
from test_openlist_mover_backup import PRELUDE, run_bash

RETENTION_API = r'''
TARGET_BASE="/d2/target"
api_get_json() { printf '{"code":200,"data":[]}'; }
get_move_task_by_id() { printf '{"id":"move-1","state":2,"progress":100}'; }
api_post_json() {
  local endpoint="$2" payload="$3" dir size
  case "${endpoint}" in
    /api/fs/mkdir) printf '{"code":200}' ;;
    /api/fs/list)
      dir=$(jq -r '.path' <<< "${payload}")
      if [[ "${dir}" == /d2/* || "${dir}" == /qbt/* ]]; then
        jq -cn '{code:200,data:{total:1,content:[{name:"a.mp4",size:4}]}}'
      elif [[ -f "${BACKUP_STATE_FILE}" ]]; then
        size=$(stat -c '%s' -- "${BACKUP_STATE_FILE}")
        jq -cn --argjson size "${size}" '{code:200,data:{total:1,content:[{name:"a.mp4",size:$size}]}}'
      else
        jq -cn '{code:200,data:{total:0,content:[]}}'
      fi
      ;;
    /api/fs/copy)
      printf 'copy\n' >> "${COPY_CALLS_FILE}"
      [[ "$(jq -r '.src_dir' <<< "${payload}")" == /qbt/source/.backup-staging/acct ]] || return 1
      if [[ "${BACKUP_RESULT}" == confirmed ]]; then
        cp -- "${SOURCE_ROOT}/.backup-staging/acct/a.mp4" "${BACKUP_STATE_FILE}"
      elif [[ "${BACKUP_RESULT}" == failed ]]; then
        printf '{"code":500,"message":"backup upload failed"}'
        return 0
      fi
      printf '{"code":200,"data":{"tasks":[{"id":"copy-1"}]}}'
      ;;
    *) printf 'unexpected API: %s\n' "${endpoint}" >&2; return 1 ;;
  esac
}
if [[ "${BACKUP_RESULT}" == present ]]; then
  cp -- "${SOURCE_ROOT}/acct/a.mp4" "${BACKUP_STATE_FILE}"
fi
case "${AUDIT_MODE}" in
  batch) process_files_batched token "$(date +%s)" ;;
  pending)
    mark_file_pending "${SOURCE_ROOT}/acct/a.mp4" move-1
    process_one_file token "${SOURCE_ROOT}/acct/a.mp4"
    ;;
  single) process_one_file token "${SOURCE_ROOT}/acct/a.mp4" ;;
esac
'''


@pytest.mark.parametrize("mode", ["single", "batch", "pending"])
@pytest.mark.parametrize("backup_result", ["unconfirmed", "failed", "confirmed", "present"])
def test_existing_d2_copy_releases_local_once_backup_is_present_or_staged(
    tmp_path: Path, mode: str, backup_result: str
) -> None:
    run_bash(
        PRELUDE + f"AUDIT_MODE={mode}\nBACKUP_RESULT={backup_result}\n" + RETENTION_API,
        tmp_path,
    )
    local_file = tmp_path / "source/acct/a.mp4"
    staged_file = tmp_path / "source/.backup-staging/acct/a.mp4"
    backup_file = tmp_path / "backup-present"
    # d2 已有同名同大小文件：本地原路径释放，备份数据留在备份盘或暂存区
    assert not local_file.exists()
    if backup_result == "present":
        assert not staged_file.exists()
    else:
        assert staged_file.read_bytes() == b"abcd"
    if backup_result in {"confirmed", "present"}:
        assert backup_file.read_bytes() == b"abcd"
    else:
        assert not backup_file.exists()
    assert not (tmp_path / "move-calls").read_text()
    expected_copy_calls = 0 if backup_result == "present" else 1
    assert len((tmp_path / "copy-calls").read_text().splitlines()) == expected_copy_calls


def test_completed_move_with_no_local_source_does_not_require_new_backup(tmp_path: Path) -> None:
    result = run_bash(
        PRELUDE
        + r'''
        rm -- "${SOURCE_ROOT}/acct/a.mp4"
        probe_remote_move_state() { REMOTE_SRC_STATUS=1; REMOTE_DST_STATUS=0; }
        ensure_backup_copy() { printf 'unexpected backup\n' >&2; return 1; }
        action=$(finalize_move_file token "${SOURCE_ROOT}/acct/a.mp4" /qbt/source/acct /d2/target/acct a.mp4 done)
        [[ "${action}" == "${ACTION_COMPLETED}" ]]
        printf '%s' "${action}"
        ''',
        tmp_path,
    )
    assert result.stdout == "completed"
