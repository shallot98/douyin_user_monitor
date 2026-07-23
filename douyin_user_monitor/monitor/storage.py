import fcntl
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator

from douyin_user_monitor.monitor.history_sync import normalize_history_sync_state
from douyin_user_monitor.monitor.profile_parser import build_account_status_fields

DEFAULT_INTERVAL_HOURS = 0.05
DEFAULT_MODE = "interval"
DEFAULT_COVERAGE_HOURS = 24.0
DEFAULT_RANDOM_MIN_SECONDS = 0.0
DEFAULT_RANDOM_MAX_SECONDS = 0.0


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


def utc_now() -> str:
    from datetime import datetime, timedelta, timezone

    china_tz = timezone(timedelta(hours=8))
    return datetime.now(china_tz).isoformat(timespec="milliseconds")


def build_default_state() -> Dict[str, Any]:
    monitoring = dict(MONITORING_DEFAULTS)
    monitoring["updated_at"] = utc_now()
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
    if not isinstance(user.get("download_records"), list):
        user["download_records"] = []
    normalize_history_sync_state(user)


class MonitorStorage:
    def __init__(self, state_file: Path):
        self.state_file = state_file
        self._ensure_exists()

    def _ensure_exists(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        if self.state_file.exists():
            return
        self.save_state(build_default_state())

    def load_state(self) -> Dict[str, Any]:
        try:
            raw = self.state_file.read_text(encoding="utf-8")
            state = json.loads(raw)
        except (json.JSONDecodeError, OSError):
            state = build_default_state()
            self.save_state(state)
        self._normalize_state(state)
        return state

    def save_state(self, state: Dict[str, Any]) -> None:
        with self._state_file_lock():
            self._save_state_unlocked(state)

    def update_state(self, mutator: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
        """Load → mutate → save under an exclusive file lock (RMW)."""
        with self._state_file_lock():
            state = self._load_state_unlocked()
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
        try:
            raw = self.state_file.read_text(encoding="utf-8")
            state = json.loads(raw)
        except (json.JSONDecodeError, OSError):
            state = build_default_state()
            self._write_json_atomic(state)
        self._normalize_state(state)
        return state

    def _save_state_unlocked(self, state: Dict[str, Any]) -> None:
        self._merge_concurrent_monitoring_fields(state)
        self._write_json_atomic(state)

    def _write_json_atomic(self, state: Dict[str, Any]) -> None:
        temp_file = self.state_file.with_suffix(".tmp")
        content = json.dumps(state, ensure_ascii=False, indent=2)
        temp_file.write_text(content, encoding="utf-8")
        temp_file.replace(self.state_file)

    def _merge_concurrent_monitoring_fields(self, state: Dict[str, Any]) -> None:
        """Preserve newer cookie_pool written by concurrent API writers.

        Coverage checkpoints keep a long-lived in-memory state; a full save
        would otherwise wipe pool items added after that snapshot was loaded.
        """
        if not self.state_file.exists():
            return
        try:
            disk_state = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        if not isinstance(disk_state, dict):
            return
        disk_mon = disk_state.get("monitoring")
        mem_mon = state.setdefault("monitoring", {})
        if not isinstance(disk_mon, dict) or not isinstance(mem_mon, dict):
            return
        self._prefer_newer_dict_field(mem_mon, disk_mon, "cookie_pool")

    @staticmethod
    def _cookie_pool_activity_score(pool: Dict[str, Any]) -> int:
        items = pool.get("items")
        if not isinstance(items, list):
            return 0
        total = 0
        for row in items:
            if isinstance(row, dict):
                total += int(row.get("use_count") or 0)
        total += int(pool.get("rotation_cursor") or 0)
        return total

    @staticmethod
    def _prefer_newer_dict_field(
        mem_mon: Dict[str, Any],
        disk_mon: Dict[str, Any],
        key: str,
    ) -> None:
        disk_val = disk_mon.get(key)
        if not isinstance(disk_val, dict):
            return
        mem_val = mem_mon.get(key)
        if not isinstance(mem_val, dict):
            mem_mon[key] = disk_val
            return

        disk_items = disk_val.get("items")
        mem_items = mem_val.get("items")
        disk_has_items = isinstance(disk_items, list) and bool(disk_items)
        mem_has_items = isinstance(mem_items, list) and bool(mem_items)
        if disk_has_items and not mem_has_items:
            mem_mon[key] = disk_val
            return

        disk_ts = str(disk_val.get("updated_at") or "")
        mem_ts = str(mem_val.get("updated_at") or "")
        if disk_ts and (not mem_ts or disk_ts > mem_ts):
            mem_mon[key] = disk_val
            return
        # Same timestamp (or missing): prefer the side with more rotation activity.
        if key == "cookie_pool" and disk_ts and disk_ts == mem_ts:
            if MonitorStorage._cookie_pool_activity_score(disk_val) > MonitorStorage._cookie_pool_activity_score(mem_val):
                mem_mon[key] = disk_val

    def _normalize_state(self, state: Dict[str, Any]) -> None:
        normalize_root_state(state)
        for user in state["users"]:
            if not isinstance(user, dict):
                continue
            normalize_user_entry(user)
