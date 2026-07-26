from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

CHINA_TZ = timezone(timedelta(hours=8))


def cn_now() -> str:
    return datetime.now(CHINA_TZ).isoformat(timespec="milliseconds")


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
