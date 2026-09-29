"""One-frame source processing with explicit candidate and failure receipts."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from scripts.cloud_playback.d2_client import D2HTTPError
from scripts.cloud_playback.media_remote import assert_source, direct_url, get_source

from .diagnostics import public_error
from .journal import publish_cover
from .models import SCHEMA_VERSION


@dataclass(frozen=True, kw_only=True)
class Dependencies:
    make_client: object
    extract: object
    inspect: object


class LookupFailure(RuntimeError):
    def __init__(self, attempts, diagnostic):
        super().__init__("Explicit source candidate lookup failed")
        self.attempts = attempts
        self.diagnostic = diagnostic


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def find_source(client, paths):
    attempts = ()
    for path in paths:
        try:
            source = get_source(client, path)
        except Exception as error:
            missing = isinstance(error, D2HTTPError) and error.status == 404 and error.code == 5
            diagnostic = public_error(error)
            attempts += ({"path": path, "status": "missing" if missing else "failed", "error": diagnostic},)
            if not missing:
                raise LookupFailure(attempts, diagnostic) from None
            continue
        return source, (*attempts, {"path": path, "status": "selected"})
    raise LookupFailure(attempts, {"code": "sources_missing", "message": "All explicit source candidates are absent"})


def process_job(job, root, dependencies):
    identity = {**job.payload(), "schema_version": SCHEMA_VERSION, "input_revision": job.input_revision}
    source = None
    attempts = ()
    stage = "source_lookup"
    try:
        client = dependencies.make_client()
        source, attempts = find_source(client, job.source_paths)
        stage = "download_address"
        address = direct_url(client, source)
        with tempfile.TemporaryDirectory(prefix="cover-", dir=root / ".tmp") as directory:
            temporary = Path(directory) / "cover.jpg"
            stage = "extract_first_frame"
            dependencies.extract(address, temporary)
            stage = "verify_jpeg"
            image = dependencies.inspect(temporary)
            stage = "verify_source_unchanged"
            assert_source(get_source(client, source["path"]), source)
            stage = "publish_jpeg"
            publish_cover(temporary, root / job.cover_path)
        return {**identity, "status": "ready", "finished_at": utc_now(), "attempts": attempts,
                "source": source, "source_verified_after": True, "cover_path": job.cover_path, **image}
    except LookupFailure as error:
        attempts, diagnostic = error.attempts, error.diagnostic
    except Exception as error:
        diagnostic = public_error(error)
    return {**identity, "status": "failed", "finished_at": utc_now(), "stage": stage,
            "attempts": attempts, "source": source, "error": diagnostic}
