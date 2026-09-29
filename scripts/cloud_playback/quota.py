"""Wait for provider-reported D2 upload capacity without submitting media tasks."""

import time
from dataclasses import dataclass
from pathlib import Path

from .d2_client import D2Error

QUOTA_ENDPOINT = "/v6/user/get_statistics_and_quota"
DEFAULT_POLL_SECONDS = 600


def _decimal_field(value, name):
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        raise D2Error(f"D2 quota field {name} must be a nonnegative decimal string")
    return int(value)


def upload_capacity(client):
    """Read numeric capacity only; zero or unknown quota is not unlimited."""
    response = client.post(QUOTA_ENDPOINT, {})
    if not isinstance(response, dict) or not isinstance(response.get("traffic_statistics_quota"), dict):
        raise D2Error("D2 quota response lacks traffic_statistics_quota object")
    traffic = response["traffic_statistics_quota"]
    quota = _decimal_field(traffic.get("daily_bytes_upload_quota"), "daily_bytes_upload_quota")
    # D2's proto3 TrafficStatisticsAndQuota omits bytes_uploaded_today when zero.
    # https://protobuf.dev/programming-guides/json/#presence-and-default-values
    used = _decimal_field(traffic.get("bytes_uploaded_today", "0"), "bytes_uploaded_today")
    if quota <= 0:
        raise D2Error("D2 daily upload quota must be positive; zero quota semantics are unverified")
    return {"quota": quota, "used": used, "remaining": max(quota - used, 0)}


def wait_for_upload_capacity(client, report, *, poll_seconds=DEFAULT_POLL_SECONDS, sleep=time.sleep, required_bytes=1):
    """Return positive capacity, without reserving it or guessing its reset time."""
    if poll_seconds <= 0:
        raise ValueError("Quota polling interval must be positive")
    if type(required_bytes) is not int or required_bytes <= 0:
        raise ValueError("Required upload size must be a positive integer")
    while True:
        capacity = upload_capacity(client)
        if required_bytes > capacity["quota"]:
            raise D2Error("Derivative exceeds the provider's entire daily upload quota")
        if capacity["remaining"] >= required_bytes:
            return capacity
        needed = {"required_bytes": required_bytes} if required_bytes != 1 else {}
        report({"status": "waiting_for_upload_quota", **capacity, **needed, "poll_seconds": poll_seconds})
        sleep(poll_seconds)


@dataclass(frozen=True, kw_only=True)
class UploadWithQuota:
    upload: object
    report: object
    poll_seconds: int = DEFAULT_POLL_SECONDS

    def __call__(self, client, source_path, destination):
        def report(event):
            self.report({**event, "upload_destination": destination})

        capacity = wait_for_upload_capacity(
            client, report, poll_seconds=self.poll_seconds, required_bytes=Path(source_path).stat().st_size,
        )
        report({"status": "processing", **capacity})
        return self.upload(client, source_path, destination)
