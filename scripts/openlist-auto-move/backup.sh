#!/usr/bin/env bash

# 备份盘复制：主力盘 move 与备份盘 copy 并行，不再等备份盘确认才 move。
#
# 做法是「硬链接暂存」：提交 move 前先把本地文件硬链接进 SOURCE_ROOT 下的暂存区，
# 再从暂存区向备份盘提交 copy（不等结果）。move 删掉原路径后，暂存区的链接仍持有
# 同一份数据，copy 照样能读完。每轮开头 drain_backup_staging 收口：备份盘同名同大小
# 才删暂存链接，没确认的重新提交 copy。
#
# 因此任何时刻文件至少存在于「本地原路径」「暂存区」「备份盘」之一；
# 硬链接不占额外空间，本地空间在两个网盘都落地后才真正释放。
#
# 校验方式是查目标端真实存在且大小一致，而不是信任 copy 任务状态：
# 结果校验更强，也不必接入 move 那套 pending 状态机。

if [[ -n "${OPENLIST_MOVER_BACKUP_LOADED:-}" ]]; then
  return 0
fi
OPENLIST_MOVER_BACKUP_LOADED=1

# 备份盘根目录；留空表示不做备份（保持旧的单目的地行为）
BACKUP_TARGET_BASE="${BACKUP_TARGET_BASE:-}"
# 暂存区目录名（位于 SOURCE_ROOT 下，OpenList 才能通过 SOURCE_MOUNT 读到；list_source_files 会跳过它）
BACKUP_STAGING_DIR_NAME="${BACKUP_STAGING_DIR_NAME:-.backup-staging}"
# 每轮最多重新提交多少个未确认的暂存 copy；0 = 不限
BACKUP_RESUBMIT_PER_RUN="${BACKUP_RESUBMIT_PER_RUN:-100}"

backup_enabled() {
  [[ -n "${BACKUP_TARGET_BASE}" ]]
}

# pending 记录只存 file_path/task_id/时间戳，dst_dir 是每轮用当前 TARGET_BASE 重算的。
# 所以在还有在途任务时改 TARGET_BASE，会让那些记录去新目标找文件、永远找不到而卡在 WAIT。
# 这里记住上一轮用的 TARGET_BASE，发现变了且仍有 pending 就拒绝本轮并给出处置方法。
target_base_marker_file() {
  printf '%s.target' "${PENDING_FILE}"
}

assert_target_base_stable() {
  local marker previous current="${TARGET_BASE}"
  marker=$(target_base_marker_file)

  if [[ -f "${marker}" ]]; then
    previous=$(<"${marker}")
  else
    previous=""
  fi

  if [[ -n "${previous}" && "${previous}" != "${current}" ]]; then
    local pending_count
    pending_count=$(pending_record_count)
    if (( pending_count > 0 )); then
      log "拒绝运行：TARGET_BASE 由 ${previous} 改为 ${current}，但仍有 ${pending_count} 个在途任务。"
      log "  在途记录不含目标目录，改基路径会让它们永远对不上账。"
      log "  处置：先把 TARGET_BASE 改回 ${previous} 跑到 pending 为 0，再切换。"
      return 1
    fi
    log "TARGET_BASE 已切换: ${previous} -> ${current}（无在途任务，安全）"
  fi

  ensure_parent_dir "${marker}"
  printf '%s' "${current}" > "${marker}"
  return 0
}

# 备份盘目标目录：从本地文件路径直接算，用**未清洗**的原始目录名。
#
# 不能从主力盘的 dst_dir 反推——主力盘目录名可能已经去过 emoji（wopan 不接受），
# 而备份盘必须与 download_records 里存的原始相对路径保持一致。
backup_dir_for_local_file() {
  local local_file_path="$1"
  local relative_path="${local_file_path#"${SOURCE_ROOT}"/}"
  if [[ -n "${STRIP_PREFIX_DIR}" ]]; then
    case "${relative_path}" in
      "${STRIP_PREFIX_DIR}/"*) relative_path="${relative_path#"${STRIP_PREFIX_DIR}"/}" ;;
    esac
  fi

  local relative_dir
  relative_dir=$(dirname -- "${relative_path}")
  relative_dir=$(trim_relative_dir "${relative_dir}") || return 1
  if [[ "${relative_dir}" == "." ]]; then
    printf '%s' "${BACKUP_TARGET_BASE}"
    return 0
  fi
  printf '%s/%s' "${BACKUP_TARGET_BASE}" "${relative_dir}"
}

copy_remote_file_to_backup() {
  local token="$1"
  local src_dir="$2"
  local dst_dir="$3"
  local file_name="$4"
  local names_json
  names_json=$(jq -cn --arg n "${file_name}" '[$n]')

  local payload
  payload=$(jq -cn --arg s "${src_dir}" --arg d "${dst_dir}" --argjson names "${names_json}" \
    '{src_dir:$s,dst_dir:$d,names:$names}')

  local resp
  resp=$(api_post_json "${token}" "/api/fs/copy" "${payload}") || return 1

  local code
  code=$(jq -r '.code // 0' <<< "${resp}")
  if [[ "${code}" == "200" ]]; then
    return 0
  fi

  local msg
  msg=$(jq -r '.message // "unknown error"' <<< "${resp}")
  # 目标已存在：交给后续的大小校验判定，不当作失败
  if [[ "${msg}" == *"exists"* ]]; then
    return 0
  fi
  log "备份复制提交失败: src=${src_dir}, dst=${dst_dir}, file=${file_name} (${msg})"
  return 1
}

# 备份盘上是否已有同名且同样大小的文件
backup_copy_confirmed() {
  local token="$1"
  local backup_dir="$2"
  local file_name="$3"
  local expected_size="$4"

  local status
  status=$(remote_entry_status "${token}" "${backup_dir}" "${file_name}")
  [[ "${status}" -eq 0 ]] || return 1

  local remote_size
  remote_size=$(get_remote_file_size "${token}" "${backup_dir}" "${file_name}") || return 1
  [[ -n "${remote_size}" ]] || return 1

  if [[ -z "${expected_size}" || "${expected_size}" == "0" ]]; then
    return 0
  fi
  local diff=$((remote_size - expected_size))
  (( diff < 0 )) && diff=$((-diff))
  if (( diff <= SIZE_MISMATCH_TOLERANCE_BYTES )); then
    return 0
  fi
  log "备份盘大小不一致: file=${file_name}, 备份=${remote_size}, 本地=${expected_size}"
  return 1
}

backup_staging_root() {
  printf '%s/%s' "${SOURCE_ROOT}" "${BACKUP_STAGING_DIR_NAME}"
}

backup_staging_path() {
  local local_file_path="$1"
  printf '%s/%s' "$(backup_staging_root)" "${local_file_path#"${SOURCE_ROOT}"/}"
}

# 暂存文件所在目录在 OpenList 里的路径（copy 的 src_dir）
backup_staging_mount_dir() {
  local staged_path="$1"
  local relative_dir
  relative_dir=$(dirname -- "${staged_path#"${SOURCE_ROOT}"/}")
  printf '%s/%s' "${SOURCE_MOUNT}" "${relative_dir}"
}

# 返回 0 = 新建了暂存链接；2 = 已有指向同一文件的暂存链接；1 = 失败
stage_local_file_for_backup() {
  local local_file_path="$1"
  local staged_path
  staged_path=$(backup_staging_path "${local_file_path}")

  if [[ -e "${staged_path}" ]]; then
    [[ "${staged_path}" -ef "${local_file_path}" ]] && return 2
    # 同名但不是同一文件（本地重新下载过）：以本地当前文件为准
    log "暂存区同名文件与本地不是同一文件，替换: ${staged_path}"
  fi
  mkdir -p -- "$(dirname -- "${staged_path}")" || return 1
  ln -f -- "${local_file_path}" "${staged_path}" || return 1
}

submit_staged_backup_copy() {
  local token="$1"
  local staged_path="$2"
  local backup_dir="$3"
  local file_name="$4"

  ensure_remote_dir "${token}" "${backup_dir}" || {
    log "备份盘目录创建失败: ${backup_dir}"
    return 1
  }
  copy_remote_file_to_backup "${token}" "$(backup_staging_mount_dir "${staged_path}")" "${backup_dir}" "${file_name}"
}

# 返回 0 = 备份盘已持有该文件，或已暂存并提交 copy（可以继续 move）；非 0 = 暂存失败，不可继续
ensure_backup_copy() {
  local token="$1"
  local local_file_path="$2"
  local file_name="$5"

  backup_enabled || return 0

  local backup_dir
  backup_dir=$(backup_dir_for_local_file "${local_file_path}") || return 1

  local local_size=""
  if [[ -f "${local_file_path}" ]]; then
    local_size=$(stat -c '%s' -- "${local_file_path}" 2>/dev/null || printf '')
  fi

  # 已经备份过就直接放行（历史文件、上一轮已完成的文件都会走到这里）
  if backup_copy_confirmed "${token}" "${backup_dir}" "${file_name}" "${local_size}"; then
    return 0
  fi

  local stage_rc=0
  stage_local_file_for_backup "${local_file_path}" || stage_rc=$?
  case "${stage_rc}" in
    0) ;;
    # 之前已暂存：copy 在途或失败，都交给 drain_backup_staging，避免重复提交
    2) return 0 ;;
    *)
      log "备份暂存失败，本轮不移动本地文件: ${local_file_path}"
      return 1
      ;;
  esac

  if submit_staged_backup_copy "${token}" "$(backup_staging_path "${local_file_path}")" "${backup_dir}" "${file_name}"; then
    log "备份已暂存并提交 copy: ${backup_dir}/${file_name}"
  else
    log "备份 copy 提交失败，已暂存，后续轮次重试: ${local_file_path}"
  fi
  return 0
}

prune_empty_staging_dirs() {
  local dir="$1"
  local root="$2"
  while [[ "${dir}" == "${root}"/* ]]; do
    rmdir -- "${dir}" 2>/dev/null || break
    dir=$(dirname -- "${dir}")
  done
}

# 每轮开头收口暂存区：备份盘同名同大小才删暂存链接（此时数据已在备份盘）；
# 未确认且没有在途 copy 的重新提交，受 BACKUP_RESUBMIT_PER_RUN 限制。
drain_backup_staging() {
  local token="$1"
  backup_enabled || return 0

  local root
  root=$(backup_staging_root)
  [[ -d "${root}" ]] || return 0

  local limit inflight_names="" can_resubmit="true" resp
  limit=$(normalize_nonnegative_limit "${BACKUP_RESUBMIT_PER_RUN}" "0")
  if resp=$(api_get_json "${token}" "/api/task/copy/undone") && [[ "$(jq -r '.code // 0' <<< "${resp}")" == "200" ]]; then
    inflight_names=$(jq -r '.data // [] | .[].name' <<< "${resp}")
  else
    # 不知道哪些 copy 还在途，重提会重复上传；本轮只做确认收口
    log "查询备份 copy 在途任务失败，本轮暂存区只确认不重提"
    can_resubmit="false"
  fi

  local staged_path relative_path backup_dir file_name staged_size task_name last_dir=""
  local total=0 released=0 inflight=0 resubmitted=0 deferred=0 failed=0
  while IFS= read -r -d '' staged_path; do
    total=$((total + 1))
    relative_path="${staged_path#"${root}"/}"
    backup_dir=$(backup_dir_for_local_file "${SOURCE_ROOT}/${relative_path}") || { failed=$((failed + 1)); continue; }
    file_name="${staged_path##*/}"
    staged_size=$(stat -c '%s' -- "${staged_path}")

    # 按目录排序遍历，每个备份目录只丢一次运行内列表快照
    if [[ "${backup_dir}" != "${last_dir}" ]]; then
      invalidate_remote_dir_cache "${backup_dir}"
      last_dir="${backup_dir}"
    fi
    if backup_copy_confirmed "${token}" "${backup_dir}" "${file_name}" "${staged_size}"; then
      rm -f -- "${staged_path}"
      prune_empty_staging_dirs "$(dirname -- "${staged_path}")" "${root}"
      released=$((released + 1))
      continue
    fi

    task_name=$(build_move_task_name "$(backup_staging_mount_dir "${staged_path}")" "${backup_dir}" "${file_name}" "copy")
    if grep -Fxq -- "${task_name}" <<< "${inflight_names}"; then
      inflight=$((inflight + 1))
      continue
    fi
    if [[ "${can_resubmit}" != "true" ]] || (( limit > 0 && resubmitted >= limit )); then
      deferred=$((deferred + 1))
      continue
    fi
    if submit_staged_backup_copy "${token}" "${staged_path}" "${backup_dir}" "${file_name}"; then
      resubmitted=$((resubmitted + 1))
    else
      failed=$((failed + 1))
    fi
  done < <(find "${root}" -type f -print0 | sort -z)

  log "备份暂存区: total=${total}, 已确认释放=${released}, copy在途=${inflight}, 重新提交=${resubmitted}, 延后=${deferred}, 提交失败=${failed}"
}

is_original_backup_file() {
  local name="${1##*/}"
  case "${name,,}" in
    *.fast.mp4|*.cover.jpg|cover.jpg) return 1 ;;
    *.mp4|*.jpg|*.jpeg|*.png|*.webp|*.gif|*.heic|*.bmp) return 0 ;;
    *) return 1 ;;
  esac
}

backup_local_originals_without_move() {
  local token="$1" run_epoch="$2" processed="${3:-0}"
  local limit attempts=0 staged=0 file_path backup_dir src_dir file_name local_size staged_path
  if ! backup_enabled; then
    log "目标盘配额不足且未配置备份盘，本地原始文件保留"
    return 0
  fi
  limit=$(normalize_nonnegative_limit "${MAX_NEW_FILES_PER_RUN}" "0")
  log "目标盘配额不足，仅继续夸克备份原始文件；不提交主力盘移动，不删除本地文件"
  while IFS= read -r -d '' file_path; do
    is_original_backup_file "${file_path}" || continue
    should_process_file "${file_path}" "${run_epoch}" || continue
    is_file_pending "${file_path}" && continue
    if (( limit > 0 && processed >= limit )); then
      log "原始文件备份达到本轮上限: ${processed}/${limit}"
      break
    fi
    backup_dir=$(backup_dir_for_local_file "${file_path}") || continue
    # 源目录按原名（/qbt 挂载接受尾空格），不能从清理过的备份目录反推
    src_dir=$(build_remote_paths "${file_path}" | sed -n 1p) || continue
    file_name="${file_path##*/}"
    local_size=$(stat -c '%s' -- "${file_path}")
    # 已有副本不消耗新增额度，后续轮次才能继续处理剩余原始文件。
    backup_copy_confirmed "${token}" "${backup_dir}" "${file_name}" "${local_size}" && continue
    # 已暂存的由 drain_backup_staging 跟进，同样不消耗新增额度
    staged_path=$(backup_staging_path "${file_path}")
    [[ "${staged_path}" -ef "${file_path}" ]] && continue
    processed=$((processed + 1))
    attempts=$((attempts + 1))
    if ensure_backup_copy "${token}" "${file_path}" "${src_dir}" "${TARGET_BASE}" "${file_name}"; then
      staged=$((staged + 1))
    else
      PROCESS_ERROR_COUNT=$((PROCESS_ERROR_COUNT + 1))
    fi
  done < <(list_source_files)
  log "仅备份处理结束: attempted=${attempts}, staged=${staged}, failed=$((attempts - staged)); 本地文件全部保留"
}
