"""Public diagnostics never copy provider bodies or media command arguments."""

import re

from scripts.cloud_playback.d2_client import D2HTTPError
from scripts.cloud_playback.media_tools import MediaJobError

from .models import BackfillError

SOURCE_CHANGED = {
    "D2 source identity, content identity, path or size changed",
    "D2 source update timestamp changed",
}


def public_error(error):
    kind = type(error).__name__
    if isinstance(error, BackfillError):
        return {"type": kind, "code": "invalid_local_state", "message": str(error)}
    if isinstance(error, D2HTTPError):
        return {"type": kind, "code": "d2_http_error", "http_status": error.status,
                "provider_code": error.code}
    if isinstance(error, MediaJobError):
        return _media_error(error)
    return {"type": kind, "code": "operation_failed", "message": "Raw exception details withheld"}


def _media_error(error):
    message = str(error)
    if message in SOURCE_CHANGED:
        return {"type": type(error).__name__, "code": "source_changed", "message": message}
    timeout = re.fullmatch(r"(ffmpeg|ffprobe) timed out after ([0-9.e+]+) seconds", message)
    if timeout:
        return {"type": type(error).__name__, "code": "media_timeout", "tool": timeout[1],
                "timeout_seconds": float(timeout[2])}
    failed = re.match(r"(ffmpeg|ffprobe) failed \((-?[0-9]+)\):", message)
    if failed:
        return {"type": type(error).__name__, "code": "media_process_failed",
                "tool": failed[1], "exit_code": int(failed[2])}
    return {"type": type(error).__name__, "code": "media_validation_failed",
            "message": "Media or remote metadata validation failed; raw details withheld"}
