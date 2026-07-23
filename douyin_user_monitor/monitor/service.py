import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from douyin_user_monitor.monitor.crawler_protocol import MonitorCrawlerProtocol
from douyin_user_monitor.monitor.downloader import AwemeAssetDownloader
from douyin_user_monitor.monitor.history_sync import (
    HISTORY_SYNC_PAGE_SIZE,
    HISTORY_SYNC_STATUS_COMPLETED,
    HISTORY_SYNC_STATUS_IDLE,
    HISTORY_SYNC_STATUS_PAUSED,
    HISTORY_SYNC_STATUS_PENDING,
    build_history_sync_state,
    normalize_history_sync_state,
)
from douyin_user_monitor.monitor.cookie_liveness import CookieLivenessService
from douyin_user_monitor.monitor.notifier import MonitorNotifierProtocol, NoopMonitorNotifier
from douyin_user_monitor.monitor.profile_parser import ACCOUNT_STATUS_NORMAL
from douyin_user_monitor.monitor.schedule import (
    LOOP_ERROR_RETRY_SECONDS,
    MIN_INTERVAL_HOURS as SCHEDULE_MIN_INTERVAL_HOURS,
    MODE_COVERAGE,
    MODE_INTERVAL,
    build_interval_gaps,
    choose_coverage_delay,
    validate_options,
)
from douyin_user_monitor.monitor.storage import (
    DEFAULT_COVERAGE_HOURS,
    DEFAULT_INTERVAL_HOURS,
    DEFAULT_MODE,
    MonitorStorage,
    utc_now,
)
from douyin_user_monitor.monitor.user_lookup import find_user_by_id, find_user_by_sec_uid
from douyin_user_monitor.monitor.user_sync import UserSyncService

MIN_INTERVAL_HOURS = SCHEDULE_MIN_INTERVAL_HOURS
CHINA_TZ = timezone(timedelta(hours=8))
COVERAGE_PHASE_RUNNING = "running"
COVERAGE_PHASE_IDLE = "idle"
LEGACY_COOKIE_KEYS = (
    "cookie_last_updated_at",
    "cookie_last_updated_source",
    "cookie_last_preview",
    "cookie_last_length",
    "cookie_last_upstream_result",
)


def _parse_iso_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=CHINA_TZ)
    return parsed


def _seconds_until(iso_value: Any) -> float:
    target = _parse_iso_datetime(iso_value)
    if target is None:
        return 0.0
    return max(0.0, (datetime.now(target.tzinfo) - target).total_seconds() * -1)


def _elapsed_since(iso_value: Any) -> float:
    started = _parse_iso_datetime(iso_value)
    if started is None:
        return 0.0
    return max(0.0, (datetime.now(started.tzinfo) - started).total_seconds())


class MonitorService:
    def __init__(
        self,
        crawler: MonitorCrawlerProtocol,
        storage: MonitorStorage,
        download_root: Path,
        notifier: Optional[MonitorNotifierProtocol] = None,
        cookie_liveness_service: Optional[CookieLivenessService] = None,
        cookie_pool_service: Optional[Any] = None,
    ):
        self._crawler = crawler
        self._storage = storage
        self._downloader = AwemeAssetDownloader(download_root)
        self._notifier = notifier or NoopMonitorNotifier()
        self._cookie_liveness_service = cookie_liveness_service
        self._cookie_pool_service = cookie_pool_service
        rotation_session = None
        if cookie_pool_service is not None and hasattr(cookie_pool_service, "rotation_session"):
            rotation_session = cookie_pool_service.rotation_session
        self._sync_service = UserSyncService(
            crawler=self._crawler,
            downloader=self._downloader,
            notifier=self._notifier,
            cookie_rotation_session=rotation_session,
        )

        self._run_lock = asyncio.Lock()
        self._loop_task: Optional[asyncio.Task] = None

        self._mode = DEFAULT_MODE
        self._interval_hours = DEFAULT_INTERVAL_HOURS
        self._coverage_hours = DEFAULT_COVERAGE_HOURS

        self._coverage_priority_user_ids: List[str] = []
        self._active_coverage_state: Optional[Dict[str, Any]] = None
        self._wake_event = asyncio.Event()
        self._reset_runtime_state()

    def _reset_runtime_state(self) -> None:
        state = self._storage.load_state()
        monitoring = state["monitoring"]
        # 优先用 desired_running（用户意图）；兼容旧数据里只有 is_running=True 的情况
        if "desired_running" in monitoring:
            self._was_running = bool(monitoring.get("desired_running"))
        else:
            self._was_running = bool(monitoring.get("is_running", False))
            monitoring["desired_running"] = self._was_running
        monitoring["is_running"] = False
        self._apply_runtime_from_monitoring(monitoring)
        monitoring["updated_at"] = utc_now()
        monitoring.pop("interval_seconds", None)
        monitoring.pop("random_min_seconds", None)
        monitoring.pop("random_max_seconds", None)
        self._drop_legacy_scan_keys(monitoring)
        self._storage.save_state(state)

    async def auto_resume(self) -> None:
        """进程启动后若用户意图仍是监控中，则恢复循环并接续覆盖进度。"""
        if self._was_running:
            await self.start_monitoring(
                mode=self._mode,
                interval_hours=self._interval_hours,
                coverage_hours=self._coverage_hours,
                resume=True,
            )

    def _apply_runtime_from_monitoring(self, monitoring: Dict[str, Any]) -> None:
        self._mode = str(monitoring.get("mode", DEFAULT_MODE))
        interval_hours = monitoring.get("interval_hours")
        if isinstance(interval_hours, (int, float)):
            self._interval_hours = float(interval_hours)
        else:
            legacy_seconds = monitoring.get("interval_seconds", DEFAULT_INTERVAL_HOURS * 3600.0)
            self._interval_hours = float(legacy_seconds) / 3600.0
        self._coverage_hours = float(monitoring.get("coverage_hours", DEFAULT_COVERAGE_HOURS))

    def list_users(self) -> List[Dict[str, Any]]:
        return self._storage.load_state()["users"]

    async def list_users_with_profile(self) -> List[Dict[str, Any]]:
        state = self._storage.load_state()
        changed = await self._hydrate_missing_user_profiles(state)
        if changed:
            self._storage.save_state(state)
        return state["users"]

    async def add_user(self, profile_url: str) -> Dict[str, Any]:
        result = await self.add_user_with_status(profile_url)
        if result["status"] == "exists":
            user = result["user"]
            raise ValueError(f"用户已存在: {user['nickname']} ({user['sec_user_id']})")
        return result["user"]

    async def add_user_with_status(self, profile_url: str) -> Dict[str, Any]:
        raw_profile_text = (profile_url or "").strip()
        if not raw_profile_text:
            raise ValueError("用户主页链接不能为空")

        sec_user_id = await self._crawler.get_sec_user_id(raw_profile_text)
        state = self._storage.load_state()
        existed = find_user_by_sec_uid(state["users"], sec_user_id)
        if existed is not None:
            changed = await self._hydrate_missing_user_profile(existed)
            if changed:
                self._storage.save_state(state)
            return {"status": "exists", "user": existed}

        try:
            profile = await self._sync_service.resolve_profile_snapshot(sec_user_id)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(
                f"用户主页可解析 sec_user_id={sec_user_id}，但获取用户资料失败: {exc}"
            ) from exc
        now = utc_now()
        user = {
            "id": str(uuid.uuid4()),
            "profile_url": raw_profile_text,
            "sec_user_id": sec_user_id,
            "nickname": profile.nickname,
            "avatar_url": profile.avatar_url,
            "account_status": profile.account_status,
            "account_status_label": profile.account_status_label,
            "account_status_reason": profile.account_status_reason,
            "account_status_updated_at": now,
            "enabled": True,
            "created_at": now,
            "updated_at": now,
            "last_checked_at": None,
            "last_download_at": None,
            "last_aweme_id": None,
            "downloaded_count": 0,
            "downloaded_aweme_ids": [],
            "download_records": [],
            "history_sync": build_history_sync_state(
                status=HISTORY_SYNC_STATUS_PENDING,
                page_size=HISTORY_SYNC_PAGE_SIZE,
            ),
            "last_error": None,
        }
        state["users"].append(user)
        self._storage.save_state(state)

        self._attach_user_to_active_coverage_state(user)
        self._enqueue_coverage_priority_user(user["id"])
        return {"status": "created", "user": user}

    async def _hydrate_missing_user_profiles(self, state: Dict[str, Any]) -> bool:
        changed = False
        for user in state["users"]:
            try:
                changed = await self._hydrate_missing_user_profile(user) or changed
            except Exception as exc:  # noqa: BLE001
                changed = self._record_profile_hydration_error(user, exc) or changed
        return changed

    async def _hydrate_missing_user_profile(self, user: Dict[str, Any]) -> bool:
        nickname = str(user.get("nickname") or "").strip()
        avatar_url = str(user.get("avatar_url") or "").strip()
        account_status = str(user.get("account_status") or ACCOUNT_STATUS_NORMAL).strip() or ACCOUNT_STATUS_NORMAL
        needs_avatar_refresh = not avatar_url and account_status == ACCOUNT_STATUS_NORMAL
        if nickname and not needs_avatar_refresh:
            return False

        sec_user_id = str(user.get("sec_user_id") or "").strip()
        if not sec_user_id:
            raise ValueError("已存在用户缺少 sec_user_id，无法补全资料")

        try:
            profile = await self._sync_service.resolve_profile_snapshot(sec_user_id)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(
                f"用户 sec_user_id={sec_user_id} 补全资料失败: {exc}"
            ) from exc
        return self._merge_profile_snapshot(user, profile)

    def _record_profile_hydration_error(self, user: Dict[str, Any], exc: Exception) -> bool:
        error_message = f"资料补全失败: {exc}"
        changed = user.get("last_error") != error_message
        user["last_error"] = error_message
        if changed:
            user["updated_at"] = utc_now()
        return changed

    def _merge_profile_snapshot(self, user: Dict[str, Any], profile: Any) -> bool:
        now = utc_now()
        changed = False
        # Preserve existing nickname when profile returns sec_user_id[:12] fallback
        # (happens for deleted/banned users whose API returns empty nickname)
        nickname = profile.nickname
        old_nickname = str(user.get("nickname") or "").strip()
        sec_user_id = str(user.get("sec_user_id") or "")
        if old_nickname and nickname == sec_user_id[:12]:
            nickname = old_nickname
        fields = {
            "nickname": nickname,
            "avatar_url": profile.avatar_url,
            "account_status": profile.account_status,
            "account_status_label": profile.account_status_label,
            "account_status_reason": profile.account_status_reason,
        }
        for key, value in fields.items():
            if user.get(key) != value:
                user[key] = value
                changed = True
        if user.get("account_status_updated_at") != now:
            user["account_status_updated_at"] = now
            changed = True
        if changed:
            user["updated_at"] = now
        return changed

    def remove_user(self, user_id: str) -> Dict[str, Any]:
        state = self._storage.load_state()
        matched = find_user_by_id(state["users"], user_id)
        if matched is None:
            raise ValueError("用户不存在")

        state["users"] = [item for item in state["users"] if item["id"] != user_id]
        self._coverage_priority_user_ids = [uid for uid in self._coverage_priority_user_ids if uid != user_id]
        self._storage.save_state(state)
        return matched

    def set_user_enabled(self, user_id: str, enabled: bool) -> Dict[str, Any]:
        state = self._storage.load_state()
        matched = find_user_by_id(state["users"], user_id)
        if matched is None:
            raise ValueError("用户不存在")

        matched["enabled"] = enabled
        matched["updated_at"] = utc_now()
        if not enabled:
            self._coverage_priority_user_ids = [uid for uid in self._coverage_priority_user_ids if uid != user_id]
        self._storage.save_state(state)
        return matched

    def start_user_history_backfill(self, user_id: str) -> Dict[str, Any]:
        state = self._storage.load_state()
        user = find_user_by_id(state["users"], user_id)
        if user is None:
            raise ValueError("用户不存在")

        self._activate_history_sync(user)
        self._storage.save_state(state)
        self._enqueue_coverage_priority_user(user_id)
        return user

    def pause_user_history_backfill(self, user_id: str) -> Dict[str, Any]:
        state = self._storage.load_state()
        user = find_user_by_id(state["users"], user_id)
        if user is None:
            raise ValueError("用户不存在")

        history_sync = normalize_history_sync_state(user)
        history_sync["status"] = HISTORY_SYNC_STATUS_PAUSED
        history_sync["updated_at"] = utc_now()
        user["updated_at"] = utc_now()
        self._storage.save_state(state)
        return user

    def resume_user_history_backfill(self, user_id: str) -> Dict[str, Any]:
        state = self._storage.load_state()
        user = find_user_by_id(state["users"], user_id)
        if user is None:
            raise ValueError("用户不存在")

        self._activate_history_sync(user, reset_completed=False)
        self._storage.save_state(state)
        self._enqueue_coverage_priority_user(user_id)
        return user

    async def start_monitoring(
        self,
        mode: str,
        interval_hours: float,
        coverage_hours: float,
        *,
        resume: bool = False,
    ) -> Dict[str, Any]:
        validate_options(
            mode=mode,
            interval_hours=interval_hours,
            coverage_hours=coverage_hours,
        )
        if self._loop_task and not self._loop_task.done():
            raise ValueError("监控已在运行中")

        self._mode = mode
        self._interval_hours = interval_hours
        self._coverage_hours = coverage_hours
        self._wake_event.clear()
        if resume:
            self._restore_priority_from_progress()
        else:
            self._coverage_priority_user_ids = []
            self._clear_coverage_progress()

        await self._update_monitoring_state(is_running=True, desired_running=True)
        self._loop_task = asyncio.create_task(self._monitor_loop(), name="douyin-monitor")
        return self.get_status()

    async def stop_monitoring(self) -> Dict[str, Any]:
        """用户主动停止：清除运行意图与轮询进度。"""
        if self._loop_task is not None:
            task = self._loop_task
            self._loop_task = None
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        self._coverage_priority_user_ids = []
        self._wake_event.clear()
        self._clear_coverage_progress()
        await self._update_monitoring_state(is_running=False, desired_running=False)
        return self.get_status()

    def get_status(self) -> Dict[str, Any]:
        state = self._storage.load_state()
        running = bool(self._loop_task and not self._loop_task.done())

        monitoring = state["monitoring"]
        monitoring["is_running"] = running
        monitoring.setdefault("desired_running", False)
        monitoring["mode"] = self._mode
        monitoring["interval_hours"] = self._interval_hours
        monitoring["coverage_hours"] = self._coverage_hours
        monitoring.pop("interval_seconds", None)
        monitoring.pop("random_min_seconds", None)
        monitoring.pop("random_max_seconds", None)
        self._drop_legacy_scan_keys(monitoring)

        self._storage.save_state(state)
        return monitoring

    async def shutdown(self) -> None:
        """进程退出：保留 desired_running 与覆盖进度，便于重启后续跑。"""
        await self._graceful_pause_for_restart()
        await self._close_resource(self._crawler)
        await self._close_resource(self._notifier)

    async def _graceful_pause_for_restart(self) -> None:
        was_running = bool(self._loop_task and not self._loop_task.done())
        self._persist_runtime_checkpoint()
        if self._loop_task is not None:
            task = self._loop_task
            self._loop_task = None
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._coverage_priority_user_ids = []
        self._wake_event.clear()
        if was_running:
            await self._update_monitoring_state(is_running=False, desired_running=True)
        else:
            await self._update_monitoring_state(is_running=False)

    async def run_once(self) -> Dict[str, Any]:
        async with self._run_lock:
            state = self._storage.load_state()
            users = self._enabled_users(state)
            summary = await self._sync_service.sync_users(users, gaps=[])
            self._save_summary(state, summary)
            return summary

    async def run_user_once(self, user_id: str) -> Dict[str, Any]:
        async with self._run_lock:
            state = self._storage.load_state()
            user = find_user_by_id(state["users"], user_id)
            if user is None:
                raise ValueError("用户不存在")

            summary = {"checked_users": 1, "downloaded_items": 0, "errors": []}
            await self._sync_service.sync_one_user(user, summary)
            self._save_summary(state, summary)
            return summary

    async def run_user_history_backfill_once(self, user_id: str) -> Dict[str, Any]:
        async with self._run_lock:
            state = self._storage.load_state()
            user = find_user_by_id(state["users"], user_id)
            if user is None:
                raise ValueError("用户不存在")

            self._activate_history_sync(user, reset_completed=False)
            summary = {"checked_users": 0, "downloaded_items": 0, "errors": []}
            try:
                summary["downloaded_items"] = await self._sync_service.run_with_cookie_rotation(
                    lambda: self._sync_service.sync_history_backfill_step(user)
                )
                user["last_error"] = None
            except Exception as exc:
                user["last_error"] = str(exc)
                summary["errors"].append(
                    {"user_id": user["id"], "nickname": user["nickname"], "error": str(exc)}
                )
            finally:
                user["last_checked_at"] = utc_now()
                user["updated_at"] = utc_now()
            self._save_summary(state, summary)
            return summary

    async def _monitor_loop(self) -> None:
        while True:
            try:
                await self._drain_priority_users()
                if self._mode == MODE_INTERVAL:
                    await self._run_interval_cycle()
                    await self._drain_priority_users()
                    await self._sleep_interruptible(self._interval_hours * 3600.0)
                else:
                    await self._resume_coverage_idle_if_needed()
                    delay = await self._run_coverage_cycle()
                    await self._wait_coverage_idle(delay)
            except asyncio.CancelledError:
                self._persist_runtime_checkpoint()
                raise
            except Exception as exc:
                self._save_loop_error(str(exc))
                await asyncio.sleep(LOOP_ERROR_RETRY_SECONDS)

    async def _run_interval_cycle(self) -> None:
        async with self._run_lock:
            state = self._storage.load_state()
            users = self._enabled_users(state)
            summary = await self._sync_service.sync_users(users, build_interval_gaps(len(users)))
            self._save_summary(state, summary)
            await self._maybe_run_cookie_liveness(state)

    async def _resume_coverage_idle_if_needed(self) -> None:
        """若上次停在覆盖周期结束后的空闲等待，重启后补完剩余等待。"""
        progress = self._read_coverage_progress()
        if progress.get("phase") != COVERAGE_PHASE_IDLE:
            return
        remaining = _seconds_until(progress.get("idle_until"))
        if remaining > 0:
            await self._wait_coverage_idle(remaining)
        self._clear_coverage_progress()

    async def _run_coverage_cycle(self) -> float:
        """Run one coverage pass; release run_lock between users so manual runs can enter."""
        async with self._run_lock:
            state = self._storage.load_state()
            self._active_coverage_state = state
            progress = self._prepare_coverage_cycle_progress(state)
            processed_user_ids = set(progress["processed_user_ids"])
            pending_users = [
                user
                for user in self._enabled_users(state)
                if str(user.get("id") or "") not in processed_user_ids
            ]
            summary = {
                "checked_users": int(progress.get("checked_users") or 0),
                "downloaded_items": int(progress.get("downloaded_items") or 0),
                "errors": list(progress.get("errors") or []),
            }
            cycle_seconds = self._coverage_hours * 3600.0
            cycle_started_at = str(progress["cycle_started_at"])

        try:
            while True:
                delay = await self._coverage_process_next_user(
                    state,
                    pending_users=pending_users,
                    processed_user_ids=processed_user_ids,
                    summary=summary,
                    cycle_seconds=cycle_seconds,
                    cycle_started_at=cycle_started_at,
                )
                if delay is None:
                    break
                if delay > 0:
                    await self._sleep_interruptible(delay)

            async with self._run_lock:
                self._save_summary(state, summary)
                await self._maybe_run_cookie_liveness(state)
                remaining = max(0.0, cycle_seconds - _elapsed_since(cycle_started_at))
                self._checkpoint_coverage_idle(state, remaining)
                return remaining
        finally:
            self._active_coverage_state = None

    async def _coverage_process_next_user(
        self,
        state: Dict[str, Any],
        *,
        pending_users: List[Dict[str, Any]],
        processed_user_ids: Set[str],
        summary: Dict[str, Any],
        cycle_seconds: float,
        cycle_started_at: str,
    ) -> Optional[float]:
        """Sync one pending user under run_lock. Return delay seconds, or None if done."""
        async with self._run_lock:
            self._insert_priority_users_into_pending(state, pending_users, processed_user_ids)
            user = self._pop_next_coverage_user(pending_users, processed_user_ids)
            if user is None:
                return None

            user_id = str(user.get("id") or "")
            summary["checked_users"] += 1
            await self._sync_service.sync_one_user(user, summary)
            processed_user_ids.add(user_id)
            self._checkpoint_coverage_running(
                state,
                cycle_started_at=cycle_started_at,
                processed_user_ids=processed_user_ids,
                summary=summary,
            )
            self._insert_priority_users_into_pending(state, pending_users, processed_user_ids)
            return self._calculate_coverage_delay_wall(
                cycle_seconds=cycle_seconds,
                cycle_started_at=cycle_started_at,
                pending_count=len(pending_users),
            )

    @staticmethod
    def _pop_next_coverage_user(
        pending_users: List[Dict[str, Any]],
        processed_user_ids: Set[str],
    ) -> Optional[Dict[str, Any]]:
        while pending_users:
            user = pending_users.pop(0)
            user_id = str(user.get("id") or "")
            if not user.get("enabled", True) or user_id in processed_user_ids:
                continue
            return user
        return None

    async def _wait_coverage_idle(self, delay: float) -> None:
        """覆盖周期结束后的等待；新用户插队可打断并立即同步。"""
        if delay <= 0:
            self._clear_coverage_progress()
            return
        idle_until = (datetime.now(CHINA_TZ) + timedelta(seconds=delay)).isoformat(timespec="seconds")
        self._write_coverage_progress(
            {
                "phase": COVERAGE_PHASE_IDLE,
                "cycle_started_at": None,
                "idle_until": idle_until,
                "processed_user_ids": [],
                "checked_users": 0,
                "downloaded_items": 0,
                "errors": [],
                "priority_user_ids": list(self._coverage_priority_user_ids),
            }
        )
        deadline = datetime.now(CHINA_TZ) + timedelta(seconds=delay)
        while True:
            await self._drain_priority_users()
            remaining = max(0.0, (deadline - datetime.now(CHINA_TZ)).total_seconds())
            if remaining <= 0:
                self._clear_coverage_progress()
                return
            woken = await self._sleep_interruptible(remaining)
            if not woken:
                self._clear_coverage_progress()
                return

    async def _drain_priority_users(self) -> None:
        """在轮次间隙处理优先用户（增量 + 一页历史回填）。"""
        if not self._coverage_priority_user_ids:
            return
        if self._active_coverage_state is not None:
            # 覆盖周期进行中：由 _insert_priority_users_into_pending 插队处理
            return

        async with self._run_lock:
            if not self._coverage_priority_user_ids:
                return
            state = self._storage.load_state()
            priority_ids = self._pop_priority_user_ids()
            summary = {"checked_users": 0, "downloaded_items": 0, "errors": []}
            for user_id in priority_ids:
                user = find_user_by_id(state["users"], user_id)
                if user is None or not user.get("enabled", True):
                    continue
                summary["checked_users"] += 1
                await self._sync_service.sync_one_user(user, summary)
            if summary["checked_users"] > 0:
                self._save_summary(state, summary)

    def _pop_priority_user_ids(self) -> List[str]:
        priority_ids = list(self._coverage_priority_user_ids)
        self._coverage_priority_user_ids = []
        return priority_ids

    async def _sleep_interruptible(self, seconds: float) -> bool:
        """可被优先队列唤醒的 sleep；返回 True 表示被提前唤醒。"""
        if seconds <= 0:
            return False
        self._wake_event.clear()
        if self._coverage_priority_user_ids:
            return True
        try:
            await asyncio.wait_for(self._wake_event.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False

    def _calculate_coverage_delay_wall(
        self,
        *,
        cycle_seconds: float,
        cycle_started_at: str,
        pending_count: int,
    ) -> float:
        if pending_count <= 0:
            return 0.0
        remaining_seconds = max(0.0, cycle_seconds - _elapsed_since(cycle_started_at))
        future_gap_count = pending_count - 1
        return choose_coverage_delay(
            remaining_seconds=remaining_seconds,
            future_gap_count=future_gap_count,
        )

    def _empty_coverage_progress(self) -> Dict[str, Any]:
        return {
            "phase": COVERAGE_PHASE_RUNNING,
            "cycle_started_at": None,
            "idle_until": None,
            "processed_user_ids": [],
            "checked_users": 0,
            "downloaded_items": 0,
            "errors": [],
            "priority_user_ids": [],
        }

    def _read_coverage_progress(self) -> Dict[str, Any]:
        state = self._storage.load_state()
        raw = state["monitoring"].get("coverage_progress")
        base = self._empty_coverage_progress()
        if not isinstance(raw, dict):
            return base
        phase = str(raw.get("phase") or "").strip()
        if phase not in {COVERAGE_PHASE_RUNNING, COVERAGE_PHASE_IDLE}:
            phase = COVERAGE_PHASE_RUNNING
        processed = raw.get("processed_user_ids")
        errors = raw.get("errors")
        priority = raw.get("priority_user_ids")
        base.update(
            {
                "phase": phase,
                "cycle_started_at": raw.get("cycle_started_at"),
                "idle_until": raw.get("idle_until"),
                "processed_user_ids": [str(x) for x in processed] if isinstance(processed, list) else [],
                "checked_users": max(0, int(raw.get("checked_users") or 0)),
                "downloaded_items": max(0, int(raw.get("downloaded_items") or 0)),
                "errors": list(errors) if isinstance(errors, list) else [],
                "priority_user_ids": [str(x) for x in priority] if isinstance(priority, list) else [],
            }
        )
        return base

    def _write_coverage_progress(self, progress: Dict[str, Any]) -> None:
        state = self._storage.load_state()
        state["monitoring"]["coverage_progress"] = progress
        state["monitoring"]["updated_at"] = utc_now()
        self._storage.save_state(state)

    def _clear_coverage_progress(self) -> None:
        state = self._storage.load_state()
        if state["monitoring"].get("coverage_progress") is None:
            return
        state["monitoring"]["coverage_progress"] = None
        state["monitoring"]["updated_at"] = utc_now()
        self._storage.save_state(state)

    def _prepare_coverage_cycle_progress(self, state: Dict[str, Any]) -> Dict[str, Any]:
        progress = self._read_coverage_progress()
        cycle_seconds = self._coverage_hours * 3600.0
        reusable = (
            progress.get("phase") == COVERAGE_PHASE_RUNNING
            and progress.get("cycle_started_at")
            and _elapsed_since(progress.get("cycle_started_at")) < cycle_seconds
        )
        if reusable:
            return progress

        started_at = utc_now()
        fresh = self._empty_coverage_progress()
        fresh["cycle_started_at"] = started_at
        state["monitoring"]["coverage_progress"] = fresh
        state["monitoring"]["updated_at"] = utc_now()
        self._storage.save_state(state)
        return fresh

    def _checkpoint_coverage_running(
        self,
        state: Dict[str, Any],
        *,
        cycle_started_at: str,
        processed_user_ids: Set[str],
        summary: Dict[str, Any],
    ) -> None:
        progress = {
            "phase": COVERAGE_PHASE_RUNNING,
            "cycle_started_at": cycle_started_at,
            "idle_until": None,
            "processed_user_ids": list(processed_user_ids),
            "checked_users": int(summary.get("checked_users") or 0),
            "downloaded_items": int(summary.get("downloaded_items") or 0),
            "errors": list(summary.get("errors") or [])[-20:],
            "priority_user_ids": list(self._coverage_priority_user_ids),
        }
        state["monitoring"]["coverage_progress"] = progress
        state["monitoring"]["last_run_at"] = utc_now()
        state["monitoring"]["last_run_result"] = {
            "checked_users": progress["checked_users"],
            "downloaded_items": progress["downloaded_items"],
            "errors": progress["errors"],
            "coverage_progress": {
                "processed_count": len(progress["processed_user_ids"]),
                "phase": COVERAGE_PHASE_RUNNING,
            },
        }
        state["monitoring"]["updated_at"] = utc_now()
        self._storage.save_state(state)

    def _checkpoint_coverage_idle(self, state: Dict[str, Any], remaining: float) -> None:
        idle_until = None
        if remaining > 0:
            idle_until = (datetime.now(CHINA_TZ) + timedelta(seconds=remaining)).isoformat(timespec="seconds")
        progress = {
            "phase": COVERAGE_PHASE_IDLE,
            "cycle_started_at": None,
            "idle_until": idle_until,
            "processed_user_ids": [],
            "checked_users": 0,
            "downloaded_items": 0,
            "errors": [],
            "priority_user_ids": list(self._coverage_priority_user_ids),
        }
        state["monitoring"]["coverage_progress"] = progress if remaining > 0 else None
        state["monitoring"]["updated_at"] = utc_now()
        self._storage.save_state(state)

    def _persist_runtime_checkpoint(self) -> None:
        """进程退出前把优先队列写回 coverage_progress，便于续跑。"""
        state = self._storage.load_state()
        monitoring = state["monitoring"]
        progress = monitoring.get("coverage_progress")
        if not isinstance(progress, dict):
            if not self._coverage_priority_user_ids:
                return
            progress = self._empty_coverage_progress()
            progress["phase"] = COVERAGE_PHASE_IDLE
        progress["priority_user_ids"] = list(self._coverage_priority_user_ids)
        monitoring["coverage_progress"] = progress
        monitoring["updated_at"] = utc_now()
        self._storage.save_state(state)

    def _restore_priority_from_progress(self) -> None:
        progress = self._read_coverage_progress()
        priority_ids = progress.get("priority_user_ids") or []
        self._coverage_priority_user_ids = [str(x) for x in priority_ids if str(x).strip()]

    def _attach_user_to_active_coverage_state(self, user: Dict[str, Any]) -> None:
        state = self._active_coverage_state
        if state is None:
            return
        if find_user_by_id(state["users"], str(user.get("id") or "")) is not None:
            return
        state["users"].append(user)

    def _enqueue_coverage_priority_user(self, user_id: str) -> None:
        """将用户加入优先检查队列并唤醒监控循环（插队到下一个）。"""
        if self._loop_task is None or self._loop_task.done():
            return
        if user_id not in self._coverage_priority_user_ids:
            self._coverage_priority_user_ids.append(user_id)
        self._wake_event.set()

    def _insert_priority_users_into_pending(
        self,
        state: Dict[str, Any],
        pending_users: List[Dict[str, Any]],
        processed_user_ids: Set[str],
    ) -> None:
        if not self._coverage_priority_user_ids:
            return

        queued_ids = self._pop_priority_user_ids()
        users = state["users"]
        priority_users: List[Dict[str, Any]] = []
        priority_ids: Set[str] = set()
        for user_id in queued_ids:
            if user_id in processed_user_ids or user_id in priority_ids:
                continue

            user = find_user_by_id(users, user_id)
            if user is None or not user.get("enabled", True):
                continue

            priority_users.append(user)
            priority_ids.add(user_id)

        if not priority_users:
            return

        pending_users[:] = [
            *priority_users,
            *[user for user in pending_users if user.get("id") not in priority_ids],
        ]

    async def _maybe_run_cookie_liveness(self, state: Dict[str, Any]) -> None:
        service = self._cookie_liveness_service
        if service is None:
            return
        try:
            await service.maybe_run(state)
            self._storage.save_state(state)
        except Exception as exc:  # noqa: BLE001
            monitoring = state.setdefault("monitoring", {})
            record = monitoring.setdefault("cookie_liveness", {})
            record["last_error"] = str(exc)
            record["updated_at"] = utc_now()
            monitoring["updated_at"] = utc_now()
            self._storage.save_state(state)

    def _save_summary(self, state: Dict[str, Any], summary: Dict[str, Any]) -> None:
        monitoring = state["monitoring"]
        monitoring["last_run_at"] = utc_now()
        monitoring["last_run_result"] = summary
        monitoring["updated_at"] = utc_now()
        self._storage.save_state(state)

    def _save_loop_error(self, message: str) -> None:
        state = self._storage.load_state()
        monitoring = state["monitoring"]
        monitoring["last_run_at"] = utc_now()
        monitoring["last_run_result"] = {"loop_error": message}
        monitoring["updated_at"] = utc_now()
        self._storage.save_state(state)

    async def _update_monitoring_state(
        self,
        is_running: bool,
        *,
        desired_running: bool | None = None,
    ) -> None:
        state = self._storage.load_state()
        monitoring = state["monitoring"]
        monitoring["is_running"] = is_running
        if desired_running is not None:
            monitoring["desired_running"] = desired_running
        monitoring["mode"] = self._mode
        monitoring["interval_hours"] = self._interval_hours
        monitoring["coverage_hours"] = self._coverage_hours
        monitoring["updated_at"] = utc_now()
        monitoring.pop("interval_seconds", None)
        monitoring.pop("random_min_seconds", None)
        monitoring.pop("random_max_seconds", None)
        self._drop_legacy_scan_keys(monitoring)
        self._storage.save_state(state)

    def _enabled_users(self, state: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [user for user in state["users"] if user.get("enabled", True)]

    def _drop_legacy_scan_keys(self, monitoring: Dict[str, Any]) -> None:
        for key in LEGACY_COOKIE_KEYS:
            monitoring.pop(key, None)

    def _activate_history_sync(
        self,
        user: Dict[str, Any],
        *,
        reset_completed: bool = True,
    ) -> None:
        history_sync = normalize_history_sync_state(user)
        restart_required = history_sync["status"] in {
            HISTORY_SYNC_STATUS_COMPLETED,
            HISTORY_SYNC_STATUS_IDLE,
        }
        should_reset = restart_required and reset_completed
        if should_reset:
            user["history_sync"] = build_history_sync_state(
                status=HISTORY_SYNC_STATUS_PENDING,
                page_size=int(history_sync["page_size"] or HISTORY_SYNC_PAGE_SIZE),
                started_at=utc_now(),
                updated_at=utc_now(),
            )
        else:
            history_sync["status"] = HISTORY_SYNC_STATUS_PENDING
            history_sync["has_more"] = True
            history_sync["started_at"] = history_sync.get("started_at") or utc_now()
            history_sync["updated_at"] = utc_now()
            history_sync["last_error"] = None
            history_sync["completed_at"] = None
        user["updated_at"] = utc_now()

    async def _close_resource(self, resource: Any) -> None:
        close_method = getattr(resource, "aclose", None)
        if close_method is None:
            return
        result = close_method()
        if asyncio.iscoroutine(result):
            await result
