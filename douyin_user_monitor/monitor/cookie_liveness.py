from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Protocol, Sequence

from douyin_user_monitor.monitor.profile_parser import ACCOUNT_STATUS_NORMAL

STATUS_HEALTHY = "healthy"
STATUS_EXPIRED = "expired"
STATUS_UNKNOWN = "unknown"
STATUS_DEGRADED = "degraded"
SIGNAL_DOWNLOAD = "download"
SIGNAL_PROBE = "probe"
SIGNAL_COMBINED = "combined"
SECONDS_PER_DAY = 86400
SAMPLE_POST_COUNT = 5

class CookieProbeCrawler(Protocol):
    async def fetch_user_post_videos(
        self,
        sec_user_id: str,
        max_cursor: int,
        count: int,
    ) -> Dict[str, Any]:
        ...

class CookieAlertSender(Protocol):
    async def send(self, message: str) -> None:
        ...

@dataclass(frozen=True)
class CookieLivenessConfig:
    enabled: bool
    interval_hours: float
    stale_days: float
    sample_user_count: int
    min_samples: int
    alert_cooldown_hours: float

@dataclass(frozen=True)
class CookieLivenessResult:
    status: str
    reason: str
    samples_used: int
    newest_create_time: Optional[int]
    samples: List[Dict[str, Any]]
    signal: str = SIGNAL_PROBE
    fresh_count: int = 0

def evaluate_cookie_liveness(
    samples: Sequence[Dict[str, Any]],
    *,
    now_ts: int,
    stale_seconds: int,
    min_samples: int,
) -> CookieLivenessResult:
    """Judge probe samples by reachability, not post freshness.

    ``now_ts`` / ``stale_seconds`` are kept for call-site compatibility and
    only used for informational newest_create_time context.
    """
    _ = now_ts, stale_seconds
    reachable = [item for item in samples if _probe_reachable(item)]
    failed = [item for item in samples if _probe_failed(item)]
    newest = _max_create_time(
        [item for item in samples if item.get("error") is None and _probe_reachable(item)]
    )
    used = len(reachable)

    if len(reachable) >= min_samples:
        return CookieLivenessResult(
            status=STATUS_HEALTHY,
            reason=f"探针可达 {len(reachable)}/{min_samples}",
            samples_used=used,
            newest_create_time=newest,
            samples=list(samples),
            signal=SIGNAL_PROBE,
        )

    if len(reachable) == 0 and len(failed) >= min_samples:
        return CookieLivenessResult(
            status=STATUS_EXPIRED,
            reason=f"探针均失败 {len(failed)}/{min_samples}",
            samples_used=0,
            newest_create_time=newest,
            samples=list(samples),
            signal=SIGNAL_PROBE,
        )

    return CookieLivenessResult(
        status=STATUS_UNKNOWN,
        reason=(
            f"探针结论不足: reachable={len(reachable)} "
            f"failed={len(failed)} need={min_samples}"
        ),
        samples_used=used,
        newest_create_time=newest,
        samples=list(samples),
        signal=SIGNAL_PROBE,
    )

def _probe_reachable(item: Dict[str, Any]) -> bool:
    if item.get("error") is not None:
        return False
    if "reachable" in item:
        return bool(item.get("reachable"))
    # Backward-compatible: success sample with create_time counts as reachable.
    return isinstance(item.get("latest_create_time"), int)

def _probe_failed(item: Dict[str, Any]) -> bool:
    """Exception, empty list, or explicit unreachable without success."""
    if item.get("error") is not None:
        return True
    if _probe_reachable(item):
        return False
    # Empty aweme_list / reachable=false with no error is a probe failure.
    if "reachable" in item or item.get("kind") == "probe":
        return True
    return False

def evaluate_download_liveness(
    samples: Sequence[Dict[str, Any]],
    *,
    stale_seconds: int,
    min_samples: int,
) -> CookieLivenessResult:
    """Judge cookie health from incremental download publish→download lag."""
    usable = [item for item in samples if isinstance(item.get("lag_seconds"), (int, float))]
    samples_used = len(usable)
    newest = _max_publish_time(usable)
    days = max(1, stale_seconds // SECONDS_PER_DAY)
    fresh = [item for item in usable if bool(item.get("is_fresh"))]
    fresh_count = len(fresh)

    if samples_used == 0:
        return CookieLivenessResult(
            status=STATUS_UNKNOWN,
            reason="无增量下载样本",
            samples_used=0,
            newest_create_time=newest,
            samples=list(samples),
            signal=SIGNAL_DOWNLOAD,
            fresh_count=0,
        )

    if fresh:
        return CookieLivenessResult(
            status=STATUS_HEALTHY,
            reason=f"存在发布-下载间隔≤{days}天的增量下载",
            samples_used=samples_used,
            newest_create_time=newest,
            samples=list(samples),
            signal=SIGNAL_DOWNLOAD,
            fresh_count=fresh_count,
        )

    if samples_used < min_samples:
        return CookieLivenessResult(
            status=STATUS_UNKNOWN,
            reason=f"增量下载样本不足: {samples_used}/{min_samples}",
            samples_used=samples_used,
            newest_create_time=newest,
            samples=list(samples),
            signal=SIGNAL_DOWNLOAD,
            fresh_count=0,
        )

    # Stale-only signal: service may confirm with probe → degraded/expired.
    return CookieLivenessResult(
        status=STATUS_EXPIRED,
        reason=f"近{samples_used}条增量下载发布-下载间隔均>{days}天",
        samples_used=samples_used,
        newest_create_time=newest,
        samples=list(samples),
        signal=SIGNAL_DOWNLOAD,
        fresh_count=0,
    )

def should_send_cookie_alert(
    *,
    status: str,
    previous_status: Optional[str],
    last_alert_at: Optional[str],
    now: datetime,
    cooldown_hours: float,
) -> bool:
    if status != STATUS_EXPIRED:
        return False
    if previous_status != STATUS_EXPIRED:
        return True
    if not last_alert_at:
        return True
    last_alert = parse_iso_datetime(last_alert_at)
    if last_alert is None:
        return True
    return now - last_alert >= timedelta(hours=cooldown_hours)

def select_probe_users(users: Sequence[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    for user in users:
        if not user.get("enabled", True):
            continue
        status = str(user.get("account_status") or ACCOUNT_STATUS_NORMAL).strip()
        if status != ACCOUNT_STATUS_NORMAL:
            continue
        sec_user_id = str(user.get("sec_user_id") or "").strip()
        if not sec_user_id:
            continue
        candidates.append(user)
    # Prefer users with download activity so probes are less often empty lists.
    candidates.sort(
        key=lambda item: (
            -int(item.get("downloaded_count") or 0),
            str(item.get("id") or ""),
        )
    )
    return candidates[: max(0, limit)]

def extract_latest_create_time(aweme_list: Any) -> Optional[int]:
    if not isinstance(aweme_list, list) or not aweme_list:
        return None
    values: List[int] = []
    for item in aweme_list:
        if not isinstance(item, dict):
            continue
        raw = item.get("create_time")
        if isinstance(raw, bool):
            continue
        if isinstance(raw, (int, float)):
            values.append(int(raw))
            continue
        if isinstance(raw, str) and raw.strip().isdigit():
            values.append(int(raw.strip()))
    if not values:
        return None
    return max(values)

def is_check_due(
    *,
    last_check_at: Optional[str],
    now: datetime,
    interval_hours: float,
) -> bool:
    if not last_check_at:
        return True
    last_check = parse_iso_datetime(last_check_at)
    if last_check is None:
        return True
    return now - last_check >= timedelta(hours=interval_hours)

def _max_create_time(samples: Sequence[Dict[str, Any]]) -> Optional[int]:
    values = [
        int(item["latest_create_time"])
        for item in samples
        if isinstance(item.get("latest_create_time"), int)
    ]
    if not values:
        return None
    return max(values)

def _max_publish_time(samples: Sequence[Dict[str, Any]]) -> Optional[int]:
    values = [
        int(item["publish_ts"])
        for item in samples
        if isinstance(item.get("publish_ts"), int)
    ]
    if not values:
        return None
    return max(values)

def parse_iso_datetime(value: str) -> Optional[datetime]:
    text = value.strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone(timedelta(hours=8)))
    return parsed

def default_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=8)))

