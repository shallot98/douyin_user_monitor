"""双目的地搬运：主力盘 move 与备份盘 copy 并行，暂存区硬链接兜住备份数据。"""

import os
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

PRELUDE = r'''
set -euo pipefail
SCRIPT_DIR="${REPO_ROOT}/scripts"
SCRIPT_BASENAME="openlist-auto-move-mp4.sh"
DEFAULT_CONFIG_FILE="${SCRIPT_DIR}/openlist-auto-move.conf"
OPENLIST_MOVER_PROJECT_ROOT="${TMP_ROOT}/project"
mkdir -p "${OPENLIST_MOVER_PROJECT_ROOT}/runtime"

source "${SCRIPT_DIR}/openlist-auto-move/common.sh"
source "${SCRIPT_DIR}/openlist-auto-move/state.sh"
source "${SCRIPT_DIR}/openlist-auto-move/api.sh"
source "${SCRIPT_DIR}/openlist-auto-move/remote_cache.sh"
source "${SCRIPT_DIR}/openlist-auto-move/remote.sh"
source "${SCRIPT_DIR}/openlist-auto-move/paths.sh"
source "${SCRIPT_DIR}/openlist-auto-move/backup.sh"
source "${SCRIPT_DIR}/openlist-auto-move/quota.sh"
source "${SCRIPT_DIR}/openlist-auto-move/reconcile.sh"
source "${SCRIPT_DIR}/openlist-auto-move/dryrun.sh"
source "${SCRIPT_DIR}/openlist-auto-move/runner.sh"
source "${SCRIPT_DIR}/openlist-auto-move/runner_legacy.sh"
source "${SCRIPT_DIR}/openlist-auto-move/runner_batch.sh"

SOURCE_ROOT="${TMP_ROOT}/source"
SOURCE_MOUNT="/qbt/source"
TARGET_BASE="/wopan/target"
BACKUP_TARGET_BASE="/kuake/target"
PENDING_FILE="${TMP_ROOT}/runtime/pending"
SKIPPED_FILE="${TMP_ROOT}/runtime/skipped"
COPY_CALLS_FILE="${TMP_ROOT}/copy-calls"
MOVE_CALLS_FILE="${TMP_ROOT}/move-calls"
BACKUP_STATE_FILE="${TMP_ROOT}/backup-present"
mkdir -p "${SOURCE_ROOT}/acct" "${TMP_ROOT}/runtime"
: > "${COPY_CALLS_FILE}"
: > "${MOVE_CALLS_FILE}"
printf abcd > "${SOURCE_ROOT}/acct/a.mp4"

FILE_PATTERN="*"
MIN_AGE_SECONDS=0
MOVE_BATCH_SIZE=50
MAX_PENDING_TASKS=2000
MAX_NEW_FILES_PER_RUN=500
MAX_RECONCILE_FILES_PER_RUN=1000
VERIFY_AFTER_MOVE="false"
REMOTE_LIST_CACHE_ENABLED="false"
SIZE_MISMATCH_TOLERANCE_BYTES=0
'''


def run_bash(script: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["REPO_ROOT"] = str(REPO_ROOT)
    env["TMP_ROOT"] = str(tmp_path)
    return subprocess.run(
        ["bash", "-c", textwrap.dedent(script)],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )


def test_backup_copy_precedes_move_and_local_is_released(tmp_path: Path) -> None:
    """备份确认后才提交 move；两个目的地都收到同一个文件。"""
    result = run_bash(
        PRELUDE
        + r'''
        # 备份盘一开始为空；copy 提交后标记为已存在
        api_post_json() {
          local path="$2" payload="$3"
          case "${path}" in
            "/api/fs/mkdir") printf '{"code":200}' ;;
            "/api/fs/list")
              local dir
              dir=$(jq -r '.path' <<< "${payload}")
              if [[ "${dir}" == /kuake/* && -f "${BACKUP_STATE_FILE}" ]]; then
                jq -cn '{code:200,data:{total:1,content:[{name:"a.mp4",size:4}]}}'
              else
                jq -cn '{code:200,data:{total:0,content:[]}}'
              fi
              ;;
            "/api/fs/copy")
              jq -r '.dst_dir' <<< "${payload}" >> "${COPY_CALLS_FILE}"
              : > "${BACKUP_STATE_FILE}"
              jq -cn '{code:200,data:{tasks:[{id:"copy-1"}]}}'
              ;;
            "/api/fs/move")
              jq -r '.dst_dir' <<< "${payload}" >> "${MOVE_CALLS_FILE}"
              jq -cn '{code:200,data:{tasks:[{id:"move-1"}]}}'
              ;;
            *) printf 'unexpected: %s\n' "${path}" >&2; return 1 ;;
          esac
        }

        process_files_batched "token" "$(date +%s)"

        # 备份先发生，move 后发生，且各只一次
        [[ "$(wc -l < "${COPY_CALLS_FILE}")" == "1" ]]
        [[ "$(wc -l < "${MOVE_CALLS_FILE}")" == "1" ]]
        [[ "$(cat "${COPY_CALLS_FILE}")" == "/kuake/target/acct" ]]
        [[ "$(cat "${MOVE_CALLS_FILE}")" == "/wopan/target/acct" ]]
        printf 'copy=%s move=%s\n' "$(cat "${COPY_CALLS_FILE}")" "$(cat "${MOVE_CALLS_FILE}")"
        ''',
        tmp_path,
    )
    assert "copy=/kuake/target/acct move=/wopan/target/acct" in result.stdout


def test_move_proceeds_while_backup_unconfirmed_and_staging_keeps_bytes(tmp_path: Path) -> None:
    """备份没确认也照常 move；暂存区硬链接保住数据，不会出现「本地已删、备份没有」。"""
    result = run_bash(
        PRELUDE
        + r"""
        # copy 返回成功但备份盘始终查不到文件（模拟上传尚未完成）
        api_post_json() {
          local path="$2" payload="$3"
          case "${path}" in
            "/api/fs/mkdir") printf '{"code":200}' ;;
            "/api/fs/list") jq -cn '{code:200,data:{total:0,content:[]}}' ;;
            "/api/fs/copy")
              jq -r '.src_dir' <<< "${payload}" >> "${COPY_CALLS_FILE}"
              jq -cn '{code:200,data:{tasks:[{id:"copy-1"}]}}'
              ;;
            "/api/fs/move")
              jq -r '.dst_dir' <<< "${payload}" >> "${MOVE_CALLS_FILE}"
              jq -cn '{code:200,data:{tasks:[{id:"move-1"}]}}'
              ;;
            *) printf 'unexpected: %s\n' "${path}" >&2; return 1 ;;
          esac
        }

        process_files_batched "token" "$(date +%s)"

        [[ "$(cat "${COPY_CALLS_FILE}")" == "/qbt/source/.backup-staging/acct" ]]
        [[ "$(cat "${MOVE_CALLS_FILE}")" == "/wopan/target/acct" ]]
        staged="${SOURCE_ROOT}/.backup-staging/acct/a.mp4"
        [[ "${staged}" -ef "${SOURCE_ROOT}/acct/a.mp4" ]]
        # move 释放本地原路径后，暂存链接仍持有数据
        rm -- "${SOURCE_ROOT}/acct/a.mp4"
        [[ "$(cat "${staged}")" == "abcd" ]]
        printf 'move_calls=%s staged_kept=yes\n' "$(wc -l < "${MOVE_CALLS_FILE}")"
        """,
        tmp_path,
    )
    assert "move_calls=1 staged_kept=yes" in result.stdout


def test_already_staged_file_is_not_copied_twice(tmp_path: Path) -> None:
    """同一文件再次进入 ensure_backup_copy（如配额熔断后恢复）不重复提交 copy。"""
    run_bash(
        PRELUDE
        + r"""
        api_post_json() {
          local path="$2" payload="$3"
          case "${path}" in
            "/api/fs/mkdir") printf '{"code":200}' ;;
            "/api/fs/list") jq -cn '{code:200,data:{total:0,content:[]}}' ;;
            "/api/fs/copy")
              jq -r '.src_dir' <<< "${payload}" >> "${COPY_CALLS_FILE}"
              jq -cn '{code:200,data:{tasks:[{id:"copy-1"}]}}'
              ;;
            *) printf 'unexpected: %s\n' "${path}" >&2; return 1 ;;
          esac
        }
        ensure_backup_copy token "${SOURCE_ROOT}/acct/a.mp4" /qbt/source/acct /wopan/target/acct a.mp4
        ensure_backup_copy token "${SOURCE_ROOT}/acct/a.mp4" /qbt/source/acct /wopan/target/acct a.mp4
        [[ "$(wc -l < "${COPY_CALLS_FILE}")" == "1" ]]
        """,
        tmp_path,
    )


def test_staging_failure_blocks_move_and_keeps_local(tmp_path: Path) -> None:
    """硬链接暂存失败时绝不提交 move。"""
    result = run_bash(
        PRELUDE
        + r"""
        # 让暂存区根路径是个普通文件，mkdir -p 必然失败
        printf x > "${SOURCE_ROOT}/.backup-staging"
        api_post_json() {
          local path="$2" payload="$3"
          case "${path}" in
            "/api/fs/mkdir") printf '{"code":200}' ;;
            "/api/fs/list") jq -cn '{code:200,data:{total:0,content:[]}}' ;;
            "/api/fs/copy") jq -r '.src_dir' <<< "${payload}" >> "${COPY_CALLS_FILE}"; printf '{"code":200}' ;;
            "/api/fs/move") jq -r '.dst_dir' <<< "${payload}" >> "${MOVE_CALLS_FILE}"; printf '{"code":200}' ;;
            *) printf 'unexpected: %s\n' "${path}" >&2; return 1 ;;
          esac
        }
        list_source_files() { printf '%s\0' "${SOURCE_ROOT}/acct/a.mp4"; }
        process_files_batched "token" "$(date +%s)"
        [[ ! -s "${COPY_CALLS_FILE}" && ! -s "${MOVE_CALLS_FILE}" ]]
        [[ -f "${SOURCE_ROOT}/acct/a.mp4" ]]
        printf 'blocked\n'
        """,
        tmp_path,
    )
    assert "blocked" in result.stdout
    assert "备份暂存失败" in result.stderr


def test_list_source_files_skips_staging(tmp_path: Path) -> None:
    result = run_bash(
        PRELUDE
        + r"""
        mkdir -p "${SOURCE_ROOT}/.backup-staging/acct"
        ln "${SOURCE_ROOT}/acct/a.mp4" "${SOURCE_ROOT}/.backup-staging/acct/a.mp4"
        list_source_files | tr '\0' '\n'
        FILE_PATTERN="*.mp4"
        list_source_files | tr '\0' '\n'
        """,
        tmp_path,
    )
    assert result.stdout.splitlines() == [str(tmp_path / "source/acct/a.mp4")] * 2


DRAIN_API = r"""
UNDONE_FILE="${TMP_ROOT}/undone"
: > "${UNDONE_FILE}"
mkdir -p "${SOURCE_ROOT}/.backup-staging/acct/sub" "${TMP_ROOT}/cloud"
printf abcd > "${SOURCE_ROOT}/.backup-staging/acct/a.mp4"
printf 12345 > "${SOURCE_ROOT}/.backup-staging/acct/sub/b.jpg"
printf zz > "${SOURCE_ROOT}/.backup-staging/acct/sub/c.jpg"
api_get_json() {
  if [[ -n "${UNDONE_FAIL:-}" ]]; then return 1; fi
  jq -Rn '{code:200,data:[inputs | {name:.}]}' < "${UNDONE_FILE}"
}
api_post_json() {
  local path="$2" payload="$3" dir
  case "${path}" in
    "/api/fs/mkdir") printf '{"code":200}' ;;
    "/api/fs/list")
      dir=$(jq -r '.path' <<< "${payload}")
      find "${TMP_ROOT}/cloud" -type f -path "${TMP_ROOT}/cloud${dir#/kuake/target}/*" -printf '%f\t%s\n' |
        jq -Rn '[inputs | split("\t") | {name:.[0],size:(.[1]|tonumber)}] | {code:200,data:{content:.,total:length}}'
      ;;
    "/api/fs/copy")
      jq -r '"\(.src_dir)/\(.names[0])"' <<< "${payload}" >> "${COPY_CALLS_FILE}"
      jq -cn '{code:200,data:{tasks:[{id:"copy-x"}]}}'
      ;;
    *) printf 'unexpected: %s\n' "${path}" >&2; return 1 ;;
  esac
}
"""


def test_drain_releases_confirmed_skips_inflight_and_resubmits_rest(tmp_path: Path) -> None:
    result = run_bash(
        PRELUDE
        + DRAIN_API
        + r"""
        # a.mp4 已落备份盘；b.jpg 有在途 copy；c.jpg 大小不一致需重提
        mkdir -p "${TMP_ROOT}/cloud/acct/sub"
        printf abcd > "${TMP_ROOT}/cloud/acct/a.mp4"
        printf z > "${TMP_ROOT}/cloud/acct/sub/c.jpg"
        build_move_task_name /qbt/source/.backup-staging/acct/sub /kuake/target/acct/sub b.jpg copy > "${UNDONE_FILE}"
        drain_backup_staging token
        [[ ! -e "${SOURCE_ROOT}/.backup-staging/acct/a.mp4" ]]
        [[ -f "${SOURCE_ROOT}/.backup-staging/acct/sub/b.jpg" ]]
        [[ -f "${SOURCE_ROOT}/.backup-staging/acct/sub/c.jpg" ]]
        cat "${COPY_CALLS_FILE}"
        """,
        tmp_path,
    )
    assert result.stdout.splitlines() == ["/qbt/source/.backup-staging/acct/sub/c.jpg"]
    assert "已确认释放=1, copy在途=1, 重新提交=1" in result.stderr


def test_drain_respects_resubmit_limit_and_prunes_empty_dirs(tmp_path: Path) -> None:
    result = run_bash(
        PRELUDE
        + DRAIN_API
        + r"""
        BACKUP_RESUBMIT_PER_RUN=1
        drain_backup_staging token
        [[ "$(wc -l < "${COPY_CALLS_FILE}")" == "1" ]]
        # 全部落盘后暂存区清空，空目录一并删掉，但保留暂存根
        mkdir -p "${TMP_ROOT}/cloud/acct/sub"
        cp "${SOURCE_ROOT}/.backup-staging/acct/a.mp4" "${TMP_ROOT}/cloud/acct/"
        cp "${SOURCE_ROOT}/.backup-staging/acct/sub/"*.jpg "${TMP_ROOT}/cloud/acct/sub/"
        drain_backup_staging token
        [[ -d "${SOURCE_ROOT}/.backup-staging" ]]
        [[ -z "$(ls -A "${SOURCE_ROOT}/.backup-staging")" ]]
        printf ok\n
        """,
        tmp_path,
    )
    assert "ok" in result.stdout
    assert "重新提交=1, 延后=2" in result.stderr


def test_drain_does_not_resubmit_when_inflight_query_fails(tmp_path: Path) -> None:
    result = run_bash(
        PRELUDE
        + DRAIN_API
        + r"""
        UNDONE_FAIL=1
        drain_backup_staging token
        [[ ! -s "${COPY_CALLS_FILE}" ]]
        [[ "$(find "${SOURCE_ROOT}/.backup-staging" -type f | wc -l)" == "3" ]]
        """,
        tmp_path,
    )
    assert "只确认不重提" in result.stderr
    assert "延后=3" in result.stderr


def test_backup_skipped_when_already_present_with_same_size(tmp_path: Path) -> None:
    """历史文件已在备份盘：不重复复制，直接放行 move。"""
    result = run_bash(
        PRELUDE
        + r'''
        api_post_json() {
          local path="$2" payload="$3"
          case "${path}" in
            "/api/fs/mkdir") printf '{"code":200}' ;;
            "/api/fs/list")
              local dir
              dir=$(jq -r '.path' <<< "${payload}")
              if [[ "${dir}" == /kuake/* ]]; then
                jq -cn '{code:200,data:{total:1,content:[{name:"a.mp4",size:4}]}}'
              else
                jq -cn '{code:200,data:{total:0,content:[]}}'
              fi
              ;;
            "/api/fs/copy")
              jq -r '.dst_dir' <<< "${payload}" >> "${COPY_CALLS_FILE}"
              jq -cn '{code:200,data:{tasks:[{id:"copy-1"}]}}'
              ;;
            "/api/fs/move")
              jq -r '.dst_dir' <<< "${payload}" >> "${MOVE_CALLS_FILE}"
              jq -cn '{code:200,data:{tasks:[{id:"move-1"}]}}'
              ;;
            *) printf 'unexpected: %s\n' "${path}" >&2; return 1 ;;
          esac
        }

        process_files_batched "token" "$(date +%s)"

        [[ ! -s "${COPY_CALLS_FILE}" ]]
        [[ "$(wc -l < "${MOVE_CALLS_FILE}")" == "1" ]]
        printf 'copy_calls=%s move_calls=%s\n' "$(wc -l < "${COPY_CALLS_FILE}")" "$(wc -l < "${MOVE_CALLS_FILE}")"
        ''',
        tmp_path,
    )
    assert "copy_calls=0 move_calls=1" in result.stdout


def test_disabled_backup_keeps_single_destination_behaviour(tmp_path: Path) -> None:
    """BACKUP_TARGET_BASE 留空 = 旧行为，不发任何 copy。"""
    result = run_bash(
        PRELUDE
        + r'''
        BACKUP_TARGET_BASE=""
        api_post_json() {
          local path="$2" payload="$3"
          case "${path}" in
            "/api/fs/mkdir") printf '{"code":200}' ;;
            "/api/fs/list") jq -cn '{code:200,data:{total:0,content:[]}}' ;;
            "/api/fs/copy")
              jq -r '.dst_dir' <<< "${payload}" >> "${COPY_CALLS_FILE}"
              jq -cn '{code:200,data:{tasks:[{id:"copy-1"}]}}'
              ;;
            "/api/fs/move")
              jq -r '.dst_dir' <<< "${payload}" >> "${MOVE_CALLS_FILE}"
              jq -cn '{code:200,data:{tasks:[{id:"move-1"}]}}'
              ;;
            *) printf 'unexpected: %s\n' "${path}" >&2; return 1 ;;
          esac
        }

        process_files_batched "token" "$(date +%s)"

        [[ ! -s "${COPY_CALLS_FILE}" ]]
        [[ "$(wc -l < "${MOVE_CALLS_FILE}")" == "1" ]]
        printf 'no_backup copy=%s move=%s\n' "$(wc -l < "${COPY_CALLS_FILE}")" "$(cat "${MOVE_CALLS_FILE}")"
        ''',
        tmp_path,
    )
    assert "no_backup copy=0 move=/wopan/target/acct" in result.stdout


def test_target_base_switch_is_blocked_while_tasks_are_in_flight(tmp_path: Path) -> None:
    """pending 记录不含目标目录，切换主力盘前必须先排空。"""
    result = run_bash(
        PRELUDE
        + r'''
        marker="${PENDING_FILE}.target"
        printf '/kuake/target' > "${marker}"
        printf 'somefile\ttask-1\t1\t0\t1\t0\n' > "${PENDING_FILE}"

        set +e
        assert_target_base_stable
        rc=$?
        set -e
        [[ "${rc}" != "0" ]]
        # 标记文件不该被改写
        [[ "$(cat "${marker}")" == "/kuake/target" ]]

        # 排空后放行，并记下新的基路径
        : > "${PENDING_FILE}"
        assert_target_base_stable
        [[ "$(cat "${marker}")" == "/wopan/target" ]]
        printf 'guard_blocked=%s then_allowed=ok\n' "${rc}"
        ''',
        tmp_path,
    )
    assert "then_allowed=ok" in result.stdout
    assert "guard_blocked=1" in result.stdout


def test_d2_direct_upload_stages_backup_and_cleans_local_source(tmp_path: Path) -> None:
    """开启 D2_DIRECT_UPLOAD 时，先硬链接进暂存区并提交夸克 copy，再直传 D2 并删除本地源文件。"""
    result = run_bash(
        PRELUDE
        + r'''
        TARGET_BASE="/d2/target"
        D2_DIRECT_UPLOAD="true"
        D2_DIRECT_CALLS_FILE="${TMP_ROOT}/d2-direct-calls"
        : > "${D2_DIRECT_CALLS_FILE}"

        d2_direct_upload_file() {
          local local_file_path="$1" dst_dir="$2" file_name="$3"
          printf '%s\t%s\n' "${dst_dir}" "${file_name}" >> "${D2_DIRECT_CALLS_FILE}"
          rm -f -- "${local_file_path}"
          unmark_file_pending "${local_file_path}"
          return 0
        }

        api_post_json() {
          local path="$2" payload="$3"
          case "${path}" in
            "/api/fs/mkdir") printf '{"code":200}' ;;
            "/api/fs/list") jq -cn '{code:200,data:{total:0,content:[]}}' ;;
            "/api/fs/copy")
              jq -r '.dst_dir' <<< "${payload}" >> "${COPY_CALLS_FILE}"
              jq -cn '{code:200,data:{tasks:[{id:"copy-1"}]}}'
              ;;
            "/api/fs/move")
              printf 'unexpected /api/fs/move call\n' >&2
              return 1
              ;;
            *) printf 'unexpected: %s\n' "${path}" >&2; return 1 ;;
          esac
        }

        process_files_batched "token" "$(date +%s)"

        # 本地原路径已删，但暂存区硬链接保留以供夸克 copy 读取
        [[ ! -f "${SOURCE_ROOT}/acct/a.mp4" ]]
        [[ -f "${SOURCE_ROOT}/.backup-staging/acct/a.mp4" ]]
        [[ "$(cat "${SOURCE_ROOT}/.backup-staging/acct/a.mp4")" == "abcd" ]]
        [[ "$(cat "${COPY_CALLS_FILE}")" == "/kuake/target/acct" ]]
        [[ "$(cat "${D2_DIRECT_CALLS_FILE}")" == $'/d2/target/acct\ta.mp4' ]]
        printf 'moved=%s errors=%s\n' "${PROCESS_MOVED_COUNT}" "${PROCESS_ERROR_COUNT}"
        ''',
        tmp_path,
    )
    assert "moved=1 errors=0" in result.stdout



def test_trailing_space_dir_is_trimmed_for_targets_but_source_keeps_original(tmp_path: Path) -> None:
    """标题截断留下尾空格的图集目录：D2 与夸克目标去掉尾空格，/qbt 源路径和本地文件保持原名。"""
    result = run_bash(
        PRELUDE
        + r'''
        TARGET_BASE="/d2/target"
        D2_DIRECT_UPLOAD="true"
        D2_DIRECT_CALLS_FILE="${TMP_ROOT}/d2-direct-calls"
        COPY_SRC_FILE="${TMP_ROOT}/copy-src"
        : > "${D2_DIRECT_CALLS_FILE}"
        : > "${COPY_SRC_FILE}"
        rm -f "${SOURCE_ROOT}/acct/a.mp4"
        mkdir -p "${SOURCE_ROOT}/acct/图片/标题截断 "
        printf abcd > "${SOURCE_ROOT}/acct/图片/标题截断 /1.jpg"

        mapfile -t ctx < <(build_remote_paths "${SOURCE_ROOT}/acct/图片/标题截断 /1.jpg")
        [[ "${ctx[0]}" == "/qbt/source/acct/图片/标题截断 " ]]
        [[ "${ctx[1]}" == "/d2/target/acct/图片/标题截断" ]]
        [[ "$(backup_dir_for_local_file "${SOURCE_ROOT}/acct/图片/标题截断 /1.jpg")" == "/kuake/target/acct/图片/标题截断" ]]

        d2_direct_upload_file() {
          local local_file_path="$1" dst_dir="$2" file_name="$3"
          printf '%s\t%s\n' "${dst_dir}" "${file_name}" >> "${D2_DIRECT_CALLS_FILE}"
          rm -f -- "${local_file_path}"
          unmark_file_pending "${local_file_path}"
          return 0
        }

        api_post_json() {
          local path="$2" payload="$3"
          case "${path}" in
            "/api/fs/mkdir") printf '{"code":200}' ;;
            "/api/fs/list") jq -cn '{code:200,data:{total:0,content:[]}}' ;;
            "/api/fs/copy")
              jq -r '.dst_dir' <<< "${payload}" >> "${COPY_CALLS_FILE}"
              jq -r '.src_dir' <<< "${payload}" >> "${COPY_SRC_FILE}"
              jq -cn '{code:200,data:{tasks:[{id:"copy-1"}]}}'
              ;;
            *) printf 'unexpected: %s\n' "${path}" >&2; return 1 ;;
          esac
        }

        process_files_batched "token" "$(date +%s)"

        [[ "$(cat "${D2_DIRECT_CALLS_FILE}")" == $'/d2/target/acct/图片/标题截断\t1.jpg' ]]
        [[ "$(cat "${COPY_CALLS_FILE}")" == "/kuake/target/acct/图片/标题截断" ]]
        # 夸克 copy 从暂存区读，暂存区保留原目录名
        [[ "$(cat "${COPY_SRC_FILE}")" == "/qbt/source/.backup-staging/acct/图片/标题截断 " ]]
        printf 'moved=%s errors=%s\n' "${PROCESS_MOVED_COUNT}" "${PROCESS_ERROR_COUNT}"
        ''',
        tmp_path,
    )
    assert "moved=1 errors=0" in result.stdout


def test_d2_transient_5xx_is_retried_without_counting_as_error_until_limit(tmp_path: Path) -> None:
    """D2 网关间歇 500：本地文件保留、不计失败；同一文件连续达到上限才计为失败。

    也锁住：一次失败不会让整轮 mover 因 set -e 提前退出。
    """
    result = run_bash(
        PRELUDE
        + r'''
        TARGET_BASE="/d2/target"
        D2_DIRECT_UPLOAD="true"
        D2_TRANSIENT_MAX_ATTEMPTS=3
        D2_DIRECT_PYTHON="${TMP_ROOT}/fake-python"
        printf '#!/usr/bin/env bash\nprintf %s\nexit 1\n' "'{\"error\":\"D2 block upload: HTTP 500\"}'" > "${D2_DIRECT_PYTHON}"
        chmod +x "${D2_DIRECT_PYTHON}"
        OPENLIST_DB="${TMP_ROOT}/none.db"

        api_post_json() {
          local path="$2"
          case "${path}" in
            "/api/fs/mkdir") printf '{"code":200}' ;;
            "/api/fs/list") jq -cn '{code:200,data:{total:0,content:[]}}' ;;
            "/api/fs/copy") jq -cn '{code:200,data:{tasks:[{id:"copy-1"}]}}' ;;
            *) printf 'unexpected: %s\n' "${path}" >&2; return 1 ;;
          esac
        }

        for round in 1 2 3; do
          process_files_batched "token" "$(date +%s)"
          printf 'round%s errors=%s\n' "${round}" "${PROCESS_ERROR_COUNT}"
        done
        [[ -f "${SOURCE_ROOT}/acct/a.mp4" ]]
        ''',
        tmp_path,
    )
    assert "round1 errors=0" in result.stdout
    assert "round2 errors=0" in result.stdout
    assert "round3 errors=1" in result.stdout
