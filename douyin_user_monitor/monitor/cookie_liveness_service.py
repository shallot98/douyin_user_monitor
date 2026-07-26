from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from douyin_user_monitor.monitor.cookie_download_liveness import (
    MAX_DOWNLOAD_SAMPLES,
    MIN_DOWNLOAD_LOOKBACK_DAYS,
    collect_incremental_download_samples,
    summarize_download_lag,
)
from douyin_user_monitor.monitor.cookie_liveness import (
    SAMPLE_POST_COUNT,
    SECONDS_PER_DAY,
    SIGNAL_COMBINED,
    STATUS_DEGRADED,
    STATUS_EXPIRED,
    STATUS_HEALTHY,
    STATUS_UNKNOWN,
    CookieAlertSender,
    CookieLivenessConfig,
    CookieLivenessResult,
    CookieProbeCrawler,
    default_now,
    evaluate_cookie_liveness,
    evaluate_download_liveness,
    extract_latest_create_time,
    is_check_due,
    parse_iso_datetime,
    select_probe_users,
    should_send_cookie_alert,
)
from douyin_user_monitor.monitor.timeutils import cn_now


class CookieLivenessService:
    def __init__(
        self,
        *,
        crawler: CookieProbeCrawler,
        config: CookieLivenessConfig,
        alerter: Optional[CookieAlertSender] = None,
        now_provider: Optional[Any] = None,
    ):
        self._crawler = crawler
        self._config = config
        self._alerter = alerter
        self._now_provider = now_provider or default_now

    async def maybe_run(self, state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not self._config.enabled:
            return None

        monitoring = state.setdefault("monitoring", {})
        record = monitoring.setdefault("cookie_liveness", {})
        now = self._now_provider()
        if not is_check_due(
            last_check_at=record.get("last_check_at"),
            now=now,
            interval_hours=self._config.interval_hours,
        ):
            return None

        result = await self.run_check(state["users"])
        previous_status = str(record.get("status") or "") or None
        last_alert_at = record.get("last_alert_at")
        alerted = False
        alert_error = None

        if should_send_cookie_alert(
            status=result.status,
            previous_status=previous_status,
            last_alert_at=str(last_alert_at) if last_alert_at else None,
            now=now,
            cooldown_hours=self._config.alert_cooldown_hours,
        ):
            if self._alerter is None:
                alert_error = "cookie 失效但未配置 hermes 微信告警"
            else:
                try:
                    await self._alerter.send(self._build_alert_message(result))
                    alerted = True
                    last_alert_at = cn_now()
                except Exception as exc:  # noqa: BLE001
                    alert_error = str(exc)

        payload = {
            "last_check_at": cn_now(),
            "status": result.status,
            "reason": result.reason,
            "signal": result.signal,
            "fresh_count": result.fresh_count,
            "newest_create_time": result.newest_create_time,
            "samples": result.samples,
            "samples_used": result.samples_used,
            "last_alert_at": last_alert_at,
            "last_error": alert_error,
            "alerted": alerted,
            "updated_at": cn_now(),
        }
        monitoring["cookie_liveness"] = payload
        monitoring["updated_at"] = cn_now()
        return payload

    async def run_check(
        self,
        users: Sequence[Dict[str, Any]],
        *,
        force_probe: bool = False,
    ) -> CookieLivenessResult:
        now = self._now_provider()
        now_ts = int(now.timestamp())
        stale_seconds = int(self._config.stale_days * SECONDS_PER_DAY)
        download_result: Optional[CookieLivenessResult] = None

        if not force_probe:
            lookback_seconds = max(stale_seconds * 2, MIN_DOWNLOAD_LOOKBACK_DAYS * SECONDS_PER_DAY)
            download_samples = collect_incremental_download_samples(
                users,
                now=now,
                lookback_seconds=lookback_seconds,
                stale_seconds=stale_seconds,
                limit=MAX_DOWNLOAD_SAMPLES,
                parse_iso_datetime=parse_iso_datetime,
            )
            download_result = evaluate_download_liveness(
                download_samples,
                stale_seconds=stale_seconds,
                min_samples=self._config.min_samples,
            )
            if download_result.status == STATUS_HEALTHY:
                return download_result

        probe_result = await self._run_probe(users, now_ts=now_ts, stale_seconds=stale_seconds)
        if download_result is None:
            return probe_result
        if download_result.status == STATUS_EXPIRED:
            return self._combine_stale_download_with_probe(download_result, probe_result)
        # download unknown → probe decides
        if probe_result.status != STATUS_UNKNOWN:
            return probe_result
        return download_result

    async def _run_probe(
        self,
        users: Sequence[Dict[str, Any]],
        *,
        now_ts: int,
        stale_seconds: int,
    ) -> CookieLivenessResult:
        probe_users = select_probe_users(users, self._config.sample_user_count)
        samples: List[Dict[str, Any]] = []
        for user in probe_users:
            samples.append(await self._probe_user(user))
        return evaluate_cookie_liveness(
            samples,
            now_ts=now_ts,
            stale_seconds=stale_seconds,
            min_samples=self._config.min_samples,
        )

    def _combine_stale_download_with_probe(
        self,
        download_result: CookieLivenessResult,
        probe_result: CookieLivenessResult,
    ) -> CookieLivenessResult:
        merged = list(download_result.samples) + list(probe_result.samples)
        if probe_result.status == STATUS_HEALTHY:
            return CookieLivenessResult(
                status=STATUS_DEGRADED,
                reason=f"{download_result.reason}; 探针可达",
                samples_used=download_result.samples_used,
                newest_create_time=probe_result.newest_create_time
                or download_result.newest_create_time,
                samples=merged,
                signal=SIGNAL_COMBINED,
                fresh_count=0,
            )
        if probe_result.status == STATUS_EXPIRED:
            return CookieLivenessResult(
                status=STATUS_EXPIRED,
                reason=f"{download_result.reason}; {probe_result.reason}",
                samples_used=download_result.samples_used,
                newest_create_time=download_result.newest_create_time,
                samples=merged,
                signal=SIGNAL_COMBINED,
                fresh_count=0,
            )
        # Strong stale-download signal + inconclusive probe: keep attention via degraded.
        return CookieLivenessResult(
            status=STATUS_DEGRADED,
            reason=f"{download_result.reason}; {probe_result.reason}",
            samples_used=download_result.samples_used,
            newest_create_time=download_result.newest_create_time,
            samples=merged,
            signal=SIGNAL_COMBINED,
            fresh_count=0,
        )

    async def _probe_user(self, user: Dict[str, Any]) -> Dict[str, Any]:
        user_id = str(user.get("id") or "")
        nickname = str(user.get("nickname") or "")
        sec_user_id = str(user.get("sec_user_id") or "")
        try:
            data = await self._crawler.fetch_user_post_videos(
                sec_user_id,
                max_cursor=0,
                count=SAMPLE_POST_COUNT,
            )
            aweme_list = data.get("aweme_list") if isinstance(data, dict) else None
            latest = extract_latest_create_time(aweme_list)
            list_len = len(aweme_list) if isinstance(aweme_list, list) else 0
            reachable = list_len > 0
            # Empty list is a common silent-expiry shape (HTTP 200, no exception).
            empty_error = None if reachable else "empty aweme_list"
            return {
                "kind": "probe",
                "user_id": user_id,
                "nickname": nickname,
                "sec_user_id": sec_user_id,
                "latest_create_time": latest,
                "aweme_count": list_len,
                "reachable": reachable,
                "error": empty_error,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "kind": "probe",
                "user_id": user_id,
                "nickname": nickname,
                "sec_user_id": sec_user_id,
                "latest_create_time": None,
                "aweme_count": 0,
                "reachable": False,
                "error": str(exc),
            }

    def _build_alert_message(self, result: CookieLivenessResult) -> str:
        newest_text = "-"
        if result.newest_create_time is not None:
            newest_text = datetime.fromtimestamp(
                result.newest_create_time,
                tz=timezone(timedelta(hours=8)),
            ).isoformat(timespec="seconds")
        lag_stats = summarize_download_lag(result.samples)
        lag_line = self._format_lag_stats_line(lag_stats)
        sample_lines = []
        for item in result.samples[:5]:
            if item.get("kind") == "download" or "lag_seconds" in item:
                lag = item.get("lag_seconds")
                lag_text = "-" if lag is None else f"{int(lag) // SECONDS_PER_DAY}d"
                sample_lines.append(
                    f"- {item.get('nickname') or item.get('user_id')}: "
                    f"aweme={item.get('aweme_id') or '-'} lag={lag_text}"
                )
                continue
            reachable = item.get("reachable")
            err = item.get("error")
            if err:
                suffix = f" error={err}"
            elif reachable:
                suffix = " reachable=true"
            else:
                suffix = " reachable=false"
            sample_lines.append(f"- {item.get('nickname') or item.get('user_id')}:{suffix}")
        lines = [
            "【抖音监控】Cookie 疑似失效",
            f"状态: {result.status}",
            f"原因: {result.reason}",
            f"信号: {result.signal}",
            f"有效样本: {result.samples_used}",
            lag_line,
            f"最新作品时间: {newest_text}",
            "样本:",
            *sample_lines,
            "请尽快更新上游 Douyin Cookie。",
        ]
        return "\n".join(lines)

    @staticmethod
    def _format_lag_stats_line(stats: Dict[str, Any]) -> str:
        count = int(stats.get("count") or 0)
        if count <= 0:
            return "增量lag: 无样本"
        def _days(value: Any) -> str:
            if value is None:
                return "-"
            return f"{int(value) // SECONDS_PER_DAY}d"
        return (
            f"增量lag: n={count} fresh={stats.get('fresh_count') or 0} "
            f"p50={_days(stats.get('lag_p50_seconds'))} "
            f"p95={_days(stats.get('lag_p95_seconds'))} "
            f"max={_days(stats.get('lag_max_seconds'))}"
        )


