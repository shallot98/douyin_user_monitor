#!/usr/bin/env bash

# Retire terminal quota failures without dropping active tasks or touching source files.
QUOTA_FAILED_TASK_STATE=7

collect_capacity_failed_task_ids() {
  local token="$1" output="$2" endpoint response
  for endpoint in /api/task/move/undone /api/task/move/done; do
    response=$(api_get_json "${token}" "${endpoint}") || return 1
    jq -e '.code == 200 and (.data | type == "array")' <<< "${response}" >/dev/null || return 1
    jq -r --arg pattern "${CAPACITY_ERROR_PATTERN}" --argjson failed "${QUOTA_FAILED_TASK_STATE}" '
      .data[] | select(.state == $failed)
      | select((.error // "") | test($pattern))
      | .id | select(type == "string" and length > 0)
    ' <<< "${response}" >> "${output}" || return 1
  done
}

remove_capacity_pending_records() {
  local ids_file="$1" limit="$2" next_file
  next_file=$(mktemp "${PENDING_FILE}.quota.XXXXXX") || return 1
  if ! awk -F '\t' -v limit="${limit}" '
    FILENAME == ARGV[1] { failed[$0] = 1; next }
    ($2 in failed) && (limit == 0 || removed < limit) { removed++; next }
    { print }
  ' "${ids_file}" "${PENDING_FILE}" > "${next_file}"; then
    rm -f -- "${next_file}"
    return 1
  fi
  if ! mv -- "${next_file}" "${PENDING_FILE}"; then
    rm -f -- "${next_file}"
    return 1
  fi
}

retire_capacity_failed_pending() {
  local token="$1" max_reconcile="$2" processed="$3"
  local limit=0 ids_file before after
  [[ -s "${PENDING_FILE}" ]] || return 0
  if (( max_reconcile > 0 )); then
    limit=$((max_reconcile - processed))
    (( limit > 0 )) || return 0
  fi
  ids_file=$(mktemp "${PENDING_FILE}.quota-ids.XXXXXX") || return 1
  before=$(pending_record_count)
  if collect_capacity_failed_task_ids "${token}" "${ids_file}" \
    && remove_capacity_pending_records "${ids_file}" "${limit}"; then
    after=$(pending_record_count)
    log "移除已确认的历史配额失败记录: $((before - after)); 本地文件保留，下轮可重新提交"
  else
    PROCESS_ERROR_COUNT=$((PROCESS_ERROR_COUNT + 1))
    log "读取或清理历史配额失败记录失败，保留未确认的 pending"
  fi
  rm -f -- "${ids_file}"
}
