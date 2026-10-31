#!/usr/bin/env bash

if [[ -n "${OPENLIST_MOVER_REMOTE_LOADED:-}" ]]; then
  return 0
fi
OPENLIST_MOVER_REMOTE_LOADED=1

REMOTE_SRC_STATUS=1
REMOTE_DST_STATUS=1

find_remote_entry_json() {
  local token="$1"
  local target_dir="$2"
  local file_name="$3"
  local page=1

  while :; do
    local resp entry_json page_count total_count
    load_remote_dir_page "${token}" "${target_dir}" "${page}" || return 2
    resp="${REMOTE_LIST_PAGE_JSON}"
    entry_json=$(jq -c --arg n "${file_name}" '.data.content // [] | map(select(.name == $n)) | .[0] // empty' <<< "${resp}")
    if [[ -n "${entry_json}" && "${entry_json}" != "null" ]]; then
      printf '%s' "${entry_json}"
      return 0
    fi

    page_count=$(jq -r '.data.content // [] | length' <<< "${resp}")
    total_count=$(jq -r '.data.total // 0' <<< "${resp}")
    if [[ "${total_count}" =~ ^[0-9]+$ ]] && (( page * REMOTE_LIST_PAGE_SIZE >= total_count )); then
      return 1
    fi
    if (( page_count < REMOTE_LIST_PAGE_SIZE )); then
      return 1
    fi
    page=$((page + 1))
  done
}

remote_entry_status() {
  local token="$1"
  local target_dir="$2"
  local file_name="$3"
  local rc=0

  if find_remote_entry_json "${token}" "${target_dir}" "${file_name}" >/dev/null 2>&1; then
    printf '0'
    return 0
  else
    rc=$?
  fi
  case "${rc}" in
    1) printf '1' ;;
    *) printf '2' ;;
  esac
}

file_exists_in_dir() {
  local status
  status=$(remote_entry_status "$@")
  case "${status}" in
    0) return 0 ;;
    1) return 1 ;;
    *) return 2 ;;
  esac
}

get_remote_file_size() {
  local token="$1"
  local target_dir="$2"
  local file_name="$3"
  local entry_json
  entry_json=$(find_remote_entry_json "${token}" "${target_dir}" "${file_name}") || return 1
  jq -r '.size // empty' <<< "${entry_json}"
}

probe_remote_move_state() {
  local token="$1"
  local src_dir="$2"
  local dst_dir="$3"
  local file_name="$4"

  REMOTE_SRC_STATUS=$(remote_entry_status "${token}" "${src_dir}" "${file_name}")
  REMOTE_DST_STATUS=$(remote_entry_status "${token}" "${dst_dir}" "${file_name}")

  if [[ "${REMOTE_SRC_STATUS}" -eq 2 || "${REMOTE_DST_STATUS}" -eq 2 ]]; then
    return 1
  fi
  return 0
}

resolve_existing_target_conflict() {
  local token="$1"
  local local_file_path="$2"
  local dst_dir="$3"
  local file_name="$4"

  if [[ ! -f "${local_file_path}" ]]; then
    log "本地源文件已不存在，无法比较大小: ${local_file_path}"
    return 1
  fi

  local local_size remote_size size_diff=0
  local_size=$(stat -c '%s' "${local_file_path}")
  remote_size=$(get_remote_file_size "${token}" "${dst_dir}" "${file_name}") || {
    log "读取目标文件信息失败: ${dst_dir}/${file_name}"
    return 1
  }
  if [[ -z "${remote_size}" ]]; then
    log "目标端未找到同名文件: ${dst_dir}/${file_name}"
    return 1
  fi

  if [[ "${local_size}" != "${remote_size}" ]]; then
    size_diff=$((local_size - remote_size))
    if (( size_diff < 0 )); then
      size_diff=$((-size_diff))
    fi
    if (( size_diff <= SIZE_MISMATCH_TOLERANCE_BYTES )); then
      local verify_rc=0
      verify_existing_target_content "${local_file_path}" "${dst_dir}" "${file_name}" || verify_rc=$?
      (( verify_rc == 0 )) || return "${verify_rc}"
      rm -f -- "${local_file_path}"
      log "目标端已存在同名且大小差异在容差内，已删除本地源文件: ${local_file_path} (local=${local_size}, remote=${remote_size}, tolerance=${SIZE_MISMATCH_TOLERANCE_BYTES})"
      return 0
    fi
    if [[ "${SKIP_CONFLICTING_FILES}" == "true" ]]; then
      mark_file_skipped "${local_file_path}"
      log "同名文件大小不一致，已加入跳过列表并继续后续任务: local=${local_size}, remote=${remote_size}, diff=${size_diff}, tolerance=${SIZE_MISMATCH_TOLERANCE_BYTES}, file=${local_file_path}"
      return 3
    fi

    log "同名文件大小不一致，拒绝删除本地源文件: local=${local_size}, remote=${remote_size}, diff=${size_diff}, tolerance=${SIZE_MISMATCH_TOLERANCE_BYTES}, file=${local_file_path}"
    return 1
  fi

  local verify_rc=0
  verify_existing_target_content "${local_file_path}" "${dst_dir}" "${file_name}" || verify_rc=$?
  (( verify_rc == 0 )) || return "${verify_rc}"
  rm -f -- "${local_file_path}"
  log "目标端已存在同名且大小一致，已删除本地源文件: ${local_file_path}"
  return 0
}

# 调 D2 比对本地与远端 content_identity；退出码 0 = 相同，3 = 不同，其他 = 无法确认
d2_verify_content() {
  local local_file_path="$1" d2_path="$2"
  local py_bin="${D2_DIRECT_PYTHON}"
  [[ -x "${py_bin}" ]] || py_bin="python3"
  PYTHONPATH="${OPENLIST_MOVER_PROJECT_DIR}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${py_bin}" -m scripts.cloud_playback.d2_verify \
    --openlist-db "${OPENLIST_DB}" --mount-path "/d2" \
    --file "${local_file_path}" --path "${d2_path}" 2>&1
}

# 删本地前的内容校验。只有 D2 目标能拿到内容标识（content_identity）；
# 其他盘 OpenList 不返回哈希，维持只比大小。
# 返回 0 = 可删；1 = 暂不能确认，下轮再试；3 = 内容不同，转 skipped 保留本地
verify_existing_target_content() {
  local local_file_path="$1" dst_dir="$2" file_name="$3"
  [[ "${D2_VERIFY_BEFORE_DELETE}" == "true" ]] || return 0
  [[ "${dst_dir}" == /d2/* || "${dst_dir}" == "/d2" ]] || return 0

  local d2_rel="${dst_dir#/d2}" out="" rc=0
  out=$(d2_verify_content "${local_file_path}" "${d2_rel}/${file_name}") || rc=$?
  case "${rc}" in
    0) return 0 ;;
    3)
      if mark_file_skipped "${local_file_path}"; then
        log "目标端同名同大小但内容不同，保留本地并加入跳过列表: ${local_file_path} (${out})"
        return 3
      fi
      log "目标端同名同大小但内容不同，保留本地（未开启 SKIP_CONFLICTING_FILES）: ${local_file_path} (${out})"
      return 1
      ;;
    *)
      log "目标端内容校验失败，本轮不删除本地: ${local_file_path} (${out})"
      return 1
      ;;
  esac
}

finalize_move_file() {
  local token="$1"
  local local_file_path="$2"
  local src_dir="$3"
  local dst_dir="$4"
  local file_name="$5"
  local reason="$6"
  local missing_target_action="${7:-${ACTION_WAIT}}"
  local local_exists="false"

  if [[ -f "${local_file_path}" ]]; then
    local_exists="true"
  fi
  if ! probe_remote_move_state "${token}" "${src_dir}" "${dst_dir}" "${file_name}"; then
    log "${reason}：查询源/目标文件状态失败，继续等待: ${local_file_path}"
    printf '%s' "${ACTION_WAIT}"
    return 0
  fi
  if [[ "${REMOTE_DST_STATUS}" -ne 0 ]]; then
    log "${reason}：目标端仍未确认存在，当前动作=${missing_target_action}: ${local_file_path}"
    printf '%s' "${missing_target_action}"
    return 0
  fi
  if [[ "${local_exists}" != "true" ]]; then
    unmark_file_pending "${local_file_path}"
    log "${reason}：目标端已确认存在，本地源文件已不存在，完成收口: ${local_file_path}"
    printf '%s' "${ACTION_COMPLETED}"
    return 0
  fi
  if ! ensure_backup_copy "${token}" "${local_file_path}" "${src_dir}" "${dst_dir}" "${file_name}"; then
    printf '%s' "${ACTION_WAIT}"
    return 0
  fi

  local cleanup_rc=1
  set +e
  resolve_existing_target_conflict "${token}" "${local_file_path}" "${dst_dir}" "${file_name}"
  cleanup_rc=$?
  set -e
  finalize_local_cleanup "${cleanup_rc}" "${local_file_path}" "${reason}"
}

finalize_local_cleanup() {
  local cleanup_rc="$1" local_file_path="$2" reason="$3"
  case "${cleanup_rc}" in
    0)
      unmark_file_pending "${local_file_path}"
      log "${reason}：目标端已确认存在，完成本地清理: ${local_file_path}"
      printf '%s' "${ACTION_COMPLETED}"
      ;;
    3)
      unmark_file_pending "${local_file_path}"
      log "${reason}：目标端冲突已转入 skipped: ${local_file_path}"
      printf '%s' "${ACTION_SKIP}"
      ;;
    *)
      log "${reason}：目标端存在但尚不能安全完成本地收口，继续等待: ${local_file_path}"
      printf '%s' "${ACTION_WAIT}"
      ;;
  esac
}

try_cleanup_existing_target() {
  local token="$1"
  local local_file_path="$2"
  local src_dir="$3"
  local dst_dir="$4"
  local file_name="$5"
  local target_rc=1

  target_rc=$(remote_entry_status "${token}" "${dst_dir}" "${file_name}")
  case "${target_rc}" in
    1) return 1 ;;
    2) return 2 ;;
  esac

  local action
  action=$(finalize_move_file "${token}" "${local_file_path}" "${src_dir}" "${dst_dir}" "${file_name}" "检测到目标端已存在同名文件" "${ACTION_WAIT}")
  case "${action}" in
    "${ACTION_COMPLETED}") return 0 ;;
    "${ACTION_SKIP}") return 3 ;;
    "${ACTION_WAIT}") return 2 ;;
    *) return 2 ;;
  esac
}

verify_move_finished() {
  local token="$1"
  local src_dir="$2"
  local dst_dir="$3"
  local file_name="$4"
  local attempt=1

  while (( attempt <= VERIFY_RETRIES )); do
    if probe_remote_move_state "${token}" "${src_dir}" "${dst_dir}" "${file_name}" \
      && [[ "${REMOTE_SRC_STATUS}" -eq 1 && "${REMOTE_DST_STATUS}" -eq 0 ]]; then
      return 0
    fi
    sleep "${VERIFY_INTERVAL_SECONDS}"
    attempt=$((attempt + 1))
  done

  log "移动任务未完成: ${file_name}（可能任务失败或云盘端尚未可见）"
  return 1
}
