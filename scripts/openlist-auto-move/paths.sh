#!/usr/bin/env bash

if [[ -n "${OPENLIST_MOVER_PATHS_LOADED:-}" ]]; then
  return 0
fi
OPENLIST_MOVER_PATHS_LOADED=1

sanitize_component() {
  local text="$1"
  local sanitized
  # \p{Extended_Pictographic} 不含肤色修饰符(1F3FB-1F3FF)与键帽符(20E3)，但 wopan 拒收残留，需显式补剥；
  # 另按实测补剥数学字母区(1D400-1D7FF)与 CJK 扩展 B-F 之外的星球面（python 侧 WOPAN_STRIP_RANGES 同源）
  sanitized=$(printf '%s' "${text}" | perl -CSDA -Mutf8 -pe 's/[\p{Extended_Pictographic}\x{1F3FB}-\x{1F3FF}]//g; s/[\x{FE0F}\x{200D}\x{20E3}]//g; s/[\x{1D400}-\x{1D7FF}\x{10000}-\x{1FFFF}\x{323B0}-\x{10FFFF}]//g;')
  sanitized=$(printf '%s' "${sanitized}" | sed -E 's/[[:space:]]+/ /g; s/^[[:space:]]+//; s/[[:space:]]+$//')
  printf '%s' "${sanitized}"
}

sanitize_relative_dir() {
  local relative_dir="$1"
  if [[ "${relative_dir}" == "." ]]; then
    printf '.'
    return 0
  fi

  local result=""
  local component
  IFS='/' read -r -a components <<< "${relative_dir}"
  for component in "${components[@]}"; do
    local cleaned
    cleaned=$(sanitize_component "${component}")
    if [[ -z "${cleaned}" ]]; then
      log "目录名去除 emoji 后为空: ${component}"
      return 1
    fi
    if [[ -z "${result}" ]]; then
      result="${cleaned}"
    else
      result="${result}/${cleaned}"
    fi
  done

  printf '%s' "${result}"
}

sanitize_file_name_in_place() {
  local file_path="$1"
  local dir_path
  local base_name
  local sanitized_name
  local target_path

  dir_path=$(dirname -- "${file_path}")
  base_name=$(basename -- "${file_path}")
  sanitized_name=$(sanitize_component "${base_name}")

  if [[ -z "${sanitized_name}" ]]; then
    log "文件名去除 emoji 后为空: ${file_path}"
    return 1
  fi

  if [[ "${sanitized_name}" == "${base_name}" ]]; then
    printf '%s' "${file_path}"
    return 0
  fi

  target_path="${dir_path}/${sanitized_name}"
  if [[ -e "${target_path}" ]]; then
    log "重命名冲突，目标已存在: ${target_path}"
    return 1
  fi

  mv -- "${file_path}" "${target_path}"
  log "已重命名文件: ${file_path} -> ${target_path}"
  printf '%s' "${target_path}"
}

# 目标盘（D2/夸克）不接受首尾带空白的目录名（OpenList 报 object not found），
# 标题截断常落在空格上。只改远端目标目录；本地与 /qbt 源路径保持原样。
# 网页侧 path_sanitize.trim_relative_dirs 做同样变换。
trim_relative_dir() {
  local relative_dir="$1"
  if [[ "${relative_dir}" == "." ]]; then
    printf '.'
    return 0
  fi

  local result="" component trimmed
  IFS='/' read -r -a components <<< "${relative_dir}"
  for component in "${components[@]}"; do
    trimmed=$(printf '%s' "${component}" | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//')
    if [[ -z "${trimmed}" ]]; then
      log "目录名去除首尾空白后为空: ${component}"
      return 1
    fi
    result="${result:+${result}/}${trimmed}"
  done
  printf '%s' "${result}"
}

predict_sanitized_file_path() {
  local file_path="$1"
  local dir_path
  local base_name
  local sanitized_name

  dir_path=$(dirname -- "${file_path}")
  base_name=$(basename -- "${file_path}")
  sanitized_name=$(sanitize_component "${base_name}")

  if [[ -z "${sanitized_name}" ]]; then
    return 1
  fi

  if [[ "${sanitized_name}" == "${base_name}" ]]; then
    printf '%s' "${file_path}"
    return 0
  fi

  printf '%s/%s' "${dir_path}" "${sanitized_name}"
}

should_process_file() {
  local file_path="$1"
  local run_epoch="$2"
  local file_epoch

  if ! file_epoch=$(stat -c '%Y' "${file_path}" 2>/dev/null); then
    log "文件已不存在，跳过: ${file_path}"
    return 1
  fi

  if is_file_skipped "${file_path}"; then
    return 1
  fi

  if [[ "${file_epoch}" -gt "${run_epoch}" ]]; then
    return 1
  fi

  if (( run_epoch - file_epoch < MIN_AGE_SECONDS )); then
    return 1
  fi

  return 0
}

build_remote_paths() {
  local file_path="$1"
  local relative_path="${file_path#${SOURCE_ROOT}/}"
  if [[ -n "${STRIP_PREFIX_DIR}" ]]; then
    case "${relative_path}" in
      "${STRIP_PREFIX_DIR}/"*) relative_path="${relative_path#${STRIP_PREFIX_DIR}/}" ;;
    esac
  fi

  local source_path="${SOURCE_MOUNT}/${relative_path}"
  local relative_dir
  relative_dir=$(dirname -- "${relative_path}")

  # 主力盘（wopan）拒绝含 emoji 的目录名，报误导性的「8005 登录失败」。
  # 这里只清洗目标目录名：文件名与本地文件都不动，备份盘另算（见 backup.sh），
  # 这样 download_records 里存的相对路径依然有效，网页侧做同样变换即可命中。
  local effective_relative_dir
  if [[ "${SANITIZE_EMOJI}" == "true" || "${PRIMARY_SANITIZE_EMOJI}" == "true" ]]; then
    effective_relative_dir=$(sanitize_relative_dir "${relative_dir}") || return 1
  else
    effective_relative_dir=$(trim_relative_dir "${relative_dir}") || return 1
  fi

  local dst_dir="${TARGET_BASE}"
  if [[ "${effective_relative_dir}" != "." ]]; then
    dst_dir="${TARGET_BASE}/${effective_relative_dir}"
  fi

  local src_dir
  local file_name
  src_dir=$(dirname -- "${source_path}")
  file_name=$(basename -- "${source_path}")
  printf '%s\n%s\n%s\n' "${src_dir}" "${dst_dir}" "${file_name}"
}

# 备份暂存区（backup.sh）里是硬链接副本，不是待搬运文件，必须跳过
list_source_files() {
  local staging_root="${SOURCE_ROOT}/${BACKUP_STAGING_DIR_NAME:-.backup-staging}"
  if [[ "${FILE_PATTERN}" == "*" ]]; then
    find "${SOURCE_ROOT}" -path "${staging_root}" -prune -o -type f -print0
    return 0
  fi

  find "${SOURCE_ROOT}" -path "${staging_root}" -prune -o -type f -name "${FILE_PATTERN}" -print0
}
