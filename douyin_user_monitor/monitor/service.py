import asyncio
import logging
import uuid
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from douyin_user_monitor.monitor.content_filter_queue import RESUME_WINDOW_DAYS as CONTENT_FILTER_RESUME_WINDOW_DAYS
from douyin_user_monitor.monitor.cookie_liveness_service import CookieLivenessService
from douyin_user_monitor.monitor.crawler_protocol import MonitorCrawlerProtocol
from douyin_user_monitor.monitor.dashboard_payload import (
    DASHBOARD_ACTIVITY_LIMIT,
    DASHBOARD_RECENT_RECORDS_PER_USER,
    project_dashboard_users,
    project_public_status,
)
from douyin_user_monitor.monitor.downloader import AwemeAssetDownloader
from douyin_user_monitor.monitor.errors import sanitize_public_error
from douyin_user_monitor.monitor.history_sync import (
    HISTORY_SYNC_PAGE_SIZE,
    HISTORY_SYNC_STATUS_COMPLETED,
    HISTORY_SYNC_STATUS_IDLE,
    HISTORY_SYNC_STATUS_PAUSED,
    HISTORY_SYNC_STATUS_PENDING,
    build_history_sync_state,
    normalize_history_sync_state,
)
from douyin_user_monitor.monitor.nickname_history import record_nickname_change
from douyin_user_monitor.monitor.notifier import MonitorNotifierProtocol, NoopMonitorNotifier
from douyin_user_monitor.monitor.profile_parser import (
    ACCOUNT_STATUS_BANNED,
    ACCOUNT_STATUS_DELETED,
    ACCOUNT_STATUS_NORMAL,
)
from douyin_user_monitor.monitor.record_store import RecordStore
from douyin_user_monitor.monitor.schedule import (
    LOOP_ERROR_RETRY_SECONDS,
    MODE_INTERVAL,
    build_interval_gaps,
    choose_coverage_delay,
    validate_options,
)
from douyin_user_monitor.monitor.schedule import (
    MIN_INTERVAL_HOURS as SCHEDULE_MIN_INTERVAL_HOURS,
)
from douyin_user_monitor.monitor.storage import (
    DEFAULT_COVERAGE_HOURS,
    DEFAULT_INTERVAL_HOURS,
    DEFAULT_MODE,
    MonitorStorage,
)
from douyin_user_monitor.monitor.sync_utils import merge_synced_downloads
from douyin_user_monitor.monitor.timeutils import CHINA_TZ, cn_now
from douyin_user_monitor.monitor.user_lookup import find_user_by_id, find_user_by_sec_uid
from douyin_user_monitor.monitor.user_sync import UserSyncService

LOGGER = logging.getLogger("uvicorn.error")
MIN_INTERVAL_HOURS = SCHEDULE_MIN_INTERVAL_HOURS
COVERAGE_PHASE_RUNNING = "running"
COVERAGE_PHASE_IDLE = "idle"
LEGACY_COOKIE_KEYS = (
    "cookie_last_updated_at",
    "cookie_last_updated_source",
    "cookie_last_preview",
    "cookie_last_length",
    "cookie_last_upstream_result",
)
# 资料补全（含失败标记）只会改动这些字段；锁内提交时仅回写它们
PROFILE_HYDRATION_FIELDS = (
    "nickname",
    "avatar_url",
    "account_status",
    "account_status_label",
    "account_status_reason",
    "account_status_updated_at",
    "last_error",
    "updated_at",
)
# sync 锁外网络阶段（sync_one_user / sync_users）实际写入的字段；锁内合并回盘时仅覆盖这些。
# 排除 enabled：它由 API 侧手动启停拥有；封号自动停用改由 _merge_user_sync_fields 按状态转变派生落盘。
# 纳入 nickname：昵称仅由抖音 profile 决定、无手动改端点，抖音端改名须随 sync 落盘。
USER_SYNC_MERGE_FIELDS = (
    "nickname",
    "avatar_url",
    "account_status",
    "account_status_label",
    "account_status_reason",
    "account_status_updated_at",
    "last_error",
    "last_checked_at",
    "last_aweme_id",
    "last_download_at",
    "history_sync",
    "updated_at",
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
        *,
        cover_publisher=None,
        content_filter=None,
        content_filter_queue=None,
        detail_cookie_pool=None,
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
            cover_publisher=cover_publisher,
            content_filter=content_filter,
            content_filter_queue=content_filter_queue,
            detail_cookie_pool=detail_cookie_pool,
        )
        self._content_filter_queue = content_filter_queue

        self._run_lock = asyncio.Lock()
        self._loop_task: Optional[asyncio.Task] = None

        self._mode = DEFAULT_MODE
        self._interval_hours = DEFAULT_INTERVAL_HOURS
        self._coverage_hours = DEFAULT_COVERAGE_HOURS

        self._coverage_priority_user_ids: List[str] = []
        self._coverage_cycle_active = False
        self._wake_event = asyncio.Event()
        self._load_runtime_state()

    def _load_runtime_state(self) -> None:
        """构造时只读状态：任何 import（脚本、测试、临时实例）都不能写线上 state。"""
        monitoring = self._storage.load_state()["monitoring"]
        # 优先用 desired_running（用户意图）；兼容旧数据里只有 is_running=True 的情况
        if "desired_running" in monitoring:
            self._was_running = bool(monitoring.get("desired_running"))
        else:
            self._was_running = bool(monitoring.get("is_running", False))
        self._apply_runtime_from_monitoring(monitoring)

    def _reset_runtime_state(self) -> None:
        """进程启动时把上次残留的运行标记归零，并清掉旧字段。"""
        was_running = self._was_running

        def _apply(state: Dict[str, Any]) -> None:
            monitoring = state["monitoring"]
            monitoring.setdefault("desired_running", was_running)
            monitoring["is_running"] = False
            monitoring["updated_at"] = cn_now()
            monitoring.pop("interval_seconds", None)
            monitoring.pop("random_min_seconds", None)
            monitoring.pop("random_max_seconds", None)
            self._drop_legacy_scan_keys(monitoring)

        self._storage.update_state(_apply)

    async def auto_resume(self) -> None:
        """进程启动后若用户意图仍是监控中，则恢复循环并接续覆盖进度。"""
        self._reset_runtime_state()
        self._start_content_filter_queue()
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

    async def list_users_async(self) -> List[Dict[str, Any]]:
        return (await self._storage.load_state_async())["users"]

    async def list_users_with_profile(self) -> List[Dict[str, Any]]:
        snapshot = self._storage.load_state()
        hydrated = await self._hydrate_missing_user_profiles(snapshot)
        if not hydrated:
            return snapshot["users"]
        return self._commit_profile_hydration(hydrated)["users"]

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
        existed = find_user_by_sec_uid(self._storage.load_state()["users"], sec_user_id)
        if existed is not None:
            return {"status": "exists", "user": await self._refresh_existing_user(existed)}

        try:
            profile = await self._sync_service.resolve_profile_snapshot(sec_user_id)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(
                f"用户主页可解析 sec_user_id={sec_user_id}，但获取用户资料失败: {exc}"
            ) from exc
        user = self._build_new_user(
            profile_url=raw_profile_text,
            sec_user_id=sec_user_id,
            profile=profile,
        )
        result: Dict[str, Any] = {"status": "created", "user": user}

        def _commit(state: Dict[str, Any]) -> None:
            # 网络解析期间另一请求可能已添加同一用户：锁内重新检查，避免重复条目
            concurrent = find_user_by_sec_uid(state["users"], sec_user_id)
            if concurrent is not None:
                result["status"] = "exists"
                result["user"] = concurrent
                return
            state["users"].append(user)

        self._storage.update_state(_commit)
        if result["status"] == "created":
            # 覆盖周期已改为按 id 从磁盘态取用户，新增用户经优先队列在下一轮被 claim 捞回
            self._enqueue_coverage_priority_user(user["id"])
        return result

    @staticmethod
    def _build_new_user(*, profile_url: str, sec_user_id: str, profile: Any) -> Dict[str, Any]:
        now = cn_now()
        return {
            "id": str(uuid.uuid4()),
            "profile_url": profile_url,
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
            "history_sync": build_history_sync_state(
                status=HISTORY_SYNC_STATUS_PENDING,
                page_size=HISTORY_SYNC_PAGE_SIZE,
            ),
            "last_error": None,
        }

    async def _refresh_existing_user(self, existed: Dict[str, Any]) -> Dict[str, Any]:
        changed = await self._hydrate_missing_user_profile(existed)
        if not changed:
            return existed
        state = self._commit_profile_hydration([existed])
        return find_user_by_id(state["users"], str(existed.get("id") or "")) or existed

    async def _hydrate_missing_user_profiles(self, state: Dict[str, Any]) -> List[Dict[str, Any]]:
        """在快照上完成网络补全，返回发生变更的用户（供锁内提交）。"""
        hydrated: List[Dict[str, Any]] = []
        for user in state["users"]:
            try:
                changed = await self._hydrate_missing_user_profile(user)
            except Exception as exc:  # noqa: BLE001
                changed = self._record_profile_hydration_error(user, exc)
            if changed:
                hydrated.append(user)
        return hydrated

    def _commit_profile_hydration(self, hydrated_users: List[Dict[str, Any]]) -> Dict[str, Any]:
        """网络补全已在快照上完成；锁内仅按 id 回写补全字段。"""
        updates = {
            str(user.get("id") or ""): {key: user.get(key) for key in PROFILE_HYDRATION_FIELDS}
            for user in hydrated_users
        }

        def _apply(state: Dict[str, Any]) -> None:
            for user in state["users"]:
                fields = updates.get(str(user.get("id") or ""))
                if fields is not None:
                    user.update(fields)

        return self._storage.update_state(_apply)

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
        error_message = f"资料补全失败: {sanitize_public_error(exc)}"
        changed = user.get("last_error") != error_message
        user["last_error"] = error_message
        if changed:
            user["updated_at"] = cn_now()
        return changed

    def _merge_profile_snapshot(self, user: Dict[str, Any], profile: Any) -> bool:
        now = cn_now()
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

    def _update_user(self, user_id: str, mutate: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
        """锁内按 id 定位用户并提交变更；用户不存在时不写盘直接抛错。"""
        found: Dict[str, Any] = {}

        def _apply(state: Dict[str, Any]) -> None:
            user = find_user_by_id(state["users"], user_id)
            if user is None:
                raise ValueError("用户不存在")
            mutate(user)
            found["user"] = user

        self._storage.update_state(_apply)
        return found["user"]

    def remove_user(self, user_id: str) -> Dict[str, Any]:
        found: Dict[str, Any] = {}

        def _remove(state: Dict[str, Any]) -> None:
            matched = find_user_by_id(state["users"], user_id)
            if matched is None:
                raise ValueError("用户不存在")
            found["user"] = matched
            state["users"] = [item for item in state["users"] if item["id"] != user_id]

        self._storage.update_state(_remove)
        # JSON 先落盘再删记录：中途失败最多留一行孤儿记录，不会出现用户还在、记录没了
        self._storage.records.delete(user_id)
        self._coverage_priority_user_ids = [uid for uid in self._coverage_priority_user_ids if uid != user_id]
        return found["user"]

    def set_user_enabled(self, user_id: str, enabled: bool) -> Dict[str, Any]:
        def _set(user: Dict[str, Any]) -> None:
            user["enabled"] = enabled
            user["updated_at"] = cn_now()

        matched = self._update_user(user_id, _set)
        if not enabled:
            self._coverage_priority_user_ids = [uid for uid in self._coverage_priority_user_ids if uid != user_id]
        return matched

    def start_user_history_backfill(self, user_id: str) -> Dict[str, Any]:
        user = self._update_user(user_id, self._activate_history_sync)
        self._enqueue_coverage_priority_user(user_id)
        return user

    def pause_user_history_backfill(self, user_id: str) -> Dict[str, Any]:
        def _pause(user: Dict[str, Any]) -> None:
            history_sync = normalize_history_sync_state(user)
            history_sync["status"] = HISTORY_SYNC_STATUS_PAUSED
            history_sync["updated_at"] = cn_now()
            user["updated_at"] = cn_now()

        return self._update_user(user_id, _pause)

    def resume_user_history_backfill(self, user_id: str) -> Dict[str, Any]:
        user = self._update_user(
            user_id,
            lambda user: self._activate_history_sync(user, reset_completed=False),
        )
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
        """纯读接口：仅在内存态上做展示层修正，不写盘（前端轮询高频调用）。

        返回独立 dict，不就地改 storage 缓存，避免污染进程内状态。
        """
        state = self._storage.load_state()
        running = bool(self._loop_task and not self._loop_task.done())

        raw = state["monitoring"] if isinstance(state.get("monitoring"), dict) else {}
        monitoring = dict(raw)
        monitoring["is_running"] = running
        monitoring.setdefault("desired_running", False)
        monitoring["mode"] = self._mode
        monitoring["interval_hours"] = self._interval_hours
        monitoring["coverage_hours"] = self._coverage_hours
        monitoring.pop("interval_seconds", None)
        monitoring.pop("random_min_seconds", None)
        monitoring.pop("random_max_seconds", None)
        self._drop_legacy_scan_keys(monitoring)
        return monitoring

    def get_public_status(self) -> Dict[str, Any]:
        """面板用精简状态：去掉 Cookie 原文与大体积 samples。"""
        return project_public_status(self.get_status())

    async def list_dashboard_users(
        self,
        *,
        recent_records_per_user: int = DASHBOARD_RECENT_RECORDS_PER_USER,
        activity_limit: int = DASHBOARD_ACTIVITY_LIMIT,
    ) -> Dict[str, Any]:
        """大盘列表：省略 aweme_ids，只保留少量最近下载记录 + 全局动态。"""
        users = self._storage.records.attach_all(await self.list_users_with_profile())
        return project_dashboard_users(
            users,
            recent_records_per_user=recent_records_per_user,
            activity_limit=activity_limit,
        )

    def _start_content_filter_queue(self) -> None:
        queue = self._content_filter_queue
        if queue is None:
            return
        since = (datetime.now(CHINA_TZ) - timedelta(days=CONTENT_FILTER_RESUME_WINDOW_DAYS)).isoformat(timespec="seconds")
        added = queue.enqueue_unlabeled(self._storage.load_state().get("users", []), since=since)
        if added:
            LOGGER.info("内容标注队列：启动时补回 %d 条未标注的新作品", added)
        queue.start()

    async def shutdown(self) -> None:
        """进程退出：保留 desired_running 与覆盖进度，便于重启后续跑。"""
        if self._content_filter_queue is not None:
            await self._content_filter_queue.stop()
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
            # 锁外网络阶段改副本，不触碰磁盘态；提交时按 id 字段级合并回盘
            users = deepcopy(self._enabled_users(self._storage.load_state()))
            summary = await self._sync_service.sync_users(users, gaps=[])
            self._commit_user_sync_results(users, summary)
            return summary

    async def run_user_once(self, user_id: str) -> Dict[str, Any]:
        async with self._run_lock:
            user = find_user_by_id(self._storage.load_state()["users"], user_id)
            if user is None:
                raise ValueError("用户不存在")

            user = deepcopy(user)
            summary = {"checked_users": 1, "downloaded_items": 0, "errors": []}
            await self._sync_service.sync_one_user(user, summary)
            self._commit_user_sync_results([user], summary)
            return summary

    async def run_user_history_backfill_once(self, user_id: str) -> Dict[str, Any]:
        async with self._run_lock:
            user = find_user_by_id(self._storage.load_state()["users"], user_id)
            if user is None:
                raise ValueError("用户不存在")

            user = deepcopy(user)
            self._activate_history_sync(user, reset_completed=False)
            summary = {"checked_users": 0, "downloaded_items": 0, "errors": []}
            try:
                summary["downloaded_items"] = await self._sync_service.run_with_cookie_rotation(
                    lambda: self._sync_service.sync_history_backfill_step(user)
                )
                user["last_error"] = None
            except Exception as exc:
                user["last_error"] = sanitize_public_error(exc)
                summary["errors"].append(
                    {"user_id": user["id"], "nickname": user["nickname"], "error": sanitize_public_error(exc)}
                )
            finally:
                user["last_checked_at"] = cn_now()
                user["updated_at"] = cn_now()
            self._commit_user_sync_results([user], summary)
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
                self._save_loop_error(sanitize_public_error(exc))
                await asyncio.sleep(LOOP_ERROR_RETRY_SECONDS)

    async def _run_interval_cycle(self) -> None:
        async with self._run_lock:
            users = deepcopy(self._enabled_users(self._storage.load_state()))
            summary = await self._sync_service.sync_users(users, build_interval_gaps(len(users)))
            self._commit_user_sync_results(users, summary)
            await self._maybe_run_cookie_liveness(self._storage.load_state())

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
        """Run one coverage pass.

        循环只持有 pending/processed 的 user_id，不再持整份 state 或 user dict 引用：
        每个用户分「锁内取快照 → 锁外同步 → 锁内按 id 合并回盘」三段，
        期间被 API 删除/禁用的用户不会被陈旧快照整体回写复活。
        """
        async with self._run_lock:
            progress = self._prepare_coverage_cycle_progress()
            processed_user_ids: Set[str] = {str(x) for x in progress["processed_user_ids"]}
            state = self._storage.load_state()
            pending_user_ids: List[str] = [
                str(user.get("id") or "")
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

        self._coverage_cycle_active = True
        try:
            while True:
                delay = await self._coverage_process_next_user(
                    pending_user_ids=pending_user_ids,
                    processed_user_ids=processed_user_ids,
                    summary=summary,
                    cycle_seconds=cycle_seconds,
                    cycle_started_at=cycle_started_at,
                )
                if delay is None:
                    break
                if delay > 0:
                    await self._sleep_interruptible(delay)

            remaining = max(0.0, cycle_seconds - _elapsed_since(cycle_started_at))
            async with self._run_lock:
                self._commit_coverage_idle(summary, remaining)
                await self._maybe_run_cookie_liveness(self._storage.load_state())
            return remaining
        finally:
            self._coverage_cycle_active = False

    async def _coverage_process_next_user(
        self,
        *,
        pending_user_ids: List[str],
        processed_user_ids: Set[str],
        summary: Dict[str, Any],
        cycle_seconds: float,
        cycle_started_at: str,
    ) -> Optional[float]:
        """Sync one pending user under run_lock. Return delay seconds, or None if done."""
        async with self._run_lock:
            self._insert_priority_user_ids_into_pending(pending_user_ids, processed_user_ids)
            claimed = self._claim_next_coverage_user(pending_user_ids, processed_user_ids)
            if claimed is None:
                return None

            user_id, user_snapshot = claimed
            summary["checked_users"] += 1
            # 锁外网络 + 下载：sync_one_user 原地改快照副本，不触碰磁盘态
            await self._sync_service.sync_one_user(user_snapshot, summary)
            processed_user_ids.add(user_id)
            self._commit_coverage_user_sync(
                user_id=user_id,
                user_snapshot=user_snapshot,
                processed_user_ids=processed_user_ids,
                summary=summary,
                cycle_started_at=cycle_started_at,
            )
            self._insert_priority_user_ids_into_pending(pending_user_ids, processed_user_ids)
            return self._calculate_coverage_delay_wall(
                cycle_seconds=cycle_seconds,
                cycle_started_at=cycle_started_at,
                pending_count=len(pending_user_ids),
            )

    def _claim_next_coverage_user(
        self,
        pending_user_ids: List[str],
        processed_user_ids: Set[str],
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        """按 id 从磁盘态取下一个待处理用户的快照副本；已删除/禁用者跳过。"""
        users = self._storage.load_state()["users"]
        while pending_user_ids:
            user_id = pending_user_ids.pop(0)
            if user_id in processed_user_ids:
                continue
            user = find_user_by_id(users, user_id)
            if user is None or not user.get("enabled", True):
                continue
            return user_id, deepcopy(user)
        return None

    @staticmethod
    def _merge_user_sync_fields(
        disk_user: Dict[str, Any], synced_snapshot: Dict[str, Any], records: RecordStore,
    ) -> None:
        prev_status = str(disk_user.get("account_status") or ACCOUNT_STATUS_NORMAL)
        prev_nickname = str(disk_user.get("nickname") or "")
        merge_synced_downloads(disk_user, synced_snapshot, records)
        for key in USER_SYNC_MERGE_FIELDS:
            if key in synced_snapshot:
                disk_user[key] = synced_snapshot[key]
        # enabled 归 API 侧、刻意不入合并白名单；仅在账号从正常转为封禁/删除时于此派生自动停用落盘，
        # 这样既不覆盖用户手动启停，又不丢失封号自动暂停（该字段在锁外快照上的变更会被字段级合并丢弃）。
        new_status = str(disk_user.get("account_status") or ACCOUNT_STATUS_NORMAL)
        if prev_status == ACCOUNT_STATUS_NORMAL and new_status in (ACCOUNT_STATUS_BANNED, ACCOUNT_STATUS_DELETED):
            disk_user["enabled"] = False
        # nickname_history 同样刻意不入白名单：它按磁盘旧值在此派生，
        # 若走白名单会被锁外快照里的旧副本覆盖。归档目录以下载当时的昵称命名，
        # 改名后档案会分散，这里留下旧名以便认出网盘上的历史目录。
        record_nickname_change(disk_user, prev_nickname)

    def _commit_user_sync_results(
        self,
        synced_users: List[Dict[str, Any]],
        summary: Dict[str, Any],
    ) -> None:
        """一次 update_state：按 id 字段级合并本轮 sync 增量并写 monitoring 汇总。

        已被并发删除的用户跳过（不复活）；未处理的用户与 cookie_pool 保持磁盘态不变。
        """

        def _apply(state: Dict[str, Any]) -> None:
            for synced in synced_users:
                disk_user = find_user_by_id(state["users"], str(synced.get("id") or ""))
                if disk_user is not None:
                    self._merge_user_sync_fields(disk_user, synced, self._storage.records)
            monitoring = state["monitoring"]
            monitoring["last_run_at"] = cn_now()
            monitoring["last_run_result"] = summary
            monitoring["updated_at"] = cn_now()

        self._storage.update_state(_apply)

    def _commit_coverage_user_sync(
        self,
        *,
        user_id: str,
        user_snapshot: Dict[str, Any],
        processed_user_ids: Set[str],
        summary: Dict[str, Any],
        cycle_started_at: str,
    ) -> None:
        """锁内按 id 重新定位磁盘态用户，字段级合并同步增量，并推进覆盖进度。"""
        progress = self._build_running_progress(cycle_started_at, processed_user_ids, summary)
        last_run_result = {
            "checked_users": progress["checked_users"],
            "downloaded_items": progress["downloaded_items"],
            "errors": progress["errors"],
            "coverage_progress": {
                "processed_count": len(progress["processed_user_ids"]),
                "phase": COVERAGE_PHASE_RUNNING,
            },
        }

        def _apply(state: Dict[str, Any]) -> None:
            disk_user = find_user_by_id(state["users"], user_id)
            # 用户已被并发删除：丢弃本次增量，绝不复活
            if disk_user is not None:
                self._merge_user_sync_fields(disk_user, user_snapshot, self._storage.records)
            monitoring = state["monitoring"]
            monitoring["coverage_progress"] = progress
            monitoring["last_run_at"] = cn_now()
            monitoring["last_run_result"] = last_run_result
            monitoring["updated_at"] = cn_now()

        self._storage.update_state(_apply)

    def _build_running_progress(
        self,
        cycle_started_at: str,
        processed_user_ids: Set[str],
        summary: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {
            "phase": COVERAGE_PHASE_RUNNING,
            "cycle_started_at": cycle_started_at,
            "idle_until": None,
            "processed_user_ids": list(processed_user_ids),
            "checked_users": int(summary.get("checked_users") or 0),
            "downloaded_items": int(summary.get("downloaded_items") or 0),
            "errors": list(summary.get("errors") or [])[-20:],
            "priority_user_ids": list(self._coverage_priority_user_ids),
        }

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
        if self._coverage_cycle_active:
            # 覆盖周期进行中：由 _insert_priority_user_ids_into_pending 插队处理
            return

        async with self._run_lock:
            if not self._coverage_priority_user_ids:
                return
            state = self._storage.load_state()
            priority_ids = self._pop_priority_user_ids()
            summary = {"checked_users": 0, "downloaded_items": 0, "errors": []}
            synced_users: List[Dict[str, Any]] = []
            for user_id in priority_ids:
                user = find_user_by_id(state["users"], user_id)
                if user is None or not user.get("enabled", True):
                    continue
                user = deepcopy(user)
                summary["checked_users"] += 1
                await self._sync_service.sync_one_user(user, summary)
                synced_users.append(user)
            if synced_users:
                self._commit_user_sync_results(synced_users, summary)

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
        def _apply(state: Dict[str, Any]) -> None:
            state["monitoring"]["coverage_progress"] = progress
            state["monitoring"]["updated_at"] = cn_now()

        self._storage.update_state(_apply)

    def _clear_coverage_progress(self) -> None:
        def _apply(state: Dict[str, Any]) -> None:
            if state["monitoring"].get("coverage_progress") is None:
                return
            state["monitoring"]["coverage_progress"] = None
            state["monitoring"]["updated_at"] = cn_now()

        self._storage.update_state(_apply)

    def _prepare_coverage_cycle_progress(self) -> Dict[str, Any]:
        progress = self._read_coverage_progress()
        cycle_seconds = self._coverage_hours * 3600.0
        reusable = (
            progress.get("phase") == COVERAGE_PHASE_RUNNING
            and progress.get("cycle_started_at")
            and _elapsed_since(progress.get("cycle_started_at")) < cycle_seconds
        )
        if reusable:
            return progress

        fresh = self._empty_coverage_progress()
        fresh["cycle_started_at"] = cn_now()

        def _apply(state: Dict[str, Any]) -> None:
            state["monitoring"]["coverage_progress"] = fresh
            state["monitoring"]["updated_at"] = cn_now()

        self._storage.update_state(_apply)
        return fresh

    def _build_idle_progress(self, remaining: float) -> Optional[Dict[str, Any]]:
        if remaining <= 0:
            return None
        idle_until = (datetime.now(CHINA_TZ) + timedelta(seconds=remaining)).isoformat(timespec="seconds")
        return {
            "phase": COVERAGE_PHASE_IDLE,
            "cycle_started_at": None,
            "idle_until": idle_until,
            "processed_user_ids": [],
            "checked_users": 0,
            "downloaded_items": 0,
            "errors": [],
            "priority_user_ids": list(self._coverage_priority_user_ids),
        }

    def _commit_coverage_idle(self, summary: Dict[str, Any], remaining: float) -> None:
        """周期收尾：一次 update_state 写回汇总与 idle 进度，不整体覆盖磁盘态。"""
        idle_progress = self._build_idle_progress(remaining)

        def _apply(state: Dict[str, Any]) -> None:
            monitoring = state["monitoring"]
            monitoring["last_run_at"] = cn_now()
            monitoring["last_run_result"] = summary
            monitoring["coverage_progress"] = idle_progress
            monitoring["updated_at"] = cn_now()

        self._storage.update_state(_apply)

    def _persist_runtime_checkpoint(self) -> None:
        """进程退出前把优先队列写回 coverage_progress，便于续跑。"""
        priority_ids = list(self._coverage_priority_user_ids)

        def _apply(state: Dict[str, Any]) -> None:
            monitoring = state["monitoring"]
            progress = monitoring.get("coverage_progress")
            if not isinstance(progress, dict):
                if not priority_ids:
                    return
                progress = self._empty_coverage_progress()
                progress["phase"] = COVERAGE_PHASE_IDLE
            progress["priority_user_ids"] = priority_ids
            monitoring["coverage_progress"] = progress
            monitoring["updated_at"] = cn_now()

        self._storage.update_state(_apply)

    def _restore_priority_from_progress(self) -> None:
        progress = self._read_coverage_progress()
        priority_ids = progress.get("priority_user_ids") or []
        self._coverage_priority_user_ids = [str(x) for x in priority_ids if str(x).strip()]

    def _enqueue_coverage_priority_user(self, user_id: str) -> None:
        """将用户加入优先检查队列并唤醒监控循环（插队到下一个）。"""
        if self._loop_task is None or self._loop_task.done():
            return
        if user_id not in self._coverage_priority_user_ids:
            self._coverage_priority_user_ids.append(user_id)
        self._wake_event.set()

    def _insert_priority_user_ids_into_pending(
        self,
        pending_user_ids: List[str],
        processed_user_ids: Set[str],
    ) -> None:
        """把优先队列的 user_id 插到 pending 前部（去重、跳过已处理）。

        存在性与 enabled 校验交由 _claim_next_coverage_user 在取用时按磁盘态判定。
        """
        if not self._coverage_priority_user_ids:
            return

        queued_ids = self._pop_priority_user_ids()
        front: List[str] = []
        promoted: Set[str] = set()
        for user_id in queued_ids:
            if user_id in processed_user_ids or user_id in promoted:
                continue
            front.append(user_id)
            promoted.add(user_id)

        if not front:
            return

        pending_user_ids[:] = [
            *front,
            *[uid for uid in pending_user_ids if uid not in promoted],
        ]

    async def _maybe_run_cookie_liveness(self, state: Dict[str, Any]) -> None:
        service = self._cookie_liveness_service
        if service is None:
            return
        # 探测网络在锁外的 state 快照上完成；仅把 cookie_liveness 字段合并回最新磁盘态，
        # 不整体回写快照，避免探测期间并发写入的 users/cookie_pool 被陈旧快照覆盖。
        try:
            # 下载时间样本来自作品记录，给探测一个带记录的用户视图
            payload = await service.maybe_run({**state, "users": self._storage.records.attach_all(state["users"])})
        except Exception as exc:  # noqa: BLE001
            self._merge_cookie_liveness_error(sanitize_public_error(exc))
            return
        if payload is None:
            return
        self._merge_cookie_liveness_payload(payload)

    def _merge_cookie_liveness_payload(self, payload: Dict[str, Any]) -> None:
        def _apply(state: Dict[str, Any]) -> None:
            monitoring = state["monitoring"]
            monitoring["cookie_liveness"] = payload
            monitoring["updated_at"] = cn_now()

        self._storage.update_state(_apply)

    def _merge_cookie_liveness_error(self, message: str) -> None:
        def _apply(state: Dict[str, Any]) -> None:
            monitoring = state["monitoring"]
            record = monitoring.setdefault("cookie_liveness", {})
            record["last_error"] = message
            record["updated_at"] = cn_now()
            monitoring["updated_at"] = cn_now()

        self._storage.update_state(_apply)

    def _save_loop_error(self, message: str) -> None:
        state = self._storage.load_state()
        monitoring = state["monitoring"]
        monitoring["last_run_at"] = cn_now()
        monitoring["last_run_result"] = {"loop_error": message}
        monitoring["updated_at"] = cn_now()
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
        monitoring["updated_at"] = cn_now()
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
                started_at=cn_now(),
                updated_at=cn_now(),
            )
        else:
            history_sync["status"] = HISTORY_SYNC_STATUS_PENDING
            if not history_sync["pending_awemes"]:
                history_sync["has_more"] = True
                history_sync["last_error"] = None
            history_sync["started_at"] = history_sync.get("started_at") or cn_now()
            history_sync["updated_at"] = cn_now()
            history_sync["completed_at"] = None
        user["updated_at"] = cn_now()

    async def _close_resource(self, resource: Any) -> None:
        close_method = getattr(resource, "aclose", None)
        if close_method is None:
            return
        result = close_method()
        if asyncio.iscoroutine(result):
            await result
