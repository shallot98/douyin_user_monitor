import asyncio
import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator

import orjson

from douyin_user_monitor.monitor.history_sync import normalize_history_sync_state
from douyin_user_monitor.monitor.nickname_history import normalize_nickname_history
from douyin_user_monitor.monitor.profile_parser import build_account_status_fields
from douyin_user_monitor.monitor.record_store import RecordStore, record_db_path
from douyin_user_monitor.monitor.timeutils import cn_now

DEFAULT_INTERVAL_HOURS = 0.05
DEFAULT_MODE = "interval"
DEFAULT_COVERAGE_HOURS = 24.0
DEFAULT_RANDOM_MIN_SECONDS = 0.0
DEFAULT_RANDOM_MAX_SECONDS = 0.0

FileStamp = tuple[int, int, int, int, int]


def file_stamp(stat: os.stat_result) -> FileStamp:
    """Identify the opened file, including replacements with the same mtime."""
    return stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size


MONITORING_DEFAULTS = {
    "is_running": False,
    # 用户期望是否持续监控；进程重启后依据此字段自动恢复，而不是 is_running
    "desired_running": False,
    "mode": DEFAULT_MODE,
    "interval_hours": DEFAULT_INTERVAL_HOURS,
    "coverage_hours": DEFAULT_COVERAGE_HOURS,
    "random_min_seconds": DEFAULT_RANDOM_MIN_SECONDS,
    "random_max_seconds": DEFAULT_RANDOM_MAX_SECONDS,
    "last_run_at": None,
    "last_run_result": {},
    # 覆盖轮询进度：重启后从已处理用户之后继续
    "coverage_progress": None,
}


def build_default_state() -> Dict[str, Any]:
    monitoring = dict(MONITORING_DEFAULTS)
    monitoring["updated_at"] = cn_now()
    return {"users": [], "monitoring": monitoring}


def normalize_root_state(state: Dict[str, Any]) -> None:
    if "users" not in state or not isinstance(state["users"], list):
        state["users"] = []
    if "monitoring" not in state or not isinstance(state["monitoring"], dict):
        state["monitoring"] = build_default_state()["monitoring"]

    monitoring = state["monitoring"]
    defaults = build_default_state()["monitoring"]
    for key, value in defaults.items():
        monitoring.setdefault(key, value)

    if "interval_hours" not in monitoring:
        legacy_seconds = monitoring.get("interval_seconds")
        if isinstance(legacy_seconds, (int, float)):
            monitoring["interval_hours"] = float(legacy_seconds) / 3600.0
        else:
            monitoring["interval_hours"] = DEFAULT_INTERVAL_HOURS


def normalize_user_entry(user: Dict[str, Any]) -> None:
    avatar_url = str(user.get("avatar_url") or "").strip()
    user["avatar_url"] = avatar_url or None
    account_status = build_account_status_fields(
        str(user.get("account_status") or ""),
        user.get("account_status_reason"),
    )
    user.update(account_status)
    account_status_updated_at = str(user.get("account_status_updated_at") or "").strip()
    user["account_status_updated_at"] = account_status_updated_at or None
    if not isinstance(user.get("downloaded_aweme_ids"), list):
        user["downloaded_aweme_ids"] = []
    normalize_nickname_history(user)
    normalize_history_sync_state(user)


class LegacyRecordsInStateError(RuntimeError):
    """JSON state still carries `download_records`; they belong in the RecordStore."""


def _assert_no_inline_records(state: Dict[str, Any], *, where: str) -> None:
    for user in state.get("users") or []:
        if isinstance(user, dict) and "download_records" in user:
            raise LegacyRecordsInStateError(
                f"{where}: user {user.get('id')!r} carries download_records in the JSON state; "
                "records live in monitor_records.db (run scripts/split_download_records.py "
                "for a legacy state file, or write through storage.records)"
            )


class MonitorStorage:
    def __init__(self, state_file: Path):
        self.state_file = state_file
        # download_records 拆到 SQLite：JSON 只剩用户配置/状态（~2MB），写一次 ~20ms
        self.records = RecordStore(record_db_path(state_file))
        # 进程内缓存：13MB+ 状态文件每次 read+json.loads 就要 300ms+，
        # 面板轮询 /status /users 会成倍放大首屏等待。
        self._cache: Dict[str, Any] | None = None
        self._cache_stamp: FileStamp | None = None
        self._async_read_lock = asyncio.Lock()
        self._ensure_exists()
    def _ensure_exists(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        if self.state_file.exists():
            return
        self.save_state(build_default_state())

    def load_state(self) -> Dict[str, Any]:
        """读取监控状态。同进程内命中文件标识缓存时直接返回共享对象。

        注意：返回值可能是缓存引用。展示层若要就地改字段（例如 get_status），
        必须先浅拷贝再改，避免污染缓存。写路径请走 update_state/save_state。
        """
        return self._load_state_unlocked()

    async def load_state_async(self) -> Dict[str, Any]:
        """Read a private disk snapshot off-loop; publish cache only on the event loop."""
        async with self._async_read_lock:
            cached = self._read_cache_if_fresh()
            if cached is not None:
                return cached
            state, stamp = await asyncio.to_thread(self._read_state_snapshot)
            # A synchronous writer may have published newer state while we awaited the read.
            cached = self._read_cache_if_fresh()
            if cached is not None:
                return cached
            self._store_cache(state, stamp)
            return state

    def save_state(self, state: Dict[str, Any]) -> None:
        with self._state_file_lock():
            self._save_state_unlocked(state)

    def update_state(self, mutator: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
        """Load → mutate → save under an exclusive file lock (RMW)."""
        with self._state_file_lock():
            state, _ = self._read_state_snapshot()
            mutator(state)
            self._save_state_unlocked(state)
            return state

    @contextmanager
    def _state_file_lock(self) -> Iterator[None]:
        lock_path = self.state_file.with_name(self.state_file.name + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _load_state_unlocked(self) -> Dict[str, Any]:
        cached = self._read_cache_if_fresh()
        if cached is not None:
            return cached
        state, stamp = self._read_state_snapshot()
        self._store_cache(state, stamp)
        return state

    def _read_state_snapshot(self) -> tuple[Dict[str, Any], FileStamp]:
        with self.state_file.open("rb") as source:
            # The path may be atomically replaced during parsing; stamp the file we opened.
            stamp = file_stamp(os.fstat(source.fileno()))
            # 读写都在事件循环上同步执行，orjson 比 stdlib json 快 ~5x。
            state = orjson.loads(source.read())
        _assert_no_inline_records(state, where=f"read {self.state_file}")
        self._normalize_state(state)
        return state, stamp

    def _save_state_unlocked(self, state: Dict[str, Any]) -> None:
        _assert_no_inline_records(state, where=f"write {self.state_file}")
        stamp = self._write_json_atomic(state)
        self._store_cache(state, stamp)

    def _write_json_atomic(self, state: Dict[str, Any]) -> FileStamp:
        temp_file = self.state_file.with_suffix(".tmp")
        content = orjson.dumps(state, option=orjson.OPT_INDENT_2 | orjson.OPT_NON_STR_KEYS)
        with temp_file.open("wb") as target:
            target.write(content)
            target.flush()
            temp_file.replace(self.state_file)
            # Rename can change ctime; retain the descriptor and stamp after replacement.
            return file_stamp(os.fstat(target.fileno()))

    def _file_stamp(self) -> FileStamp | None:
        try:
            return file_stamp(self.state_file.stat())
        except OSError:
            return None

    def _read_cache_if_fresh(self) -> Dict[str, Any] | None:
        if self._cache is None:
            return None
        stamp = self._file_stamp()
        if stamp is None or self._cache_stamp != stamp:
            return None
        return self._cache

    def _store_cache(self, state: Dict[str, Any], stamp: FileStamp) -> None:
        self._cache = state
        self._cache_stamp = stamp

    def _normalize_state(self, state: Dict[str, Any]) -> None:
        normalize_root_state(state)
        for user in state["users"]:
            if not isinstance(user, dict):
                continue
            normalize_user_entry(user)
