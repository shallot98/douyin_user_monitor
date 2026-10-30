#!/usr/bin/env bash

if [[ -n "${OPENLIST_MOVER_REMOTE_CACHE_LOADED:-}" ]]; then
  return 0
fi
OPENLIST_MOVER_REMOTE_CACHE_LOADED=1

REMOTE_LIST_PAGE_JSON=""
REMOTE_LIST_CACHE_DIR=""
# 失效时清理的分页数上限（目录页数通常远小于此）
REMOTE_LIST_CACHE_INVALIDATE_MAX_PAGES="${REMOTE_LIST_CACHE_INVALIDATE_MAX_PAGES:-20}"

remote_list_cache_default_dir() {
  printf '%s/runtime/.openlist-auto-move.remote-cache.%s' "${OPENLIST_MOVER_PROJECT_DIR}" "$$"
}

ensure_remote_list_cache_dir() {
  [[ "${REMOTE_LIST_CACHE_ENABLED}" == "true" ]] || return 1
  if [[ -z "${REMOTE_LIST_CACHE_DIR}" ]]; then
    REMOTE_LIST_CACHE_DIR=$(remote_list_cache_default_dir)
  fi
  mkdir -p "${REMOTE_LIST_CACHE_DIR}"
}

clear_remote_list_cache_dir() {
  [[ -n "${REMOTE_LIST_CACHE_DIR}" && -d "${REMOTE_LIST_CACHE_DIR}" ]] || return 0
  find "${REMOTE_LIST_CACHE_DIR}" -type f -name '*.json' -delete
}

cleanup_remote_list_cache() {
  if [[ -n "${REMOTE_LIST_CACHE_DIR}" && "${REMOTE_LIST_CACHE_DIR}" == "${OPENLIST_MOVER_PROJECT_DIR}/runtime/.openlist-auto-move.remote-cache."* ]]; then
    rm -rf -- "${REMOTE_LIST_CACHE_DIR}"
  fi
  REMOTE_LIST_CACHE_DIR=""
  REMOTE_LIST_PAGE_JSON=""
}

reset_remote_list_cache() {
  REMOTE_LIST_PAGE_JSON=""
  if [[ "${REMOTE_LIST_CACHE_ENABLED}" == "true" ]]; then
    ensure_remote_list_cache_dir
    clear_remote_list_cache_dir
  else
    cleanup_remote_list_cache
  fi
}

remote_list_refresh_json() {
  if [[ "${REMOTE_LIST_REFRESH}" == "true" ]]; then
    printf 'true'
  else
    printf 'false'
  fi
}

remote_list_cache_key() {
  local target_dir="$1"
  local page="$2"
  local refresh
  refresh=$(remote_list_refresh_json)
  printf '%s\t%s\t%s\t%s' "${target_dir}" "${page}" "${REMOTE_LIST_PAGE_SIZE}" "${refresh}"
}

remote_list_cache_file() {
  local target_dir="$1"
  local page="$2"
  local key_hash
  key_hash=$(remote_list_cache_key "${target_dir}" "${page}" | sha256sum | awk '{ print $1 }')
  printf '%s/%s.json' "${REMOTE_LIST_CACHE_DIR}" "${key_hash}"
}

# 单个目录的缓存失效：刚往这个目录写过东西，再校验前必须丢掉旧列表，
# 否则会一直读到写入前的快照（备份落地校验就踩过这个坑）。
invalidate_remote_dir_cache() {
  local target_dir="$1"
  [[ "${REMOTE_LIST_CACHE_ENABLED}" == "true" ]] || return 0
  [[ -n "${REMOTE_LIST_CACHE_DIR}" && -d "${REMOTE_LIST_CACHE_DIR}" ]] || return 0

  local page=1 cache_file
  while (( page <= REMOTE_LIST_CACHE_INVALIDATE_MAX_PAGES )); do
    cache_file=$(remote_list_cache_file "${target_dir}" "${page}")
    rm -f -- "${cache_file}"
    page=$((page + 1))
  done
  REMOTE_LIST_PAGE_JSON=""
  return 0
}

load_remote_dir_page() {
  local token="$1"
  local target_dir="$2"
  local page="$3"
  local cache_key="" cache_file="" resp=""
  REMOTE_LIST_PAGE_JSON=""

  cache_key=$(remote_list_cache_key "${target_dir}" "${page}")
  if [[ "${REMOTE_LIST_CACHE_ENABLED}" == "true" ]]; then
    ensure_remote_list_cache_dir
    cache_file=$(remote_list_cache_file "${target_dir}" "${page}")
    if [[ -s "${cache_file}" ]]; then
      REMOTE_LIST_PAGE_JSON=$(<"${cache_file}")
      return 0
    fi
  fi

  local payload
  payload=$(jq -cn \
    --arg p "${target_dir}" \
    --argjson page "${page}" \
    --argjson per_page "${REMOTE_LIST_PAGE_SIZE}" \
    --argjson refresh "$(remote_list_refresh_json)" \
    '{path:$p,password:"",page:$page,per_page:$per_page,refresh:$refresh}')

  resp=$(api_post_json "${token}" "/api/fs/list" "${payload}") || return 2
  if [[ "$(jq -r '.code // 0' <<< "${resp}")" != "200" ]]; then
    return 2
  fi

  if [[ "${REMOTE_LIST_CACHE_ENABLED}" == "true" ]]; then
    printf '%s' "${resp}" > "${cache_file}"
  fi
  REMOTE_LIST_PAGE_JSON="${resp}"
}

list_remote_dir_page() {
  load_remote_dir_page "$@" || return 2
  printf '%s' "${REMOTE_LIST_PAGE_JSON}"
}
